import json
import logging
import os
import sqlite3
import subprocess
import sys
import time

import pytest

from conftest import (
    payout_failed_event,
    payout_paid_event,
    recorded_connect_events,
    stripe_signature,
)
from payout_rescue.intake import take_in_failed_payout
from payout_rescue.llm import FakeProvider
from payout_rescue.recover import recover_failed_payout
from payout_rescue.rescue import approve_retry, record_seller_fix
from payout_rescue.stripe_gateway import GatewayError


@pytest.mark.parametrize("read", ["get_payout", "get_connected_account", "get_external_account"])
def test_an_intake_read_failure_names_a_command_that_recovers_the_payout(
    make_app, monkeypatch, caplog, tmp_path, read
):
    app = make_app()

    def unavailable(*args):
        raise GatewayError("Stripe unavailable")

    monkeypatch.setattr(app.gateway, read, unavailable)
    with caplog.at_level(logging.ERROR):
        response = app.post_event(payout_failed_event())

    assert response.status_code == 503
    assert "No failed payouts yet." in app.client.get("/").text
    assert "python -m payout_rescue.recover acct_seller_1 po_failed_1" in caplog.text

    env = {
        **os.environ,
        "STRIPE_GATEWAY": "fake",
        "LLM_PROVIDER": "fake",
        "PAYOUT_RESCUE_DB": str(tmp_path / "test.db"),
    }
    for _ in range(2):
        recovered = subprocess.run(
            [sys.executable, "-m", "payout_rescue.recover", "acct_seller_1", "po_failed_1"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert recovered.returncode == 0, recovered.stderr
        assert "awaiting_seller" in recovered.stdout
    page = app.client.get("/").text
    assert "Waiting for the seller" in page
    assert page.count('<th scope="row">classification</th>') == 1
    assert page.count('<th scope="row">model_draft</th>') == 1
    assert "Approve retry" not in page


@pytest.mark.parametrize("status", ["pending", "paid", "canceled"])
def test_recovery_refuses_a_payout_that_is_not_failed(make_app, monkeypatch, status):
    app = make_app(provider=FakeProvider([RuntimeError("must not draft")]))
    payout = app.gateway.get_payout("acct_seller_1", "po_failed_1")
    monkeypatch.setattr(app.gateway, "get_payout", lambda *_: {**payout, "status": status})

    with pytest.raises(GatewayError, match="not failed"):
        recover_failed_payout(app.store, app.gateway, app.provider, "acct_seller_1", "po_failed_1")

    assert "No failed payouts yet." in app.client.get("/").text
    assert not {"add_external_account", "create_payout"} & {c[0] for c in app.gateway.calls}


def test_recovery_leaves_an_eval_payout_to_the_eval_run(make_app, monkeypatch):
    app = make_app(provider=FakeProvider([RuntimeError("must not draft")]))
    payout = app.gateway.get_payout("acct_seller_1", "po_failed_1")
    monkeypatch.setattr(
        app.gateway,
        "get_payout",
        lambda *_: {
            **payout,
            "metadata": {"payout_rescue_eval": "eval-test"},
        },
    )

    result = recover_failed_payout(
        app.store, app.gateway, app.provider, "acct_seller_1", "po_failed_1"
    )

    assert result == "ignored (not a live failed payout)"
    assert "No failed payouts yet." in app.client.get("/").text


def test_recovering_a_failed_retry_escalates_the_original_payout(make_app):
    app = make_app(provider=FakeProvider(["Draft.", RuntimeError("must not draft again")]))
    app.post_event(payout_failed_event())
    app.client.post("/failed-payouts/po_failed_1/seller-fix")
    app.client.post("/failed-payouts/po_failed_1/approval")
    app.gateway.retry_outcome = ("failed", "account_closed")
    app.gateway.calls.clear()

    status = recover_failed_payout(
        app.store, app.gateway, app.provider, "acct_seller_1", "po_fake_retry_1"
    )

    assert status == "escalated"
    page = app.client.get("/").text
    assert "Escalated to a person" in page
    assert page.count('class="payout-card"') == 1
    assert "failure code: account_closed" in page
    assert not {"add_external_account", "create_payout"} & {c[0] for c in app.gateway.calls}


def test_a_bad_signature_returns_400_and_stores_nothing(make_app):
    app = make_app()

    response = app.post_event(payout_failed_event(), signature="t=1,v1=not-the-signature")

    assert response.status_code == 400
    assert app.store.get_failed_payout("po_failed_1") is None
    assert app.gateway.calls == []


def test_a_signature_made_with_another_secret_returns_400(make_app):
    app = make_app()
    event = payout_failed_event()

    response = app.post_event(event, signature=stripe_signature(json.dumps(event), "whsec_other"))

    assert response.status_code == 400


def test_a_failed_payout_with_a_fixable_failure_code_is_stored_as_a_rescue(make_app):
    app = make_app(failure_code="account_closed")

    response = app.post_event(payout_failed_event(failure_code="account_closed"))

    assert response.status_code == 200
    row = app.store.get_failed_payout("po_failed_1")
    assert row["account_id"] == "acct_seller_1"
    assert row["external_account_id"] == "ba_fake_closed"
    assert row["failure_code"] == "account_closed"
    assert (row["amount"], row["currency"]) == (12500, "usd")
    assert row["run_id"] == "live"
    assert row["status"] == "awaiting_seller"
    classification = app.store.steps_for("po_failed_1")[0]
    assert classification["type"] == "classification"
    assert classification["inputs"] == {"failure_code": "account_closed"}
    assert classification["outputs"] == {"retry_permitted": True, "classification": "rescue"}


def test_an_invalid_currency_event_ends_in_escalation(make_app):
    app = make_app(failure_code="invalid_currency")

    app.post_event(payout_failed_event(failure_code="invalid_currency"))

    assert app.store.get_failed_payout("po_failed_1")["status"] == "escalated"
    classification = app.store.steps_for("po_failed_1")[0]
    assert classification["type"] == "classification"
    assert classification["outputs"] == {"retry_permitted": False, "classification": "escalation"}


def test_a_failed_payout_with_no_failure_code_ends_in_escalation(make_app):
    app = make_app(failure_code=None)

    app.post_event(payout_failed_event(failure_code=None))

    row = app.store.get_failed_payout("po_failed_1")
    assert row["failure_code"] is None
    assert row["status"] == "escalated"
    classification = app.store.steps_for("po_failed_1")[0]
    assert classification["inputs"] == {"failure_code": None}
    assert classification["outputs"] == {"retry_permitted": False, "classification": "escalation"}


def test_the_same_event_twice_leaves_one_row_and_one_classification(make_app):
    app = make_app()

    first = app.post_event(payout_failed_event())
    second = app.post_event(payout_failed_event())

    assert (first.status_code, second.status_code) == (200, 200)
    assert len(app.store.list_failed_payouts()) == 1
    assert [s["type"] for s in app.store.steps_for("po_failed_1")].count("classification") == 1


def test_the_app_reads_the_payout_the_connected_account_and_the_external_account(make_app):
    app = make_app()

    app.post_event(payout_failed_event())

    assert [call[0] for call in app.gateway.calls] == [
        "get_payout",
        "get_connected_account",
        "get_external_account",
    ]


def test_an_event_of_another_type_is_accepted_and_ignored(make_app):
    app = make_app()

    response = app.post_event({**payout_failed_event(), "type": "payout.created"})

    assert response.status_code == 200
    assert app.store.list_failed_payouts() == []


def test_a_payout_failed_event_for_the_platform_itself_is_ignored(make_app):
    app = make_app()
    event = payout_failed_event()
    del event["account"]

    response = app.post_event(event)

    assert response.status_code == 200
    assert app.store.list_failed_payouts() == []


def test_the_webhook_refuses_every_event_when_no_secret_is_configured(make_app, monkeypatch):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    app = make_app(webhook_secret=None)

    response = app.post_event(payout_failed_event())

    assert response.status_code == 503
    assert app.store.list_failed_payouts() == []


def test_a_signature_older_than_five_minutes_returns_400(make_app):
    app = make_app()
    payload = json.dumps(payout_failed_event())
    stale = stripe_signature(payload, timestamp=int(time.time()) - 301)

    assert app.post_body(payload, signature=stale).status_code == 400


def test_a_body_that_is_not_utf8_returns_400(make_app):
    app = make_app()

    assert app.post_body(b"\xff\xfe", signature="t=1,v1=00").status_code == 400


def test_a_delivery_that_fails_half_way_stores_nothing_and_the_retry_starts_clean(
    make_app, monkeypatch
):
    app = make_app(failure_code="invalid_currency")
    event = payout_failed_event(failure_code="invalid_currency")
    real_insert = app.store._insert

    def fail_on_steps(table, row, on_conflict=""):
        if table == "steps":
            raise sqlite3.OperationalError("disk is full")
        return real_insert(table, row, on_conflict)

    monkeypatch.setattr(app.store, "_insert", fail_on_steps)
    with pytest.raises(sqlite3.OperationalError):
        app.post_event(event)
    assert app.store.get_failed_payout("po_failed_1") is None

    monkeypatch.undo()
    app.post_event(event)

    assert app.store.get_failed_payout("po_failed_1")["status"] == "escalated"
    step_types = [step["type"] for step in app.store.steps_for("po_failed_1")]
    assert step_types == ["classification", "model_draft"]


def test_a_failed_payout_with_no_external_account_is_still_stored(make_app, monkeypatch):
    app = make_app(failure_code="invalid_currency")
    payout = app.gateway.get_payout("acct_seller_1", "po_failed_1")
    monkeypatch.setattr(app.gateway, "get_payout", lambda *_: {**payout, "destination": None})

    response = app.post_event(payout_failed_event(failure_code="invalid_currency"))

    assert response.status_code == 200
    row = app.store.get_failed_payout("po_failed_1")
    assert row["external_account_id"] is None
    assert row["status"] == "escalated"


def test_a_recorded_connect_event_is_taken_in_for_the_connected_account_it_names(make_app):
    app = make_app()
    paid_event, failed_event = recorded_connect_events()
    payout_id = failed_event["data"]["object"]["id"]

    app.post_event(paid_event)
    app.post_event(failed_event)

    # The connected account id comes from the event's `account` field.
    account_id = failed_event["account"]
    assert account_id.startswith("acct_")
    assert ("get_payout", account_id, payout_id) in app.gateway.calls
    (row,) = app.store.list_failed_payouts()
    assert (row["payout_id"], row["account_id"]) == (payout_id, account_id)
    assert [step["type"] for step in app.store.steps_for(payout_id)] == [
        "classification",
        "model_draft",
    ]


def test_a_failed_payout_of_an_eval_run_is_left_to_the_eval_run(make_app):
    app = make_app()
    event = payout_failed_event("po_eval_1")
    event["data"]["object"]["metadata"] = {"payout_rescue_eval": "eval-1"}

    response = app.post_event(event)

    assert response.status_code == 200
    assert app.store.list_failed_payouts() == []
    assert app.gateway.calls == []


def test_the_outcome_of_an_eval_runs_retry_is_left_to_the_eval_run(make_app):
    app = make_app()
    take_in_failed_payout(
        app.store, app.gateway, app.provider, "acct_seller_1", "po_eval_1", run_id="eval-1"
    )
    record_seller_fix(app.store, app.gateway, "po_eval_1")
    approve_retry(app.store, app.gateway, "po_eval_1")
    retry_id = app.store.get_failed_payout("po_eval_1")["retry_payout_id"]

    app.post_event(payout_paid_event(retry_id))

    assert app.store.get_failed_payout("po_eval_1")["status"] == "retrying"
