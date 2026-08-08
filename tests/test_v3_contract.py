from __future__ import annotations

from datetime import datetime, timedelta

from jin_market_pulse.alerts import send_due_event_results
from jin_market_pulse.bot_queries import handle_market_query
from jin_market_pulse.config import KST
from jin_market_pulse.jobs import process_telegram_update
from jin_market_pulse.models import EconomicEvent, MorningAnalysis
from jin_market_pulse.reports import render_morning_report, select_report_assets
from jin_market_pulse.state import StateStore
from jin_market_pulse.telegram import BOT_COMMANDS, feedback_keyboard


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, object]]] = []
        self.edited: list[tuple[int, str]] = []
        self.callbacks: list[tuple[str, str]] = []

    def send(self, text: str, **kwargs: object) -> list[int]:
        self.sent.append((text, kwargs))
        return [101]

    def edit(self, message_id: int, text: str, **_kwargs: object) -> bool:
        self.edited.append((message_id, text))
        return True

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.callbacks.append((callback_id, text))


def _event(event_time: datetime) -> EconomicEvent:
    return EconomicEvent(
        event_id="v3-cpi",
        title="Consumer Price Index",
        title_ko="소비자물가지수",
        country="USD",
        country_ko="미국",
        event_time_kst=event_time,
        importance="★★★★★",
        forecast="2.7%",
        previous="2.6%",
        actual="2.8%",
        sensitivity_stronger="달러·금리 상승 압력",
        sensitivity_weaker="달러·금리 하락 압력",
        source="공식 경제 캘린더",
    )


def test_schema_v4_persists_watchlist_feedback_and_quality(tmp_path):
    state = StateStore(tmp_path / "state.sqlite3")
    state.set_watch_asset("one", "btc", priority=3)
    state.set_watch_asset("one", "gold", priority=1)
    state.record_feedback("one", 10, "message:10", "move", "useful")
    state.record_feedback("one", 11, "message:11", "move", "noise")
    state.record_quality_event("breaking_scan", "quiet")

    assert state.readiness()["schema_version"] == 4
    assert [item["asset_key"] for item in state.watchlist("one")] == ["btc", "gold"]
    assert state.feedback_summary()["total"] == 2
    assert state.feedback_summary()["sensitivity_factor"] == 1.0
    assert state.quality_summary()["events"][0]["category"] == "breaking_scan"


def test_watchlist_focus_quality_and_review_commands(settings, tmp_path):
    state = StateStore(tmp_path / "state.sqlite3")

    added = handle_market_query("관심 자산에 비트 추가", settings, state).text
    focused = handle_market_query("/focus 비트", settings, state).text
    listed = handle_market_query("/watchlist", settings, state).text
    quality = handle_market_query("/quality", settings, state).text
    review = handle_market_query("/review", settings, state).text

    assert "추가했습니다" in added
    assert "최우선" in focused and "최우선" in listed
    assert "최근 품질 점검" in quality
    assert "최근 7일 보고 점검" in review
    commands = {item["command"] for item in BOT_COMMANDS}
    assert {"watchlist", "focus", "quality", "review"} <= commands


def test_focus_asset_gets_single_personalized_report_slot(market_data):
    selected = select_report_assets(market_data.quotes, ["eth", "gold"])

    assert selected[-1].key == "eth"
    assert sum(quote.key in {"eth", "gold"} for quote in selected) == 1


def test_morning_report_marks_new_and_continuing_changes(market_data):
    market_data.focus_asset_keys = ["btc"]
    market_data.recent_report_facts = {
        "asset:v2:btc": {"numeric_value": 0.4, "direction": 1},
        "asset:v2:dxy": {"numeric_value": -0.2, "direction": -1},
    }
    market_data.quotes["btc"].percent_change = -1.0
    market_data.quotes["btc"].absolute_change = -1000

    text = render_morning_report(market_data, MorningAnalysis(signals=[]))

    assert "새 변화: BTC 방향 반전" in text
    assert "이어짐:" in text
    assert len(text) <= 2000


def test_event_uses_pre_alert_message_then_updates_15_minute_reaction(
    settings,
    market_data,
    tmp_path,
    monkeypatch,
):
    state = StateStore(tmp_path / "state.sqlite3")
    event = _event(datetime.now(KST) - timedelta(minutes=20))
    state.update_event(
        event.event_id,
        tracked_for_result_at=datetime.now(KST).isoformat(),
        pre_alert_message_id=77,
        before_snapshot={"btc": {"current": 100000}},
    )
    telegram = FakeTelegram()
    monkeypatch.setattr(
        "jin_market_pulse.alerts.fetch_economic_events",
        lambda *_args, **_kwargs: [event],
    )
    monkeypatch.setattr(
        "jin_market_pulse.alerts.fetch_market_quotes",
        lambda *_args, **_kwargs: (market_data.quotes, []),
    )

    assert send_due_event_results(settings, state, telegram) == 1
    assert telegram.sent == []
    assert telegram.edited[0][0] == 77
    assert state.event_record(event.event_id)["result_message_id"] == 77

    assert send_due_event_results(settings, state, telegram) == 1
    assert "15분 후" in telegram.edited[-1][1]
    assert state.event_record(event.event_id)["reaction_15m_at"]


def test_feedback_callback_is_saved_without_new_chat_message(settings, tmp_path):
    state = StateStore(tmp_path / "state.sqlite3")
    telegram = FakeTelegram()
    payload = {
        "callback_query": {
            "id": "callback-one",
            "data": "fb:breaking:noise",
            "message": {
                "message_id": 55,
                "chat": {"id": settings.telegram_chat_id},
            },
        }
    }

    process_telegram_update(payload, settings, state, telegram)

    assert telegram.sent == []
    assert telegram.callbacks[-1][1] == "불필요함으로 반영했습니다."
    assert state.feedback_summary()["noise"] == 1


def test_invalid_feedback_callback_is_ignored_safely(settings, tmp_path):
    state = StateStore(tmp_path / "state.sqlite3")
    telegram = FakeTelegram()

    process_telegram_update(
        {
            "callback_query": {
                "id": "callback-invalid",
                "data": "fb:breaking:forged",
                "message": {
                    "message_id": 56,
                    "chat": {"id": settings.telegram_chat_id},
                },
            }
        },
        settings,
        state,
        telegram,
    )

    assert state.feedback_summary()["total"] == 0
    assert telegram.callbacks[-1][1] == "반영하지 못했습니다."


def test_legacy_event_without_message_id_recovers_with_one_message(
    settings,
    market_data,
    tmp_path,
    monkeypatch,
):
    state = StateStore(tmp_path / "state.sqlite3")
    event = _event(datetime.now(KST) - timedelta(minutes=20))
    state.update_event(
        event.event_id,
        tracked_for_result_at=datetime.now(KST).isoformat(),
        result_sent_at=(datetime.now(KST) - timedelta(minutes=15)).isoformat(),
        actual=event.actual,
        before_snapshot={"btc": {"current": 100000}},
    )
    telegram = FakeTelegram()
    monkeypatch.setattr(
        "jin_market_pulse.alerts.fetch_economic_events",
        lambda *_args, **_kwargs: [event],
    )
    monkeypatch.setattr(
        "jin_market_pulse.alerts.fetch_market_quotes",
        lambda *_args, **_kwargs: (market_data.quotes, []),
    )

    assert send_due_event_results(settings, state, telegram) == 1
    assert len(telegram.sent) == 1
    assert "15분 후" in telegram.sent[0][0]
    assert state.event_record(event.event_id)["result_message_id"] == 101


def test_feedback_buttons_are_compact_and_nonpersistent():
    markup = feedback_keyboard("breaking")
    callbacks = [button["callback_data"] for button in markup["inline_keyboard"][0]]

    assert callbacks == [
        "fb:breaking:useful",
        "fb:breaking:noise",
        "fb:breaking:late",
    ]
    assert all(len(value.encode("utf-8")) <= 64 for value in callbacks)
