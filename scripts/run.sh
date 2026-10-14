#!/usr/bin/env bash
# The single run command: forwards Stripe webhook events to the local app and starts the app.
# The Stripe CLI must be logged in to the sandbox "payout-rescue" (see scripts/setup-stripe.sh).
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-8000}"
WEBHOOK_URL="http://localhost:${PORT}/webhooks/stripe"

STRIPE_WEBHOOK_SECRET="$(stripe listen --print-secret)"
export STRIPE_WEBHOOK_SECRET

# Connect events go to the same URL by default, so --forward-connect-to is not needed.
stripe listen --forward-to "$WEBHOOK_URL" --events payout.failed,payout.paid &
LISTEN_PID=$!
trap 'kill "$LISTEN_PID" 2>/dev/null || true' EXIT

PORT="$PORT" uv run python -m payout_rescue
