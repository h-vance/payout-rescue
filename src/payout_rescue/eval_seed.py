"""The eval's labeled set: six sellers, each with failed payouts of one kind.

Everything it makes on Stripe carries the metadata `payout_rescue_eval`. A second run finds
the connected accounts by that tag and reuses them. Nothing here deletes anything.
"""

import time
from collections.abc import Callable

from payout_rescue.stripe_gateway import GatewayError

TAG_KEY = "payout_rescue_eval"  # its value is the id of the run that made the object
KIND_KEY = "payout_rescue_eval_kind"

# One fictional seller for each kind of failed payout. The kind is the failure code that
# Stripe's test external account for it gives.
SELLERS = {
    "account_closed": "Juniper Candle Co.",
    "no_account": "Harbor Street Prints",
    "invalid_currency": "Larkspur Ceramics",
    "insufficient_funds": "Copper Kettle Coffee",
    "debit_not_authorized": "Northwind Bike Repair",
    # The unknown-code kind: no bank number gives a code the app has never seen, so a test
    # debit card stands in.
    "declined": "Saltmarsh Soap Works",
}
UNKNOWN_CODE_KIND = "declined"
CASES_PER_KIND = 3
PAYOUT_AMOUNT = 1000  # 10 USD
MIN_BALANCE = CASES_PER_KIND * PAYOUT_AMOUNT  # every case of a fixable kind pays out once
FUND_AMOUNT = 5000


def wait_for_all(
    items: list, ready: Callable, what: str, seconds: int = 420, pause: float = 2
) -> None:
    """Poll every item together until `ready(item)` is true for all of them."""
    deadline = time.monotonic() + seconds
    waiting = list(items)
    while waiting := [item for item in waiting if not ready(item)]:
        if time.monotonic() > deadline:
            raise TimeoutError(f"After {seconds} seconds, still waiting for {what}: {waiting}")
        time.sleep(pause)


def seed_accounts(gateway, run_id: str, sellers: dict = SELLERS, log=print) -> dict:
    """One connected account per kind, verified and funded. Says which path each one took."""
    tagged = {
        account["metadata"].get(KIND_KEY): account
        for account in gateway.find_tagged_accounts(TAG_KEY)
    }
    accounts = {}
    for kind, name in sellers.items():
        if kind in tagged:
            account_id, path = tagged[kind]["id"], "reused"
        else:
            # the tag is set by the first of several Stripe calls. If a later
            # one fails, the half-made account is still reused, and the wait below says so.
            created = gateway.create_connected_account(name, {TAG_KEY: run_id, KIND_KEY: kind})
            account_id, path = created["id"], "created"
            # Payouts are not enabled for an account with no external account.
            gateway.add_external_account(account_id)
        accounts[kind] = {"account_id": account_id, "name": name, "account": path}
        log(f"seed: {kind}: {path} {account_id} ({name})")
    ids = [account["account_id"] for account in accounts.values()]
    # Verification with Stripe's test values takes one to three minutes for a new account.
    wait_for_all(
        ids, lambda i: gateway.get_connected_account(i).get("payouts_enabled"), "payouts enabled"
    )
    for account in accounts.values():
        short = gateway.available_balance(account["account_id"]) < MIN_BALANCE
        if short:
            gateway.fund(account["account_id"], FUND_AMOUNT, {TAG_KEY: run_id})
        account["funding"] = "funded" if short else "not needed"
    # The money from a destination charge reaches the balance a moment after the charge.
    wait_for_all(ids, lambda i: gateway.available_balance(i) >= MIN_BALANCE, "funds", seconds=120)
    log(f"seed: funding: { {k: a['funding'] for k, a in accounts.items()} }")
    return accounts


def seed_failed_payouts(
    gateway, accounts: dict, run_id: str, cases_per_kind: int = CASES_PER_KIND, log=print
) -> tuple[list[dict], list[str]]:
    """Real failed payouts: the cases. Also the fallbacks taken, as sentences.

    Stripe disables an external account when a payout to it fails, so the failing external
    account is added again before each payout.
    """
    cases, fallbacks, left_out = [], [], set()
    for _ in range(cases_per_kind):
        created = []
        for kind, account in accounts.items():
            if kind in left_out:
                continue
            account_id = account["account_id"]
            # The money of the last failed payout comes back a moment after it fails.
            wait_for_all(
                [account_id],
                lambda i: gateway.available_balance(i) >= PAYOUT_AMOUNT,
                "funds",
                seconds=120,
            )
            try:
                payout = gateway.create_failing_payout(
                    account_id, PAYOUT_AMOUNT, kind, {TAG_KEY: run_id}
                )
            except GatewayError as error:
                left_out.add(kind)
                fallbacks.append(
                    f"{kind}: Stripe made no payout ({error}). Its cases are left out."
                )
                continue
            created.append((kind, account, payout["id"]))
        ids = [(account["account_id"], payout_id) for _, account, payout_id in created]
        try:
            # Only `failed` ends the wait: a payout that fails can be seen as `paid` first.
            wait_for_all(
                ids, lambda p: gateway.get_payout(*p)["status"] == "failed", "payouts to fail", 90
            )
        except TimeoutError:
            pass  # a payout that did not fail is left out below, with a sentence
        for kind, account, payout_id in created:
            payout = gateway.get_payout(account["account_id"], payout_id)
            if payout["status"] != "failed":
                fallbacks.append(f"{kind}: the payout {payout_id} did not fail. It is left out.")
                continue
            if payout["failure_code"] != kind:
                fallbacks.append(
                    f"{kind}: the payout {payout_id} failed with {payout['failure_code']}, "
                    f"not with {kind}. It is scored under the code Stripe gave."
                )
            cases.append(
                {
                    "kind": kind,
                    "seller": account["name"],
                    "account_id": account["account_id"],
                    "payout_id": payout_id,
                }
            )
            log(f"seed: {kind}: {payout_id} failed with {payout['failure_code']}")
    return cases, fallbacks
