import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import feedparser
import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from openai import OpenAI


KST = ZoneInfo("Asia/Seoul")
PARIS = ZoneInfo("Europe/Paris")
NEW_YORK = ZoneInfo("America/New_York")
UTC = timezone.utc

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
SENT_ALERTS_FILE = Path(os.getenv("SENT_ALERTS_FILE", "sent_alerts.json"))

# Railway file storage can reset on redeploy. This JSON state prevents repeats while
# the current worker filesystem is alive. For stronger persistence later, move it to
# Redis, Railway Volume, Postgres, or another small external store.
STATE_RETENTION_DAYS = 14
ALERT_MONITOR_MINUTES = 15
PRE_EVENT_REMINDER_HOURS = 6

REPORT_SCHEDULES = [
    {
        "id": "morning",
        "label": "Morning Market Report",
        "timezone": KST,
        "hour": 6,
        "minute": 50,
    },
    {
        "id": "korea_pre",
        "label": "Korea Pre-Market",
        "timezone": KST,
        "hour": 8,
        "minute": 0,
    },
    {
        "id": "korea_close",
        "label": "Korea Close Recap",
        "timezone": KST,
        "hour": 16,
        "minute": 0,
    },
    {
        "id": "europe_pre",
        "label": "Europe Pre-Market",
        "timezone": PARIS,
        "hour": 8,
        "minute": 0,
    },
    {
        "id": "europe_close",
        "label": "Europe Close Recap",
        "timezone": PARIS,
        "hour": 18,
        "minute": 0,
    },
    {
        "id": "us_pre",
        "label": "US Pre-Market",
        "timezone": NEW_YORK,
        "hour": 8,
        "minute": 30,
    },
    {
        "id": "us_close",
        "label": "US Close Recap",
        "timezone": NEW_YORK,
        "hour": 16,
        "minute": 30,
    },
]

NEWS_FEEDS = [
    (
        "Global Markets",
        "https://news.google.com/rss/search?q=global%20markets%20stocks%20bonds%20dollar%20oil%20gold%20when:1d&hl=en-US&gl=US&ceid=US:en",
    ),
    (
        "US Macro",
        "https://news.google.com/rss/search?q=Federal%20Reserve%20CPI%20PCE%20jobs%20PMI%20Treasury%20yields%20DXY%20when:1d&hl=en-US&gl=US&ceid=US:en",
    ),
    (
        "Crypto Structure",
        "https://news.google.com/rss/search?q=bitcoin%20ethereum%20ETF%20Binance%20Coinbase%20CME%20stablecoin%20when:1d&hl=en-US&gl=US&ceid=US:en",
    ),
    (
        "Korea Markets",
        "https://news.google.com/rss/search?q=KOSPI%20KOSDAQ%20USD%20KRW%20foreign%20investors%20semiconductor%20when:1d&hl=ko&gl=KR&ceid=KR:ko",
    ),
    (
        "Breaking Market Risk",
        "https://news.google.com/rss/search?q=war%20attack%20assassination%20bank%20failure%20exchange%20withdrawals%20circuit%20breaker%20when:1d&hl=en-US&gl=US&ceid=US:en",
    ),
]

CRYPTO_IDS = ["bitcoin", "ethereum"]
INDEX_SYMBOLS = {
    "S&P 500": "^spx",
    "Nasdaq 100": "^ndx",
    "Dow Jones": "^dji",
    "KOSPI": "^kospi",
    "KOSDAQ": "^kosdaq",
    "USD/KRW": "usdkrw",
    "DXY": "dx.f",
    "Gold": "gc.f",
    "WTI Oil": "cl.f",
}

HIGH_VALUE_TERMS = {
    "cpi",
    "pce",
    "core pce",
    "fomc",
    "powell",
    "fed chair",
    "jobs report",
    "nonfarm",
    "payrolls",
    "unemployment",
    "jobless claims",
    "gdp",
    "pmi",
    "ism",
    "treasury yield",
    "treasury auction",
    "dxy",
    "dollar index",
    "nasdaq",
    "s&p 500",
    "bitcoin etf",
    "ethereum etf",
    "spot etf",
    "etf inflow",
    "etf outflow",
    "binance",
    "coinbase",
    "cme",
    "stablecoin",
    "usdt",
    "usdc",
    "depeg",
    "hack",
    "withdrawal",
    "suspended withdrawals",
    "kospi",
    "kosdaq",
    "usd/krw",
    "won",
    "foreign investors",
    "oil",
    "gold",
    "opec",
    "china stimulus",
    "china pmi",
    "geopolitical",
    "war",
    "missile",
    "attack",
    "sanction",
    "bank failure",
    "liquidity",
    "circuit breaker",
}

NOISE_TERMS = {
    "price prediction",
    "price target",
    "analyst predicts",
    "expert predicts",
    "trader says",
    "influencer",
    "meme coin",
    "airdrop",
    "presale",
    "sponsored",
    "advertisement",
    "could soar",
    "set to explode",
    "next bitcoin",
    "buy now",
    "strong buy",
}

SEVERITY_FIVE_TERMS = {
    "fomc",
    "fed chair powell",
    "cpi",
    "pce",
    "core pce",
    "nonfarm payrolls",
    "jobs report",
    "assassination",
    "shot",
    "attack",
    "war",
    "military conflict",
    "invasion",
    "emergency rate",
    "bank failure",
    "circuit breaker",
    "depeg",
    "usdt",
    "usdc",
    "withdrawals suspended",
    "suspended withdrawals",
    "exchange hack",
    "hacked",
    "oil surge",
    "oil spike",
}

EVENT_FIVE_TERMS = {
    "cpi",
    "consumer price",
    "pce",
    "core pce",
    "fomc",
    "federal funds rate",
    "fed interest rate decision",
    "fed chair powell",
    "nonfarm payrolls",
    "unemployment rate",
    "employment change",
    "jobs report",
}

EVENT_FOUR_TERMS = {
    "jobless claims",
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
    "bOK",
    "exports",
    "imports",
}


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def send_telegram_message(text: str) -> None:
    token = require_env("TELEGRAM_BOT_TOKEN")
    chat_id = require_env("TELEGRAM_CHAT_ID")
    response = requests.post(
        TELEGRAM_API_URL.format(token=token),
        json={
            "chat_id": chat_id,
            "text": text[:3900],
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    response.raise_for_status()


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower()).strip()


def contains_any(text: str, terms: set[str]) -> bool:
    normalized = normalize_text(text)
    return any(term.lower() in normalized for term in terms)


def stable_key(*parts: str) -> str:
    raw = "|".join(normalize_text(part) for part in parts if part)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def load_sent_state() -> dict[str, Any]:
    if not SENT_ALERTS_FILE.exists():
        return {"items": {}}
    try:
        return json.loads(SENT_ALERTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("Could not read sent state. Starting with empty state.")
        return {"items": {}}


def save_sent_state(state: dict[str, Any]) -> None:
    cutoff = datetime.now(UTC) - timedelta(days=STATE_RETENTION_DAYS)
    items = state.setdefault("items", {})
    for key, value in list(items.items()):
        sent_at = value.get("sent_at")
        try:
            sent_dt = datetime.fromisoformat(sent_at)
        except Exception:
            continue
        if sent_dt < cutoff:
            items.pop(key, None)
    SENT_ALERTS_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def already_sent(key: str) -> bool:
    return key in load_sent_state().get("items", {})


def mark_sent(key: str, kind: str, title: str, url: str = "") -> None:
    state = load_sent_state()
    state.setdefault("items", {})[key] = {
        "kind": kind,
        "title": title,
        "url": url,
        "sent_at": datetime.now(UTC).isoformat(),
    }
    save_sent_state(state)


def fetch_news(max_per_feed: int = 10) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for source, url in NEWS_FEEDS:
        parsed = feedparser.parse(url)
        for entry in parsed.entries[:max_per_feed]:
            title = entry.get("title", "").strip()
            if not title:
                continue
            items.append(
                {
                    "source": source,
                    "title": title,
                    "link": entry.get("link", "").strip(),
                    "published": entry.get("published", "").strip(),
                    "summary": re.sub("<[^<]+?>", "", entry.get("summary", "")).strip(),
                }
            )
    return items


def fetch_crypto_prices() -> dict[str, Any]:
    response = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={
            "ids": ",".join(CRYPTO_IDS),
            "vs_currencies": "usd",
            "include_24hr_change": "true",
            "include_24hr_vol": "true",
            "include_market_cap": "true",
        },
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def fetch_major_indices() -> list[dict[str, str]]:
    results = []
    for name, symbol in INDEX_SYMBOLS.items():
        try:
            response = requests.get(
                f"https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv",
                timeout=20,
            )
            response.raise_for_status()
            lines = response.text.strip().splitlines()
            if len(lines) < 2:
                continue

            header = [h.strip() for h in lines[0].split(",")]
            row = [r.strip() for r in lines[1].split(",")]
            data = dict(zip(header, row))
            results.append(
                {
                    "name": name,
                    "close": data.get("Close", "N/A"),
                    "open": data.get("Open", "N/A"),
                    "high": data.get("High", "N/A"),
                    "low": data.get("Low", "N/A"),
                    "date": data.get("Date", "N/A"),
                    "time": data.get("Time", "N/A"),
                }
            )
        except Exception as exc:
            results.append({"name": name, "close": f"fetch failed: {exc}", "date": "", "time": ""})
    return results


def classify_event(event: dict[str, Any]) -> dict[str, str]:
    title = str(event.get("title", ""))
    country = str(event.get("country", ""))
    impact = str(event.get("impact", ""))
    combined = f"{country} {title} {impact}"

    if contains_any(combined, EVENT_FIVE_TERMS):
        stars = "★★★★★"
    elif impact == "High" or contains_any(combined, EVENT_FOUR_TERMS):
        stars = "★★★★"
    elif impact == "Medium":
        stars = "★★★"
    else:
        stars = "exclude"

    sensitivity = build_event_sensitivity(title)
    return {"stars": stars, "sensitivity": sensitivity}


def build_event_sensitivity(title: str) -> str:
    text = normalize_text(title)
    if "pmi" in text or "ism" in text:
        return "강하면 달러·금리 상승 압력, 약하면 달러 약세·금리 하락 압력"
    if "cpi" in text or "pce" in text or "ppi" in text:
        return "높으면 인플레 부담, 낮으면 달러 약세 압력"
    if "job" in text or "payroll" in text or "unemployment" in text:
        return "강하면 달러·금리 상승 압력, 약하면 고용 약화 신호"
    if "gdp" in text:
        return "강하면 경기 견조·금리 부담, 약하면 경기둔화 신호"
    if "rate" in text or "fomc" in text or "powell" in text:
        return "달러·미국채 금리·Nasdaq·BTC 반응 확인"
    if "oil" in text or "opec" in text:
        return "유가 강세는 인플레 부담, 약세는 부담 완화"
    return "DXY·미국채 금리·Nasdaq·BTC 반응 확인"


def fetch_economic_calendar(days_ahead: int = 1) -> list[dict[str, str]]:
    response = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=20)
    response.raise_for_status()
    events = response.json()

    now = datetime.now(KST)
    end = now + timedelta(days=days_ahead)
    filtered = []
    for event in events:
        event_time = event.get("date", "")
        try:
            event_dt = datetime.fromisoformat(event_time.replace("Z", "+00:00")).astimezone(KST)
        except ValueError:
            continue

        if not (now.date() <= event_dt.date() <= end.date()):
            continue

        classification = classify_event(event)
        if classification["stars"] == "exclude":
            continue

        filtered.append(
            {
                "time_kst": event_dt.strftime("%Y-%m-%d %H:%M"),
                "country": str(event.get("country", "")),
                "title": str(event.get("title", "")),
                "importance": classification["stars"],
                "sensitivity": classification["sensitivity"],
                "forecast": str(event.get("forecast", "")),
                "previous": str(event.get("previous", "")),
                "timestamp": event_dt.isoformat(),
            }
        )
    return sorted(filtered, key=lambda item: item["timestamp"])[:20]


def score_news_item(item: dict[str, str]) -> dict[str, Any]:
    title = item.get("title", "")
    summary = item.get("summary", "")
    text = f"{title} {summary}"

    if contains_any(text, NOISE_TERMS):
        return {"include": False, "importance": "noise", "reason": "가격 전망·홍보성·SNS성 노이즈"}

    score = 0
    if contains_any(text, HIGH_VALUE_TERMS):
        score += 2
    if contains_any(text, SEVERITY_FIVE_TERMS):
        score += 3
    if re.search(r"\b(crash|plunge|surge|spike|halt|emergency|breaking)\b", normalize_text(text)):
        score += 1

    if score >= 5:
        importance = "★★★★★"
    elif score >= 2:
        importance = "★★★★"
    else:
        importance = "★★★"

    return {
        "include": score >= 1,
        "importance": importance,
        "reason": "시장 가격·정책·유동성·위험자산에 연결 가능",
    }


def filter_news(news: list[dict[str, str]]) -> list[dict[str, str]]:
    filtered = []
    seen = set()
    for item in news:
        key = stable_key(item.get("title", ""), item.get("link", ""))
        if key in seen:
            continue
        seen.add(key)
        scored = score_news_item(item)
        if not scored["include"]:
            continue
        filtered.append(
            {
                **item,
                "importance": scored["importance"],
                "selection_reason": scored["reason"],
                "dedupe_key": key,
            }
        )
    importance_order = {"★★★★★": 0, "★★★★": 1, "★★★": 2}
    return sorted(filtered, key=lambda item: importance_order.get(item["importance"], 9))[:18]


def collect_market_data(report_type: str) -> dict[str, Any]:
    raw_news = fetch_news()
    return {
        "report_type": report_type,
        "generated_at_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "news": filter_news(raw_news),
        "economic_calendar": fetch_economic_calendar(days_ahead=1),
        "crypto_prices": fetch_crypto_prices(),
        "major_indices": fetch_major_indices(),
        "already_sent_items": list(load_sent_state().get("items", {}).values())[-20:],
    }


def common_writing_rules() -> str:
    return """
공통 작성 원칙:
- 한국어로 작성
- 짧고 쉬운 문장 사용
- 뉴스 나열 금지, 핵심 변수만 정리
- 실제 발생한 뉴스, 공식 경제지표, 주요 시장 데이터만 사용
- 전문가 개인 전망, SNS 노이즈, 알트코인 홍보, 가격 목표가 제외
- 매수/매도, 롱/숏, 진입/손절, 가격 예측 표현 금지
- 이미 긴급 알림으로 보낸 정보는 반복하지 말고 업데이트만 짧게 언급
- 관찰형 리포트로 작성
- 달러, 미국채 금리, Nasdaq, BTC 압력 연결을 보여줄 것

금지 표현:
매수해도 된다, 숏을 봐야 한다, 롱 진입, 손절, 대응해야 한다, 오를 가능성이 높다,
내릴 가능성이 높다, 강력 추천, 전문가들은 전망한다, 곧 상승할 것, 반드시 확인해야 한다

허용 표현:
달러 강세 압력, 달러 약세 압력, 금리 상승 압력, 금리 하락 압력, Nasdaq 부담,
Nasdaq 부담 완화, BTC 위험자산 압박, BTC 위험자산 동행, 경기둔화 신호,
인플레 부담, 고용 약화 신호, 유동성 둔화 신호, 시장 반응 확인 대상, 관찰 우선순위
""".strip()


def report_prompt(report_type: str) -> str:
    prompts = {
        "morning": """
# Morning Market Report

반드시 아래 제목과 섹션을 그대로 사용하세요.

## 0. [Current Asset Snapshot]
BTC, ETH, Nasdaq, DXY, 미국 2년물/10년물 금리, KOSPI, 유가, 금 중 오늘 중요한 것만 표로 요약하세요.

## 1. [Signal vs Noise]
오늘 시장에서 실제로 중요한 핵심 신호 2~4개만 정리하세요.

## 2. [Economic Calendar]
앞으로 24시간 안의 중요한 일정만 표로 정리하세요. 중요도 낮은 일정은 제외하세요.

## 3. [Market Pulse]
Crypto, Dollar, Rates, Nasdaq, KOSPI, Oil/Gold 중 중요한 것만 표로 요약하세요.

## 4. [Indicator Sensitivity]
오늘 주요 지표가 강하게/약하게 나올 때 달러, 금리, Nasdaq, BTC에 생기는 압력을 표로 정리하세요.

## 5. [Today’s Priority]
오늘 확인할 우선순위 3~5개만 번호로 정리하세요.
마지막 문장은 반드시 "오늘 핵심은 [가장 중요한 지표/이벤트] → DXY → 미국채 금리 → Nasdaq → BTC 순서로 관찰." 형식으로 끝내세요.
""",
        "korea_pre": """
Korea Pre-Market 리포트입니다.
한국장 시작 전 1시간 관점으로 작성하세요.
전일 미국장, DXY, 미국채 금리, 원/달러, 반도체, 2차전지, 금융, 조선, 방산, KOSPI/KOSDAQ 핵심 압력만 짧게 정리하세요.
""",
        "korea_close": """
Korea Close Recap 리포트입니다.
한국장 마감 후 사용자가 따로 뉴스를 찾지 않아도 되게 충분히 정리하세요.
KOSPI/KOSDAQ 등락, 외국인/기관/개인 수급, 원/달러 환율, 강한 섹터, 약한 섹터, 삼성전자/SK하이닉스/2차전지/금융/조선 등 지수 영향 섹터, 한국장에 영향을 준 뉴스, 다음 미국장 변수 중심으로 작성하세요.
""",
        "europe_pre": """
Europe Pre-Market 리포트입니다.
유럽장 전 1시간 관점으로 작성하세요.
DAX, Euro Stoxx, EUR/USD, 유럽 금리, 유가, 지정학 이슈 중 미국장과 BTC에 이어질 글로벌 위험심리 변수만 정리하세요.
""",
        "europe_close": """
Europe Close Recap 리포트입니다.
유럽장 마감 후 30분 관점으로 작성하세요.
유럽 증시, 유로, 금리, 유가, 방산/에너지/은행 섹터, 미국장과 BTC에 연결되는 변수를 정리하세요.
""",
        "us_pre": """
US Pre-Market 리포트입니다.
미국장 전 1시간 관점으로 작성하세요.
미국 경제지표, Fed 발언, DXY, 미국채 2년물/10년물, Nasdaq 선물, BTC, 예정 이벤트와 관찰 포인트를 정리하세요.
""",
        "us_close": """
US Close Recap 리포트입니다.
미국장 마감 후 30분 관점으로 작성하세요.
Nasdaq/S&P500/Dow 등락, 강한 섹터, 약한 섹터, 빅테크, 반도체, DXY, 미국채 2년물/10년물, 유가/금, BTC/ETH 동행 여부, ETF 자금 흐름, 장중 핵심 뉴스, 다음 아시아장 변수 중심으로 충분히 정리하세요.
""",
    }
    return prompts[report_type].strip()


def create_briefing(market_data: dict[str, Any], report_type: str = "morning") -> str:
    client = OpenAI(api_key=require_env("OPENAI_API_KEY"))
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    raw_data = json.dumps(market_data, ensure_ascii=False, indent=2)

    prompt = f"""
당신은 한국 개인투자자를 위한 관찰형 시장 브리핑 에디터입니다.
이 봇의 목표는 뉴스 많이 보여주기가 아니라, 오늘 시장에서 진짜 봐야 할 변수만 남기는 것입니다.

{common_writing_rules()}

이번 리포트 타입:
{report_prompt(report_type)}

중요도 정책:
- ★★★: 참고 수준. 필요할 때만 짧게 반영.
- ★★★★: 정규 브리핑 반영. 긴급 알림처럼 쓰지 않음.
- ★★★★★: 시장 충격성, 예상 불가능성, 글로벌 위험자산 영향, 실제 가격 반응을 우선 반영.

원자료:
{raw_data}
""".strip()

    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "You write concise Korean market briefings without trading advice.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("OpenAI returned an empty response.")
    return content.strip()


def create_emergency_alert(item: dict[str, str]) -> str:
    client = OpenAI(api_key=require_env("OPENAI_API_KEY"))
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    raw_data = json.dumps(item, ensure_ascii=False, indent=2)
    prompt = f"""
아래 원자료를 바탕으로 Telegram 긴급 시장 알림을 작성하세요.

형식:
[긴급 시장 알림 | ★★★★★]

핵심:
- 실제 발생한 내용 1~2줄

시장 반응:
- DXY:
- 미국채 금리:
- Nasdaq:
- BTC:

의미:
- 달러/금리/위험자산 압력 중심으로 짧게 설명

규칙:
- 한국어
- 루머처럼 쓰지 말고 확인된 내용만
- 매수/매도, 롱/숏, 가격 예측, 전문가 전망 금지
- 모르는 시장 반응은 "확인 필요"라고 작성

원자료:
{raw_data}
""".strip()
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You write urgent Korean market alerts without trading advice."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("OpenAI returned an empty emergency alert.")
    return content.strip()


def create_pre_event_reminder(event: dict[str, str]) -> str:
    return f"""[중요 이벤트 사전 알림 | ★★★★★]

{event['time_kst']} KST {event['country']} {event['title']} 발표 예정.

시장 관찰 포인트:
- DXY 반응
- 미국채 2년물/10년물 반응
- Nasdaq 선물 반응
- BTC 위험자산 동행 여부

결과 발표 후 실제 발표값과 시장 반응만 별도 정리합니다."""


def send_report(report_type: str) -> None:
    try:
        market_data = collect_market_data(report_type)
        briefing = create_briefing(market_data, report_type)
        label = next(item["label"] for item in REPORT_SCHEDULES if item["id"] == report_type)
        title = f"{label} | {market_data['generated_at_kst']} KST"
        send_telegram_message(f"{title}\n\n{briefing}")
        logging.info("%s sent successfully.", label)
    except Exception as exc:
        logging.exception("Failed to send %s.", report_type)
        try:
            send_telegram_message(
                f"[브리핑 봇 오류]\n{datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST\n{exc}"
            )
        except Exception:
            logging.exception("Failed to send Telegram error message.")


def monitor_emergency_alerts() -> None:
    try:
        news = filter_news(fetch_news(max_per_feed=6))
        for item in news:
            if item.get("importance") != "★★★★★":
                continue
            key = f"emergency:{item['dedupe_key']}"
            if already_sent(key):
                continue
            alert = create_emergency_alert(item)
            send_telegram_message(alert)
            mark_sent(key, "emergency", item.get("title", ""), item.get("link", ""))
            logging.info("Emergency alert sent: %s", item.get("title", ""))

        crypto = fetch_crypto_prices()
        for asset, values in crypto.items():
            change = float(values.get("usd_24h_change") or 0)
            if abs(change) < 5:
                continue
            key = f"crypto_move:{asset}:{datetime.now(KST).strftime('%Y-%m-%d-%H')}"
            if already_sent(key):
                continue
            direction = "급등" if change > 0 else "급락"
            item = {
                "title": f"{asset.upper()} 24시간 {direction} {change:.2f}%",
                "importance": "★★★★★",
                "source": "CoinGecko",
                "summary": "BTC/ETH 급변은 위험자산 심리와 연동될 수 있어 긴급 알림 후보로 처리합니다.",
            }
            alert = create_emergency_alert(item)
            send_telegram_message(alert)
            mark_sent(key, "crypto_move", item["title"])
    except Exception:
        logging.exception("Emergency alert monitor failed.")


def send_due_pre_event_reminders() -> None:
    try:
        now = datetime.now(KST)
        window_end = now + timedelta(hours=PRE_EVENT_REMINDER_HOURS)
        for event in fetch_economic_calendar(days_ahead=1):
            if event.get("importance") != "★★★★★":
                continue
            event_dt = datetime.fromisoformat(event["timestamp"])
            if not (now <= event_dt <= window_end):
                continue
            key = stable_key("pre_event", event["title"], event["timestamp"])
            state_key = f"pre_event:{key}"
            if already_sent(state_key):
                continue
            send_telegram_message(create_pre_event_reminder(event))
            mark_sent(state_key, "pre_event", event["title"])
            logging.info("Pre-event reminder sent: %s", event["title"])
    except Exception:
        logging.exception("Pre-event reminder failed.")


def start_scheduler() -> None:
    scheduler = BlockingScheduler(timezone=KST)

    for item in REPORT_SCHEDULES:
        scheduler.add_job(
            send_report,
            CronTrigger(hour=item["hour"], minute=item["minute"], timezone=item["timezone"]),
            args=[item["id"]],
            id=f"report_{item['id']}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )

    scheduler.add_job(
        monitor_emergency_alerts,
        "interval",
        minutes=ALERT_MONITOR_MINUTES,
        id="emergency_alert_monitor",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        send_due_pre_event_reminders,
        "interval",
        minutes=30,
        id="pre_event_reminders",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    readable_schedule = ", ".join(
        f"{item['label']} {item['hour']:02d}:{item['minute']:02d} {item['timezone'].key}"
        for item in REPORT_SCHEDULES
    )
    logging.info("Railway worker started. Schedule: %s", readable_schedule)
    scheduler.start()


def main() -> None:
    load_dotenv()
    setup_logging()

    require_env("TELEGRAM_BOT_TOKEN")
    require_env("TELEGRAM_CHAT_ID")
    require_env("OPENAI_API_KEY")

    if os.getenv("RUN_ON_START", "false").lower() == "true":
        send_report("morning")

    start_scheduler()


if __name__ == "__main__":
    main()
