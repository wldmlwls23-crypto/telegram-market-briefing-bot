from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from openai import OpenAI

from .config import KST, Settings
from .models import (
    AssetQuote,
    EconomicEvent,
    EmergencyAnalysis,
    MarketData,
    MorningAnalysis,
    NewsItem,
)


FORBIDDEN_PHRASES = {
    "확인 필요",
    "매수해도 된다",
    "숏을 봐야 한다",
    "롱 진입",
    "손절",
    "대응해야 한다",
    "오를 가능성이 높다",
    "내릴 가능성이 높다",
    "강력 추천",
}
SNAPSHOT_ORDER = [
    "btc",
    "eth",
    "nasdaq100",
    "sp500",
    "dxy",
    "us2y",
    "us10y",
    "kospi",
    "kosdaq",
    "usdkrw",
    "wti",
    "gold",
]


def _clean_ai_text(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text.replace("|", " ")).strip()
    for phrase in FORBIDDEN_PHRASES:
        cleaned = cleaned.replace(phrase, "")
    return cleaned.strip(" -")


def _contains_generated_number(analysis: MorningAnalysis) -> bool:
    values = [
        analysis.headline,
        analysis.cross_asset_chain,
        *[signal.title_ko for signal in analysis.signals],
        *[signal.meaning for signal in analysis.signals],
    ]
    return any(re.search(r"\d", value) for value in values)


def _quote_payload(quote: AssetQuote) -> dict[str, object]:
    return {
        "key": quote.key,
        "name_ko": quote.name_ko,
        "kind": quote.kind,
        "direction": (
            "상승"
            if (quote.absolute_change or 0) > 0
            else "하락"
            if (quote.absolute_change or 0) < 0
            else "보합"
        ),
        "source": quote.source,
        "as_of_kst": quote.as_of.astimezone(KST).strftime("%Y-%m-%d %H:%M"),
    }


def _analysis_input(data: MarketData) -> str:
    payload = {
        "generated_at_kst": data.generated_at_kst.isoformat(),
        "asset_directions": [_quote_payload(quote) for quote in data.quotes.values()],
        "news_candidates": [
            {
                "candidate_id": item.news_id,
                "title": item.title,
                "publisher": item.publisher,
                "published_at": item.published_at.isoformat() if item.published_at else "",
                "summary": item.summary,
            }
            for item in data.news[:16]
        ],
        "future_events": [
            {
                "event_id": event.event_id,
                "title_ko": event.title_ko,
                "country_ko": event.country_ko,
                "importance": event.importance,
            }
            for event in data.events[:8]
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def create_morning_analysis(data: MarketData, settings: Settings) -> MorningAnalysis:
    client = OpenAI(api_key=settings.openai_api_key)
    instructions = """
당신은 한국 개인투자자를 위한 관찰형 시장 편집자입니다.
입력에 있는 후보와 자산 방향만 사용해 아침에 볼 핵심을 선별하세요.

반드시 지킬 규칙:
- 뉴스 후보는 최대 3개만 선택하고 candidate_id를 그대로 반환합니다.
- title_ko는 선택한 영어 제목의 짧고 정확한 한국어 번역이어야 합니다.
- meaning은 왜 달러, 미국채, Nasdaq, BTC에 중요한지만 쉬운 한국어로 설명합니다.
- 원자료에 없는 원인, 결과, 수급, 가격, 발언을 만들지 않습니다.
- 출력 텍스트에는 숫자, 날짜, 시간, 별표, 표, 파이프 문자를 쓰지 않습니다.
- 자산이 함께 움직였다는 사실과 인과관계를 구분합니다.
- 매수, 매도, 롱, 숏, 손절, 가격 전망을 쓰지 않습니다.
- future_events에서 오늘 중요한 event_id만 고릅니다.
- priority_asset_keys는 입력에 존재하는 key만 사용합니다.
- 중요한 뉴스가 없으면 signals는 빈 배열로 둡니다.
""".strip()
    request: dict[str, object] = {
        "model": settings.openai_model,
        "instructions": instructions,
        "input": _analysis_input(data),
        "text_format": MorningAnalysis,
        "reasoning": {"effort": settings.openai_reasoning_effort},
        "store": False,
    }
    if settings.openai_web_search:
        request["tools"] = [{"type": "web_search"}]
    try:
        response = client.responses.parse(**request)
    except Exception:
        if "tools" not in request:
            raise
        logging.warning("OpenAI web search path failed; retrying structured analysis without it.")
        request.pop("tools", None)
        response = client.responses.parse(**request)
    analysis = response.output_parsed
    if analysis is None:
        raise RuntimeError("OpenAI returned no structured MorningAnalysis")
    if _contains_generated_number(analysis):
        raise RuntimeError("OpenAI analysis contained a number not allowed by the output contract")
    return _validated_analysis(analysis, data)


def _validated_analysis(analysis: MorningAnalysis, data: MarketData) -> MorningAnalysis:
    news_ids = {item.news_id for item in data.news}
    event_ids = {event.event_id for event in data.events}
    asset_keys = set(data.quotes)
    analysis.headline = _clean_ai_text(analysis.headline)
    analysis.cross_asset_chain = _clean_ai_text(analysis.cross_asset_chain)
    analysis.signals = [
        signal
        for signal in analysis.signals
        if signal.candidate_id in news_ids
    ][:3]
    for signal in analysis.signals:
        signal.title_ko = _clean_ai_text(signal.title_ko)
        signal.meaning = _clean_ai_text(signal.meaning)
    analysis.sensitivities = [
        item for item in analysis.sensitivities if item.event_id in event_ids
    ][:2]
    analysis.priority_event_ids = [
        event_id for event_id in analysis.priority_event_ids if event_id in event_ids
    ][:3]
    analysis.priority_asset_keys = [
        key for key in analysis.priority_asset_keys if key in asset_keys
    ][:3]
    return analysis


def fallback_morning_analysis(data: MarketData) -> MorningAnalysis:
    dxy = data.quotes.get("dxy")
    rates = data.quotes.get("us10y") or data.quotes.get("us2y")
    nasdaq = data.quotes.get("nasdaq100")
    btc = data.quotes.get("btc")

    def direction(quote: AssetQuote | None) -> str:
        if not quote or quote.absolute_change is None:
            return "변화 제한"
        return "상승" if quote.absolute_change > 0 else "하락" if quote.absolute_change < 0 else "보합"

    headline = (
        f"달러는 {direction(dxy)}, 미국채 금리는 {direction(rates)}, "
        f"Nasdaq은 {direction(nasdaq)}, BTC는 {direction(btc)} 흐름입니다."
    )
    chain = "달러와 금리 변화가 기술주와 BTC에 같은 압력으로 이어지는지 관찰합니다."
    return MorningAnalysis(
        headline=headline,
        signals=[],
        cross_asset_chain=chain,
        sensitivities=[],
        priority_event_ids=[event.event_id for event in data.events[:3]],
        priority_asset_keys=[key for key in ("dxy", "us10y", "nasdaq100", "btc") if key in data.quotes][
            :3
        ],
    )


def _format_number(value: float, quote: AssetQuote) -> str:
    if quote.kind == "crypto":
        decimals = 0 if value >= 1000 else 2
        return f"${value:,.{decimals}f}"
    if quote.kind == "yield":
        return f"{value:.2f}%"
    if quote.key == "usdkrw":
        return f"{value:,.2f}원"
    if quote.kind == "commodity":
        return f"${value:,.2f}"
    if quote.key == "dxy":
        return f"{value:,.2f}"
    return f"{value:,.2f}"


def format_quote(quote: AssetQuote) -> str:
    current = _format_number(quote.current, quote)
    as_of = quote.as_of.astimezone(KST).strftime("%m/%d %H:%M")
    if quote.previous is None or quote.absolute_change is None:
        return f"- {quote.name_ko}: {current} / {as_of} KST"
    previous = _format_number(quote.previous, quote)
    if quote.kind == "yield":
        change = quote.absolute_change * 100
        return (
            f"- {quote.name_ko}: {current} / {quote.comparison_label} {previous} "
            f"→ {change:+.1f}bp / {as_of} KST"
        )
    percent = quote.percent_change
    percent_text = f"{percent:+.2f}%" if percent is not None else ""
    return (
        f"- {quote.name_ko}: {current} / {quote.comparison_label} {previous} "
        f"→ {percent_text} / {as_of} KST"
    )


def _event_by_id(events: list[EconomicEvent]) -> dict[str, EconomicEvent]:
    return {event.event_id: event for event in events}


def _news_by_id(news: list[NewsItem]) -> dict[str, NewsItem]:
    return {item.news_id: item for item in news}


def render_morning_report(data: MarketData, analysis: MorningAnalysis) -> str:
    event_map = _event_by_id(data.events)
    news_map = _news_by_id(data.news)
    lines = [
        "# Morning Market Report",
        data.generated_at_kst.strftime("%Y-%m-%d %H:%M KST"),
        "",
        analysis.headline,
        "",
        "## 0. [Current Asset Snapshot]",
    ]
    for key in SNAPSHOT_ORDER:
        quote = data.quotes.get(key)
        if quote:
            lines.append(format_quote(quote))
    sources = ", ".join(sorted({quote.source for quote in data.quotes.values()}))
    lines.extend(["", f"데이터 출처: {sources}", "", "## 1. [Signal vs Noise]"])
    if analysis.signals:
        for signal in analysis.signals:
            news = news_map[signal.candidate_id]
            lines.extend(
                [
                    f"- {signal.title_ko}",
                    f"  의미: {signal.meaning}",
                    f"  출처: {news.publisher}",
                ]
            )
    else:
        lines.append("- 가격 흐름을 넘어설 만큼 검증된 신규 핵심 뉴스는 제한적입니다.")

    lines.extend(["", "## 2. [Economic Calendar]"])
    future_events = [
        event
        for event in data.events
        if event.event_time_kst >= data.generated_at_kst
        and event.event_time_kst.astimezone(KST).date()
        == data.generated_at_kst.astimezone(KST).date()
    ][:5]
    future_event_map = _event_by_id(future_events)
    if not future_events:
        lines.append("- 현재 시각 이후 중요도 높은 일정이 없습니다.")
    for event in future_events:
        lines.append(
            f"- {event.event_time_kst.strftime('%m/%d %H:%M')} KST / "
            f"{event.country_ko} / {event.title_ko} / {event.importance}"
        )
        if event.value_summary:
            lines.append(f"  값: {event.value_summary}")
        lines.append(f"  의미: {event.sensitivity_stronger}")

    lines.extend(["", "## 3. [Market Pulse]", f"- {analysis.cross_asset_chain}"])
    lines.extend(["", "## 4. [Indicator Sensitivity]"])
    selected_sensitivity_ids = [item.event_id for item in analysis.sensitivities]
    if not selected_sensitivity_ids:
        selected_sensitivity_ids = [event.event_id for event in future_events[:2]]
    selected_events = [
        future_event_map[event_id]
        for event_id in selected_sensitivity_ids
        if event_id in future_event_map
    ][:2]
    if not selected_events:
        lines.append("- 오늘 직접 연결할 중요 지표 시나리오가 없습니다.")
    for event in selected_events:
        lines.extend(
            [
                f"- {event.title_ko}",
                f"  강하게 나오면: {event.sensitivity_stronger}",
                f"  약하게 나오면: {event.sensitivity_weaker}",
            ]
        )

    lines.extend(["", "## 5. [Today's Priority]"])
    priority_labels: list[str] = []
    for event_id in analysis.priority_event_ids:
        if event_id in future_event_map:
            priority_labels.append(future_event_map[event_id].title_ko)
    for key in analysis.priority_asset_keys:
        if key in data.quotes:
            priority_labels.append(data.quotes[key].name_ko)
    if not priority_labels:
        priority_labels = [
            event.title_ko for event in future_events[:2]
        ] + [
            data.quotes[key].name_ko
            for key in ("dxy", "us10y", "nasdaq100")
            if key in data.quotes
        ]
    for index, label in enumerate(dict.fromkeys(priority_labels), start=1):
        if index > 3:
            break
        lines.append(f"{index}. {label}")
    first_priority = next(iter(dict.fromkeys(priority_labels)), "달러와 금리 흐름")
    lines.extend(
        [
            "",
            f"오늘 핵심은 {first_priority} → DXY → 미국채 금리 → Nasdaq → BTC 순서로 관찰.",
        ]
    )
    result = "\n".join(lines).strip()
    validate_rendered_report(result)
    return result


def validate_rendered_report(text: str) -> None:
    for phrase in FORBIDDEN_PHRASES:
        if phrase in text:
            raise ValueError(f"Forbidden phrase in report: {phrase}")
    if "|" in text:
        raise ValueError("Pipe character is forbidden in Telegram report")
    if "N/A" in text:
        raise ValueError("N/A is forbidden in Telegram report")
    required = [
        "## 0. [Current Asset Snapshot]",
        "## 1. [Signal vs Noise]",
        "## 2. [Economic Calendar]",
        "## 3. [Market Pulse]",
        "## 4. [Indicator Sensitivity]",
        "## 5. [Today's Priority]",
    ]
    missing = [heading for heading in required if heading not in text]
    if missing:
        raise ValueError(f"Missing required report sections: {missing}")


def create_emergency_analysis(
    news_group: list[NewsItem],
    quotes: dict[str, AssetQuote],
    settings: Settings,
) -> EmergencyAnalysis:
    client = OpenAI(api_key=settings.openai_api_key)
    payload = {
        "news": [item.model_dump(mode="json") for item in news_group[:3]],
        "market_directions": [_quote_payload(quote) for quote in quotes.values()],
    }
    request: dict[str, object] = {
        "model": settings.openai_model,
        "instructions": (
            "신뢰 가능한 출처가 같은 사건을 보도하는지 검토하세요. "
            "확인된 사실만 한국어로 요약하고 가격 숫자는 쓰지 마세요. "
            "입력 news_id만 source_news_ids에 넣으세요. 루머면 verified=false입니다. "
            "매매 지시와 가격 전망은 금지합니다."
        ),
        "input": json.dumps(payload, ensure_ascii=False),
        "text_format": EmergencyAnalysis,
        "reasoning": {"effort": settings.openai_reasoning_effort},
        "store": False,
    }
    if settings.openai_web_search:
        request["tools"] = [{"type": "web_search"}]
    response = client.responses.parse(**request)
    analysis = response.output_parsed
    if analysis is None:
        raise RuntimeError("OpenAI returned no structured EmergencyAnalysis")
    allowed_ids = {item.news_id for item in news_group}
    analysis.source_news_ids = [
        news_id for news_id in analysis.source_news_ids if news_id in allowed_ids
    ]
    analysis.summary_ko = _clean_ai_text(analysis.summary_ko)
    analysis.meaning = _clean_ai_text(analysis.meaning)
    if re.search(r"\d", f"{analysis.summary_ko} {analysis.meaning}"):
        raise RuntimeError("Emergency analysis contained an unverified number")
    if not analysis.source_news_ids:
        analysis.verified = False
    return analysis


def render_emergency_alert(
    analysis: EmergencyAnalysis,
    news_group: list[NewsItem],
    quotes: dict[str, AssetQuote],
) -> str:
    news_map = _news_by_id(news_group)
    lines = [
        "[긴급 시장 알림 / ★★★★★]",
        "",
        "핵심:",
        f"- {analysis.summary_ko}",
        "",
        "시장 반응:",
    ]
    for key in ("dxy", "us10y", "nasdaq100", "btc"):
        quote = quotes.get(key)
        if quote:
            lines.append(format_quote(quote))
    lines.extend(["", "의미:", f"- {analysis.meaning}", "", "출처:"])
    for news_id in analysis.source_news_ids:
        item = news_map.get(news_id)
        if item:
            lines.append(f"- {item.publisher}")
    return "\n".join(lines).strip()


def render_data_health_alert(missing: list[str], errors: list[str]) -> str:
    lines = [
        "[시장 데이터 점검 알림]",
        "",
        "핵심 데이터가 부족해 품질이 낮은 모닝 리포트는 보내지 않았습니다.",
        "",
        "누락:",
        *[f"- {item}" for item in missing],
    ]
    if errors:
        lines.extend(["", "공급원 오류:", *[f"- {error[:180]}" for error in errors[:4]]])
    return "\n".join(lines)
