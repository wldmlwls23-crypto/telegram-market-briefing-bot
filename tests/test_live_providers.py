from __future__ import annotations

import os

import pytest

from jin_market_pulse.providers import critical_data_errors, fetch_market_quotes


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_TESTS", "").lower() != "true",
    reason="Set RUN_LIVE_TESTS=true to call public market data providers.",
)
def test_live_critical_market_data(settings):
    quotes, errors = fetch_market_quotes(settings)
    assert critical_data_errors(quotes) == [], errors
