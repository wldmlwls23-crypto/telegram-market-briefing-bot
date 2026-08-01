from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Literal

from .advisor import (
    create_advisor_answer,
    create_current_move_answer,
    matched_topics,
    render_topic_explanation,
)
from .calendar import event_meaning, fetch_economic_events
from .config import KST, Settings
from .models import AssetQuote, EconomicEvent
from .news import fetch_news
from .providers import (
    fetch_asset_quote,
    fetch_btc_intraday_series,
    fetch_market_quotes,
)
from .reports import format_quote
from .session_reports import next_report_time
from .state import StateStore
from .telegram import MAIN_KEYBOARD, REMOVE_KEYBOARD
from .us_open import next_us_open_preview_time


Intent = Literal[
    "start",
    "menu",
    "help",
    "brief",
    "last",
    "status",
    "settings",
    "mute",
    "reset",
    "alerts",
    "alert_create",
    "calendar",
    "markets",
    "compare",
    "current_cause",
    "price",
    "definition",
    "relation",
    "advice",
    "follow_up",
    "unknown",
]


ASSET_DEFINITIONS: dict[str, dict[str, Any]] = {
    "btc": {
        "name": "BTC",
        "aliases": {"btc", "비트", "비트코인", "bitcoin"},
    },
    "eth": {
        "name": "ETH",
        "aliases": {"eth", "이더", "이더리움", "ethereum"},
    },
    "sp500": {
        "name": "S&P 500",
        "aliases": {"s&p500", "s&p 500", "sp500", "에스앤피", "snp"},
    },
    "nasdaq100": {
        "name": "Nasdaq 100",
        "aliases": {"nasdaq", "nasdaq100", "나스닥", "나스닥100", "ndx"},
    },
    "dow": {
        "name": "Dow Jones",
        "aliases": {"dow", "다우", "다우존스"},
    },
    "dxy": {
        "name": "DXY",
        "aliases": {"dxy", "달러인덱스", "달러 지수"},
    },
    "us2y": {
        "name": "미국채 2년물",
        "aliases": {"미국채 2년", "미국채2년", "미 2년", "2년물", "us2y"},
    },
    "us10y": {
        "name": "미국채 10년물",
        "aliases": {"미국채 10년", "미국채10년", "미 10년", "10년물", "us10y"},
    },
    "kospi": {
        "name": "KOSPI",
        "aliases": {"kospi", "코스피"},
    },
    "kosdaq": {
        "name": "KOSDAQ",
        "aliases": {"kosdaq", "코스닥"},
    },
    "usdkrw": {
        "name": "원/달러",
        "aliases": {"원달러", "원/달러", "환율", "usdkrw"},
    },
    "wti": {
        "name": "WTI",
        "aliases": {"wti", "유가", "원유", "서부텍사스유"},
    },
    "gold": {
        "name": "금",
        "aliases": {"gold", "금값", "금 가격", "금 시세"},
    },
}
SUPPORTED_ORDER = tuple(ASSET_DEFINITIONS)

PRICE_TERMS = {
    "가격",
    "얼마",
    "시세",
    "변동",
    "현재가",
    "종가",
    "올랐",
    "내렸",
    "원화",
}
EXPLAIN_TERMS = {"뭐야", "무엇", "뜻", "설명", "알려줘", "왜 중요"}
CAUSE_TERMS = {"왜", "이유", "원인", "배경", "무슨 일"}
RELATION_TERMS = {
    "관계",
    "영향",
    "연결",
    "어떻게",
    "오르면",
    "내리면",
    "높으면",
    "낮으면",
    "중요",
}
ADVICE_TERMS = {
    "전망",
    "예측",
    "사도",
    "살까",
    "팔까",
    "매수",
    "매도",
    "목표가",
    "수익",
    "오를까",
    "내릴까",
    "들어가도",
}
FOLLOW_UP_TERMS = {
    "그럼",
    "아까",
    "그거",
    "좀 더 쉽게",
    "더 쉽게",
    "숫자만",
    "다시",
    "그 뉴스",
    "이거",
}
MARKET_TERMS = {
    "시장",
    "주식",
    "코인",
    "달러",
    "채권",
    "금리",
    "물가",
    "인플레",
    "경기",
    "유동성",
    "연준",
    "fed",
    "환율",
}


@dataclass
class RouteResult:
    intent: Intent
    asset_keys: list[str] = field(default_factory=list)
    period: str = ""
    command: str = ""
    candidates: list[str] = field(default_factory=list)


@dataclass
class BotResponse:
    text: str
    reply_markup: dict[str, Any] | None = None
    parse_mode: str = "HTML"


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def _asset_keys(text: str) -> list[str]:
    normalized = _normalized(text)
    found: list[tuple[int, str]] = []
    for key, definition in ASSET_DEFINITIONS.items():
        for alias in sorted(definition["aliases"], key=len, reverse=True):
            index = normalized.find(alias)
            if index >= 0:
                found.append((index, key))
                break
    # A standalone "금" means gold, while 지금/금리/오늘금리는 not gold.
    gold_match = re.search(
        r"(?<![가-힣])금(?:은|는|이|가|을|를|도|과|와)?(?![가-힣])",
        normalized,
    )
    if gold_match:
        found.append((gold_match.start(), "gold"))
    return list(dict.fromkeys(key for _, key in sorted(found)))


def _period(text: str) -> str:
    normalized = _normalized(text)
    if "다음 주" in normalized or "다음주" in normalized:
        return "next_week"
    if "이번 주" in normalized or "이번주" in normalized or "주간" in normalized:
        return "this_week"
    if "내일" in normalized:
        return "tomorrow"
    if "오늘" in normalized:
        return "today"
    if "24시간" in normalized or "하루" in normalized:
        return "24h"
    if "어제 종가" in normalized or "전일 종가" in normalized:
        return "previous_close"
    if re.search(r"(새벽|오전)\s*\d{1,2}시", normalized):
        return "intraday"
    return ""


def route_query(text: str, context: dict[str, Any] | None = None) -> RouteResult:
    normalized = _normalized(text)
    command = normalized.split(" ", 1)[0] if normalized.startswith("/") else ""
    assets = _asset_keys(normalized)
    period = _period(normalized)

    command_intents: dict[str, Intent] = {
        "/start": "start",
        "/menu": "menu",
        "/help": "help",
        "/brief": "brief",
        "/last": "last",
        "/status": "status",
        "/settings": "settings",
        "/mute": "mute",
        "/reset": "reset",
        "/alerts": "alerts",
        "/calendar": "calendar",
        "/week": "calendar",
        "/markets": "markets",
        "/compare": "compare",
        "/price": "price",
    }
    if command in command_intents:
        intent = command_intents[command]
        if command == "/week":
            period = period or "this_week"
        if command == "/alerts" and any(
            term in normalized for term in {"위", "아래", "이상", "이하"}
        ):
            intent = "alert_create"
        return RouteResult(intent, assets, period, command)

    if normalized in {"현재 시장", "전체 시장", "전체 시세"}:
        return RouteResult("markets", assets, period)
    if normalized in {"메뉴", "메뉴 열기", "버튼 열기"}:
        return RouteResult("menu", assets, period)
    if normalized in {"오늘 일정", "일정"}:
        return RouteResult("calendar", assets, "today")
    if normalized in {"이번 주", "이번주"}:
        return RouteResult("calendar", assets, "this_week")
    if normalized in {"최근 리포트", "오늘 모닝 다시", "모닝 다시"}:
        return RouteResult("brief", assets, period)
    if normalized in {"상태", "봇 상태"}:
        return RouteResult("status", assets, period)
    if normalized == "왜 움직여?":
        inherited = str((context or {}).get("asset_key") or "")
        return RouteResult(
            "current_cause" if inherited else "unknown",
            [inherited] if inherited else [],
            period,
        )
    if any(term in normalized for term in FOLLOW_UP_TERMS) and context:
        return RouteResult("follow_up", assets, period)

    if any(term in normalized for term in ADVICE_TERMS):
        return RouteResult("advice", assets, period)

    if (
        (
            "알림" in normalized
            and any(
                term in normalized
                for term in {"위", "아래", "이상", "이하", "밑", "넘"}
            )
        )
        or re.search(
            r"(위|아래|밑|이상|이하|넘)(?:이면|으면|면|일 때).*(알려|알림)",
            normalized,
        )
    ):
        return RouteResult("alert_create", assets, period)
    if "알림" in normalized and any(
        term in normalized for term in {"목록", "내 알림", "삭제", "지워"}
    ):
        return RouteResult("alerts", assets, period)
    if (
        any(
            kind in normalized
            for kind in {
                "긴급", "경제", "지표", "한국 마감", "한국장",
                "유럽", "미국장", "미국 장", "새벽",
            }
        )
        and "알림" in normalized
        and any(term in normalized for term in {"꺼", "켜"})
    ):
        return RouteResult("settings", assets, period)
    if "새벽" in normalized and any(
        term in normalized for term in {"무음", "소리", "켜", "꺼", "해제"}
    ):
        return RouteResult("settings", assets, period)
    if "조용히" in normalized or (
        "알림" in normalized and any(term in normalized for term in {"꺼", "켜"})
    ):
        return RouteResult("mute", assets, period)
    if "앞으로 언제" in normalized or "메시지 언제" in normalized:
        return RouteResult("status", assets, period)

    if (
        "일정" in normalized
        or "캘린더" in normalized
        or "발표" in normalized
        or ("경제지표" in normalized and bool(period))
    ):
        return RouteResult("calendar", assets, period or "24h")
    if any(term in normalized for term in {"비교", "중 뭐", "랑", "과"}) and len(assets) >= 2:
        return RouteResult("compare", assets[:2], period)

    asks_cause = any(term in normalized for term in CAUSE_TERMS)
    movement_terms = {
        "지금",
        "오늘",
        "떨어",
        "내려",
        "오르",
        "올라",
        "하락",
        "상승",
        "급락",
        "급등",
        "가격",
        "움직",
        "이래",
    }
    if (
        assets
        and asks_cause
        and any(term in normalized for term in movement_terms)
        and "왜 중요" not in normalized
    ):
        return RouteResult("current_cause", assets[:1], period)

    if normalized in {"미국채", "국채", "미 국채"}:
        return RouteResult(
            "unknown",
            candidates=["us2y", "us10y"],
        )

    topics = matched_topics(normalized)
    wants_explanation = any(term in normalized for term in EXPLAIN_TERMS)
    asks_relation = any(term in normalized for term in RELATION_TERMS)
    wants_price = any(term in normalized for term in PRICE_TERMS)

    if (
        len(topics) == 1
        and wants_explanation
        and not asks_relation
        and not wants_price
    ):
        return RouteResult("definition", assets, period)
    if asks_relation and (
        topics
        or assets
        or any(term in normalized for term in MARKET_TERMS)
    ):
        return RouteResult("relation", assets, period)
    if assets and (wants_price or period):
        return RouteResult("price", assets[:1], period)
    if assets:
        return RouteResult("price", assets[:1], period)
    if topics:
        return RouteResult("definition", assets, period)
    if normalized in {"사용법", "사용법 알려줘", "도움말", "뭘 물어봐"}:
        return RouteResult("help", assets, period)
    return RouteResult("unknown", assets, period)


def _format_number(value: float, unit: str = "") -> str:
    if unit == "%":
        return f"{value:.2f}%"
    if unit == "원":
        return f"{value:,.2f}원"
    if unit == "USD":
        return f"${value:,.2f}"
    return f"{value:,.2f}"


def _market_state_ko(state: str) -> str:
    normalized = state.upper()
    if normalized == "OPEN":
        return "거래 중"
    if normalized == "PRE":
        return "장전"
    if normalized == "POST":
        return "장후"
    if normalized == "OFFICIAL_DAILY":
        return "공식 일일 고시"
    return "장 마감값"


def _quote_html(quote: AssetQuote, *, compact: bool = False) -> str:
    display_unit = quote.unit or ("USD" if quote.kind == "crypto" else "")
    current = _format_number(quote.current, display_unit)
    if quote.kind == "yield" and quote.absolute_change is not None:
        change = f"{quote.absolute_change * 100:+.1f}bp"
    elif quote.percent_change is not None:
        arrow = "▲" if quote.percent_change > 0 else "▼" if quote.percent_change < 0 else "•"
        change = f"{arrow}{abs(quote.percent_change):.2f}%"
    else:
        change = "변화율 미제공"
    first = f"<b>{html.escape(quote.name_ko)}</b>  {html.escape(current)} · {html.escape(change)}"
    if compact:
        return first
    previous = ""
    if quote.previous is not None:
        previous = (
            f"\n{html.escape(quote.comparison_label)} "
            f"{html.escape(_format_number(quote.previous, display_unit))}"
        )
    flags = ""
    if quote.proxy:
        flags = "\n대용 자산 값"
    elif quote.stale:
        flags = "\n마지막 정상값"
    return (
        f"{first}{previous}"
        f"\n기준 {quote.as_of.astimezone(KST):%m/%d %H:%M} KST · "
        f"{html.escape(_market_state_ko(quote.market_state))}"
        f"\n출처 {html.escape(quote.source)}{flags}"
    )


def _calendar_html(
    events: list[EconomicEvent],
    *,
    title: str = "앞으로 24시간 중요 일정",
) -> str:
    lines = [f"<b>{html.escape(title)}</b>"]
    if not events:
        lines.append("해당 기간에 선별된 중요 일정이 없습니다.")
        return "\n".join(lines)
    for index, event in enumerate(events):
        if index:
            lines.append("")
        values = []
        if event.actual:
            values.append(f"실제: {event.actual}")
        if event.forecast:
            values.append(f"예상: {event.forecast}")
        elif not event.actual:
            values.append("예상치 미공개")
        if event.previous:
            values.append(f"이전: {event.previous}")
        lines.extend(
            [
                f"<b>{html.escape(event.country_ko)} · {html.escape(event.title_ko)}</b>",
                f"발표 시간: <b>{event.event_time_kst:%m/%d %H:%M} KST</b>",
                f"중요도: {event.importance}",
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
    return "\n".join(lines)


def _calendar_range(
    events: list[EconomicEvent],
    period: str,
    now: datetime,
) -> tuple[list[EconomicEvent], str]:
    if period == "today":
        end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        title = "오늘 남은 중요 일정"
    elif period == "tomorrow":
        start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return (
            [event for event in events if start <= event.event_time_kst < end][:5],
            "내일 중요 일정",
        )
    elif period == "this_week":
        end = (now + timedelta(days=7 - now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        title = "이번 주 핵심 경제일정"
    elif period == "next_week":
        start = (now + timedelta(days=7 - now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end = start + timedelta(days=7)
        return (
            [event for event in events if start <= event.event_time_kst < end][:8],
            "다음 주 핵심 경제일정",
        )
    else:
        end = now + timedelta(hours=24)
        title = "앞으로 24시간 중요 일정"
    selected = [event for event in events if now <= event.event_time_kst <= end]
    limit = 8 if period in {"this_week", "next_week"} else 3
    ranked = sorted(
        selected,
        key=lambda event: (
            0 if event.importance == "★★★★★" else 1,
            event.event_time_kst,
        ),
    )[:limit]
    return sorted(ranked, key=lambda event: event.event_time_kst), title


def _week_events(events: list[EconomicEvent], now: datetime) -> list[EconomicEvent]:
    selected, _ = _calendar_range(events, "this_week", now)
    return selected


def _help() -> str:
    return "\n".join(
        [
            "<b>JIN Market Pulse 사용법</b>",
            "빠른 버튼은 /menu로 열 수 있으며 한 번 사용하면 자동으로 접힙니다.",
            "",
            "<b>가격</b>",
            "• 이더 얼마야",
            "• 비트 원화로",
            "• 미 10년물 변동",
            "",
            "<b>비교·이유</b>",
            "• 비트랑 금 중 뭐가 더 올랐어?",
            "• 코스피 왜 떨어져?",
            "• DXY가 뭐야?",
            "",
            "<b>일정</b>",
            "• 오늘 일정 / 이번 주 일정 / 다음 주 일정",
            "",
            "<b>개인 알림</b>",
            "• 비트 65000 아래면 알려줘",
            "• 내 알림 목록 / 알림 2 삭제",
            "",
            "매수·매도와 가격 예측 대신 현재 데이터, 위험 요인, 예정 이벤트를 알려드립니다.",
        ]
    )


def _start() -> str:
    return "\n".join(
        [
            "<b>JIN Market Pulse</b>",
            "아침 시장, 실시간 가격, 경제일정과 움직임의 배경을 여기서 바로 물어보세요.",
            "",
            "평소 말투로 질문하거나 필요할 때 /menu로 빠른 버튼을 여세요.",
            "예: <i>이더 얼마야</i> · <i>코스피 왜 떨어져?</i>",
        ]
    )


def _live_quote(
    key: str,
    settings: Settings,
    *,
    period: str = "",
    original_text: str = "",
) -> str:
    if key == "btc" and period == "intraday":
        hour_match = re.search(r"(?:새벽|오전)\s*(\d{1,2})시", original_text)
        if hour_match:
            hour = max(0, min(int(hour_match.group(1)), 23))
            series = fetch_btc_intraday_series(settings)
            now = datetime.now(KST)
            start_kst = datetime.combine(now.date(), time(hour=hour), tzinfo=KST)
            if start_kst > now:
                start_kst -= timedelta(days=1)
            points = [
                point
                for point in series.points
                if point.timestamp.astimezone(KST) >= start_kst
            ]
            if len(points) >= 2:
                start = points[0].value
                end = points[-1].value
                change = (end - start) / start * 100
                return "\n".join(
                    [
                        "<b>BTC 구간 변동</b>",
                        f"{start_kst:%m/%d %H:%M} KST  {_format_number(start, 'USD')}",
                        f"현재  {_format_number(end, 'USD')} · <b>{change:+.2f}%</b>",
                        f"출처 {html.escape(series.source)}",
                    ]
                )
    quote = fetch_asset_quote(key, settings)
    lines = [_quote_html(quote)]
    if key in {"btc", "eth"} and any(term in original_text for term in {"원화", "원으로"}):
        fx = fetch_asset_quote("usdkrw", settings)
        lines.insert(
            1,
            (
                f"\n원화 환산 약 <b>{quote.current * fx.current:,.0f}원</b>"
                f"\n환율 기준 {fx.as_of.astimezone(KST):%m/%d %H:%M} KST"
            ),
        )
    return "\n".join(lines)


def _compare(keys: list[str], settings: Settings) -> str:
    quotes = [fetch_asset_quote(key, settings) for key in keys[:2]]
    lines = ["<b>변동 비교</b>"]
    for quote in quotes:
        lines.append(_quote_html(quote, compact=True))
        lines.append(
            f"  {html.escape(quote.comparison_label)} · {quote.as_of.astimezone(KST):%m/%d %H:%M} KST"
        )
    comparable = (
        quotes[0].percent_change is not None
        and quotes[1].percent_change is not None
        and quotes[0].comparison_label == quotes[1].comparison_label
    )
    if comparable:
        leader = max(quotes, key=lambda quote: quote.percent_change or 0)
        lines.extend(
            [
                "",
                f"같은 비교 기준에서는 <b>{html.escape(leader.name_ko)}</b>의 변동률이 더 높습니다.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "비교 기준이 달라 방향과 크기를 따로 표시했습니다. 서로 다른 기준의 수치를 한 순위로 단정하지 않습니다.",
            ]
        )
    return "\n".join(lines)


def _markets(settings: Settings) -> str:
    quotes, errors = fetch_market_quotes(settings)
    lines = ["<b>현재 시장</b>"]
    for key in SUPPORTED_ORDER:
        quote = quotes.get(key)
        if quote and not quote.stale:
            lines.append(_quote_html(quote, compact=True))
    lines.append(f"\n<i>조회 {datetime.now(KST):%m/%d %H:%M} KST</i>")
    if errors and len(quotes) < 8:
        lines.append("일부 공급원 값은 마지막 정상값 또는 생략 처리했습니다.")
    return "\n".join(lines)


def _calendar(
    settings: Settings,
    period: str,
    store: StateStore | None = None,
) -> str:
    now = datetime.now(KST)
    days = 14 if period == "next_week" else 8 if period == "this_week" else 2
    events = fetch_economic_events(settings, days_ahead=days, store=store)
    selected, title = _calendar_range(events, period or "24h", now)
    if period == "this_week":
        selected = _week_events(events, now)
    if store:
        tracked_at = now.isoformat()
        for event in selected:
            store.update_event(
                event.event_id,
                title=event.title,
                event_time=event.event_time_kst.isoformat(),
                importance=event.importance,
                forecast=event.forecast,
                previous=event.previous,
                tracked_for_result_at=tracked_at,
            )
    return _calendar_html(selected, title=title)


def _advice_refusal(settings: Settings) -> str:
    return "\n".join(
        [
        "<b>매수·매도 결정과 가격 예측은 제공하지 않습니다.</b>",
        "대신 현재 가격, 위험 요인과 예정 이벤트를 확인할 수 있습니다.",
        "가격은 <i>비트 얼마야</i>, 일정은 <i>오늘 일정</i>이라고 물어보세요.",
        ]
    )


def _advisor_or_limit(
    text: str,
    settings: Settings,
    store: StateStore | None,
) -> str:
    if not settings.enable_ai_advisor or store is None:
        return "<b>일반 관계 설명 기능이 잠시 꺼져 있습니다.</b>"
    if not store.claim_usage_slot(
        "advisor",
        settings.ai_advisor_daily_limit,
        shared_limit=settings.ai_advisor_daily_limit,
    ):
        return (
            "<b>오늘의 AI 설명 횟수를 모두 사용했습니다.</b>\n"
            "가격·일정·내장 용어 설명은 계속 무료로 조회할 수 있습니다."
        )
    try:
        return create_advisor_answer(text, settings)
    except Exception:
        store.release_ai_advisor_slot()
        topics = matched_topics(text)
        if topics:
            return render_topic_explanation(topics[0])
        return "<b>AI 설명을 불러오지 못했습니다.</b>\n현재 가격과 일정 조회는 정상 이용할 수 있습니다."


def _data_only_cause(target: AssetQuote, quotes: dict[str, AssetQuote]) -> str:
    lines = [
        f"<b>{html.escape(target.name_ko)} 움직임</b>",
        _quote_html(target, compact=True),
        "",
        "<b>데이터로 확인되는 동행</b>",
    ]
    peers = [
        quote
        for key in ("dxy", "us10y", "nasdaq100", "kospi", "wti", "gold", "btc")
        if (quote := quotes.get(key))
        and quote.key != target.key
        and quote.percent_change is not None
        and quote.verified
    ][:4]
    lines.extend(_quote_html(quote, compact=True) for quote in peers)
    lines.extend(
        [
            "",
            "직접 원인을 입증할 신뢰 자료가 없어 가격 동행만 표시했습니다. 동행은 원인과 다를 수 있습니다.",
        ]
    )
    return "\n".join(lines)


def _current_move_or_limit(
    text: str,
    key: str,
    settings: Settings,
    store: StateStore | None,
) -> str:
    quotes, _ = fetch_market_quotes(settings)
    target = quotes.get(key) or fetch_asset_quote(key, settings)
    quotes[key] = target
    if not settings.enable_ai_advisor or store is None:
        return _data_only_cause(target, quotes)
    if not store.claim_usage_slot(
        "current_cause",
        settings.ai_current_cause_daily_limit,
        shared_limit=settings.ai_advisor_daily_limit,
    ):
        return _data_only_cause(target, quotes)
    try:
        return create_current_move_answer(
            text,
            target,
            quotes,
            fetch_news(max_per_feed=5),
            settings,
        )
    except Exception:
        store.release_usage_slot("current_cause")
        return _data_only_cause(target, quotes)


def _parse_alert(text: str) -> tuple[float, str, bool] | None:
    match = re.search(
        r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(달러|원|usd|krw)?\s*"
        r"(아래|이하|밑|위|이상|넘)",
        _normalized(text),
    )
    if not match:
        return None
    threshold = float(match.group(1).replace(",", ""))
    direction = "below" if match.group(3) in {"아래", "이하", "밑"} else "above"
    recurring = "반복" in _normalized(text) or "계속" in _normalized(text)
    return threshold, direction, recurring


def _alert_create(
    text: str,
    keys: list[str],
    settings: Settings,
    store: StateStore | None,
) -> str:
    if store is None:
        return "<b>가격 알림 저장소를 사용할 수 없습니다.</b>"
    if not keys:
        return "어떤 자산인지 함께 써주세요. 예: <i>비트 65000 아래면 알려줘</i>"
    parsed = _parse_alert(text)
    if not parsed:
        return "가격과 방향을 함께 써주세요. 예: <i>금 2500 위면 알려줘</i>"
    threshold, direction, recurring = parsed
    try:
        alert_id = store.create_price_alert(
            settings.telegram_chat_id,
            keys[0],
            direction,
            threshold,
            recurring=recurring,
            max_alerts=settings.max_price_alerts,
        )
    except ValueError:
        return f"<b>활성 가격 알림은 최대 {settings.max_price_alerts}개입니다.</b>\n기존 알림을 삭제한 뒤 추가하세요."
    direction_ko = "아래" if direction == "below" else "위"
    mode = "반복" if recurring else "1회"
    return "\n".join(
        [
            "<b>가격 알림을 저장했습니다.</b>",
            f"#{alert_id} · {html.escape(ASSET_DEFINITIONS[keys[0]]['name'])} "
            f"{threshold:,.2f} {direction_ko} · {mode}",
            "30분 간격으로 점검합니다.",
        ]
    )


def _alerts(text: str, settings: Settings, store: StateStore | None) -> str:
    if store is None:
        return "<b>가격 알림 저장소를 사용할 수 없습니다.</b>"
    delete_match = re.search(r"(?:알림\s*)?(\d+)\s*(?:삭제|지워)", _normalized(text))
    if delete_match:
        removed = store.delete_price_alert(
            settings.telegram_chat_id,
            int(delete_match.group(1)),
        )
        return "가격 알림을 삭제했습니다." if removed else "해당 번호의 활성 알림이 없습니다."
    alerts = store.list_price_alerts(settings.telegram_chat_id)
    lines = ["<b>내 가격 알림</b>"]
    if not alerts:
        lines.append("활성 알림이 없습니다.")
        lines.append("예: <i>비트 65000 아래면 알려줘</i>")
        return "\n".join(lines)
    for item in alerts:
        direction = "아래" if item["direction"] == "below" else "위"
        mode = "반복" if item["recurring"] else "1회"
        name = ASSET_DEFINITIONS.get(item["asset_key"], {}).get("name", item["asset_key"])
        lines.append(
            f"#{item['id']} · {html.escape(str(name))} "
            f"{float(item['threshold']):,.2f} {direction} · {mode}"
        )
    lines.append("\n삭제: <i>알림 2 삭제</i>")
    return "\n".join(lines)


def _mute(text: str, settings: Settings, store: StateStore | None) -> str:
    if store is None:
        return "<b>알림 설정 저장소를 사용할 수 없습니다.</b>"
    normalized = _normalized(text)
    if any(term in normalized for term in {"다시 켜", "해제", "unmute", "/mute off"}):
        store.update_preferences(settings.telegram_chat_id, muted_until="")
        return "자동 알림을 다시 켰습니다."
    match = re.search(r"(\d{1,3})\s*(시간|h|분|m)", normalized)
    if match:
        amount = int(match.group(1))
        delta = timedelta(hours=amount) if match.group(2) in {"시간", "h"} else timedelta(minutes=amount)
    else:
        delta = timedelta(hours=8)
    until = datetime.now(KST) + delta
    store.update_preferences(
        settings.telegram_chat_id,
        muted_until=until.astimezone().isoformat(),
    )
    return f"자동 알림을 <b>{until:%m/%d %H:%M} KST</b>까지 조용히 합니다.\n개인 가격 알림과 직접 질문 답변은 계속 동작합니다."


def _settings(text: str, settings: Settings, store: StateStore | None) -> str:
    if store is None:
        return "<b>설정 저장소를 사용할 수 없습니다.</b>"
    normalized = _normalized(text)
    values: dict[str, Any] = {}
    if "긴급" in normalized and "꺼" in normalized:
        values["emergency_alerts"] = False
    elif "긴급" in normalized and "켜" in normalized:
        values["emergency_alerts"] = True
    if "경제" in normalized and "꺼" in normalized:
        values["event_alerts"] = False
    elif "경제" in normalized and "켜" in normalized:
        values["event_alerts"] = True
    if any(term in normalized for term in {"한국 마감", "한국장"}):
        if "꺼" in normalized:
            values["korea_close_reports"] = False
        elif "켜" in normalized:
            values["korea_close_reports"] = True
    if "유럽" in normalized and "마감" in normalized:
        if "꺼" in normalized:
            values["europe_close_reports"] = False
        elif "켜" in normalized:
            values["europe_close_reports"] = True
    if "미국" in normalized and any(term in normalized for term in {"개장", "장 시작", "장시작"}):
        if "꺼" in normalized:
            values["us_open_reports"] = False
        elif "켜" in normalized:
            values["us_open_reports"] = True
    if "새벽" in normalized:
        if any(term in normalized for term in {"꺼", "해제", "소리"}):
            values["overnight_silent"] = False
        elif any(term in normalized for term in {"켜", "무음", "조용"}):
            values["overnight_silent"] = True
    prefs = store.update_preferences(settings.telegram_chat_id, **values) if values else store.preferences(settings.telegram_chat_id)
    return "\n".join(
        [
            "<b>알림 설정</b>",
            f"경제지표: {'켜짐' if prefs['event_alerts'] else '꺼짐'}",
            f"긴급 뉴스: {'켜짐' if prefs['emergency_alerts'] else '꺼짐'}",
            f"한국장 마감: {'켜짐' if prefs['korea_close_reports'] else '꺼짐'}",
            f"유럽장 마감: {'켜짐·무음' if prefs['europe_close_reports'] else '꺼짐'}",
            f"미국장 개장 전: {'켜짐·조건부' if prefs['us_open_reports'] else '꺼짐'}",
            f"새벽 속보 무음: {'켜짐' if prefs['overnight_silent'] else '꺼짐'}",
            f"일시 정지: {'켜짐' if store.is_muted(settings.telegram_chat_id) else '꺼짐'}",
            "",
            "예: <i>한국 마감 알림 꺼줘</i> · <i>유럽 마감 알림 켜줘</i>",
            "<i>미국장 시작 알림 꺼줘</i> · <i>미국장 시작 알림 켜줘</i>",
            "<i>긴급 알림 꺼줘</i> · <i>새벽 무음 켜줘</i> · <i>8시간 조용히</i>",
        ]
    )


def _status(settings: Settings, store: StateStore | None) -> str:
    now = datetime.now(KST)
    next_morning = now.replace(hour=6, minute=50, second=0, microsecond=0)
    if next_morning <= now:
        next_morning += timedelta(days=1)
    lines = [
        "<b>JIN Market Pulse 상태</b>",
        f"다음 모닝: {next_morning:%m/%d %H:%M} KST",
        f"다음 한국장 마감: {next_report_time('korea_close', now):%m/%d %H:%M} KST",
        (
            "다음 유럽장 마감: "
            f"{next_report_time('europe_close', now).astimezone(KST):%m/%d %H:%M} KST · 무음"
        ),
        (
            "다음 미국장 개장 전 점검: "
            f"{next_us_open_preview_time(now).astimezone(KST):%m/%d %H:%M} KST · 조건 충족 시"
        ),
    ]
    if store is None:
        lines.append("상태 저장소: 연결 안 됨")
        return "\n".join(lines)
    latest = store.latest_saved_message("morning:")
    if latest:
        created = datetime.fromisoformat(latest["created_at"]).astimezone(KST)
        lines.append(f"마지막 모닝: {created:%m/%d %H:%M} KST")
    for report_type, label in (
        ("korea_close", "한국장 마감"),
        ("europe_close", "유럽장 마감"),
        ("us_open", "미국장 개장 전"),
    ):
        report = store.latest_report_run(report_type)
        if not report:
            continue
        updated = datetime.fromisoformat(report["updated_at"]).astimezone(KST)
        if report["status"] == "skipped":
            lines.append(
                f"최근 {label}: 건너뜀 · {html.escape(str(report['skip_reason']))}"
            )
        else:
            lines.append(f"최근 {label}: {updated:%m/%d %H:%M} KST")
    snapshot = store.latest_market_snapshot()
    if snapshot:
        captured = datetime.fromisoformat(snapshot["captured_at"]).astimezone(KST)
        lines.append(f"최근 시장 데이터: {captured:%m/%d %H:%M} KST")
    prefs = store.preferences(settings.telegram_chat_id)
    lines.extend(
        [
            f"경제지표 알림: {'켜짐' if prefs['event_alerts'] else '꺼짐'}",
            f"긴급 속보: {'켜짐' if prefs['emergency_alerts'] else '꺼짐'}",
            f"한국장 마감: {'켜짐' if prefs['korea_close_reports'] else '꺼짐'}",
            f"유럽장 마감: {'켜짐·무음' if prefs['europe_close_reports'] else '꺼짐'}",
            f"미국장 개장 전: {'켜짐·조건부' if prefs['us_open_reports'] else '꺼짐'}",
            f"가격 알림: {len(store.list_price_alerts(settings.telegram_chat_id))}개",
        ]
    )
    usage = store.usage_summary()
    used = sum(usage.values())
    lines.append(f"AI 질문 잔여: {max(settings.ai_advisor_daily_limit - used, 0)}회")
    unhealthy = [
        item
        for item in store.provider_health()
        if int(item["consecutive_failures"]) >= 3
    ]
    lines.append(f"데이터 공급원: {'일부 장애' if unhealthy else '정상'}")
    scan = store.runtime_state("breaking_scan")
    if scan.get("checked_at"):
        checked = datetime.fromisoformat(scan["checked_at"]).astimezone(KST)
        lines.append(f"마지막 속보 검사: {checked:%m/%d %H:%M} KST")
    return "\n".join(lines)


def _brief(store: StateStore | None) -> str:
    if store is None:
        return "저장된 모닝 리포트가 없습니다."
    saved = store.latest_saved_message("morning:")
    return saved["text"] if saved else "저장된 모닝 리포트가 없습니다."


def _last(store: StateStore | None, report_type: str = "") -> str:
    if store is None:
        return "저장된 최근 메시지가 없습니다."
    if report_type in {"morning", "korea_close", "europe_close", "us_open"}:
        saved = store.latest_saved_message(f"{report_type}:")
        return saved["text"] if saved else "해당 리포트가 아직 저장되지 않았습니다."
    saved = store.latest_saved_message("")
    return saved["text"] if saved else "저장된 최근 메시지가 없습니다."


def _last_response(text: str, store: StateStore | None) -> BotResponse:
    normalized = _normalized(text)
    report_type = ""
    if "korea_close" in normalized or "한국" in normalized:
        report_type = "korea_close"
    elif "europe_close" in normalized or "유럽" in normalized:
        report_type = "europe_close"
    elif "us_open" in normalized or ("미국" in normalized and "개장" in normalized):
        report_type = "us_open"
    elif "morning" in normalized or "모닝" in normalized:
        report_type = "morning"
    return BotResponse(
        _last(store, report_type),
        reply_markup={
            "inline_keyboard": [
                [
                    {"text": "모닝", "callback_data": "last:morning"},
                    {"text": "한국 마감", "callback_data": "last:korea_close"},
                    {"text": "유럽 마감", "callback_data": "last:europe_close"},
                ],
                [{"text": "미국 개장 전", "callback_data": "last:us_open"}],
            ]
        },
    )


def _candidate_response(candidates: list[str]) -> BotResponse:
    buttons = [
        {
            "text": ASSET_DEFINITIONS[key]["name"],
            "callback_data": f"price:{key}",
        }
        for key in candidates[:2]
    ]
    return BotResponse(
        "어느 자산을 뜻하는지 골라주세요.",
        reply_markup={"inline_keyboard": [buttons]},
    )


def _follow_up(
    text: str,
    settings: Settings,
    store: StateStore,
    context: dict[str, Any],
) -> str:
    normalized = _normalized(text)
    asset_key = str(context.get("asset_key") or "")
    if "숫자만" in normalized and asset_key:
        quote = fetch_asset_quote(asset_key, settings)
        return _quote_html(quote, compact=True)
    if "쉽게" in normalized:
        topic = str(context.get("topic") or "")
        if topic:
            return render_topic_explanation(topic, simple=True)
    if "그럼" in normalized and asset_key:
        if "비트" in normalized:
            return _current_move_or_limit(text, "btc", settings, store)
        return _current_move_or_limit(text, asset_key, settings, store)
    if "아까" in normalized:
        return _last(store)
    if "그 뉴스" in normalized and (asset_key or "비트" in normalized):
        return _current_move_or_limit(
            text,
            "btc" if "비트" in normalized else asset_key,
            settings,
            store,
        )
    return _help()


def handle_market_query(
    text: str,
    settings: Settings,
    store: StateStore | None = None,
) -> BotResponse:
    context = store.get_chat_context(settings.telegram_chat_id) if store else {}
    route = route_query(text, context)
    topics = matched_topics(_normalized(text))

    if route.candidates:
        return _candidate_response(route.candidates)
    if route.intent == "start":
        return BotResponse(_start(), REMOVE_KEYBOARD)
    if route.intent == "menu":
        return BotResponse(
            "빠른 버튼을 열었습니다. 한 번 사용하면 자동으로 접힙니다.",
            MAIN_KEYBOARD,
        )
    if route.intent == "help":
        return BotResponse(_help(), REMOVE_KEYBOARD)
    if route.intent == "brief":
        return BotResponse(_brief(store), REMOVE_KEYBOARD)
    if route.intent == "last":
        return _last_response(text, store)
    if route.intent == "status":
        return BotResponse(_status(settings, store), REMOVE_KEYBOARD)
    if route.intent == "settings":
        return BotResponse(_settings(text, settings, store), REMOVE_KEYBOARD)
    if route.intent == "mute":
        return BotResponse(_mute(text, settings, store), REMOVE_KEYBOARD)
    if route.intent == "reset":
        if store:
            store.reset_chat_context(settings.telegram_chat_id)
        return BotResponse(
            "최근 대화 연결만 초기화했습니다.\n"
            "가격 알림, 알림 설정, 저장된 리포트와 시장 데이터는 그대로입니다.",
            REMOVE_KEYBOARD,
        )
    if route.intent == "alerts":
        return BotResponse(_alerts(text, settings, store), REMOVE_KEYBOARD)
    if route.intent == "alert_create":
        return BotResponse(
            _alert_create(text, route.asset_keys, settings, store),
            REMOVE_KEYBOARD,
        )
    if route.intent == "calendar":
        result = _calendar(settings, route.period or "24h", store)
    elif route.intent == "markets":
        result = _markets(settings)
    elif route.intent == "compare":
        result = _compare(route.asset_keys, settings)
    elif route.intent == "current_cause" and route.asset_keys:
        result = _current_move_or_limit(text, route.asset_keys[0], settings, store)
    elif route.intent == "price" and route.asset_keys:
        result = _live_quote(
            route.asset_keys[0],
            settings,
            period=route.period,
            original_text=text,
        )
    elif route.intent == "definition" and topics:
        result = render_topic_explanation(topics[0])
    elif route.intent == "relation":
        result = _advisor_or_limit(text, settings, store)
    elif route.intent == "advice":
        result = _advice_refusal(settings)
    elif route.intent == "follow_up" and store:
        result = _follow_up(text, settings, store, context)
    else:
        return BotResponse(
            "질문을 정확히 이해하지 못했습니다.\n"
            "예: <i>이더 얼마야</i> · <i>코스피 왜 떨어져?</i> · <i>이번 주 일정</i>",
            REMOVE_KEYBOARD,
        )

    if store:
        new_context: dict[str, Any] = {
            "last_question": text[:500],
            "last_intent": route.intent,
            "last_response": result[:3000],
        }
        if route.asset_keys:
            new_context["asset_key"] = route.asset_keys[0]
        if topics:
            new_context["topic"] = topics[0]
        store.set_chat_context(settings.telegram_chat_id, new_context)
    return BotResponse(result, REMOVE_KEYBOARD)


def answer_market_query(
    text: str,
    settings: Settings,
    store: StateStore | None = None,
) -> str:
    return handle_market_query(text, settings, store).text
