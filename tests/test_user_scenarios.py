from __future__ import annotations

import json
from pathlib import Path

import pytest

from jin_market_pulse.bot_queries import route_query


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "user_scenarios.json"


def _scenarios():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_fixture_contains_at_least_150_real_user_scenarios():
    scenarios = _scenarios()
    assert len(scenarios) >= 150
    assert len({item["id"] for item in scenarios}) == len(scenarios)


@pytest.mark.parametrize(
    "scenario",
    _scenarios(),
    ids=lambda item: f"{item['id']}-{item['intent']}",
)
def test_deterministic_router_contract(scenario):
    route = route_query(
        scenario["text"],
        scenario.get("context"),
    )

    assert route.intent == scenario["intent"]
    if "assets" in scenario:
        assert route.asset_keys == scenario["assets"]
    if "period" in scenario:
        assert route.period == scenario["period"]
    if "candidates" in scenario:
        assert route.candidates == scenario["candidates"]
