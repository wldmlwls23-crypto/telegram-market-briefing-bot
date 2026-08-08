from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jin_market_pulse.providers import (
    btc_quote_from_series,
    critical_data_errors,
    fetch_asset_quote,
    fetch_btc_intraday_series,
    fetch_market_quotes,
    fetch_fred_treasury_quotes,
    fetch_treasury_quotes,
    fetch_yahoo_quote,
    _verify_outlier_directions,
)
from jin_market_pulse.state import StateStore


class FakeResponse:
    def __init__(self, payload=None, content: bytes = b"", text: str = ""):
        self._payload = payload
        self.content = content
        self.text = text

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_yahoo_uses_last_two_daily_closes_not_chart_range_start(monkeypatch, settings):
    timestamp = int(datetime.now(timezone.utc).timestamp())
    previous_timestamp = timestamp - 24 * 60 * 60
    payload = {
        "chart": {
            "result": [
                {
                    "meta": {
                        "regularMarketPrice": 97,
                        "chartPreviousClose": 80,
                        "previousClose": 85,
                        "regularMarketTime": timestamp,
                        "marketState": "CLOSED",
                        "exchangeTimezoneName": "UTC",
                        "currency": "USD",
                    },
                    "timestamp": [previous_timestamp, timestamp],
                    "indicators": {"quote": [{"close": [100, 97]}]},
                }
            ]
        }
    }
    monkeypatch.setattr(
        "jin_market_pulse.providers._session",
        lambda: type(
            "Session",
            (),
            {"get": lambda *args, **kwargs: FakeResponse(payload)},
        )(),
    )
    quote = fetch_yahoo_quote(
        "nasdaq100",
        {"symbol": "^NDX", "name": "Nasdaq 100", "kind": "index", "unit": "pt"},
        settings,
    )
    assert quote.previous == 100
    assert quote.absolute_change == -3
    assert quote.percent_change == -3
    assert quote.calculation_version == 2
    assert quote.reference_at == datetime.fromtimestamp(previous_timestamp, timezone.utc)


def test_critical_gate_requires_four_market_axes(market_data):
    assert critical_data_errors(market_data.quotes) == []
    market_data.quotes.pop("dxy")
    assert critical_data_errors(market_data.quotes) == ["DXY"]


def test_market_quotes_uses_yahoo_when_coingecko_is_rate_limited(
    monkeypatch,
    settings,
    market_data,
):
    fallback = {
        "btc": market_data.quotes["btc"],
        "eth": market_data.quotes["eth"],
    }
    monkeypatch.setattr(
        "jin_market_pulse.providers.fetch_crypto_quotes",
        lambda _settings: (_ for _ in ()).throw(RuntimeError("429")),
    )
    monkeypatch.setattr(
        "jin_market_pulse.providers.fetch_yahoo_crypto_quotes",
        lambda _settings: fallback,
    )
    monkeypatch.setattr("jin_market_pulse.providers.YAHOO_ASSETS", {})
    monkeypatch.setattr(
        "jin_market_pulse.providers.fetch_treasury_quotes",
        lambda _settings: {},
    )

    quotes, errors = fetch_market_quotes(settings)

    assert quotes["btc"].current == market_data.quotes["btc"].current
    assert quotes["eth"].current == market_data.quotes["eth"].current
    assert any(error.startswith("CoinGecko:") for error in errors)


def test_treasury_parses_latest_two_observations(monkeypatch, settings):
    today = datetime.now(timezone.utc).date()
    previous = today - timedelta(days=1)
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
 xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices"
 xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
  <entry><content><m:properties>
    <d:NEW_DATE>{previous.isoformat()}T00:00:00</d:NEW_DATE>
    <d:BC_2YEAR>4.10</d:BC_2YEAR><d:BC_10YEAR>4.20</d:BC_10YEAR>
  </m:properties></content></entry>
  <entry><content><m:properties>
    <d:NEW_DATE>{today.isoformat()}T00:00:00</d:NEW_DATE>
    <d:BC_2YEAR>4.15</d:BC_2YEAR><d:BC_10YEAR>4.30</d:BC_10YEAR>
  </m:properties></content></entry>
</feed>""".encode()
    monkeypatch.setattr(
        "jin_market_pulse.providers._session",
        lambda: type(
            "Session",
            (),
            {"get": lambda *args, **kwargs: FakeResponse(content=xml)},
        )(),
    )
    quotes = fetch_treasury_quotes(settings)
    assert quotes["us2y"].absolute_change == pytest.approx(0.05)
    assert round(quotes["us10y"].absolute_change * 100, 1) == 10.0


def test_btc_intraday_falls_back_to_fifteen_minutes(monkeypatch, settings):
    timestamp = int(datetime.now(timezone.utc).timestamp())
    calls = []

    def fake_intraday(_settings, *, interval):
        from jin_market_pulse.models import PricePoint, PriceSeries

        calls.append(interval)
        count = 10 if interval == "5m" else 24
        return PriceSeries(
            key="btc",
            name="BTC",
            source="Yahoo Finance",
            points=[
                PricePoint(
                    timestamp=datetime.fromtimestamp(timestamp + index * 300, timezone.utc),
                    value=100000 + index,
                )
                for index in range(count)
            ],
        )

    monkeypatch.setattr(
        "jin_market_pulse.providers._fetch_yahoo_intraday",
        fake_intraday,
    )

    series = fetch_btc_intraday_series(settings)

    assert calls == ["5m", "15m"]
    assert len(series.points) == 24


def test_btc_quote_uses_same_twenty_four_hour_series_as_chart():
    from jin_market_pulse.models import PricePoint, PriceSeries

    now = datetime.now(timezone.utc)
    series = PriceSeries(
        key="btc",
        name="BTC",
        source="Yahoo Finance",
        points=[
            PricePoint(timestamp=now - timedelta(hours=24), value=100000),
            PricePoint(timestamp=now, value=97000),
        ],
    )

    quote = btc_quote_from_series(series)

    assert quote.current == 97000
    assert quote.previous == 100000
    assert quote.percent_change == -3
    assert quote.comparison_label == "24시간 전"


def test_single_crypto_lookup_uses_fast_yahoo_path(
    monkeypatch,
    settings,
    market_data,
):
    calls = []

    def fake_yahoo(key, definition, _settings):
        calls.append((key, definition["symbol"]))
        return market_data.quotes[key]

    monkeypatch.setattr(
        "jin_market_pulse.providers.fetch_yahoo_quote",
        fake_yahoo,
    )

    quote = fetch_asset_quote("eth", settings)

    assert quote.key == "eth"
    assert quote.comparison_label == "직전 UTC 종가"
    assert calls == [("eth", "ETH-USD")]


def test_single_quote_falls_back_to_sqlite_cache(
    monkeypatch,
    settings,
    market_data,
):
    store = StateStore(settings.state_db)
    cached = market_data.quotes["btc"].model_copy(
        update={"source": "cached fixture", "calculation_version": 2}
    )
    store.cache_set(
        "quote:v2:btc",
        cached.model_dump(mode="json"),
        source=cached.source,
        ttl_seconds=60,
    )
    monkeypatch.setattr(
        "jin_market_pulse.providers.fetch_yahoo_quote",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("timeout")),
    )

    quote = fetch_asset_quote("btc", settings, store)

    assert "마지막 검증값" in quote.source
    assert "cached" in quote.quality_flags
    assert quote.validation_status == "last_verified"


def test_wti_outlier_is_rejected_when_proxy_moves_opposite(
    monkeypatch,
    settings,
    market_data,
):
    quote = market_data.quotes["wti"].model_copy(
        update={"percent_change": -8.0, "absolute_change": -8.0}
    )
    verifier = market_data.quotes["wti"].model_copy(
        update={
            "key": "wti_proxy",
            "name_ko": "WTI 대용 ETF",
            "percent_change": 2.0,
            "absolute_change": 2.0,
        }
    )
    monkeypatch.setattr(
        "jin_market_pulse.providers.fetch_yahoo_quote",
        lambda *_args: verifier,
    )
    errors = []

    _verify_outlier_directions({"wti": quote}, settings, errors)

    assert quote.verified is False
    assert any("방향 불일치" in flag for flag in quote.quality_flags)
    assert errors == ["wti: outlier direction mismatch"]


def test_fred_parses_latest_two_complete_rows(monkeypatch, settings):
    today = datetime.now(timezone.utc).date()
    previous = today - timedelta(days=1)
    csv_text = (
        "DATE,DGS2,DGS10\n"
        f"{previous.isoformat()},4.10,4.20\n"
        f"{today.isoformat()},4.15,4.30\n"
    )
    monkeypatch.setattr(
        "jin_market_pulse.providers._session",
        lambda: type(
            "Session",
            (),
            {"get": lambda *args, **kwargs: FakeResponse(text=csv_text)},
        )(),
    )

    quotes = fetch_fred_treasury_quotes(settings)

    assert quotes["us2y"].absolute_change == pytest.approx(0.05)
    assert quotes["us10y"].absolute_change == pytest.approx(0.10)
