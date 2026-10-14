import sqlite3

import pytest

from payout_rescue.store import Store

FAILED_PAYOUT = {
    "payout_id": "po_failed_1",
    "run_id": "live",
    "account_id": "acct_seller_1",
    "external_account_id": "ba_closed_1",
    "failure_code": "account_closed",
    "amount": 12500,
    "currency": "usd",
    "status": "new",
}


def test_store_creates_both_tables(tmp_path):
    path = tmp_path / "test.db"
    Store(path)

    tables = {
        row[0]
        for row in sqlite3.connect(path).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }

    assert {"failed_payouts", "steps"} <= tables


def test_store_reads_back_a_failed_payout(tmp_path):
    store = Store(tmp_path / "test.db")

    store.insert_failed_payout(FAILED_PAYOUT)
    row = store.get_failed_payout("po_failed_1")

    assert row["failure_code"] == "account_closed"
    assert row["amount"] == 12500
    assert row["status"] == "new"
    assert row["seller_message"] is None
    assert row["escalation_note"] is None
    assert row["retry_payout_id"] is None
    assert row["created_at"] and row["updated_at"]


def test_store_reads_back_a_step_for_its_payout(tmp_path):
    store = Store(tmp_path / "test.db")
    store.insert_failed_payout(FAILED_PAYOUT)

    store.insert_step(
        {
            "payout_id": "po_failed_1",
            "run_id": "live",
            "type": "classification",
            "inputs": {"failure_code": "account_closed"},
            "outputs": {"retry_permitted": True},
        }
    )
    steps = store.steps_for("po_failed_1")

    assert len(steps) == 1
    assert steps[0]["type"] == "classification"
    assert steps[0]["inputs"] == {"failure_code": "account_closed"}
    assert steps[0]["outputs"] == {"retry_permitted": True}
    assert steps[0]["model"] is None
    assert steps[0]["created_at"]


def test_store_rejects_an_unknown_status(tmp_path):
    store = Store(tmp_path / "test.db")

    with pytest.raises(sqlite3.IntegrityError):
        store.insert_failed_payout({**FAILED_PAYOUT, "status": "recovered"})


def test_store_rejects_an_unknown_step_type(tmp_path):
    store = Store(tmp_path / "test.db")
    store.insert_failed_payout(FAILED_PAYOUT)

    with pytest.raises(sqlite3.IntegrityError):
        store.insert_step({"payout_id": "po_failed_1", "run_id": "live", "type": "policy_block"})


def test_store_reads_back_a_failed_payout_with_no_failure_code(tmp_path):
    store = Store(tmp_path / "test.db")

    store.insert_failed_payout({**FAILED_PAYOUT, "failure_code": None})

    assert store.get_failed_payout("po_failed_1")["failure_code"] is None


def test_store_refuses_to_change_a_failed_payout_that_is_not_stored(tmp_path):
    store = Store(tmp_path / "test.db")

    with pytest.raises(LookupError):
        store.update_failed_payout("po_unknown", status="escalated")
    assert store.update_failed_payout("po_unknown", expected_status="new", status="paid") is False
