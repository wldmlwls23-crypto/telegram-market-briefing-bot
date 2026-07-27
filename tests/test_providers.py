from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jin_market_pulse.providers import (
    critical_data_errors,
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
