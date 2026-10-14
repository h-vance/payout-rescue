"""A rescue after the draft: the seller's fix, the approval, and the outcome of the retry."""

import threading

from payout_rescue import drafting
from payout_rescue.store import (
    APPROVAL_STEP,
    AWAITING_APPROVAL,
    AWAITING_SELLER,
    ESCALATED,
    PAID,
    RETRY_STEP,
    RETRYING,
    Store,
    utc_now,
)
from payout_rescue.stripe_gateway import StripeGateway

APPROVER = "demo operator"  # the local page has no login

SELLER_FIX = "seller_fix"
APPROVAL = "approval"
# The one place that says which status permits which action. The page reads it too.
_ACTION_FOR_STATUS = {AWAITING_SELLER: SELLER_FIX, AWAITING_APPROVAL: APPROVAL}

# The statuses a retry outcome may change. `paid` is not final: Stripe sends payout.paid
# before payout.failed for a payout that fails.
# The keys are Stripe's words for how a payout ended.
_STATUSES_BEFORE_OUTCOME = {"paid": (RETRYING,), "failed": (RETRYING, PAID)}

# One action at a time, so a double click cannot run the same action twice at once. A retry
# outcome waits for it too, so it never arrives before the approval has stored the retry id.
# This process-wide lock is held across Stripe calls.
_ACTION_LOCK = threading.Lock()


def permitted_action(failed_payout: dict) -> str | None:
    return _ACTION_FOR_STATUS.get(failed_payout["status"])


def record_seller_fix(store: Store, gateway: StripeGateway, payout_id: str) -> None:
    """Add the good test external account, then wait for the approval. Does nothing twice."""
    with _ACTION_LOCK:
        failed_payout = store.get_failed_payout(payout_id)
        if permitted_action(failed_payout) != SELLER_FIX:
            return
        # If the gateway refuses, nothing is stored and the button is still there.
        gateway.add_external_account(failed_payout["account_id"])
        store.update_failed_payout(
            payout_id, expected_status=AWAITING_SELLER, status=AWAITING_APPROVAL
        )


def approve_retry(
    store: Store, gateway: StripeGateway, payout_id: str, actor: str = APPROVER
) -> None:
    """Record the approval and create the retry. A second approval does nothing.

    `actor` is who gave the approval: the person at the page, unless a caller says otherwise.
    """
    with _ACTION_LOCK:
        failed_payout = store.get_failed_payout(payout_id)
        if permitted_action(failed_payout) != APPROVAL:
            return
        approved_at = utc_now()
        # No money moves before this point. If the gateway refuses, nothing is stored, and
        # the approval can be given again. Stripe returns the same payout for the same
        # idempotency key, so the retry is never created twice.
        # A crash between the API call and local storage can leave an unlinked retry.
        # Stripe also replays a refusal it stored for this key.
        retry = gateway.create_payout(
            failed_payout["account_id"],
            failed_payout["amount"],
            failed_payout["currency"],
            idempotency_key=payout_id,
        )
        step = {"payout_id": payout_id, "run_id": failed_payout["run_id"]}
        store.update_failed_payout(
            payout_id,
            expected_status=AWAITING_APPROVAL,
            status=RETRYING,
            retry_payout_id=retry["id"],
            steps=[
                {**step, "type": APPROVAL_STEP, "actor": actor, "created_at": approved_at},
                {
                    **step,
                    "type": RETRY_STEP,
                    "inputs": {"idempotency_key": payout_id},
                    "outputs": {"retry_payout_id": retry["id"]},
                },
            ],
        )


def record_retry_outcome(
    store: Store, retry_payout_id: str, outcome: str, failure_code: str | None
) -> bool:
    """Apply Stripe's outcome for a retry to the failed payout it replaces.

    `outcome` is the retry's status at Stripe: `paid` or `failed`. The result is False when
    the payout is not a retry. A failed retry ends in escalation and is never taken in as a
    new failed payout. Code writes its whole note: there is no new model draft for it.
    """
    with _ACTION_LOCK:
        failed_payout = store.get_failed_payout_by_retry(retry_payout_id)
        if not failed_payout:
            return False
        if failed_payout["status"] not in _STATUSES_BEFORE_OUTCOME[outcome]:
            return True
        if outcome == "paid":
            changes = {"status": PAID}
            outputs = {"status": outcome}
        else:
            note = drafting.escalation_note(failure_code, f"The retry {retry_payout_id} failed.")
            changes = {"status": ESCALATED, "escalation_note": note}
            outputs = {"status": outcome, "failure_code": failure_code}
        store.update_failed_payout(
            failed_payout["payout_id"],
            steps=[
                {
                    "payout_id": failed_payout["payout_id"],
                    "run_id": failed_payout["run_id"],
                    "type": RETRY_STEP,
                    "inputs": {"retry_payout_id": retry_payout_id},
                    "outputs": outputs,
                }
            ],
            **changes,
        )
        return True
