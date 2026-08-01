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


def test_telegram_webhook_checks_secret_chat_and_duplicate(
    settings,
    monkeypatch,
):
    answers = []
    monkeypatch.setattr(
        "jin_market_pulse.server.handle_market_query",
        lambda text, _settings, _store: __import__(
            "jin_market_pulse.bot_queries",
            fromlist=["BotResponse"],
        ).BotResponse(f"<b>{text}</b>"),
    )
    monkeypatch.setattr(
        "jin_market_pulse.server.TelegramClient.send",
        lambda self, text, parse_mode=None, **_kwargs: answers.append((text, parse_mode)),
    )
    client = TestClient(create_app(settings))
    payload = {
        "update_id": 7001,
        "message": {
            "chat": {"id": settings.telegram_chat_id},
            "text": "비트 얼마야",
        },
    }

    assert client.post("/telegram/webhook", json=payload).status_code == 401
    response = client.post(
        "/telegram/webhook",
        json=payload,
        headers={
            "X-Telegram-Bot-Api-Secret-Token": settings.telegram_webhook_secret
        },
    )
    duplicate = client.post(
        "/telegram/webhook",
        json=payload,
        headers={
            "X-Telegram-Bot-Api-Secret-Token": settings.telegram_webhook_secret
        },
    )

    assert response.json() == {"status": "sent"}
    assert duplicate.json() == {"status": "duplicate"}
    assert answers == [("<b>비트 얼마야</b>", "HTML")]


def test_telegram_webhook_ignores_other_chat(settings):
    client = TestClient(create_app(settings))
    response = client.post(
        "/telegram/webhook",
        json={
            "update_id": 8001,
            "message": {"chat": {"id": "not-allowed"}, "text": "/markets"},
        },
        headers={
            "X-Telegram-Bot-Api-Secret-Token": settings.telegram_webhook_secret
        },
    )

    assert response.json() == {"status": "ignored"}
