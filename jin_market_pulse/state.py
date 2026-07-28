from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from .config import KST
from .models import AssetQuote


UTC = timezone.utc


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = RLock()

    def _empty(self) -> dict[str, Any]:
        return {
            "version": 3,
            "alerts": {},
            "events": {},
            "market_snapshots": [],
            "telegram_update_ids": [],
            "ai_advisor_usage": {"date": "", "count": 0},
        }

    def load(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                return self._empty()
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return self._empty()
            if "items" in data and "alerts" not in data:
                data = {
                    "version": 2,
                    "alerts": data.get("items", {}),
                    "events": {},
                    "market_snapshots": [],
                }
            base = self._empty()
            base.update(data)
            return base

    def save(self, data: dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(data, ensure_ascii=False, indent=2)
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
                text=True,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                Path(temp_name).replace(self.path)
            finally:
                temp_path = Path(temp_name)
                if temp_path.exists():
                    temp_path.unlink()

    def alert_in_cooldown(self, topic_key: str, hours: int = 6) -> bool:
        record = self.load()["alerts"].get(topic_key)
        if not record:
            return False
        try:
            sent_at = datetime.fromisoformat(record["sent_at"])
        except (KeyError, TypeError, ValueError):
            return False
        return datetime.now(UTC) - sent_at < timedelta(hours=hours)

    def recent_alert_count(self, minutes: int = 30) -> int:
        cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
        count = 0
        for record in self.load()["alerts"].values():
            try:
                if datetime.fromisoformat(record["sent_at"]) >= cutoff:
                    count += 1
            except (KeyError, TypeError, ValueError):
                continue
        return count

    def mark_alert(self, topic_key: str, title: str, urls: list[str]) -> None:
        data = self.load()
        data["alerts"][topic_key] = {
            "title": title,
            "urls": urls,
            "sent_at": datetime.now(UTC).isoformat(),
        }
        self._prune(data)
        self.save(data)

    def event_record(self, event_id: str) -> dict[str, Any]:
        return self.load()["events"].get(event_id, {})

    def update_event(self, event_id: str, **values: Any) -> None:
        data = self.load()
        record = data["events"].setdefault(event_id, {})
        record.update(values)
        record["updated_at"] = datetime.now(UTC).isoformat()
        self._prune(data)
        self.save(data)

    def save_event_snapshot(
        self,
        event_id: str,
        stage: str,
        quotes: dict[str, AssetQuote],
    ) -> None:
        snapshot = {
            key: {
                "current": quote.current,
                "kind": quote.kind,
                "as_of": quote.as_of.isoformat(),
            }
            for key, quote in quotes.items()
            if key in {"btc", "dxy", "nasdaq100", "us2y", "us10y"}
        }
        self.update_event(event_id, **{f"{stage}_snapshot": snapshot})

    def add_market_snapshot(self, quotes: dict[str, AssetQuote]) -> None:
        data = self.load()
        data["market_snapshots"].append(
            {
                "captured_at": datetime.now(UTC).isoformat(),
                "quotes": {
                    key: {"current": quote.current, "kind": quote.kind}
                    for key, quote in quotes.items()
                    if key in {"btc", "dxy", "nasdaq100", "us10y"}
                },
            }
        )
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        data["market_snapshots"] = [
            item
            for item in data["market_snapshots"]
            if datetime.fromisoformat(item["captured_at"]) >= cutoff
        ]
        self.save(data)

    def claim_telegram_update(self, update_id: int) -> bool:
        with self._lock:
            data = self.load()
            update_ids = data.setdefault("telegram_update_ids", [])
            if update_id in update_ids:
                return False
            update_ids.append(update_id)
            data["telegram_update_ids"] = update_ids[-100:]
            self.save(data)
            return True

    def forget_telegram_update(self, update_id: int) -> None:
        with self._lock:
            data = self.load()
            data["telegram_update_ids"] = [
                value
                for value in data.get("telegram_update_ids", [])
                if value != update_id
            ]
            self.save(data)

    def claim_ai_advisor_slot(self, daily_limit: int) -> bool:
        with self._lock:
            data = self.load()
            today = datetime.now(KST).date().isoformat()
            usage = data.setdefault(
                "ai_advisor_usage",
                {"date": today, "count": 0},
            )
            if usage.get("date") != today:
                usage = {"date": today, "count": 0}
                data["ai_advisor_usage"] = usage
            if int(usage.get("count", 0)) >= max(daily_limit, 0):
                return False
            usage["count"] = int(usage.get("count", 0)) + 1
            self.save(data)
            return True

    def release_ai_advisor_slot(self) -> None:
        with self._lock:
            data = self.load()
            today = datetime.now(KST).date().isoformat()
            usage = data.get("ai_advisor_usage", {})
            if usage.get("date") == today:
                usage["count"] = max(int(usage.get("count", 0)) - 1, 0)
                self.save(data)

    def _prune(self, data: dict[str, Any]) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=21)
        for collection_name in ("alerts", "events"):
            collection = data.get(collection_name, {})
            for key, record in list(collection.items()):
                raw_time = record.get("sent_at") or record.get("updated_at")
                if not raw_time:
                    continue
                try:
                    if datetime.fromisoformat(raw_time) < cutoff:
                        collection.pop(key, None)
                except (TypeError, ValueError):
                    continue
