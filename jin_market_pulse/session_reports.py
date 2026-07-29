from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .calendar import fetch_economic_events
from .config import KST, PARIS, Settings
from .models import AssetQuote, NewsItem, PriceSeries
from .news import fetch_news, verified_topic_groups
from .providers import (
    fetch_asset_quote,
    fetch_intraday_series,
    verify_outlier_directions,
)
from .state import StateStore
from .telegram import TelegramClient


REPORT_LIMIT = 1200
REPORT_TYPES = {"korea_close", "europe_close"}

SESSION_SPECS: dict[str, dict[str, Any]] = {
    "korea_close": {
        "title": "한국장 마감",
        "timezone": KST,
        "send_time": time(15, 50),
        "core": ("kospi", "kosdaq", "usdkrw", "samsung", "skhynix"),
        "context": ("nasdaq_futures", "btc"),
        "session_start": time(9, 0),
        "silent": False,
    },
    "europe_close": {
        "title": "유럽장 마감",
        "timezone": PARIS,
        "send_time": time(18, 5),
        "core": ("eurostoxx50", "dax", "eurusd"),
        "context": ("wti", "gold", "nasdaq_futures", "btc"),
        "session_start": time(9, 0),
        "silent": True,
    },
}

ASSET_LABELS = {
    "kospi": "KOSPI",
    "kosdaq": "KOSDAQ",
    "usdkrw": "원/달러",
    "samsung": "삼성전자",
    "skhynix": "SK하이닉스",
    "nasdaq_futures": "Nasdaq 선물",
    "btc": "BTC",
    "eurostoxx50": "Euro Stoxx 50",
    "dax": "DAX",
    "eurusd": "EUR/USD",
    "wti": "WTI",
    "gold": "금",
}


@dataclass
class SessionReportResult:
    report_type: str
    report_key: str
    session_date: str
    status: str
    text: str = ""
    skip_reason: str = ""
    quotes: dict[str, AssetQuote] = field(default_factory=dict)
    facts: list[dict[str, Any]] = field(default_factory=list)
    telegram_message_id: int | None = None


def _direction(value: float | None) -> int:
    if value is None or abs(value) < 0.005:
        return 0
    return 1 if value > 0 else -1


def _format_value(quote: AssetQuote) -> str:
    if quote.key in {"usdkrw", "samsung", "skhynix"}:
        return f"{quote.current:,.0f}"
    if quote.key in {"btc", "wti", "gold"}:
        return f"${quote.current:,.2f}"
    if quote.key == "eurusd":
        return f"{quote.current:.4f}"
    return f"{quote.current:,.2f}"


def _move_line(quote: AssetQuote) -> str:
    percent = quote.percent_change
    arrow = "▬"
    change = "보합"
    if percent is not None:
        arrow = "▲" if percent > 0 else "▼" if percent < 0 else "▬"
        change = f"{abs(percent):.2f}%"
    return (
        f"• {html.escape(ASSET_LABELS.get(quote.key, quote.name_ko))} "
        f"<b>{html.escape(_format_value(quote))}</b> {arrow}{change}"
    )


def _series_change(
    series: PriceSeries,
    *,
    start: datetime,
    end: datetime,
) -> float | None:
    points = [
        point
        for point in series.points
        if start <= point.timestamp.astimezone(start.tzinfo) <= end
    ]
    if len(points) < 2 or points[0].value == 0:
        return None
    return (points[-1].value - points[0].value) / points[0].value * 100


def _apply_session_change(
    quote: AssetQuote,
    settings: Settings,
    *,
    start: datetime,
    end: datetime,
) -> None:
    try:
        series = fetch_intraday_series(
            quote.key,
            settings,
            hours=36,
            minimum_points=2,
        )
        percent = _series_change(series, start=start, end=end)
        if percent is None:
            return
        quote.previous = quote.current / (1 + percent / 100)
        quote.absolute_change = quote.current - quote.previous
        quote.percent_change = percent
        quote.comparison_label = "오늘 장 시작"
    except Exception:
        logging.info("Session intraday fallback unavailable for %s.", quote.key)


def _news_candidates(
    report_type: str,
    news: list[NewsItem],
) -> list[list[NewsItem]]:
    relevant = (
        {"kospi", "kosdaq", "usdkrw", "nasdaq100", "btc"}
        if report_type == "korea_close"
        else {"sp500", "nasdaq100", "dxy", "wti", "gold", "btc"}
    )
    groups = [
        group
        for group in verified_topic_groups(news)
        if relevant & {key for item in group for key in item.relevant_asset_keys}
    ]
    groups.sort(
        key=lambda group: max(
            item.published_at or datetime.min.replace(tzinfo=KST)
            for item in group
        ),
        reverse=True,
    )
    return groups[:1]


def _short_news(group: list[NewsItem]) -> tuple[str, str, bool, str]:
    best = min(group, key=lambda item: item.source_tier)
    original = best.title.rsplit(" - ", 1)[0].strip()
    if any("가" <= char <= "힣" for char in original):
        title = original
    else:
        assets = [
            ASSET_LABELS[key]
            for key in best.relevant_asset_keys
            if key in ASSET_LABELS
        ]
        subject = "·".join(assets[:2]) or "시장"
        title = f"{subject} 관련 새 정책·경제 보도"
    if len(title) > 82:
        title = title[:79].rstrip() + "..."
    publishers = sorted({item.publisher for item in group})
    source = "·".join(publishers[:2])
    official = any(item.official_source for item in group)
    published = max(
        (
            item.published_at.astimezone(KST).strftime("%H:%M KST")
            for item in group
            if item.published_at
        ),
        default="시각 미표기",
    )
    return title, source, official, published


def _fact_rows(
    quotes: dict[str, AssetQuote],
    news_groups: list[list[NewsItem]],
) -> list[dict[str, Any]]:
    facts = [
        {
            "fact_key": f"asset:{key}",
            "numeric_value": quote.percent_change,
            "direction": _direction(quote.percent_change),
            "official": False,
        }
        for key, quote in quotes.items()
        if quote.percent_change is not None
    ]
    for group in news_groups:
        facts.append(
            {
                "fact_key": f"news:{group[0].topic_key}",
                "numeric_value": None,
                "direction": 0,
                "official": any(item.official_source for item in group),
            }
        )
    return facts


def _novel_fact_keys(
    facts: list[dict[str, Any]],
    state: StateStore,
) -> set[str]:
    previous = state.recent_report_facts(hours=18)
    novel: set[str] = set()
    for fact in facts:
        key = str(fact["fact_key"])
        old = previous.get(key)
        if not old:
            novel.add(key)
            continue
        if fact.get("official") and not old.get("official"):
            novel.add(key)
            continue
        direction = int(fact.get("direction") or 0)
        if direction and direction != int(old.get("direction") or 0):
            novel.add(key)
            continue
        current = fact.get("numeric_value")
        prior = old.get("numeric_value")
        if current is not None and prior is not None and abs(float(current) - float(prior)) >= 0.5:
            novel.add(key)
    return novel


def _headline(quotes: dict[str, AssetQuote], core: tuple[str, ...]) -> str:
    ranked = sorted(
        (
            quote
            for key in core
            if (quote := quotes.get(key)) and quote.percent_change is not None
        ),
        key=lambda quote: abs(float(quote.percent_change or 0)),
        reverse=True,
    )
    if not ranked:
        return "당일 종가가 확인된 자산만 간단히 정리했습니다."
    first = ranked[0]
    direction = "상승" if float(first.percent_change or 0) > 0 else "하락"
    return (
        f"{ASSET_LABELS.get(first.key, first.name_ko)}가 "
        f"{abs(float(first.percent_change or 0)):.2f}% {direction}하며 장을 주도했습니다."
    )


def _next_check(settings: Settings, state: StateStore, now: datetime) -> str:
    try:
        events = [
            event
            for event in fetch_economic_events(settings, days_ahead=1, store=state)
            if event.event_time_kst > now.astimezone(KST)
        ]
        if events:
            event = min(events, key=lambda item: item.event_time_kst)
            return f"{event.event_time_kst:%m/%d %H:%M} {event.title_ko}"
    except Exception:
        logging.info("Session report calendar unavailable.")
    return "다음 중요 지표와 미국 선물 방향"


def render_session_report(
    report_type: str,
    quotes: dict[str, AssetQuote],
    news_groups: list[list[NewsItem]],
    novel_keys: set[str],
    *,
    now: datetime,
    next_check: str,
) -> str:
    spec = SESSION_SPECS[report_type]
    core = spec["core"]
    context = spec["context"]
    core_quotes = [quotes[key] for key in core if key in quotes]
    context_quotes = [quotes[key] for key in context if key in quotes]
    lines = [
        f"<b>JIN Market Pulse · {spec['title']}</b>",
        f"{now.astimezone(spec['timezone']):%m/%d %H:%M} 현지",
        "",
        "<b>한눈에</b>",
        html.escape(_headline(quotes, core)),
        "",
        "<b>오늘 장의 실제 움직임</b>",
        *[_move_line(quote) for quote in core_quotes],
    ]
    if context_quotes:
        lines.extend(
            [
                "",
                "<b>같은 시간 시장</b>",
                *[_move_line(quote) for quote in context_quotes],
            ]
        )
    lines.extend(["", "<b>왜 움직였나</b>"])
    shown_news = False
    for group in news_groups:
        key = f"news:{group[0].topic_key}"
        if key not in novel_keys:
            continue
        title, source, official, published = _short_news(group)
        level = "공식 발표" if official else "주요 매체 보도"
        lines.append(f"• {html.escape(title)}")
        related = [
            quotes[key]
            for key in {asset for item in group for asset in item.relevant_asset_keys}
            if key in quotes and quotes[key].percent_change is not None
        ]
        if related:
            reaction = max(
                related,
                key=lambda quote: abs(float(quote.percent_change or 0)),
            )
            arrow = "▲" if float(reaction.percent_change or 0) > 0 else "▼"
            lines.append(
                f"  반응: {html.escape(ASSET_LABELS.get(reaction.key, reaction.name_ko))} "
                f"{arrow}{abs(float(reaction.percent_change or 0)):.2f}%"
            )
        lines.append(f"  {level} · {html.escape(source)} · {published}")
        shown_news = True
    if not shown_news:
        lines.append("• 검증된 새 원인은 없으며 가격 흐름만 확인됐습니다.")

    changed_assets = [
        quote
        for quote in core_quotes + context_quotes
        if f"asset:{quote.key}" in novel_keys
    ]
    lines.extend(["", "<b>앞선 보고 이후 달라진 점</b>"])
    if changed_assets:
        strongest = max(
            changed_assets,
            key=lambda quote: abs(float(quote.percent_change or 0)),
        )
        lines.append(
            f"• {html.escape(ASSET_LABELS.get(strongest.key, strongest.name_ko))} "
            f"방향·변동폭이 새 기준에 도달했습니다."
        )
    else:
        lines.append("• 새 방향 전환이나 추가 급변은 없었습니다.")

    lines.extend(
        [
            "",
            "<b>다음 체크</b>",
            f"• {html.escape(next_check)}",
        ]
    )
    text = "\n".join(lines)
    if len(text) > REPORT_LIMIT:
        compact = [
            f"<b>JIN Market Pulse · {spec['title']}</b>",
            f"{now.astimezone(spec['timezone']):%m/%d %H:%M} 현지",
            "",
            "<b>한눈에</b>",
            html.escape(_headline(quotes, core)),
            "",
            "<b>오늘 장의 실제 움직임</b>",
            *[_move_line(quote) for quote in core_quotes],
            "",
            "<b>왜 움직였나</b>",
            "• 검증된 새 원인만 후속 속보로 갱신합니다.",
            "",
            "<b>앞선 보고 이후 달라진 점</b>",
            "• 새 방향 전환과 0.5%p 이상 추가 변동만 표시합니다.",
            "",
            "<b>다음 체크</b>",
            f"• {html.escape(next_check)}",
        ]
        text = "\n".join(compact)
    validate_session_report(text)
    return text


def validate_session_report(text: str) -> None:
    if len(text) > REPORT_LIMIT:
        raise ValueError("Session report exceeds 1200 characters")
    for forbidden in ("|", "#", "N/A", "확인 필요", "매수", "매도"):
        if forbidden in text:
            raise ValueError(f"Forbidden session report phrase: {forbidden}")
    for heading in (
        "한눈에",
        "오늘 장의 실제 움직임",
        "왜 움직였나",
        "앞선 보고 이후 달라진 점",
        "다음 체크",
    ):
        if heading not in text:
            raise ValueError(f"Missing session report heading: {heading}")


def build_session_report(
    report_type: str,
    settings: Settings,
    state: StateStore,
    *,
    now: datetime | None = None,
) -> SessionReportResult:
    if report_type not in REPORT_TYPES:
        raise ValueError(f"Unsupported report type: {report_type}")
    spec = SESSION_SPECS[report_type]
    local_now = (now or datetime.now(spec["timezone"])).astimezone(spec["timezone"])
    session_date = local_now.date().isoformat()
    report_key = f"{report_type}:{session_date}"
    if local_now.weekday() >= 5:
        return SessionReportResult(
            report_type,
            report_key,
            session_date,
            "skipped",
            skip_reason="주말 휴장",
        )

    quotes: dict[str, AssetQuote] = {}
    errors: list[str] = []
    for key in (*spec["core"], *spec["context"]):
        try:
            quote = fetch_asset_quote(key, settings, state)
            if not quote.stale and quote.verified:
                quotes[key] = quote
        except Exception as exc:
            errors.append(f"{key}:{type(exc).__name__}")
    errors.extend(verify_outlier_directions(quotes, settings))
    quotes = {
        key: quote
        for key, quote in quotes.items()
        if quote.verified and not quote.stale
    }

    anchor = quotes.get(spec["core"][0])
    if not anchor:
        return SessionReportResult(
            report_type,
            report_key,
            session_date,
            "skipped",
            skip_reason="대표 지수 당일 종가 없음",
        )
    anchor_date = anchor.as_of.astimezone(spec["timezone"]).date()
    if anchor_date != local_now.date():
        return SessionReportResult(
            report_type,
            report_key,
            session_date,
            "skipped",
            skip_reason="휴장 또는 당일 종가 미확인",
        )

    session_start = datetime.combine(
        local_now.date(),
        spec["session_start"],
        tzinfo=spec["timezone"],
    )
    for key in spec["context"]:
        if key in quotes:
            _apply_session_change(
                quotes[key],
                settings,
                start=session_start,
                end=local_now,
            )

    groups = _news_candidates(report_type, fetch_news())
    facts = _fact_rows(quotes, groups)
    novel = _novel_fact_keys(facts, state)
    text = render_session_report(
        report_type,
        quotes,
        groups,
        novel,
        now=local_now,
        next_check=_next_check(settings, state, local_now),
    )
    return SessionReportResult(
        report_type,
        report_key,
        session_date,
        "ready",
        text=text,
        quotes=quotes,
        facts=facts,
        skip_reason="; ".join(errors[:3]),
    )


def send_session_report(
    report_type: str,
    settings: Settings,
    state: StateStore,
    telegram: TelegramClient,
    *,
    deliver: bool,
    idempotency_key: str | None = None,
    now: datetime | None = None,
) -> SessionReportResult:
    result = build_session_report(report_type, settings, state, now=now)
    if idempotency_key:
        result.report_key = f"{result.report_key}:{idempotency_key[:80]}"
    if result.status == "skipped":
        state.record_report_run(
            result.report_key,
            report_type,
            result.session_date,
            "skipped",
            skip_reason=result.skip_reason,
        )
        return result
    if not deliver:
        state.record_report_run(
            result.report_key,
            report_type,
            result.session_date,
            "preview",
            text=result.text,
            facts=result.facts,
            skip_reason=result.skip_reason,
        )
        result.status = "preview"
        return result
    if not state.claim_job(result.report_key, lease_seconds=15 * 60):
        result.status = "duplicate"
        return result
    try:
        spec = SESSION_SPECS[report_type]
        message_ids = telegram.send(
            result.text,
            parse_mode="HTML",
            disable_notification=bool(spec["silent"]),
        )
        result.telegram_message_id = message_ids[0] if message_ids else None
        result.status = "sent"
        state.record_report_run(
            result.report_key,
            report_type,
            result.session_date,
            "sent",
            text=result.text,
            facts=result.facts,
            telegram_message_id=result.telegram_message_id,
            skip_reason=result.skip_reason,
        )
        state.save_message(
            f"{report_type}:{result.session_date}",
            result.text,
            parse_mode="HTML",
        )
        state.finish_job(result.report_key, success=True)
        return result
    except Exception as exc:
        state.finish_job(
            result.report_key,
            success=False,
            error=type(exc).__name__,
        )
        raise


def report_due(report_type: str, now: datetime) -> bool:
    spec = SESSION_SPECS[report_type]
    local = now.astimezone(spec["timezone"])
    target = spec["send_time"]
    return (
        local.weekday() < 5
        and local.hour == target.hour
        and target.minute <= local.minute <= target.minute + 14
    )


def next_report_time(report_type: str, now: datetime | None = None) -> datetime:
    spec = SESSION_SPECS[report_type]
    current = (now or datetime.now(spec["timezone"])).astimezone(spec["timezone"])
    candidate = datetime.combine(
        current.date(),
        spec["send_time"],
        tzinfo=spec["timezone"],
    )
    if candidate <= current:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate
