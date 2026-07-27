from __future__ import annotations

from jin_market_pulse.models import (
    MorningAnalysis,
    SensitivitySelection,
    SignalSelection,
)
from jin_market_pulse.reports import (
    fallback_morning_analysis,
    format_quote,
    render_morning_report,
    validate_rendered_report,
)
from jin_market_pulse.telegram import split_message


def test_quote_shows_verified_baseline(market_data):
    text = format_quote(market_data.quotes["nasdaq100"])
    assert "전일 종가" in text
    assert "19,200.00" in text
    assert "-1.04%" in text


def test_morning_report_has_fixed_readable_sections(market_data):
    analysis = MorningAnalysis(
        headline="달러와 금리가 함께 올라 위험자산 부담이 커진 흐름입니다.",
        signals=[
            SignalSelection(
                candidate_id="news-one",
                title_ko="연준이 기준금리를 동결",
                meaning="달러와 금리의 방향이 Nasdaq과 BTC에 이어지는지 보는 뉴스입니다.",
            )
        ],
        cross_asset_chain="달러와 금리 상승이 Nasdaq과 BTC에 같은 부담으로 이어졌습니다.",
        sensitivities=[SensitivitySelection(event_id="event-one")],
        priority_event_ids=["event-one"],
        priority_asset_keys=["dxy", "us10y"],
    )
    text = render_morning_report(market_data, analysis)
    validate_rendered_report(text)
    assert "## 0. [Current Asset Snapshot]" in text
    assert "## 5. [Today's Priority]" in text
    assert "실제" not in text
    assert "|" not in text
    assert "확인 필요" not in text
    assert "N/A" not in text


def test_telegram_split_preserves_all_text():
    paragraphs = [f"문단 {index} " + ("가" * 500) for index in range(12)]
    original = "\n\n".join(paragraphs)
    parts = split_message(original, limit=1000)
    assert len(parts) > 1
    assert all(len(part) <= 1000 for part in parts)
    assert "\n\n".join(parts) == original


def test_report_only_lists_events_remaining_today(market_data):
    from copy import deepcopy
    from datetime import timedelta

    data = deepcopy(market_data)
    data.generated_at_kst = data.generated_at_kst.replace(hour=6, minute=50)
    data.events[0].event_time_kst = data.generated_at_kst + timedelta(hours=2)
    tomorrow = deepcopy(data.events[0])
    tomorrow.event_id = "tomorrow-event"
    tomorrow.title_ko = "내일 지표"
    tomorrow.event_time_kst = data.generated_at_kst + timedelta(days=1)
    data.events.append(tomorrow)

    report = render_morning_report(data, fallback_morning_analysis(data))

    assert data.events[0].title_ko in report
    assert "내일 지표" not in report
