from __future__ import annotations

from jin_market_pulse.alerts import render_event_result, render_pre_event_reminder
from jin_market_pulse.models import EmergencyAnalysis
from jin_market_pulse.reports import render_emergency_alert


def test_pre_event_reminder_matches_readable_label_order(market_data):
    text = render_pre_event_reminder(market_data.events[0])

    assert text.startswith("<b>[중요 경제지표 사전 알림]</b>")
    assert "발표 시간:" in text
    assert "중요도:" in text
    assert "예상:" in text
    assert "이전:" in text
    assert "의미:" in text
    assert "해석:" in text


def test_event_result_contains_actual_and_market_reaction(market_data):
    event = market_data.events[0].model_copy(update={"actual": "0.3%"})
    before = {
        "btc": {"current": 100000},
        "us10y": {"current": 4.2},
    }

    text = render_event_result(event, before, market_data.quotes)

    assert text.startswith("<b>[중요 경제지표 결과]</b>")
    assert "실제 0.3% / 예상 0.2% / 이전 0.1%" in text
    assert "판정: 예상보다 높음 · 이전보다 높음" in text
    assert "출처:" in text
    assert "발표 전후 시장 반응:" in text
    assert "BTC: 발표 전 100,000.00" in text


def test_emergency_news_uses_html_sections_and_escapes_text(market_data):
    analysis = EmergencyAnalysis(
        verified=True,
        summary_ko="정책 <변경> 발표",
        meaning="달러 & 금리 변동성 확대",
        source_news_ids=["news-one"],
    )

    text = render_emergency_alert(
        analysis,
        market_data.news,
        market_data.quotes,
    )

    assert text.startswith("<b>[긴급 시장 뉴스]</b>")
    assert "발생 시간:" in text
    assert "중요도: ★★★★★" in text
    assert "정책 &lt;변경&gt; 발표" in text
    assert "달러 &amp; 금리 변동성 확대" in text
