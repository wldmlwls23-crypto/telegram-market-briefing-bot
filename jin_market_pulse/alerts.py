from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta

from .calendar import event_meaning, fetch_economic_events
from .config import KST, Settings
from .models import AssetQuote, EconomicEvent
from .news import emergency_groups, fetch_news
from .providers import fetch_market_quotes
from .reports import (
    create_emergency_analysis,
    render_emergency_alert,
)
from .state import StateStore
from .telegram import TelegramClient


PRE_EVENT_HOURS = 6
BASELINE_WINDOW_MINUTES = 30
RESULT_MIN_DELAY_MINUTES = 10


def render_pre_event_reminder(event: EconomicEvent) -> str:
    lines = [
        "<b>[중요 경제지표 사전 알림]</b>",
        "",
        f"<b>{html.escape(event.country_ko)} · {html.escape(event.title_ko)}</b>",
        f"발표 시간: <b>{event.event_time_kst:%m/%d %H:%M} KST</b>",
        f"중요도: {event.importance}",
    ]
    values = []
    if event.forecast:
        values.append(f"예상: {html.escape(event.forecast)}")
    if event.previous:
        values.append(f"이전: {html.escape(event.previous)}")
    if values:
        lines.append(" / ".join(values))
    lines.extend(
        [
            f"의미: {html.escape(event_meaning(event))}",
            (
                "해석: 상회 시 "
                f"{html.escape(event.sensitivity_stronger)}"
                " / 하회 시 "
                f"{html.escape(event.sensitivity_weaker)}"
            ),
            "관찰: DXY → 미국채 금리 → Nasdaq → BTC",
        ]
    )
    return "\n".join(lines)


def send_due_pre_event_reminders(
    settings: Settings,
    state: StateStore,
    telegram: TelegramClient,
) -> None:
    now = datetime.now(KST)
    window_end = now + timedelta(hours=PRE_EVENT_HOURS)
    for event in fetch_economic_events(settings, days_ahead=1):
        if event.importance != "★★★★★":
            continue
        if not (now <= event.event_time_kst <= window_end):
            continue
        record = state.event_record(event.event_id)
        if record.get("pre_alert_sent_at"):
            continue
        telegram.send(render_pre_event_reminder(event), parse_mode="HTML")
        state.update_event(
            event.event_id,
            title=event.title,
            event_time=event.event_time_kst.isoformat(),
            stage="pre_alert_sent",
            pre_alert_sent_at=now.isoformat(),
        )
        logging.info("Pre-event reminder sent: %s", event.title)


def capture_due_event_baselines(settings: Settings, state: StateStore) -> None:
    now = datetime.now(KST)
    window_end = now + timedelta(minutes=BASELINE_WINDOW_MINUTES)
    due = [
        event
        for event in fetch_economic_events(settings, days_ahead=1)
        if event.importance == "★★★★★"
        and now <= event.event_time_kst <= window_end
        and not state.event_record(event.event_id).get("before_snapshot")
    ]
    if not due:
        return
    quotes, _ = fetch_market_quotes(settings)
    for event in due:
        state.save_event_snapshot(event.event_id, "before", quotes)
        state.update_event(event.event_id, stage="baseline_captured")
        logging.info("Pre-event market baseline captured: %s", event.title)


def _reaction_lines(
    before_snapshot: dict[str, dict[str, object]],
    quotes: dict[str, AssetQuote],
) -> list[str]:
    lines: list[str] = []
    for key in ("dxy", "us2y", "us10y", "nasdaq100", "btc"):
        before = before_snapshot.get(key)
        quote = quotes.get(key)
        if not before or not quote:
            continue
        try:
            start = float(before["current"])
        except (KeyError, TypeError, ValueError):
            continue
        if start == 0:
            continue
        if quote.kind == "yield":
            change = (quote.current - start) * 100
            lines.append(
                f"- {quote.name_ko}: 발표 전 {start:.2f}% → 현재 {quote.current:.2f}% "
                f"({change:+.1f}bp)"
            )
        else:
            percent = (quote.current - start) / start * 100
            lines.append(
                f"- {quote.name_ko}: 발표 전 {start:,.2f} → 현재 {quote.current:,.2f} "
                f"({percent:+.2f}%)"
            )
    return lines


def render_event_result(
    event: EconomicEvent,
    before_snapshot: dict[str, dict[str, object]],
    quotes: dict[str, AssetQuote],
) -> str:
    lines = [
        "<b>[중요 경제지표 결과]</b>",
        "",
        f"<b>{html.escape(event.country_ko)} · {html.escape(event.title_ko)}</b>",
        f"발표 시간: <b>{event.event_time_kst:%m/%d %H:%M} KST</b>",
        f"중요도: {event.importance}",
    ]
    if event.value_summary:
        lines.append(html.escape(event.value_summary))
    lines.append(f"의미: {html.escape(event_meaning(event))}")
    reaction = _reaction_lines(before_snapshot, quotes)
    if reaction:
        lines.extend(["", "발표 전후 시장 반응:", *reaction])
    stronger_applies = bool(event.actual and event.forecast)
    lines.extend(
        [
            "",
            (
                "해석: 상회 시 "
                f"{html.escape(event.sensitivity_stronger)}"
                " / 하회 시 "
                f"{html.escape(event.sensitivity_weaker)}"
            ),
        ]
    )
    if not stronger_applies:
        lines.append("- 예상치 비교가 불가능해 방향을 단정하지 않습니다.")
    return "\n".join(lines)


def send_due_event_results(
    settings: Settings,
    state: StateStore,
    telegram: TelegramClient,
) -> None:
    now = datetime.now(KST)
    events = fetch_economic_events(settings, lookback_hours=12, days_ahead=0)
    for event in events:
        if event.importance != "★★★★★" or not event.actual:
            continue
        if now - event.event_time_kst < timedelta(minutes=RESULT_MIN_DELAY_MINUTES):
            continue
        record = state.event_record(event.event_id)
        if record.get("result_sent_at"):
            continue
        quotes, _ = fetch_market_quotes(settings)
        before_snapshot = record.get("before_snapshot", {})
        telegram.send(
            render_event_result(event, before_snapshot, quotes),
            parse_mode="HTML",
        )
        state.update_event(
            event.event_id,
            stage="result_sent",
            result_sent_at=now.isoformat(),
            actual=event.actual,
        )
        logging.info("Event result update sent: %s", event.title)


def monitor_emergency_alerts(
    settings: Settings,
    state: StateStore,
    telegram: TelegramClient,
) -> None:
    if state.recent_alert_count(minutes=30) >= 3:
        logging.info("Emergency alert burst limit reached.")
        return
    groups = emergency_groups(fetch_news())
    for group in groups:
        topic = group[0].topic_key
        if state.alert_in_cooldown(topic, hours=6):
            continue
        quotes, _ = fetch_market_quotes(settings)
        analysis = create_emergency_analysis(group, quotes, settings)
        if not analysis.verified:
            logging.info("Emergency candidate was not verified: %s", group[0].title)
            continue
        telegram.send(
            render_emergency_alert(analysis, group, quotes),
            parse_mode="HTML",
        )
        state.mark_alert(
            topic,
            group[0].title,
            [item.url for item in group if item.news_id in analysis.source_news_ids],
        )
        logging.info("Emergency alert sent: %s", group[0].title)
        break
