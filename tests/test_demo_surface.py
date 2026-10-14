"""The public demo surface: local assets and accessible server-rendered content."""

import re
from html.parser import HTMLParser
from urllib.parse import urlparse

from test_eval_results_on_the_page import RESULTS, page_with, text_of


class Elements(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.tags = []
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_the_demo_page_links_a_served_local_stylesheet(make_app):
    app = make_app()
    page = app.client.get("/").text
    stylesheets = [
        attrs["href"]
        for tag, attrs in Elements(page).tags
        if tag == "link" and attrs.get("rel") == "stylesheet"
    ]

    assert len(stylesheets) == 1
    assert urlparse(stylesheets[0]).path == "/static/app.css"
    response = app.client.get(stylesheets[0])
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert response.text.strip()
    assert not any(tag in {"script", "style", "noscript"} for tag, _ in Elements(page).tags)
    header = re.search(r"<header\b[^>]*>(.*?)</header>", page, re.S).group(1)
    assert "Payout Rescue" in header
    assert "Failed seller payouts, handled in minutes." in header
    assert "Stripe sandbox · payout-rescue" in header


def test_eval_results_have_labelled_facts_and_explicit_criterion_badges(make_app):
    page = page_with(make_app, RESULTS)
    facts = re.search(r'<dl\b[^>]*aria-label="The eval run"[^>]*>(.*?)</dl>', page, re.S)
    assert facts is not None
    assert re.findall(r"<dt>(.*?)</dt>", facts.group(1)) == [
        "Cases:",
        "Model:",
        "Run:",
        "Events:",
        "Fallbacks:",
        "Human steps:",
        "Model calls:",
        "Cost:",
    ]
    assert "Human steps: approval simulated, seller fix simulated" in text_of(facts.group(1))
    badges = re.findall(r'<span\b[^>]*aria-hidden="true"[^>]*>(Met|Not met)</span>', page)
    assert badges == ["Not met", "Met", "Not met"]


def test_a_payout_card_labels_its_status_and_separates_the_draft(make_app):
    from conftest import payout_failed_event
    from payout_rescue.llm import FakeProvider

    app = make_app(provider=FakeProvider(["Send new bank details."]))
    app.post_event(payout_failed_event())
    page = app.client.get("/").text
    card_header = re.search(r'<header class="payout-header">(.*?)</header>', page, re.S)
    assert card_header is not None
    assert "acct_seller_1" in card_header.group(1)
    assert "Waiting for the seller" in card_header.group(1)
    assert 'data-status="awaiting_seller"' in card_header.group(1)
    letter = re.search(r'<section class="letter-panel"[^>]*>(.*?)</section>', page, re.S)
    assert letter is not None
    assert "Seller message Send new bank details." in text_of(letter.group(1))


def test_trace_keeps_eight_columns_and_marks_failed_retry_outcomes(make_app):
    from conftest import payout_failed_event

    app = make_app()
    app.post_event(payout_failed_event())
    app.client.post("/failed-payouts/po_failed_1/seller-fix")
    app.client.post("/failed-payouts/po_failed_1/approval")
    app.post_event(payout_failed_event("po_fake_retry_1"))
    page = app.client.get("/").text
    trace = re.search(r'<table class="trace-table">(.*?)</table>', page, re.S)
    assert trace is not None
    assert re.findall(r'<th scope="col">(.*?)</th>', trace.group(1)) == [
        "Time (UTC)", "Step", "By", "Inputs", "Outputs", "Model", "Tokens", "Cost",
    ]
    cells = [attrs for tag, attrs in Elements(trace.group(1)).tags if tag == "td"]
    assert any(cell.get("data-step") == "approval" for cell in cells)
    assert any(
        cell.get("data-step") == "retry" and cell.get("data-outcome") == "failed"
        for cell in cells
    )


def test_the_cost_explanation_is_in_the_page_footer(make_app):
    from conftest import payout_failed_event

    app = make_app()
    app.post_event(payout_failed_event())
    page = app.client.get("/").text
    footer = re.search(r"<footer[^>]*>(.*?)</footer>", page, re.S)
    assert footer is not None
    assert 'A cost marked "estimate" is the model provider\'s own estimate.' in text_of(
        footer.group(1)
    )
    assert page.index("</article>") < page.index("<footer")
