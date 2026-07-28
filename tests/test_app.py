from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jin_market_pulse.app import MarketPulseApp
from jin_market_pulse.models import PricePoint, PriceSeries


def test_morning_sends_chart_before_html_text(
    settings,
    market_data,
    monkeypatch,
):
    start = datetime.now(timezone.utc) - timedelta(hours=24)
    market_data.btc_series = PriceSeries(
        key="btc",
        name="BTC",
        source="fixture",
        points=[
            PricePoint(
                timestamp=start + timedelta(minutes=index * 5),
                value=100000 + index,
            )
            for index in range(24)
        ],
    )
    app = MarketPulseApp(settings)
    events = []
    monkeypatch.setattr(app, "collect_morning_data", lambda: market_data)
    monkeypatch.setattr(
        "jin_market_pulse.app.create_morning_analysis",
        lambda data, _settings: __import__(
            "jin_market_pulse.models",
            fromlist=["MorningAnalysis"],
        ).MorningAnalysis(signals=[]),
    )
    monkeypatch.setattr(
        app.telegram,
        "send_photo",
        lambda content: events.append(("photo", len(content))),
    )
    monkeypatch.setattr(
        app.telegram,
        "send",
        lambda text, parse_mode=None: events.append(("text", parse_mode)),
    )

    assert app.send_morning_report() == "sent"
    assert events[0][0] == "photo"
    assert events[0][1] > 1000
    assert events[1] == ("text", "HTML")


def test_chart_failure_does_not_block_text_report(
    settings,
    market_data,
    monkeypatch,
):
    app = MarketPulseApp(settings)
    monkeypatch.setattr(app, "collect_morning_data", lambda: market_data)
    monkeypatch.setattr(
        "jin_market_pulse.app.create_morning_analysis",
        lambda data, _settings: __import__(
            "jin_market_pulse.models",
            fromlist=["MorningAnalysis"],
        ).MorningAnalysis(signals=[]),
    )
    sent = []
    monkeypatch.setattr(
        app.telegram,
        "send",
        lambda text, parse_mode=None: sent.append(parse_mode),
    )

    assert app.send_morning_report() == "sent"
    assert sent == ["HTML"]
