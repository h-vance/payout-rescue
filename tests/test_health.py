import os

from fastapi.testclient import TestClient

from payout_rescue.app import create_app


def test_health_route_returns_200(tmp_path):
    client = TestClient(create_app(db_path=tmp_path / "test.db"), base_url="http://localhost")

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_app_never_reads_the_env_file(tmp_path, monkeypatch):
    # Integration tests are skipped when STRIPE_SECRET_KEY is unset. That only works if
    # building the app does not load .env into the test process.
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)

    create_app(db_path=tmp_path / "test.db")

    assert "STRIPE_SECRET_KEY" not in os.environ
