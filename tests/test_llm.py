import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from payout_rescue.llm import (
    ClaudeApiProvider,
    ClaudeCliProvider,
    FakeProvider,
    ProviderError,
    complete,
    provider_from_env,
)

REFERENCE = (Path(__file__).parent / "fixtures" / "claude_cli_reference.json").read_text()
MESSAGES = [
    {"role": "system", "content": "You draft one short seller message."},
    {"role": "user", "content": "The payout failed with account_closed."},
]


class RecordingRunner:
    """Stands in for subprocess.run and records how the CLI would have been started."""

    def __init__(self, stdout=REFERENCE, returncode=0, raises=None):
        self.stdout, self.returncode, self.raises = stdout, returncode, raises
        self.argv, self.kwargs, self.cwd_was_empty = None, None, None

    def __call__(self, argv, **kwargs):
        self.argv, self.kwargs = argv, kwargs
        self.cwd_was_empty = os.listdir(kwargs["cwd"]) == []
        if self.raises:
            raise self.raises
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr="boom")


def cli_output(**changes) -> str:
    return json.dumps({**json.loads(REFERENCE), **changes})


# --- fake provider


def test_fake_provider_returns_scripted_text_with_usage_and_model():
    completion = FakeProvider(["Please send new bank details."]).complete(MESSAGES, [])

    assert completion.text == "Please send new bank details."
    assert completion.tool_calls == []
    assert completion.usage.input_tokens > 0
    assert completion.usage.output_tokens > 0
    assert completion.model == "fake:scripted"
    assert completion.cost_estimate_usd is None


def test_fake_provider_raises_when_scripted_to():
    with pytest.raises(ProviderError):
        FakeProvider([ProviderError("scripted failure")]).complete(MESSAGES, [])


def test_fake_provider_gives_a_default_draft_when_the_script_is_empty():
    assert FakeProvider().complete(MESSAGES, []).text


# --- provider selection


@pytest.mark.parametrize(
    ("name", "provider_class"),
    [("fake", FakeProvider), ("claude-cli", ClaudeCliProvider), ("claude-api", ClaudeApiProvider)],
)
def test_provider_is_selected_by_llm_provider(monkeypatch, name, provider_class):
    monkeypatch.setenv("LLM_PROVIDER", name)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-never-sent")

    assert isinstance(provider_from_env(), provider_class)


@pytest.mark.parametrize("value", [None, "", "gpt"])
def test_an_unset_or_unknown_llm_provider_is_refused(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
    else:
        monkeypatch.setenv("LLM_PROVIDER", value)

    with pytest.raises(ProviderError, match="fake, claude-cli, claude-api"):
        provider_from_env()


def test_complete_uses_the_provider_from_the_environment(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "fake")

    assert complete(MESSAGES, []).model == "fake:scripted"


# --- claude-cli provider


def test_claude_cli_parses_the_synthetic_reference_shape():
    completion = ClaudeCliProvider(runner=RecordingRunner()).complete(MESSAGES, [])

    assert completion.text == "OK"
    assert completion.tool_calls == []
    # input_tokens + cache_creation_input_tokens + cache_read_input_tokens, not 2.
    assert completion.usage.input_tokens == 50
    assert completion.usage.output_tokens == 4
    assert completion.model == "claude-cli:claude-opus-5-5"
    assert completion.cost_estimate_usd == 0.001


def test_claude_cli_names_the_model_that_wrote_the_text_not_the_cli_helper_model():
    # A real call on 2026-10-11 listed a small helper model first in modelUsage.
    model_usage = {
        "claude-haiku-5-5": {"outputTokens": 23, "costUSD": 0.0001679},
        "claude-sonnet-5-5": {"outputTokens": 4, "costUSD": 0.028656},
    }
    runner = RecordingRunner(stdout=cli_output(modelUsage=model_usage))

    completion = ClaudeCliProvider(runner=runner).complete(MESSAGES, [])

    assert completion.model == "claude-cli:claude-sonnet-5-5"


def test_claude_cli_keeps_the_draft_when_a_model_usage_entry_has_no_cost():
    runner = RecordingRunner(stdout=cli_output(modelUsage={"claude-a": {}, "claude-b": None}))

    completion = ClaudeCliProvider(runner=runner).complete(MESSAGES, [])

    assert completion.text == "OK"
    assert completion.model.startswith("claude-cli:claude-")


def test_claude_cli_command_has_every_adr_0012_flag_and_no_bare(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_MODEL", "haiku")
    runner = RecordingRunner()

    ClaudeCliProvider(runner=runner).complete(MESSAGES, [])

    assert runner.argv == [
        "claude",
        "-p",
        "The payout failed with account_closed.",
        "--output-format",
        "json",
        "--system-prompt",
        "You draft one short seller message.",
        "--model",
        "haiku",
        "--tools",
        "",
        "--disallowedTools",
        "mcp__*",
        "--max-turns",
        "1",
        "--no-session-persistence",
    ]
    assert "--bare" not in runner.argv
    assert runner.kwargs["timeout"] == 120
    assert runner.cwd_was_empty


def test_claude_cli_model_defaults_to_sonnet(monkeypatch):
    monkeypatch.delenv("CLAUDE_CLI_MODEL", raising=False)
    runner = RecordingRunner()

    ClaudeCliProvider(runner=runner).complete(MESSAGES, [])

    assert runner.argv[runner.argv.index("--model") + 1] == "sonnet"


def test_claude_cli_subprocess_never_gets_application_secrets(monkeypatch):
    secrets = {"ANTHROPIC_API_KEY", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"}
    for key in secrets:
        monkeypatch.setenv(key, "test-secret-never-sent")
    runner = RecordingRunner()

    ClaudeCliProvider(runner=runner).complete(MESSAGES, [])

    assert not secrets.intersection(runner.kwargs["env"])
    assert "PATH" in runner.kwargs["env"]


@pytest.mark.parametrize(
    "runner",
    [
        RecordingRunner(returncode=1),
        RecordingRunner(stdout=cli_output(is_error=True)),
        RecordingRunner(stdout=cli_output(subtype="error_max_turns")),
        RecordingRunner(stdout="not json"),
        RecordingRunner(stdout=cli_output(result=None)),
        RecordingRunner(stdout=cli_output(total_cost_usd=None)),
        RecordingRunner(stdout=cli_output(modelUsage={})),
        RecordingRunner(stdout=cli_output(usage=None)),
        RecordingRunner(raises=subprocess.TimeoutExpired("claude", 120)),
        RecordingRunner(raises=FileNotFoundError("claude")),
    ],
    ids=[
        "non-zero exit",
        "is_error",
        "subtype",
        "bad json",
        "no text",
        "no cost",
        "no model name",
        "no usage",
        "timeout",
        "claude not installed",
    ],
)
def test_claude_cli_failure_is_a_provider_error(runner):
    with pytest.raises(ProviderError):
        ClaudeCliProvider(runner=runner).complete(MESSAGES, [])


@pytest.mark.parametrize(
    ("messages", "tools"),
    [
        (MESSAGES, [{"name": "lookup"}]),
        (MESSAGES[1:], []),
        (MESSAGES + [{"role": "user", "content": "And one more thing."}], []),
    ],
    ids=["tools", "no system message", "two user messages"],
)
def test_claude_cli_accepts_only_one_system_and_one_user_message_and_no_tools(messages, tools):
    runner = RecordingRunner()

    with pytest.raises(ProviderError):
        ClaudeCliProvider(runner=runner).complete(messages, tools)

    assert runner.argv is None


# --- claude-api provider


class RecordingAnthropicClient:
    def __init__(self, stop_reason="end_turn", raises=None, tool_use=False):
        self.request = None
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self._stop_reason, self._raises, self._tool_use = stop_reason, raises, tool_use

    def _create(self, **request):
        self.request = request
        if self._raises:
            raise self._raises
        tool_use = SimpleNamespace(type="tool_use", id="toolu_1", name="lookup", input={"id": 1})
        return SimpleNamespace(
            model="claude-opus-5-5",
            stop_reason=self._stop_reason,
            content=[
                SimpleNamespace(type="thinking", thinking=""),
                SimpleNamespace(type="text", text="Please send new bank details."),
                *([tool_use] if self._tool_use else []),
            ],
            usage=SimpleNamespace(
                input_tokens=40,
                cache_creation_input_tokens=5,
                cache_read_input_tokens=5,
                output_tokens=9,
            ),
        )


def test_claude_api_returns_text_usage_and_model():
    client = RecordingAnthropicClient()

    completion = ClaudeApiProvider(client=client).complete(MESSAGES, [])

    assert completion.text == "Please send new bank details."
    assert completion.tool_calls == []
    assert completion.usage.input_tokens == 50
    assert completion.usage.output_tokens == 9
    assert completion.model == "claude-api:claude-opus-5-5"
    assert completion.cost_estimate_usd is None
    assert client.request["system"] == "You draft one short seller message."
    assert client.request["messages"] == MESSAGES[1:]
    assert "tools" not in client.request


def test_claude_api_passes_tools_on_and_returns_the_tool_calls():
    client = RecordingAnthropicClient(tool_use=True)
    tools = [{"name": "lookup", "description": "Look up", "input_schema": {"type": "object"}}]

    completion = ClaudeApiProvider(client=client).complete(MESSAGES, tools)

    assert client.request["tools"] == tools
    assert completion.tool_calls == [{"id": "toolu_1", "name": "lookup", "input": {"id": 1}}]


def test_claude_api_with_no_credential_is_a_provider_error():
    client = RecordingAnthropicClient(raises=TypeError("Could not resolve authentication method"))

    with pytest.raises(ProviderError, match="could not be called"):
        ClaudeApiProvider(client=client).complete(MESSAGES, [])


def test_claude_api_refusal_is_a_provider_error():
    with pytest.raises(ProviderError, match="refus"):
        ClaudeApiProvider(client=RecordingAnthropicClient("refusal")).complete(MESSAGES, [])


# --- integration: each calls a real model once. Run with: uv run pytest -m integration

SAY_OK = [
    {"role": "system", "content": "Reply with the single word OK."},
    {"role": "user", "content": "Say OK."},
]


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude is not on PATH")
def test_claude_cli_drafts_with_the_real_cli():
    completion = ClaudeCliProvider().complete(SAY_OK, [])

    assert "OK" in completion.text.upper()
    assert completion.model.startswith("claude-cli:claude-")
    assert completion.usage.input_tokens > 2
    assert completion.cost_estimate_usd is not None


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY is unset")
def test_claude_api_drafts_with_the_real_api():
    completion = ClaudeApiProvider().complete(SAY_OK, [])

    assert "OK" in completion.text.upper()
    assert completion.model.startswith("claude-api:claude-")
