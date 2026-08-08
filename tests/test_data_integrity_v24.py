from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from jin_market_pulse.models import AssetQuote
from jin_market_pulse.providers import (
    DataValidationError,
    fetch_asset_quote,
    fetch_verified_korea_quote,
    fetch_yahoo_quote,
)
from jin_market_pulse.reports import _analysis_input
from jin_market_pulse.session_reports import _fact_rows
from jin_market_pulse.server import create_app
from jin_market_pulse.state import StateStore


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _yahoo_payload(previous: float, current: float, *, split: float | None = None):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    prior = now - timedelta(days=1)
    result = {
        "meta": {
            "regularMarketPrice": current,
            "chartPreviousClose": previous * 1.25,
            "previousClose": previous * 1.1,
            "regularMarketTime": int(now.timestamp()),
            "marketState": "CLOSED",
            "exchangeTimezoneName": "UTC",
            "currency": "KRW",
        },
        "timestamp": [int(prior.timestamp()), int(now.timestamp())],
        "indicators": {"quote": [{"close": [previous, current]}]},
    }
    if split is not None:
        result["events"] = {
            "splits": {
                "split": {
                    "date": int(now.timestamp()),
                    "numerator": split,
                    "denominator": 1,
                }
            }
        }
    return {"chart": {"result": [result]}}


@pytest.mark.parametrize(
    ("key", "kind", "previous", "current", "expected"),
    [
        ("skhynix", "equity", 1_495_000, 1_422_000, -4.882943),
        ("samsung", "equity", 230_500, 231_000, 0.21692),
        ("kospi", "index", 6_296.38, 6_258.77, -0.597331),
        ("kosdaq", "index", 801.67, 798.81, -0.356755),
    ],
)
def test_regression_uses_one_session_change(
    monkeypatch, settings, key, kind, previous, current, expected
):
    monkeypatch.setattr(
        "jin_market_pulse.providers.request",
        lambda *_args, **_kwargs: FakeResponse(_yahoo_payload(previous, current)),
    )
    quote = fetch_yahoo_quote(
        key,
        {"symbol": key, "name": key, "kind": kind, "unit": "KRW"},
        settings,
    )

    assert quote.previous == pytest.approx(previous)
    assert quote.current == pytest.approx(current)
    assert quote.percent_change == pytest.approx(expected, abs=0.00001)
    assert quote.calculation_version == 2


def test_yahoo_rejects_range_metadata_when_daily_history_is_missing(
    monkeypatch, settings
):
    payload = _yahoo_payload(100, 97)
    payload["chart"]["result"][0]["timestamp"] = []
    payload["chart"]["result"][0]["indicators"] = {"quote": [{"close": []}]}
    monkeypatch.setattr(
        "jin_market_pulse.providers.request",
        lambda *_args, **_kwargs: FakeResponse(payload),
    )

    with pytest.raises(DataValidationError, match="fewer than two daily closes"):
        fetch_yahoo_quote(
            "nasdaq100",
            {"symbol": "^NDX", "name": "Nasdaq 100", "kind": "index", "unit": "pt"},
            settings,
        )


def test_yahoo_adjusts_reference_close_for_confirmed_split(monkeypatch, settings):
    monkeypatch.setattr(
        "jin_market_pulse.providers.request",
        lambda *_args, **_kwargs: FakeResponse(_yahoo_payload(100, 50, split=2)),
    )

    quote = fetch_yahoo_quote(
        "samsung",
        {"symbol": "005930.KS", "name": "Samsung", "kind": "equity", "unit": "KRW"},
        settings,
    )

    assert quote.previous == 50
    assert quote.percent_change == 0
    assert quote.quality_flags


def test_yahoo_does_not_double_adjust_already_normalized_split(
    monkeypatch, settings
):
    monkeypatch.setattr(
        "jin_market_pulse.providers.request",
        lambda *_args, **_kwargs: FakeResponse(_yahoo_payload(50, 51, split=2)),
    )

    quote = fetch_yahoo_quote(
        "samsung",
        {"symbol": "005930.KS", "name": "Samsung", "kind": "equity", "unit": "KRW"},
        settings,
    )

    assert quote.previous == 50
    assert quote.percent_change == pytest.approx(2.0)
    assert any("이미 보정" in flag for flag in quote.quality_flags)


def test_equity_rejects_unconfirmed_large_discontinuity(monkeypatch, settings):
    monkeypatch.setattr(
        "jin_market_pulse.providers.request",
        lambda *_args, **_kwargs: FakeResponse(_yahoo_payload(100, 50)),
    )

    with pytest.raises(DataValidationError, match="unconfirmed"):
        fetch_yahoo_quote(
            "skhynix",
            {"symbol": "000660.KS", "name": "SK Hynix", "kind": "equity", "unit": "KRW"},
            settings,
        )


def test_closed_future_uses_completed_daily_settlement_when_meta_disagrees(
    monkeypatch, settings
):
    payload = _yahoo_payload(4_242.0, 4_340.7)
    payload["chart"]["result"][0]["meta"]["regularMarketPrice"] = 4_399.7
    payload["chart"]["result"][0]["meta"]["marketState"] = None
    monkeypatch.setattr(
        "jin_market_pulse.providers.request",
        lambda *_args, **_kwargs: FakeResponse(payload),
    )

    quote = fetch_yahoo_quote(
        "gold",
        {"symbol": "GC=F", "name": "Gold", "kind": "commodity", "unit": "USD"},
        settings,
    )

    assert quote.current == pytest.approx(4_340.7)
    assert quote.previous == pytest.approx(4_242.0)
    assert "Yahoo 완료 일봉 종가 사용" in quote.quality_flags


def _korea_quote(source: str, current: float, previous: float) -> AssetQuote:
    change = current - previous
    return AssetQuote(
        key="kospi",
        name_ko="KOSPI",
        kind="index",
        current=current,
        previous=previous,
        absolute_change=change,
        percent_change=change / previous * 100,
        as_of=datetime.now(timezone.utc),
        market_state="CLOSE",
        source=source,
        comparison_label="previous close",
        stale=False,
        unit="pt",
        validation_sources=[source],
        calculation_version=2,
    )


def test_korea_quote_requires_naver_yahoo_tolerance(monkeypatch, settings):
    monkeypatch.setattr(
        "jin_market_pulse.providers.fetch_naver_korea_quote",
        lambda *_args: _korea_quote("Naver", 6_258.77, 6_296.38),
    )
    monkeypatch.setattr(
        "jin_market_pulse.providers.fetch_yahoo_quote",
        lambda *_args: _korea_quote("Yahoo", 6_258.78, 6_296.38),
    )

    quote = fetch_verified_korea_quote(
        "kospi",
        {"symbol": "^KS11", "name": "KOSPI", "kind": "index", "unit": "pt"},
        settings,
    )

    assert len(quote.validation_sources) == 2


def test_korea_quote_rejects_provider_disagreement(monkeypatch, settings):
    monkeypatch.setattr(
        "jin_market_pulse.providers.fetch_naver_korea_quote",
        lambda *_args: _korea_quote("Naver", 6_258.77, 6_296.38),
    )
    monkeypatch.setattr(
        "jin_market_pulse.providers.fetch_yahoo_quote",
        lambda *_args: _korea_quote("Yahoo", 6_300.00, 6_296.38),
    )

    with pytest.raises(DataValidationError, match="mismatch"):
        fetch_verified_korea_quote(
            "kospi",
            {"symbol": "^KS11", "name": "KOSPI", "kind": "index", "unit": "pt"},
            settings,
        )


def test_expired_last_verified_crypto_is_not_returned(
    monkeypatch, settings, market_data
):
    store = StateStore(settings.state_db)
    quote = market_data.quotes["btc"].model_copy(
        update={
            "as_of": datetime.now(timezone.utc) - timedelta(hours=2),
            "calculation_version": 2,
        }
    )
    store.cache_set(
        "quote:v2:btc",
        quote.model_dump(mode="json"),
        source=quote.source,
        ttl_seconds=60,
    )
    monkeypatch.setattr(
        "jin_market_pulse.providers.fetch_yahoo_quote",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("timeout")),
    )

    with pytest.raises(RuntimeError, match="timeout"):
        fetch_asset_quote("btc", settings, store)


def test_last_verified_quote_is_display_only_not_analysis_or_fact(
    market_data, tmp_path
):
    fallback = market_data.quotes["kospi"].model_copy(
        update={"validation_status": "last_verified", "calculation_version": 2}
    )
    market_data.quotes["kospi"] = fallback

    analysis_payload = __import__("json").loads(_analysis_input(market_data))
    analysis_keys = {item["key"] for item in analysis_payload["assets"]}
    fact_keys = {item["fact_key"] for item in _fact_rows({"kospi": fallback}, [])}

    assert "kospi" not in analysis_keys
    assert "asset:v2:kospi" not in fact_keys

    state = StateStore(tmp_path / "state.sqlite3")
    state.add_market_snapshot({"kospi": fallback})
    snapshot = state.latest_market_snapshot()
    assert snapshot is not None
    assert "kospi" not in snapshot["quotes"]


def test_data_audit_endpoint_is_secret_protected(settings, monkeypatch):
    secured = replace(settings, cron_secret="a-secure-audit-secret")
    monkeypatch.setattr(
        "jin_market_pulse.server.audit_market_data",
        lambda *_args: {"calculation_version": 2, "assets": {}, "errors": []},
    )
    client = TestClient(create_app(secured))

    assert client.post("/jobs/data-audit").status_code == 401
    response = client.post(
        "/jobs/data-audit",
        headers={"Authorization": "Bearer a-secure-audit-secret"},
    )
    assert response.status_code == 200
    assert response.json()["calculation_version"] == 2
