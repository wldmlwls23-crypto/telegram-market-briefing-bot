from __future__ import annotations

from datetime import datetime, timezone

import pytest

from jin_market_pulse.config import Settings
from jin_market_pulse.models import AssetQuote, EconomicEvent, MarketData, NewsItem


@pytest.fixture
def settings(tmp_path):
    return Settings(
        telegram_bot_token="test-token",
        telegram_chat_id="test-chat",
        openai_api_key="test-openai",
        openai_model="gpt-5.6",
        openai_reasoning_effort="medium",
        openai_web_search=False,
        fmp_api_key=None,
        state_dir=tmp_path,
        enabled_reports=("morning",),
        enable_emergency_alerts=False,
        run_on_start=False,
        request_timeout_seconds=5,
        telegram_webhook_secret="a-secure-webhook-secret",
    )


def make_quote(
    key: str,
    name: str,
    current: float,
    previous: float,
    *,
    kind: str = "index",
    source: str = "fixture",
) -> AssetQuote:
    absolute = current - previous
    return AssetQuote(
        key=key,
        name_ko=name,
        kind=kind,
        current=current,
        previous=previous,
        absolute_change=absolute,
        percent_change=absolute / previous * 100,
        as_of=datetime.now(timezone.utc),
        market_state="CLOSED",
        source=source,
        comparison_label="전일 종가",
        stale=False,
        unit="",
    )


@pytest.fixture
def market_data():
    now = datetime.now(timezone.utc).astimezone()
    quotes = {
        "btc": make_quote("btc", "BTC", 97000, 100000, kind="crypto"),
        "eth": make_quote("eth", "ETH", 3400, 3500, kind="crypto"),
        "nasdaq100": make_quote("nasdaq100", "Nasdaq 100", 19000, 19200),
        "sp500": make_quote("sp500", "S&P 500", 5300, 5350),
        "dow": make_quote("dow", "Dow Jones", 41000, 41200),
        "dxy": make_quote("dxy", "DXY", 104, 103, kind="fx"),
        "us2y": make_quote("us2y", "미국채 2년물", 4.1, 4.15, kind="yield"),
        "us10y": make_quote("us10y", "미국채 10년물", 4.3, 4.2, kind="yield"),
        "kospi": make_quote("kospi", "KOSPI", 3100, 3120),
        "kosdaq": make_quote("kosdaq", "KOSDAQ", 850, 860),
        "usdkrw": make_quote("usdkrw", "원/달러", 1380, 1375, kind="fx"),
        "wti": make_quote("wti", "WTI 유가", 68, 67, kind="commodity"),
        "gold": make_quote("gold", "금", 2400, 2380, kind="commodity"),
    }
    event = EconomicEvent(
        event_id="event-one",
        title="Core PCE Price Index m/m",
        title_ko="근원 PCE 물가",
        country="USD",
        country_ko="미국",
        event_time_kst=now,
        importance="★★★★★",
        forecast="0.2%",
        previous="0.1%",
        sensitivity_stronger="예상보다 높으면 인플레 부담과 달러·금리 상승 압력",
        sensitivity_weaker="예상보다 낮으면 인플레 부담 완화와 달러 약세 압력",
    )
    news = NewsItem(
        news_id="news-one",
        topic_key="fed-shock",
        title="Federal Reserve keeps rates unchanged - Reuters",
        publisher="Reuters",
        published_at=now,
        url="https://example.com/news",
        trusted_source=True,
    )
    return MarketData(
        generated_at_kst=now,
        quotes=quotes,
        events=[event],
        news=[news],
        errors=[],
    )
