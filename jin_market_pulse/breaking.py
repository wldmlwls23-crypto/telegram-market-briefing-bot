from __future__ import annotations

import html
import logging
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import KST, Settings
from .models import AssetQuote, EmergencyAnalysis, NewsItem
from .news import breaking_groups, fetch_news
from .providers import fetch_market_quotes
from .reports import create_emergency_analysis
from .state import StateStore
from .telegram import TelegramClient


UTC = timezone.utc

MOVE_THRESHOLDS: dict[str, tuple[float, float]] = {
    "btc": (1.8, 3.0),
    "eth": (2.5, 4.0),
    "nasdaq100": (0.8, 1.5),
    "kospi": (0.8, 1.5),
    "kosdaq": (0.8, 1.5),
    "dxy": (0.35, 0.7),
    "usdkrw": (0.35, 0.7),
    "wti": (1.5, 3.0),
    "gold": (1.5, 3.0),
}

ASSET_NAMES = {
    "btc": "BTC",
    "eth": "ETH",
    "nasdaq100": "Nasdaq 100",
    "kospi": "KOSPI",
    "kosdaq": "KOSDAQ",
    "dxy": "DXY",
    "usdkrw": "원/달러",
    "wti": "WTI",
    "gold": "금",
}

PUBLISHER_NAMES = {
    "reuters": "로이터",
    "associated press": "AP",
    "ap news": "AP",
    "bloomberg": "블룸버그",
    "financial times": "파이낸셜타임스",
    "the wall street journal": "월스트리트저널",
    "wsj": "월스트리트저널",
    "federal reserve": "미 연준",
    "bureau of labor statistics": "미 노동통계국",
    "bureau of economic analysis": "미 경제분석국",
    "u.s. department of the treasury": "미 재무부",
    "us treasury": "미 재무부",
    "bank of korea": "한국은행",
    "ecb": "유럽중앙은행",
    "opec": "OPEC",
}


@dataclass
class MarketMove:
    asset_key: str
    window_minutes: int
    before: float
    current: float
    percent: float
    as_of: datetime
    usual_percent: float | None = None
    speed_ratio: float | None = None
    trigger_percent: float | None = None


def _publisher_ko(value: str) -> str:
    normalized = value.lower()
    for key, name in PUBLISHER_NAMES.items():
        if key in normalized:
            return name
    return value


def _format_price(key: str, value: float) -> str:
    if key in {"btc", "eth", "wti", "gold"}:
        return f"${value:,.2f}"
    if key == "usdkrw":
        return f"{value:,.1f}원"
    return f"{value:,.2f}"


def _snapshot_move(
    quote: AssetQuote,
    snapshot: dict[str, Any],
    *,
    window_minutes: int,
    now: datetime,
) -> MarketMove | None:
    old = (snapshot.get("quotes") or {}).get(quote.key)
    if not old or int(old.get("calculation_version") or 0) < 2:
        return None
    try:
        before = float(old["current"])
        captured = datetime.fromisoformat(str(snapshot["captured_at"]))
    except (KeyError, TypeError, ValueError):
        return None
    target = now - timedelta(minutes=window_minutes)
    tolerance = timedelta(minutes=25 if window_minutes == 30 else 75)
    if abs(captured.astimezone(UTC) - target) > tolerance or before == 0:
        return None
    percent = (quote.current - before) / before * 100
    return MarketMove(
        asset_key=quote.key,
        window_minutes=window_minutes,
        before=before,
        current=quote.current,
        percent=percent,
        as_of=quote.as_of,
    )


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _historical_moves(
    history: list[dict[str, Any]],
    asset_key: str,
    window_minutes: int,
) -> list[float]:
    points: list[tuple[datetime, float]] = []
    for snapshot in history:
        raw = (snapshot.get("quotes") or {}).get(asset_key)
        if not raw or int(raw.get("calculation_version") or 0) < 2:
            continue
        try:
            captured = datetime.fromisoformat(str(snapshot["captured_at"])).astimezone(UTC)
            value = float(raw["current"])
        except (KeyError, TypeError, ValueError):
            continue
        if value > 0:
            points.append((captured, value))
    samples: list[float] = []
    tolerance = timedelta(minutes=20 if window_minutes == 30 else 45)
    start_index = 0
    for end_index, (end_time, end_value) in enumerate(points):
        target = end_time - timedelta(minutes=window_minutes)
        while start_index + 1 < end_index and points[start_index + 1][0] <= target:
            start_index += 1
        candidates = points[max(0, start_index - 1):min(end_index, start_index + 2)]
        if not candidates:
            continue
        start_time, start_value = min(
            candidates,
            key=lambda item: abs(item[0] - target),
        )
        if abs(start_time - target) > tolerance or start_value == 0:
            continue
        change = abs((end_value - start_value) / start_value * 100)
        if change >= 0.005:
            samples.append(change)
    return samples


def _adaptive_trigger(
    history: list[dict[str, Any]],
    asset_key: str,
    window_minutes: int,
    fixed_limit: float,
) -> tuple[float, float | None]:
    samples = _historical_moves(history, asset_key, window_minutes)
    if len(samples) < 12:
        return fixed_limit, None
    usual = max(statistics.median(samples), _percentile(samples, 0.75))
    multiplier = 2.25 if window_minutes == 30 else 2.0
    return max(fixed_limit, usual * multiplier), usual


def detect_large_moves(
    quotes: dict[str, AssetQuote],
    state: StateStore,
    *,
    now: datetime | None = None,
) -> list[MarketMove]:
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    snapshots = {
        30: state.latest_market_snapshot(
            before=current_time - timedelta(minutes=30)
        ),
        120: state.latest_market_snapshot(
            before=current_time - timedelta(minutes=120)
        ),
    }
    history = state.market_snapshot_history(
        since=current_time - timedelta(days=7)
    )
    detected: list[MarketMove] = []
    for key, (short_limit, long_limit) in MOVE_THRESHOLDS.items():
        quote = quotes.get(key)
        if (
            not quote
            or quote.stale
            or not quote.verified
            or quote.validation_status != "verified"
            or quote.calculation_version < 2
        ):
            continue
        short_move = _snapshot_move(
            quote,
            snapshots[30],
            window_minutes=30,
            now=current_time,
        )
        short_trigger, short_usual = _adaptive_trigger(
            history,
            key,
            30,
            short_limit,
        )
        for window, limit in ((30, short_limit), (120, long_limit)):
            move = (
                short_move
                if window == 30
                else _snapshot_move(
                    quote,
                    snapshots[window],
                    window_minutes=window,
                    now=current_time,
                )
            )
            trigger, usual = (
                (short_trigger, short_usual)
                if window == 30
                else _adaptive_trigger(history, key, 120, limit)
            )
            accelerated = True
            if window == 120 and short_move:
                recent_floor = max(short_limit * 0.5, (short_usual or 0) * 1.5)
                accelerated = abs(short_move.percent) >= recent_floor
            elif window == 120:
                accelerated = False
            if move and abs(move.percent) >= trigger and accelerated:
                move.usual_percent = usual
                move.speed_ratio = abs(move.percent) / usual if usual else None
                move.trigger_percent = trigger
                detected.append(move)
                break
    return sorted(detected, key=lambda item: abs(item.percent), reverse=True)


def _group_assets(group: list[NewsItem]) -> set[str]:
    return {
        key
        for item in group
        for key in item.relevant_asset_keys
    }


def _related_moves(
    group: list[NewsItem],
    moves: list[MarketMove],
) -> list[MarketMove]:
    assets = _group_assets(group)
    if "nasdaq_futures" in assets:
        assets.add("nasdaq100")
    if "us10y" in assets:
        assets.update({"dxy", "nasdaq100"})
    return [move for move in moves if move.asset_key in assets]


def _verification_level(
    group: list[NewsItem],
    related_moves: list[MarketMove],
) -> str:
    if any(item.official_source for item in group):
        return "공식 발표"
    if any(item.source_tier == 1 for item in group) and related_moves:
        return "주요 매체 보도 + 가격 반응"
    publishers = {item.publisher.lower() for item in group}
    if len(publishers) >= 2 and related_moves:
        return "복수 보도 + 가격 반응"
    return ""


def _fallback_analysis(
    group: list[NewsItem],
    level: str,
) -> EmergencyAnalysis:
    best = min(group, key=lambda item: item.source_tier)
    assets = _group_assets(group)
    asset_text = "·".join(ASSET_NAMES[key] for key in assets if key in ASSET_NAMES)
    if any(item.official_source for item in group):
        summary = f"{asset_text or '시장'} 관련 공식 발표가 나왔습니다."
    else:
        summary = f"{asset_text or '시장'} 관련 주요 보도가 나왔습니다."
    return EmergencyAnalysis(
        verified=bool(level),
        summary_ko=summary,
        meaning=(
            "직접 원인으로 단정하지 않고 발표 뒤 가격 반응과 추가 공식 사실을 함께 봅니다."
        ),
        source_news_ids=[best.news_id],
    )


def _analysis(
    group: list[NewsItem],
    quotes: dict[str, AssetQuote],
    settings: Settings,
    state: StateStore,
    level: str,
) -> EmergencyAnalysis:
    if not state.claim_usage_slot("auto_ai", settings.auto_ai_daily_limit):
        return _fallback_analysis(group, level)
    try:
        result = create_emergency_analysis(group, quotes, settings)
        if result.verified:
            return result
    except Exception:
        logging.info("Breaking-news AI summary unavailable; using data fallback.")
    finally:
        # Successful calls remain counted. Failed structured output is still a billable call.
        pass
    return _fallback_analysis(group, level)


def _move_lines(moves: list[MarketMove]) -> list[str]:
    if not moves:
        return ["• 기준을 넘는 즉시 가격 반응은 아직 포착되지 않았습니다."]
    lines: list[str] = []
    for move in moves[:4]:
        arrow = "▲" if move.percent > 0 else "▼"
        lines.append(
            f"• {ASSET_NAMES[move.asset_key]} "
            f"{_format_price(move.asset_key, move.before)} → "
            f"<b>{_format_price(move.asset_key, move.current)}</b> "
            f"{arrow}{abs(move.percent):.2f}% · {move.window_minutes}분"
        )
        if move.speed_ratio is not None and move.usual_percent is not None:
            lines.append(
                f"  최근 7일 평소 {move.window_minutes}분 변동 "
                f"{move.usual_percent:.2f}%의 <b>{move.speed_ratio:.1f}배 속도</b>"
            )
    return lines


def render_breaking_alert(
    *,
    analysis: EmergencyAnalysis | None,
    group: list[NewsItem] | None,
    moves: list[MarketMove],
    verification_level: str,
    movement_only: bool = False,
) -> str:
    now = datetime.now(KST)
    title = "[급변 감지]" if movement_only else "[긴급 시장 속보]"
    lines = [
        f"<b>{title}</b>",
        f"{now:%m/%d %H:%M} KST",
        "",
        "<b>무슨 일이 발생했나</b>",
    ]
    if movement_only:
        lines.append("• 기준을 넘는 가격 급변이 감지됐습니다.")
    else:
        lines.append(f"• {html.escape(analysis.summary_ko if analysis else '')}")
    lines.extend(
        [
            "",
            "<b>확인 수준</b>",
            f"• {html.escape(verification_level or '가격 데이터만 확인')}",
            "",
            "<b>발생 직전 → 현재</b>",
            *_move_lines(moves),
            "",
            "<b>해석</b>",
        ]
    )
    if movement_only:
        lines.extend(
            [
                "• 확인된 원인: 아직 공개되지 않았습니다.",
                "• 가능한 배경: 추측하지 않고 60분 동안 자동 검증합니다.",
            ]
        )
    else:
        label = "확인된 원인" if verification_level == "공식 발표" else "가능한 배경"
        lines.append(
            f"• {label}: {html.escape(analysis.meaning if analysis else '')}"
        )
        unrelated = [
            move
            for move in moves
            if group and move.asset_key not in _group_assets(group)
        ]
        if unrelated:
            lines.append(
                f"• 반대·비동조: {ASSET_NAMES[unrelated[0].asset_key]} 움직임은 따로 봐야 합니다."
            )
    if group:
        publishers = sorted({_publisher_ko(item.publisher) for item in group})
        published = max(
            (
                item.published_at.astimezone(KST)
                for item in group
                if item.published_at
            ),
            default=now,
        )
        lines.extend(
            [
                "",
                "<b>출처</b>",
                f"• {' · '.join(html.escape(name) for name in publishers[:2])} "
                f"· {published:%H:%M} KST",
            ]
        )
    return "\n".join(lines)[:1900]


def _movement_topic(move: MarketMove) -> str:
    bucket = int(datetime.now(UTC).timestamp() // (6 * 3600))
    return f"move:{move.asset_key}:{bucket}"


def _move_payload(moves: list[MarketMove]) -> dict[str, float]:
    return {move.asset_key: round(move.percent, 4) for move in moves}


def _overlaps_event_result(
    group: list[NewsItem],
    state: StateStore,
) -> bool:
    text = " ".join(item.title.lower() for item in group)
    event_titles = " ".join(state.recent_event_result_titles(hours=6))
    if not event_titles:
        return False
    aliases = (
        ("cpi", "consumer price"),
        ("pce", "personal consumption"),
        ("payroll", "nonfarm"),
        ("unemployment claims", "jobless claims"),
        ("gdp", "gross domestic"),
        ("fomc", "rate decision"),
    )
    return any(
        any(alias in text for alias in group_aliases)
        and any(alias in event_titles for alias in group_aliases)
        for group_aliases in aliases
    )


def _send_or_update_news(
    group: list[NewsItem],
    related: list[MarketMove],
    all_moves: list[MarketMove],
    quotes: dict[str, AssetQuote],
    settings: Settings,
    state: StateStore,
    telegram: TelegramClient,
    disable_notification: bool,
) -> str:
    level = _verification_level(group, related)
    if not level:
        return "rejected"
    if _overlaps_event_result(group, state):
        return "event_merged"
    topic = group[0].topic_key
    existing = state.alert_record(topic)
    news_ids = {item.news_id for item in group}
    in_cooldown = bool(existing and state.alert_in_cooldown(topic, hours=6))
    if in_cooldown:
        old_payload = existing.get("payload", {})
        old_ids = set(old_payload.get("news_ids") or [])
        old_moves = old_payload.get("moves") or {}
        meaningful_move_update = any(
            abs(move.percent - float(old_moves.get(move.asset_key, move.percent)))
            >= 0.5
            for move in related
        )
        if news_ids.issubset(old_ids) and not meaningful_move_update:
            return "duplicate"
    if (
        not in_cooldown
        and (
            state.recent_alert_count(minutes=30) >= 2
            or state.daily_alert_count() >= 3
        )
    ):
        return "limited"
    analysis = _analysis(group, quotes, settings, state, level)
    text = render_breaking_alert(
        analysis=analysis,
        group=group,
        moves=related or all_moves,
        verification_level=level,
    )
    if in_cooldown:
        message_id = existing.get("telegram_message_id")
        if message_id and text != existing.get("payload", {}).get("text"):
            telegram.edit(int(message_id), text)
            state.touch_alert(
                topic,
                payload={
                    "text": text,
                    "assets": sorted(_group_assets(group)),
                    "verification_level": level,
                    "news_ids": sorted(news_ids),
                    "moves": _move_payload(related),
                },
                status="verified",
            )
            return "updated"
        return "duplicate"
    message_ids = telegram.send(
        text,
        parse_mode="HTML",
        disable_notification=disable_notification,
    )
    state.mark_alert(
        topic,
        group[0].title,
        [item.url for item in group],
        telegram_message_id=message_ids[0] if message_ids else None,
        payload={
            "text": text,
            "assets": sorted(_group_assets(group)),
            "verification_level": level,
            "news_ids": sorted(item.news_id for item in group),
            "moves": _move_payload(related),
        },
        status="verified",
    )
    return "sent"


def _update_pending_causes(
    groups: list[list[NewsItem]],
    quotes: dict[str, AssetQuote],
    moves: list[MarketMove],
    settings: Settings,
    state: StateStore,
    telegram: TelegramClient,
) -> int:
    updated = 0
    for pending in state.pending_alerts():
        assets = set(pending.get("payload", {}).get("assets") or [])
        for group in groups:
            if not assets & _group_assets(group):
                continue
            related = [
                move for move in moves if move.asset_key in assets
            ]
            level = _verification_level(group, related)
            if not level:
                continue
            analysis = _analysis(group, quotes, settings, state, level)
            text = render_breaking_alert(
                analysis=analysis,
                group=group,
                moves=related or moves,
                verification_level=level,
            )
            message_id = pending.get("telegram_message_id")
            if message_id and telegram.edit(int(message_id), text):
                state.touch_alert(
                    str(pending["topic_key"]),
                    payload={
                        "text": text,
                        "assets": sorted(assets),
                        "verification_level": level,
                        "news_ids": sorted(item.news_id for item in group),
                        "moves": _move_payload(related),
                    },
                    status="verified",
                )
                updated += 1
            break
    return updated


def monitor_breaking_alerts(
    settings: Settings,
    state: StateStore,
    telegram: TelegramClient,
    *,
    check_prices: bool,
    disable_notification: bool = False,
) -> dict[str, int]:
    if settings.breaking_alert_mode == "off":
        return {"news": 0, "moves": 0, "sent": 0, "updated": 0}
    news = fetch_news()
    groups = breaking_groups(news)
    quotes: dict[str, AssetQuote] = {}
    moves: list[MarketMove] = []
    if check_prices or groups or state.pending_alerts():
        quotes, _ = fetch_market_quotes(settings, state)
    if check_prices and quotes:
        moves = detect_large_moves(quotes, state)

    sent = 0
    updated = _update_pending_causes(
        groups,
        quotes,
        moves,
        settings,
        state,
        telegram,
    )
    for group in groups:
        related = _related_moves(group, moves)
        result = _send_or_update_news(
            group,
            related,
            moves,
            quotes,
            settings,
            state,
            telegram,
            disable_notification,
        )
        sent += int(result == "sent")
        updated += int(result == "updated")
        if sent >= 1:
            break

    if check_prices and moves and sent == 0:
        move = moves[0]
        topic = _movement_topic(move)
        if (
            not state.alert_in_cooldown(topic, hours=6)
            and state.recent_alert_count(minutes=30) < 2
            and state.daily_alert_count() < 3
        ):
            text = render_breaking_alert(
                analysis=None,
                group=None,
                moves=moves,
                verification_level="가격 데이터만 확인",
                movement_only=True,
            )
            message_ids = telegram.send(
                text,
                parse_mode="HTML",
                disable_notification=disable_notification,
            )
            state.mark_alert(
                topic,
                f"{ASSET_NAMES[move.asset_key]} rapid move",
                [],
                telegram_message_id=message_ids[0] if message_ids else None,
                payload={
                    "text": text,
                    "assets": [item.asset_key for item in moves],
                    "verification_level": "가격 데이터만 확인",
                    "news_ids": [],
                    "moves": _move_payload(moves),
                },
                status="pending_cause",
            )
            sent += 1

    if check_prices and quotes:
        state.add_market_snapshot(quotes)
    state.set_runtime_state(
        "breaking_scan",
        {
            "checked_at": datetime.now(UTC).isoformat(),
            "news_candidates": len(groups),
            "large_moves": len(moves),
            "sent": sent,
            "updated": updated,
        },
    )
    return {
        "news": len(groups),
        "moves": len(moves),
        "sent": sent,
        "updated": updated,
    }
