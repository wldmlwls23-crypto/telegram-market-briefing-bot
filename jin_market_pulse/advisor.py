from __future__ import annotations

import base64
import html
import io
import json
import re
from typing import Any

from openai import OpenAI

from .config import Settings
from .models import AssetQuote, CurrentMoveAnalysis, NewsItem


TOPIC_EXPLANATIONS: dict[str, dict[str, Any]] = {
    "dxy": {
        "aliases": {"dxy", "달러인덱스", "달러 지수"},
        "title": "DXY(달러지수)",
        "body": "미국 달러가 유로·엔·파운드 등 주요 통화 묶음보다 얼마나 강한지 보여주는 지수입니다. 오르면 달러 강세, 내리면 달러 약세입니다.",
        "market": "달러가 빠르게 강해지면 달러로 표시되는 자산과 글로벌 유동성에 부담이 될 수 있어 Nasdaq·BTC와 함께 봅니다. 다만 매일 반대로 움직이는 공식은 아닙니다.",
    },
    "cpi": {
        "aliases": {"cpi", "소비자물가지수", "소비자 물가"},
        "title": "CPI(소비자물가지수)",
        "body": "소비자가 사는 상품과 서비스 가격이 얼마나 변했는지 보여주는 미국 물가 지표입니다.",
        "market": "예상보다 높으면 금리 인하 기대가 약해져 달러·금리가 오르고 성장주와 BTC가 부담받을 수 있습니다. 경기 충격이 더 큰 날에는 반응이 달라질 수 있습니다.",
    },
    "pce": {
        "aliases": {"pce", "개인소비지출", "근원 pce"},
        "title": "PCE 물가",
        "body": "미국 개인소비지출의 가격 변화를 측정하며 Fed가 물가 판단에 특히 중요하게 보는 지표입니다.",
        "market": "예상과의 차이가 Fed 금리 기대를 바꿔 DXY·미국채·Nasdaq·BTC 순으로 영향을 줄 수 있습니다.",
    },
    "fomc": {
        "aliases": {"fomc", "연준 회의", "연방공개시장위원회"},
        "title": "FOMC",
        "body": "미국 연방준비제도가 기준금리와 통화정책 방향을 결정하는 회의입니다.",
        "market": "금리 결정만큼 성명, 점도표, 의장 발언의 매파·비둘기파 변화가 중요합니다.",
    },
    "pmi": {
        "aliases": {"pmi", "ism", "구매관리자지수"},
        "title": "PMI·ISM",
        "body": "기업의 신규 주문·고용·생산 등을 설문해 경기 흐름을 빠르게 보여주는 지표입니다. 보통 50 위는 확장, 아래는 위축을 뜻합니다.",
        "market": "강하면 경기 우려는 줄지만 금리 부담이 커질 수 있고, 약하면 금리는 내려도 경기둔화 우려가 커질 수 있습니다.",
    },
    "employment": {
        "aliases": {"고용", "비농업", "실업률", "실업수당", "nfp"},
        "title": "미국 고용지표",
        "body": "일자리 증가, 실업률, 임금, 실업수당을 통해 미국 노동시장의 강도를 봅니다.",
        "market": "고용이 예상보다 강하면 달러·금리 상승 압력, 약하면 금리 하락 압력이 생기기 쉽지만 침체 우려가 커지면 위험자산에는 악재가 될 수 있습니다.",
    },
    "rates": {
        "aliases": {"미국채 금리", "국채 금리", "채권 금리", "10년물", "2년물"},
        "title": "미국채 금리",
        "body": "미국 정부 채권의 시장 수익률입니다. 2년물은 Fed 정책 기대에, 10년물은 성장·물가·기간 프리미엄에 더 민감합니다.",
        "market": "금리가 오르면 미래 이익의 현재가치가 낮아져 기술주 부담이 커질 수 있고 달러 유동성에 민감한 BTC도 영향을 받을 수 있습니다.",
    },
    "real_rates": {
        "aliases": {"실질금리", "실질 금리"},
        "title": "실질금리",
        "body": "명목금리에서 기대 인플레이션을 뺀 금리입니다. 현금과 안전채권을 보유할 실질 보상이 얼마나 되는지 보여줍니다.",
        "market": "실질금리가 오르면 금처럼 이자를 주지 않는 자산과 고평가 성장주에 부담이 되기 쉽습니다.",
    },
    "qe": {
        "aliases": {"qe", "양적완화"},
        "title": "QE(양적완화)",
        "body": "중앙은행이 채권을 사들여 금융시장에 유동성을 공급하고 장기금리를 낮추는 정책입니다.",
        "market": "유동성 확대는 일반적으로 위험자산에 우호적이지만 경기·물가 상황과 이미 반영된 기대를 함께 봐야 합니다.",
    },
    "qt": {
        "aliases": {"qt", "양적긴축"},
        "title": "QT(양적긴축)",
        "body": "중앙은행이 보유채권을 줄여 시중 유동성을 회수하는 정책입니다.",
        "market": "유동성에는 부담이지만 속도, 재무부 현금잔고, 은행 준비금 등 다른 자금 흐름이 영향을 완화할 수 있습니다.",
    },
    "vix": {
        "aliases": {"vix", "공포지수", "변동성지수"},
        "title": "VIX(변동성지수)",
        "body": "S&P 500 옵션 가격으로 계산한 향후 약 30일의 예상 변동성입니다. 시장 불안이 커질 때 대체로 상승합니다.",
        "market": "방향 예측 지표라기보다 위험 회피 강도를 보는 보조 지표입니다. 높은 수치가 곧바로 추가 하락을 보장하지는 않습니다.",
    },
    "funding": {
        "aliases": {"펀딩비", "펀딩 비", "funding rate"},
        "title": "코인 펀딩비",
        "body": "무기한 선물 가격을 현물에 가깝게 유지하려고 롱과 숏 사이에 주고받는 비용입니다.",
        "market": "지나치게 양수면 롱 쏠림, 지나치게 음수면 숏 쏠림 가능성을 보여주지만 단독 매매 신호는 아닙니다.",
    },
    "open_interest": {
        "aliases": {"미결제약정", "미결제 약정", "open interest", "oi"},
        "title": "미결제약정(OI)",
        "body": "아직 청산되거나 종료되지 않은 선물·옵션 계약의 총량입니다.",
        "market": "가격과 함께 늘면 신규 포지션 유입, 급격히 줄면 청산이나 포지션 정리를 시사할 수 있지만 롱·숏 방향은 따로 확인해야 합니다.",
    },
    "btc": {
        "aliases": {"btc", "비트", "비트코인", "bitcoin"},
        "title": "비트코인(BTC)",
        "body": "중앙 발행기관 없이 네트워크 참여자가 거래를 검증하는 디지털 자산이며 공급량 상한은 2,100만 개입니다.",
        "market": "단기적으로 달러·금리·유동성·ETF 자금·규제·레버리지 청산에 함께 영향을 받습니다.",
    },
    "eth": {
        "aliases": {"eth", "이더", "이더리움", "ethereum"},
        "title": "이더리움(ETH)",
        "body": "스마트계약과 분산 애플리케이션을 실행하는 블록체인의 기본 자산입니다.",
        "market": "BTC 흐름 외에도 네트워크 사용, 스테이킹, ETF 자금, 규제 변화에 영향을 받습니다.",
    },
    "gold": {
        "aliases": {"gold", "금값", "금 가격", "금 시세"},
        "title": "금",
        "body": "이자를 지급하지 않지만 물가·정책·지정학 불안 때 가치 저장 수단으로 쓰이는 자산입니다.",
        "market": "일반적으로 실질금리와 달러가 내리면 우호적이지만 안전자산 수요가 강하면 달러와 함께 오를 수도 있습니다.",
    },
    "wti": {
        "aliases": {"wti", "유가", "원유", "서부텍사스유"},
        "title": "WTI 유가",
        "body": "미국 서부텍사스산 원유 가격으로 국제 유가의 대표 기준 중 하나입니다.",
        "market": "수요·OPEC+ 공급·재고·지정학에 움직이며 급등하면 물가와 금리 부담을 키울 수 있습니다.",
    },
    "usdkrw": {
        "aliases": {"원달러", "원/달러", "환율", "usdkrw"},
        "title": "원/달러 환율",
        "body": "1달러를 사는 데 필요한 원화의 수입니다. 오르면 원화 약세, 내리면 원화 강세입니다.",
        "market": "달러 방향, 외국인 자금, 한국 수출입 물가와 국내 주식시장 부담을 함께 볼 때 유용합니다.",
    },
}


def matched_topics(text: str) -> list[str]:
    normalized = text.lower()
    return [
        key
        for key, topic in TOPIC_EXPLANATIONS.items()
        if any(alias in normalized for alias in topic["aliases"])
    ]


def render_topic_explanation(topic_key: str, *, simple: bool = False) -> str:
    topic = TOPIC_EXPLANATIONS[topic_key]
    body = str(topic["body"])
    market = str(topic["market"])
    if simple:
        return f"<b>{html.escape(str(topic['title']))}</b>\n{html.escape(body)}"
    return "\n".join(
        [
            f"<b>{html.escape(str(topic['title']))}</b>",
            html.escape(body),
            "",
            "<b>시장에서는</b>",
            html.escape(market),
        ]
    )


def _clean_advisor_text(text: str, max_length: int = 1100) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"^[#*]+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > max_length:
        text = text[: max_length - 1].rstrip() + "…"
    return text


def create_advisor_answer(question: str, settings: Settings) -> str:
    client = OpenAI(api_key=settings.openai_api_key)
    response = client.responses.create(
        model=settings.openai_model,
        instructions=(
            "한국인 비개발자에게 금융시장 개념과 일반적인 관계를 설명한다. "
            "쉬운 한국어로 5문장 이내에 답한다. 일반적 관계와 대표적인 예외를 함께 말한다. "
            "입력에 검증된 실시간 수치가 없으면 현재 상황이나 원인을 단정하지 않는다. "
            "매수·매도, 목표가, 수익 보장, 가격 예측은 하지 않는다. 마크다운 표와 제목은 쓰지 않는다."
        ),
        input=question,
        reasoning={"effort": "low"},
        max_output_tokens=450,
        store=False,
    )
    cleaned = _clean_advisor_text(response.output_text)
    if not cleaned:
        raise RuntimeError("OpenAI advisor returned empty text")
    return "<b>쉽게 설명하면</b>\n" + html.escape(cleaned)


def _current_context(
    target: AssetQuote,
    market_quotes: dict[str, AssetQuote],
    news: list[NewsItem],
) -> tuple[str, dict[str, NewsItem]]:
    quote_keys = (
        target.key,
        "dxy",
        "us2y",
        "us10y",
        "nasdaq100",
        "kospi",
        "wti",
        "gold",
        "btc",
    )
    quotes_payload = []
    for key in dict.fromkeys(quote_keys):
        quote = market_quotes.get(key)
        if (
            quote is None
            or quote.stale
            or not quote.verified
            or quote.validation_status != "verified"
            or quote.calculation_version < 2
        ):
            continue
        quotes_payload.append(
            {
                "asset": quote.name_ko,
                "current": quote.current,
                "previous": quote.previous,
                "change_percent": quote.percent_change,
                "change_bp": (
                    quote.absolute_change * 100
                    if quote.kind == "yield" and quote.absolute_change is not None
                    else None
                ),
                "as_of": quote.as_of.isoformat(),
                "comparison": quote.comparison_label,
                "source": quote.source,
            }
        )
    related = [
        item
        for item in news
        if not item.relevant_asset_keys or target.key in item.relevant_asset_keys
    ][:8]
    news_index = {item.news_id: item for item in related}
    news_payload = [
        {
            "news_id": item.news_id,
            "title": item.title,
            "publisher": item.publisher,
            "published_at": item.published_at.isoformat() if item.published_at else "",
            "source_tier": item.source_tier,
        }
        for item in related
    ]
    return (
        json.dumps(
            {"quotes": quotes_payload, "recent_news": news_payload},
            ensure_ascii=False,
        ),
        news_index,
    )


def create_current_move_analysis(
    question: str,
    target: AssetQuote,
    market_quotes: dict[str, AssetQuote],
    news: list[NewsItem],
    settings: Settings,
) -> tuple[CurrentMoveAnalysis, dict[str, NewsItem]]:
    context, news_index = _current_context(target, market_quotes, news)
    client = OpenAI(api_key=settings.openai_api_key)
    response = client.responses.parse(
        model=settings.openai_model,
        instructions=(
            "검증된 시장자료만 사용해 현재 가격 움직임을 분류한다. "
            "observed에는 입력 숫자로 확인되는 사실만 쓴다. "
            "confirmed_causes는 공식 발표 또는 신뢰 매체가 직접 연결한 원인만 쓴다. "
            "possible_background에는 시각상 동행하거나 일반적으로 가능한 배경만 쓴다. "
            "counter_evidence에는 설명과 맞지 않는 반대 움직임을 쓴다. "
            "수치를 새로 만들지 말고, 원인을 확인하지 못하면 confirmed_causes를 비운다. "
            "한국어로 짧게 쓰고 매매 지시나 가격 예측은 하지 않는다."
        ),
        input=f"질문: {question}\n검증 자료: {context}",
        reasoning={"effort": "low"},
        max_output_tokens=650,
        store=False,
        text_format=CurrentMoveAnalysis,
    )
    analysis = response.output_parsed
    if analysis is None:
        raise RuntimeError("OpenAI returned no CurrentMoveAnalysis")
    analysis.source_news_ids = [
        news_id
        for news_id in analysis.source_news_ids
        if news_id in news_index
    ]
    return analysis, news_index


def create_current_move_answer(
    question: str,
    target: AssetQuote,
    market_quotes: dict[str, AssetQuote],
    news: list[NewsItem],
    settings: Settings,
) -> str:
    analysis, news_index = create_current_move_analysis(
        question,
        target,
        market_quotes,
        news,
        settings,
    )
    direction = (
        f"{target.percent_change:+.2f}%"
        if target.percent_change is not None
        else "변화율 미제공"
    )
    lines = [
        f"<b>{html.escape(target.name_ko)} 움직임</b>",
        (
            f"{target.current:,.2f} · {html.escape(target.comparison_label)} 대비 "
            f"<b>{html.escape(direction)}</b>"
        ),
    ]
    if analysis.observed:
        lines.extend(["", "<b>관찰된 사실</b>"])
        lines.extend(f"• {html.escape(item)}" for item in analysis.observed[:3])
    if analysis.confirmed_causes:
        lines.extend(["", "<b>확인된 원인</b>"])
        lines.extend(f"• {html.escape(item)}" for item in analysis.confirmed_causes[:2])
    elif analysis.possible_background:
        lines.extend(
            [
                "",
                "<b>직접 원인은 확정되지 않았습니다</b>",
                "아래는 같은 시간대에 관찰된 배경입니다.",
            ]
        )
    if analysis.possible_background:
        lines.extend(f"• {html.escape(item)}" for item in analysis.possible_background[:2])
    if analysis.counter_evidence:
        lines.extend(["", "<b>반대 증거</b>"])
        lines.extend(f"• {html.escape(item)}" for item in analysis.counter_evidence[:2])
    cited = [
        news_index[news_id]
        for news_id in analysis.source_news_ids
        if news_id in news_index
    ]
    if cited:
        lines.extend(["", "<b>근거 매체</b>"])
        lines.extend(
            f"• {html.escape(item.publisher)} · "
            f"{item.published_at.astimezone().strftime('%m/%d %H:%M') if item.published_at else '시각 미표기'}"
            for item in cited[:3]
        )
    return "\n".join(lines)


def explain_image(content: bytes, question: str, settings: Settings) -> str:
    encoded = base64.b64encode(content).decode("ascii")
    client = OpenAI(api_key=settings.openai_api_key)
    response = client.responses.create(
        model=settings.openai_model,
        instructions=(
            "한국어 금융시장 도우미다. 이미지에서 실제로 읽히는 제목, 지표명, 수치와 시각을 먼저 구분한다. "
            "읽히지 않는 값은 추측하지 않는다. 의미를 쉬운 한국어로 설명하되 이미지 판독값을 실시간 값처럼 말하지 않는다. "
            "매수·매도나 가격 예측은 하지 않고 700자 이내로 답한다."
        ),
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": question or "이 이미지를 쉽게 설명해줘"},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{encoded}",
                        "detail": "high",
                    },
                ],
            }
        ],
        reasoning={"effort": "low"},
        max_output_tokens=600,
        store=False,
    )
    cleaned = _clean_advisor_text(response.output_text, 1000)
    if not cleaned:
        raise RuntimeError("OpenAI image explanation was empty")
    return "<b>이미지 설명</b>\n" + html.escape(cleaned)


def transcribe_voice(content: bytes, settings: Settings, filename: str = "voice.ogg") -> str:
    client = OpenAI(api_key=settings.openai_api_key)
    file_obj = io.BytesIO(content)
    file_obj.name = filename
    result = client.audio.transcriptions.create(
        model="gpt-4o-mini-transcribe",
        file=file_obj,
        language="ko",
        response_format="text",
    )
    if isinstance(result, str):
        text = result
    else:
        text = str(getattr(result, "text", "") or "")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise RuntimeError("Voice transcription was empty")
    return text[:1000]
