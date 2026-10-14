"""The eval seed on the real sandbox: a second run reuses the connected accounts.

Run: uv run --env-file .env pytest -m integration tests/test_eval_seed_integration.py -s
Skipped when STRIPE_SECRET_KEY is unset. On a sandbox with no eval accounts yet, the first
seed creates six and waits a few minutes for Stripe to verify them. It makes no payout.
"""

import os

import pytest

from payout_rescue.eval_seed import SELLERS, TAG_KEY, seed_accounts
from payout_rescue.stripe_gateway import gateway_from_env

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("STRIPE_SECRET_KEY"), reason="STRIPE_SECRET_KEY unset"),
]


def test_the_seed_run_twice_creates_no_new_connected_accounts():
    gateway = gateway_from_env()

    first = seed_accounts(gateway, "eval-seed-test-1")
    second = seed_accounts(gateway, "eval-seed-test-2")

    assert {account["account"] for account in second.values()} == {"reused"}
    assert [a["account_id"] for a in second.values()] == [a["account_id"] for a in first.values()]
    assert len(gateway.find_tagged_accounts(TAG_KEY)) == len(SELLERS)
