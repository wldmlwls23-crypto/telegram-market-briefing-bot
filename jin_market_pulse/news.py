from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

import feedparser

from .models import NewsItem


UTC = timezone.utc
NEWS_QUERIES = [
    ("Federal Reserve OR CPI OR PCE OR jobs OR Treasury yields OR DXY when:1d", "en-US", "US", "US:en"),
    ("Nasdaq OR S&P 500 OR oil OR gold OR global markets when:1d", "en-US", "US", "US:en"),
    ("bitcoin OR ethereum OR crypto ETF OR Coinbase OR Binance OR stablecoin when:1d", "en-US", "US", "US:en"),
    ("KOSPI OR KOSDAQ OR 원달러 OR 외국인 투자자 OR 반도체 when:1d", "ko", "KR", "KR:ko"),
    ("war OR attack OR bank failure OR exchange hack OR circuit breaker when:1d", "en-US", "US", "US:en"),
]


def _feed_url(query: str, language: str, country: str, edition: str) -> str:
    params = urlencode({"q": query, "hl": language, "gl": country, "ceid": edition})
    return f"https://news.google.com/rss/search?{params}"


NEWS_FEEDS = [_feed_url(*query) for query in NEWS_QUERIES]

OFFICIAL_PUBLISHERS = {
    "federal reserve",
    "u.s. department of the treasury",
    "us treasury",
    "sec.gov",
    "u.s. securities and exchange commission",
    "bureau of labor statistics",
    "bureau of economic analysis",
    "white house",
    "bank of korea",
    "ecb",
    "bank of japan",
    "opec",
    "coinbase",
    "binance",
    "cme group",
}
TRUSTED_PUBLISHERS = {
    "reuters",
    "associated press",
    "ap news",
    "bloomberg",
    "financial times",
    "the wall street journal",
    "cnbc",
    "bbc",
    "the new york times",
    "coindesk",
    "the block",
    "연합뉴스",
    "한국경제",
    "매일경제",
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
    "could soar",
    "set to explode",
    "buy now",
    "strong buy",
}
HARD_SHOCK_TERMS = {
    "assassination",
    "shot",
    "attack",
    "military conflict",
    "invasion",
    "emergency rate",
    "bank failure",
    "circuit breaker",
    "depeg",
    "withdrawals suspended",
    "suspended withdrawals",
    "exchange hack",
    "hacked",
    "war begins",
    "급락",
    "급등",
    "피격",
    "전쟁",
    "출금 중단",
    "디페깅",
}


def _normalized(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+-\s+[^-]{2,50}$", "", value)
    return re.sub(r"\s+", " ", value.lower()).strip()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _publisher(entry: object, title: str) -> str:
    source = getattr(entry, "source", None)
    if source:
        source_title = source.get("title", "").strip()
        if source_title:
            return source_title
    match = re.search(r"\s+-\s+([^-]{2,60})$", title)
    return match.group(1).strip() if match else "Unknown"


def _published(entry: object) -> datetime | None:
    raw = getattr(entry, "published", "") or getattr(entry, "updated", "")
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _publisher_matches(publisher: str, allowed: set[str]) -> bool:
    normalized = publisher.lower()
    return any(term in normalized for term in allowed)


def topic_key(title: str, summary: str = "") -> str:
    text = _normalized(f"{title} {summary}")
    categories = [
        ("oil-geopolitical", {"oil", "wti", "crude"}, {"war", "attack", "iran", "israel", "opec"}),
        ("btc-sharp-move", {"bitcoin", "btc"}, {"crash", "plunge", "surge", "급락", "급등"}),
        ("eth-sharp-move", {"ethereum", "eth"}, {"crash", "plunge", "surge", "급락", "급등"}),
        ("stablecoin-risk", {"stablecoin", "usdt", "usdc"}, {"depeg", "디페깅"}),
        ("exchange-risk", {"coinbase", "binance", "exchange"}, {"hack", "withdrawal", "suspended", "출금"}),
        ("us-inflation", {"cpi", "pce", "inflation"}, {"actual", "released", "rose", "fell", "%"}),
        ("fed-shock", {"fomc", "powell", "federal reserve"}, {"rate", "emergency", "statement"}),
        ("bank-failure", {"bank"}, {"failure", "collapse", "failed"}),
    ]
    for label, subject_terms, event_terms in categories:
        if any(term in text for term in subject_terms) and any(term in text for term in event_terms):
            return label
    words = [
        word
        for word in re.findall(r"[a-z0-9가-힣]+", text)
        if len(word) > 3 and word not in {"with", "from", "that", "this", "after", "market"}
    ]
    return f"news-{_digest(' '.join(sorted(set(words[:12]))))}"


def fetch_news(max_per_feed: int = 8) -> list[NewsItem]:
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=30)
    result: list[NewsItem] = []
    seen: set[str] = set()
    for feed_url in NEWS_FEEDS:
        parsed = feedparser.parse(feed_url)
        for entry in parsed.entries[:max_per_feed]:
            title = str(entry.get("title", "")).strip()
            if not title:
                continue
            normalized = _normalized(title)
            if any(term in normalized for term in NOISE_TERMS):
                continue
            published_at = _published(entry)
            if published_at and published_at.astimezone(UTC) < cutoff:
                continue
            publisher = _publisher(entry, title)
            url = str(entry.get("link", "")).strip()
            summary = re.sub(r"<[^>]+>", " ", str(entry.get("summary", ""))).strip()
            news_id = _digest(f"{normalized}|{publisher.lower()}")
            if news_id in seen:
                continue
            seen.add(news_id)
            official = _publisher_matches(publisher, OFFICIAL_PUBLISHERS)
            trusted = official or _publisher_matches(publisher, TRUSTED_PUBLISHERS)
            if not trusted:
                continue
            result.append(
                NewsItem(
                    news_id=news_id,
                    topic_key=topic_key(title, summary),
                    title=title,
                    publisher=publisher,
                    published_at=published_at,
                    url=url,
                    summary=summary[:800],
                    official_source=official,
                    trusted_source=trusted,
                )
            )
    return sorted(
        result,
        key=lambda item: item.published_at or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )[:24]


def emergency_groups(news: list[NewsItem]) -> list[list[NewsItem]]:
    grouped: dict[str, list[NewsItem]] = defaultdict(list)
    for item in news:
        text = _normalized(f"{item.title} {item.summary}")
        if any(term in text for term in HARD_SHOCK_TERMS):
            grouped[item.topic_key].append(item)

    verified: list[list[NewsItem]] = []
    for items in grouped.values():
        publishers = {item.publisher.lower() for item in items}
        if any(item.official_source for item in items) or len(publishers) >= 2:
            verified.append(items)
    return sorted(
        verified,
        key=lambda items: max(
            (item.published_at or datetime.min.replace(tzinfo=UTC)) for item in items
        ),
        reverse=True,
    )
