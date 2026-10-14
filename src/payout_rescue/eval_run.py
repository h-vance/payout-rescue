"""The eval run: the labeled set on real Stripe and the real model, in one command.

Run: uv run python -m payout_rescue.eval_run
It writes evals/results/<run id>.json. Each case goes through the functions the webhook and
the two buttons call. The run reads each payout's status from Stripe by polling, so it does
not need `stripe listen`. A person gives neither the seller fix nor the approval: the run does.
"""

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

from payout_rescue import config, drafting, evals
from payout_rescue.classification import retry_permitted
from payout_rescue.eval_seed import (
    CASES_PER_KIND,
    SELLERS,
    UNKNOWN_CODE_KIND,
    seed_accounts,
    seed_failed_payouts,
    wait_for_all,
)
from payout_rescue.intake import take_in_failed_payout
from payout_rescue.llm import Completion, Provider, ProviderError
from payout_rescue.rescue import (
    SELLER_FIX,
    approve_retry,
    permitted_action,
    record_retry_outcome,
    record_seller_fix,
)
from payout_rescue.store import (
    APPROVAL_STEP,
    ESCALATED,
    MODEL_DRAFT_STEP,
    PAID,
    RETRY_STEP,
    Store,
    utc_now,
)
from payout_rescue.stripe_gateway import GatewayError

CRITERION_3_LIMIT_SECONDS = 120
# No person approves in an eval run, and the trace must not say that one did.
SIMULATED_APPROVER = "eval run (simulated)"
# 18 drafts and 18 ratings, and a few spare for a rating that must be asked for again.
MAX_MODEL_CALLS = 44


class CountingProvider:
    """Counts the model calls and their cost, and refuses a call past the limit."""

    def __init__(self, provider: Provider, max_calls: int):
        self._provider, self._max_calls = provider, max_calls
        self.calls, self.cost_usd, self.cost_is_estimate = 0, 0.0, False

    def complete(self, messages: list[dict], tools: list[dict]) -> Completion:
        if self.calls >= self._max_calls:
            raise ProviderError(
                f"The eval run's limit of {self._max_calls} model calls is reached."
            )
        self.calls += 1
        completion = self._provider.complete(messages, tools)
        cost, is_estimate = drafting.cost_usd(completion)
        self.cost_usd += cost
        self.cost_is_estimate = self.cost_is_estimate or is_estimate
        return completion


def seconds_to_paid(steps: list[dict]) -> float | None:
    """Criterion 3 for one rescue, from the trace: first step to paid, without the wait for
    people. None when the retry was not paid.

    The wait for people is the time from the model draft to the approval. In it are the
    seller fix and the approver's click.
    """

    def time_of(found) -> datetime | None:
        step = next((step for step in steps if found(step)), None)
        return step and datetime.fromisoformat(step["created_at"])

    draft = time_of(lambda step: step["type"] == MODEL_DRAFT_STEP)
    approval = time_of(lambda step: step["type"] == APPROVAL_STEP)
    paid = time_of(
        lambda step: step["type"] == RETRY_STEP and (step["outputs"] or {}).get("status") == "paid"
    )
    if not (draft and approval and paid):
        return None
    first = datetime.fromisoformat(steps[0]["created_at"])
    return ((paid - first) - (approval - draft)).total_seconds()


def run_case(store: Store, gateway, provider: Provider, case: dict, run_id: str) -> dict:
    """One case, from the failed payout to its scores. The result is one row of the results.

    If Stripe stops the case part way, the row says so, and what was drafted is still scored.
    """
    account_id, payout_id = case["account_id"], case["payout_id"]
    error = None
    try:
        # What the webhook does for a payout.failed event.
        take_in_failed_payout(store, gateway, provider, account_id, payout_id, run_id=run_id)
        if permitted_action(store.get_failed_payout(payout_id)) == SELLER_FIX:
            # What the two buttons do, in the order a rescue needs them.
            record_seller_fix(store, gateway, payout_id)
            approve_retry(store, gateway, payout_id, actor=SIMULATED_APPROVER)
            retry_id = store.get_failed_payout(payout_id)["retry_payout_id"]
            # What the webhook does for the retry's payout.paid or payout.failed event.
            wait_for_all(
                [retry_id],
                lambda i: gateway.get_payout(account_id, i)["status"] in ("paid", "failed"),
                "the retry to end",
                seconds=CRITERION_3_LIMIT_SECONDS,
                pause=1,
            )
            retry = gateway.get_payout(account_id, retry_id)
            record_retry_outcome(store, retry_id, retry["status"], retry.get("failure_code"))
    except (GatewayError, TimeoutError) as stopped:
        error = str(stopped)
    failed_payout = store.get_failed_payout(payout_id) or {}
    steps = store.steps_for(payout_id)
    draft = next((step for step in steps if step["type"] == MODEL_DRAFT_STEP), None)
    row = {
        **case,
        "failure_code": failed_payout.get("failure_code", case["kind"]),
        "status": failed_payout.get("status", "not taken in"),
        "retry_payout_id": failed_payout.get("retry_payout_id"),
        "seconds_to_paid": seconds_to_paid(steps),
        "error": error,
    }
    if draft is None:
        # Nothing was drafted, so there is nothing to score. The row is not in the scores.
        return {**row, "model": None, "text": "", "accurate": None, "quality": None}
    facts = dict(draft["inputs"])
    draft_kind = facts.pop("draft")
    text = failed_payout["seller_message"] or failed_payout["escalation_note"] or ""
    scores = evals.evaluate_case(provider, draft_kind, facts, text)
    if scores["judge_failed"]:
        # Ask the judge once more before the case counts as a judge failure.
        again = evals.judge(provider, draft_kind, facts, text)
        scores |= {
            "quality": again.rating,
            "quality_reasoning": again.reasoning,
            "judge_failed": again.judge_failed,
        }
    return {
        **row,
        **scores,
        "model": draft["model"],
        "draft_error": draft["outputs"].get("error"),
        "text": text,
    }


def _criteria(rows: list[dict], sellers: dict, cases_per_kind: int) -> dict:
    """The three evaluation criteria, measured on the case rows.

    A fixable case that the seed left out, or that Stripe stopped, counts against criteria
    1 and 3: both need every expected fixable case to reach a paid retry.
    """
    expected = cases_per_kind * sum(retry_permitted(kind) for kind in sellers)
    fixable = [row for row in rows if retry_permitted(row["failure_code"])]
    others = [row for row in rows if not retry_permitted(row["failure_code"])]
    paid = [row for row in fixable if row["status"] == PAID]
    # `accurate` includes the check for the raw failure code on line one of the note.
    escalated = [row for row in others if row["status"] == ESCALATED and row["accurate"]]
    times = [row["seconds_to_paid"] for row in rows if row["seconds_to_paid"] is not None]
    all_paid = bool(expected) and len(paid) == expected == len(fixable)
    return {
        "1_paid_retry_on_fixable_cases": {
            "fixable_cases_expected": expected,
            "fixable_cases": len(fixable),
            "paid": len(paid),
            # Of the fixable cases the set should have, so a case left out counts against it.
            "auto_resolution_rate": len(paid) / expected if expected else None,
            "met": all_paid if expected else None,
        },
        "2_escalation_of_other_cases": {
            "other_cases": len(others),
            "escalated_with_an_accurate_note": len(escalated),
            "met": len(escalated) == len(others) if others else None,
        },
        "3_seconds_from_first_step_to_paid": {
            "limit": CRITERION_3_LIMIT_SECONDS,
            "paid_cases": len(times),
            "median": round(median(times), 1) if times else None,
            "max": round(max(times), 1) if times else None,
            "met": (all_paid and max(times) < CRITERION_3_LIMIT_SECONDS) if expected else None,
            "note": "First step to paid, from steps.created_at, without the time from the "
            "model draft to the approval. That time is the wait for people; the seller fix "
            "is inside it and has no step of its own. The run polls Stripe once a second.",
        },
    }


def run_eval(
    store: Store,
    gateway,
    provider: Provider,
    results_dir: Path,
    run_id: str | None = None,
    sellers: dict = SELLERS,
    cases_per_kind: int = CASES_PER_KIND,
    max_model_calls: int = MAX_MODEL_CALLS,
    log=print,
) -> dict:
    """Seed, run every case, score, and write the results file. The results are returned."""
    run_id = run_id or datetime.now(UTC).strftime("eval-%Y%m%dT%H%M%SZ")
    started_at, clock = utc_now(), time.monotonic()
    counting = CountingProvider(provider, max_model_calls)
    accounts = seed_accounts(gateway, run_id, sellers, log=log)
    cases, fallbacks = seed_failed_payouts(gateway, accounts, run_id, cases_per_kind, log=log)
    seed_seconds = time.monotonic() - clock
    results_dir.mkdir(parents=True, exist_ok=True)
    rows, results = [], {}
    for number, case in enumerate(cases, 1):
        row = run_case(store, gateway, counting, case, run_id)
        rows.append(row)
        log(
            f"run: {number}/{len(cases)} {row['kind']} {row['payout_id']}: {row['status']}, "
            f"accurate {row['accurate']}, quality {row['quality']}, {counting.calls} model calls"
        )
        has_unknown_code = any(row["kind"] == UNKNOWN_CODE_KIND for row in rows)
        results = {
            "run_id": run_id,
            "started_at": started_at,
            "seconds": {
                "seed": round(seed_seconds, 1),
                "run": round(time.monotonic() - clock - seed_seconds, 1),
            },
            "provider_model": ", ".join(sorted({row["model"] for row in rows if row["model"]})),
            "cases_expected": len(sellers) * cases_per_kind,
            # A case with no draft has nothing to score. It is in case_rows only.
            **evals.score([row for row in rows if row["accurate"] is not None]),
            "criteria": _criteria(rows, sellers, cases_per_kind),
            "events": "polled",
            "seller_fix": "simulated",
            "approval": "simulated",
            "paths": {
                "accounts": accounts,
                "fallbacks": fallbacks,
                "unknown_code": "the test debit card 4000056655665572, on Stripe"
                if has_unknown_code
                else "not tested on Stripe",
            },
            "model_calls": counting.calls,
            "cost_usd": round(counting.cost_usd, 4),
            "cost_is_estimate": counting.cost_is_estimate,
            "case_rows": rows,
        }
        # Written after every case, so a run that stops early still leaves what it measured.
        (results_dir / f"{run_id}.json").write_text(json.dumps(results, indent=2) + "\n")
    return results


if __name__ == "__main__":
    from dotenv import load_dotenv

    from payout_rescue.llm import provider_from_env
    from payout_rescue.stripe_gateway import gateway_from_env

    load_dotenv()
    # the cases go into the app's own SQLite file, under the run's id, so one file
    # holds every trace. The page lists only the live rows.
    done = run_eval(
        Store(config.db_path()),
        gateway_from_env(),
        provider_from_env(),
        results_dir=config.results_dir(),
    )
    print(json.dumps({key: value for key, value in done.items() if key != "case_rows"}, indent=2))
