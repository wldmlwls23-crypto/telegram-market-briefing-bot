from __future__ import annotations

from datetime import datetime, timedelta

from jin_market_pulse.calendar import (
    _official_bea_events,
    _official_bls_events,
    fetch_economic_events,
)
from jin_market_pulse.config import KST
from jin_market_pulse.http_client import ProviderRequestError


class FakeCalendarResponse:
    def __init__(self, payload=None, text=""):
        self.payload = payload
        self.text = text

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


def test_calendar_live_request_uses_compatible_headers_and_cache_buster(
    monkeypatch,
    settings,
):
    now = datetime.now(KST)
    captured = {}
    payload = [
        {
            "title": "Consumer Price Index y/y",
            "country": "USD",
            "date": (now + timedelta(hours=2)).isoformat(),
            "impact": "High",
            "forecast": "2.7%",
            "previous": "2.6%",
        }
    ]

    def fake_get(*_args, **kwargs):
        captured.update(kwargs)
        return FakeCalendarResponse(payload)

    monkeypatch.setattr("jin_market_pulse.calendar.requests.get", fake_get)

    assert fetch_economic_events(settings, days_ahead=1)
    assert "JIN-Market-Pulse" in captured["headers"]["User-Agent"]
    assert captured["headers"]["Cache-Control"] == "no-cache"
    assert isinstance(captured["params"]["_"], int)


def test_calendar_uses_live_tradingview_fallback_with_result_values(
    monkeypatch,
    settings,
):
    now = datetime.now(KST)
    payload = {
        "result": [
            {
                "title": "Core PCE Price Index YoY",
                "currency": "USD",
                "date": (now - timedelta(minutes=30)).isoformat(),
                "importance": 1,
                "actual": 3.2,
                "forecast": 3.1,
                "previous": 3.0,
                "unit": "%",
                "scale": None,
            }
        ]
    }

    def fake_request(_method, url, *_args, **_kwargs):
        if "faireconomy" in url:
            raise ProviderRequestError("economic_calendar", "blocked")
        return FakeCalendarResponse(payload)

    monkeypatch.setattr("jin_market_pulse.calendar.request", fake_request)

    events = fetch_economic_events(
        settings,
        lookback_hours=2,
        days_ahead=0,
    )

    assert len(events) == 1
    assert events[0].actual == "3.2%"
    assert events[0].forecast == "3.1%"
    assert events[0].previous == "3%"
    assert events[0].source == "TradingView economic calendar"


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


def test_bls_official_calendar_parses_high_impact_release(
    monkeypatch,
    settings,
):
    ical = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART;TZID=US-Eastern:20260807T083000
SUMMARY:Employment Situation
END:VEVENT
END:VCALENDAR"""
    monkeypatch.setattr(
        "jin_market_pulse.calendar.request",
        lambda *args, **kwargs: FakeCalendarResponse(text=ical),
    )

    events = _official_bls_events(settings, None)

    assert len(events) == 1
    assert events[0]["source"] == "U.S. Bureau of Labor Statistics"
    assert events[0]["date"].endswith("-04:00")


def test_bea_official_schedule_parses_release_rows(
    monkeypatch,
    settings,
):
    body = """
    <tr class="scheduled-releases-type-press">
      <td><div class="release-date">August 4</div>
      <small class="text-muted">8:30 AM</small></td>
      <td class="release-title views-field views-field-field-scheduled-releases-type">
        U.S. International Trade in Goods and Services, June 2026
      </td>
    </tr>
    """
    monkeypatch.setattr(
        "jin_market_pulse.calendar.request",
        lambda *args, **kwargs: FakeCalendarResponse(text=body),
    )

    events = _official_bea_events(settings, None)

    assert len(events) == 1
    assert events[0]["source"] == "U.S. Bureau of Economic Analysis"
