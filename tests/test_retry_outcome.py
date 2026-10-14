"""What happens to a rescue when Stripe reports the outcome of its retry."""

from conftest import payout_failed_event, payout_paid_event, recorded_connect_events

RETRY_ID = "po_fake_retry_1"


def app_with_a_retry(make_app):
    app = make_app()
    app.post_event(payout_failed_event())
    app.client.post("/failed-payouts/po_failed_1/seller-fix")
    app.client.post("/failed-payouts/po_failed_1/approval")
    assert app.store.get_failed_payout("po_failed_1")["retry_payout_id"] == RETRY_ID
    return app


def outcome_steps(app) -> list[dict]:
    steps = app.store.steps_for("po_failed_1")
    return [step["outputs"] for step in steps if step["type"] == "retry"][1:]


def test_a_payout_paid_event_for_the_retry_ends_at_paid_and_adds_no_row(make_app):
    app = app_with_a_retry(make_app)

    response = app.post_event(payout_paid_event(RETRY_ID))

    assert response.status_code == 200
    assert app.store.get_failed_payout("po_failed_1")["status"] == "paid"
    assert len(app.store.list_failed_payouts()) == 1
    assert outcome_steps(app) == [{"status": "paid"}]
    assert "Paid" in app.client.get("/").text


def test_a_payout_failed_event_for_the_retry_ends_in_escalation_with_its_failure_code(make_app):
    app = app_with_a_retry(make_app)

    app.post_event(payout_failed_event(RETRY_ID, failure_code="no_account"))

    row = app.store.get_failed_payout("po_failed_1")
    assert row["status"] == "escalated"
    assert row["escalation_note"].splitlines()[0] == "failure code: no_account"
    # The retry is never taken in as a new failed payout, even with a fixable failure code.
    assert len(app.store.list_failed_payouts()) == 1
    assert outcome_steps(app) == [{"status": "failed", "failure_code": "no_account"}]
    assert "<button" not in app.client.get("/").text


def test_paid_then_failed_for_the_same_retry_ends_in_escalation(make_app):
    # Stripe sends payout.paid before payout.failed for a payout that fails.
    app = app_with_a_retry(make_app)

    app.post_event(payout_paid_event(RETRY_ID))
    assert app.store.get_failed_payout("po_failed_1")["status"] == "paid"
    app.post_event(payout_failed_event(RETRY_ID, failure_code="account_closed"))

    row = app.store.get_failed_payout("po_failed_1")
    assert row["status"] == "escalated"
    assert row["escalation_note"].splitlines()[0] == "failure code: account_closed"
    assert len(app.store.list_failed_payouts()) == 1
    assert outcome_steps(app) == [
        {"status": "paid"},
        {"status": "failed", "failure_code": "account_closed"},
    ]


def test_a_late_payout_paid_event_does_not_undo_an_escalation(make_app):
    app = app_with_a_retry(make_app)
    app.post_event(payout_failed_event(RETRY_ID, failure_code="account_closed"))

    app.post_event(payout_paid_event(RETRY_ID))

    assert app.store.get_failed_payout("po_failed_1")["status"] == "escalated"
    assert len(outcome_steps(app)) == 1


def test_the_same_retry_outcome_twice_records_one_step(make_app):
    app = app_with_a_retry(make_app)

    app.post_event(payout_paid_event(RETRY_ID))
    app.post_event(payout_paid_event(RETRY_ID))

    assert outcome_steps(app) == [{"status": "paid"}]


def test_a_payout_paid_event_for_a_payout_that_is_not_a_retry_is_ignored(make_app):
    app = make_app()

    response = app.post_event(payout_paid_event("po_someone_elses"))

    assert response.status_code == 200
    assert app.store.list_failed_payouts() == []
    assert app.gateway.calls == []


def test_the_recorded_events_of_a_failed_retry_never_show_the_rescue_as_paid(make_app):
    app = app_with_a_retry(make_app)
    paid_event, failed_event = recorded_connect_events()
    app.store.update_failed_payout(
        "po_failed_1", retry_payout_id=failed_event["data"]["object"]["id"]
    )

    # The real payout.paid event already carries the status `failed`.
    app.post_event(paid_event)
    assert app.store.get_failed_payout("po_failed_1")["status"] == "escalated"
    app.post_event(failed_event)

    row = app.store.get_failed_payout("po_failed_1")
    assert row["status"] == "escalated"
    assert row["escalation_note"].splitlines()[0] == "failure code: account_closed"
    assert len(app.store.list_failed_payouts()) == 1
    assert outcome_steps(app) == [{"status": "failed", "failure_code": "account_closed"}]
