# Payout Rescue

A local Stripe Connect demo for handling failed seller payouts. It classifies the failure, drafts a seller message or escalation note, and creates a retry after an operator approves it.

`account_closed` and `no_account` can proceed to a retry after the seller's bank details are updated. Other failure codes escalate for manual review. Classification and payout actions run in code; Claude only drafts text. Each step is recorded in SQLite and shown on the page.

## Try it without external services

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --frozen
cp .env.example .env
uv run python -m payout_rescue
```

Open http://localhost:8000. In another terminal, add a simulated failed payout:

```sh
uv run --env-file .env python scripts/send_test_event.py
```

The example environment uses fake Stripe and model providers. The seller-fix button supplies test bank details; approval creates a simulated retry. Fake retries need a separate outcome event to appear as paid. The saved evaluation displayed on the page is a historical sandbox run, separate from these simulated actions.

## Use the Stripe sandbox

Create a Stripe sandbox named `payout-rescue`, enable Connect, and run `./scripts/setup-stripe.sh` to save its test key. Install the [Stripe CLI](https://docs.stripe.com/stripe-cli) and use `stripe login` to select that same sandbox.

Update `.env`:

| Variable | Value |
|---|---|
| `STRIPE_GATEWAY` | `sdk` |
| `STRIPE_SECRET_KEY` | Your sandbox's `sk_test_` or `rk_test_` key |
| `LLM_PROVIDER` | `claude-api`, `claude-cli`, or `fake` |
| `ANTHROPIC_API_KEY` | Required for `claude-api` |
| `CLAUDE_API_MODEL` | Optional API model override |
| `CLAUDE_CLI_MODEL` | Optional CLI model override; defaults to `sonnet` |

The CLI provider requires a locally installed and authenticated `claude` command. It runs with tools disabled. The API provider uses your own Anthropic API credentials. Model calls can incur charges.

```sh
./scripts/run.sh          # Starts the app and forwards signed Stripe events
./scripts/eval.sh         # Creates sandbox sellers and runs the evaluation
./scripts/fail_payout.sh  # Creates a failing sandbox payout for account_closed
./scripts/fail_payout.sh invalid_currency
```

The evaluation creates six sandbox connected accounts on its first run and reuses them later. It creates test charges and payouts, simulates the seller fix and approval, and makes roughly 36–44 model calls with a real provider. Sandbox account verification may require completing requirements in Stripe. There is no automated cleanup of the created Stripe resources.

## Recorded evaluation

The [saved run](evals/results/eval-20261011T211353Z.json) contains 18 real sandbox cases using `claude-sonnet-5-5`. Resource identifiers have been replaced with examples.

- Paid retries: 6 of 6 fixable cases.
- Accurate escalations: 12 of 12 other cases.
- Automated rescue time: median 9.3 seconds, max 9.7 seconds.
- Message accuracy: 100%.
- Message quality: 4.0 of 5.

These are historical results, not a current benchmark. Accuracy uses phrase checks with negation handling; it does not prove meaning. Quality is scored by a model. The run polled Stripe rather than receiving webhooks, and timing excludes human waiting. New runs write ignored files under `evals/results/`; the page displays the newest run.

## Tests

```sh
uv run pytest
uv run ruff check .
```

The default suite makes no Stripe or model calls. Integration tests are opt-in and create sandbox resources:

```sh
uv run --env-file .env pytest -m integration
```

Rescue integration tests also require `STRIPE_CONNECTED_ACCOUNT`, pointing to your own sandbox connected account. Raw draft recordings, when enabled with `RECORD_REAL_DRAFTS=1`, go to the ignored `artifacts/` directory.

## Recovery

Use **Retry draft** after a model failure. For intake failures, run the command logged with HTTP 503:

```sh
uv run python -m payout_rescue.recover acct_... po_...
```

Use the same environment and database as the app. Recovery creates no payout. `PAYOUT_RESCUE_DB` and `PAYOUT_RESCUE_RESULTS` override the default database and results paths.

## Limits

This is a local sandbox demo with no login. Keep it bound to loopback; it is not ready for public hosting. The gateway rejects live keys and checks the sandbox name. Webhook signatures are verified, and repeated approval requests use the original payout ID as the Stripe idempotency key.

Messages are displayed locally; nothing is sent to sellers. The seller-fix button installs Stripe test bank details. The demo assumes USD and one external account per seller. Processing is synchronous, and a paid sandbox retry is not evidence of live settlement timing. Local database files and logs can contain seller details and draft text.
