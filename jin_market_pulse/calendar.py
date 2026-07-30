from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import requests

from .config import KST, NEW_YORK, Settings
from .http_client import ProviderRequestError, request
from .models import EconomicEvent
from .state import StateStore


CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
TRADINGVIEW_CALENDAR_URL = "https://economic-calendar.tradingview.com/events"
CALENDAR_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/138.0 Safari/537.36 JIN-Market-Pulse/2.3"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Cache-Control": "no-cache",
}
TRADINGVIEW_REQUEST_HEADERS = {
    "User-Agent": CALENDAR_REQUEST_HEADERS["User-Agent"],
    "Accept": "application/json,text/plain,*/*",
    "Origin": "https://www.tradingview.com",
}
BLS_CALENDAR_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
BEA_SCHEDULE_URL = "https://www.bea.gov/news/schedule"
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
    "employment situation",
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
    "gdp price index": "GDP 물가지수",
    "final gdp": "GDP 성장률 확정치",
    "advance gdp": "GDP 성장률 속보치",
    "flash gdp": "GDP 성장률 속보치",
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
    "employment situation": "미국 고용보고서",
    "job openings and labor turnover": "미국 구인·이직 보고서(JOLTS)",
    "employment cost index": "미국 고용비용지수",
    "producer price index": "생산자물가지수(PPI)",
    "personal income and outlays": "미국 개인소득·소비(PCE)",
    "international trade in goods and services": "미국 무역수지",
}

OFFICIAL_RELEASE_TERMS = {
    "employment situation": "High",
    "consumer price index": "High",
    "producer price index": "High",
    "employment cost index": "High",
    "job openings and labor turnover": "High",
    "productivity and costs": "High",
    "import and export price": "High",
}


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in {"", "none", "null", "n/a"} else text


def _tradingview_value(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if value is None or value == "":
        return ""
    if isinstance(value, float):
        text = f"{value:g}"
    else:
        text = str(value).strip()
    unit = str(item.get("unit") or "").strip()
    scale = str(item.get("scale") or "").strip()
    if unit == "$":
        return f"${text}{scale}"
    if unit == "%":
        return f"{text}%"
    return f"{text}{scale}"


def _tradingview_events(
    settings: Settings,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    response = request(
        "GET",
        TRADINGVIEW_CALENDAR_URL,
        settings,
        provider="economic_calendar_fallback",
        attempts=3,
        headers=TRADINGVIEW_REQUEST_HEADERS,
        params={
            "from": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "to": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "countries": "US",
        },
        session=SimpleNamespace(get=requests.get),
    )
    payload = response.json()
    items = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ProviderRequestError(
            "economic_calendar_fallback",
            "invalid JSON shape",
        )
    return [
        {
            "title": str(item.get("title") or "").strip(),
            "country": str(item.get("currency") or "USD").upper(),
            "date": str(item.get("date") or ""),
            "impact": "High" if int(item.get("importance") or -1) >= 1 else "Medium",
            "actual": _tradingview_value(item, "actual"),
            "forecast": _tradingview_value(item, "forecast"),
            "previous": _tradingview_value(item, "previous"),
            "source": "TradingView economic calendar",
        }
        for item in items
        if isinstance(item, dict) and item.get("title") and item.get("date")
    ]


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
        (("gdp price",), "미국 경제 전반의 물가 상승 속도"),
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


def _unfold_ical(raw: str) -> list[str]:
    unfolded: list[str] = []
    for line in raw.replace("\r\n", "\n").split("\n"):
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def _official_bls_events(
    settings: Settings,
    store: StateStore | None,
) -> list[dict[str, Any]]:
    cache_key = "calendar:official_bls"
    try:
        response = request(
            "GET",
            BLS_CALENDAR_URL,
            settings,
            provider="bls_calendar",
            attempts=3,
            headers={
                "User-Agent": (
                    "JIN-Market-Pulse/2.2 "
                    f"(personal bot; {settings.data_contact_email})"
                ),
                "Accept": "text/calendar",
            },
            session=SimpleNamespace(get=requests.get),
        )
        text = response.text
        if "BEGIN:VCALENDAR" not in text:
            raise ProviderRequestError("bls_calendar", "invalid calendar response")
        lines = _unfold_ical(text)
        raw_events: list[dict[str, Any]] = []
        event: dict[str, str] | None = None
        for line in lines:
            if line == "BEGIN:VEVENT":
                event = {}
                continue
            if line == "END:VEVENT":
                if event:
                    summary = event.get("SUMMARY", "")
                    normalized = summary.lower()
                    impact = next(
                        (
                            value
                            for term, value in OFFICIAL_RELEASE_TERMS.items()
                            if term in normalized
                        ),
                        "",
                    )
                    if impact and event.get("DTSTART"):
                        when = datetime.strptime(
                            event["DTSTART"],
                            "%Y%m%dT%H%M%S",
                        ).replace(tzinfo=NEW_YORK)
                        raw_events.append(
                            {
                                "title": summary.replace("\\,", ","),
                                "country": "USD",
                                "date": when.isoformat(),
                                "impact": impact,
                                "forecast": "",
                                "previous": "",
                                "actual": "",
                                "source": "U.S. Bureau of Labor Statistics",
                            }
                        )
                event = None
                continue
            if event is None or ":" not in line:
                continue
            name, value = line.split(":", 1)
            key = name.split(";", 1)[0]
            if key in {"SUMMARY", "DTSTART"}:
                event[key] = value
        if store:
            store.cache_set(
                cache_key,
                raw_events,
                source="U.S. Bureau of Labor Statistics",
                ttl_seconds=12 * 60 * 60,
            )
            store.record_provider_result("bls_calendar", success=True)
        return raw_events
    except Exception as exc:
        cached = (
            store.cache_get(cache_key, max_stale_seconds=30 * 24 * 3600)
            if store
            else None
        )
        if store:
            store.record_provider_result(
                "bls_calendar",
                success=False,
                error=type(exc).__name__,
            )
        if cached and isinstance(cached["payload"], list):
            return cached["payload"]
        logging.warning("BLS release calendar unavailable.")
        return []


def _official_bea_events(
    settings: Settings,
    store: StateStore | None,
) -> list[dict[str, Any]]:
    cache_key = "calendar:official_bea"
    try:
        response = request(
            "GET",
            BEA_SCHEDULE_URL,
            settings,
            provider="bea_calendar",
            attempts=3,
            headers={"User-Agent": "JIN-Market-Pulse/2.2"},
            session=SimpleNamespace(get=requests.get),
        )
        body = response.text
        pattern = re.compile(
            r'<div class="release-date">\s*([^<]+?)\s*</div>\s*'
            r'<small[^>]*>\s*([^<]+?)\s*</small>.*?'
            r'<td class="release-title[^"]*"[^>]*>\s*([^<]+?)\s*</td>',
            re.DOTALL | re.IGNORECASE,
        )
        raw_events: list[dict[str, Any]] = []
        year = datetime.now(KST).year
        for date_text, time_text, title in pattern.findall(body):
            normalized = re.sub(r"\s+", " ", title).strip()
            if not any(
                term in normalized.lower()
                for term in (
                    "gdp",
                    "personal income and outlays",
                    "international trade in goods and services",
                )
            ):
                continue
            when = datetime.strptime(
                f"{date_text.strip()} {year} {time_text.strip()}",
                "%B %d %Y %I:%M %p",
            ).replace(tzinfo=NEW_YORK)
            title_for_ranking = (
                f"PCE Price Index · {normalized}"
                if "personal income and outlays" in normalized.lower()
                else normalized
            )
            raw_events.append(
                {
                    "title": title_for_ranking,
                    "country": "USD",
                    "date": when.isoformat(),
                    "impact": "High",
                    "forecast": "",
                    "previous": "",
                    "actual": "",
                    "source": "U.S. Bureau of Economic Analysis",
                }
            )
        if not raw_events:
            raise ProviderRequestError("bea_calendar", "no releases parsed")
        if store:
            store.cache_set(
                cache_key,
                raw_events,
                source="U.S. Bureau of Economic Analysis",
                ttl_seconds=12 * 60 * 60,
            )
            store.record_provider_result("bea_calendar", success=True)
        return raw_events
    except Exception as exc:
        cached = (
            store.cache_get(cache_key, max_stale_seconds=30 * 24 * 3600)
            if store
            else None
        )
        if store:
            store.record_provider_result(
                "bea_calendar",
                success=False,
                error=type(exc).__name__,
            )
        if cached and isinstance(cached["payload"], list):
            return cached["payload"]
        logging.warning("BEA release calendar unavailable.")
        return []


def fetch_economic_events(
    settings: Settings,
    *,
    lookback_hours: int = 0,
    days_ahead: int = 4,
    store: StateStore | None = None,
) -> list[EconomicEvent]:
    now = datetime.now(KST)
    start = now - timedelta(hours=lookback_hours)
    end = now + timedelta(days=days_ahead)
    urls = [("thisweek", CALENDAR_URL)]
    start_of_next_week = (now + timedelta(days=7 - now.weekday())).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    raw_events: list[dict[str, Any]] = []
    for key, url in urls:
        cache_key = f"calendar:{key}"
        try:
            response = request(
                "GET",
                url,
                settings,
                provider="economic_calendar",
                attempts=3,
                headers=CALENDAR_REQUEST_HEADERS,
                params={"_": int(now.timestamp() // 300)},
                session=SimpleNamespace(get=requests.get),
            )
            payload = response.json()
            if not isinstance(payload, list):
                raise ProviderRequestError(
                    "economic_calendar",
                    "invalid JSON shape",
                )
            raw_events.extend(item for item in payload if isinstance(item, dict))
            if store:
                store.cache_set(
                    cache_key,
                    payload,
                    source="Forex Factory calendar",
                    ttl_seconds=15 * 60,
                )
                store.record_provider_result("economic_calendar", success=True)
        except Exception as exc:
            fallback: list[dict[str, Any]] = []
            fallback_succeeded = False
            try:
                fallback = _tradingview_events(settings, start, end)
                fallback_succeeded = True
                if store:
                    store.record_provider_result(
                        "economic_calendar_fallback",
                        success=True,
                    )
            except Exception as fallback_exc:
                if store:
                    store.record_provider_result(
                        "economic_calendar_fallback",
                        success=False,
                        error=type(fallback_exc).__name__,
                    )
            cached = (
                store.cache_get(cache_key, max_stale_seconds=7 * 24 * 3600)
                if store
                else None
            )
            if fallback:
                raw_events.extend(fallback)
                logging.info(
                    "Economic calendar primary feed failed; using live fallback."
                )
                if store:
                    store.cache_set(
                        cache_key,
                        fallback,
                        source="TradingView economic calendar",
                        ttl_seconds=15 * 60,
                    )
            elif cached and isinstance(cached["payload"], list):
                raw_events.extend(
                    item
                    for item in cached["payload"]
                    if isinstance(item, dict)
                )
                logging.warning(
                    "Economic calendar live fetch failed; using cached %s data.",
                    key,
                )
            else:
                logging.warning(
                    "Economic calendar unavailable for %s.",
                    key,
                    exc_info=True,
                )
            if store:
                store.record_provider_result(
                    "economic_calendar",
                    success=fallback_succeeded,
                    error="" if fallback_succeeded else type(exc).__name__,
                )

    if end >= start_of_next_week or not raw_events:
        official = [
            *_official_bls_events(settings, store),
            *_official_bea_events(settings, store),
        ]
        if raw_events:
            current_feed_end = max(
                (
                    datetime.fromisoformat(str(item.get("date", ""))).astimezone(KST)
                    for item in raw_events
                    if item.get("date")
                ),
                default=now,
            )
            official = [
                item
                for item in official
                if datetime.fromisoformat(str(item["date"])).astimezone(KST)
                > current_feed_end
            ]
        raw_events.extend(official)

    result: list[EconomicEvent] = []
    seen: set[str] = set()
    for raw in raw_events:
        try:
            event_time = datetime.fromisoformat(str(raw.get("date", ""))).astimezone(KST)
        except (TypeError, ValueError):
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
        event_id = _event_id(title, country, event_time)
        if event_id in seen:
            continue
        seen.add(event_id)
        stronger, weaker = _sensitivity(title, country)
        result.append(
            EconomicEvent(
                event_id=event_id,
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
                source=str(raw.get("source") or "Forex Factory calendar"),
            )
        )
    return sorted(result, key=lambda event: event.event_time_kst)
