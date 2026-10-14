"""The real gateway, with a stand-in for the Stripe client. No call leaves the machine."""

from types import SimpleNamespace

import pytest
import stripe

from payout_rescue.stripe_gateway import (
    FakeStripeGateway,
    GatewayError,
    SdkStripeGateway,
    gateway_from_env,
)


class Recorder:
    """Stands in for any part of stripe.StripeClient and records the calls made on it."""

    def __init__(self, calls=None, path=(), error=None):
        self.calls = [] if calls is None else calls
        self._path, self._error = path, error

    def __getattr__(self, name):
        return Recorder(self.calls, (*self._path, name), self._error)

    def __call__(self, *args, **kwargs):
        self.calls.append((".".join(self._path), args, kwargs))
        if self._error:
            raise self._error
        return {"id": "obj_1"}


@pytest.mark.parametrize("key", ["sk_live_abc", "rk_live_abc", "", "pk_test_abc"])
def test_the_gateway_refuses_a_key_that_is_not_a_sandbox_key(key):
    with pytest.raises(GatewayError):
        SdkStripeGateway(key, client=Recorder())


@pytest.mark.parametrize("key", ["sk_test_abc", "rk_test_abc"])
def test_the_gateway_accepts_a_sandbox_key(key):
    SdkStripeGateway(key, client=Recorder())


def test_the_retry_is_a_payout_on_the_connected_account_with_the_idempotency_key():
    client = Recorder()

    SdkStripeGateway("sk_test_abc", client=client).create_payout(
        "acct_seller_1", 12500, "usd", idempotency_key="po_failed_1"
    )

    assert client.calls == [
        (
            "v1.payouts.create",
            ({"amount": 12500, "currency": "usd"},),
            {"options": {"stripe_account": "acct_seller_1", "idempotency_key": "po_failed_1"}},
        )
    ]


def test_the_seller_fix_adds_the_good_test_external_account_as_the_default():
    client = Recorder()

    SdkStripeGateway("sk_test_abc", client=client).add_external_account("acct_seller_1")

    ((name, (account_id, params), _),) = client.calls
    assert (name, account_id) == ("v1.accounts.external_accounts.create", "acct_seller_1")
    assert params["default_for_currency"] is True
    assert params["external_account"]["routing_number"] == "110000000"
    assert params["external_account"]["account_number"] == "000123456789"


def test_a_stripe_error_becomes_a_gateway_error_with_stripes_message():
    client = Recorder(error=stripe.InvalidRequestError("No such payout", param="id"))

    with pytest.raises(GatewayError, match="No such payout"):
        SdkStripeGateway("sk_test_abc", client=client).get_payout("acct_seller_1", "po_x")


def test_the_gateway_from_env_is_the_fake_only_when_asked(monkeypatch):
    monkeypatch.setenv("STRIPE_GATEWAY", "fake")
    assert isinstance(gateway_from_env(), FakeStripeGateway)


def test_the_gateway_from_env_needs_a_secret_key(monkeypatch):
    monkeypatch.delenv("STRIPE_GATEWAY", raising=False)
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    with pytest.raises(GatewayError, match="STRIPE_SECRET_KEY"):
        gateway_from_env()


def test_a_failing_payout_goes_to_the_test_external_account_for_its_failure_code():
    client = Recorder()

    SdkStripeGateway("sk_test_abc", client=client).create_failing_payout(
        "acct_seller_1", 1000, "invalid_currency"
    )

    (_, (_, external), _), (name, _, _) = client.calls
    assert external["external_account"]["account_number"] == "000444444440"
    assert name == "v1.payouts.create"


def sandbox_client(display_name: str, livemode: bool) -> SimpleNamespace:
    account = {"id": "acct_platform", "settings": {"dashboard": {"display_name": display_name}}}
    return SimpleNamespace(
        v1=SimpleNamespace(
            accounts=SimpleNamespace(retrieve_current=lambda: account),
            balance=SimpleNamespace(retrieve=lambda: {"livemode": livemode}),
        )
    )


def test_the_sandbox_check_passes_for_the_sandbox_payout_rescue():
    client = sandbox_client("payout-rescue sandbox", livemode=False)

    SdkStripeGateway("sk_test_abc", client=client).check_sandbox()


@pytest.mark.parametrize(
    ("display_name", "livemode"),
    [("another sandbox", False), ("payout-rescue sandbox", True), ("payout-rescue", False)],
)
def test_the_sandbox_check_refuses_any_other_account(display_name, livemode):
    client = sandbox_client(display_name, livemode)

    with pytest.raises(GatewayError, match="not the sandbox"):
        SdkStripeGateway("sk_test_abc", client=client).check_sandbox()


def test_the_gateway_from_env_is_the_real_one_and_is_checked_before_use(monkeypatch):
    checked = []
    monkeypatch.delenv("STRIPE_GATEWAY", raising=False)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
    monkeypatch.setattr(SdkStripeGateway, "check_sandbox", lambda self: checked.append(self))

    gateway = gateway_from_env()

    assert isinstance(gateway, SdkStripeGateway)
    assert checked == [gateway]
