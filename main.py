import json
import logging
import os
import sys
from datetime import datetime
from typing import Any

import feedparser
import pytz
import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from openai import OpenAI


KST = pytz.timezone("Asia/Seoul")
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

NEWS_FEEDS = [
    ("Global Markets", "https://news.google.com/rss/search?q=global%20markets%20stocks%20bonds%20dollar%20when:1d&hl=en-US&gl=US&ceid=US:en"),
    ("Crypto", "https://news.google.com/rss/search?q=bitcoin%20ethereum%20crypto%20market%20when:1d&hl=en-US&gl=US&ceid=US:en"),
    ("Korea Markets", "https://news.google.com/rss/search?q=Korea%20markets%20economy%20KOSPI%20when:1d&hl=ko&gl=KR&ceid=KR:ko"),
]

CRYPTO_IDS = ["bitcoin", "ethereum", "solana", "ripple"]
INDEX_SYMBOLS = {
    "S&P 500": "^spx",
    "Nasdaq 100": "^ndx",
    "Dow Jones": "^dji",
    "KOSPI": "^kospi",
    "USD/KRW": "usdkrw",
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


def fetch_news() -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for source, url in NEWS_FEEDS:
        parsed = feedparser.parse(url)
        for entry in parsed.entries[:5]:
            items.append(
                {
                    "source": source,
                    "title": entry.get("title", "").strip(),
                    "link": entry.get("link", "").strip(),
                    "published": entry.get("published", "").strip(),
                }
            )
    return items[:12]


def fetch_crypto_prices() -> dict[str, Any]:
    response = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={
            "ids": ",".join(CRYPTO_IDS),
            "vs_currencies": "usd",
            "include_24hr_change": "true",
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
                    "date": data.get("Date", "N/A"),
                    "time": data.get("Time", "N/A"),
                }
            )
        except Exception as exc:
            results.append({"name": name, "close": f"fetch failed: {exc}", "date": "", "time": ""})
    return results


def fetch_economic_calendar() -> list[dict[str, str]]:
    response = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=20)
    response.raise_for_status()
    events = response.json()

    today = datetime.now(KST).date()
    filtered = []
    for event in events:
        event_time = event.get("date", "")
        try:
            event_dt = datetime.fromisoformat(event_time.replace("Z", "+00:00")).astimezone(KST)
        except ValueError:
            continue

        if event_dt.date() == today and event.get("impact") in {"High", "Medium"}:
            filtered.append(
                {
                    "time_kst": event_dt.strftime("%H:%M"),
                    "country": event.get("country", ""),
                    "title": event.get("title", ""),
                    "impact": event.get("impact", ""),
                    "forecast": str(event.get("forecast", "")),
                    "previous": str(event.get("previous", "")),
                }
            )
    return filtered[:10]


def collect_market_data() -> dict[str, Any]:
    return {
        "generated_at_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "news": fetch_news(),
        "economic_calendar": fetch_economic_calendar(),
        "crypto_prices": fetch_crypto_prices(),
        "major_indices": fetch_major_indices(),
    }


def create_briefing(market_data: dict[str, Any]) -> str:
    client = OpenAI(api_key=require_env("OPENAI_API_KEY"))
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    raw_data = json.dumps(market_data, ensure_ascii=False, indent=2)

    prompt = f"""
당신은 한국 개인투자자를 위한 시장 브리핑 에디터입니다.
아래 원자료에서 중복, 광고성 표현, 가격 변동과 무관한 잡음을 제거하고 핵심만 요약하세요.

필수 형식:
[Signal vs Noise]
- 시장에 실제 영향을 줄 가능성이 높은 신호 3~5개
- 노이즈 또는 아직 확인이 필요한 내용은 짧게 분리

[Economic Calendar]
- 한국시간 기준 오늘 중요한 경제 일정
- 일정이 없으면 "오늘 확인된 고중요 일정 없음"이라고 작성

[Crypto / Macro / Stocks]
- 코인, 매크로, 주식/지수 흐름을 나누어 요약

[오늘 중요한 것]
- 사용자가 오늘 반드시 지켜봐야 할 3가지만 번호로 정리

규칙:
- 한국어로 작성
- 과장하지 말 것
- 투자 조언처럼 단정하지 말 것
- 3,500자 이하
- 출처 링크는 꼭 필요한 경우에만 짧게 포함

원자료:
{raw_data}
""".strip()

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You write concise Korean market briefings."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("OpenAI returned an empty response.")
    return content.strip()


def send_market_briefing() -> None:
    try:
        market_data = collect_market_data()
        briefing = create_briefing(market_data)
        title = f"시장 브리핑 | {market_data['generated_at_kst']} KST"
        send_telegram_message(f"{title}\n\n{briefing}")
        logging.info("Briefing sent successfully.")
    except Exception as exc:
        logging.exception("Failed to send briefing.")
        try:
            send_telegram_message(
                f"[브리핑 봇 오류]\n{datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST\n{exc}"
            )
        except Exception:
            logging.exception("Failed to send Telegram error message.")


def start_scheduler() -> None:
    scheduler = BlockingScheduler(timezone=KST)
    schedule_times = [(6, 50), (12, 0), (18, 0), (23, 30)]

    for hour, minute in schedule_times:
        scheduler.add_job(
            send_market_briefing,
            CronTrigger(hour=hour, minute=minute, timezone=KST),
            id=f"briefing_{hour:02d}_{minute:02d}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )

    logging.info("Railway worker started. Briefings run at 06:50, 12:00, 18:00, 23:30 KST.")
    scheduler.start()


def main() -> None:
    load_dotenv()
    setup_logging()

    require_env("TELEGRAM_BOT_TOKEN")
    require_env("TELEGRAM_CHAT_ID")
    require_env("OPENAI_API_KEY")

    if os.getenv("RUN_ON_START", "false").lower() == "true":
        send_market_briefing()

    start_scheduler()


if __name__ == "__main__":
    main()
