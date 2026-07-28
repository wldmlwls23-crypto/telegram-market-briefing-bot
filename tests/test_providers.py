from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jin_market_pulse.providers import (
    btc_quote_from_series,
    critical_data_errors,
    fetch_asset_quote,
    fetch_btc_intraday_series,
    fetch_market_quotes,
    fetch_treasury_quotes,
    fetch_yahoo_quote,
)


class FakeResponse:
    def __init__(self, payload=None, content: bytes = b""):
        self._payload = payload
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_yahoo_uses_previous_close_not_open(monkeypatch, settings):
    timestamp = int(datetime.now(timezone.utc).timestamp())
    payload = {
        "chart": {
            "result": [
                {
                    "meta": {
                        "regularMarketPrice": 97,
                        "chartPreviousClose": 100,
                        "regularMarketTime": timestamp,
                        "marketState": "CLOSED",
                    }
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
