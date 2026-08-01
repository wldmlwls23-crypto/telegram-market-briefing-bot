from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta
from typing import Any

from .advisor import explain_image, transcribe_voice
from .alerts import (
    capture_due_event_baselines,
    monitor_emergency_alerts,
    send_due_event_results,
    send_due_pre_event_reminders,
)
from .app import MarketPulseApp
from .breaking import monitor_breaking_alerts
from .bot_queries import ASSET_DEFINITIONS, handle_market_query
from .config import KST, Settings
from .links import explain_news_link, first_https_url
from .providers import fetch_asset_quote, fetch_market_quotes
from .session_reports import report_due, send_session_report
from .state import StateStore
from .telegram import TelegramClient
from .us_open import send_us_open_preview, us_open_preview_due


def _allowed_chat(payload: dict[str, Any], settings: Settings) -> bool:
    callback = payload.get("callback_query") or {}
    message = payload.get("message") or callback.get("message") or {}
    chat = message.get("chat") or {}
    return str(chat.get("id")) == str(settings.telegram_chat_id)


def _callback_text(data: str) -> str:
    if data.startswith("price:"):
        return f"/price {data.split(':', 1)[1]}"
    if data.startswith("last:"):
        return f"/last {data.split(':', 1)[1]}"
    mapping = {
        "cmd:markets": "/markets",
        "cmd:calendar": "/calendar",
        "cmd:week": "/week",
        "cmd:last": "/last",
        "cmd:status": "/status",
        "cmd:cause": "왜 움직여?",
    }
    return mapping.get(data, "/help")


def _send_bot_response(
    response: Any,
    telegram: TelegramClient,
    *,
    reply_to_message_id: int | None = None,
) -> None:
    telegram.send(
        response.text,
        parse_mode=response.parse_mode,
        reply_markup=response.reply_markup,
        reply_to_message_id=reply_to_message_id,
    )


def _process_photo(
    message: dict[str, Any],
    settings: Settings,
    state: StateStore,
    telegram: TelegramClient,
) -> None:
    if not state.claim_usage_slot(
        "image",
        settings.image_daily_limit,
        shared_limit=settings.ai_advisor_daily_limit,
    ):
        telegram.send(
            "<b>오늘의 이미지 설명 횟수를 모두 사용했습니다.</b>",
            parse_mode="HTML",
        )
        return
    photos = message.get("photo") or []
    if not photos:
        state.release_usage_slot("image")
        return
    largest = max(photos, key=lambda item: int(item.get("file_size") or 0))
    try:
        telegram.send_action("typing")
        content = telegram.get_file(str(largest["file_id"]))
        answer = explain_image(
            content,
            str(message.get("caption") or "이 이미지를 쉽게 설명해줘"),
            settings,
        )
        telegram.send(answer, parse_mode="HTML")
    except Exception:
        state.release_usage_slot("image")
        raise


def _process_voice(
    message: dict[str, Any],
    settings: Settings,
    state: StateStore,
    telegram: TelegramClient,
) -> None:
    voice = message.get("voice") or {}
    if int(voice.get("duration") or 0) > 60:
        telegram.send("음성 질문은 60초 이하만 처리할 수 있습니다.")
        return
    if not state.claim_usage_slot(
        "voice",
        settings.voice_daily_limit,
        shared_limit=settings.ai_advisor_daily_limit,
    ):
        telegram.send(
            "<b>오늘의 음성 질문 횟수를 모두 사용했습니다.</b>",
            parse_mode="HTML",
        )
        return
    try:
        telegram.send_action("typing")
        content = telegram.get_file(str(voice["file_id"]))
        transcript = transcribe_voice(content, settings)
        response = handle_market_query(transcript, settings, state)
        telegram.send(
            f"<i>들은 질문: {html.escape(transcript)}</i>\n\n{response.text}",
            parse_mode="HTML",
            reply_markup=response.reply_markup,
        )
    except Exception:
        state.release_usage_slot("voice")
        raise


def _process_link(
    text: str,
    settings: Settings,
    state: StateStore,
    telegram: TelegramClient,
) -> bool:
    url = first_https_url(text)
    if not url:
        return False
    if not state.claim_usage_slot(
        "link",
        settings.ai_current_cause_daily_limit,
        shared_limit=settings.ai_advisor_daily_limit,
    ):
        telegram.send(
            "<b>오늘의 AI 뉴스 검증 횟수를 모두 사용했습니다.</b>",
            parse_mode="HTML",
        )
        return True
    try:
        telegram.send_action("typing")
        telegram.send(
            explain_news_link(url, text, settings),
            parse_mode="HTML",
        )
    except Exception:
        state.release_usage_slot("link")
        raise
    return True


def process_telegram_update(
    payload: dict[str, Any],
    settings: Settings,
    state: StateStore,
    telegram: TelegramClient,
) -> None:
    if not _allowed_chat(payload, settings):
        return
    callback = payload.get("callback_query") or {}
    if callback:
        callback_id = str(callback.get("id") or "")
        if callback_id:
            telegram.answer_callback(callback_id)
        text = _callback_text(str(callback.get("data") or ""))
        response = handle_market_query(text, settings, state)
        _send_bot_response(response, telegram)
        return

    message = payload.get("message") or {}
    if message.get("photo"):
        _process_photo(message, settings, state, telegram)
        return
    if message.get("voice"):
        _process_voice(message, settings, state, telegram)
        return

    text = str(message.get("text") or message.get("caption") or "").strip()
    if not text:
        return
    if _process_link(text, settings, state, telegram):
        return
    if message.get("forward_origin") and not first_https_url(text):
        telegram.send(
            "<b>전달된 문장만으로는 사실 여부를 단정하지 않습니다.</b>\n"
            "기사의 공개 HTTPS 링크를 함께 보내면 원문과 다른 출처를 확인해 요약합니다.",
            parse_mode="HTML",
        )
        return
    response = handle_market_query(text, settings, state)
    _send_bot_response(
        response,
        telegram,
        reply_to_message_id=(
            int(message["message_id"])
            if isinstance(message.get("message_id"), int)
            else None
        ),
    )


def process_pending_telegram_updates(
    settings: Settings,
    state: StateStore,
    telegram: TelegramClient,
    *,
    limit: int = 10,
) -> int:
    processed = 0
    for item in state.pending_telegram_updates(limit=limit):
        update_id = int(item["update_id"])
        state.mark_telegram_update(update_id, "processing")
        try:
            process_telegram_update(
                item["payload"],
                settings,
                state,
                telegram,
            )
            state.mark_telegram_update(update_id, "done")
            processed += 1
        except Exception as exc:
            state.mark_telegram_update(
                update_id,
                "retry",
                error=type(exc).__name__,
            )
            logging.exception("Telegram queued update failed: %s", update_id)
    return processed


def check_price_alerts(
    settings: Settings,
    state: StateStore,
    telegram: TelegramClient,
) -> int:
    alerts = state.list_price_alerts(settings.telegram_chat_id)
    if not alerts:
        return 0
    sent = 0
    quote_cache: dict[str, Any] = {}
    for item in alerts:
        key = str(item["asset_key"])
        try:
            quote = quote_cache.get(key)
            if quote is None:
                quote = fetch_asset_quote(key, settings, state)
                quote_cache[key] = quote
        except Exception:
            logging.warning("Price alert quote failed for %s.", key)
            continue
        threshold = float(item["threshold"])
        direction = str(item["direction"])
        recurring = bool(item["recurring"])
        armed = bool(item["armed"])
        triggered = (
            quote.current <= threshold
            if direction == "below"
            else quote.current >= threshold
        )
        if armed and triggered:
            direction_ko = "아래" if direction == "below" else "위"
            name = ASSET_DEFINITIONS.get(key, {}).get("name", quote.name_ko)
            telegram.send(
                "\n".join(
                    [
                        "<b>[개인 가격 알림]</b>",
                        f"{html.escape(str(name))}이 설정값 {threshold:,.2f} {direction_ko}에 도달했습니다.",
                        f"현재값: <b>{quote.current:,.2f}</b>",
                        f"기준 {quote.as_of.astimezone(KST):%m/%d %H:%M} KST · {html.escape(quote.source)}",
                    ]
                ),
                parse_mode="HTML",
            )
            state.update_price_alert_state(
                int(item["id"]),
                active=True if recurring else False,
                armed=False if recurring else None,
                triggered=True,
            )
            sent += 1
            continue
        if recurring and not armed:
            reset = (
                quote.current >= threshold * 1.01
                if direction == "below"
                else quote.current <= threshold * 0.99
            )
            if reset:
                state.update_price_alert_state(int(item["id"]), armed=True)
    return sent


def report_provider_health(
    state: StateStore,
    telegram: TelegramClient,
) -> None:
    for item in state.provider_health():
        provider = str(item["provider"])
        failures = int(item["consecutive_failures"])
        notified = bool(item["notified"])
        if provider == "economic_calendar_fallback":
            if failures == 0 and notified:
                state.clear_provider_notified(provider)
            continue
        provider_label = {
            "economic_calendar": "경제 캘린더",
            "bls_calendar": "미국 노동통계국 일정",
            "bea_calendar": "미국 경제분석국 일정",
        }.get(provider, provider)
        if failures >= 3 and not notified:
            telegram.send(
                "\n".join(
                    [
                        "<b>[데이터 공급원 장애]</b>",
                        f"{html.escape(provider_label)}가 연속 {failures}회 응답하지 않았습니다.",
                        "해당 값은 마지막 정상값을 표시하거나 메시지에서 생략합니다.",
                    ]
                ),
                parse_mode="HTML",
            )
            state.mark_provider_notified(provider)
        elif failures == 0 and notified and item["last_error"] == "RECOVERED":
            telegram.send(
                f"<b>[데이터 공급원 정상화]</b>\n{html.escape(provider_label)} 응답이 복구됐습니다.",
                parse_mode="HTML",
            )
            state.clear_provider_notified(provider)


def run_tick(
    settings: Settings,
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    state = StateStore(settings.state_db, legacy_json=settings.state_file)
    telegram = TelegramClient(settings)
    now = datetime.now(KST)
    quarter = (now.minute // 15) * 15
    slot = idempotency_key or f"{now:%Y%m%d-%H}-{quarter:02d}"
    job_key = f"tick:{slot}"
    if not state.claim_job(job_key, lease_seconds=90):
        return {"status": "duplicate", "slot": slot}
    result: dict[str, Any] = {"status": "ok", "slot": slot}
    try:
        result["telegram_updates"] = process_pending_telegram_updates(
            settings,
            state,
            telegram,
        )
        prefs = state.preferences(settings.telegram_chat_id)
        muted = state.is_muted(settings.telegram_chat_id)
        check_prices = 18 <= now.minute <= 27 or 48 <= now.minute <= 57

        if (
            "morning" in settings.enabled_reports
            and now.hour == 6
            and 45 <= now.minute <= 59
        ):
            result["morning"] = MarketPulseApp(settings).send_morning_report()

        if (
            "us_open" in settings.enabled_reports
            and prefs.get("us_open_reports", True)
            and not muted
            and us_open_preview_due(now)
        ):
            preview = send_us_open_preview(settings, state, telegram, now=now)
            result["us_open"] = preview.status

        for report_type, preference in (
            ("korea_close", "korea_close_reports"),
            ("europe_close", "europe_close_reports"),
        ):
            if (
                report_type in settings.enabled_reports
                and prefs.get(preference, True)
                and not muted
                and report_due(report_type, now)
            ):
                report = send_session_report(
                    report_type,
                    settings,
                    state,
                    telegram,
                    deliver=True,
                )
                result[report_type] = report.status

        if settings.enable_event_alerts and prefs["event_alerts"] and not muted:
            send_due_pre_event_reminders(settings, state, telegram)
            capture_due_event_baselines(settings, state)
            result["event_results"] = send_due_event_results(
                settings,
                state,
                telegram,
            )

        if (
            settings.enable_emergency_alerts
            and prefs["emergency_alerts"]
            and not muted
        ):
            result["breaking"] = monitor_breaking_alerts(
                settings,
                state,
                telegram,
                check_prices=check_prices,
                disable_notification=bool(
                    prefs.get("overnight_silent", True)
                    and 0 <= now.hour < 6
                ),
            )

        result["price_alerts"] = (
            check_price_alerts(settings, state, telegram)
            if check_prices
            else 0
        )
        report_provider_health(state, telegram)
        state.finish_job(job_key, success=True)
        return result
    except Exception as exc:
        state.finish_job(
            job_key,
            success=False,
            error=type(exc).__name__,
        )
        raise
