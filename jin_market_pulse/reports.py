from __future__ import annotations

import html
import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

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


UTC = timezone.utc
REPORT_LIMIT = 2000
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
CORE_ASSET_KEYS = ("btc", "nasdaq100", "dxy", "us10y", "kospi", "wti")
EXTRA_THRESHOLDS = {
    "eth": 3.0,
    "sp500": 1.5,
    "dow": 1.5,
    "kosdaq": 1.5,
    "usdkrw": 0.7,
    "gold": 1.5,
}
TIER_ONE_PUBLISHERS = {
    "reuters",
    "bloomberg",
    "associated press",
    "ap news",
    "financial times",
    "the wall street journal",
    "wsj",
}
REQUIRED_HEADINGS = (
    "<b>0. 현재 시장</b>",
    "<b>1. 핵심 신호</b>",
    "<b>2. 오늘 일정</b>",
    "<b>3. 시장 연결</b>",
    "<b>4. 지표 시나리오</b>",
    "<b>5. 오늘 관찰 순서</b>",
)


def _short(text: str, limit: int) -> str:
    cleaned = re.sub(r"\s+", " ", text.replace("|", " ").replace("#", " ")).strip()
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1].rstrip() + "…"


def _clean_ai_text(text: str) -> str:
    cleaned = _short(text, 220)
    for phrase in FORBIDDEN_PHRASES:
        cleaned = cleaned.replace(phrase, "")
    return cleaned.strip(" -")


def _contains_generated_number(analysis: MorningAnalysis) -> bool:
    values = [
        value
        for signal in analysis.signals
        for value in (signal.title_ko, signal.meaning)
    ]
    return any(re.search(r"\d", value) for value in values)


def _quote_payload(quote: AssetQuote) -> dict[str, object]:
    return {
        "key": quote.key,
        "name_ko": quote.name_ko,
        "kind": quote.kind,
        "current": quote.current,
        "previous": quote.previous,
        "percent_change": quote.percent_change,
        "absolute_change": quote.absolute_change,
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


def _publisher_is_tier_one(publisher: str) -> bool:
    normalized = publisher.lower()
    return any(name in normalized for name in TIER_ONE_PUBLISHERS)


def _story_tokens(item: NewsItem) -> set[str]:
    title = re.sub(r"\s+-\s+[^-]{2,50}$", "", item.title.lower())
    return {
        token
        for token in re.findall(r"[a-z0-9가-힣]+", title)
        if len(token) > 2
        and token not in {"the", "and", "for", "with", "from", "after"}
    }


def _same_story(left: NewsItem, right: NewsItem) -> bool:
    if left.topic_key == right.topic_key:
        return True
    left_tokens = _story_tokens(left)
    right_tokens = _story_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    return overlap / union >= 0.55


def qualified_morning_news(data: MarketData) -> list[NewsItem]:
    cutoff = data.generated_at_kst.astimezone(UTC) - timedelta(hours=24)
    recent = [
        item
        for item in data.news
        if item.published_at is not None
        and item.published_at.astimezone(UTC) >= cutoff
    ]
    grouped: dict[str, list[NewsItem]] = defaultdict(list)
    for item in recent:
        grouped[item.topic_key].append(item)

    qualified: list[NewsItem] = []
    for item in recent:
        publishers = {
            candidate.publisher.lower() for candidate in grouped[item.topic_key]
        }
        if (
            item.official_source
            or _publisher_is_tier_one(item.publisher)
            or len(publishers) >= 2
        ):
            qualified.append(item)
    return qualified[:16]


def _analysis_input(data: MarketData) -> str:
    payload = {
        "generated_at_kst": data.generated_at_kst.isoformat(),
        "assets": [_quote_payload(quote) for quote in data.quotes.values()],
        "news_candidates": [
            {
                "candidate_id": item.news_id,
                "topic_key": item.topic_key,
                "title": item.title,
                "publisher": item.publisher,
                "published_at": item.published_at.isoformat()
                if item.published_at
                else "",
                "summary": item.summary,
            }
            for item in qualified_morning_news(data)
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def create_morning_analysis(data: MarketData, settings: Settings) -> MorningAnalysis:
    client = OpenAI(api_key=settings.openai_api_key)
    request: dict[str, object] = {
        "model": settings.openai_model,
        "instructions": """
당신은 한국 개인투자자를 위한 시장 뉴스 편집자입니다.
검증된 뉴스 후보 중 실제 핵심 자산 움직임을 이해하는 데 필요한 뉴스만 최대 두 건 고릅니다.

규칙:
- candidate_id는 입력 값을 그대로 사용합니다.
- 같은 topic_key 사건은 한 건만 고릅니다.
- title_ko는 짧고 정확한 한국어 번역으로 씁니다.
- meaning은 왜 관련 자산에 중요한지 한 문장으로 씁니다.
- related_asset_keys에는 입력에 존재하는 자산 key만 최대 세 개 넣습니다.
- 확인된 직접 원인이 아니면 relation을 '시장 배경'으로 둡니다.
- 자산 반응이 뉴스와 반대면 relation을 '엇갈림'으로 둡니다.
- 뉴스가 가격 움직임을 직접 설명한다고 신뢰할 때만 '원인 후보'로 둡니다.
- 출력 문장에는 숫자, 날짜, 시간, 별표, 표, 파이프 문자를 쓰지 않습니다.
- 입력에 없는 사실, 원인, 수급, 발언을 만들지 않습니다.
- 매수·매도·전망·가격 목표를 쓰지 않습니다.
- 중요 뉴스가 없으면 signals를 빈 배열로 둡니다.
""".strip(),
        "input": _analysis_input(data),
        "text_format": MorningAnalysis,
        "reasoning": {"effort": settings.openai_reasoning_effort},
        "max_output_tokens": settings.openai_max_output_tokens,
        "store": False,
    }
    if settings.openai_web_search:
        request["tools"] = [{"type": "web_search", "search_context_size": "low"}]
    try:
        response = client.responses.parse(**request)
    except Exception:
        if "tools" not in request:
            raise
        logging.warning("OpenAI web search path failed; retrying without it.")
        request.pop("tools", None)
        response = client.responses.parse(**request)
    analysis = response.output_parsed
    if analysis is None:
        raise RuntimeError("OpenAI returned no structured MorningAnalysis")
    if _contains_generated_number(analysis):
        raise RuntimeError("OpenAI analysis contained a generated number")
    return _validated_analysis(analysis, data)


def _validated_analysis(
    analysis: MorningAnalysis,
    data: MarketData,
) -> MorningAnalysis:
    news_map = {item.news_id: item for item in qualified_morning_news(data)}
    seen_news: list[NewsItem] = []
    validated = []
    for signal in analysis.signals:
        news = news_map.get(signal.candidate_id)
        if not news or any(_same_story(news, seen) for seen in seen_news):
            continue
        related = [
            key for key in signal.related_asset_keys if key in data.quotes
        ][:3]
        if not related:
            continue
        signal.title_ko = _short(_clean_ai_text(signal.title_ko), 55)
        signal.meaning = _short(_clean_ai_text(signal.meaning), 105)
        signal.related_asset_keys = related
        seen_news.append(news)
        validated.append(signal)
        if len(validated) == 2:
            break
    return MorningAnalysis(signals=validated)


def fallback_morning_analysis(data: MarketData) -> MorningAnalysis:
    return MorningAnalysis(signals=[])


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


def _movement(quote: AssetQuote) -> tuple[str, str]:
    change = quote.absolute_change
    arrow = "▲" if (change or 0) > 0 else "▼" if (change or 0) < 0 else "•"
    if quote.kind == "yield" and change is not None:
        return arrow, f"{abs(change * 100):.1f}bp"
    if quote.percent_change is not None:
        return arrow, f"{abs(quote.percent_change):.2f}%"
    return arrow, ""


def format_quote(quote: AssetQuote) -> str:
    current = _format_number(quote.current, quote)
    arrow, movement = _movement(quote)
    if quote.previous is not None and movement:
        previous = _format_number(quote.previous, quote)
        return (
            f"- {quote.name_ko}: {current} / {quote.comparison_label} "
            f"{previous} → {arrow}{movement}"
        )
    return f"- {quote.name_ko}: {current} · {quote.as_of.astimezone(KST):%m/%d %H:%M} KST"


def _format_quote_html(quote: AssetQuote) -> str:
    current = html.escape(_format_number(quote.current, quote))
    arrow, movement = _movement(quote)
    comparison = "24시간" if quote.key == "btc" else quote.comparison_label
    move_text = f" {arrow}{movement}" if movement else ""
    return (
        f"- {html.escape(quote.name_ko)} <b>{current}</b>{move_text}"
        f" · {html.escape(_short(comparison, 18))}"
    )


def select_report_assets(quotes: dict[str, AssetQuote]) -> list[AssetQuote]:
    selected = [quotes[key] for key in CORE_ASSET_KEYS if key in quotes]
    extras: list[tuple[float, AssetQuote]] = []
    for key, threshold in EXTRA_THRESHOLDS.items():
        quote = quotes.get(key)
        if quote and quote.percent_change is not None:
            ratio = abs(quote.percent_change) / threshold
            if ratio >= 1:
                extras.append((ratio, quote))
    if extras:
        selected.append(max(extras, key=lambda item: item[0])[1])
    return selected


def select_future_events(data: MarketData) -> list[EconomicEvent]:
    start = data.generated_at_kst
    end = start + timedelta(hours=24)
    candidates = [
        event
        for event in data.events
        if start <= event.event_time_kst <= end
    ]
    priority = sorted(
        candidates,
        key=lambda event: (
            -len(event.importance),
            event.event_time_kst,
        ),
    )[:3]
    return sorted(priority, key=lambda event: event.event_time_kst)


def _axis_text(quote: AssetQuote | None, label: str) -> str:
    if not quote:
        return ""
    arrow, movement = _movement(quote)
    return f"{label}{arrow}{movement}"


def _market_summary(quotes: dict[str, AssetQuote]) -> tuple[str, str]:
    rate = quotes.get("us10y") or quotes.get("us2y")
    pieces = [
        _axis_text(quotes.get("dxy"), "달러"),
        _axis_text(rate, "금리"),
        _axis_text(quotes.get("nasdaq100"), "Nasdaq"),
        _axis_text(quotes.get("btc"), "BTC"),
    ]
    first = " · ".join(piece for piece in pieces if piece)

    dxy_change = (quotes.get("dxy").absolute_change if quotes.get("dxy") else None)
    rate_change = rate.absolute_change if rate else None
    nasdaq_change = (
        quotes.get("nasdaq100").absolute_change if quotes.get("nasdaq100") else None
    )
    btc_change = quotes.get("btc").absolute_change if quotes.get("btc") else None
    if (
        dxy_change is not None
        and rate_change is not None
        and dxy_change > 0
        and rate_change > 0
    ):
        second = (
            "달러·금리 상승 부담이 위험자산에도 반영됐습니다."
            if (nasdaq_change or 0) < 0 and (btc_change or 0) < 0
            else "달러·금리 상승에도 위험자산 반응은 엇갈렸습니다."
        )
    elif (
        dxy_change is not None
        and rate_change is not None
        and dxy_change < 0
        and rate_change < 0
    ):
        second = (
            "달러·금리 하락 흐름이 위험자산에도 반영됐습니다."
            if (nasdaq_change or 0) > 0 and (btc_change or 0) > 0
            else "완화 흐름이 모든 위험자산으로 이어지지는 않았습니다."
        )
    else:
        second = "달러와 금리 방향이 엇갈려 Nasdaq과 BTC 반응을 따로 봐야 합니다."
    return first, second


def _news_by_id(news: list[NewsItem]) -> dict[str, NewsItem]:
    return {item.news_id: item for item in news}


def _event_values(event: EconomicEvent) -> str:
    values = []
    if event.forecast:
        values.append(f"예상 {event.forecast}")
    if event.previous:
        values.append(f"이전 {event.previous}")
    return " · ".join(values)


def _signal_reaction(signal: object, data: MarketData) -> str:
    keys = getattr(signal, "related_asset_keys", [])
    return " · ".join(
        _axis_text(data.quotes.get(key), data.quotes[key].name_ko)
        for key in keys
        if key in data.quotes
    )


def _render_report(
    data: MarketData,
    analysis: MorningAnalysis,
    *,
    signal_limit: int,
) -> str:
    news_map = _news_by_id(data.news)
    events = select_future_events(data)
    summary, relationship = _market_summary(data.quotes)
    lines = [
        "<b>JIN Market Pulse</b>",
        data.generated_at_kst.strftime("%m/%d %H:%M KST"),
        "",
        "<b>한눈에</b>",
        html.escape(relationship),
        "",
        REQUIRED_HEADINGS[0],
    ]
    lines.extend(_format_quote_html(quote) for quote in select_report_assets(data.quotes))

    lines.extend(["", REQUIRED_HEADINGS[1]])
    signals = analysis.signals[:signal_limit]
    if not signals:
        lines.append("- 가격 흐름을 넘어설 신규 핵심 뉴스는 제한적")
    for index, signal in enumerate(signals, start=1):
        news = news_map.get(signal.candidate_id)
        if not news:
            continue
        published = (
            news.published_at.astimezone(KST).strftime("%H:%M")
            if news.published_at
            else ""
        )
        source = " · ".join(
            part
            for part in (_short(news.publisher, 28), published)
            if part
        )
        lines.extend(
            [
                f"{index}) <b>{html.escape(signal.title_ko)}</b>",
                f"반응: {html.escape(_signal_reaction(signal, data))}",
                f"{html.escape(signal.relation)}: {html.escape(signal.meaning)}",
                f"<i>{html.escape(source)}</i>",
            ]
        )

    lines.extend(["", REQUIRED_HEADINGS[2]])
    if not events:
        lines.append("- 앞으로 24시간 내 주요 일정 없음")
    for event in events:
        lines.append(
            f"- {event.event_time_kst:%m/%d %H:%M} "
            f"<b>{html.escape(_short(event.title_ko, 32))}</b>"
        )
        values = _event_values(event)
        if values:
            lines.append(f"  {html.escape(_short(values, 55))}")

    lines.extend(
        [
            "",
            REQUIRED_HEADINGS[3],
            html.escape(summary),
            "",
            REQUIRED_HEADINGS[4],
        ]
    )
    if events:
        focus = max(events, key=lambda event: (len(event.importance), -event.event_time_kst.timestamp()))
        lines.extend(
            [
                f"- <b>{html.escape(_short(focus.title_ko, 32))}</b>",
                f"상회: {html.escape(_short(focus.sensitivity_stronger, 76))}",
                f"하회: {html.escape(_short(focus.sensitivity_weaker, 76))}",
            ]
        )
    else:
        lines.append("- 예정된 핵심 지표 시나리오 없음")

    lines.extend(["", REQUIRED_HEADINGS[5]])
    priorities: list[str] = []
    if events:
        focus = max(events, key=lambda event: (len(event.importance), -event.event_time_kst.timestamp()))
        priorities.append(f"{focus.event_time_kst:%H:%M} {focus.title_ko} 결과")
    priorities.extend(["DXY → 미국채 10년물", "Nasdaq → BTC"])
    for index, value in enumerate(priorities[:3], start=1):
        lines.append(f"{index}. {html.escape(_short(value, 48))}")
    return "\n".join(lines).strip()


def render_morning_report(data: MarketData, analysis: MorningAnalysis) -> str:
    for signal_limit in (2, 1, 0):
        result = _render_report(data, analysis, signal_limit=signal_limit)
        try:
            validate_rendered_report(result)
            return result
        except ValueError as exc:
            if "exceeds" not in str(exc):
                raise
    raise ValueError("Morning report exceeds 2000 characters after compaction")


def validate_rendered_report(text: str) -> None:
    for phrase in FORBIDDEN_PHRASES:
        if phrase in text:
            raise ValueError(f"Forbidden phrase in report: {phrase}")
    if "|" in text or "#" in text:
        raise ValueError("Table and Markdown heading characters are forbidden")
    if "N/A" in text:
        raise ValueError("N/A is forbidden in Telegram report")
    missing = [heading for heading in REQUIRED_HEADINGS if heading not in text]
    if missing:
        raise ValueError(f"Missing required report sections: {missing}")
    if len(text) > REPORT_LIMIT:
        raise ValueError(f"Morning report exceeds {REPORT_LIMIT} characters")


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
        "max_output_tokens": settings.openai_max_output_tokens,
        "store": False,
    }
    if settings.openai_web_search:
        request["tools"] = [{"type": "web_search", "search_context_size": "low"}]
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
