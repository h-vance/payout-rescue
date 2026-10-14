"""Recover intake for an existing failed sandbox payout, without creating a payout.

Run: uv run python -m payout_rescue.recover <connected-account-id> <payout-id>
Uses the same .env and SQLite file as the app. No webhook listener is required.
"""

import argparse

from dotenv import load_dotenv

from payout_rescue import config
from payout_rescue.intake import take_in_payout_event
from payout_rescue.llm import Provider, ProviderError, provider_from_env
from payout_rescue.store import Store
from payout_rescue.stripe_gateway import GatewayError, StripeGateway, gateway_from_env


def recover_failed_payout(
    store: Store, gateway: StripeGateway, provider: Provider, account_id: str, payout_id: str
) -> str:
    """Read Stripe's current facts and use normal intake, including its duplicate guards."""
    payout = gateway.get_payout(account_id, payout_id)
    if payout.get("status") != "failed":
        raise GatewayError("This payout is not failed; no recovery was performed.")
    take_in_payout_event(
        store,
        gateway,
        provider,
        {
            "type": "payout.failed",
            "account": account_id,
            "data": {"object": payout},
        },
    )
    row = store.get_failed_payout(payout_id) or store.get_failed_payout_by_retry(payout_id)
    return row["status"] if row else "ignored (not a live failed payout)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("account_id")
    parser.add_argument("payout_id")
    args = parser.parse_args(argv)
    load_dotenv()
    try:
        gateway = gateway_from_env()  # Enforces the same sandbox checks as app startup.
        provider = provider_from_env()
        status = recover_failed_payout(
            Store(config.db_path()), gateway, provider, args.account_id, args.payout_id
        )
    except (GatewayError, ProviderError) as error:
        parser.exit(1, f"Recovery failed: {error}\n")
    print(f"{args.payout_id}: {status}")
    if status == "new":
        print("The draft did not finish. Open the page and use Retry draft.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
