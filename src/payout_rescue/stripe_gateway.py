"""Every Stripe call, behind one interface so unit tests never call Stripe.

`StripeGateway` is the few calls the app makes. The real gateway and the fake also have
sandbox setup calls, which only the eval run, the scripts and the integration tests use.
"""

import functools
import os
from datetime import UTC, date, datetime
from typing import Protocol

import stripe

from payout_rescue.classification import FIXABLE_FAILURE_CODES

# The fake's payouts fail with a fixable failure code unless a test asks for another one.
DEFAULT_FAILURE_CODE = min(FIXABLE_FAILURE_CODES)


SANDBOX_KEY_PREFIXES = ("sk_test_", "rk_test_")
# The sandbox's display name in the Dashboard. The account object has no other sandbox marker.
SANDBOX_DISPLAY_NAME = "payout-rescue sandbox"

# Stripe's US test external accounts: a payout to the first is paid, to the second it fails.
_ROUTING_NUMBER = "110000000"
_GOOD_ACCOUNT_NUMBER = "000123456789"
# The test external account whose payout fails with each failure code
# (https://docs.stripe.com/connect/testing#payouts).
_FAILING_ACCOUNT_NUMBERS = {
    "account_closed": "000111111113",
    "no_account": "000111111116",
    "invalid_currency": "000444444440",
    "insufficient_funds": "000222222227",
    "debit_not_authorized": "000333333335",
}
# The test debit card 4000056655665572. As a connected account's external account, its payout
# fails with `declined`.
_FAILING_CARD_TOKENS = {"declined": "tok_visa_debit_us_transferFail"}


class GatewayError(Exception):
    """Stripe refused a call, or the gateway refused to make one."""


class StripeGateway(Protocol):
    def get_payout(self, account_id: str, payout_id: str) -> dict: ...

    def get_connected_account(self, account_id: str) -> dict: ...

    def get_external_account(self, account_id: str, external_account_id: str) -> dict: ...

    def add_external_account(self, account_id: str) -> dict:
        """Add the good test external account: the seller's new bank details."""
        ...

    def create_payout(
        self, account_id: str, amount: int, currency: str, idempotency_key: str
    ) -> dict:
        """Create the retry: a new manual payout on the connected account."""
        ...


class FakeStripeGateway:
    """Returns canned objects and records every call, for unit tests."""

    def __init__(self, failure_code: str | None = DEFAULT_FAILURE_CODE):
        self.failure_code = failure_code
        self.calls: list[tuple] = []
        self._payouts_by_key: dict[str, dict] = {}
        # What the sandbox setup below has made, and what a test wants Stripe to answer.
        self._accounts: list[dict] = []
        self._failing_payouts: dict[str, dict] = {}
        self.balances: dict[str, int] = {}
        self.retry_outcome: tuple[str, str | None] = ("paid", None)

    def get_payout(self, account_id: str, payout_id: str) -> dict:
        self.calls.append(("get_payout", account_id, payout_id))
        if payout_id in self._failing_payouts:
            return self._failing_payouts[payout_id]
        for retry in self._payouts_by_key.values():
            if retry["id"] == payout_id:
                status, failure_code = self.retry_outcome
                return {**retry, "status": status, "failure_code": failure_code}
        return {
            "id": payout_id,
            "amount": 12500,
            "currency": "usd",
            "status": "failed",
            "failure_code": self.failure_code,
            "destination": "ba_fake_closed",
        }

    def get_connected_account(self, account_id: str) -> dict:
        self.calls.append(("get_connected_account", account_id))
        return {
            "id": account_id,
            "business_profile": {"name": "Fake Seller"},
            "payouts_enabled": True,
        }

    def get_external_account(self, account_id: str, external_account_id: str) -> dict:
        self.calls.append(("get_external_account", account_id, external_account_id))
        return {"id": external_account_id, "bank_name": "FAKE BANK", "last4": "1113"}

    def add_external_account(self, account_id: str) -> dict:
        self.calls.append(("add_external_account", account_id))
        return {"id": "ba_fake_good", "bank_name": "FAKE BANK", "last4": "6789"}

    def create_payout(
        self, account_id: str, amount: int, currency: str, idempotency_key: str
    ) -> dict:
        self.calls.append(("create_payout", account_id, amount, currency, idempotency_key))
        # Same key, same payout: mirrors Stripe's idempotency.
        return self._payouts_by_key.setdefault(
            idempotency_key,
            {
                "id": f"po_fake_retry_{len(self._payouts_by_key) + 1}",
                "amount": amount,
                "currency": currency,
                "status": "pending",
            },
        )

    def find_tagged_accounts(self, tag_key: str) -> list[dict]:
        self.calls.append(("find_tagged_accounts", tag_key))
        return [account for account in self._accounts if tag_key in account["metadata"]]

    def create_connected_account(self, name: str, metadata: dict) -> dict:
        self.calls.append(("create_connected_account", name, metadata))
        account = {"id": f"acct_fake_{len(self._accounts) + 1}", "metadata": metadata}
        self._accounts.append(account)
        return account

    def available_balance(self, account_id: str) -> int:
        return self.balances.get(account_id, 100_000)

    def fund(self, account_id: str, amount: int, metadata: dict | None = None) -> dict:
        self.calls.append(("fund", account_id, amount))
        self.balances[account_id] = self.available_balance(account_id) + amount
        return {"id": "pi_fake"}

    def create_failing_payout(
        self, account_id: str, amount: int, failure_code: str, metadata: dict | None = None
    ) -> dict:
        self.calls.append(("create_failing_payout", account_id, amount, failure_code))
        payout = {
            "id": f"po_fake_failed_{len(self._failing_payouts) + 1}",
            "amount": amount,
            "currency": "usd",
            "status": "failed",
            "failure_code": failure_code,
            "destination": "ba_fake_closed",
        }
        self._failing_payouts[payout["id"]] = payout
        return payout


def _stripe_errors_as_gateway_errors(method):
    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except stripe.StripeError as error:
            raise GatewayError(error.user_message or str(error)) from error

    return wrapper


def _as_dict(obj) -> dict:
    return obj.to_dict() if hasattr(obj, "to_dict") else dict(obj)


def _test_external_account(account_number: str) -> dict:
    return {
        "object": "bank_account",
        "country": "US",
        "currency": "usd",
        "routing_number": _ROUTING_NUMBER,
        "account_number": account_number,
    }


def _test_tag() -> dict:
    """Marks what the sandbox setup creates, so a person can find it in the Dashboard."""
    return {"payout_rescue_test": date.today().isoformat()}


class SdkStripeGateway:
    """The real gateway: the Stripe Python SDK with a sandbox key. It refuses any other key."""

    def __init__(self, secret_key: str, client=None):
        if not secret_key.startswith(SANDBOX_KEY_PREFIXES):
            raise GatewayError("STRIPE_SECRET_KEY is not a sandbox key (sk_test_ or rk_test_).")
        self._client = client or stripe.StripeClient(secret_key)

    @_stripe_errors_as_gateway_errors
    def check_sandbox(self) -> None:
        """Refuse to go on unless the key belongs to the sandbox "payout-rescue"."""
        account = _as_dict(self._client.v1.accounts.retrieve_current())
        name = ((account.get("settings") or {}).get("dashboard") or {}).get("display_name")
        livemode = _as_dict(self._client.v1.balance.retrieve()).get("livemode")
        if livemode is not False or name != SANDBOX_DISPLAY_NAME:
            raise GatewayError(
                f"The key belongs to {name!r} (livemode={livemode}), not the sandbox."
            )

    @_stripe_errors_as_gateway_errors
    def get_payout(self, account_id: str, payout_id: str) -> dict:
        return _as_dict(
            self._client.v1.payouts.retrieve(payout_id, options={"stripe_account": account_id})
        )

    @_stripe_errors_as_gateway_errors
    def get_connected_account(self, account_id: str) -> dict:
        return _as_dict(self._client.v1.accounts.retrieve(account_id))

    @_stripe_errors_as_gateway_errors
    def get_external_account(self, account_id: str, external_account_id: str) -> dict:
        return _as_dict(
            self._client.v1.accounts.external_accounts.retrieve(account_id, external_account_id)
        )

    @_stripe_errors_as_gateway_errors
    def add_external_account(self, account_id: str) -> dict:
        return self._add_default_external_account(account_id, _GOOD_ACCOUNT_NUMBER)

    @_stripe_errors_as_gateway_errors
    def create_payout(
        self, account_id: str, amount: int, currency: str, idempotency_key: str
    ) -> dict:
        return _as_dict(
            self._client.v1.payouts.create(
                {"amount": amount, "currency": currency},
                options={"stripe_account": account_id, "idempotency_key": idempotency_key},
            )
        )

    def _add_default_external_account(self, account_id: str, account_number: str) -> dict:
        return _as_dict(
            self._client.v1.accounts.external_accounts.create(
                account_id,
                {
                    "external_account": _test_external_account(account_number),
                    "default_for_currency": True,
                },
            )
        )

    # Sandbox setup. The app never calls these; the integration tests and the eval run use
    # them to make connected accounts and real failed payouts.

    @_stripe_errors_as_gateway_errors
    def find_tagged_accounts(self, tag_key: str) -> list[dict]:
        """Every connected account whose metadata has this key."""
        # This scans all connected accounts in the sandbox.
        accounts = self._client.v1.accounts.list({"limit": 100}).auto_paging_iter()
        return [_as_dict(a) for a in accounts if tag_key in (_as_dict(a).get("metadata") or {})]

    @_stripe_errors_as_gateway_errors
    def create_connected_account(self, name: str, metadata: dict) -> dict:
        """A connected account the platform fully controls, made with Accounts v2.

        The identity values are Stripe's test values, which pass verification in a sandbox.
        The statement descriptor comes from the business name, or Stripe keeps payouts
        restricted.
        """
        # Synthetic identity and statement descriptor use the same test business name.
        words = "".join(c for c in name if c.isalnum() or c == " ").split()
        email = f"{'-'.join(words).lower()}@example.com"
        created = self._client.raw_request(
            "post",
            "/v2/core/accounts",
            contact_email=email,
            display_name=name,
            dashboard="none",
            identity={"country": "us", "entity_type": "individual"},
            configuration={
                "merchant": {"capabilities": {"card_payments": {"requested": True}}},
                "recipient": {
                    "capabilities": {"stripe_balance": {"stripe_transfers": {"requested": True}}}
                },
            },
            defaults={
                "currency": "usd",
                "responsibilities": {
                    "fees_collector": "application",
                    "losses_collector": "application",
                },
            },
            metadata=metadata,
        )
        account_id = created.data["id"]
        self._client.raw_request(
            "post",
            f"/v2/core/accounts/{account_id}",
            configuration={
                "merchant": {
                    "mcc": "5734",
                    "statement_descriptor": {
                        "descriptor": " ".join(words).upper()[:22],
                        "prefix": "".join(words).upper()[:10],
                    },
                }
            },
            defaults={
                "profile": {
                    "business_url": "https://accessible.stripe.com",
                    "doing_business_as": name,
                    "product_description": f"{name}, a seller on Acme Market",
                }
            },
            identity={
                "attestations": {
                    "terms_of_service": {
                        "account": {
                            "date": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "ip": "8.8.8.8",
                        }
                    }
                },
                "individual": {
                    "given_name": words[0],
                    "surname": " ".join(words[1:]),
                    "email": email,
                    "phone": "0000000000",
                    "date_of_birth": {"day": 1, "month": 1, "year": 1901},
                    "address": {
                        "line1": "address_full_match",
                        "city": "San Francisco",
                        "state": "CA",
                        "postal_code": "94107",
                        "country": "us",
                    },
                    "id_numbers": [{"type": "us_ssn", "value": "000000000"}],
                },
            },
        )
        self._client.v1.accounts.update(
            account_id, {"settings": {"payouts": {"schedule": {"interval": "manual"}}}}
        )
        return {"id": account_id, "metadata": metadata}

    @_stripe_errors_as_gateway_errors
    def available_balance(self, account_id: str) -> int:
        balance = _as_dict(self._client.v1.balance.retrieve(options={"stripe_account": account_id}))
        return sum(b["amount"] for b in balance["available"] if b["currency"] == "usd")

    @_stripe_errors_as_gateway_errors
    def fund(self, account_id: str, amount: int, metadata: dict | None = None) -> dict:
        """A destination charge. The money reaches the available balance a moment later."""
        return _as_dict(
            self._client.v1.payment_intents.create(
                {
                    "amount": amount,
                    "currency": "usd",
                    "payment_method": "pm_card_bypassPending",
                    "confirm": True,
                    "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
                    "transfer_data": {"destination": account_id},
                    "metadata": metadata or _test_tag(),
                }
            )
        )

    @_stripe_errors_as_gateway_errors
    def create_failing_payout(
        self,
        account_id: str,
        amount: int,
        failure_code: str = "account_closed",
        metadata: dict | None = None,
    ) -> dict:
        """Add the test external account for this failure code, then pay out to it."""
        card = _FAILING_CARD_TOKENS.get(failure_code)
        if card:
            external_account = _as_dict(
                self._client.v1.accounts.external_accounts.create(
                    account_id, {"external_account": card}
                )
            )
        else:
            external_account = self._add_default_external_account(
                account_id, _FAILING_ACCOUNT_NUMBERS[failure_code]
            )
        return _as_dict(
            self._client.v1.payouts.create(
                {
                    "amount": amount,
                    "currency": "usd",
                    "destination": external_account["id"],
                    "metadata": metadata or _test_tag(),
                },
                options={"stripe_account": account_id},
            )
        )

    @_stripe_errors_as_gateway_errors
    def list_payout_ids(self, account_id: str, created_since: int) -> list[str]:
        payouts = self._client.v1.payouts.list(
            {"created": {"gte": created_since}, "limit": 100},
            options={"stripe_account": account_id},
        )
        return [payout.id for payout in payouts.auto_paging_iter()]


def gateway_from_env() -> StripeGateway:
    """The real gateway, checked against the sandbox. STRIPE_GATEWAY=fake gives the fake."""
    if os.environ.get("STRIPE_GATEWAY") == "fake":
        return FakeStripeGateway()
    secret_key = os.environ.get("STRIPE_SECRET_KEY")
    if not secret_key:
        raise GatewayError("STRIPE_SECRET_KEY is not set. Run scripts/setup-stripe.sh.")
    gateway = SdkStripeGateway(secret_key)
    gateway.check_sandbox()
    return gateway
