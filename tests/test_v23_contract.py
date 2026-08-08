from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from jin_market_pulse.bot_queries import handle_market_query
from jin_market_pulse.breaking import (
    MarketMove,
    _send_or_update_news,
    _verification_level,
    detect_large_moves,
    render_breaking_alert,
)
from jin_market_pulse.config import KST, PARIS
from jin_market_pulse.models import EmergencyAnalysis, NewsItem
from jin_market_pulse.reports import fallback_morning_analysis, render_morning_report
from jin_market_pulse.server import create_app
from jin_market_pulse.session_reports import (
    SessionReportResult,
    _novel_fact_keys,
    next_report_time,
    render_session_report,
    report_due,
    validate_session_report,
)
from jin_market_pulse.state import StateStore
from jin_market_pulse.telegram import TelegramClient


def _news(
    *,
    publisher: str = "Reuters",
    tier: int = 1,
    official: bool = False,
    news_id: str = "news-1",
) -> NewsItem:
    return NewsItem(
        news_id=news_id,
        topic_key="fed-shock:20260729",
        title="Federal Reserve rate decision",
        publisher=publisher,
        published_at=datetime.now(timezone.utc),
        url=f"https://example.com/{news_id}",
        official_source=official,
        trusted_source=True,
        source_tier=tier,
        relevant_asset_keys=["dxy", "nasdaq100"],
    )


def test_schema_v3_and_report_state_persist(tmp_path):
    state = StateStore(tmp_path / "state.sqlite3")
    state.record_report_run(
        "korea_close:2026-07-29",
        "korea_close",
        "2026-07-29",
        "sent",
        text="report",
        facts=[
            {
                "fact_key": "asset:v2:kospi",
                "numeric_value": -1.2,
                "direction": -1,
                "official": False,
            }
        ],
    )

    assert state.readiness()["schema_version"] == 3
    assert state.latest_report_run("korea_close")["text"] == "report"
    assert state.recent_report_facts()["asset:v2:kospi"]["direction"] == -1


def test_fact_dedup_allows_reversal_and_half_point_extension(tmp_path):
    state = StateStore(tmp_path / "state.sqlite3")
    state.record_report_run(
        "morning:one",
        "morning",
        "2026-07-29",
        "sent",
        facts=[
            {
                "fact_key": "asset:btc",
                "numeric_value": -1.0,
                "direction": -1,
                "official": False,
            }
        ],
    )

    same = [{"fact_key": "asset:btc", "numeric_value": -1.2, "direction": -1}]
    extended = [{"fact_key": "asset:btc", "numeric_value": -1.6, "direction": -1}]
    reversed_move = [{"fact_key": "asset:btc", "numeric_value": 0.1, "direction": 1}]

    assert _novel_fact_keys(same, state) == set()
    assert _novel_fact_keys(extended, state) == {"asset:btc"}
    assert _novel_fact_keys(reversed_move, state) == {"asset:btc"}


def test_korea_and_europe_due_windows_include_delayed_cron():
    korea = datetime(2026, 7, 29, 15, 58, tzinfo=KST)
    europe = datetime(2026, 7, 29, 18, 12, tzinfo=PARIS)

    assert report_due("korea_close", korea)
    assert report_due("europe_close", europe)
    assert not report_due("korea_close", korea.replace(hour=15, minute=40))


def test_next_europe_report_honors_dst():
    summer = datetime(2026, 7, 29, 19, 0, tzinfo=PARIS)
    winter = datetime(2026, 12, 2, 19, 0, tzinfo=PARIS)

    assert next_report_time("europe_close", summer).utcoffset() == timedelta(hours=2)
    assert next_report_time("europe_close", winter).utcoffset() == timedelta(hours=1)


def test_session_report_mobile_contract(market_data):
    quotes = {
        key: value
        for key, value in market_data.quotes.items()
        if key in {"kospi", "kosdaq", "usdkrw", "btc"}
    }
    text = render_session_report(
        "korea_close",
        quotes,
        [],
        {"asset:v2:kospi"},
        now=datetime.now(KST),
        next_check="07/29 21:30 미국 GDP",
    )

    validate_session_report(text)
    assert len(text) <= 1200
    assert all(value not in text for value in ("|", "#", "N/A", "확인 필요"))
    assert "앞선 보고 이후 달라진 점" in text


def test_morning_starts_with_us_close_summary(market_data):
    market_data.events = []
    text = render_morning_report(
        market_data,
        fallback_morning_analysis(market_data),
    )

    assert "미국장 마감:" in text
    assert "S&amp;P 500" in text


def test_breaking_primary_source_requires_price_reaction():
    group = [_news()]
    move = MarketMove(
        asset_key="dxy",
        window_minutes=30,
        before=100,
        current=100.5,
        percent=0.5,
        as_of=datetime.now(timezone.utc),
    )

    assert _verification_level(group, []) == ""
    assert _verification_level(group, [move]) == "주요 매체 보도 + 가격 반응"


def test_breaking_official_source_can_stand_alone():
    group = [_news(publisher="Federal Reserve", tier=0, official=True)]

    assert _verification_level(group, []) == "공식 발표"


def test_breaking_secondary_requires_two_publishers_and_move():
    group = [
        _news(publisher="CNBC", tier=2, news_id="one"),
        _news(publisher="BBC", tier=2, news_id="two"),
    ]
    move = MarketMove(
        asset_key="nasdaq100",
        window_minutes=30,
        before=20000,
        current=19800,
        percent=-1.0,
        as_of=datetime.now(timezone.utc),
    )

    assert _verification_level(group, []) == ""
    assert _verification_level(group, [move]) == "복수 보도 + 가격 반응"


def test_duplicate_breaking_does_not_spend_ai(settings, tmp_path, monkeypatch):
    state = StateStore(tmp_path / "state.sqlite3")
    group = [_news()]
    move = MarketMove(
        asset_key="dxy",
        window_minutes=30,
        before=100,
        current=100.5,
        percent=0.5,
        as_of=datetime.now(timezone.utc),
    )
    state.mark_alert(
        group[0].topic_key,
        group[0].title,
        [group[0].url],
        payload={
            "news_ids": [group[0].news_id],
            "moves": {"dxy": 0.5},
        },
    )
    monkeypatch.setattr(
        "jin_market_pulse.breaking._analysis",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("AI must not run for duplicate")
        ),
    )

    result = _send_or_update_news(
        group,
        [move],
        [move],
        {},
        settings,
        state,
        object(),
        False,
    )

    assert result == "duplicate"


def test_detect_large_moves_uses_30m_and_2h_thresholds(market_data, monkeypatch, tmp_path):
    state = StateStore(tmp_path / "state.sqlite3")
    quote = market_data.quotes["btc"]
    quote.current = 98100

    def fake_snapshot(*, before=None):
        assert before is not None
        minutes = round((datetime.now(timezone.utc) - before).total_seconds() / 60)
        value = 100000 if minutes < 60 else 101500
        return {
            "captured_at": before.isoformat(),
            "quotes": {"btc": {"current": value, "calculation_version": 2}},
        }

    monkeypatch.setattr(state, "latest_market_snapshot", fake_snapshot)
    moves = detect_large_moves({"btc": quote}, state)

    assert moves
    assert moves[0].window_minutes == 30
    assert moves[0].percent <= -1.8


def test_detect_large_moves_requires_faster_than_recent_usual_move(
    market_data,
    monkeypatch,
    tmp_path,
):
    state = StateStore(tmp_path / "state.sqlite3")
    now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
    quote = market_data.quotes["btc"]
    quote.current = 101900

    monkeypatch.setattr(
        state,
        "latest_market_snapshot",
        lambda *, before=None: {
            "captured_at": before.isoformat(),
            "quotes": {"btc": {"current": 100000, "calculation_version": 2}},
        },
    )
    history = []
    value = 100000.0
    for index in range(20):
        history.append(
            {
                "captured_at": (now - timedelta(minutes=(20 - index) * 30)).isoformat(),
                    "quotes": {
                        "btc": {"current": value, "calculation_version": 2}
                    },
            }
        )
        value *= 1.01
    monkeypatch.setattr(
        state,
        "market_snapshot_history",
        lambda *, since: history,
    )

    assert detect_large_moves({"btc": quote}, state, now=now) == []

    quote.current = 103000
    moves = detect_large_moves({"btc": quote}, state, now=now)

    assert moves
    assert moves[0].speed_ratio is not None
    assert moves[0].speed_ratio >= 2.9


def test_movement_alert_explains_usual_speed_comparison():
    move = MarketMove(
        asset_key="btc",
        window_minutes=30,
        before=100000,
        current=103000,
        percent=3.0,
        as_of=datetime.now(timezone.utc),
        usual_percent=0.8,
        speed_ratio=3.75,
        trigger_percent=1.8,
    )

    text = render_breaking_alert(
        analysis=None,
        group=None,
        moves=[move],
        verification_level="가격 데이터만 확인",
        movement_only=True,
    )

    assert "최근 7일 평소" in text
    assert "3.8배 속도" in text


def test_movement_only_alert_promises_update_not_guess():
    move = MarketMove(
        asset_key="btc",
        window_minutes=30,
        before=100000,
        current=98000,
        percent=-2.0,
        as_of=datetime.now(timezone.utc),
    )
    text = render_breaking_alert(
        analysis=None,
        group=None,
        moves=[move],
        verification_level="가격 데이터만 확인",
        movement_only=True,
    )

    assert "[급변 감지]" in text
    assert "100,000.00" in text and "98,000.00" in text
    assert "60분 동안 자동 검증" in text


def test_verified_breaking_alert_contains_level_source_and_move():
    group = [_news(publisher="Federal Reserve", tier=0, official=True)]
    move = MarketMove(
        asset_key="dxy",
        window_minutes=30,
        before=100,
        current=100.4,
        percent=0.4,
        as_of=datetime.now(timezone.utc),
    )
    analysis = EmergencyAnalysis(
        verified=True,
        summary_ko="연준이 새 정책 결정을 발표했습니다.",
        meaning="달러와 금리 반응을 함께 봅니다.",
        source_news_ids=["news-1"],
    )
    text = render_breaking_alert(
        analysis=analysis,
        group=group,
        moves=[move],
        verification_level="공식 발표",
    )

    assert "공식 발표" in text
    assert "미 연준" in text
    assert "100.00" in text and "100.40" in text


def test_telegram_supports_silent_close_report(settings, monkeypatch):
    calls = []
    client = TelegramClient(settings)

    def fake_call(method, *, json_body=None, **_kwargs):
        calls.append((method, json_body))
        return {"message_id": 10}

    monkeypatch.setattr(client, "_call", fake_call)
    client.send("유럽장 마감", disable_notification=True)

    assert calls[0][1]["disable_notification"] is True


def test_manual_report_endpoint_requires_auth_and_idempotency(settings, monkeypatch):
    secured = replace(settings, cron_secret="a-secure-test-secret")
    client = TestClient(create_app(secured))

    assert client.post("/jobs/report/korea_close").status_code == 401
    assert (
        client.post(
            "/jobs/report/korea_close?deliver=true",
            headers={"Authorization": "Bearer a-secure-test-secret"},
        ).status_code
        == 400
    )

    monkeypatch.setattr(
        "jin_market_pulse.server.send_session_report",
        lambda *_args, **_kwargs: SessionReportResult(
            "korea_close",
            "korea_close:test",
            "2026-07-29",
            "preview",
            text="preview",
        ),
    )
    response = client.post(
        "/jobs/report/korea_close",
        headers={"Authorization": "Bearer a-secure-test-secret"},
    )
    assert response.status_code == 200
    assert response.json()["text"] == "preview"


def test_close_and_overnight_settings_are_independent(settings, tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")

    handle_market_query("한국 마감 알림 꺼줘", settings, store)
    handle_market_query("유럽 마감 알림 꺼줘", settings, store)
    handle_market_query("미국장 시작 알림 꺼줘", settings, store)
    handle_market_query("새벽 무음 해제", settings, store)
    prefs = store.preferences(settings.telegram_chat_id)

    assert prefs["korea_close_reports"] is False
    assert prefs["europe_close_reports"] is False
    assert prefs["us_open_reports"] is False
    assert prefs["overnight_silent"] is False


def test_cron_runs_every_fifteen_minutes():
    config = json.loads(open("railway.cron.json", encoding="utf-8").read())

    assert config["deploy"]["cronSchedule"] == "5,20,35,50 * * * *"
