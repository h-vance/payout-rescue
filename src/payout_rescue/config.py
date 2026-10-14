"""Values a person sets by hand."""

import os
from pathlib import Path

# List prices in USD per million tokens, used to cost a model draft when the provider reports
# no cost of its own (fake, claude-api). claude-cli reports its own estimate.
# These are the prices of claude-opus-5-5, the default claude-api model.
INPUT_PRICE_USD_PER_MTOK = 4.00
OUTPUT_PRICE_USD_PER_MTOK = 20.00
PRICES_SET_ON = "2026-10-11"

# The three invented numbers of the Acme Market story. They are not measured and
# not from a customer. The page shows them in a box that says so.
ILLUSTRATIVE_ASSUMPTIONS = (
    "Acme Market pays 4,000 sellers each week.",
    "2% of those payouts fail.",
    "A failed payout takes 3 days to resolve by hand.",
)


def db_path() -> str:
    """The one SQLite file. The app and the eval run both use it."""
    return os.environ.get("PAYOUT_RESCUE_DB", "payout_rescue.db")


def results_dir() -> Path:
    """Where the eval run writes its results files and the page reads the newest one."""
    return Path(os.environ.get("PAYOUT_RESCUE_RESULTS", "evals/results"))
