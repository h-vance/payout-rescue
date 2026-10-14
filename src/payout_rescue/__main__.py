"""The process entrypoint that scripts/run.sh starts. It reads .env; `create_app` never does."""

import logging
import os

import uvicorn
from dotenv import load_dotenv

from payout_rescue.app import create_app
from payout_rescue.llm import provider_from_env
from payout_rescue.stripe_gateway import gateway_from_env

if __name__ == "__main__":
    load_dotenv()
    # Show the app's own info lines, such as the one for each draft, in the terminal.
    logging.basicConfig(level=logging.INFO)
    # Chosen once at start, so a wrong LLM_PROVIDER stops the app before the first webhook.
    provider = provider_from_env()
    # The same for Stripe: a key that is not the sandbox's stops the app here.
    gateway = gateway_from_env()
    uvicorn.run(
        create_app(provider=provider, gateway=gateway), port=int(os.environ.get("PORT", "8000"))
    )
