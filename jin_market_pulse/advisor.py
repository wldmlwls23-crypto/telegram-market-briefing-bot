from __future__ import annotations

import html
import re

from openai import OpenAI

from .config import Settings


TOPIC_EXPLANATIONS = {
    "dxy": {
        "aliases": {"dxy", "달러인덱스", "달러 지수"},
        "title": "DXY(달러지수)",
        "body": (
            "미국 달러가 유로·엔·파운드 등 주요 통화 묶음에 비해 얼마나 강한지 "
            "보여주는 지수입니다. DXY가 오르면 달러 강세, 내리면 달러 약세로 봅니다."
        ),
        "market": (
            "달러 강세는 달러로 표시되는 원자재와 글로벌 유동성에 부담이 될 수 있어 "
            "Nasdaq·BTC와 함께 확인하는 경우가 많습니다."
        ),
    },
    "rates": {
        "aliases": {"미국채 금리", "국채 금리", "채권 금리", "금리"},
        "title": "미국채 금리",
        "body": (
            "미국 정부가 발행한 채권의 시장 수익률입니다. 2년물은 Fed 정책 기대에, "
            "10년물은 성장·물가·장기 자금비용에 더 민감합니다."
        ),
        "market": (
            "금리가 빠르게 오르면 미래 이익의 현재가치가 낮아져 기술주에 부담이 될 수 "
            "있고, BTC 같은 위험자산의 유동성 환경에도 영향을 줍니다."
        ),
    },
    "nasdaq": {
        "aliases": {"nasdaq", "nasdaq100", "나스닥", "나스닥100"},
        "title": "Nasdaq 100",
        "body": (
            "Nasdaq 시장의 대형 비금융 기업 중심 지수로, 빅테크와 성장주의 비중이 큽니다."
        ),
        "market": (
            "금리와 달러 변화에 민감해 글로벌 위험선호를 볼 때 BTC와 함께 비교하기 좋습니다."
        ),
    },
    "btc": {
        "aliases": {"btc", "비트", "비트코인", "bitcoin"},
        "title": "비트코인(BTC)",
        "body": (
            "중앙 발행기관 없이 네트워크가 거래를 검증하는 디지털 자산입니다. "
            "공급량이 제한되어 있지만 가격 변동성은 매우 큽니다."
        ),
        "market": (
            "단기적으로 달러·금리·유동성·ETF 수급·규제와 위험선호의 영향을 함께 받습니다."
        ),
    },
    "eth": {
        "aliases": {"eth", "이더", "이더리움", "ethereum"},
        "title": "이더리움(ETH)",
        "body": (
            "스마트계약과 애플리케이션을 실행할 수 있는 블록체인 네트워크의 기본 자산입니다."
        ),
        "market": (
            "BTC 흐름뿐 아니라 네트워크 사용, 스테이킹, ETF와 규제 변화에도 영향을 받습니다."
        ),
    },
    "wti": {
        "aliases": {"wti", "유가", "원유", "서부텍사스유"},
        "title": "WTI 유가",
        "body": "미국 서부텍사스산 원유 가격으로 국제 유가의 대표 기준 중 하나입니다.",
        "market": (
            "상승하면 물가와 금리 부담을 높일 수 있고, 급락하면 수요 둔화 우려를 반영할 수 있습니다."
        ),
    },
    "gold": {
        "aliases": {"gold", "금값", "금 가격", "금시세"},
        "title": "금",
        "body": "인플레이션·금융 불안 때 자금이 이동하기도 하는 대표적인 안전자산입니다.",
        "market": "일반적으로 실질금리와 달러가 오르면 부담, 내려가면 우호적인 환경이 됩니다.",
    },
    "usdkrw": {
        "aliases": {"원달러", "원/달러", "환율", "usdkrw"},
        "title": "원/달러 환율",
        "body": "1달러를 사는 데 필요한 원화의 수입니다. 오르면 원화 약세, 내리면 원화 강세입니다.",
        "market": "외국인 자금 흐름과 수입물가, 한국 주식시장의 부담을 볼 때 중요합니다.",
    },
    "cpi": {
        "aliases": {"cpi", "소비자물가지수", "소비자 물가"},
        "title": "소비자물가지수(CPI)",
        "body": "소비자가 구매하는 상품과 서비스 가격의 변화를 측정하는 대표 물가지표입니다.",
        "market": "예상보다 높으면 달러·금리 상승과 위험자산 부담으로 이어질 수 있습니다.",
    },
    "pce": {
        "aliases": {"pce", "개인소비지출", "근원 pce"},
        "title": "PCE 물가",
        "body": "미국 개인소비지출 가격 변화를 측정하며 Fed가 중요하게 보는 물가지표입니다.",
        "market": "예상과의 차이가 Fed 금리 기대를 바꿔 달러·미국채·Nasdaq·BTC에 영향을 줍니다.",
    },
    "fomc": {
        "aliases": {"fomc", "연준 회의", "연방공개시장위원회"},
        "title": "FOMC",
        "body": "미국 연방준비제도가 기준금리와 통화정책 방향을 결정하는 회의입니다.",
        "market": "결정 자체뿐 아니라 성명과 의장 발언의 매파·비둘기파 변화가 중요합니다.",
    },
    "ism": {
        "aliases": {"ism", "구매관리자지수", "pmi"},
        "title": "ISM 지수",
        "body": "미국 기업의 신규주문·고용·생산 등을 설문해 경기 흐름을 빠르게 보여주는 지표입니다.",
        "market": "예상보다 강하면 경기 기대와 금리가, 약하면 경기둔화 우려가 커질 수 있습니다.",
    },
}


def matched_topics(text: str) -> list[str]:
    normalized = text.lower()
    return [
        key
        for key, topic in TOPIC_EXPLANATIONS.items()
        if any(alias in normalized for alias in topic["aliases"])
    ]


def render_topic_explanation(topic_key: str) -> str:
    topic = TOPIC_EXPLANATIONS[topic_key]
    return "\n".join(
        [
            f"<b>{html.escape(topic['title'])}</b>",
            html.escape(topic["body"]),
            "",
            f"<b>시장에서는</b>",
            html.escape(topic["market"]),
        ]
    )


def _clean_advisor_text(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"^[#*]+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > 1100:
        text = text[:1099].rstrip() + "…"
    return text


def create_advisor_answer(question: str, settings: Settings) -> str:
    client = OpenAI(api_key=settings.openai_api_key)
    response = client.responses.create(
        model=settings.openai_model,
        instructions=(
            "한국의 비개발자 개인투자자에게 시장 원리를 설명하는 교육형 상담사입니다. "
            "쉬운 한국어로 핵심부터 최대 다섯 문단 이내로 답하세요. "
            "개념과 자산 간 일반적인 관계만 설명하고, 실시간 가격·뉴스·발표값을 아는 척하지 마세요. "
            "사용자가 현재 상황의 원인을 물으면 검증된 실시간 자료가 없어 단정할 수 없다고 밝히세요. "
            "매수·매도·롱·숏·손절·수익 보장·가격 예측을 제공하지 마세요. "
            "표와 마크다운 제목은 쓰지 마세요."
        ),
        input=question,
        reasoning={"effort": "low"},
        max_output_tokens=500,
        store=False,
    )
    cleaned = _clean_advisor_text(response.output_text)
    if not cleaned:
        raise RuntimeError("OpenAI advisor returned empty text")
    return "<b>시장 설명</b>\n" + html.escape(cleaned)
