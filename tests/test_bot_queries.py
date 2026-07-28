from __future__ import annotations

from dataclasses import replace

from jin_market_pulse.bot_queries import answer_market_query
from jin_market_pulse.state import StateStore


def test_natural_korean_asset_query_returns_verified_quote(
    settings,
    market_data,
    monkeypatch,
):
    monkeypatch.setattr(
        "jin_market_pulse.bot_queries.fetch_asset_quote",
        lambda key, _settings: market_data.quotes[key],
    )

    answer = answer_market_query("지금 이더리움 가격 얼마야", settings)

    assert "<b>ETH</b>" in answer
    assert "$3,400" in answer
    assert "fixture" in answer


def test_dxy_definition_uses_free_built_in_explanation(settings):
    answer = answer_market_query("DXY가 뭐야? 쉽게 설명해줘", settings)

    assert "<b>DXY(달러지수)</b>" in answer
    assert "주요 통화 묶음" in answer
    assert "Nasdaq·BTC" in answer


def test_relationship_question_uses_limited_ai_advisor(
    settings,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "jin_market_pulse.bot_queries.create_advisor_answer",
        lambda question, _settings: f"<b>AI</b>\n{question}",
    )
    store = StateStore(tmp_path / "state.json")

    answer = answer_market_query(
        "DXY와 비트는 어떤 관계야? 설명해줘",
        settings,
        store,
    )

    assert answer.startswith("<b>AI</b>")


def test_ai_advisor_daily_limit_is_enforced(
    settings,
    tmp_path,
    monkeypatch,
):
    limited = replace(settings, ai_advisor_daily_limit=1)
    monkeypatch.setattr(
        "jin_market_pulse.bot_queries.create_advisor_answer",
        lambda question, _settings: "<b>AI 설명</b>",
    )
    store = StateStore(tmp_path / "state.json")

    first = answer_market_query("달러와 금리는 어떤 관계야?", limited, store)
    second = answer_market_query("주식과 유동성은 어떤 관계야?", limited, store)

    assert first == "<b>AI 설명</b>"
    assert "오늘의 AI 설명 횟수" in second


def test_current_cause_question_uses_verified_context_before_price_lookup(
    settings,
    market_data,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "jin_market_pulse.bot_queries.fetch_market_quotes",
        lambda _settings: (market_data.quotes.copy(), []),
    )
    monkeypatch.setattr(
        "jin_market_pulse.bot_queries.fetch_news",
        lambda max_per_feed=5: market_data.news,
    )
    monkeypatch.setattr(
        "jin_market_pulse.bot_queries.create_current_move_answer",
        lambda question, target, quotes, news, _settings: (
            f"<b>현재 움직임 설명</b>\n{target.key}:{question}"
        ),
    )

    answer = answer_market_query(
        "지금 비트가 왜 떨어져?",
        settings,
        StateStore(tmp_path / "state.json"),
    )

    assert answer.startswith("<b>현재 움직임 설명</b>")
    assert "btc:지금 비트가 왜 떨어져?" in answer


def test_cause_intent_wins_even_when_question_contains_price_word(
    settings,
    market_data,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "jin_market_pulse.bot_queries.fetch_market_quotes",
        lambda _settings: (market_data.quotes.copy(), []),
    )
    monkeypatch.setattr(
        "jin_market_pulse.bot_queries.fetch_news",
        lambda max_per_feed=5: [],
    )
    monkeypatch.setattr(
        "jin_market_pulse.bot_queries.create_current_move_answer",
        lambda question, target, quotes, news, _settings: (
            "<b>현재 움직임 설명</b>\n원인 분석"
        ),
    )

    answer = answer_market_query(
        "코스피 왜 이렇게 가격이 떨어지는지 이유에 대해 알려줘",
        settings,
        StateStore(tmp_path / "state.json"),
    )

    assert answer == "<b>현재 움직임 설명</b>\n원인 분석"


def test_trading_prediction_is_refused(settings):
    answer = answer_market_query("비트 지금 사도 될까?", settings)

    assert "매수·매도 결정과 가격 예측은 제공하지 않습니다" in answer
