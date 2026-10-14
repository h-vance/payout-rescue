import json
import logging

import pytest

from conftest import payout_failed_event
from payout_rescue import config
from payout_rescue.llm import Completion, FakeProvider, ProviderError, Usage

GATEWAY_WRITES = {"add_external_account", "create_payout"}


class RecordingProvider:
    """Returns one fixed completion and records what the model was given."""

    def __init__(self, completion: Completion):
        self.completion, self.calls = completion, []

    def complete(self, messages, tools):
        self.calls.append((messages, tools))
        return self.completion


def model_draft_steps(app) -> list[dict]:
    return [s for s in app.store.steps_for("po_failed_1") if s["type"] == "model_draft"]


def test_a_fixable_failure_code_ends_with_a_seller_message_and_a_model_draft_step(make_app):
    app = make_app(provider=FakeProvider(["Your bank account is closed. Send new details."]))

    app.post_event(payout_failed_event())

    row = app.store.get_failed_payout("po_failed_1")
    assert row["status"] == "awaiting_seller"
    assert row["seller_message"] == "Your bank account is closed. Send new details."
    assert row["escalation_note"] is None
    (step,) = model_draft_steps(app)
    assert step["model"] == "fake:scripted"
    assert step["input_tokens"] > 0
    assert step["output_tokens"] == 8
    assert step["outputs"] == {
        "text": "Your bank account is closed. Send new details.",
        "cost_is_estimate": False,
    }


def test_the_cost_of_a_draft_is_tokens_times_the_two_config_prices(make_app, monkeypatch):
    monkeypatch.setattr(config, "INPUT_PRICE_USD_PER_MTOK", 2.0)
    monkeypatch.setattr(config, "OUTPUT_PRICE_USD_PER_MTOK", 10.0)
    completion = Completion("Draft.", Usage(1_000_000, 500_000), "claude-api:claude-opus-5-5")
    app = make_app(provider=RecordingProvider(completion))

    app.post_event(payout_failed_event())

    assert model_draft_steps(app)[0]["cost_usd"] == pytest.approx(7.0)


def test_an_invalid_currency_event_ends_with_a_note_whose_first_line_is_the_raw_code(make_app):
    app = make_app(
        failure_code="invalid_currency",
        provider=FakeProvider(["The seller's bank cannot take this currency."]),
    )

    app.post_event(payout_failed_event(failure_code="invalid_currency"))

    row = app.store.get_failed_payout("po_failed_1")
    assert row["status"] == "escalated"
    assert row["seller_message"] is None
    assert row["escalation_note"].splitlines() == [
        "failure code: invalid_currency",
        "The seller's bank cannot take this currency.",
    ]
    assert len(model_draft_steps(app)) == 1


def test_a_missing_failure_code_gives_none_given_as_the_first_line(make_app):
    app = make_app(failure_code=None, provider=FakeProvider(["No reason came from the bank."]))

    app.post_event(payout_failed_event(failure_code=None))

    note = app.store.get_failed_payout("po_failed_1")["escalation_note"]
    assert note.splitlines()[0] == "failure code: none given"


def test_when_the_provider_raises_the_escalation_and_the_raw_code_still_stand(make_app):
    app = make_app(
        failure_code="invalid_currency", provider=FakeProvider([ProviderError("model is down")])
    )

    response = app.post_event(payout_failed_event(failure_code="invalid_currency"))

    assert response.status_code == 200
    row = app.store.get_failed_payout("po_failed_1")
    assert row["status"] == "escalated"
    assert row["escalation_note"] == "failure code: invalid_currency"
    assert not GATEWAY_WRITES & {call[0] for call in app.gateway.calls}
    (step,) = model_draft_steps(app)
    assert step["outputs"] == {"error": "model is down"}
    assert step["model"] is None


def test_when_the_provider_raises_on_a_fixable_failure_code_there_is_no_seller_message(make_app):
    app = make_app(provider=FakeProvider([ProviderError("model is down")]))

    app.post_event(payout_failed_event())

    row = app.store.get_failed_payout("po_failed_1")
    assert row["status"] == "new"
    assert row["seller_message"] is None
    assert model_draft_steps(app)[0]["outputs"] == {"error": "model is down"}
    assert not GATEWAY_WRITES & {call[0] for call in app.gateway.calls}


def test_the_model_gets_the_three_objects_as_input_and_no_tools(make_app):
    provider = RecordingProvider(Completion("Draft.", Usage(10, 1), "fake:scripted"))
    app = make_app(provider=provider)

    app.post_event(payout_failed_event())

    ((messages, tools),) = provider.calls
    assert tools == []
    assert [m["role"] for m in messages] == ["system", "user"]
    given = json.loads(messages[1]["content"])
    assert given["payout"] == {
        "id": "po_failed_1",
        "amount": "125.00 USD",
        "failure_code": "account_closed",
        "failure_reason": "The bank account has been closed.",
    }
    assert given["connected_account"] == {"id": "acct_seller_1", "name": "Fake Seller"}
    assert given["external_account"] == {
        "id": "ba_fake_closed",
        "bank_name": "FAKE BANK",
        "last4": "1113",
    }
    assert model_draft_steps(app)[0]["inputs"] == {"draft": "seller_message", **given}


def test_a_provider_cost_estimate_is_written_to_the_step_and_marked(make_app):
    completion = Completion(
        "Draft.", Usage(6804, 12), "claude-cli:claude-sonnet-5-5", cost_estimate_usd=0.02726
    )
    app = make_app(provider=RecordingProvider(completion))

    app.post_event(payout_failed_event())

    (step,) = model_draft_steps(app)
    assert step["cost_usd"] == 0.02726
    assert step["outputs"]["cost_is_estimate"] is True
    assert step["model"] == "claude-cli:claude-sonnet-5-5"


def test_a_duplicate_event_does_not_draft_a_second_time(make_app):
    provider = RecordingProvider(Completion("Draft.", Usage(10, 1), "fake:scripted"))
    app = make_app(provider=provider)

    app.post_event(payout_failed_event())
    app.post_event(payout_failed_event())

    assert len(provider.calls) == 1
    assert len(model_draft_steps(app)) == 1


def test_the_config_prices_carry_the_date_they_were_set():
    assert config.INPUT_PRICE_USD_PER_MTOK > 0
    assert config.OUTPUT_PRICE_USD_PER_MTOK > 0
    assert len(config.PRICES_SET_ON) == len("2026-10-11")


def test_an_unknown_failure_code_is_given_to_the_model_as_reason_not_known(make_app):
    provider = RecordingProvider(Completion("Draft.", Usage(10, 1), "fake:scripted"))
    app = make_app(failure_code="declined", provider=provider)

    app.post_event(payout_failed_event(failure_code="declined"))

    given = json.loads(provider.calls[0][0][1]["content"])
    assert given["payout"]["failure_code"] == "declined"
    assert given["payout"]["failure_reason"] == "The reason is not known."


@pytest.mark.parametrize("blank", ["", "  \n"])
def test_a_reply_with_no_text_is_treated_as_a_failed_draft(make_app, blank):
    app = make_app(provider=RecordingProvider(Completion(blank, Usage(10, 0), "fake:scripted")))

    app.post_event(payout_failed_event())

    row = app.store.get_failed_payout("po_failed_1")
    assert row["status"] == "new"
    assert row["seller_message"] is None
    assert model_draft_steps(app)[0]["outputs"] == {"error": "The model returned no text."}


def test_a_failure_code_line_echoed_by_the_model_is_dropped_from_the_note(make_app):
    reply = "Failure code: account_closed\nThe bank cannot take this currency."
    app = make_app(failure_code="invalid_currency", provider=FakeProvider([reply]))

    app.post_event(payout_failed_event(failure_code="invalid_currency"))

    note = app.store.get_failed_payout("po_failed_1")["escalation_note"]
    assert note.splitlines() == [
        "failure code: invalid_currency",
        "The bank cannot take this currency.",
    ]


def test_a_delivery_that_crashed_before_the_draft_is_finished_by_the_retry(make_app):
    class CrashesOnce:
        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("the process died")
            return Completion("Send new details.", Usage(10, 3), "fake:scripted")

    app = make_app(provider=CrashesOnce())
    with pytest.raises(RuntimeError):
        app.post_event(payout_failed_event())
    assert app.store.get_failed_payout("po_failed_1")["status"] == "new"

    app.post_event(payout_failed_event())

    row = app.store.get_failed_payout("po_failed_1")
    assert row["status"] == "awaiting_seller"
    assert row["seller_message"] == "Send new details."
    step_types = [step["type"] for step in app.store.steps_for("po_failed_1")]
    assert step_types == ["classification", "model_draft"]


def test_with_no_llm_provider_set_the_classification_and_the_raw_code_still_stand(
    make_app, monkeypatch
):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    app = make_app(failure_code="invalid_currency", provider=None)

    response = app.post_event(payout_failed_event(failure_code="invalid_currency"))

    assert response.status_code == 200
    row = app.store.get_failed_payout("po_failed_1")
    assert row["status"] == "escalated"
    assert row["escalation_note"] == "failure code: invalid_currency"
    assert "LLM_PROVIDER" in model_draft_steps(app)[0]["outputs"]["error"]


def test_each_draft_is_written_to_one_log_line_with_the_payout_id_and_its_first_line(
    make_app, caplog
):
    caplog.set_level(logging.INFO, logger="payout_rescue.intake")
    app = make_app(provider=FakeProvider(["Hello Fake Seller,\nYour bank account is closed."]))

    app.post_event(payout_failed_event("po_failed_1"))
    app.post_event(payout_failed_event("po_failed_1"))

    (line,) = [record.getMessage() for record in caplog.records]
    assert line == "seller_message drafted for po_failed_1: Hello Fake Seller,"


def test_the_log_line_of_an_escalation_note_starts_with_the_raw_failure_code(make_app, caplog):
    caplog.set_level(logging.INFO, logger="payout_rescue.intake")
    app = make_app(failure_code="invalid_currency", provider=FakeProvider(["A person must look."]))

    app.post_event(payout_failed_event("po_failed_2", failure_code="invalid_currency"))

    (line,) = [record.getMessage() for record in caplog.records]
    assert line == "escalation_note drafted for po_failed_2: failure code: invalid_currency"


def test_a_failed_draft_writes_no_draft_log_line(make_app, caplog):
    caplog.set_level(logging.INFO, logger="payout_rescue.intake")
    app = make_app(provider=FakeProvider([ProviderError("the model is away")]))

    app.post_event(payout_failed_event())

    assert caplog.records == []
