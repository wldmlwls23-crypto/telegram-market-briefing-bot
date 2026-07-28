from __future__ import annotations

import os
from datetime import datetime

import pytest

from jin_market_pulse.calendar import fetch_economic_events
from jin_market_pulse.config import KST
from jin_market_pulse.providers import critical_data_errors, fetch_market_quotes


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_TESTS", "").lower() != "true",
    reason="Set RUN_LIVE_TESTS=true to call public market data providers.",
)
@pytest.mark.live
def test_live_critical_market_data(settings):
    quotes, errors = fetch_market_quotes(settings)
    assert critical_data_errors(quotes) == [], errors


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_TESTS", "").lower() != "true",
    reason="Set RUN_LIVE_TESTS=true to call public calendar providers.",
)
@pytest.mark.live
def test_live_calendar_has_only_future_verified_events(settings):
    events = fetch_economic_events(settings, days_ahead=14)

    assert events
    assert all(event.event_time_kst >= datetime.now(KST) for event in events)
    assert all(event.source for event in events)
