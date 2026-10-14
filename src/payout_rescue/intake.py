"""Intake: what the app does with one Stripe payout event.

A payout.failed event becomes one stored failed payout, classified and drafted. An event
for a retry gives the outcome of that retry.
"""

import logging
import threading

from payout_rescue import drafting
from payout_rescue.classification import ESCALATION, classify, retry_permitted
from payout_rescue.eval_seed import TAG_KEY as EVAL_TAG_KEY
from payout_rescue.llm import Provider, ProviderError, provider_from_env
from payout_rescue.rescue import record_retry_outcome
from payout_rescue.store import (
    AWAITING_SELLER,
    CLASSIFICATION_STEP,
    ESCALATED,
    MODEL_DRAFT_STEP,
    NEW,
    Store,
)
from payout_rescue.stripe_gateway import StripeGateway

LIVE_RUN_ID = "live"
logger = logging.getLogger(__name__)
_PAYOUT_EVENTS = ("payout.paid", "payout.failed")
_draft_retry_lock = threading.Lock()


def retry_failed_draft(store: Store, provider: Provider | None, payout_id: str) -> None:
    """Retry a failed live seller message from its recorded facts, without Stripe writes.

    An ordinary duplicate webhook remains a no-op. This explicit action preserves every
    failed attempt, and a second click after success does not draft again.
    """
    with _draft_retry_lock:
        row = store.get_failed_payout(payout_id)
        if not row or row["run_id"] != LIVE_RUN_ID or row["status"] != NEW:
            return
        attempt = next(
            (s for s in reversed(store.steps_for(payout_id)) if s["type"] == MODEL_DRAFT_STEP),
            None,
        )
        if not attempt or "error" not in (attempt["outputs"] or {}):
            return
        facts = {key: value for key, value in attempt["inputs"].items() if key != "draft"}
        _draft(store, provider, payout_id, row["run_id"], row["failure_code"], False, facts)


def take_in_payout_event(
    store: Store, gateway: StripeGateway, provider: Provider | None, event: dict
) -> None:
    """Act on one verified webhook event. Any event that is not the live app's is ignored."""
    # Only payouts of connected accounts count. The platform's own payouts carry no account
    # id and are not sellers' payouts.
    if event["type"] not in _PAYOUT_EVENTS or not event.get("account"):
        return
    payout = event["data"]["object"]
    # An eval run handles its own payouts, by polling. If the app took them in too, each
    # would be drafted twice and would fill the page.
    retried = store.get_failed_payout_by_retry(payout["id"])
    if EVAL_TAG_KEY in (payout.get("metadata") or {}) or (
        retried and retried["run_id"] != LIVE_RUN_ID
    ):
        return
    # Stripe sends payout.paid and then payout.failed for a payout that fails, and that
    # payout.paid can already carry the status `failed`. So the payout's
    # own status counts as well as the event type.
    failed = event["type"] == "payout.failed" or payout.get("status") == "failed"
    outcome = "failed" if failed else "paid"
    is_retry = record_retry_outcome(store, payout["id"], outcome, payout.get("failure_code"))
    if failed and not is_retry:
        take_in_failed_payout(store, gateway, provider, event["account"], payout["id"])


def take_in_failed_payout(
    store: Store,
    gateway: StripeGateway,
    provider: Provider | None,
    account_id: str,
    payout_id: str,
    run_id: str = LIVE_RUN_ID,
) -> None:
    """Store, classify and draft. Safe to run again for the same payout id.

    A duplicate webhook does not start a second rescue. A delivery that failed after the
    row was stored but before the draft was recorded is finished by Stripe's retry.
    """
    if any(step["type"] == MODEL_DRAFT_STEP for step in store.steps_for(payout_id)):
        return
    # If a read fails, no partial row is stored. The webhook reports the failure and logs
    # the local recovery command so the operator can retry intake without a new payout.
    payout = gateway.get_payout(account_id, payout_id)
    external_account_id = payout.get("destination")
    connected_account = gateway.get_connected_account(account_id)
    external_account = (
        gateway.get_external_account(account_id, external_account_id)
        if external_account_id
        else None
    )
    failure_code = payout.get("failure_code")
    classification = classify(failure_code)
    escalation = classification == ESCALATION
    # The row and its classification step are stored together. For an escalation the raw
    # failure code line is stored here too, so it never depends on the model.
    store.insert_failed_payout(
        {
            "payout_id": payout_id,
            "run_id": run_id,
            "account_id": account_id,
            "external_account_id": external_account_id,
            "failure_code": failure_code,
            "amount": payout["amount"],
            "currency": payout["currency"],
            "status": ESCALATED if escalation else NEW,
            "escalation_note": drafting.escalation_note(failure_code) if escalation else None,
        },
        steps=[
            {
                "payout_id": payout_id,
                "run_id": run_id,
                "type": CLASSIFICATION_STEP,
                "inputs": {"failure_code": failure_code},
                "outputs": {
                    "retry_permitted": retry_permitted(failure_code),
                    "classification": classification,
                },
            }
        ],
    )
    draft_input = drafting.build_draft_input(payout, connected_account, external_account)
    _draft(store, provider, payout_id, run_id, failure_code, escalation, draft_input)


def _draft(
    store: Store,
    provider: Provider | None,
    payout_id: str,
    run_id: str,
    failure_code: str | None,
    escalation: bool,
    draft_input: dict,
) -> None:
    kind = drafting.ESCALATION_NOTE if escalation else drafting.SELLER_MESSAGE
    step = {
        "payout_id": payout_id,
        "run_id": run_id,
        "type": MODEL_DRAFT_STEP,
        "inputs": {"draft": kind, **draft_input},
    }
    try:
        completion = drafting.draft(provider or provider_from_env(), kind, draft_input)
    except ProviderError as error:
        # The classification stands. With a fixable failure code the failed payout stays
        # `new`, with no seller message.
        store.insert_step({**step, "outputs": {"error": str(error)}})
        return
    cost, is_estimate = drafting.cost_usd(completion)
    step |= {
        "outputs": {"text": completion.text, "cost_is_estimate": is_estimate},
        "model": completion.model,
        "input_tokens": completion.usage.input_tokens,
        "output_tokens": completion.usage.output_tokens,
        "cost_usd": cost,
    }
    if escalation:
        text = drafting.escalation_note(failure_code, completion.text)
        store.update_failed_payout(payout_id, steps=[step], escalation_note=text)
    else:
        text = completion.text
        store.update_failed_payout(
            payout_id, steps=[step], status=AWAITING_SELLER, seller_message=text
        )
    # The drafted message is shown on the page and written to a log line.
    logger.info("%s drafted for %s: %s", kind, payout_id, text.strip().splitlines()[0])
