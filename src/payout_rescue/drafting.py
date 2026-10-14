"""Drafting: the model writes the seller message or the escalation note. It decides nothing."""

import json

from payout_rescue import config
from payout_rescue.classification import failure_reason
from payout_rescue.llm import Completion, FakeProvider, Provider, ProviderError
from payout_rescue.money import format_amount

SELLER_MESSAGE = "seller_message"
ESCALATION_NOTE = "escalation_note"

_FAILURE_CODE_PREFIX = "failure code:"
_RULES = (
    "Use only the facts in the JSON you are given. Give the failure reason from the "
    "failure_reason field; if that field says the reason is not known, say that the bank "
    "returned the payout and that the reason is not known, and do not guess one. State the "
    "payout amount exactly as given. Do not give dates, deadlines or promises. Do not give "
    "any number that is not in the JSON. Write plain text, no markdown, under 120 words."
)
_SYSTEM_PROMPTS = {
    SELLER_MESSAGE: (
        "You write a short message from a marketplace to one of its sellers. A payout to the "
        "seller's bank account failed. Say why it failed, in plain words. Tell the seller the "
        "one thing to do: send new bank account details. " + _RULES
    ),
    ESCALATION_NOTE: (
        "You write a short note for a person on a marketplace's finance team. A payout to a "
        "seller failed and software cannot fix it, so a person must look at it. Say what "
        "failed and why. Do not tell them to retry the payout. The note already starts with "
        "the raw failure code, so do not write that line again. " + _RULES
    ),
}


def failure_code_line(failure_code: str | None) -> str:
    """The first line of every escalation note. Code writes it, so it never needs the model."""
    return f"{_FAILURE_CODE_PREFIX} {failure_code or 'none given'}"


def escalation_note(failure_code: str | None, drafted_text: str = "") -> str:
    """The raw failure code line, then the model's text with any echo of that line removed."""
    kept = [
        line
        for line in drafted_text.strip().splitlines()
        if not line.strip().lower().startswith(_FAILURE_CODE_PREFIX)
    ]
    return "\n".join([failure_code_line(failure_code), *kept]).strip()


def build_draft_input(payout: dict, connected_account: dict, external_account: dict | None) -> dict:
    """What the model is given: the payout, the connected account and the external account.

    Only the fields a draft needs are passed on, so nothing else about the seller reaches
    the model or the trace.
    """
    amount = format_amount(payout["amount"], payout["currency"])
    failure_code = payout.get("failure_code")
    return {
        "payout": {
            "id": payout["id"],
            "amount": amount,
            "failure_code": failure_code,
            "failure_reason": failure_reason(failure_code) or "The reason is not known.",
        },
        "connected_account": {
            "id": connected_account["id"],
            "name": (connected_account.get("business_profile") or {}).get("name"),
        },
        "external_account": external_account
        and {
            "id": external_account["id"],
            "bank_name": external_account.get("bank_name"),
            "last4": external_account.get("last4"),
        },
    }


def draft(provider: Provider, kind: str, draft_input: dict) -> Completion:
    """One model call. A reply with no text is a provider error, like any other failure."""
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPTS[kind]},
        {"role": "user", "content": json.dumps(draft_input, indent=2)},
    ]
    completion = provider.complete(messages, [])
    if not completion.text.strip():
        raise ProviderError("The model returned no text.")
    return completion


def cost_usd(completion: Completion) -> tuple[float, bool]:
    """The cost of one draft, and whether it is the provider's own estimate."""
    if completion.cost_estimate_usd is not None:
        return completion.cost_estimate_usd, True
    if completion.model == FakeProvider.MODEL:
        return 0.0, False  # no model ran, so nothing was spent
    usage = completion.usage
    cost = (
        usage.input_tokens * config.INPUT_PRICE_USD_PER_MTOK
        + usage.output_tokens * config.OUTPUT_PRICE_USD_PER_MTOK
    ) / 1_000_000
    return cost, False
