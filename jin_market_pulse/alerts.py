from __future__ import annotations

import html
import logging
from collections import defaultdict
from datetime import datetime, timedelta

from .calendar import event_meaning, fetch_economic_events
from .config import KST, Settings
from .models import AssetQuote, EconomicEvent, NewsItem
from .news import emergency_groups, fetch_news
from .providers import fetch_market_quotes
from .reports import create_emergency_analysis, render_emergency_alert
from .state import StateStore
from .telegram import TelegramClient


PRE_EVENT_MINUTES_MIN = 60
PRE_EVENT_MINUTES_MAX = 90
BASELINE_WINDOW_MINUTES = 30
RESULT_MIN_DELAY_MINUTES = 5
RESULT_RETRY_WINDOW_MINUTES = 120


def _as_events(value: EconomicEvent | list[EconomicEvent]) -> list[EconomicEvent]:
    return value if isinstance(value, list) else [value]


def render_pre_event_reminder(
    event_or_events: EconomicEvent | list[EconomicEvent],
) -> str:
    events = _as_events(event_or_events)
    first = events[0]
    lines = [
        "<b>[중요 경제지표 사전 알림]</b>",
        "",
        f"발표 시간: <b>{first.event_time_kst:%m/%d %H:%M} KST</b>",
        "중요도: ★★★★★",
    ]
    for index, event in enumerate(events):
        if index:
            lines.append("")
        lines.append(f"<b>{html.escape(event.country_ko)} · {html.escape(event.title_ko)}</b>")
        values = []
        if event.forecast:
            values.append(f"예상: {event.forecast}")
        else:
            values.append("예상치 미공개")
        if event.previous:
            values.append(f"이전: {event.previous}")
        lines.extend(
            [
                html.escape(" / ".join(values)),
                f"의미: {html.escape(event_meaning(event))}",
                (
                    "해석: 상회 시 "
                    f"{html.escape(event.sensitivity_stronger)}"
                    " / 하회 시 "
                    f"{html.escape(event.sensitivity_weaker)}"
                ),
            ]
        )
    lines.extend(["", "관찰 순서: DXY → 미국채 금리 → Nasdaq → BTC"])
    return "\n".join(lines)


def _group_events_by_time(
    events: list[EconomicEvent],
) -> list[list[EconomicEvent]]:
    grouped: dict[str, list[EconomicEvent]] = defaultdict(list)
    for event in events:
        key = event.event_time_kst.replace(second=0, microsecond=0).isoformat()
        grouped[key].append(event)
    return [grouped[key] for key in sorted(grouped)]


def send_due_pre_event_reminders(
    settings: Settings,
    state: StateStore,
    telegram: TelegramClient,
) -> None:
    now = datetime.now(KST)
    due = []
    for event in fetch_economic_events(
        settings,
        days_ahead=1,
        store=state,
    ):
        minutes = (event.event_time_kst - now).total_seconds() / 60
        if (
            event.importance == "★★★★★"
            and PRE_EVENT_MINUTES_MIN <= minutes <= PRE_EVENT_MINUTES_MAX
            and not state.event_record(event.event_id).get("pre_alert_sent_at")
        ):
            due.append(event)
    for group in _group_events_by_time(due):
        message_ids = telegram.send(
            render_pre_event_reminder(group),
            parse_mode="HTML",
        )
        for event in group:
            state.update_event(
                event.event_id,
                title=event.title,
                event_time=event.event_time_kst.isoformat(),
                stage="pre_alert_sent",
                pre_alert_sent_at=now.isoformat(),
                pre_alert_message_id=message_ids[0] if message_ids else None,
            )
        logging.info("Pre-event reminder sent for %s event(s).", len(group))


def capture_due_event_baselines(settings: Settings, state: StateStore) -> None:
    now = datetime.now(KST)
    window_end = now + timedelta(minutes=BASELINE_WINDOW_MINUTES)
    due = [
        event
        for event in fetch_economic_events(
            settings,
            days_ahead=1,
            store=state,
        )
        if event.importance == "★★★★★"
        and now <= event.event_time_kst <= window_end
        and not state.event_record(event.event_id).get("before_snapshot")
    ]
    if not due:
        return
    quotes, _ = fetch_market_quotes(settings, state)
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
        if not before or not quote or quote.stale or not quote.verified:
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
                f"• {quote.name_ko}: 발표 전 {start:.2f}% → 현재 {quote.current:.2f}% "
                f"({change:+.1f}bp)"
            )
        else:
            percent = (quote.current - start) / start * 100
            lines.append(
                f"• {quote.name_ko}: 발표 전 {start:,.2f} → 현재 {quote.current:,.2f} "
                f"({percent:+.2f}%)"
            )
    return lines


def render_event_result(
    event_or_events: EconomicEvent | list[EconomicEvent],
    before_snapshot: dict[str, dict[str, object]],
    quotes: dict[str, AssetQuote],
) -> str:
    events = _as_events(event_or_events)
    first = events[0]
    lines = [
        "<b>[중요 경제지표 결과]</b>",
        "",
        f"발표 시간: <b>{first.event_time_kst:%m/%d %H:%M} KST</b>",
        "중요도: ★★★★★",
    ]
    for index, event in enumerate(events):
        if index:
            lines.append("")
        lines.append(f"<b>{html.escape(event.country_ko)} · {html.escape(event.title_ko)}</b>")
        if event.value_summary:
            lines.append(html.escape(event.value_summary))
        lines.append(f"의미: {html.escape(event_meaning(event))}")
        if event.actual and event.forecast:
            lines.append(
                "해석: 상회 시 "
                f"{html.escape(event.sensitivity_stronger)}"
                " / 하회 시 "
                f"{html.escape(event.sensitivity_weaker)}"
            )
    reaction = _reaction_lines(before_snapshot, quotes)
    if reaction:
        lines.extend(["", "<b>발표 전후 시장 반응:</b>", *reaction])
    return "\n".join(lines)


def send_due_event_results(
    settings: Settings,
    state: StateStore,
    telegram: TelegramClient,
) -> None:
    now = datetime.now(KST)
    events = fetch_economic_events(
        settings,
        lookback_hours=3,
        days_ahead=0,
        store=state,
    )
    due = [
        event
        for event in events
        if event.importance == "★★★★★"
        and event.actual
        and timedelta(minutes=RESULT_MIN_DELAY_MINUTES)
        <= now - event.event_time_kst
        <= timedelta(minutes=RESULT_RETRY_WINDOW_MINUTES)
    ]
    for group in _group_events_by_time(due):
        unsent = [
            event
            for event in group
            if not state.event_record(event.event_id).get("result_sent_at")
        ]
        if not unsent:
            # Correct prior messages only when the published number changed.
            for event in group:
                record = state.event_record(event.event_id)
                if record.get("actual") == event.actual:
                    continue
                message_id = record.get("result_message_id")
                if message_id:
                    quotes, _ = fetch_market_quotes(settings, state)
                    before = record.get("before_snapshot", {})
                    telegram.edit(
                        int(message_id),
                        render_event_result(event, before, quotes),
                    )
                    state.update_event(event.event_id, actual=event.actual)
            continue
        quotes, _ = fetch_market_quotes(settings, state)
        before = state.event_record(unsent[0].event_id).get("before_snapshot", {})
        message_ids = telegram.send(
            render_event_result(unsent, before, quotes),
            parse_mode="HTML",
        )
        for event in unsent:
            state.update_event(
                event.event_id,
                stage="result_sent",
                result_sent_at=now.isoformat(),
                actual=event.actual,
                result_message_id=message_ids[0] if message_ids else None,
            )
        logging.info("Event result update sent for %s event(s).", len(unsent))


def _meaningful_market_move(
    group: list[NewsItem],
    quotes: dict[str, AssetQuote],
) -> bool:
    relevant = {
        key
        for item in group
        for key in item.relevant_asset_keys
    }
    thresholds = {
        "btc": 3.0,
        "eth": 3.0,
        "sp500": 1.5,
        "nasdaq100": 1.5,
        "kospi": 1.5,
        "kosdaq": 1.5,
        "dxy": 0.4,
        "wti": 2.5,
        "gold": 2.5,
    }
    for key in relevant:
        quote = quotes.get(key)
        if (
            quote
            and quote.verified
            and not quote.stale
            and quote.percent_change is not None
            and abs(quote.percent_change) >= thresholds.get(key, 2.0)
        ):
            return True
    if {"us2y", "us10y"} & relevant:
        return any(
            quote
            and quote.verified
            and quote.absolute_change is not None
            and abs(quote.absolute_change * 100) >= 5
            for key in ("us2y", "us10y")
            if (quote := quotes.get(key))
        )
    return False


def monitor_emergency_alerts(
    settings: Settings,
    state: StateStore,
    telegram: TelegramClient,
) -> None:
    if state.recent_alert_count(minutes=30) >= 3:
        logging.info("Emergency alert burst limit reached.")
        return
    if state.daily_alert_count() >= 2:
        logging.info("Emergency alert daily limit reached.")
        return
    groups = emergency_groups(fetch_news())
    for group in groups:
        topic = group[0].topic_key
        if state.alert_in_cooldown(topic, hours=6):
            continue
        quotes, _ = fetch_market_quotes(settings, state)
        if not _meaningful_market_move(group, quotes):
            logging.info("Emergency candidate had no verified market move.")
            continue
        analysis = create_emergency_analysis(group, quotes, settings)
        if not analysis.verified:
            logging.info("Emergency candidate was not verified: %s", group[0].title)
            continue
        message_ids = telegram.send(
            render_emergency_alert(analysis, group, quotes),
            parse_mode="HTML",
        )
        state.mark_alert(
            topic,
            group[0].title,
            [item.url for item in group if item.news_id in analysis.source_news_ids],
            telegram_message_id=message_ids[0] if message_ids else None,
        )
        logging.info("Emergency alert sent: %s", group[0].title)
        break
