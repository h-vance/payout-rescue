import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from payout_rescue.app import create_app
from payout_rescue.llm import FakeProvider
from payout_rescue.stripe_gateway import FakeStripeGateway

WEBHOOK_SECRET = "whsec_test_secret"
FIXTURES = Path(__file__).parent / "fixtures"


def recorded_connect_events() -> list[dict]:
    """Two real events from the sandbox for one failed payout of a connected account.

    Recorded with `stripe listen` on 2026-10-11: payout.paid, then payout.failed. Both
    carry the payout with the status `failed`.
    """
    return json.loads((FIXTURES / "connect_payout_events_for_a_failed_payout.json").read_text())


def payout_failed_event(payout_id="po_failed_1", failure_code="account_closed") -> dict:
    """A payout.failed event for a connected account, in the shape Stripe sends."""
    return _payout_event("payout.failed", payout_id, "failed", failure_code)


def payout_paid_event(payout_id: str) -> dict:
    return _payout_event("payout.paid", payout_id, "paid", None)


def _payout_event(event_type: str, payout_id: str, status: str, failure_code: str | None) -> dict:
    return {
        "id": f"evt_{payout_id}_{status}",
        "object": "event",
        "type": event_type,
        "account": "acct_seller_1",
        "livemode": False,
        "data": {
            "object": {
                "id": payout_id,
                "object": "payout",
                "amount": 12500,
                "currency": "usd",
                "status": status,
                "failure_code": failure_code,
                "destination": "ba_fake_closed",
            }
        },
    }


def stripe_signature(
    payload: str, secret: str = WEBHOOK_SECRET, timestamp: int | None = None
) -> str:
    """Sign a webhook body with Stripe's real scheme: HMAC-SHA256 over "<timestamp>.<body>"."""
    timestamp = timestamp or int(time.time())
    signature = hmac.new(secret.encode(), f"{timestamp}.{payload}".encode(), hashlib.sha256)
    return f"t={timestamp},v1={signature.hexdigest()}"


class App:
    """The app under test, with its fakes and its Store."""

    def __init__(
        self,
        tmp_path,
        failure_code="account_closed",
        webhook_secret=WEBHOOK_SECRET,
        provider=...,
        gateway=None,
    ):
        self.gateway = gateway or FakeStripeGateway(failure_code=failure_code)
        # Pass provider=None to make the app pick its provider from LLM_PROVIDER.
        self.provider = FakeProvider() if provider is ... else provider
        # The eval results the page reads. Empty unless a test writes a file here.
        self.results_dir = tmp_path / "results"
        app = create_app(
            db_path=tmp_path / "test.db",
            gateway=self.gateway,
            webhook_secret=webhook_secret,
            provider=self.provider,
            results_dir=self.results_dir,
        )
        self.store = app.state.store
        self.client = TestClient(app, base_url="http://localhost")

    def post_event(self, event: dict, signature: str | None = None):
        return self.post_body(json.dumps(event), signature)

    def post_body(self, payload: str | bytes, signature: str | None = None):
        return self.client.post(
            "/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": signature or stripe_signature(payload)},
        )


@pytest.fixture
def make_app(tmp_path):
    return lambda **kwargs: App(tmp_path, **kwargs)
