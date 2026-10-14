"""The trace as the page shows it: one row per step of one failed payout."""

import json


def trace_rows(steps: list[dict]) -> list[dict]:
    """Display values for each step, in the order given. An empty string means "nothing"."""
    return [
        {
            "time": step["created_at"],
            "type": step["type"],
            "actor": step["actor"] or "",
            "inputs": _as_text(step["inputs"]),
            "outputs": _as_text(step["outputs"]),
            "model": step["model"] or "",
            "tokens": _tokens(step),
            "cost": _cost(step),
        }
        for step in steps
    ]


def _as_text(value: dict | None) -> str:
    return "" if value is None else json.dumps(value, ensure_ascii=False)


def _tokens(step: dict) -> str:
    if step["input_tokens"] is None:
        return ""
    return f"{step['input_tokens']:,} in, {step['output_tokens']:,} out"


def _cost(step: dict) -> str:
    if step["cost_usd"] is None:
        return ""
    # A cost the provider reported itself is its estimate. Any other cost is
    # tokens times the two config prices.
    estimate = " estimate" if (step["outputs"] or {}).get("cost_is_estimate") else ""
    # Four decimals, because one draft costs a few cents or less.
    return f"${step['cost_usd']:.4f}{estimate}" if step["cost_usd"] else "$0.00"
