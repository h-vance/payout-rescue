import pytest

from payout_rescue.classification import retry_permitted


@pytest.mark.parametrize("failure_code", ["account_closed", "no_account"])
def test_retry_is_permitted_for_the_two_fixable_failure_codes(failure_code):
    assert retry_permitted(failure_code) is True


@pytest.mark.parametrize(
    "failure_code",
    [
        "invalid_currency",
        "insufficient_funds",
        "debit_not_authorized",
        "declined",
        "could_not_process",
        "a_code_stripe_adds_next_year",
        "",
        None,
    ],
)
def test_retry_is_not_permitted_for_any_other_failure_code(failure_code):
    assert retry_permitted(failure_code) is False
