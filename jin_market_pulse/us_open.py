from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from .calendar import fetch_economic_events
from .config import KST, NEW_YORK, Settings
from .models import AssetQuote, EconomicEvent
from .providers import fetch_asset_quote
from .state import StateStore
from .telegram import TelegramClient


US_OPEN_TARGET = time(9, 0)
US_OPEN_LIMIT = 750
US_OPEN_KEYS = ("nasdaq_futures", "dxy", "us10y", "btc")

LABELS = {
    "nasdaq_futures": "Nasdaq 선물",
    "dxy": "DXY",
    "us10y": "미 10년물",
    "btc": "BTC",
}


@dataclass
class USOpenPreviewResult:
    report_key: str
    session_date: str
    status: str
    text: str = ""
    skip_reason: str = ""
    quotes: dict[str, AssetQuote] = field(default_factory=dict)
    telegram_message_id: int | None = None


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (nth - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    next_month = date(year + (month == 12), month % 12 + 1, 1)
    last = next_month - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    leap_adjust = (32 + 2 * e + 2 * i - h - k) % 7
    correction = (a + 11 * h + 22 * leap_adjust) // 451
    month = (h + leap_adjust - 7 * correction + 114) // 31
    day = (h + leap_adjust - 7 * correction + 114) % 31 + 1
    return date(year, month, day)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def us_equity_holidays(year: int) -> set[date]:
    holidays = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed(date(year, 6, 19)),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
    }
    next_new_year = _observed(date(year + 1, 1, 1))
    if next_new_year.year == year:
        holidays.add(next_new_year)
    return holidays


def is_us_equity_session(day: date) -> bool:
    return day.weekday() < 5 and day not in us_equity_holidays(day.year)


def us_open_preview_due(now: datetime) -> bool:
    local = now.astimezone(NEW_YORK)
    target = datetime.combine(local.date(), US_OPEN_TARGET, tzinfo=NEW_YORK)
    return (
        is_us_equity_session(local.date())
        and target <= local <= target + timedelta(minutes=14)
    )


def next_us_open_preview_time(now: datetime | None = None) -> datetime:
    current = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    candidate = datetime.combine(current.date(), US_OPEN_TARGET, tzinfo=NEW_YORK)
    if candidate <= current:
        candidate += timedelta(days=1)
    while not is_us_equity_session(candidate.date()):
        candidate += timedelta(days=1)
    return candidate


def _quote_line(quote: AssetQuote) -> str:
    label = LABELS[quote.key]
    if quote.key == "us10y":
        value = f"{quote.current:.3f}%"
        change = (
            f"{'▲' if quote.absolute_change > 0 else '▼'}"
            f"{abs(quote.absolute_change) * 100:.1f}bp"
            if quote.absolute_change is not None and quote.absolute_change != 0
            else "▬보합"
        )
    else:
        value = (
            f"${quote.current:,.0f}" if quote.key == "btc"
            else f"{quote.current:,.2f}"
        )
        change = (
            f"{'▲' if quote.percent_change > 0 else '▼'}"
            f"{abs(quote.percent_change):.2f}%"
            if quote.percent_change is not None and quote.percent_change != 0
            else "▬보합"
        )
    return f"• {label} <b>{value}</b> {change}"


def _signal_reasons(
    quotes: dict[str, AssetQuote],
    events: list[EconomicEvent],
) -> list[str]:
    reasons: list[str] = []
    limits = {"nasdaq_futures": 0.7, "dxy": 0.3, "btc": 1.5}
    for key, limit in limits.items():
        quote = quotes.get(key)
        if quote and quote.percent_change is not None and abs(quote.percent_change) >= limit:
            reasons.append(f"{LABELS[key]} 변동 {abs(quote.percent_change):.2f}%")
    yield_quote = quotes.get("us10y")
    if (
        yield_quote
        and yield_quote.absolute_change is not None
        and abs(yield_quote.absolute_change) * 100 >= 5
    ):
        reasons.append(f"미 10년물 변동 {abs(yield_quote.absolute_change) * 100:.1f}bp")
    if events:
        reasons.append(f"개장 전후 5성 일정 {len(events)}건")
    return reasons


def render_us_open_preview(
    quotes: dict[str, AssetQuote],
    events: list[EconomicEvent],
    reasons: list[str],
    *,
    now: datetime,
) -> str:
    local = now.astimezone(NEW_YORK)
    open_at = datetime.combine(local.date(), time(9, 30), tzinfo=NEW_YORK)
    minutes = max(round((open_at - local).total_seconds() / 60), 0)
    ranked = sorted(
        (
            quote
            for quote in quotes.values()
            if quote.percent_change is not None
        ),
        key=lambda quote: abs(float(quote.percent_change or 0)),
        reverse=True,
    )
    strongest = ranked[0] if ranked else None
    lines = [
        "<b>[미국장 개장 전]</b>",
        f"{now.astimezone(KST):%m/%d %H:%M} KST · 개장 약 {minutes}분 전",
        "",
        "<b>한눈에</b>",
        *[_quote_line(quotes[key]) for key in US_OPEN_KEYS if key in quotes],
        "",
        "<b>지금 알리는 이유</b>",
        f"• {html.escape(' · '.join(reasons[:2]))}",
        "",
        "<b>장 시작 전 달라진 점</b>",
    ]
    if strongest:
        direction = "상승" if float(strongest.percent_change or 0) > 0 else "하락"
        lines.append(
            f"• {LABELS[strongest.key]}가 비교 기준보다 "
            f"{abs(float(strongest.percent_change or 0)):.2f}% {direction}했습니다."
        )
    else:
        lines.append("• 금리와 예정 지표를 중심으로 확인합니다.")
    lines.extend(["", "<b>개장 변수</b>"])
    if events:
        event = min(events, key=lambda item: item.event_time_kst)
        values = event.value_summary or "예상치 미공개"
        lines.append(
            f"• {event.event_time_kst:%H:%M} {html.escape(event.title_ko)} · "
            f"{html.escape(values)}"
        )
    else:
        lines.append("• 개장 직후 예정된 5성 경제지표는 없습니다.")
    lines.extend(
        [
            "",
            "<b>관찰 순서</b>",
            "• 미 10년물 → Nasdaq 선물 → 정규장 → BTC",
        ]
    )
    text = "\n".join(lines)
    if len(text) > US_OPEN_LIMIT:
        raise ValueError("US open preview exceeds mobile limit")
    return text


def build_us_open_preview(
    settings: Settings,
    state: StateStore,
    *,
    now: datetime | None = None,
) -> USOpenPreviewResult:
    current = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    session_date = current.date().isoformat()
    report_key = f"us_open:{session_date}"
    if not is_us_equity_session(current.date()):
        return USOpenPreviewResult(report_key, session_date, "skipped", skip_reason="미국장 휴장")

    quotes: dict[str, AssetQuote] = {}
    for key in US_OPEN_KEYS:
        try:
            quote = fetch_asset_quote(key, settings, state)
            if (
                quote.verified
                and not quote.stale
                and quote.validation_status == "verified"
                and quote.calculation_version >= 2
            ):
                quotes[key] = quote
        except Exception:
            continue
    if "nasdaq_futures" not in quotes:
        return USOpenPreviewResult(
            report_key,
            session_date,
            "skipped",
            skip_reason="Nasdaq 선물 데이터 부족",
            quotes=quotes,
        )

    open_at = datetime.combine(current.date(), time(9, 30), tzinfo=NEW_YORK)
    try:
        events = [
            event
            for event in fetch_economic_events(settings, days_ahead=1, store=state)
            if event.importance == "★★★★★"
            and current <= event.event_time_kst.astimezone(NEW_YORK) <= open_at + timedelta(hours=2)
        ]
    except Exception:
        events = []
    reasons = _signal_reasons(quotes, events)
    if not reasons:
        return USOpenPreviewResult(
            report_key,
            session_date,
            "skipped",
            skip_reason="개장 전 특이사항 없음",
            quotes=quotes,
        )
    return USOpenPreviewResult(
        report_key,
        session_date,
        "ready",
        text=render_us_open_preview(quotes, events, reasons, now=current),
        quotes=quotes,
    )


def send_us_open_preview(
    settings: Settings,
    state: StateStore,
    telegram: TelegramClient,
    *,
    now: datetime | None = None,
) -> USOpenPreviewResult:
    result = build_us_open_preview(settings, state, now=now)
    if result.status == "skipped":
        state.record_report_run(
            result.report_key,
            "us_open",
            result.session_date,
            "skipped",
            skip_reason=result.skip_reason,
        )
        return result
    if not state.claim_job(result.report_key, lease_seconds=15 * 60):
        result.status = "duplicate"
        return result
    try:
        message_ids = telegram.send(result.text, parse_mode="HTML")
        result.telegram_message_id = message_ids[0] if message_ids else None
        result.status = "sent"
        state.record_report_run(
            result.report_key,
            "us_open",
            result.session_date,
            "sent",
            text=result.text,
            telegram_message_id=result.telegram_message_id,
        )
        state.save_message(result.report_key, result.text, parse_mode="HTML")
        state.finish_job(result.report_key, success=True)
        return result
    except Exception as exc:
        state.finish_job(result.report_key, success=False, error=type(exc).__name__)
        raise
