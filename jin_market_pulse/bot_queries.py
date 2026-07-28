from __future__ import annotations

import html
import logging
import re

from .advisor import (
    create_advisor_answer,
    matched_topics,
    render_topic_explanation,
)
from .calendar import fetch_economic_events
from .config import KST, Settings
from .models import AssetQuote, EconomicEvent, MarketData
from .providers import fetch_asset_quote, fetch_market_quotes
from .reports import format_quote, select_future_events
from .state import StateStore


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
PRICE_TERMS = {
    "가격",
    "얼마",
    "시세",
    "변동",
    "현재가",
}
EXPLAIN_TERMS = {
    "뭐야",
    "무엇",
    "뜻",
    "설명",
    "알려줘",
    "왜 중요",
}
ADVICE_TERMS = {
    "전망",
    "예측",
    "오를까",
    "내릴까",
    "매수",
    "매도",
    "살까",
    "팔까",
    "사도",
    "팔아도",
    "들어가도",
}
RELATION_TERMS = {"관계", "영향", "연결", "왜", "오르면", "내리면"}
CURRENT_TERMS = {"지금", "오늘", "현재", "방금"}
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
            "<b>JIN 시장 상담 사용법</b>",
            "- 비트 얼마야",
            "- DXY가 뭐야?",
            "- 금리가 Nasdaq에 왜 중요해?",
            "- ETH 변동",
            "- /price gold",
            "- /markets",
            "- /calendar",
            "",
            "가격·일정·시장 개념을 물어볼 수 있습니다.",
        ]
    )


def _live_quote(key: str, settings: Settings) -> str:
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
    except Exception:
        logging.warning("Single asset query failed for %s.", key, exc_info=True)
        return "<b>해당 자산의 최신 유효값이 없습니다.</b>"


def _advisor_or_limit(
    text: str,
    settings: Settings,
    store: StateStore | None,
) -> str:
    if not settings.enable_ai_advisor or store is None:
        return "<b>AI 시장 설명이 현재 비활성화되어 있습니다.</b>"
    if not store.claim_ai_advisor_slot(settings.ai_advisor_daily_limit):
        return "\n".join(
            [
                "<b>오늘의 AI 설명 횟수를 모두 사용했습니다.</b>",
                "가격·일정·기본 용어 설명은 계속 이용할 수 있습니다.",
            ]
        )
    try:
        return create_advisor_answer(text, settings)
    except Exception:
        store.release_ai_advisor_slot()
        logging.exception("AI advisor request failed.")
        return "<b>AI 설명을 불러오지 못했습니다. 잠시 후 다시 시도해주세요.</b>"


def answer_market_query(
    text: str,
    settings: Settings,
    store: StateStore | None = None,
) -> str:
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

    key = _asset_key(normalized)
    topics = matched_topics(normalized)
    wants_price = normalized.startswith("/price") or any(
        term in normalized for term in PRICE_TERMS
    )
    wants_explanation = any(term in normalized for term in EXPLAIN_TERMS)
    asks_relation = any(term in normalized for term in RELATION_TERMS)

    if key and wants_price:
        return _live_quote(key, settings)

    if any(term in normalized for term in ADVICE_TERMS):
        return "\n".join(
            [
                "<b>매수·매도 결정과 가격 예측은 제공하지 않습니다.</b>",
                "대신 현재 가격, 지표 뜻, 자산 간 일반적인 관계를 설명할 수 있습니다.",
            ]
        )

    if "왜" in normalized and any(
        term in normalized for term in CURRENT_TERMS
    ):
        return "\n".join(
            [
                "<b>현재 움직임의 원인은 단정하지 않습니다.</b>",
                "실시간 뉴스와 여러 자산의 반응을 함께 검증해야 하기 때문입니다.",
                "현재 숫자는 /markets에서 확인할 수 있습니다.",
            ]
        )

    if len(topics) == 1 and wants_explanation and not asks_relation:
        return render_topic_explanation(topics[0])

    if asks_relation and (
        topics or any(term in normalized for term in MARKET_TERMS)
    ):
        return _advisor_or_limit(text, settings, store)

    if len(topics) == 1 and wants_explanation:
        return render_topic_explanation(topics[0])

    if key:
        return _live_quote(key, settings)

    if topics:
        return render_topic_explanation(topics[0])

    if any(term in normalized for term in MARKET_TERMS):
        return _advisor_or_limit(text, settings, store)

    return _help()
