"""The one SQLite file: failed payouts and the steps of their traces."""

import json
import sqlite3
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

# The statuses of a failed payout and the types of a step. They are named here once, and
# the tables refuse any other value.
STATUSES = (NEW, AWAITING_SELLER, AWAITING_APPROVAL, RETRYING, PAID, ESCALATED) = (
    "new",
    "awaiting_seller",
    "awaiting_approval",
    "retrying",
    "paid",
    "escalated",
)
STEP_TYPES = (CLASSIFICATION_STEP, MODEL_DRAFT_STEP, APPROVAL_STEP, RETRY_STEP) = (
    "classification",
    "model_draft",
    "approval",
    "retry",
)


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS failed_payouts (
    payout_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    external_account_id TEXT,
    failure_code TEXT,
    amount INTEGER NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ({_sql_list(STATUSES)})),
    seller_message TEXT,
    escalation_note TEXT,
    retry_payout_id TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payout_id TEXT NOT NULL REFERENCES failed_payouts (payout_id),
    run_id TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ({_sql_list(STEP_TYPES)})),
    inputs TEXT,
    outputs TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    actor TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""


def utc_now() -> str:
    """The time now, in the format the tables use for `created_at`."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# Step columns stored as JSON text.
_JSON_COLUMNS = ("inputs", "outputs")


class Store:
    def __init__(self, path: str | Path):
        # One connection is shared by the webhook and the page's request threads. The lock
        # keeps each transaction whole.
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)

    def insert_failed_payout(self, row: dict, steps: Sequence[dict] = ()) -> bool:
        """Store a failed payout and its first steps, all or nothing.

        False, and nothing stored, when a failed payout with this payout id is already stored.
        """
        with self._lock, self._db:
            conflict = "ON CONFLICT (payout_id) DO NOTHING"
            inserted = self._insert("failed_payouts", row, conflict) == 1
            if inserted:
                for step in steps:
                    self._insert("steps", self._encode_step(step))
        return inserted

    def update_failed_payout(
        self,
        payout_id: str,
        steps: Sequence[dict] = (),
        expected_status: str | None = None,
        **changes,
    ) -> bool:
        """Change a failed payout and add the steps that go with the change, all or nothing.

        With `expected_status`, nothing is changed unless the row has that status, and the
        result says whether the change was made. Without it, a missing row is an error.
        """
        # Column names come from this package's callers, never from a request.
        assignments = "".join(f"{column} = ?, " for column in changes)
        guard = " AND status = ?" if expected_status else ""
        values = (*changes.values(), payout_id, *([expected_status] if expected_status else []))
        with self._lock, self._db:
            cursor = self._db.execute(
                f"UPDATE failed_payouts SET {assignments}"
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                f"WHERE payout_id = ?{guard}",
                values,
            )
            if cursor.rowcount != 1:
                if expected_status is None:
                    raise LookupError(f"No failed payout has the payout id {payout_id}.")
                return False
            for step in steps:
                self._insert("steps", self._encode_step(step))
        return True

    def get_failed_payout(self, payout_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM failed_payouts WHERE payout_id = ?", (payout_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_failed_payout_by_retry(self, retry_payout_id: str) -> dict | None:
        """The failed payout that this payout is the retry of."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM failed_payouts WHERE retry_payout_id = ?", (retry_payout_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_failed_payouts(self, run_id: str | None = None) -> list[dict]:
        """Every failed payout, newest first. With a run id, only that run's."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM failed_payouts WHERE ?1 IS NULL OR run_id = ?1 "
                "ORDER BY created_at DESC, rowid DESC",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def insert_step(self, row: dict) -> None:
        with self._lock, self._db:
            self._insert("steps", self._encode_step(row))

    def steps_for(self, payout_id: str) -> list[dict]:
        """The trace of one failed payout, oldest step first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM steps WHERE payout_id = ? ORDER BY id", (payout_id,)
            ).fetchall()
        return [self._decode_step(row) for row in rows]

    def _insert(self, table: str, row: dict, on_conflict: str = "") -> int:
        # Table, column names and the conflict clause come from this module, never from a
        # request. The caller owns the transaction.
        columns = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        cursor = self._db.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({marks}) {on_conflict}", tuple(row.values())
        )
        return cursor.rowcount

    @staticmethod
    def _encode_step(row: dict) -> dict:
        return {
            key: json.dumps(value) if key in _JSON_COLUMNS else value for key, value in row.items()
        }

    @staticmethod
    def _decode_step(row: sqlite3.Row) -> dict:
        step = dict(row)
        for key in _JSON_COLUMNS:
            if step[key] is not None:
                step[key] = json.loads(step[key])
        return step
