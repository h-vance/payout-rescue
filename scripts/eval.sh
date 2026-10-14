#!/usr/bin/env bash
# The eval run, in one command: seeds the labeled set on the Stripe sandbox "payout-rescue",
# runs every case with the provider in LLM_PROVIDER, and writes evals/results/<run id>.json.
# With a real provider this makes roughly 36 to 44 model calls.
set -euo pipefail
cd "$(dirname "$0")/.."

uv run python -m payout_rescue.eval_run
