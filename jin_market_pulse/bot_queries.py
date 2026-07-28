from __future__ import annotations

import html
import re

from .calendar import fetch_economic_events
from .config import KST, Settings
from .models import AssetQuote, EconomicEvent, MarketData
from .providers import fetch_asset_quote, fetch_market_quotes
from .reports import format_quote, select_future_events


ASSET_ALIASES = {
    "btc": {"btc", "비트", "비트코인", "bitcoin"},
    "eth": {"eth", "이더", "이더리움", "ethereum"},
    "sp500": {"s&p500", "s&p 500", "sp500", "에스앤피", "snp"},
    "nasdaq100": {"nasdaq", "nasdaq100", "나스닥", "나스닥100", "ndx"},
    "dow": {"dow", "다우", "다우존스"},
    "dxy": {"dxy", "달러인덱스", "달러 지수"},
    "us2y": {"미국채2년", "미국채 2년", "미2년", "2년물", "us2y"},
    "us10y": {"미국채10년", "미국채 10년", "미10년", "10년물", "us10y"},
    "kospi": {"kospi", "코스피"},
    "kosdaq": {"kosdaq", "코스닥"},
    "usdkrw": {"원달러", "원/달러", "환율", "usdkrw"},
    "wti": {"wti", "유가", "원유", "서부텍사스유"},
    "gold": {"gold", "금값", "금 가격", "금시세"},
}
SUPPORTED_ORDER = (
    "btc",
    "eth",
    "sp500",
    "nasdaq100",
    "dow",
    "dxy",
    "us2y",
    "us10y",
    "kospi",
    "kosdaq",
    "usdkrw",
    "wti",
    "gold",
)
NON_NUMERIC_TERMS = {
    "왜",
    "전망",
    "예측",
    "오를까",
    "내릴까",
    "매수",
    "매도",
    "살까",
    "팔까",
}


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def _asset_key(text: str) -> str | None:
    normalized = _normalized(text)
    for key, aliases in ASSET_ALIASES.items():
        if any(alias in normalized for alias in sorted(aliases, key=len, reverse=True)):
            return key
    return None


def _quote_html(quote: AssetQuote) -> str:
    return html.escape(format_quote(quote).removeprefix("- "))


def _calendar_html(events: list[EconomicEvent]) -> str:
    lines = ["<b>앞으로 24시간 주요 일정</b>"]
    if not events:
        lines.append("주요 일정 없음")
        return "\n".join(lines)
    for event in events:
        lines.append(
            f"- {event.event_time_kst:%m/%d %H:%M} "
            f"<b>{html.escape(event.title_ko)}</b>"
        )
        values = []
        if event.forecast:
            values.append(f"예상 {event.forecast}")
        if event.previous:
            values.append(f"이전 {event.previous}")
        if values:
            lines.append("  " + html.escape(" · ".join(values)))
    return "\n".join(lines)


def _help() -> str:
    return "\n".join(
        [
            "<b>숫자 조회 사용법</b>",
            "- 비트 얼마야",
            "- ETH 변동",
            "- /price gold",
            "- /markets",
            "- /calendar",
            "",
            "가격·변동·일정만 조회할 수 있습니다.",
        ]
    )


def answer_market_query(text: str, settings: Settings) -> str:
    normalized = _normalized(text)
    if normalized.startswith("/calendar") or "경제일정" in normalized or normalized == "일정":
        from datetime import datetime

        events = fetch_economic_events(settings, days_ahead=2)
        data = MarketData(
            generated_at_kst=datetime.now(KST),
            quotes={},
            events=events,
            news=[],
        )
        return _calendar_html(select_future_events(data))

    if normalized.startswith("/markets") or normalized in {"전체 시장", "전체시세"}:
        quotes, _ = fetch_market_quotes(settings)
        lines = ["<b>지원 자산 현재값</b>"]
        lines.extend(
            _quote_html(quotes[key])
            for key in SUPPORTED_ORDER
            if key in quotes
        )
        lines.append(f"\n<i>조회 {__import__('datetime').datetime.now(KST):%m/%d %H:%M} KST</i>")
        return "\n".join(lines)

    if any(term in normalized for term in NON_NUMERIC_TERMS):
        return _help()

    key = _asset_key(normalized)
    if key:
        try:
            quote = fetch_asset_quote(key, settings)
            return "\n".join(
                [
                    f"<b>{html.escape(quote.name_ko)}</b>",
                    _quote_html(quote),
                    f"<i>기준 {quote.as_of.astimezone(KST):%m/%d %H:%M} KST · "
                    f"{html.escape(quote.source)}</i>",
                ]
            )
        except (KeyError, RuntimeError, ValueError):
            pass
        return "<b>해당 자산의 최신 유효값이 없습니다.</b>"

    return _help()
