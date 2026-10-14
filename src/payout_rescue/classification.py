"""Classification: a failed payout is a rescue or an escalation, by its failure code alone."""

# The one place the fixable failure codes live. The seller can clear
# both by supplying new bank details. No other code decides anything with these two codes.
FIXABLE_FAILURE_CODES = frozenset({"account_closed", "no_account"})

RESCUE = "rescue"
ESCALATION = "escalation"


def retry_permitted(failure_code: str | None) -> bool:
    """True only for a fixable failure code. A missing or unknown code is not fixable."""
    return failure_code in FIXABLE_FAILURE_CODES


def classify(failure_code: str | None) -> str:
    return RESCUE if retry_permitted(failure_code) else ESCALATION


# What each known failure code means, in Stripe's own words
# (https://docs.stripe.com/api/payouts/failures, read 2026-10-11). Code looks the reason up,
# so the model never has to work out what a failure code means. Any other code has no entry.
_FAILURE_REASONS = {
    "account_closed": "The bank account has been closed.",
    "no_account": (
        "The bank account details on file are probably incorrect. "
        "No bank account could be located with those details."
    ),
    "invalid_currency": (
        "The bank was unable to process this payout because of its currency. This is probably "
        "because the bank account cannot accept payments in that currency."
    ),
    "insufficient_funds": "The platform's Stripe account has insufficient funds for the payout.",
    "debit_not_authorized": "Debit transactions are not approved on the bank account.",
}


def failure_reason(failure_code: str | None) -> str | None:
    """The plain-words reason for a known failure code. None for a missing or unknown code."""
    return _FAILURE_REASONS.get(failure_code)


# Words and phrases for the eval's failure reason check. The scorer also
# rejects common denials. These patterns do not prove the meaning of arbitrary prose.
_REASON_WORDS = {
    "account_closed": ("closed",),
    "no_account": (
        "incorrect",
        "not be located",
        "cannot be located",
        "can't be located",
        "couldn't be located",
        "not found",
        "does not exist",
        "no bank account",
    ),
    "invalid_currency": ("currency",),
    "insufficient_funds": ("insufficient funds", "not enough funds"),
    "debit_not_authorized": ("not approved", "not authorized", "not authorised"),
}
# For a failure code with no known reason, the honest text says so.
_REASON_NOT_KNOWN_WORDS = ("not known", "unknown", "no reason")


def reason_words(failure_code: str | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The words a text about this failure code must have one of, and must have none of.

    The second group is the words for every other known reason: a text that has one of
    them gives a reason the bank did not give.
    """
    wanted = _REASON_WORDS.get(failure_code, _REASON_NOT_KNOWN_WORDS)
    others = tuple(
        word for code, words in _REASON_WORDS.items() if code != failure_code for word in words
    )
    return wanted, others
