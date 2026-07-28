from __future__ import annotations

from dataclasses import replace

from jin_market_pulse.bot_queries import BotResponse
from jin_market_pulse.jobs import (
    process_pending_telegram_updates,
    process_telegram_update,
    report_provider_health,
    run_tick,
)
from jin_market_pulse.state import StateStore


class FakeTelegram:
    def __init__(self, settings=None):
        self.sent = []
        self.callbacks = []

    def send(self, text, **kwargs):
        self.sent.append((text, kwargs))
        return [100 + len(self.sent)]

    def send_action(self, action="typing"):
        return None

    def get_file(self, file_id):
        return b"fake-media"

    def answer_callback(self, callback_query_id, text=""):
        self.callbacks.append(callback_query_id)


def test_callback_query_is_answered_and_processed(
    settings,
    tmp_path,
    monkeypatch,
):
    state = StateStore(tmp_path / "state.sqlite3")
    telegram = FakeTelegram()
    monkeypatch.setattr(
        "jin_market_pulse.jobs.handle_market_query",
        lambda text, _settings, _state: BotResponse(f"<b>{text}</b>"),
    )
    payload = {
        "update_id": 1,
        "callback_query": {
            "id": "callback-one",
            "data": "price:btc",
            "message": {"chat": {"id": settings.telegram_chat_id}},
        },
    }

    process_telegram_update(payload, settings, state, telegram)

    assert telegram.callbacks == ["callback-one"]
    assert telegram.sent[0][0] == "<b>/price btc</b>"


def test_photo_question_uses_limited_image_explanation(
    settings,
    tmp_path,
    monkeypatch,
):
    state = StateStore(tmp_path / "state.sqlite3")
    telegram = FakeTelegram()
    monkeypatch.setattr(
        "jin_market_pulse.jobs.explain_image",
        lambda content, question, _settings: f"<b>{question}:{len(content)}</b>",
    )
    payload = {
        "message": {
            "chat": {"id": settings.telegram_chat_id},
            "caption": "이 지표 설명해줘",
            "photo": [
                {"file_id": "small", "file_size": 10},
                {"file_id": "large", "file_size": 20},
            ],
        }
    }

    process_telegram_update(payload, settings, state, telegram)

    assert "이 지표 설명해줘:10" in telegram.sent[0][0]
    assert state.usage_summary()["image"] == 1


def test_voice_longer_than_sixty_seconds_is_rejected(settings, tmp_path):
    state = StateStore(tmp_path / "state.sqlite3")
    telegram = FakeTelegram()
    payload = {
        "message": {
            "chat": {"id": settings.telegram_chat_id},
            "voice": {"file_id": "voice", "duration": 61},
        }
    }

    process_telegram_update(payload, settings, state, telegram)

    assert "60초 이하" in telegram.sent[0][0]
    assert "voice" not in state.usage_summary()


def test_durable_queue_retries_failed_update(
    settings,
    tmp_path,
    monkeypatch,
):
    state = StateStore(tmp_path / "state.sqlite3")
    state.claim_telegram_update(
        22,
        {"update_id": 22, "message": {"chat": {"id": settings.telegram_chat_id}}},
    )
    monkeypatch.setattr(
        "jin_market_pulse.jobs.process_telegram_update",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert process_pending_telegram_updates(
        settings,
        state,
        FakeTelegram(),
    ) == 0
    pending = state.pending_telegram_updates()
    assert pending[0]["update_id"] == 22
    assert pending[0]["attempts"] == 1


def test_provider_failure_and_recovery_each_notify_once(tmp_path):
    state = StateStore(tmp_path / "state.sqlite3")
    telegram = FakeTelegram()
    for _ in range(3):
        state.record_provider_result("Yahoo", success=False, error="timeout")

    report_provider_health(state, telegram)
    report_provider_health(state, telegram)
    assert len(telegram.sent) == 1
    assert "장애" in telegram.sent[0][0]

    state.record_provider_result("Yahoo", success=True)
    report_provider_health(state, telegram)
    report_provider_health(state, telegram)
    assert len(telegram.sent) == 2
    assert "정상화" in telegram.sent[1][0]


def test_tick_idempotency_blocks_duplicate_slot(
    settings,
    monkeypatch,
):
    telegram = FakeTelegram()
    monkeypatch.setattr(
        "jin_market_pulse.jobs.TelegramClient",
        lambda _settings: telegram,
    )
    monkeypatch.setattr(
        "jin_market_pulse.jobs.process_pending_telegram_updates",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        "jin_market_pulse.jobs.send_due_pre_event_reminders",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "jin_market_pulse.jobs.capture_due_event_baselines",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "jin_market_pulse.jobs.send_due_event_results",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "jin_market_pulse.jobs.check_price_alerts",
        lambda *_args: 0,
    )

    first = run_tick(settings, idempotency_key="fixed")
    second = run_tick(settings, idempotency_key="fixed")

    assert first["status"] == "ok"
    assert second["status"] == "duplicate"
