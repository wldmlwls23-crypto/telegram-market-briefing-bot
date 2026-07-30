from __future__ import annotations

from datetime import datetime, timedelta

from jin_market_pulse.alerts import send_due_event_results
from jin_market_pulse.config import KST
from jin_market_pulse.models import EconomicEvent
from jin_market_pulse.state import StateStore


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, object]]] = []
        self.edited: list[tuple[int, str]] = []

    def send(self, text: str, **kwargs: object) -> list[int]:
        self.sent.append((text, kwargs))
        return [100 + len(self.sent)]

    def edit(self, message_id: int, text: str, **kwargs: object) -> bool:
        self.edited.append((message_id, text))
        return True


def _event(
    event_id: str,
    event_time: datetime,
    *,
    actual: str,
    title: str = "ISM Services PMI",
) -> EconomicEvent:
    return EconomicEvent(
        event_id=event_id,
        title=title,
        title_ko=title,
        country="USD",
        country_ko="미국",
        event_time_kst=event_time,
        importance="★★★★",
        forecast="50.0",
        previous="49.0",
        actual=actual,
        sensitivity_stronger="달러·금리 상승 압력",
        sensitivity_weaker="달러·금리 하락 압력",
        source="Economic calendar",
    )


def test_calendar_tracked_four_star_event_sends_result(
    settings,
    tmp_path,
    monkeypatch,
):
    state = StateStore(tmp_path / "state.sqlite3")
    event = _event(
        "tracked-four-star",
        datetime.now(KST) - timedelta(minutes=10),
        actual="51.2",
    )
    state.update_event(
        event.event_id,
        tracked_for_result_at=datetime.now(KST).isoformat(),
    )
    telegram = FakeTelegram()
    monkeypatch.setattr(
        "jin_market_pulse.alerts.fetch_economic_events",
        lambda *_args, **_kwargs: [event],
    )
    monkeypatch.setattr(
        "jin_market_pulse.alerts.fetch_market_quotes",
        lambda *_args, **_kwargs: ({}, []),
    )

    delivered = send_due_event_results(settings, state, telegram)

    assert delivered == 1
    assert len(telegram.sent) == 1
    assert "실제 51.2 / 예상 50.0 / 이전 49.0" in telegram.sent[0][0]
    assert state.event_record(event.event_id)["result_sent_at"]


def test_late_same_time_result_edits_existing_message(
    settings,
    tmp_path,
    monkeypatch,
):
    state = StateStore(tmp_path / "state.sqlite3")
    event_time = datetime.now(KST) - timedelta(minutes=10)
    first = _event("first-result", event_time, actual="51.2")
    second = _event(
        "second-result",
        event_time,
        actual="52.1",
        title="ISM Prices",
    )
    for event in (first, second):
        state.update_event(
            event.event_id,
            tracked_for_result_at=datetime.now(KST).isoformat(),
        )
    telegram = FakeTelegram()
    batches = iter(([first], [first, second]))
    monkeypatch.setattr(
        "jin_market_pulse.alerts.fetch_economic_events",
        lambda *_args, **_kwargs: next(batches),
    )
    monkeypatch.setattr(
        "jin_market_pulse.alerts.fetch_market_quotes",
        lambda *_args, **_kwargs: ({}, []),
    )

    first_delivered = send_due_event_results(settings, state, telegram)
    second_delivered = send_due_event_results(settings, state, telegram)

    assert first_delivered == 1
    assert second_delivered == 1
    assert len(telegram.sent) == 1
    assert len(telegram.edited) == 1
    assert telegram.edited[0][0] == 101
    assert "ISM Services PMI" in telegram.edited[0][1]
    assert "ISM Prices" in telegram.edited[0][1]
    assert state.event_record(second.event_id)["result_message_id"] == 101


def test_fallback_event_matches_tracked_time_without_duplicate(
    settings,
    tmp_path,
    monkeypatch,
):
    state = StateStore(tmp_path / "state.sqlite3")
    event_time = datetime.now(KST) - timedelta(minutes=10)
    original = _event("primary-event", event_time, actual="")
    fallback = _event("fallback-event", event_time, actual="51.2")
    state.update_event(
        original.event_id,
        title=original.title,
        event_time=event_time.isoformat(),
        tracked_for_result_at=datetime.now(KST).isoformat(),
    )
    telegram = FakeTelegram()
    batches = iter(([fallback], [original.model_copy(update={"actual": "51.2"})]))
    monkeypatch.setattr(
        "jin_market_pulse.alerts.fetch_economic_events",
        lambda *_args, **_kwargs: next(batches),
    )
    monkeypatch.setattr(
        "jin_market_pulse.alerts.fetch_market_quotes",
        lambda *_args, **_kwargs: ({}, []),
    )

    first_delivered = send_due_event_results(settings, state, telegram)
    second_delivered = send_due_event_results(settings, state, telegram)

    assert first_delivered == 1
    assert second_delivered == 0
    assert len(telegram.sent) == 1
    assert state.event_record(original.event_id)["stage"] == "result_sent_via_fallback"
