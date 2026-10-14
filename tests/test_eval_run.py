"""The eval run, with the fake gateway and the fake provider. Nothing leaves the machine."""

import json

import pytest

from payout_rescue import eval_run, eval_seed, evals
from payout_rescue.llm import FakeProvider
from payout_rescue.store import Store
from payout_rescue.stripe_gateway import FakeStripeGateway, GatewayError

TWO_SELLERS = {"account_closed": "Juniper Candle Co.", "invalid_currency": "Larkspur Ceramics"}
SELLER_MESSAGE = (
    "Your payout of 10.00 USD failed. The bank account has been closed. "
    "Please send us new bank account details."
)
NOTE = "A payout of 10.00 USD failed because of its currency. A person must look at it."


def rating(value: int) -> str:
    return json.dumps({"rating": value, "reasoning": "Clear."})


def run(tmp_path, script, gateway=None, **kwargs) -> dict:
    kwargs.setdefault("sellers", TWO_SELLERS)
    kwargs.setdefault("cases_per_kind", 1)
    return eval_run.run_eval(
        Store(tmp_path / "eval.db"),
        gateway or FakeStripeGateway(),
        FakeProvider(script),
        results_dir=tmp_path / "results",
        run_id="eval-test",
        log=lambda line: None,
        **kwargs,
    )


def test_a_run_over_two_cases_writes_a_results_file_with_every_field(tmp_path):
    results = run(tmp_path, [SELLER_MESSAGE, rating(5), NOTE, rating(4)])

    assert json.loads((tmp_path / "results" / "eval-test.json").read_text()) == results
    assert set(results) == {
        "run_id",
        "started_at",
        "seconds",
        "provider_model",
        "cases",
        "cases_expected",
        "message_accuracy",
        "message_quality",
        "judge_failures",
        "by_failure_code",
        "criteria",
        "events",
        "seller_fix",
        "approval",
        "paths",
        "model_calls",
        "cost_usd",
        "cost_is_estimate",
        "case_rows",
    }
    assert results["cases"] == 2
    assert results["message_accuracy"] == 1.0
    assert results["message_quality"] == 4.5
    assert list(results["by_failure_code"]) == ["account_closed", "invalid_currency"]
    assert results["provider_model"] == "fake:scripted"
    assert (results["events"], results["seller_fix"], results["approval"]) == (
        "polled",
        "simulated",
        "simulated",
    )
    assert results["model_calls"] == 4
    assert (results["cost_usd"], results["cost_is_estimate"]) == (0.0, False)
    assert set(results["seconds"]) == {"seed", "run"}
    assert set(results["criteria"]) == {
        "1_paid_retry_on_fixable_cases",
        "2_escalation_of_other_cases",
        "3_seconds_from_first_step_to_paid",
    }
    assert set(results["paths"]) == {"accounts", "fallbacks", "unknown_code"}


def test_the_rescue_and_the_escalation_go_through_the_apps_own_steps(tmp_path):
    results = run(tmp_path, [SELLER_MESSAGE, rating(5), NOTE, rating(4)])
    rescue, escalation = results["case_rows"]
    store = Store(tmp_path / "eval.db")

    assert (rescue["status"], escalation["status"]) == ("paid", "escalated")
    assert rescue["text"] == SELLER_MESSAGE
    assert escalation["text"] == f"failure code: invalid_currency\n{NOTE}"
    steps = store.steps_for(rescue["payout_id"])
    assert [step["type"] for step in steps] == [
        "classification",
        "model_draft",
        "approval",
        "retry",
        "retry",
    ]
    # The trace does not say a person gave the approval.
    assert steps[2]["actor"] == "eval run (simulated)"
    # The rows carry the eval run id, not `live`.
    assert {step["run_id"] for step in steps} == {"eval-test"}
    assert store.get_failed_payout(rescue["payout_id"])["run_id"] == "eval-test"
    criteria = results["criteria"]
    assert criteria["1_paid_retry_on_fixable_cases"] == {
        "fixable_cases_expected": 1,
        "fixable_cases": 1,
        "paid": 1,
        "auto_resolution_rate": 1.0,
        "met": True,
    }
    assert criteria["2_escalation_of_other_cases"] == {
        "other_cases": 1,
        "escalated_with_an_accurate_note": 1,
        "met": True,
    }
    assert criteria["3_seconds_from_first_step_to_paid"]["met"] is True
    assert criteria["3_seconds_from_first_step_to_paid"]["limit"] == 120


def test_the_scores_in_the_results_file_match_the_scoring_of_the_case_rows(tmp_path):
    bad_message = SELLER_MESSAGE.replace("10.00", "99.00")
    # The cases come in rounds: one of each kind, then one of each kind again.
    script = [bad_message, rating(2), NOTE, rating(4), SELLER_MESSAGE, rating(5), NOTE, rating(3)]

    results = run(tmp_path, script, cases_per_kind=2)

    expected = evals.score(results["case_rows"])
    assert results["cases"] == expected["cases"] == 4
    for key in ("message_accuracy", "message_quality", "judge_failures", "by_failure_code"):
        assert results[key] == expected[key]
    assert results["message_accuracy"] == 0.75
    assert results["message_quality"] == 3.5
    failed = [row for row in results["case_rows"] if not row["accurate"]]
    assert [row["text"] for row in failed] == [bad_message]


def test_a_judge_reply_that_cannot_be_read_is_asked_for_once_more(tmp_path):
    results = run(
        tmp_path, [SELLER_MESSAGE, "no json", rating(4), NOTE, rating(4)], cases_per_kind=1
    )

    assert results["judge_failures"] == 0
    assert results["message_quality"] == 4.0
    assert results["model_calls"] == 5


def test_the_run_stops_calling_the_model_at_the_call_limit(tmp_path):
    results = run(tmp_path, [SELLER_MESSAGE, rating(5), NOTE, rating(4)], max_model_calls=2)

    assert results["model_calls"] == 2
    rescue, escalation = results["case_rows"]
    assert rescue["accurate"] is True
    # The second case got no draft and no rating. It is in the results as a failure.
    assert (escalation["accurate"], escalation["judge_failed"]) == (False, True)
    assert escalation["status"] == "escalated"


def test_a_failed_retry_counts_against_criterion_1(tmp_path):
    gateway = FakeStripeGateway()
    gateway.retry_outcome = ("failed", "no_account")

    results = run(tmp_path, [SELLER_MESSAGE, rating(5), NOTE, rating(4)], gateway=gateway)

    assert results["case_rows"][0]["status"] == "escalated"
    assert results["criteria"]["1_paid_retry_on_fixable_cases"] == {
        "fixable_cases_expected": 1,
        "fixable_cases": 1,
        "paid": 0,
        "auto_resolution_rate": 0.0,
        "met": False,
    }
    assert results["criteria"]["3_seconds_from_first_step_to_paid"]["met"] is False


def test_a_case_that_stripe_stops_keeps_its_scores_and_the_run_goes_on(tmp_path):
    class RefusesTheSellerFix(FakeStripeGateway):
        def add_external_account(self, account_id):
            if ("create_failing_payout", account_id, 1000, "account_closed") in self.calls:
                raise GatewayError("Stripe is down")
            return super().add_external_account(account_id)

    script = [SELLER_MESSAGE, rating(5), NOTE, rating(4)]

    results = run(tmp_path, script, gateway=RefusesTheSellerFix())
    stopped, escalation = results["case_rows"]

    assert (stopped["error"], stopped["status"]) == ("Stripe is down", "awaiting_seller")
    # Stripe stopped the rescue, not the model: the draft is still scored.
    assert (stopped["accurate"], stopped["quality"]) == (True, 5)
    assert (escalation["status"], escalation["error"]) == ("escalated", None)
    assert results["cases"] == 2
    assert results["criteria"]["1_paid_retry_on_fixable_cases"]["met"] is False
    assert results["criteria"]["3_seconds_from_first_step_to_paid"]["met"] is False


def test_a_case_that_was_never_taken_in_is_listed_but_not_scored(tmp_path):
    class LosesThePayout(FakeStripeGateway):
        def get_connected_account(self, account_id):
            if any(call[0] == "create_failing_payout" for call in self.calls):
                raise GatewayError("No such account")
            return super().get_connected_account(account_id)

    results = run(tmp_path, [], gateway=LosesThePayout())

    assert (results["cases"], results["cases_expected"]) == (0, 2)
    assert [row["status"] for row in results["case_rows"]] == ["not taken in"] * 2
    assert results["model_calls"] == 0
    assert results["criteria"]["1_paid_retry_on_fixable_cases"]["met"] is False


def test_a_fixable_kind_that_the_seed_left_out_counts_against_criterion_1(tmp_path):
    class NoClosedAccount(FakeStripeGateway):
        def create_failing_payout(self, account_id, amount, failure_code, metadata):
            if failure_code == "account_closed":
                raise GatewayError("refused")
            return super().create_failing_payout(account_id, amount, failure_code, metadata)

    results = run(tmp_path, [NOTE, rating(4)], gateway=NoClosedAccount())

    assert results["criteria"]["1_paid_retry_on_fixable_cases"] == {
        "fixable_cases_expected": 1,
        "fixable_cases": 0,
        "paid": 0,
        "auto_resolution_rate": 0.0,
        "met": False,
    }


def test_the_unknown_code_case_on_the_test_card_is_recorded_as_tested(tmp_path):
    sellers = {"declined": "Saltmarsh Soap Works"}
    note = "A payout of 10.00 USD failed. The reason is not known. A person must look at it."

    results = run(tmp_path, [note, rating(4)], gateway=FakeStripeGateway(), sellers=sellers)

    (row,) = results["case_rows"]
    assert (row["kind"], row["draft_kind"], row["failure_code"]) == (
        "declined",
        "escalation_note",
        "declined",
    )
    assert row["accurate"] is True
    assert "test debit card" in results["paths"]["unknown_code"]
    assert results["paths"]["fallbacks"] == []


def test_a_new_connected_account_gets_an_external_account_before_the_wait(tmp_path):
    gateway = FakeStripeGateway()

    eval_seed.seed_accounts(gateway, "eval-1", {"no_account": "Harbor Street Prints"}, log=print)
    eval_seed.seed_accounts(gateway, "eval-2", {"no_account": "Harbor Street Prints"}, log=print)

    assert [c for c in gateway.calls if c[0] == "add_external_account"] == [
        ("add_external_account", "acct_fake_1")
    ]


def test_the_seed_reuses_the_connected_accounts_it_tagged(tmp_path):
    gateway = FakeStripeGateway()
    quiet = {"log": lambda line: None}

    first = eval_seed.seed_accounts(gateway, "eval-1", TWO_SELLERS, **quiet)
    second = eval_seed.seed_accounts(gateway, "eval-2", TWO_SELLERS, **quiet)

    assert {kind: account["account"] for kind, account in first.items()} == {
        "account_closed": "created",
        "invalid_currency": "created",
    }
    assert {account["account"] for account in second.values()} == {"reused"}
    assert [a["account_id"] for a in first.values()] == [a["account_id"] for a in second.values()]
    assert len([call for call in gateway.calls if call[0] == "create_connected_account"]) == 2
    assert first["account_closed"]["name"] == "Juniper Candle Co."


def test_the_seed_funds_only_an_account_with_a_low_balance(tmp_path):
    gateway = FakeStripeGateway()
    gateway.balances = {"acct_fake_1": 0}

    accounts = eval_seed.seed_accounts(gateway, "eval-1", TWO_SELLERS, log=lambda line: None)

    assert [account["funding"] for account in accounts.values()] == ["funded", "not needed"]
    assert [call[1] for call in gateway.calls if call[0] == "fund"] == ["acct_fake_1"]


def test_when_the_test_card_is_refused_the_run_records_the_fallback(tmp_path):
    class NoCard(FakeStripeGateway):
        def create_failing_payout(self, account_id, amount, failure_code, metadata):
            if failure_code == "declined":
                raise GatewayError("This card cannot be an external account.")
            return super().create_failing_payout(account_id, amount, failure_code, metadata)

    sellers = {"invalid_currency": "Larkspur Ceramics", "declined": "Saltmarsh Soap Works"}

    results = run(tmp_path, [NOTE, rating(4)], gateway=NoCard(), sellers=sellers)

    assert results["cases"] == 1
    assert results["paths"]["unknown_code"] == "not tested on Stripe"
    (fallback,) = results["paths"]["fallbacks"]
    assert "declined" in fallback and "cannot be an external account" in fallback


def test_a_payout_that_fails_with_another_code_is_recorded_as_a_path(tmp_path):
    class WrongCode(FakeStripeGateway):
        def create_failing_payout(self, account_id, amount, failure_code, metadata):
            return super().create_failing_payout(account_id, amount, "declined", metadata)

    results = run(
        tmp_path, [NOTE, rating(4)], gateway=WrongCode(), sellers={"invalid_currency": "L"}
    )

    assert results["case_rows"][0]["failure_code"] == "declined"
    assert "invalid_currency" in results["paths"]["fallbacks"][0]


def test_the_time_to_paid_leaves_out_the_wait_between_the_draft_and_the_approval():
    steps = [
        {"type": "classification", "created_at": "2026-10-11T10:00:00.000Z", "outputs": {}},
        {"type": "model_draft", "created_at": "2026-10-11T10:00:10.000Z", "outputs": {}},
        {"type": "approval", "created_at": "2026-10-11T10:05:10.000Z", "outputs": None},
        {
            "type": "retry",
            "created_at": "2026-10-11T10:05:11.000Z",
            "outputs": {"retry_payout_id": "po_1"},
        },
        {"type": "retry", "created_at": "2026-10-11T10:05:14.500Z", "outputs": {"status": "paid"}},
    ]

    # 10 seconds to the draft, then 4.5 seconds from the approval to paid.
    assert eval_run.seconds_to_paid(steps) == pytest.approx(14.5)
    assert eval_run.seconds_to_paid(steps[:4]) is None


def test_the_default_set_is_six_kinds_of_three_cases():
    assert len(eval_seed.SELLERS) == 6
    assert eval_seed.CASES_PER_KIND == 3
    assert len(set(eval_seed.SELLERS.values())) == 6


def test_waiting_stops_with_an_error_that_names_what_is_still_waited_for():
    with pytest.raises(TimeoutError, match="still waiting for funds: \\['acct_1'\\]"):
        eval_seed.wait_for_all(["acct_1"], lambda item: False, "funds", seconds=0, pause=0)


def test_a_payout_that_does_not_fail_is_left_out_with_a_sentence(tmp_path, monkeypatch):
    class PaysOut(FakeStripeGateway):
        def get_payout(self, account_id, payout_id):
            return {**super().get_payout(account_id, payout_id), "status": "paid"}

    # The real wait is 90 seconds. Here it gives up at once.
    wait_for_all = eval_seed.wait_for_all
    monkeypatch.setattr(
        eval_seed,
        "wait_for_all",
        lambda items, ready, what, *a, **k: wait_for_all(items, ready, what, 0, 0),
    )
    accounts = {"account_closed": {"account_id": "acct_fake_1", "name": "Juniper Candle Co."}}

    cases, fallbacks = eval_seed.seed_failed_payouts(
        PaysOut(), accounts, "eval-1", cases_per_kind=1, log=lambda line: None
    )

    assert cases == []
    assert fallbacks == [
        "account_closed: the payout po_fake_failed_1 did not fail. It is left out."
    ]
