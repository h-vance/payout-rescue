"""The eval results at the top of the page, read from the newest results file."""

import html
import json
import re

from conftest import payout_failed_event

RESULTS = {
    "run_id": "eval-20260102T030405Z",
    "provider_model": "claude-cli:claude-sonnet-5-5",
    "cases": 17,
    "message_accuracy": 16 / 17,
    "message_quality": 3.8235,
    "by_failure_code": {
        "account_closed": {"cases": 3, "message_accuracy": 2 / 3, "message_quality": 4.6667},
        "invalid_currency": {"cases": 14, "message_accuracy": 1.0, "message_quality": 3.5},
    },
    "criteria": {
        "1_paid_retry_on_fixable_cases": {
            "fixable_cases_expected": 3,
            "fixable_cases": 3,
            "paid": 2,
            "auto_resolution_rate": 2 / 3,
            "met": False,
        },
        "2_escalation_of_other_cases": {
            "other_cases": 14,
            "escalated_with_an_accurate_note": 14,
            "met": True,
        },
        "3_seconds_from_first_step_to_paid": {
            "limit": 120,
            "paid_cases": 2,
            "median": 9.3,
            "max": 11.8,
            "met": False,
        },
    },
    "events": "polled",
    "paths": {"fallbacks": []},
    "seller_fix": "simulated",
    "approval": "simulated",
    "model_calls": 37,
    "cost_usd": 1.2345,
    "cost_is_estimate": True,
    "case_rows": [
        {"draft_kind": "seller_message", "quality": 3, "quality_reasoning": "The failure code."},
        {
            "draft_kind": "escalation_note",
            "quality": 3,
            "quality_reasoning": "It opens with a raw failure code line.",
        },
        {
            "draft_kind": "escalation_note",
            "quality": 3,
            "quality_reasoning": "Opens with the raw Failure Code, then ids.",
        },
        {"draft_kind": "escalation_note", "quality": 3, "quality_reasoning": "Too long."},
        {
            "draft_kind": "escalation_note",
            "quality": 4,
            "quality_reasoning": "Failure code up top.",
        },
    ],
}


def page_with(make_app, *results: dict) -> str:
    app = make_app()
    app.results_dir.mkdir(exist_ok=True)
    for one in results:
        (app.results_dir / f"{one['run_id']}.json").write_text(json.dumps(one))
    response = app.client.get("/")
    assert response.status_code == 200
    return response.text


def text_of(page: str) -> str:
    """The page as a reader sees it: no tags, single spaces."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())


def test_the_page_shows_the_two_scores_from_the_results_file(make_app):
    text = text_of(page_with(make_app, RESULTS))

    assert "Message accuracy 94%" in text
    assert "16 of 17 drafted messages passed every accuracy check" in text
    assert "Message quality 3.8 of 5" in text


def test_the_note_under_the_quality_score_counts_the_notes_from_the_results_file(make_app):
    text = text_of(page_with(make_app, RESULTS))

    # Two of the four escalation notes rated 3 were marked down for the failure code line.
    assert (
        "Two escalation notes scored 3; the judge marked down the raw failure code on line "
        "one, which code writes on purpose." in text
    )


def test_the_note_is_left_out_when_no_escalation_note_was_marked_down_for_line_one(make_app):
    results = {**RESULTS, "case_rows": RESULTS["case_rows"][:1]}

    assert "the judge marked down" not in text_of(page_with(make_app, results))


def test_one_marked_down_note_is_written_in_the_singular(make_app):
    results = {**RESULTS, "case_rows": RESULTS["case_rows"][:2]}

    assert "One escalation note scored 3; the judge" in text_of(page_with(make_app, results))


def test_the_page_shows_the_three_success_criteria_with_the_measured_results(make_app):
    text = text_of(page_with(make_app, RESULTS))

    assert (
        "Payout Rescue reaches a paid retry on 100% of fixable-code cases with one human action "
        "(the approval). Not met: 2 of 3 fixable cases reached a paid retry (67%)." in text
    )
    assert (
        "the drafted note passes the accuracy checks. Met: 14 of 14 other cases were "
        "escalated with an accurate note." in text
    )
    assert (
        "excluding the seller button press. Not met: median 9.3 seconds, max 11.8 seconds, "
        "on 2 paid retries (limit 120 seconds)." in text
    )


def test_the_page_shows_a_row_for_each_failure_code(make_app):
    page = page_with(make_app, RESULTS)
    table = re.search(
        r"<table[^>]*>\s*<caption>Scores for each failure code.*?</table>", page, re.S
    )
    rows = [text_of(row) for row in re.findall(r"<tr>.*?</tr>", table.group(0), re.S)]

    assert rows == [
        "Failure code Cases Message accuracy Message quality",
        "account_closed 3 67% 4.7",
        "invalid_currency 14 100% 3.5",
    ]


def test_the_page_shows_the_facts_of_the_run_one_line_each(make_app):
    text = text_of(page_with(make_app, RESULTS))

    for line in (
        "Cases: 17",
        "Model: claude-cli:claude-sonnet-5-5",
        "Run: eval-20260102T030405Z",
        "Events: polled",
        "Fallbacks: none",
        "Human steps: approval simulated, seller fix simulated",
        "Model calls: 37",
        "Cost: $1.23 (estimate)",
    ):
        assert line in text


def test_the_results_come_before_the_failed_payouts_in_the_given_order(make_app):
    text = text_of(page_with(make_app, RESULTS))
    order = [
        "Message accuracy 94%",
        "Message quality 3.8 of 5",
        "Payout Rescue reaches a paid retry",
        "Scores for each failure code",
        "Cases: 17",
        "No failed payouts yet.",
    ]

    assert [text.index(part) for part in order] == sorted(text.index(part) for part in order)


def test_the_page_reads_the_newest_results_file(make_app):
    older = {**RESULTS, "run_id": "eval-20250101T000000Z", "cases": 3}

    text = text_of(page_with(make_app, RESULTS, older))

    assert "Run: eval-20260102T030405Z" in text
    assert "Cases: 17" in text


def test_the_assumptions_sit_in_their_own_labelled_box(make_app):
    page = page_with(make_app, RESULTS)
    box = re.search(r'<aside class="assumptions".*?</aside>', page, re.S).group(0)

    assert "Illustrative assumptions, not customer data" in text_of(box)
    assert "4,000 sellers" in text_of(box)
    assert len(re.findall(r"<li>", box)) == 3
    # The box holds no measured number.
    assert "94%" not in box


def test_with_no_results_file_the_page_says_how_to_run_the_eval(make_app):
    app = make_app()
    app.post_event(payout_failed_event())

    response = app.client.get("/")

    assert response.status_code == 200
    assert "No eval run yet" in response.text
    assert "./scripts/eval.sh" in response.text
    assert "Message accuracy" not in response.text
    assert "po_failed_1" in response.text


def test_a_results_file_that_cannot_be_read_is_treated_as_no_eval_run(make_app):
    app = make_app()
    app.results_dir.mkdir()
    (app.results_dir / "eval-broken.json").write_text("{not json")

    assert "No eval run yet" in app.client.get("/").text


def test_a_run_that_stopped_early_shows_how_many_cases_it_has(make_app):
    results = {**RESULTS, "cases_expected": 18}

    assert "Cases: 17 of 18" in text_of(page_with(make_app, results))


def test_a_case_row_with_no_draft_does_not_break_the_page(make_app):
    row = {"kind": "account_closed", "status": "not taken in", "accurate": None, "quality": None}
    results = {**RESULTS, "case_rows": [*RESULTS["case_rows"], row]}

    assert "Message accuracy 94%" in text_of(page_with(make_app, results))


def test_a_results_file_with_no_scored_case_or_another_shape_is_treated_as_no_eval_run(make_app):
    no_scores = {**RESULTS, "cases": 0, "message_accuracy": None, "message_quality": None}

    assert "No eval run yet" in page_with(make_app, no_scores)
    # This name sorts after the first file, so it is the newest.
    assert "No eval run yet" in page_with(make_app, {"run_id": "eval-other-shape"})


def test_the_page_names_the_fallbacks_the_run_took(make_app):
    results = {**RESULTS, "paths": {"fallbacks": ["declined: Stripe made no payout."]}}

    assert "Fallbacks: declined: Stripe made no payout." in text_of(page_with(make_app, results))
