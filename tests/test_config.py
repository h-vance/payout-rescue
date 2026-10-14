from pathlib import Path

from payout_rescue import config


def test_the_app_and_the_eval_run_get_their_paths_from_the_same_two_places(monkeypatch):
    monkeypatch.delenv("PAYOUT_RESCUE_DB", raising=False)
    monkeypatch.delenv("PAYOUT_RESCUE_RESULTS", raising=False)
    assert (config.db_path(), config.results_dir()) == ("payout_rescue.db", Path("evals/results"))

    monkeypatch.setenv("PAYOUT_RESCUE_DB", "/tmp/other.db")
    monkeypatch.setenv("PAYOUT_RESCUE_RESULTS", "/tmp/results")
    assert (config.db_path(), config.results_dir()) == ("/tmp/other.db", Path("/tmp/results"))
