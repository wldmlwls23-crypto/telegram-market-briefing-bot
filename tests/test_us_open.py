from __future__ import annotations

from datetime import datetime, timezone

from jin_market_pulse.config import KST, NEW_YORK
from jin_market_pulse.models import AssetQuote
from jin_market_pulse.state import StateStore
from jin_market_pulse.us_open import (
    build_us_open_preview,
    is_us_equity_session,
    next_us_open_preview_time,
    us_open_preview_due,
)


def _quote(
    key: str,
    current: float,
    previous: float,
    *,
    kind: str = "index",
) -> AssetQuote:
    change = current - previous
    return AssetQuote(
        key=key,
        name_ko=key,
        kind=kind,
        current=current,
        previous=previous,
        absolute_change=change,
        percent_change=change / previous * 100,
        as_of=datetime.now(timezone.utc),
        market_state="PRE",
        source="fixture",
        validation_status="verified",
        validation_sources=["fixture"],
        calculation_version=2,
    )


def test_us_open_preview_due_uses_new_york_clock_and_dst():
    summer = datetime(2026, 7, 6, 9, 5, tzinfo=NEW_YORK)
    winter = datetime(2026, 12, 7, 9, 5, tzinfo=NEW_YORK)

    assert us_open_preview_due(summer)
    assert us_open_preview_due(winter)
    assert summer.astimezone(KST).hour == 22
    assert winter.astimezone(KST).hour == 23


def test_us_open_preview_skips_observed_independence_day():
    assert not is_us_equity_session(datetime(2026, 7, 3).date())
    next_run = next_us_open_preview_time(
        datetime(2026, 7, 2, 10, 0, tzinfo=NEW_YORK)
    )
    assert next_run.date().isoformat() == "2026-07-06"


def test_us_open_preview_is_conditional_and_mobile_sized(
    settings,
    tmp_path,
    monkeypatch,
):
    state = StateStore(tmp_path / "state.sqlite3")
    quotes = {
        "nasdaq_futures": _quote("nasdaq_futures", 20200, 20000),
        "dxy": _quote("dxy", 100.1, 100.0, kind="fx"),
        "us10y": _quote("us10y", 4.31, 4.30, kind="yield"),
        "btc": _quote("btc", 100500, 100000, kind="crypto"),
    }
    monkeypatch.setattr(
        "jin_market_pulse.us_open.fetch_asset_quote",
        lambda key, *_args: quotes[key],
    )
    monkeypatch.setattr(
        "jin_market_pulse.us_open.fetch_economic_events",
        lambda *_args, **_kwargs: [],
    )

    result = build_us_open_preview(
        settings,
        state,
        now=datetime(2026, 7, 6, 9, 5, tzinfo=NEW_YORK),
    )

    assert result.status == "ready"
    assert "[미국장 개장 전]" in result.text
    assert "Nasdaq 선물" in result.text
    assert len(result.text) <= 750
    assert all(value not in result.text for value in ("|", "#", "N/A", "확인 필요"))


def test_us_open_preview_skips_quiet_market(settings, tmp_path, monkeypatch):
    state = StateStore(tmp_path / "state.sqlite3")
    quotes = {
        "nasdaq_futures": _quote("nasdaq_futures", 20020, 20000),
        "dxy": _quote("dxy", 100.1, 100.0, kind="fx"),
        "us10y": _quote("us10y", 4.31, 4.30, kind="yield"),
        "btc": _quote("btc", 100500, 100000, kind="crypto"),
    }
    monkeypatch.setattr(
        "jin_market_pulse.us_open.fetch_asset_quote",
        lambda key, *_args: quotes[key],
    )
    monkeypatch.setattr(
        "jin_market_pulse.us_open.fetch_economic_events",
        lambda *_args, **_kwargs: [],
    )

    result = build_us_open_preview(
        settings,
        state,
        now=datetime(2026, 7, 6, 9, 5, tzinfo=NEW_YORK),
    )

    assert result.status == "skipped"
    assert result.skip_reason == "개장 전 특이사항 없음"
