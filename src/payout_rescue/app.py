import json
import logging
import os
import shlex
from pathlib import Path

import stripe
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from payout_rescue import config
from payout_rescue import store as store_module
from payout_rescue.eval_results import latest_results_view
from payout_rescue.intake import LIVE_RUN_ID, retry_failed_draft, take_in_payout_event
from payout_rescue.llm import Provider
from payout_rescue.money import format_amount
from payout_rescue.rescue import approve_retry, permitted_action, record_seller_fix
from payout_rescue.store import Store
from payout_rescue.stripe_gateway import FakeStripeGateway, GatewayError, StripeGateway
from payout_rescue.trace import trace_rows

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
logger = logging.getLogger(__name__)

# What a person reads for each status. The statuses are the ones the Store accepts.
STATUS_LABELS = {
    store_module.NEW: "Not drafted",
    store_module.AWAITING_SELLER: "Waiting for the seller",
    store_module.AWAITING_APPROVAL: "Waiting for approval",
    store_module.RETRYING: "Retry created",
    store_module.PAID: "Paid",
    store_module.ESCALATED: "Escalated to a person",
}


def create_app(
    db_path: str | Path | None = None,
    gateway: StripeGateway | None = None,
    webhook_secret: str | None = None,
    provider: Provider | None = None,
    results_dir: str | Path | None = None,
) -> FastAPI:
    app = FastAPI(title="Payout Rescue")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])

    @app.middleware("http")
    async def protect_local_actions(request: Request, call_next):
        # Signed webhooks authenticate separately. Browser actions must originate here.
        if request.method == "POST" and request.url.path != "/webhooks/stripe":
            origin = request.headers.get("origin")
            expected_origin = f"{request.url.scheme}://{request.url.netloc}"
            if (origin is not None and origin != expected_origin) or request.headers.get(
                "sec-fetch-site"
            ) == "cross-site":
                return PlainTextResponse("Cross-site action refused.", status_code=403)
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        return response

    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    store = Store(db_path or config.db_path())
    app.state.store = store
    # The process entrypoint passes the real gateway. With none given, nothing reaches Stripe.
    gateway = gateway or FakeStripeGateway()
    results_dir = Path(results_dir or config.results_dir())

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    def require_failed_payout(payout_id: str) -> None:
        if not store.get_failed_payout(payout_id):
            raise HTTPException(404, "No failed payout has this payout id.")

    def render_page(request: Request, error_for: str | None = None, error: str | None = None):
        failed_payouts = [
            {
                **row,
                "status_label": STATUS_LABELS[row["status"]],
                "amount_display": format_amount(row["amount"], row["currency"]),
                "action": permitted_action(row),
                "draft_failed": row["status"] == store_module.NEW,
                "can_retry_draft": row["status"] == store_module.NEW
                and any(
                    s["type"] == store_module.MODEL_DRAFT_STEP and "error" in (s["outputs"] or {})
                    for s in store.steps_for(row["payout_id"])
                ),
                "error": error if row["payout_id"] == error_for else None,
                "trace": trace_rows(store.steps_for(row["payout_id"])),
            }
            # The eval run keeps its cases in the same file, under its own run id.
            for row in store.list_failed_payouts(LIVE_RUN_ID)
        ]
        return TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {
                "failed_payouts": failed_payouts,
                "prices_set_on": config.PRICES_SET_ON,
                # Read on every request, so a new eval run shows without a restart.
                "eval": latest_results_view(results_dir),
                "assumptions": config.ILLUSTRATIVE_ASSUMPTIONS,
            },
            status_code=502 if error else 200,
        )

    def run_action(action, request: Request, payout_id: str):
        """Run one button's action. A gateway error is shown under that failed payout."""
        require_failed_payout(payout_id)
        try:
            action(store, gateway, payout_id)
        except GatewayError as error:
            return render_page(request, error_for=payout_id, error=str(error))
        return RedirectResponse("/", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def page(request: Request):
        return render_page(request)

    @app.post("/failed-payouts/{payout_id}/seller-fix")
    def seller_fix(request: Request, payout_id: str):
        return run_action(record_seller_fix, request, payout_id)

    @app.post("/failed-payouts/{payout_id}/approval")
    def approval(request: Request, payout_id: str):
        return run_action(approve_retry, request, payout_id)

    @app.post("/failed-payouts/{payout_id}/draft")
    def retry_draft(payout_id: str):
        require_failed_payout(payout_id)
        retry_failed_draft(store, provider, payout_id)
        return RedirectResponse("/", status_code=303)

    @app.post("/webhooks/stripe")
    async def stripe_webhook(request: Request) -> dict:
        secret = webhook_secret or os.environ.get("STRIPE_WEBHOOK_SECRET")
        if not secret:
            raise HTTPException(503, "STRIPE_WEBHOOK_SECRET is not set.")
        try:
            payload = (await request.body()).decode("utf-8")
            stripe.WebhookSignature.verify_header(
                payload,
                request.headers.get("Stripe-Signature", ""),
                secret,
                stripe.Webhook.DEFAULT_TOLERANCE,
            )
            event = json.loads(payload)
        except (stripe.SignatureVerificationError, ValueError) as error:
            raise HTTPException(400, "The webhook signature is not valid.") from error
        # Drafting runs synchronously; this process handles one webhook at a time.
        try:
            take_in_payout_event(store, gateway, provider, event)
        except GatewayError as error:
            command = shlex.join(
                [
                    "uv",
                    "run",
                    "python",
                    "-m",
                    "payout_rescue.recover",
                    event["account"],
                    event["data"]["object"]["id"],
                ]
            )
            logger.error("Stripe intake failed. To recover this payout, run: %s", command)
            raise HTTPException(
                503, "Stripe intake failed. See the server log for recovery."
            ) from error
        return {"received": True}

    return app
