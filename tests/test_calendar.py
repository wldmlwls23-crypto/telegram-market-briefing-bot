from __future__ import annotations

from datetime import datetime, timedelta

from jin_market_pulse.calendar import fetch_economic_events
from jin_market_pulse.config import KST


class FakeCalendarResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_calendar_excludes_past_and_low_impact(monkeypatch, settings):
    now = datetime.now(KST)
    payload = [
        {
            "title": "Core PCE Price Index m/m",
            "country": "USD",
            "date": (now - timedelta(hours=1)).isoformat(),
            "impact": "High",
            "forecast": "0.2%",
            "previous": "0.1%",
        },
        {
            "title": "Core PCE Price Index m/m",
            "country": "USD",
            "date": (now + timedelta(hours=2)).isoformat(),
            "impact": "High",
            "forecast": "0.2%",
            "previous": "0.1%",
        },
        {
            "title": "Minor Housing Indicator",
            "country": "AUD",
            "date": (now + timedelta(hours=3)).isoformat(),
            "impact": "Low",
            "forecast": "1",
            "previous": "2",
        },
    ]
    monkeypatch.setattr(
        "jin_market_pulse.calendar.requests.get",
        lambda *args, **kwargs: FakeCalendarResponse(payload),
    )
    events = fetch_economic_events(settings, days_ahead=1)
    assert len(events) == 1
    assert events[0].title_ko == "근원 PCE 물가"
    assert events[0].value_summary == "예상 0.2% / 이전 0.1%"


def test_calendar_lookback_keeps_actual_result(monkeypatch, settings):
    now = datetime.now(KST)
    payload = [
        {
            "title": "Consumer Price Index y/y",
            "country": "USD",
            "date": (now - timedelta(hours=2)).isoformat(),
            "impact": "High",
            "actual": "2.8%",
            "forecast": "2.7%",
            "previous": "2.6%",
        }
    ]
    monkeypatch.setattr(
        "jin_market_pulse.calendar.requests.get",
        lambda *args, **kwargs: FakeCalendarResponse(payload),
    )
    events = fetch_economic_events(settings, lookback_hours=12, days_ahead=0)
    assert len(events) == 1
    assert events[0].actual == "2.8%"


def test_non_us_inflation_uses_country_market_axis(monkeypatch, settings):
    now = datetime.now(KST)
    payload = [
        {
            "title": "Consumer Price Index y/y",
            "country": "JPY",
            "date": (now + timedelta(hours=2)).isoformat(),
            "impact": "High",
            "forecast": "1.4%",
            "previous": "1.3%",
        }
    ]
    monkeypatch.setattr(
        "jin_market_pulse.calendar.requests.get",
        lambda *args, **kwargs: FakeCalendarResponse(payload),
    )
    event = fetch_economic_events(settings, days_ahead=1)[0]
    assert "일본 통화·금리" in event.sensitivity_stronger
    assert "달러·미국채" not in event.sensitivity_stronger
