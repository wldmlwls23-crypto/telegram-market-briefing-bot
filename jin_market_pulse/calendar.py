from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

import requests

from .config import KST, Settings
from .models import EconomicEvent


CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CORE_COUNTRIES = {"USD", "KRW", "CNY", "EUR", "JPY"}
FIVE_STAR_TERMS = {
    "consumer price",
    "cpi",
    "core pce",
    "pce price",
    "federal funds rate",
    "fed interest rate decision",
    "fed chair powell",
    "non-farm employment",
    "nonfarm payrolls",
    "jobs report",
}
FOUR_STAR_TERMS = {
    "jobless claims",
    "unemployment claims",
    "pmi",
    "ism",
    "gdp",
    "treasury auction",
    "retail sales",
    "ppi",
    "opec",
    "boj",
    "ecb",
    "bank of korea",
    "exports",
    "imports",
    "consumer sentiment",
}
EXCLUDED_TERMS = {
    "president speaks",
    "treasury currency report",
    "mortgage",
    "consumer credit",
}
COUNTRY_KO = {
    "USD": "미국",
    "KRW": "한국",
    "CNY": "중국",
    "EUR": "유로존",
    "JPY": "일본",
    "GBP": "영국",
    "AUD": "호주",
    "CAD": "캐나다",
}
TITLE_KO = {
    "core pce price index": "근원 PCE 물가",
    "pce price index": "PCE 물가",
    "consumer price index": "소비자물가지수(CPI)",
    "cpi": "소비자물가지수(CPI)",
    "non-farm employment change": "비농업 고용 변화",
    "nonfarm payrolls": "비농업 고용지표",
    "unemployment claims": "신규 실업수당 청구",
    "jobless claims": "신규 실업수당 청구",
    "unemployment rate": "실업률",
    "employment change": "고용 변화",
    "final gdp": "GDP 확정치",
    "advance gdp": "GDP 속보치",
    "flash gdp": "GDP 속보치",
    "gdp price index": "GDP 물가지수",
    "ism manufacturing pmi": "ISM 제조업 PMI",
    "ism services pmi": "ISM 서비스업 PMI",
    "manufacturing pmi": "제조업 PMI",
    "services pmi": "서비스업 PMI",
    "consumer sentiment": "소비자심리지수",
    "fomc statement": "FOMC 성명",
    "fomc meeting minutes": "FOMC 의사록",
    "fomc press conference": "FOMC 기자회견",
    "federal funds rate": "미국 기준금리 결정",
    "fed chair powell speaks": "파월 Fed 의장 발언",
    "treasury auction": "미국채 입찰",
    "retail sales": "소매판매",
    "ppi": "생산자물가지수(PPI)",
}


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in {"", "none", "null", "n/a"} else text


def _event_id(title: str, country: str, event_time: datetime) -> str:
    raw = f"{country}|{title.lower().strip()}|{event_time.isoformat()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _title_ko(title: str) -> str:
    normalized = title.lower().strip()
    for term, translated in TITLE_KO.items():
        if term in normalized:
            return translated
    return title


def _importance(event: dict[str, Any]) -> str | None:
    title = str(event.get("title", "")).lower()
    country = str(event.get("country", "")).upper()
    impact = str(event.get("impact", ""))
    if any(term in title for term in EXCLUDED_TERMS):
        return None
    if country == "USD" and any(term in title for term in FIVE_STAR_TERMS):
        return "★★★★★"
    if country in CORE_COUNTRIES and (
        impact == "High" or any(term in title for term in FOUR_STAR_TERMS)
    ):
        return "★★★★"
    if country in {"EUR", "JPY", "CNY", "KRW"} and any(
        term in title for term in {"rate decision", "cpi", "pmi", "gdp"}
    ):
        return "★★★★"
    return None


def _sensitivity(title: str, country: str) -> tuple[str, str]:
    text = title.lower()
    country_name = COUNTRY_KO.get(country, country)
    currency_rate = (
        "달러·미국채 금리"
        if country == "USD"
        else f"{country_name} 통화·금리"
    )
    if "unemployment claims" in text or "jobless claims" in text:
        return (
            f"예상보다 낮으면 고용 강세로 {currency_rate} 상승 압력",
            f"예상보다 높으면 고용 약화로 {currency_rate} 하락 압력",
        )
    if "unemployment rate" in text:
        return (
            f"예상보다 낮으면 고용 강세로 {currency_rate} 상승 압력",
            f"예상보다 높으면 고용 약화로 {currency_rate} 하락 압력",
        )
    if "pmi" in text or "ism" in text or "consumer sentiment" in text:
        return (
            "예상보다 높으면 경기 우려 완화와 금리 상승 압력",
            "예상보다 낮으면 경기둔화 우려와 금리 하락 압력",
        )
    if any(term in text for term in ("cpi", "pce", "ppi", "price index")):
        return (
            f"예상보다 높으면 인플레 부담과 {currency_rate} 상승 압력",
            f"예상보다 낮으면 인플레 부담 완화와 {currency_rate} 하락 압력",
        )
    if any(term in text for term in ("employment", "payroll", "jobs report")):
        return (
            f"예상보다 높으면 고용 강세로 {currency_rate} 상승 압력",
            f"예상보다 낮으면 고용 약화로 {currency_rate} 하락 압력",
        )
    if "gdp" in text:
        return (
            "예상보다 높으면 경기 견조와 금리 부담",
            "예상보다 낮으면 경기둔화 신호와 금리 하락 압력",
        )
    if any(term in text for term in ("rate", "fomc", "powell")):
        if country != "USD":
            return (
                f"매파적이면 {currency_rate} 상승 압력",
                f"비둘기파적이면 {currency_rate} 하락 압력",
            )
        return (
            "매파적이면 달러·금리 상승과 Nasdaq·BTC 부담",
            "비둘기파적이면 달러·금리 하락과 위험자산 부담 완화",
        )
    return (
        "예상보다 강하면 달러·금리 반응을 우선 관찰",
        "예상보다 약하면 Nasdaq·BTC 동행 여부를 관찰",
    )


def event_meaning(event: EconomicEvent) -> str:
    text = event.title.lower()
    meanings = (
        (("cpi", "consumer price"), "소비자가 체감하는 물가의 상승 속도"),
        (("pce",), "Fed가 중요하게 보는 미국 소비 물가"),
        (("ppi", "producer price"), "생산 단계의 물가 압력"),
        (("non-farm", "nonfarm", "payroll"), "미국 고용 증가와 경기 강도"),
        (("unemployment claims", "jobless claims"), "최근 미국 고용시장의 약화 여부"),
        (("unemployment rate",), "노동시장 전체의 실업 비율"),
        (("pmi", "ism"), "기업 활동을 통해 보는 경기 확장·위축"),
        (("gdp",), "경제 전체의 성장 속도"),
        (("retail sales",), "미국 소비지출의 강도"),
        (("consumer sentiment",), "소비자가 느끼는 경기와 지출 심리"),
        (("fomc", "federal funds rate"), "Fed의 금리 결정과 향후 정책 방향"),
        (("powell",), "Fed의 향후 금리·물가 판단 단서"),
        (("treasury auction",), "미국채 수요와 시장금리 압력"),
    )
    for terms, meaning in meanings:
        if any(term in text for term in terms):
            return meaning
    return f"{event.country_ko} 경기·물가·금리 흐름을 판단하는 자료"


def fetch_economic_events(
    settings: Settings,
    *,
    lookback_hours: int = 0,
    days_ahead: int = 4,
) -> list[EconomicEvent]:
    response = requests.get(CALENDAR_URL, timeout=settings.request_timeout_seconds)
    response.raise_for_status()
    now = datetime.now(KST)
    start = now - timedelta(hours=lookback_hours)
    end = now + timedelta(days=days_ahead)
    result: list[EconomicEvent] = []
    for raw in response.json():
        try:
            event_time = datetime.fromisoformat(str(raw.get("date", ""))).astimezone(KST)
        except ValueError:
            continue
        if event_time < start or event_time > end:
            continue
        importance = _importance(raw)
        if importance is None:
            continue
        title = str(raw.get("title", "")).strip()
        country = str(raw.get("country", "")).upper()
        forecast = _clean(raw.get("forecast"))
        previous = _clean(raw.get("previous"))
        actual = _clean(raw.get("actual"))
        qualitative = any(
            term in title.lower()
            for term in ("speaks", "minutes", "statement", "rate decision", "press conference")
        )
        if not (forecast or previous or actual or qualitative):
            continue
        stronger, weaker = _sensitivity(title, country)
        result.append(
            EconomicEvent(
                event_id=_event_id(title, country, event_time),
                title=title,
                title_ko=_title_ko(title),
                country=country,
                country_ko=COUNTRY_KO.get(country, country),
                event_time_kst=event_time,
                importance=importance,
                forecast=forecast,
                previous=previous,
                actual=actual,
                sensitivity_stronger=stronger,
                sensitivity_weaker=weaker,
            )
        )
    return sorted(result, key=lambda event: event.event_time_kst)
