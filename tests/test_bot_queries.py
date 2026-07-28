from __future__ import annotations

from jin_market_pulse.bot_queries import answer_market_query


def test_natural_korean_asset_query_returns_verified_quote(
    settings,
    market_data,
    monkeypatch,
):
    monkeypatch.setattr(
        "jin_market_pulse.bot_queries.fetch_asset_quote",
        lambda key, _settings: market_data.quotes[key],
    )

    answer = answer_market_query("이더리움 변동 알려줘", settings)

    assert "<b>ETH</b>" in answer
    assert "$3,400" in answer
    assert "fixture" in answer


def test_unknown_query_returns_numeric_help(settings):
    answer = answer_market_query("비트 왜 오를까?", settings)

    assert "숫자 조회 사용법" in answer
    assert "/price gold" in answer
