from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import KST
from .models import AssetQuote


UTC = timezone.utc
SCHEMA_VERSION = 1


class StateStore:
    """Transactional state shared by webhooks, scheduled jobs, and deploys."""

    def __init__(self, path: Path, *, legacy_json: Path | None = None):
        supplied = Path(path)
        if supplied.suffix.lower() == ".json":
            self.path = supplied.with_suffix(".sqlite3")
            self.legacy_json = supplied
        else:
            self.path = supplied
            self.legacy_json = legacy_json
        self.path.parent.mkdir(parents=True, exist_ok=True)
        is_new_database = not self.path.exists()
        self._initialize(enable_wal=is_new_database)
        self._migrate_legacy_json_once()

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=15,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA foreign_keys = ON")
        if write:
            connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            if write:
                connection.commit()
        except Exception:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self, *, enable_wal: bool) -> None:
        with self._connect() as db:
            if enable_wal:
                db.execute("PRAGMA journal_mode = WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alerts (
                    topic_key TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    urls_json TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    update_count INTEGER NOT NULL DEFAULT 0,
                    telegram_message_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS market_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    captured_at TEXT NOT NULL,
                    quotes_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS telegram_updates (
                    update_id INTEGER PRIMARY KEY,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'claimed',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS usage_counters (
                    usage_date TEXT NOT NULL,
                    category TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (usage_date, category)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    job_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    lease_until TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    delivery_key TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    telegram_message_id INTEGER,
                    content_hash TEXT NOT NULL DEFAULT '',
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (delivery_key, stage)
                );
                CREATE TABLE IF NOT EXISTS provider_cache (
                    cache_key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_health (
                    provider TEXT PRIMARY KEY,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    last_success_at TEXT,
                    last_failure_at TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    notified INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS chat_context (
                    chat_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_preferences (
                    chat_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS price_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    asset_key TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    threshold REAL NOT NULL,
                    recurring INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    armed INTEGER NOT NULL DEFAULT 1,
                    last_triggered_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS saved_messages (
                    message_key TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    parse_mode TEXT NOT NULL DEFAULT 'HTML',
                    created_at TEXT NOT NULL
                );
                """
            )
            db.execute(
                """
                INSERT INTO metadata(key, value) VALUES('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (str(SCHEMA_VERSION),),
            )

    def _migrate_legacy_json_once(self) -> None:
        if not self.legacy_json or not self.legacy_json.exists():
            return
        with self._connect() as db:
            migrated = db.execute(
                "SELECT value FROM metadata WHERE key='legacy_json_migrated'"
            ).fetchone()
        if migrated:
            return
        try:
            raw = json.loads(self.legacy_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        backup = self.legacy_json.with_suffix(
            self.legacy_json.suffix + ".pre-sqlite.bak"
        )
        if not backup.exists():
            shutil.copy2(self.legacy_json, backup)
        alerts = raw.get("alerts") or raw.get("items") or {}
        events = raw.get("events") or {}
        snapshots = raw.get("market_snapshots") or []
        now = datetime.now(UTC).isoformat()
        with self._connect(write=True) as db:
            for topic_key, record in alerts.items():
                db.execute(
                    """
                    INSERT OR IGNORE INTO alerts(
                        topic_key, title, urls_json, sent_at
                    ) VALUES(?, ?, ?, ?)
                    """,
                    (
                        topic_key,
                        str(record.get("title") or topic_key),
                        json.dumps(record.get("urls") or [], ensure_ascii=False),
                        str(record.get("sent_at") or now),
                    ),
                )
            for event_id, record in events.items():
                db.execute(
                    """
                    INSERT OR REPLACE INTO events(event_id, payload_json, updated_at)
                    VALUES(?, ?, ?)
                    """,
                    (
                        event_id,
                        json.dumps(record, ensure_ascii=False),
                        str(record.get("updated_at") or now),
                    ),
                )
            for item in snapshots:
                db.execute(
                    """
                    INSERT INTO market_snapshots(captured_at, quotes_json)
                    VALUES(?, ?)
                    """,
                    (
                        str(item.get("captured_at") or now),
                        json.dumps(item.get("quotes") or {}, ensure_ascii=False),
                    ),
                )
            usage = raw.get("ai_advisor_usage") or {}
            if usage.get("date"):
                db.execute(
                    """
                    INSERT OR REPLACE INTO usage_counters(usage_date, category, count)
                    VALUES(?, 'advisor', ?)
                    """,
                    (str(usage["date"]), int(usage.get("count", 0))),
                )
            db.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES(?, ?)",
                ("legacy_json_migrated", now),
            )
        logging.info("Legacy JSON state migrated to SQLite; original preserved.")

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _loads(value: str | None, default: Any) -> Any:
        if not value:
            return default
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default

    def alert_in_cooldown(self, topic_key: str, hours: int = 6) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT sent_at FROM alerts WHERE topic_key=?", (topic_key,)
            ).fetchone()
        if not row:
            return False
        return datetime.now(UTC) - datetime.fromisoformat(row["sent_at"]) < timedelta(
            hours=hours
        )

    def recent_alert_count(self, minutes: int = 30) -> int:
        cutoff = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS count FROM alerts WHERE sent_at>=?", (cutoff,)
            ).fetchone()
        return int(row["count"])

    def daily_alert_count(self) -> int:
        start = datetime.now(KST).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(UTC)
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS count FROM alerts WHERE sent_at>=?",
                (start.isoformat(),),
            ).fetchone()
        return int(row["count"])

    def mark_alert(
        self,
        topic_key: str,
        title: str,
        urls: list[str],
        telegram_message_id: int | None = None,
    ) -> None:
        with self._connect(write=True) as db:
            db.execute(
                """
                INSERT INTO alerts(
                    topic_key, title, urls_json, sent_at, telegram_message_id
                ) VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(topic_key) DO UPDATE SET
                    title=excluded.title,
                    urls_json=excluded.urls_json,
                    sent_at=excluded.sent_at,
                    telegram_message_id=COALESCE(
                        excluded.telegram_message_id, alerts.telegram_message_id
                    )
                """,
                (
                    topic_key,
                    title,
                    self._json(urls),
                    datetime.now(UTC).isoformat(),
                    telegram_message_id,
                ),
            )

    def event_record(self, event_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload_json FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
        return self._loads(row["payload_json"], {}) if row else {}

    def update_event(self, event_id: str, **values: Any) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect(write=True) as db:
            row = db.execute(
                "SELECT payload_json FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
            payload = self._loads(row["payload_json"], {}) if row else {}
            payload.update(values)
            payload["updated_at"] = now
            db.execute(
                """
                INSERT INTO events(event_id, payload_json, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (event_id, self._json(payload), now),
            )

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
        now = datetime.now(UTC)
        payload = {
            key: {
                "current": quote.current,
                "kind": quote.kind,
                "as_of": quote.as_of.isoformat(),
            }
            for key, quote in quotes.items()
            if key in {"btc", "eth", "dxy", "nasdaq100", "kospi", "us10y", "wti", "gold"}
        }
        cutoff = (now - timedelta(days=7)).isoformat()
        with self._connect(write=True) as db:
            db.execute(
                """
                INSERT INTO market_snapshots(captured_at, quotes_json)
                VALUES(?, ?)
                """,
                (now.isoformat(), self._json(payload)),
            )
            db.execute(
                "DELETE FROM market_snapshots WHERE captured_at<?", (cutoff,)
            )

    def latest_market_snapshot(
        self, *, before: datetime | None = None
    ) -> dict[str, Any]:
        query = "SELECT captured_at, quotes_json FROM market_snapshots"
        params: tuple[Any, ...] = ()
        if before:
            query += " WHERE captured_at<=?"
            params = (before.astimezone(UTC).isoformat(),)
        query += " ORDER BY captured_at DESC LIMIT 1"
        with self._connect() as db:
            row = db.execute(query, params).fetchone()
        if not row:
            return {}
        return {
            "captured_at": row["captured_at"],
            "quotes": self._loads(row["quotes_json"], {}),
        }

    def claim_telegram_update(
        self,
        update_id: int,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        now = datetime.now(UTC).isoformat()
        with self._connect(write=True) as db:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO telegram_updates(
                    update_id, payload_json, status, created_at, updated_at
                ) VALUES(?, ?, 'claimed', ?, ?)
                """,
                (update_id, self._json(payload or {}), now, now),
            )
            return cursor.rowcount == 1

    def mark_telegram_update(
        self,
        update_id: int,
        status: str,
        *,
        error: str = "",
    ) -> None:
        with self._connect(write=True) as db:
            db.execute(
                """
                UPDATE telegram_updates
                SET status=?,
                    attempts=attempts + CASE WHEN ?='processing' THEN 1 ELSE 0 END,
                    last_error=?, updated_at=?
                WHERE update_id=?
                """,
                (
                    status,
                    status,
                    error[:500],
                    datetime.now(UTC).isoformat(),
                    update_id,
                ),
            )

    def pending_telegram_updates(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT update_id, payload_json, attempts
                FROM telegram_updates
                WHERE status IN ('claimed', 'retry') AND attempts<3
                ORDER BY update_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "update_id": int(row["update_id"]),
                "payload": self._loads(row["payload_json"], {}),
                "attempts": int(row["attempts"]),
            }
            for row in rows
        ]

    def forget_telegram_update(self, update_id: int) -> None:
        with self._connect(write=True) as db:
            db.execute(
                "DELETE FROM telegram_updates WHERE update_id=?", (update_id,)
            )

    def claim_usage_slot(
        self,
        category: str,
        daily_limit: int,
        *,
        shared_limit: int | None = None,
    ) -> bool:
        today = datetime.now(KST).date().isoformat()
        with self._connect(write=True) as db:
            total = db.execute(
                "SELECT COALESCE(SUM(count), 0) AS count FROM usage_counters WHERE usage_date=?",
                (today,),
            ).fetchone()
            row = db.execute(
                """
                SELECT count FROM usage_counters
                WHERE usage_date=? AND category=?
                """,
                (today, category),
            ).fetchone()
            current = int(row["count"]) if row else 0
            if current >= max(daily_limit, 0):
                return False
            if shared_limit is not None and int(total["count"]) >= shared_limit:
                return False
            db.execute(
                """
                INSERT INTO usage_counters(usage_date, category, count)
                VALUES(?, ?, 1)
                ON CONFLICT(usage_date, category)
                DO UPDATE SET count=count+1
                """,
                (today, category),
            )
            return True

    def release_usage_slot(self, category: str) -> None:
        today = datetime.now(KST).date().isoformat()
        with self._connect(write=True) as db:
            db.execute(
                """
                UPDATE usage_counters SET count=MAX(count-1, 0)
                WHERE usage_date=? AND category=?
                """,
                (today, category),
            )

    def usage_summary(self) -> dict[str, int]:
        today = datetime.now(KST).date().isoformat()
        with self._connect() as db:
            rows = db.execute(
                "SELECT category, count FROM usage_counters WHERE usage_date=?",
                (today,),
            ).fetchall()
        return {str(row["category"]): int(row["count"]) for row in rows}

    def claim_ai_advisor_slot(self, daily_limit: int) -> bool:
        return self.claim_usage_slot("advisor", daily_limit)

    def release_ai_advisor_slot(self) -> None:
        self.release_usage_slot("advisor")

    def claim_job(self, job_key: str, lease_seconds: int = 900) -> bool:
        now = datetime.now(UTC)
        lease_until = now + timedelta(seconds=lease_seconds)
        with self._connect(write=True) as db:
            row = db.execute(
                "SELECT status, lease_until FROM jobs WHERE job_key=?", (job_key,)
            ).fetchone()
            if row:
                if row["status"] == "completed":
                    return False
                if row["lease_until"] and datetime.fromisoformat(row["lease_until"]) > now:
                    return False
                db.execute(
                    """
                    UPDATE jobs SET status='running', lease_until=?,
                        attempts=attempts+1, started_at=?, last_error=''
                    WHERE job_key=?
                    """,
                    (lease_until.isoformat(), now.isoformat(), job_key),
                )
            else:
                db.execute(
                    """
                    INSERT INTO jobs(
                        job_key, status, lease_until, attempts, started_at
                    ) VALUES(?, 'running', ?, 1, ?)
                    """,
                    (job_key, lease_until.isoformat(), now.isoformat()),
                )
            return True

    def finish_job(
        self,
        job_key: str,
        *,
        success: bool,
        error: str = "",
    ) -> None:
        with self._connect(write=True) as db:
            db.execute(
                """
                UPDATE jobs SET status=?, lease_until=NULL, completed_at=?,
                    last_error=?
                WHERE job_key=?
                """,
                (
                    "completed" if success else "retry",
                    datetime.now(UTC).isoformat() if success else None,
                    error[:500],
                    job_key,
                ),
            )

    def job_record(self, job_key: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_key=?", (job_key,)).fetchone()
        return dict(row) if row else {}

    def delivery_sent(self, delivery_key: str, stage: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT 1 FROM deliveries WHERE delivery_key=? AND stage=?
                """,
                (delivery_key, stage),
            ).fetchone()
        return bool(row)

    def mark_delivery(
        self,
        delivery_key: str,
        stage: str,
        *,
        telegram_message_id: int | None = None,
        content_hash: str = "",
    ) -> None:
        with self._connect(write=True) as db:
            db.execute(
                """
                INSERT OR REPLACE INTO deliveries(
                    delivery_key, stage, telegram_message_id, content_hash, sent_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    delivery_key,
                    stage,
                    telegram_message_id,
                    content_hash,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def cache_set(
        self,
        key: str,
        payload: Any,
        *,
        source: str,
        ttl_seconds: int,
    ) -> None:
        now = datetime.now(UTC)
        with self._connect(write=True) as db:
            db.execute(
                """
                INSERT INTO provider_cache(
                    cache_key, payload_json, source, fetched_at, expires_at
                ) VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    source=excluded.source,
                    fetched_at=excluded.fetched_at,
                    expires_at=excluded.expires_at
                """,
                (
                    key,
                    self._json(payload),
                    source,
                    now.isoformat(),
                    (now + timedelta(seconds=ttl_seconds)).isoformat(),
                ),
            )

    def cache_get(
        self,
        key: str,
        *,
        max_stale_seconds: int | None = None,
    ) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM provider_cache WHERE cache_key=?", (key,)
            ).fetchone()
        if not row:
            return None
        fetched_at = datetime.fromisoformat(row["fetched_at"])
        now = datetime.now(UTC)
        expired = datetime.fromisoformat(row["expires_at"]) < now
        if (
            expired
            and max_stale_seconds is not None
            and now - fetched_at > timedelta(seconds=max_stale_seconds)
        ):
            return None
        if expired and max_stale_seconds is None:
            return None
        return {
            "payload": self._loads(row["payload_json"], None),
            "source": row["source"],
            "fetched_at": fetched_at,
            "stale": expired,
        }

    def record_provider_result(
        self,
        provider: str,
        *,
        success: bool,
        error: str = "",
    ) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        with self._connect(write=True) as db:
            row = db.execute(
                "SELECT * FROM provider_health WHERE provider=?", (provider,)
            ).fetchone()
            previous_failures = int(row["consecutive_failures"]) if row else 0
            previous_notified = int(row["notified"]) if row else 0
            failures = 0 if success else previous_failures + 1
            notified = previous_notified
            recovered = bool(success and previous_failures >= 3 and previous_notified)
            if success:
                notified = 1 if recovered else 0
            db.execute(
                """
                INSERT INTO provider_health(
                    provider, consecutive_failures, last_success_at,
                    last_failure_at, last_error, notified
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    consecutive_failures=excluded.consecutive_failures,
                    last_success_at=COALESCE(
                        excluded.last_success_at, provider_health.last_success_at
                    ),
                    last_failure_at=COALESCE(
                        excluded.last_failure_at, provider_health.last_failure_at
                    ),
                    last_error=excluded.last_error,
                    notified=excluded.notified
                """,
                (
                    provider,
                    failures,
                    now if success else None,
                    None if success else now,
                    "RECOVERED" if recovered else "" if success else error[:500],
                    notified,
                ),
            )
        return {
            "provider": provider,
            "consecutive_failures": failures,
            "notified": bool(notified),
            "recovered": recovered,
        }

    def provider_health(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM provider_health ORDER BY provider"
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_provider_notified(self, provider: str) -> None:
        with self._connect(write=True) as db:
            db.execute(
                "UPDATE provider_health SET notified=1 WHERE provider=?", (provider,)
            )

    def clear_provider_notified(self, provider: str) -> None:
        with self._connect(write=True) as db:
            db.execute(
                """
                UPDATE provider_health
                SET notified=0, last_error=''
                WHERE provider=?
                """,
                (provider,),
            )

    def get_chat_context(self, chat_id: str, max_age_hours: int = 24) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload_json, updated_at FROM chat_context WHERE chat_id=?",
                (str(chat_id),),
            ).fetchone()
        if not row:
            return {}
        if datetime.now(UTC) - datetime.fromisoformat(row["updated_at"]) > timedelta(
            hours=max_age_hours
        ):
            return {}
        return self._loads(row["payload_json"], {})

    def set_chat_context(self, chat_id: str, payload: dict[str, Any]) -> None:
        with self._connect(write=True) as db:
            db.execute(
                """
                INSERT INTO chat_context(chat_id, payload_json, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (
                    str(chat_id),
                    self._json(payload),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def reset_chat_context(self, chat_id: str) -> None:
        with self._connect(write=True) as db:
            db.execute("DELETE FROM chat_context WHERE chat_id=?", (str(chat_id),))

    def preferences(self, chat_id: str) -> dict[str, Any]:
        defaults = {
            "emergency_alerts": True,
            "event_alerts": True,
            "muted_until": "",
        }
        with self._connect() as db:
            row = db.execute(
                "SELECT payload_json FROM user_preferences WHERE chat_id=?",
                (str(chat_id),),
            ).fetchone()
        if row:
            defaults.update(self._loads(row["payload_json"], {}))
        return defaults

    def update_preferences(self, chat_id: str, **values: Any) -> dict[str, Any]:
        payload = self.preferences(chat_id)
        payload.update(values)
        with self._connect(write=True) as db:
            db.execute(
                """
                INSERT INTO user_preferences(chat_id, payload_json, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (
                    str(chat_id),
                    self._json(payload),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return payload

    def is_muted(self, chat_id: str) -> bool:
        raw = str(self.preferences(chat_id).get("muted_until") or "")
        if not raw:
            return False
        try:
            return datetime.fromisoformat(raw) > datetime.now(UTC)
        except ValueError:
            return False

    def create_price_alert(
        self,
        chat_id: str,
        asset_key: str,
        direction: str,
        threshold: float,
        *,
        recurring: bool = False,
        max_alerts: int = 5,
    ) -> int:
        with self._connect(write=True) as db:
            count = db.execute(
                """
                SELECT COUNT(*) AS count FROM price_alerts
                WHERE chat_id=? AND active=1
                """,
                (str(chat_id),),
            ).fetchone()
            if int(count["count"]) >= max_alerts:
                raise ValueError("price alert limit reached")
            cursor = db.execute(
                """
                INSERT INTO price_alerts(
                    chat_id, asset_key, direction, threshold, recurring, created_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    str(chat_id),
                    asset_key,
                    direction,
                    threshold,
                    1 if recurring else 0,
                    datetime.now(UTC).isoformat(),
                ),
            )
            return int(cursor.lastrowid)

    def list_price_alerts(self, chat_id: str, *, active_only: bool = True) -> list[dict[str, Any]]:
        query = "SELECT * FROM price_alerts WHERE chat_id=?"
        if active_only:
            query += " AND active=1"
        query += " ORDER BY id"
        with self._connect() as db:
            rows = db.execute(query, (str(chat_id),)).fetchall()
        return [dict(row) for row in rows]

    def delete_price_alert(self, chat_id: str, alert_id: int) -> bool:
        with self._connect(write=True) as db:
            cursor = db.execute(
                """
                UPDATE price_alerts SET active=0
                WHERE id=? AND chat_id=? AND active=1
                """,
                (alert_id, str(chat_id)),
            )
            return cursor.rowcount == 1

    def update_price_alert_state(
        self,
        alert_id: int,
        *,
        active: bool | None = None,
        armed: bool | None = None,
        triggered: bool = False,
    ) -> None:
        assignments: list[str] = []
        values: list[Any] = []
        if active is not None:
            assignments.append("active=?")
            values.append(1 if active else 0)
        if armed is not None:
            assignments.append("armed=?")
            values.append(1 if armed else 0)
        if triggered:
            assignments.append("last_triggered_at=?")
            values.append(datetime.now(UTC).isoformat())
        if not assignments:
            return
        values.append(alert_id)
        with self._connect(write=True) as db:
            db.execute(
                f"UPDATE price_alerts SET {', '.join(assignments)} WHERE id=?",
                tuple(values),
            )

    def save_message(
        self,
        key: str,
        text: str,
        *,
        parse_mode: str = "HTML",
    ) -> None:
        with self._connect(write=True) as db:
            db.execute(
                """
                INSERT OR REPLACE INTO saved_messages(
                    message_key, text, parse_mode, created_at
                ) VALUES(?, ?, ?, ?)
                """,
                (key, text, parse_mode, datetime.now(UTC).isoformat()),
            )

    def saved_message(self, key: str) -> dict[str, str] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM saved_messages WHERE message_key=?", (key,)
            ).fetchone()
        return dict(row) if row else None

    def latest_saved_message(self, prefix: str) -> dict[str, str] | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT * FROM saved_messages
                WHERE message_key LIKE ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (f"{prefix}%",),
            ).fetchone()
        return dict(row) if row else None

    def readiness(self) -> dict[str, Any]:
        with self._connect() as db:
            version = db.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
        return {
            "database": "ok",
            "schema_version": int(version["value"]) if version else 0,
            "path": str(self.path),
        }
