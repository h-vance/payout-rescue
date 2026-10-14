"""One rescue and one escalation with the real provider, claude-cli, on the real sandbox.

Run: uv run --env-file .env pytest -m integration tests/test_claude_cli_integration.py -s
It makes two real model calls on the subscription login. Skipped when
STRIPE_SECRET_KEY is unset or `claude` is not on PATH.

With RECORD_REAL_DRAFTS=1 it also saves what the model wrote, and the trace rows, to
artifacts/real-drafts.json (ignored by Git). Sanitize recordings before sharing them.
Set STRIPE_CONNECTED_ACCOUNT to a funded sandbox connected account.
"""

import json
import os
import shutil
from pathlib import Path

import pytest

from conftest import App
from payout_rescue.llm import ClaudeCliProvider
from payout_rescue.stripe_gateway import gateway_from_env
from test_stripe_integration import (
    CONNECTED_ACCOUNT,
    finish_the_rescue,
    take_in_a_real_failed_payout,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("STRIPE_SECRET_KEY"), reason="STRIPE_SECRET_KEY unset"),
    pytest.mark.skipif(shutil.which("claude") is None, reason="claude is not on PATH"),
]


def draft_record(store, payout_id: str) -> dict:
    """The failed payout and its model draft step, as the Store holds them."""
    (draft,) = [step for step in store.steps_for(payout_id) if step["type"] == "model_draft"]
    return {"failed_payout": store.get_failed_payout(payout_id), "model_draft_step": draft}


@pytest.fixture(scope="module")
def real_drafts(tmp_path_factory):
    if not CONNECTED_ACCOUNT:
        pytest.skip("STRIPE_CONNECTED_ACCOUNT unset")
    gateway = gateway_from_env()
    app = App(tmp_path_factory.mktemp("db"), gateway=gateway, provider=ClaudeCliProvider())
    rescued = take_in_a_real_failed_payout(app, gateway, "account_closed")
    finish_the_rescue(app, gateway, rescued["id"])
    escalated = take_in_a_real_failed_payout(app, gateway, "invalid_currency")
    drafts = {
        "rescue": draft_record(app.store, rescued["id"]),
        "escalation": draft_record(app.store, escalated["id"]),
    }
    if os.environ.get("RECORD_REAL_DRAFTS"):
        Path("artifacts").mkdir(exist_ok=True)
        Path("artifacts/real-drafts.json").write_text(json.dumps(drafts, indent=2) + "\n")
    return drafts


def assert_drafted_by_claude_cli(step: dict) -> None:
    # The model that wrote the text, not the helper model the CLI lists first.
    assert step["model"].startswith(
        "claude-cli:claude-" + os.environ.get("CLAUDE_CLI_MODEL", "sonnet")
    )
    assert step["outputs"]["cost_is_estimate"] is True
    assert step["cost_usd"] > 0
    assert step["input_tokens"] > 2  # the summed count, not usage.input_tokens alone


def test_one_rescue_with_claude_cli_ends_at_paid(real_drafts):
    rescue = real_drafts["rescue"]

    assert rescue["failed_payout"]["status"] == "paid"
    assert (
        rescue["failed_payout"]["seller_message"] == rescue["model_draft_step"]["outputs"]["text"]
    )
    assert_drafted_by_claude_cli(rescue["model_draft_step"])


def test_a_real_invalid_currency_payout_ends_in_escalation(real_drafts):
    escalation = real_drafts["escalation"]

    assert escalation["failed_payout"]["status"] == "escalated"
    note = escalation["failed_payout"]["escalation_note"]
    assert note.splitlines()[0] == "failure code: invalid_currency"
    assert len(note.splitlines()) > 1
    assert_drafted_by_claude_cli(escalation["model_draft_step"])
