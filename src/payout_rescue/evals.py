"""Eval scoring: message accuracy, checked by code, and message quality, by a judge.

Everything here works on a drafted text and the facts the model was given to draft it from.
Nothing here calls Stripe. Only `judge` calls a model, through the LLM seam.
"""

import json
import re
from dataclasses import asdict, dataclass
from statistics import fmean

from payout_rescue.classification import reason_words
from payout_rescue.drafting import ESCALATION_NOTE, failure_code_line
from payout_rescue.llm import Provider, ProviderError

LOWEST_RATING, HIGHEST_RATING = 1, 5


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    reason: str


@dataclass(frozen=True)
class Quality:
    rating: int
    reasoning: str
    # True when the judge gave no rating that could be read. The rating is then the lowest.
    judge_failed: bool = False


# --- message accuracy

# These are conservative pattern checks, not a semantic proof. Common denials are rejected;
# unusual wording can still need a person's review. No model decides message accuracy.
_DENIAL_BEFORE_REASON = re.compile(
    r"(?:\b(?:not|never|no|cannot)\b|\b\w+n['’]t\b)"
    r"(?:\s+(?:been|be|being|actually|really|currently|necessarily|yet|the|a|an|"
    r"because|of|due|to|caused|by|have|has|had))*\s*$"
    r"|\bnot\s+(?:true|correct)\s+that\s+[^.!?;\n]*$",
    re.I,
)
_DENIAL_AFTER_REASON = re.compile(
    r"^\s+(?:(?:is|are|was|were)\s+(?:not|never)|(?:is|are|was|were)n['’]t)\s+"
    r"(?:(?:the|a|an|actual|real|reported)\s+)*(?:reason|cause|problem|issue|true|correct)\b",
    re.I,
)

# The one seller action in a rescue: send new bank details.
_SELLER_ACTION = re.compile(
    r"\b(new|updated|different|another)\s+"
    r"((bank\s+account|bank|account)(\s+(details|information|number))?|details)\b"
    r"|\bupdate\s+(your|their|the)\s+(bank|account)\b",
    re.I,
)
# Keep wrapped lines and parenthetical commas together. Match the denial's relationship
# to the instruction, not just any negative word elsewhere in the sentence.
_CLAUSE_BOUNDARY = re.compile(r"[.!?;]|\b(?:but|however)\b", re.I)
_DENIAL_BEFORE_ACTION = re.compile(
    r"(?:\b(?:not|never|no|cannot)\b|\b\w+n['’]t\b)"
    r"(?:[\s,]+(?:need|to|send|provide|submit|supply|give|us|me|them|your|any|ever|"
    r"please|have|must|should|request|ask|for|be|required|asked|expected|or))*[\s,]*$",
    re.I,
)
_DENIAL_AFTER = re.compile(
    r"^\s+(?:(?:are|is|were|was|will|would|do|does|should|could|can|have|has)\s+)?"
    r"(?:not|never|unnecessary|optional|\w+n['’]t)\b",
    re.I,
)

_MONTHS = (
    "january|february|march|april|june|july|august|september|october|november|december"
    r"|may\s+\d|(of|in|by|before|until)\s+may"
)
_HOW_MANY = r"\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|few|several|couple of"
_DATES = re.compile(
    rf"\b({_MONTHS}|monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|today|tonight|tomorrow|yesterday|within|deadline"
    rf"|({_HOW_MANY}|next|this|business|working|the)\s+(day|week|month|hour|year)s?)\b",
    re.I,
)
# Any statement about the future counts as a promise.
_PROMISES = re.compile(r"\b(will|shall|guarantee\w*|promise\w*|soon|shortly)\b|'ll\b", re.I)
_WORDS_WITH_A_DIGIT = re.compile(r"[\w.,]*\d[\w.,]*")


def accuracy_checks(kind: str, draft_input: dict, text: str) -> list[Check]:
    """Every accuracy check for one drafted text. The text is accurate when all of them pass.

    `kind` is the draft kind, `draft_input` is what the model was given, and `text` is the
    seller message or the whole escalation note.
    """
    escalation = kind == ESCALATION_NOTE
    checks = [
        _failure_reason_check(draft_input["payout"]["failure_code"], text),
        _seller_action_check(escalation, text),
        _amount_check(draft_input["payout"]["amount"], text),
        _nothing_invented_check(draft_input, text),
    ]
    if escalation:
        checks.append(_line_one_check(draft_input["payout"]["failure_code"], text))
    return checks


def _has_word(words: tuple[str, ...], text: str) -> str | None:
    """The first of the words that the text has, as a whole word."""
    return next((w for w in words if re.search(rf"\b{re.escape(w)}\b", text, re.I)), None)


def _failure_reason_check(failure_code: str | None, text: str) -> Check:
    wanted, others = reason_words(failure_code)
    wrong = _has_word(others, text)
    denied = any(
        _DENIAL_BEFORE_REASON.search(text[: match.start()])
        or _DENIAL_AFTER_REASON.search(text[match.end() :])
        for word in wanted
        for match in re.finditer(rf"\b{re.escape(word)}\b", text, re.I)
    )
    if wrong:
        reason = f"The text gives a reason the bank did not give: {wrong!r}."
    elif denied:
        reason = "The text denies the bank's failure reason."
    elif _has_word(wanted, text):
        reason = f"The text states the reason (one of: {list(wanted)})."
    else:
        reason = f"The text does not state the reason (one of: {list(wanted)})."
    return Check(
        "failure_reason", not wrong and not denied and _has_word(wanted, text) is not None, reason
    )


def _seller_action_check(escalation: bool, text: str) -> Check:
    # Scope a denial to the clause containing the action. A negated failure reason in
    # another sentence (e.g. "account not found") does not negate a later instruction.
    actions = [
        not (
            _DENIAL_BEFORE_ACTION.search(clause[: match.start()])
            or _DENIAL_AFTER.search(clause[match.end() :])
        )
        for clause in _CLAUSE_BOUNDARY.split(text)
        for match in _SELLER_ACTION.finditer(clause)
    ]
    asks = any(actions)
    said = "asks" if asks else "does not ask"
    if escalation:
        return Check("no_seller_action", not asks, f"The note {said} for new bank details.")
    if actions and not all(actions):
        return Check("seller_action", False, "The message denies the need for new bank details.")
    return Check("seller_action", asks, f"The message {said} for new bank details.")


def _amount_check(amount: str, text: str) -> Check:
    """`amount` is the payout's amount and currency as the model was given it: `125.00 USD`."""
    number, currency = (re.escape(part) for part in amount.split())
    together = rf"(?<![\d.,]){number}\s*{currency}\b|\b{currency}\s*\$?{number}(?![\d,]|\.\d)"
    passed = re.search(together, text, re.I) is not None
    return Check(
        "amount_and_currency",
        passed,
        f"The text {'has' if passed else 'does not have'} the amount {amount}.",
    )


def _nothing_invented_check(draft_input: dict, text: str) -> Check:
    given = _words_with_a_digit(json.dumps(draft_input, ensure_ascii=False))
    found = sorted(_words_with_a_digit(text) - given)
    found += [match.group(0) for regex in (_DATES, _PROMISES) for match in regex.finditer(text)]
    return Check(
        "nothing_invented",
        not found,
        f"Not in the input: {found}."
        if found
        else "No date, promise or number that is not in the input.",
    )


def _line_one_check(failure_code: str | None, text: str) -> Check:
    wanted = failure_code_line(failure_code)
    first = text.splitlines()[0] if text else ""
    passed = first == wanted
    return Check(
        "raw_failure_code_on_line_one",
        passed,
        f"Line one is {first!r}." if passed else f"Line one is {first!r}, not {wanted!r}.",
    )


def _words_with_a_digit(text: str) -> set[str]:
    """Every run of letters, digits, dots and commas that has a digit: amounts, ids, dates."""
    return {word.strip(".,") for word in _WORDS_WITH_A_DIGIT.findall(text)}


# --- message quality

_RUBRIC = (
    "You rate one short message about a failed payout for clarity and tone. You are given "
    "JSON with the facts the writer had and the message. Do not check the facts; code does "
    "that. Rate from 1 to 5:\n"
    "5: a reader knows at once what happened and what to do next; plain words; short; "
    "polite and calm.\n"
    "4: clear and polite, with one small fault, such as a long sentence or a needless line.\n"
    "3: the reader gets the point, but must work for it, or the tone is a little off.\n"
    "2: hard to follow, or cold, or it blames the reader.\n"
    "1: the reader cannot tell what happened or what to do, or the tone is rude.\n"
    'Reply with JSON only: {"rating": <whole number 1 to 5>, "reasoning": "<one sentence>"}'
)


def judge(provider: Provider, kind: str, draft_input: dict, text: str) -> Quality:
    """One model call that rates a drafted text. A reply that cannot be read is a failure
    with the lowest rating, never an error."""
    messages = [
        {"role": "system", "content": _RUBRIC},
        {
            "role": "user",
            "content": json.dumps({"kind": kind, "facts": draft_input, "message": text}, indent=2),
        },
    ]
    try:
        reply = provider.complete(messages, []).text
        # The first JSON object in the reply; text around it is ignored.
        verdict, _ = json.JSONDecoder().raw_decode(reply[reply.index("{") :])
        rating, reasoning = verdict["rating"], verdict["reasoning"]
        if isinstance(rating, bool) or not isinstance(rating, int | float) or rating != int(rating):
            raise ValueError(f"the rating is {rating!r}")
        if not LOWEST_RATING <= rating <= HIGHEST_RATING:
            raise ValueError(f"the rating is {rating!r}")
        if not isinstance(reasoning, str) or not reasoning.strip():
            raise ValueError("there is no reasoning")
    except (ProviderError, ValueError, KeyError, TypeError) as error:
        return Quality(LOWEST_RATING, f"The judge gave no rating: {error!r}", judge_failed=True)
    return Quality(int(rating), reasoning.strip())


# --- the scores


def evaluate_case(provider: Provider, kind: str, draft_input: dict, text: str) -> dict:
    """Both scores for one case, as one row for `score` and for the results file."""
    checks = accuracy_checks(kind, draft_input, text)
    quality = judge(provider, kind, draft_input, text)
    return {
        "failure_code": draft_input["payout"]["failure_code"],
        "draft_kind": kind,
        "accurate": all(check.passed for check in checks),
        "checks": [asdict(check) for check in checks],
        "quality": quality.rating,
        "quality_reasoning": quality.reasoning,
        "judge_failed": quality.judge_failed,
    }


def score(cases: list[dict]) -> dict:
    """Message accuracy and message quality for all cases, and for each failure code."""
    by_failure_code: dict[str, list[dict]] = {}
    for case in cases:
        by_failure_code.setdefault(case["failure_code"] or "none given", []).append(case)
    return {
        **_scores(cases),
        "by_failure_code": {
            code: _scores(by_failure_code[code]) for code in sorted(by_failure_code)
        },
    }


def _scores(cases: list[dict]) -> dict:
    return {
        "cases": len(cases),
        # Message accuracy: the share of cases that pass every check.
        "message_accuracy": fmean(case["accurate"] for case in cases) if cases else None,
        # Message quality: the mean rating.
        "message_quality": fmean(case["quality"] for case in cases) if cases else None,
        "judge_failures": sum(case["judge_failed"] for case in cases),
    }
