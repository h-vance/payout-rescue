#!/usr/bin/env bash
# Make one real failed payout on the Stripe sandbox "payout-rescue", for a live demo.
# Usage: ./scripts/fail_payout.sh [failure_code]     (default: account_closed)
# It uses the seller that the eval run made for that failure code, so run ./scripts/eval.sh
# once first. With ./scripts/run.sh running, the failed payout is on the page in seconds.
set -euo pipefail
cd "$(dirname "$0")/.."

uv run --env-file .env python - "${1:-account_closed}" <<'PY'
import sys

from payout_rescue.eval_seed import (
    FUND_AMOUNT,
    KIND_KEY,
    PAYOUT_AMOUNT,
    SELLERS,
    TAG_KEY,
    wait_for_all,
)
from payout_rescue.stripe_gateway import GatewayError, gateway_from_env

failure_code = sys.argv[1]
if failure_code not in SELLERS:
    sys.exit(f"Give one of: {', '.join(SELLERS)}")
try:
    gateway = gateway_from_env()
    sellers = {a["metadata"].get(KIND_KEY): a["id"] for a in gateway.find_tagged_accounts(TAG_KEY)}
    if failure_code not in sellers:
        sys.exit("No seller for this failure code yet. Run ./scripts/eval.sh once first.")
    seller = sellers[failure_code]
    # Each rescue pays out for real, so the seller's balance goes down. Top it up when short.
    if gateway.available_balance(seller) < PAYOUT_AMOUNT:
        gateway.fund(seller, FUND_AMOUNT)
        wait_for_all([seller], lambda s: gateway.available_balance(s) >= PAYOUT_AMOUNT, "funds", 120)
    payout = gateway.create_failing_payout(seller, PAYOUT_AMOUNT, failure_code)
except (GatewayError, TimeoutError) as error:
    sys.exit(str(error))
print(f"{SELLERS[failure_code]} ({seller}): payout {payout['id']} will fail.")
PY
