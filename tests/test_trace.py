"""The trace on the page: one row per step under each failed payout, oldest first."""

import html
import re
import time
from types import SimpleNamespace

from conftest import FIXTURES, payout_failed_event, payout_paid_event
from payout_rescue import config
from payout_rescue.llm import ClaudeCliProvider, Completion, FakeProvider, ProviderError, Usage

CLI_REFERENCE = (FIXTURES / "claude_cli_reference.json").read_text()
COLUMNS = ["time", "step", "by", "inputs", "outputs", "model", "tokens", "cost"]


def reference_cli_provider() -> ClaudeCliProvider:
    """The claude-cli provider, answering with the synthetic reference reply."""
    return ClaudeCliProvider(
        runner=lambda argv, **kwargs: SimpleNamespace(returncode=0, stdout=CLI_REFERENCE, stderr="")
    )


class FixedProvider:
    def __init__(self, completion: Completion):
        self.completion = completion

    def complete(self, messages, tools):
        return self.completion


def rendered_trace(page: str) -> list[dict]:
    """The body rows of the page's one trace table, as a dict of cell texts per row."""
    body = re.search(r"<tbody>(.*?)</tbody>", page, re.S).group(1)
    rows = []
    for row in re.findall(r"<tr>(.*?)</tr>", body, re.S):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
        texts = [html.unescape(re.sub(r"<[^>]+>", "", cell)).strip() for cell in cells]
        rows.append(dict(zip(COLUMNS, texts, strict=True)))
    return rows


def test_the_trace_of_a_rescue_shows_every_step_in_time_order(make_app, monkeypatch):
    app = make_app(provider=reference_cli_provider())
    create_payout = app.gateway.create_payout

    def slow_create_payout(*args, **kwargs):
        time.sleep(0.01)  # Stripe takes time, so the click is earlier than the stored retry
        return create_payout(*args, **kwargs)

    monkeypatch.setattr(app.gateway, "create_payout", slow_create_payout)
    app.post_event(payout_failed_event())
    app.client.post("/failed-payouts/po_failed_1/seller-fix")
    app.client.post("/failed-payouts/po_failed_1/approval")
    app.post_event(payout_paid_event("po_fake_retry_1"))

    page = app.client.get("/").text
    rows = rendered_trace(page)

    assert [row["step"] for row in rows] == [
        "classification",
        "model_draft",
        "approval",
        "retry",
        "retry",
    ]
    assert [row["time"] for row in rows] == sorted(row["time"] for row in rows)
    classification, draft, approval, retry, outcome = rows
    assert '"failure_code": "account_closed"' in classification["inputs"]
    assert '"classification": "rescue"' in classification["outputs"]
    # The input count includes fresh, cache-creation and cache-read tokens.
    assert draft["tokens"] == "50 in, 4 out"
    assert draft["cost"] == "$0.0010 estimate"
    assert draft["model"] == "claude-cli:claude-opus-5-5"
    assert approval["by"] == "demo operator"
    # The approval row shows the time of the click, not the time the row was stored.
    assert draft["time"] <= approval["time"] < retry["time"]
    assert '"retry_payout_id": "po_fake_retry_1"' in retry["outputs"]
    assert '"status": "paid"' in outcome["outputs"]
    # Only a model draft has a model, tokens and a cost.
    for row in (classification, approval, retry, outcome):
        assert (row["model"], row["tokens"], row["cost"]) == ("", "", "")


def test_the_trace_of_an_escalation_shows_its_two_steps_and_the_cost_arithmetic(
    make_app, monkeypatch
):
    monkeypatch.setattr(config, "INPUT_PRICE_USD_PER_MTOK", 2.0)
    monkeypatch.setattr(config, "OUTPUT_PRICE_USD_PER_MTOK", 10.0)
    monkeypatch.setattr(config, "PRICES_SET_ON", "2026-01-02")
    completion = Completion("A note.", Usage(1_000_000, 500_000), "claude-api:claude-opus-5-5")
    app = make_app(failure_code="invalid_currency", provider=FixedProvider(completion))
    app.post_event(payout_failed_event(failure_code="invalid_currency"))

    page = app.client.get("/").text
    classification, draft = rendered_trace(page)

    assert (classification["step"], draft["step"]) == ("classification", "model_draft")
    assert '"classification": "escalation"' in classification["outputs"]
    assert draft["tokens"] == "1,000,000 in, 500,000 out"
    # 1,000,000 x 2.00 + 500,000 x 10.00, per million tokens. Not an estimate.
    assert draft["cost"] == "$7.0000"
    assert "2026-01-02" in page


def test_a_draft_by_the_fake_provider_costs_nothing(make_app):
    app = make_app()
    app.post_event(payout_failed_event())

    draft = rendered_trace(app.client.get("/").text)[1]

    assert draft["model"] == "fake:scripted"
    assert draft["cost"] == "$0.00"


def test_a_failed_draft_shows_its_error_in_the_trace(make_app):
    app = make_app(provider=FakeProvider([ProviderError("the model is away")]))
    app.post_event(payout_failed_event())

    draft = rendered_trace(app.client.get("/").text)[1]

    assert "the model is away" in draft["outputs"]
    assert draft["cost"] == ""
