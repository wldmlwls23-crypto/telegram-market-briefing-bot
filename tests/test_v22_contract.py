from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jin_market_pulse.app import SensitiveDataFilter
from jin_market_pulse.bot_queries import (
    _calendar_html,
    _parse_alert,
    handle_market_query,
    route_query,
)
from jin_market_pulse.calendar import fetch_economic_events
from jin_market_pulse.http_client import is_safe_public_https_url
from jin_market_pulse.jobs import check_price_alerts
from jin_market_pulse.models import AssetQuote
from jin_market_pulse.state import StateStore
from jin_market_pulse.telegram import (
    MAIN_KEYBOARD,
    SAFE_PART_LIMIT,
    split_html_message,
)


def test_router_does_not_mistake_jigeum_or_rate_for_gold():
    assert "gold" not in route_query("지금 금리가 왜 올라?").asset_keys
    assert route_query("금 가격 얼마야").asset_keys == ["gold"]


def test_start_returns_persistent_mobile_keyboard(settings, tmp_path):
    response = handle_market_query(
        "/start",
        settings,
        StateStore(tmp_path / "state.json"),
    )

    assert response.reply_markup == MAIN_KEYBOARD
    assert response.reply_markup["resize_keyboard"] is True
    assert "현재 시장" in str(response.reply_markup)


def test_ambiguous_treasury_question_returns_two_buttons(settings):
    response = handle_market_query("미국채", settings)

    assert "골라주세요" in response.text
    buttons = response.reply_markup["inline_keyboard"][0]
    assert [button["callback_data"] for button in buttons] == [
        "price:us2y",
        "price:us10y",
    ]


def test_alert_parser_supports_commas_direction_and_recurring():
    assert _parse_alert("비트 65,000 아래면 반복 알림") == (
        65000.0,
        "below",
        True,
    )
    assert _parse_alert("금 2,500 넘으면 알려줘") == (
        2500.0,
        "above",
        False,
    )


def test_price_alert_is_one_shot(
    settings,
    market_data,
    tmp_path,
    monkeypatch,
):
    store = StateStore(tmp_path / "state.json")
    alert_id = store.create_price_alert(
        settings.telegram_chat_id,
        "btc",
        "below",
        98000,
    )
    sent = []
    monkeypatch.setattr(
        "jin_market_pulse.jobs.fetch_asset_quote",
        lambda key, _settings, _store: market_data.quotes[key],
    )
    telegram = type(
        "Telegram",
        (),
        {"send": lambda self, text, parse_mode=None: sent.append(text)},
    )()

    assert check_price_alerts(settings, store, telegram) == 1
    assert store.list_price_alerts(settings.telegram_chat_id) == []
    assert store.list_price_alerts(
        settings.telegram_chat_id,
        active_only=False,
    )[0]["id"] == alert_id
    assert len(sent) == 1


def test_recurring_alert_rearms_only_after_one_percent_recovery(
    settings,
    market_data,
    tmp_path,
    monkeypatch,
):
    store = StateStore(tmp_path / "state.json")
    alert_id = store.create_price_alert(
        settings.telegram_chat_id,
        "btc",
        "below",
        98000,
        recurring=True,
    )
    current = {"value": 97000.0}

    def quote(*_args):
        return market_data.quotes["btc"].model_copy(
            update={"current": current["value"]}
        )

    monkeypatch.setattr("jin_market_pulse.jobs.fetch_asset_quote", quote)
    telegram = type(
        "Telegram",
        (),
        {"send": lambda self, text, parse_mode=None: None},
    )()

    check_price_alerts(settings, store, telegram)
    assert store.list_price_alerts(settings.telegram_chat_id)[0]["armed"] == 0
    current["value"] = 98500
    check_price_alerts(settings, store, telegram)
    assert store.list_price_alerts(settings.telegram_chat_id)[0]["armed"] == 0
    current["value"] = 99000
    check_price_alerts(settings, store, telegram)
    assert store.list_price_alerts(settings.telegram_chat_id)[0]["armed"] == 1
    assert store.list_price_alerts(settings.telegram_chat_id)[0]["id"] == alert_id


def test_context_expires_after_24_hours(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.set_chat_context("1", {"asset_key": "btc"})
    with store._connect(write=True) as db:
        db.execute(
            "UPDATE chat_context SET updated_at=? WHERE chat_id='1'",
            ((datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(),),
        )

    assert store.get_chat_context("1") == {}


def test_sqlite_legacy_migration_preserves_original_and_backup(tmp_path):
    legacy = tmp_path / "sent_alerts.json"
    legacy.write_text(
        json.dumps(
            {
                "alerts": {
                    "topic": {
                        "title": "title",
                        "urls": ["https://example.com"],
                        "sent_at": datetime.now(timezone.utc).isoformat(),
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    store = StateStore(tmp_path / "jin_market_pulse.sqlite3", legacy_json=legacy)

    assert legacy.exists()
    assert legacy.with_suffix(".json.pre-sqlite.bak").exists()
    assert store.alert_in_cooldown("topic")


def test_sqlite_concurrent_context_writes_do_not_corrupt(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")

    def write(index: int) -> None:
        store.set_chat_context(str(index % 4), {"index": index})

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(32)))

    assert store.readiness()["database"] == "ok"
    assert any(store.get_chat_context(str(index)) for index in range(4))


def test_provider_health_notifies_after_third_failure_and_marks_recovery(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    for _ in range(3):
        state = store.record_provider_result(
            "provider",
            success=False,
            error="timeout",
        )
    assert state["consecutive_failures"] == 3
    store.mark_provider_notified("provider")
    recovered = store.record_provider_result("provider", success=True)
    assert recovered["recovered"] is True
    assert store.provider_health()[0]["last_error"] == "RECOVERED"


def test_html_split_never_returns_oversized_or_open_tags():
    long_html = "<b>제목</b>\n\n" + ("긴 문장 " * 1200)
    parts = split_html_message(long_html)

    assert len(parts) > 1
    assert all(len(part) <= SAFE_PART_LIMIT for part in parts)
    assert all("<b>" not in part and "</b>" not in part for part in parts)


def test_calendar_mobile_output_has_meaning_and_no_forbidden_placeholders(market_data):
    event = market_data.events[0].model_copy(update={"forecast": ""})
    text = _calendar_html([event])

    assert "예상치 미공개" in text
    assert "의미:" in text
    assert "확인 필요" not in text
    assert "N/A" not in text
    assert "|" not in text


def test_public_url_guard_rejects_internal_and_non_https_addresses():
    assert not is_safe_public_https_url("http://example.com")
    assert not is_safe_public_https_url("https://127.0.0.1/admin")
    assert not is_safe_public_https_url("https://localhost/internal")
    assert not is_safe_public_https_url("https://169.254.169.254/latest/meta-data")


def test_log_filter_masks_keys_tokens_and_query_secrets():
    record = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        "s" + "k-" + "abcdefghijklmnopqrstuvwxyz "
        "bot123456:abcdefghijklmnopqrstuvwxyz apikey=secret",
        (),
        None,
    )
    SensitiveDataFilter().filter(record)

    assert "abcdefghijklmnopqrstuvwxyz" not in str(record.msg)
    assert "apikey=secret" not in str(record.msg)
    assert "[REDACTED]" in str(record.msg)


def test_ai_usage_categories_share_single_daily_budget(settings, tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")

    assert store.claim_usage_slot("image", 2, shared_limit=2)
    assert store.claim_usage_slot("voice", 3, shared_limit=2)
    assert not store.claim_usage_slot("advisor", 5, shared_limit=2)


def test_job_and_delivery_idempotency(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")

    assert store.claim_job("morning:one")
    store.mark_delivery("morning:one", "chart", telegram_message_id=10)
    store.finish_job("morning:one", success=True)
    assert not store.claim_job("morning:one")
    assert store.delivery_sent("morning:one", "chart")


def test_railway_and_python_runtime_are_pinned():
    root = Path(__file__).parents[1]
    railway = json.loads((root / "railway.json").read_text(encoding="utf-8"))

    assert railway["build"]["builder"] == "RAILPACK"
    assert railway["deploy"]["healthcheckPath"] == "/health"
    assert railway["deploy"]["sleepApplication"] is True
    assert railway["deploy"]["limitOverride"]["containers"] == {
        "cpu": 0.25,
        "memoryBytes": 268435456,
    }
    assert (root / ".python-version").read_text(encoding="utf-8").strip().startswith("3.12")


def test_railway_cron_has_bounded_schedule_and_runtime():
    root = Path(__file__).parents[1]
    config = json.loads(
        (root / "railway.cron.json").read_text(encoding="utf-8")
    )
    deploy = config["deploy"]

    assert config["build"]["builder"] == "RAILPACK"
    assert deploy["startCommand"] == "python -m jin_market_pulse.cron"
    assert deploy["cronSchedule"] == "20,50 * * * *"
    assert deploy["sleepApplication"] is False
    assert deploy["restartPolicyType"] == "NEVER"


def test_env_example_contains_only_placeholders():
    text = (Path(__file__).parents[1] / ".env.example").read_text(encoding="utf-8")

    assert "your_telegram_bot_token_here" in text
    assert "your_openai_api_key_here" in text
    assert "TELEGRAM_WEBHOOK_SECRET=" in text
    assert "STATE_DIR=/data" in text
    assert "sk-" not in text


def test_settings_toggle_is_distinct_from_temporary_mute(settings, tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")

    result = handle_market_query("긴급 알림 꺼줘", settings, store)

    assert "긴급 뉴스: 꺼짐" in result.text
    assert store.preferences(settings.telegram_chat_id)["emergency_alerts"] is False
    assert not store.is_muted(settings.telegram_chat_id)


def test_unknown_prediction_returns_data_alternative(settings):
    result = handle_market_query("비트 오를까?", settings)

    assert "가격 예측은 제공하지 않습니다" in result.text
    assert "오늘 일정" in result.text
