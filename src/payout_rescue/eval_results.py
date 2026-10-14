"""The newest eval results file, made ready for the page."""

import json
from pathlib import Path

from payout_rescue.drafting import ESCALATION_NOTE

# The evaluation criteria, by their key in the results file.
SUCCESS_CRITERIA = {
    "1_paid_retry_on_fixable_cases": (
        "Payout Rescue reaches a paid retry on 100% of fixable-code cases with one human action "
        "(the approval)."
    ),
    "2_escalation_of_other_cases": (
        "Every case with a non-fixable or unknown code is escalated by code with the raw "
        "failure code in the note, and the drafted note passes the accuracy checks."
    ),
    "3_seconds_from_first_step_to_paid": (
        "A full rescue (webhook to paid retry) completes in under 2 minutes of wall-clock "
        "time on the demo machine, excluding the seller button press."
    ),
}
_COUNT_WORDS = ("No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine")


def latest_results_view(results_dir: Path) -> dict | None:
    """What the page shows for the newest eval run. None when there is none it can show."""
    # Run IDs begin with their UTC timestamp, so filename order is chronological.
    files = sorted(results_dir.glob("*.json"))
    try:
        return results_view(json.loads(files[-1].read_text())) if files else None
    except (ValueError, OSError, KeyError, TypeError, AttributeError):
        # a file that is not JSON, has another shape, or has no scored case yet
        # (a run that stopped early) is shown as no eval run. The page must not fail for it.
        return None


def results_view(results: dict) -> dict:
    """What the page shows for one eval run, as text ready to print."""
    cases, expected = results["cases"], results.get("cases_expected", results["cases"])
    return {
        "accuracy": _percent(results["message_accuracy"]),
        "accurate_cases": round(results["message_accuracy"] * cases),
        "cases": cases,
        "quality": f"{results['message_quality']:.1f}",
        "judge_note": _judge_note(results["case_rows"]),
        "criteria": [
            {"text": text, "result": _criterion_result(key, results["criteria"][key])}
            for key, text in SUCCESS_CRITERIA.items()
        ],
        "failure_codes": [
            {
                "failure_code": failure_code,
                "cases": row["cases"],
                "accuracy": _percent(row["message_accuracy"]),
                "quality": f"{row['message_quality']:.1f}",
            }
            for failure_code, row in results["by_failure_code"].items()
        ],
        "facts": [
            f"Cases: {cases}" + (f" of {expected}" if cases != expected else ""),
            f"Model: {results['provider_model']}",
            f"Run: {results['run_id']}",
            f"Events: {results['events']}",
            "Fallbacks: " + ("; ".join(results["paths"]["fallbacks"]) or "none"),
            f"Human steps: approval {results['approval']}, seller fix {results['seller_fix']}",
            f"Model calls: {results['model_calls']}",
            f"Cost: ${results['cost_usd']:.2f}"
            + (" (estimate)" if results["cost_is_estimate"] else ""),
        ],
    }


def _percent(share: float) -> str:
    return f"{share:.0%}"


def _judge_note(case_rows: list[dict]) -> str | None:
    """Says how many escalation notes the judge rated 3 because of the failure code line.

    Code writes that line on purpose, so the note keeps a reader from taking the
    lower rating as a fault of the model.
    """
    # "because of the failure code line" is read from the judge's own sentence by
    # the words "failure code". A judge that says it in other words is not counted.
    count = sum(
        row.get("draft_kind") == ESCALATION_NOTE
        and row["quality"] == 3
        and "failure code" in row["quality_reasoning"].lower()
        for row in case_rows
    )
    if not count:
        return None
    number = _COUNT_WORDS[count] if count < len(_COUNT_WORDS) else str(count)
    return (
        f"{number} escalation note{'s' if count > 1 else ''} scored 3; the judge marked down "
        "the raw failure code on line one, which code writes on purpose."
    )


def _criterion_result(key: str, measured: dict) -> str:
    met = {True: "Met", False: "Not met", None: "Not measured"}[measured["met"]]
    if key == "1_paid_retry_on_fixable_cases":
        paid, expected = measured["paid"], measured["fixable_cases_expected"]
        rate = _percent(measured["auto_resolution_rate"])
        detail = f"{paid} of {expected} fixable cases reached a paid retry ({rate})."
    elif key == "2_escalation_of_other_cases":
        detail = (
            f"{measured['escalated_with_an_accurate_note']} of {measured['other_cases']} other "
            "cases were escalated with an accurate note."
        )
    elif measured["paid_cases"]:
        detail = (
            f"median {measured['median']} seconds, max {measured['max']} seconds, on "
            f"{measured['paid_cases']} paid retries (limit {measured['limit']} seconds)."
        )
    else:
        detail = "no retry was paid."
    return f"{met}: {detail}"
