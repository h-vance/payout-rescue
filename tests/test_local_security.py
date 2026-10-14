"""Browser requests must not operate a local sandbox from another site."""

import pytest

from conftest import payout_failed_event
from payout_rescue.llm import FakeProvider, ProviderError


@pytest.mark.parametrize("action", ["seller-fix", "approval", "draft"])
@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://another-site.example"},
        {"Origin": "null"},
        {"Origin": "http://localhost:9000"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_cross_site_actions_are_rejected_without_side_effects(make_app, action, headers):
    provider = FakeProvider([ProviderError("failed"), RuntimeError("must not call model")])
    app = make_app(provider=provider if action == "draft" else FakeProvider())
    app.post_event(payout_failed_event())
    if action == "approval":
        app.client.post("/failed-payouts/po_failed_1/seller-fix")
    before = app.store.get_failed_payout("po_failed_1")
    app.gateway.calls.clear()

    response = app.client.post(f"/failed-payouts/po_failed_1/{action}", headers=headers)

    assert response.status_code == 403
    assert app.store.get_failed_payout("po_failed_1") == before
    assert app.gateway.calls == []


def test_same_origin_browser_actions_work(make_app):
    app = make_app()
    app.post_event(payout_failed_event())
    response = app.client.post(
        "/failed-payouts/po_failed_1/seller-fix",
        headers={"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 200
    assert app.store.get_failed_payout("po_failed_1")["status"] == "awaiting_approval"


def test_an_untrusted_host_cannot_read_the_local_page(make_app):
    response = make_app().client.get("/", headers={"Host": "another-site.example"})
    assert response.status_code == 400
