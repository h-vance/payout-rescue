import pytest

from conftest import payout_failed_event
from payout_rescue.intake import take_in_failed_payout
from payout_rescue.llm import FakeProvider, ProviderError
from payout_rescue.stripe_gateway import GatewayError

SELLER_BUTTON = "Seller sent new bank details"
APPROVE_BUTTON = "Approve retry"
SELLER_FIX_URL = "/failed-payouts/po_failed_1/seller-fix"
APPROVAL_URL = "/failed-payouts/po_failed_1/approval"


def gateway_calls(app, name: str) -> list[tuple]:
    return [call for call in app.gateway.calls if call[0] == name]


def step_types(app) -> list[str]:
    return [step["type"] for step in app.store.steps_for("po_failed_1")]


def test_the_page_with_no_failed_payouts_says_so(make_app):
    response = make_app().client.get("/")

    assert response.status_code == 200
    assert "No failed payouts yet." in response.text
    assert "<button" not in response.text


def test_a_rescue_goes_from_seller_message_to_approval_to_retry(make_app):
    app = make_app(provider=FakeProvider(["Your bank account is closed. Send new details."]))
    app.post_event(payout_failed_event())

    page = app.client.get("/").text
    assert "po_failed_1" in page
    assert "125.00 USD" in page
    assert "account_closed" in page
    assert "Waiting for the seller" in page
    assert "Your bank account is closed. Send new details." in page
    assert SELLER_BUTTON in page
    assert APPROVE_BUTTON not in page

    response = app.client.post(SELLER_FIX_URL, follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/")
    assert app.store.get_failed_payout("po_failed_1")["status"] == "awaiting_approval"
    assert gateway_calls(app, "add_external_account") == [("add_external_account", "acct_seller_1")]
    page = app.client.get("/").text
    assert APPROVE_BUTTON in page
    assert SELLER_BUTTON not in page

    response = app.client.post(APPROVAL_URL, follow_redirects=False)
    assert response.status_code == 303
    row = app.store.get_failed_payout("po_failed_1")
    assert row["status"] == "retrying"
    assert row["retry_payout_id"] == "po_fake_retry_1"
    assert step_types(app) == ["classification", "model_draft", "approval", "retry"]
    approval, retry = app.store.steps_for("po_failed_1")[2:]
    assert approval["actor"] == "demo operator"
    assert retry["outputs"] == {"retry_payout_id": "po_fake_retry_1"}
    assert gateway_calls(app, "create_payout") == [
        ("create_payout", "acct_seller_1", 12500, "usd", "po_failed_1")
    ]
    page = app.client.get("/").text
    assert "Retry created" in page
    assert "<button" not in page


def test_two_approve_posts_make_one_retry_step_and_one_gateway_create_call(make_app):
    app = make_app()
    app.post_event(payout_failed_event())
    app.client.post(SELLER_FIX_URL)

    first = app.client.post(APPROVAL_URL, follow_redirects=False)
    second = app.client.post(APPROVAL_URL, follow_redirects=False)

    assert (first.status_code, second.status_code) == (303, 303)
    assert step_types(app).count("approval") == 1
    assert step_types(app).count("retry") == 1
    assert len(gateway_calls(app, "create_payout")) == 1


def test_an_escalation_shows_its_note_and_no_buttons(make_app):
    app = make_app(
        failure_code="invalid_currency", provider=FakeProvider(["The bank cannot take USD."])
    )
    app.post_event(payout_failed_event(failure_code="invalid_currency"))

    page = app.client.get("/").text

    assert "Escalated to a person" in page
    assert "failure code: invalid_currency" in page
    assert "The bank cannot take USD." in page
    assert "<button" not in page


def test_an_approval_does_nothing_before_the_seller_fix(make_app):
    app = make_app()
    app.post_event(payout_failed_event())

    app.client.post(APPROVAL_URL)

    assert app.store.get_failed_payout("po_failed_1")["status"] == "awaiting_seller"
    assert gateway_calls(app, "create_payout") == []
    assert "approval" not in step_types(app)


def test_the_buttons_do_nothing_for_an_escalation(make_app):
    app = make_app(failure_code="invalid_currency")
    app.post_event(payout_failed_event(failure_code="invalid_currency"))

    app.client.post(SELLER_FIX_URL)
    app.client.post(APPROVAL_URL)

    assert app.store.get_failed_payout("po_failed_1")["status"] == "escalated"
    assert gateway_calls(app, "add_external_account") == []
    assert gateway_calls(app, "create_payout") == []


def test_the_seller_fix_twice_adds_one_external_account(make_app):
    app = make_app()
    app.post_event(payout_failed_event())

    app.client.post(SELLER_FIX_URL)
    app.client.post(SELLER_FIX_URL)

    assert len(gateway_calls(app, "add_external_account")) == 1


@pytest.mark.parametrize(
    "url",
    ["/failed-payouts/po_unknown/seller-fix", "/failed-payouts/po_unknown/approval"],
)
def test_a_button_for_an_unknown_payout_returns_404(make_app, url):
    assert make_app().client.post(url).status_code == 404


def test_the_drafted_text_is_escaped_on_the_page(make_app):
    app = make_app(provider=FakeProvider(["<script>alert(1)</script> Send new details."]))
    app.post_event(payout_failed_event())

    page = app.client.get("/").text

    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_a_failed_draft_can_be_retried_without_moving_money(make_app):
    app = make_app(provider=FakeProvider([ProviderError("model is down"), "Send new details."]))
    app.post_event(payout_failed_event())

    page = app.client.get("/").text

    assert "The draft failed" in page
    assert "Retry draft" in page
    assert SELLER_BUTTON not in page
    assert APPROVE_BUTTON not in page

    response = app.client.post("/failed-payouts/po_failed_1/draft")

    assert response.status_code == 200
    assert "Waiting for the seller" in response.text
    assert "Send new details." in response.text
    assert SELLER_BUTTON in response.text
    assert "Retry draft" not in response.text
    assert "model is down" in response.text  # The failed attempt stays in the trace.
    assert not gateway_calls(app, "add_external_account")
    assert not gateway_calls(app, "create_payout")


def test_draft_retry_keeps_errors_and_stops_after_success(make_app):
    app = make_app(
        provider=FakeProvider(
            [
                ProviderError("first failure"),
                ProviderError("second failure"),
                "Recovered message.",
                RuntimeError("must not draft again"),
            ]
        )
    )
    app.post_event(payout_failed_event())
    url = "/failed-payouts/po_failed_1/draft"

    failed = app.client.post(url)
    assert "Retry draft" in failed.text
    assert "second failure" in failed.text
    assert SELLER_BUTTON not in failed.text

    recovered = app.client.post(url)
    assert "Recovered message." in recovered.text
    assert "first failure" in recovered.text
    assert "second failure" in recovered.text
    assert SELLER_BUTTON in recovered.text
    assert app.client.post(url).status_code == 200
    assert app.post_event(payout_failed_event()).status_code == 200
    assert "Recovered message." in app.client.get("/").text


def test_draft_retry_does_not_change_an_escalation_or_an_unknown_payout(make_app):
    app = make_app(
        failure_code="invalid_currency",
        provider=FakeProvider(
            [
                ProviderError("model is down"),
                RuntimeError("must not draft an escalation"),
            ]
        ),
    )
    app.post_event(payout_failed_event(failure_code="invalid_currency"))

    page = app.client.post("/failed-payouts/po_failed_1/draft").text
    assert "Escalated to a person" in page
    assert "failure code: invalid_currency" in page
    assert "Retry draft" not in page
    assert app.client.post("/failed-payouts/po_missing/draft").status_code == 404


def test_when_the_gateway_refuses_the_retry_the_approval_can_be_given_again(make_app, monkeypatch):
    app = make_app()
    app.post_event(payout_failed_event())
    app.client.post(SELLER_FIX_URL)
    real_create = app.gateway.create_payout

    def refuse(*args, **kwargs):
        raise GatewayError("Stripe is down")

    monkeypatch.setattr(app.gateway, "create_payout", refuse)
    response = app.client.post(APPROVAL_URL)
    # Not a bare error page: the page, one line under that payout, and the button still there.
    assert response.status_code == 502
    assert "Stripe is down" in response.text
    assert 'role="alert"' in response.text
    assert APPROVE_BUTTON in response.text
    row = app.store.get_failed_payout("po_failed_1")
    assert (row["status"], row["retry_payout_id"]) == ("awaiting_approval", None)
    assert step_types(app) == ["classification", "model_draft"]

    monkeypatch.setattr(app.gateway, "create_payout", real_create)
    app.client.post(APPROVAL_URL)

    assert app.store.get_failed_payout("po_failed_1")["status"] == "retrying"
    assert step_types(app) == ["classification", "model_draft", "approval", "retry"]


def test_when_the_gateway_refuses_the_external_account_the_seller_fix_can_be_given_again(
    make_app, monkeypatch
):
    app = make_app()
    app.post_event(payout_failed_event())

    def refuse(*args, **kwargs):
        raise GatewayError("Stripe is down")

    monkeypatch.setattr(app.gateway, "add_external_account", refuse)
    response = app.client.post(SELLER_FIX_URL)
    assert response.status_code == 502
    assert "Stripe is down" in response.text
    assert SELLER_BUTTON in response.text

    assert app.store.get_failed_payout("po_failed_1")["status"] == "awaiting_seller"
    assert SELLER_BUTTON in app.client.get("/").text


def test_the_approval_step_is_timed_before_the_retry_step(make_app):
    app = make_app()
    app.post_event(payout_failed_event())
    app.client.post(SELLER_FIX_URL)

    app.client.post(APPROVAL_URL)

    approval, retry = app.store.steps_for("po_failed_1")[2:]
    assert approval["created_at"] <= retry["created_at"]


def test_a_payout_id_with_url_characters_gets_working_buttons(make_app):
    app = make_app()
    app.post_event(payout_failed_event(payout_id="po a?b#c"))

    page = app.client.get("/").text

    assert 'action="/failed-payouts/po%20a%3Fb%23c/seller-fix"' in page
    assert app.client.post("/failed-payouts/po%20a%3Fb%23c/seller-fix").status_code == 200
    assert app.store.get_failed_payout("po a?b#c")["status"] == "awaiting_approval"


def test_each_button_is_tied_to_its_failed_payout_for_a_screen_reader(make_app):
    app = make_app()
    app.post_event(payout_failed_event(payout_id="po_failed_1"))
    app.post_event(payout_failed_event(payout_id="po_failed_2"))

    page = app.client.get("/").text

    assert 'aria-describedby="failed-payout-1"' in page
    assert 'aria-describedby="failed-payout-2"' in page


def test_a_gateway_error_is_shown_only_under_its_own_failed_payout(make_app, monkeypatch):
    app = make_app()
    app.post_event(payout_failed_event("po_failed_1"))
    app.post_event(payout_failed_event("po_failed_2"))

    def refuse(*args, **kwargs):
        raise GatewayError("Stripe is down")

    monkeypatch.setattr(app.gateway, "add_external_account", refuse)
    page = app.client.post(SELLER_FIX_URL).text

    assert page.count("Stripe is down") == 1
    # The newest failed payout is first on the page; the error belongs to the older one.
    assert page.index("po_failed_2") < page.index("po_failed_1") < page.index("Stripe is down")


def test_the_page_lists_only_the_live_failed_payouts_not_an_eval_runs(make_app):
    app = make_app()
    app.post_event(payout_failed_event("po_failed_1"))
    take_in_failed_payout(
        app.store, app.gateway, app.provider, "acct_seller_1", "po_eval_1", run_id="eval-1"
    )

    page = app.client.get("/").text

    assert "po_failed_1" in page
    assert "po_eval_1" not in page
    assert len(app.store.list_failed_payouts()) == 2
