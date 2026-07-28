from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

from jin_market_pulse.models import MorningAnalysis, NewsItem, SignalSelection
from jin_market_pulse.reports import (
    _validated_analysis,
    REPORT_LIMIT,
    fallback_morning_analysis,
    format_quote,
    render_morning_report,
    select_future_events,
    select_report_assets,
    validate_rendered_report,
)
from jin_market_pulse.telegram import split_message


def test_quote_shows_verified_baseline(market_data):
    text = format_quote(market_data.quotes["nasdaq100"])
    assert "전일 종가" in text
    assert "19,000.00" in text
    assert "▼1.04%" in text


def test_morning_report_is_compact_html_with_six_korean_sections(market_data):
    analysis = MorningAnalysis(
        signals=[
            SignalSelection(
                candidate_id="news-one",
                title_ko="연준이 기준금리를 동결",
                meaning="달러와 금리의 방향이 위험자산 부담으로 이어지는지 보는 뉴스입니다.",
                related_asset_keys=["dxy", "us10y", "nasdaq100"],
                relation="시장 배경",
            )
        ]
    )

    text = render_morning_report(market_data, analysis)

    validate_rendered_report(text)
    assert text.startswith("<b>JIN Market Pulse</b>")
    assert "<b>0. 현재 시장</b>" in text
    assert "<b>5. 오늘 관찰 순서</b>" in text
    assert len(text) <= REPORT_LIMIT
    assert "##" not in text
    assert "|" not in text
    assert "확인 필요" not in text
    assert "N/A" not in text
    assert "ETH" not in text


def test_telegram_split_preserves_all_text():
    paragraphs = [f"문단 {index} " + ("가" * 500) for index in range(12)]
    original = "\n\n".join(paragraphs)
    parts = split_message(original, limit=1000)
    assert len(parts) > 1
    assert all(len(part) <= 1000 for part in parts)
    assert "\n\n".join(parts) == original


def test_report_window_excludes_past_and_includes_next_day(market_data):
    data = deepcopy(market_data)
    data.generated_at_kst = data.generated_at_kst.replace(hour=6, minute=50)
    past = deepcopy(data.events[0])
    past.event_id = "past"
    past.title_ko = "지난 지표"
    past.event_time_kst = data.generated_at_kst - timedelta(minutes=1)
    tonight = deepcopy(data.events[0])
    tonight.event_id = "tonight"
    tonight.title_ko = "오늘 밤 지표"
    tonight.event_time_kst = data.generated_at_kst + timedelta(hours=16)
    next_morning = deepcopy(data.events[0])
    next_morning.event_id = "next-morning"
    next_morning.title_ko = "다음 날 새벽 지표"
    next_morning.event_time_kst = data.generated_at_kst + timedelta(hours=22)
    too_late = deepcopy(data.events[0])
    too_late.event_id = "too-late"
    too_late.title_ko = "범위 밖 지표"
    too_late.event_time_kst = data.generated_at_kst + timedelta(hours=25)
    data.events = [past, tonight, next_morning, too_late]

    selected = select_future_events(data)
    report = render_morning_report(data, fallback_morning_analysis(data))

    assert [event.event_id for event in selected] == ["tonight", "next-morning"]
    assert "오늘 밤 지표" in report
    assert "다음 날 새벽 지표" in report
    assert "지난 지표" not in report
    assert "범위 밖 지표" not in report


def test_only_largest_threshold_breaking_extra_asset_is_added(market_data):
    market_data.quotes["eth"].percent_change = -3.1
    market_data.quotes["gold"].percent_change = 3.0
    market_data.quotes["gold"].absolute_change = 70

    selected = select_report_assets(market_data.quotes)
    keys = [quote.key for quote in selected]

    assert keys[:6] == ["btc", "nasdaq100", "dxy", "us10y", "kospi", "wti"]
    assert keys[-1] == "gold"
    assert "eth" not in keys


def test_similar_news_titles_are_rendered_only_once(market_data):
    second = NewsItem(
        news_id="news-two",
        topic_key="different-generated-key",
        title="Federal Reserve keeps rates unchanged after meeting - Bloomberg",
        publisher="Bloomberg",
        published_at=market_data.generated_at_kst,
        url="https://example.com/two",
        trusted_source=True,
    )
    market_data.news.append(second)
    analysis = MorningAnalysis(
        signals=[
            SignalSelection(
                candidate_id="news-one",
                title_ko="연준이 기준금리를 동결",
                meaning="달러와 금리 흐름의 시장 배경입니다.",
                related_asset_keys=["dxy"],
            ),
            SignalSelection(
                candidate_id="news-two",
                title_ko="연준이 회의 후 금리를 동결",
                meaning="달러와 금리 흐름의 같은 배경입니다.",
                related_asset_keys=["us10y"],
            ),
        ]
    )

    validated = _validated_analysis(analysis, market_data)

    assert len(validated.signals) == 1
