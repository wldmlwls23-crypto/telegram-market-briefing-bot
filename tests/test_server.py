from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from jin_market_pulse.app import MarketPulseApp
from jin_market_pulse.server import create_app


def test_morning_job_rejects_missing_or_wrong_secret(settings):
    secured = replace(settings, cron_secret="a-secure-test-secret")
    client = TestClient(create_app(secured))

    assert client.post("/jobs/morning").status_code == 401
    assert (
        client.post(
            "/jobs/morning",
            headers={"Authorization": "Bearer wrong-secret-value"},
        ).status_code
        == 401
    )


def test_morning_job_runs_with_valid_secret(settings, monkeypatch):
    secured = replace(settings, cron_secret="a-secure-test-secret")
    monkeypatch.setattr(MarketPulseApp, "send_morning_report", lambda self: "sent")
    client = TestClient(create_app(secured))

    response = client.post(
        "/jobs/morning",
        headers={"Authorization": "Bearer a-secure-test-secret"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "sent"}


def test_health_does_not_expose_configuration(settings):
    response = TestClient(create_app(settings)).get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "jin-market-pulse",
        "mode": "serverless",
    }
