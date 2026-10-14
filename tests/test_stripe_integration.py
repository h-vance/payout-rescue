"""One rescue on the real Stripe sandbox "payout-rescue". The model is the fake provider.

Run: uv run --env-file .env pytest -m integration tests/test_stripe_integration.py -s
Skipped when STRIPE_SECRET_KEY is unset. Set STRIPE_CONNECTED_ACCOUNT to a funded
sandbox connected account.

How the events arrive: the app under test has no port, so `stripe listen` cannot reach it.
The test posts each event itself, signed, after it reads the payout's real status from Stripe
through the gateway. `./scripts/run.sh` is the path with real webhook delivery.
"""

import os
import time

import pytest

from conftest import App, payout_failed_event, payout_paid_event
from payout_rescue.stripe_gateway import gateway_from_env

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("STRIPE_SECRET_KEY"), reason="STRIPE_SECRET_KEY unset"),
]

CONNECTED_ACCOUNT = os.environ.get("STRIPE_CONNECTED_ACCOUNT", "")
AMOUNT = 1000
FUND_AMOUNT = 20000


def wait_for(read, done, seconds=120):
    deadline = time.monotonic() + seconds
    while not done(value := read()):
        if time.monotonic() > deadline:
            raise TimeoutError(f"Still {value!r} after {seconds} seconds.")
        time.sleep(2)
    return value


def wait_for_balance(gateway):
    wait_for(
        lambda: gateway.available_balance(CONNECTED_ACCOUNT), lambda available: available >= AMOUNT
    )


def wait_for_outcome(gateway, payout_id) -> dict:
    return wait_for(
        lambda: gateway.get_payout(CONNECTED_ACCOUNT, payout_id),
        lambda payout: payout["status"] in ("paid", "failed"),
    )


def event_for(payout: dict) -> dict:
    """The event Stripe sends for this payout's real status, for the connected account."""
    if payout["status"] == "paid":
        event = payout_paid_event(payout["id"])
    else:
        event = payout_failed_event(payout["id"], payout["failure_code"])
    return {**event, "account": CONNECTED_ACCOUNT}


def take_in_a_real_failed_payout(app, gateway, failure_code="account_closed") -> dict:
    """Make a real failed payout on the sandbox and post its event to the app."""
    if gateway.available_balance(CONNECTED_ACCOUNT) < AMOUNT:
        gateway.fund(CONNECTED_ACCOUNT, FUND_AMOUNT)
    # The money from a destination charge reaches the balance a moment after the charge.
    wait_for_balance(gateway)
    created = gateway.create_failing_payout(CONNECTED_ACCOUNT, AMOUNT, failure_code)
    failed = wait_for_outcome(gateway, created["id"])
    assert (failed["status"], failed["failure_code"]) == ("failed", failure_code)
    app.post_event(event_for(failed))
    return failed


def finish_the_rescue(app, gateway, payout_id) -> dict:
    """The seller fix, two approval posts, then the real outcome of the retry."""
    app.client.post(f"/failed-payouts/{payout_id}/seller-fix")
    # The failed payout's money comes back to the balance a moment after it fails.
    wait_for_balance(gateway)
    app.client.post(f"/failed-payouts/{payout_id}/approval")
    app.client.post(f"/failed-payouts/{payout_id}/approval")
    retry_id = app.store.get_failed_payout(payout_id)["retry_payout_id"]
    retry = wait_for_outcome(gateway, retry_id)
    app.post_event(event_for(retry))
    return retry


@pytest.fixture(scope="module")
def rescue(tmp_path_factory):
    """One rescue, from a real failed payout to the outcome of its retry."""
    if not CONNECTED_ACCOUNT:
        pytest.skip("STRIPE_CONNECTED_ACCOUNT unset")
    gateway = gateway_from_env()  # also checks that the key belongs to the sandbox
    started = int(time.time())
    app = App(tmp_path_factory.mktemp("db"), gateway=gateway)
    failed = take_in_a_real_failed_payout(app, gateway)
    retry = finish_the_rescue(app, gateway, failed["id"])
    print(
        f"\nfailed payout {failed['id']}, retry {retry['id']} ({retry['status']}); "
        "events posted by the test, outcome read by polling"
    )
    return app, gateway, failed["id"], started


def test_one_rescue_on_real_stripe_ends_at_paid(rescue):
    app, gateway, payout_id, _ = rescue

    row = app.store.get_failed_payout(payout_id)
    assert row["status"] == "paid"
    assert row["failure_code"] == "account_closed"
    assert row["seller_message"]
    assert gateway.get_payout(CONNECTED_ACCOUNT, row["retry_payout_id"])["status"] == "paid"
    assert [step["type"] for step in app.store.steps_for(payout_id)] == [
        "classification",
        "model_draft",
        "approval",
        "retry",
        "retry",
    ]


def test_two_approve_posts_leave_one_new_payout_in_stripe(rescue):
    app, gateway, payout_id, started = rescue
    retry_id = app.store.get_failed_payout(payout_id)["retry_payout_id"]

    # Asking Stripe again with the same idempotency key gives the same payout back.
    assert (
        gateway.create_payout(CONNECTED_ACCOUNT, AMOUNT, "usd", idempotency_key=payout_id)["id"]
        == retry_id
    )
    assert sorted(gateway.list_payout_ids(CONNECTED_ACCOUNT, created_since=started)) == sorted(
        [payout_id, retry_id]
    )
