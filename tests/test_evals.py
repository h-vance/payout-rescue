"""Eval scoring on fixed rows. No Stripe and no real model."""

import json

import pytest

from conftest import FIXTURES
from payout_rescue import evals
from payout_rescue.classification import _FAILURE_REASONS
from payout_rescue.drafting import (
    ESCALATION_NOTE,
    SELLER_MESSAGE,
    build_draft_input,
    failure_code_line,
)
from payout_rescue.llm import FakeProvider, ProviderError

CONNECTED_ACCOUNT = {"id": "acct_seller_1", "business_profile": {"name": "Fake Seller"}}
EXTERNAL_ACCOUNT = {"id": "ba_fake_closed", "bank_name": "FAKE BANK", "last4": "1113"}


def draft_input(failure_code: str | None = "account_closed") -> dict:
    payout = {"id": "po_failed_1", "amount": 12500, "currency": "usd", "failure_code": failure_code}
    return build_draft_input(payout, CONNECTED_ACCOUNT, EXTERNAL_ACCOUNT)


GOOD_SELLER_MESSAGE = (
    "Hello Fake Seller,\n\nYour payout of 125.00 USD failed. The bank account ending in 1113 "
    "has been closed.\n\nPlease send us new bank account details."
)
GOOD_ESCALATION_NOTE = (
    "failure code: invalid_currency\nA payout of 125.00 USD to Fake Seller failed. The bank "
    "was unable to process this payout because of its currency. A person must look at it."
)


def failed_checks(kind: str, facts: dict, text: str) -> dict[str, str]:
    """The names of the checks that failed, each with its reason."""
    checks = evals.accuracy_checks(kind, facts, text)
    return {check.name: check.reason for check in checks if not check.passed}


# --- message accuracy


def test_a_correct_seller_message_passes_every_accuracy_check():
    checks = evals.accuracy_checks(SELLER_MESSAGE, draft_input(), GOOD_SELLER_MESSAGE)

    assert [check.name for check in checks] == [
        "failure_reason",
        "seller_action",
        "amount_and_currency",
        "nothing_invented",
    ]
    assert all(check.passed and check.reason for check in checks)


@pytest.mark.parametrize(
    "reason",
    [
        "is not closed",
        "isn't closed",
        "isn’t closed",
        "has never been closed",
        "has not been closed",
        "has been closed. The account is not closed",
        "being closed is not the reason",
    ],
)
def test_a_negated_failure_reason_fails_even_when_the_keyword_is_present(reason):
    text = GOOD_SELLER_MESSAGE.replace("has been closed", reason)

    assert "failure_reason" in failed_checks(SELLER_MESSAGE, draft_input(), text)


@pytest.mark.parametrize(
    "action",
    [
        "You do not need to send new bank details.",
        "Don't send updated bank details.",
        "You don’t need to update your bank account.",
        "Never send new bank account details.",
        "New bank details are not required.",
        "Updated bank details aren't needed.",
        "Please send new bank details. You do not need to send new bank details.",
        "Do not\nsend new bank details.",
        "You do not need to send, or provide, new bank details.",
    ],
)
def test_a_denied_seller_action_does_not_count_as_an_instruction(action):
    text = GOOD_SELLER_MESSAGE.replace("Please send us new bank account details.", action)

    assert "seller_action" in failed_checks(SELLER_MESSAGE, draft_input(), text)


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("no_account", "The bank account could be located."),
        ("no_account", "The bank details are not incorrect."),
        ("invalid_currency", "The failure was not caused by the currency."),
        ("insufficient_funds", "The problem is not insufficient funds."),
        ("debit_not_authorized", "It is not true that debits are not authorized."),
        ("declined", "The reason is not unknown."),
    ],
)
def test_denials_of_other_failure_reasons_are_rejected(code, reason):
    text = f"A payout of 125.00 USD failed. {reason}"
    checks = {c.name: c for c in evals.accuracy_checks(ESCALATION_NOTE, draft_input(code), text)}

    assert not checks["failure_reason"].passed


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("no_account", "The bank account could not be located."),
        ("no_account", "No bank account could be located with those details."),
        ("invalid_currency", "The bank cannot accept payments in that currency."),
        ("invalid_currency", "The currency is not supported by the bank."),
        ("invalid_currency", "The currency isn't accepted by the bank."),
        ("insufficient_funds", "There are not enough funds."),
        ("debit_not_authorized", "Debit transactions are not authorized."),
        ("declined", "The reason is not known."),
    ],
)
def test_negative_words_that_express_the_actual_failure_reason_still_pass(code, reason):
    checks = evals.accuracy_checks(ESCALATION_NOTE, draft_input(code), reason)

    assert next(c for c in checks if c.name == "failure_reason").passed


def test_an_escalation_can_say_new_bank_details_are_not_needed():
    text = GOOD_ESCALATION_NOTE + " New bank details are not required."

    assert failed_checks(ESCALATION_NOTE, draft_input("invalid_currency"), text) == {}


@pytest.mark.parametrize(
    "instruction",
    [
        "Please send new bank details because the bank cannot accept this payout.",
        "The bank cannot accept the currency so send new bank details.",
    ],
)
def test_an_unrelated_negation_does_not_hide_an_escalations_seller_instruction(instruction):
    text = GOOD_ESCALATION_NOTE + " " + instruction

    assert "no_seller_action" in failed_checks(
        ESCALATION_NOTE, draft_input("invalid_currency"), text
    )


def test_a_negative_failure_reason_does_not_negate_the_seller_instruction():
    text = (
        "Your payout of 125.00 USD failed because the account was not found "
        "so please send new bank details."
    )

    assert failed_checks(SELLER_MESSAGE, draft_input("no_account"), text) == {}


def test_a_seller_message_with_a_wrong_amount_fails():
    text = GOOD_SELLER_MESSAGE.replace("125.00", "152.00")

    failed = failed_checks(SELLER_MESSAGE, draft_input(), text)

    assert "125.00 USD" in failed["amount_and_currency"]
    assert "152.00" in failed["nothing_invented"]


def test_a_seller_message_with_the_wrong_currency_fails():
    text = GOOD_SELLER_MESSAGE.replace("USD", "EUR")

    assert "amount_and_currency" in failed_checks(SELLER_MESSAGE, draft_input(), text)


@pytest.mark.parametrize(
    "invented",
    [
        "We will retry on March 3.",
        "Please reply by Friday.",
        "Reply in 5 business days.",
        "Send them before tomorrow.",
        "Please reply within two weeks.",
        "Reply before the end of May.",
        "Reply by the end of the month.",
    ],
)
def test_a_seller_message_with_an_invented_date_fails(invented):
    failed = failed_checks(SELLER_MESSAGE, draft_input(), f"{GOOD_SELLER_MESSAGE} {invented}")

    assert list(failed) == ["nothing_invented"]


@pytest.mark.parametrize(
    "promise",
    [
        "We will pay you again soon.",
        "We guarantee the next payout.",
        "We promise.",
        "Your money will arrive once you do.",
        "You'll get it after that.",
    ],
)
def test_a_seller_message_with_a_promise_fails(promise):
    failed = failed_checks(SELLER_MESSAGE, draft_input(), f"{GOOD_SELLER_MESSAGE} {promise}")

    assert list(failed) == ["nothing_invented"]


def test_a_seller_message_with_the_wrong_failure_reason_fails():
    text = GOOD_SELLER_MESSAGE.replace("has been closed", "cannot take this currency")

    assert list(failed_checks(SELLER_MESSAGE, draft_input(), text)) == ["failure_reason"]


@pytest.mark.parametrize(
    ("failure_code", "text"),
    [
        # "enclosed" is not "closed".
        (
            "account_closed",
            "Your payout of 125.00 USD failed. We enclosed a form. Send new bank details.",
        ),
        # The real reason, and then a second one the bank did not give.
        (
            "invalid_currency",
            "failure code: invalid_currency\nA payout of 125.00 USD failed because "
            "of its currency. The account was closed.",
        ),
        (
            "declined",
            "failure code: declined\nA payout of 125.00 USD failed. The account was closed "
            "by an unknown party.",
        ),
    ],
)
def test_a_text_with_a_reason_the_bank_did_not_give_fails(failure_code, text):
    kind = SELLER_MESSAGE if failure_code == "account_closed" else ESCALATION_NOTE

    assert list(failed_checks(kind, draft_input(failure_code), text)) == ["failure_reason"]


def test_the_amount_and_the_currency_must_be_together():
    text = (
        "Your payout of 125.00 EUR failed. The bank account ending in 1113, a USD account, "
        "has been closed. Please send us new bank account details."
    )

    assert list(failed_checks(SELLER_MESSAGE, draft_input(), text)) == ["amount_and_currency"]


@pytest.mark.parametrize("amount", ["125.00 USD", "$125.00 USD", "USD 125.00", "125.00 usd"])
def test_the_amount_can_be_written_in_the_usual_ways(amount):
    text = GOOD_SELLER_MESSAGE.replace("125.00 USD", amount)

    assert failed_checks(SELLER_MESSAGE, draft_input(), text) == {}


@pytest.mark.parametrize(
    "ask",
    [
        "Please send us new bank details.",
        "Please send updated bank details.",
        "Send us the details of a new bank account.",
        "Please update your bank account.",
    ],
)
def test_the_seller_action_can_be_asked_for_in_the_usual_ways(ask):
    text = GOOD_SELLER_MESSAGE.replace("Please send us new bank account details.", ask)

    assert failed_checks(SELLER_MESSAGE, draft_input(), text) == {}


def test_a_seller_message_that_asks_for_nothing_fails():
    text = GOOD_SELLER_MESSAGE.replace("Please send us new bank account details.", "Sorry.")

    assert list(failed_checks(SELLER_MESSAGE, draft_input(), text)) == ["seller_action"]


def test_a_message_for_an_unknown_failure_code_must_say_the_reason_is_not_known():
    facts = draft_input("declined")
    honest = "failure code: declined\nA payout of 125.00 USD failed. The reason is not known."
    guessing = "failure code: declined\nA payout of 125.00 USD failed. The account was closed."

    assert failed_checks(ESCALATION_NOTE, facts, honest) == {}
    assert list(failed_checks(ESCALATION_NOTE, facts, guessing)) == ["failure_reason"]


def test_every_failure_code_with_a_known_reason_can_be_checked():
    for failure_code, reason in _FAILURE_REASONS.items():
        note = f"{failure_code_line(failure_code)}\nA payout of 125.00 USD failed. {reason}"
        assert failed_checks(ESCALATION_NOTE, draft_input(failure_code), note) == {}, failure_code


def test_a_correct_escalation_note_passes_every_accuracy_check():
    checks = evals.accuracy_checks(
        ESCALATION_NOTE, draft_input("invalid_currency"), GOOD_ESCALATION_NOTE
    )

    assert [check.name for check in checks] == [
        "failure_reason",
        "no_seller_action",
        "amount_and_currency",
        "nothing_invented",
        "raw_failure_code_on_line_one",
    ]
    assert all(check.passed for check in checks)


def test_an_escalation_note_without_the_raw_failure_code_fails():
    text = GOOD_ESCALATION_NOTE.split("\n", 1)[1]

    failed = failed_checks(ESCALATION_NOTE, draft_input("invalid_currency"), text)

    assert list(failed) == ["raw_failure_code_on_line_one"]
    assert "failure code: invalid_currency" in failed["raw_failure_code_on_line_one"]


def test_an_escalation_note_that_gives_the_seller_action_fails():
    text = GOOD_ESCALATION_NOTE + " Ask the seller to update their bank account."

    failed = failed_checks(ESCALATION_NOTE, draft_input("invalid_currency"), text)

    assert list(failed) == ["no_seller_action"]


# --- message quality: the judge


def judge_with(reply) -> evals.Quality:
    return evals.judge(FakeProvider([reply]), SELLER_MESSAGE, draft_input(), GOOD_SELLER_MESSAGE)


def test_the_judge_returns_the_rating_and_the_reasoning_from_the_models_json():
    quality = judge_with('{"rating": 4, "reasoning": "Clear and polite."}')

    assert quality == evals.Quality(rating=4, reasoning="Clear and polite.", judge_failed=False)


@pytest.mark.parametrize(
    "reply",
    [
        '```json\n{"rating": 5, "reasoning": "Good."}\n```',
        'Rating: {"rating": 5, "reasoning": "Good."} (see the {rubric})',
        '{"rating": 5.0, "reasoning": "Good."}',
    ],
)
def test_the_judge_reads_a_rating_with_text_around_the_json(reply):
    assert judge_with(reply) == evals.Quality(5, "Good.")


@pytest.mark.parametrize(
    "reply",
    [
        "I think it is a 4.",
        '{"rating": 4, "reasoning": ',
        '{"rating": 9, "reasoning": "Great."}',
        '{"rating": 4.5, "reasoning": "Good."}',
        '{"rating": "four", "reasoning": "Good."}',
        '{"rating": true, "reasoning": "Good."}',
        '{"reasoning": "No rating."}',
        "[4]",
        ProviderError("the model is away"),
    ],
)
def test_a_judge_reply_that_cannot_be_read_is_a_scored_failure_not_a_crash(reply):
    quality = judge_with(reply)

    assert (quality.rating, quality.judge_failed) == (evals.LOWEST_RATING, True)
    assert quality.reasoning


def test_the_judge_is_given_the_rubric_the_facts_and_the_message():
    class Recording(FakeProvider):
        def complete(self, messages, tools):
            self.seen = (messages, tools)
            return super().complete(messages, tools)

    provider = Recording(['{"rating": 3, "reasoning": "Fine."}'])

    evals.judge(provider, SELLER_MESSAGE, draft_input(), GOOD_SELLER_MESSAGE)

    (system, user), tools = provider.seen
    assert tools == []
    assert "1" in system["content"] and "5" in system["content"] and "JSON" in system["content"]
    assert GOOD_SELLER_MESSAGE in json.loads(user["content"])["message"]
    assert json.loads(user["content"])["facts"] == draft_input()


# --- the scores


def case(failure_code, accurate, rating, judge_failed=False) -> dict:
    return {
        "failure_code": failure_code,
        "accurate": accurate,
        "quality": rating,
        "judge_failed": judge_failed,
    }


def test_the_quality_score_is_the_mean_of_the_scripted_ratings():
    provider = FakeProvider([json.dumps({"rating": r, "reasoning": "x"}) for r in (5, 4, 2)])

    cases = [
        evals.evaluate_case(provider, SELLER_MESSAGE, draft_input(), GOOD_SELLER_MESSAGE)
        for _ in range(3)
    ]
    scores = evals.score(cases)

    assert scores["message_quality"] == pytest.approx(11 / 3)
    assert scores["message_accuracy"] == 1.0
    assert scores["cases"] == 3


def test_message_accuracy_is_the_share_of_cases_that_pass_every_check():
    scores = evals.score(
        [case("account_closed", True, 5), case("account_closed", False, 3), case(None, True, 4)]
    )

    assert scores["message_accuracy"] == pytest.approx(2 / 3)


def test_the_breakdown_per_failure_code_adds_up_to_the_overall_scores():
    cases = [
        case("account_closed", True, 5),
        case("account_closed", False, 3),
        case("no_account", True, 4),
        case("invalid_currency", True, 1, judge_failed=True),
        case("invalid_currency", False, 2),
        case("invalid_currency", True, 5),
    ]

    scores = evals.score(cases)
    rows = scores["by_failure_code"]

    assert list(rows) == ["account_closed", "invalid_currency", "no_account"]
    assert rows["account_closed"] == {
        "cases": 2,
        "message_accuracy": 0.5,
        "message_quality": 4.0,
        "judge_failures": 0,
    }
    assert sum(row["cases"] for row in rows.values()) == scores["cases"] == 6
    for key in ("message_accuracy", "message_quality"):
        weighted = sum(row[key] * row["cases"] for row in rows.values()) / scores["cases"]
        assert weighted == pytest.approx(scores[key])
    assert sum(row["judge_failures"] for row in rows.values()) == scores["judge_failures"] == 1


def test_scoring_no_cases_gives_no_scores():
    assert evals.score([]) == {
        "cases": 0,
        "message_accuracy": None,
        "message_quality": None,
        "judge_failures": 0,
        "by_failure_code": {},
    }


# --- the real drafts recorded from the sandbox


@pytest.mark.parametrize(
    ("name", "text_field"),
    [("rescue", "seller_message"), ("escalation", "escalation_note")],
)
def test_the_real_drafts_from_claude_cli_pass_every_accuracy_check(name, text_field):
    recorded = json.loads((FIXTURES / "real_drafts.json").read_text())[name]
    facts = dict(recorded["model_draft_step"]["inputs"])
    kind = facts.pop("draft")

    failed = failed_checks(kind, facts, recorded["failed_payout"][text_field])

    assert failed == {}
