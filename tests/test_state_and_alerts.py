from __future__ import annotations

from jin_market_pulse.alerts import _reaction_lines
from jin_market_pulse.state import StateStore


def test_state_persists_alert_cooldown(tmp_path):
    path = tmp_path / "sent_alerts.json"
    first = StateStore(path)
    first.mark_alert("oil-shock", "Oil shock", ["https://example.com"])
    second = StateStore(path)
    assert second.alert_in_cooldown("oil-shock", hours=6)
    assert second.recent_alert_count(minutes=30) == 1


def test_event_stage_and_snapshot_persist(tmp_path, market_data):
    state = StateStore(tmp_path / "state.json")
    state.save_event_snapshot("event-one", "before", market_data.quotes)
    state.update_event("event-one", stage="baseline_captured")
    record = state.event_record("event-one")
    assert record["stage"] == "baseline_captured"
    assert record["before_snapshot"]["btc"]["current"] == 97000


def test_reaction_uses_before_to_after_values(market_data):
    before = {
        "btc": {"current": 100000, "kind": "crypto"},
        "us10y": {"current": 4.2, "kind": "yield"},
    }
    lines = _reaction_lines(before, market_data.quotes)
    assert any("100,000.00" in line and "-3.00%" in line for line in lines)
    assert any("+10.0bp" in line for line in lines)
