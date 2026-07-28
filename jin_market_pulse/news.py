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
    ("Federal Reserve CPI PCE jobs Treasury yields DXY when:1d", "en-US", "US", "US:en"),
    ("Nasdaq S&P 500 oil gold global markets when:1d", "en-US", "US", "US:en"),
    ("bitcoin ethereum crypto ETF stablecoin exchange hack when:1d", "en-US", "US", "US:en"),
    ("KOSPI KOSDAQ 원달러 외국인 반도체 when:1d", "ko", "KR", "KR:ko"),
    ("war attack bank failure circuit breaker depeg when:1d", "en-US", "US", "US:en"),
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
    "cme group",
}
PRIMARY_PUBLISHERS = {
    "reuters",
    "associated press",
    "ap news",
    "bloomberg",
    "financial times",
    "the wall street journal",
    "wsj",
}
SECONDARY_PUBLISHERS = {
    "cnbc",
    "bbc",
    "the new york times",
    "nikkei",
    "연합뉴스",
    "한국경제",
    "매일경제",
    "coindesk",
    "the block",
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
    "전망",
    "목표가",
}
HARD_SHOCK_TERMS = {
    "assassination",
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
    "긴급 인하",
    "서킷브레이커",
    "디페그",
    "출금 중단",
    "해킹",
}

ASSET_TERMS = {
    "btc": {"bitcoin", "btc", "비트코인"},
    "eth": {"ethereum", "eth", "이더리움"},
    "nasdaq100": {"nasdaq", "tech stocks", "기술주"},
    "sp500": {"s&p 500", "wall street", "미국 증시"},
    "dxy": {"dollar", "dxy", "달러"},
    "us10y": {"treasury yield", "bond yield", "미국채", "금리"},
    "kospi": {"kospi", "코스피", "한국 증시"},
    "kosdaq": {"kosdaq", "코스닥"},
    "wti": {"oil", "crude", "wti", "유가", "원유"},
    "gold": {"gold", "금값"},
    "usdkrw": {"won", "usd/krw", "원달러", "원화"},
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


def source_tier(publisher: str) -> int:
    if _publisher_matches(publisher, OFFICIAL_PUBLISHERS):
        return 0
    if _publisher_matches(publisher, PRIMARY_PUBLISHERS):
        return 1
    if _publisher_matches(publisher, SECONDARY_PUBLISHERS):
        return 2
    return 3


def relevant_assets(title: str, summary: str = "") -> list[str]:
    normalized = _normalized(f"{title} {summary}")
    return [
        key
        for key, terms in ASSET_TERMS.items()
        if any(term in normalized for term in terms)
    ]


def topic_key(
    title: str,
    summary: str = "",
    published_at: datetime | None = None,
) -> str:
    text = _normalized(f"{title} {summary}")
    categories = [
        ("oil-geopolitical", {"oil", "wti", "crude", "유가"}, {"war", "attack", "iran", "israel", "opec", "전쟁"}),
        ("btc-sharp-move", {"bitcoin", "btc", "비트코인"}, {"crash", "plunge", "surge", "급락", "급등"}),
        ("eth-sharp-move", {"ethereum", "eth", "이더리움"}, {"crash", "plunge", "surge", "급락", "급등"}),
        ("stablecoin-risk", {"stablecoin", "usdt", "usdc"}, {"depeg", "디페그"}),
        ("exchange-risk", {"coinbase", "binance", "exchange", "거래소"}, {"hack", "withdrawal", "suspended", "출금", "해킹"}),
        ("us-inflation", {"cpi", "pce", "inflation", "물가"}, {"actual", "released", "rose", "fell", "%", "발표"}),
        ("fed-shock", {"fomc", "powell", "federal reserve", "연준"}, {"rate", "emergency", "statement", "금리"}),
        ("bank-failure", {"bank", "은행"}, {"failure", "collapse", "failed", "파산"}),
    ]
    bucket = (published_at or datetime.now(UTC)).strftime("%Y%m%d%H")
    for label, subject_terms, event_terms in categories:
        if any(term in text for term in subject_terms) and any(term in text for term in event_terms):
            return f"{label}:{bucket[:8]}"
    words = [
        word
        for word in re.findall(r"[a-z0-9가-힣]+", text)
        if len(word) > 2
        and word not in {"with", "from", "that", "this", "after", "market", "시장"}
    ]
    # Shared nouns and the date bucket collapse Korean/English headlines about
    # the same institution, action and asset without relying on exact wording.
    return f"news-{_digest('|'.join(sorted(set(words[:16]))) + '|' + bucket[:8])}"


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
            tier = source_tier(publisher)
            if tier >= 3:
                continue
            url = str(entry.get("link", "")).strip()
            summary = re.sub(r"<[^>]+>", " ", str(entry.get("summary", ""))).strip()
            news_id = _digest(f"{normalized}|{publisher.lower()}")
            if news_id in seen:
                continue
            seen.add(news_id)
            official = tier == 0
            result.append(
                NewsItem(
                    news_id=news_id,
                    topic_key=topic_key(title, summary, published_at),
                    title=title,
                    publisher=publisher,
                    published_at=published_at,
                    url=url,
                    summary=summary[:800],
                    official_source=official,
                    trusted_source=tier <= 2,
                    source_tier=tier,
                    relevant_asset_keys=relevant_assets(title, summary),
                )
            )
    return sorted(
        result,
        key=lambda item: item.published_at or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )[:24]


def verified_topic_groups(news: list[NewsItem]) -> list[list[NewsItem]]:
    grouped: dict[str, list[NewsItem]] = defaultdict(list)
    for item in news:
        grouped[item.topic_key].append(item)
    verified: list[list[NewsItem]] = []
    for items in grouped.values():
        publishers = {item.publisher.lower() for item in items}
        if any(item.official_source for item in items) or len(publishers) >= 2:
            verified.append(items)
    return verified


def emergency_groups(news: list[NewsItem]) -> list[list[NewsItem]]:
    candidates = []
    for items in verified_topic_groups(news):
        text = _normalized(
            " ".join(f"{item.title} {item.summary}" for item in items)
        )
        if any(term in text for term in HARD_SHOCK_TERMS):
            candidates.append(items)
    return sorted(
        candidates,
        key=lambda items: max(
            (item.published_at or datetime.min.replace(tzinfo=UTC))
            for item in items
        ),
        reverse=True,
    )
