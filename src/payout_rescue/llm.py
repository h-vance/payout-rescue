"""The LLM seam: one function, three providers behind it."""

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Protocol

import anthropic

CLAUDE_CLI_TIMEOUT_SECONDS = 120


class ProviderError(Exception):
    """A provider could not return a completion."""


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class Completion:
    text: str
    usage: Usage
    model: str  # one string: provider and model name
    tool_calls: list[dict] = field(default_factory=list)
    # Only claude-cli reports a cost, and it is the CLI's estimate.
    cost_estimate_usd: float | None = None


class Provider(Protocol):
    def complete(self, messages: list[dict], tools: list[dict]) -> Completion: ...


def complete(messages: list[dict], tools: list[dict]) -> Completion:
    return provider_from_env().complete(messages, tools)


def provider_from_env() -> Provider:
    name = os.environ.get("LLM_PROVIDER", "")
    if name not in PROVIDERS:
        raise ProviderError(f"LLM_PROVIDER is {name!r}. Set it to one of: {', '.join(PROVIDERS)}.")
    return PROVIDERS[name]()


def _split_system(messages: list[dict]) -> tuple[str | None, list[dict]]:
    system = [m["content"] for m in messages if m["role"] == "system"]
    rest = [m for m in messages if m["role"] != "system"]
    return ("\n\n".join(system) or None), rest


class FakeProvider:
    """Returns scripted answers in order; an exception in the script is raised."""

    MODEL = "fake:scripted"
    DEFAULT_TEXT = "[fake provider] This is a scripted draft. No model wrote it."

    def __init__(self, script: list[str | Exception] | None = None):
        self._script = list(script or [])

    def complete(self, messages: list[dict], tools: list[dict]) -> Completion:
        answer = self._script.pop(0) if self._script else self.DEFAULT_TEXT
        if isinstance(answer, Exception):
            raise answer
        return Completion(
            text=answer,
            # Word counts stand in for tokens so cost arithmetic has something to work on.
            usage=Usage(
                input_tokens=sum(len(str(m["content"]).split()) for m in messages),
                output_tokens=len(answer.split()),
            ),
            model=self.MODEL,
        )


class ClaudeCliProvider:
    """The Claude Code CLI in headless mode, on the subscription login."""

    def __init__(self, runner=subprocess.run):
        self._runner = runner

    def complete(self, messages: list[dict], tools: list[dict]) -> Completion:
        system, conversation = _split_system(messages)
        if tools or system is None or [m["role"] for m in conversation] != ["user"]:
            raise ProviderError(
                "claude-cli takes one system message, one user message and no tools."
            )
        argv = [
            "claude",
            "-p",
            conversation[0]["content"],
            "--output-format",
            "json",
            "--system-prompt",
            system,
            "--model",
            os.environ.get("CLAUDE_CLI_MODEL", "sonnet"),
            "--tools",
            "",
            "--disallowedTools",
            "mcp__*",
            "--max-turns",
            "1",
            "--no-session-persistence",
        ]
        # -p prefers ANTHROPIC_API_KEY to the subscription login when the key is present.
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"ANTHROPIC_API_KEY", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"}
        }
        # Run outside the repository to avoid loading project configuration.
        with tempfile.TemporaryDirectory() as empty_dir:
            try:
                process = self._runner(
                    argv,
                    cwd=empty_dir,
                    env=env,
                    timeout=CLAUDE_CLI_TIMEOUT_SECONDS,
                    capture_output=True,
                    text=True,
                )
            except (subprocess.TimeoutExpired, OSError) as error:
                raise ProviderError(f"claude-cli did not run: {error}") from error
        if process.returncode != 0:
            raise ProviderError(
                f"claude-cli exited with {process.returncode}: {process.stderr[:500]}"
            )
        return self._parse(process.stdout)

    @staticmethod
    def _parse(stdout: str) -> Completion:
        try:
            result = json.loads(stdout)
            if result["is_error"] or result["subtype"] != "success":
                raise ProviderError(
                    f"claude-cli reported an error (subtype {result['subtype']!r})."
                )
            text, cost, models = result["result"], result["total_cost_usd"], result["modelUsage"]
            if not isinstance(text, str) or not isinstance(cost, int | float) or not models:
                raise ProviderError("claude-cli output has no text, no cost or no model name.")
            usage = result["usage"]
            return Completion(
                text=text,
                usage=Usage(
                    # usage.input_tokens alone leaves out the cached prompt.
                    input_tokens=usage["input_tokens"]
                    + usage["cache_creation_input_tokens"]
                    + usage["cache_read_input_tokens"],
                    output_tokens=usage["output_tokens"],
                ),
                # The CLI can list helper models; attribute the draft to the highest-cost one.
                model="claude-cli:"
                + max(models, key=lambda name: (models[name] or {}).get("costUSD") or 0),
                cost_estimate_usd=cost,
            )
        except (ValueError, KeyError, TypeError) as error:
            raise ProviderError(
                f"claude-cli output was not the expected JSON: {error!r}"
            ) from error


class ClaudeApiProvider:
    """Claude through the Anthropic API."""

    def __init__(self, client: anthropic.Anthropic | None = None):
        self._client = client or anthropic.Anthropic()

    def complete(self, messages: list[dict], tools: list[dict]) -> Completion:
        system, conversation = _split_system(messages)
        request = {
            "model": os.environ.get("CLAUDE_API_MODEL", "claude-opus-5-5"),
            "max_tokens": 16000,
            "output_config": {"effort": "low"},
            "messages": conversation,
            # If the model declines, the API re-runs the request on a fallback model.
            "betas": ["server-side-fallback-2026-07-01"],
            "fallbacks": "default",
        }
        if system:
            request["system"] = system
        if tools:
            request["tools"] = tools
        try:
            response = self._client.beta.messages.create(**request)
        except anthropic.APIStatusError as error:
            raise ProviderError(
                f"claude-api returned {error.status_code}: {error.message}"
            ) from error
        except anthropic.APIConnectionError as error:
            raise ProviderError(f"claude-api could not be reached: {error}") from error
        except (anthropic.AnthropicError, TypeError) as error:
            # The SDK raises a bare TypeError when it finds no API key or other credential.
            raise ProviderError(f"claude-api could not be called: {error}") from error
        if response.stop_reason == "refusal":
            raise ProviderError("claude-api refused the request.")
        usage = response.usage
        return Completion(
            text="".join(block.text for block in response.content if block.type == "text"),
            usage=Usage(
                input_tokens=usage.input_tokens
                + (usage.cache_creation_input_tokens or 0)
                + (usage.cache_read_input_tokens or 0),
                output_tokens=usage.output_tokens,
            ),
            model=f"claude-api:{response.model}",
            tool_calls=[
                {"id": block.id, "name": block.name, "input": block.input}
                for block in response.content
                if block.type == "tool_use"
            ],
        )


PROVIDERS: dict[str, type[Provider]] = {
    "fake": FakeProvider,
    "claude-cli": ClaudeCliProvider,
    "claude-api": ClaudeApiProvider,
}
