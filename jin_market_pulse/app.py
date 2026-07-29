from __future__ import annotations

import hashlib
import logging
import re
import sys
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .alerts import (
    capture_due_event_baselines,
    send_due_event_results,
    send_due_pre_event_reminders,
)
from .breaking import monitor_breaking_alerts
from .calendar import fetch_economic_events
from .chart import render_btc_chart
from .config import KST, PARIS, Settings
from .models import MarketData
from .news import fetch_news
from .providers import (
    btc_quote_from_series,
    critical_data_errors,
    fetch_btc_intraday_series,
    fetch_market_quotes,
)
from .reports import (
    create_morning_analysis,
    fallback_morning_analysis,
    render_data_health_alert,
    render_morning_report,
)
from .state import StateStore
from .session_reports import send_session_report
from .telegram import TelegramClient


def _redact(value: object) -> object:
    if not isinstance(value, str):
        return value
    patterns = (
        (r"\bsk-[A-Za-z0-9_-]{12,}\b", "[OPENAI_KEY]"),
        (r"\bbot\d+:[A-Za-z0-9_-]{12,}\b", "bot[TELEGRAM_TOKEN]"),
        (
            r"(?i)(apikey|api_key|token|secret)=([^&\s]+)",
            r"\1=[REDACTED]",
        ),
    )
    for pattern, replacement in patterns:
        value = re.sub(pattern, replacement, value)
    return value


class SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                key: _redact(value)
                for key, value in record.args.items()
            }
        return True


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(SensitiveDataFilter())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[handler],
        force=True,
    )


class MarketPulseApp:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.state = StateStore(
            settings.state_db,
            legacy_json=settings.state_file,
        )
        self.telegram = TelegramClient(settings)

    def collect_morning_data(self) -> MarketData:
        quotes, errors = fetch_market_quotes(self.settings, self.state)
        events = fetch_economic_events(
            self.settings,
            days_ahead=4,
            store=self.state,
        )
        recent_facts = self.state.recent_report_facts(hours=18)
        news = [
            item
            for item in fetch_news()
            if f"news:{item.topic_key}" not in recent_facts
        ]
        btc_series = None
        try:
            btc_series = fetch_btc_intraday_series(self.settings)
            quotes["btc"] = btc_quote_from_series(btc_series)
        except Exception as exc:
            errors.append(f"BTC chart: {exc}")
            logging.warning("BTC chart data unavailable; sending text only.")
        return MarketData(
            generated_at_kst=datetime.now(KST),
            quotes=quotes,
            events=events,
            news=news,
            btc_series=btc_series,
            errors=errors,
        )

    def send_morning_report(
        self,
        *,
        idempotency_key: str | None = None,
    ) -> str:
        delivery_key = (
            f"morning:{idempotency_key}"
            if idempotency_key
            else f"morning:{datetime.now(KST):%Y-%m-%d}"
        )
        if not self.state.claim_job(delivery_key, lease_seconds=15 * 60):
            return "duplicate"
        try:
            data = self.collect_morning_data()
            missing = critical_data_errors(data.quotes)
            if missing:
                if not self.state.delivery_sent(delivery_key, "health"):
                    message_ids = self.telegram.send(
                        render_data_health_alert(missing, data.errors)
                    )
                    self.state.mark_delivery(
                        delivery_key,
                        "health",
                        telegram_message_id=message_ids[0] if message_ids else None,
                    )
                self.state.finish_job(delivery_key, success=True)
                logging.error("Morning report withheld. Missing critical data: %s", missing)
                return "withheld"
            try:
                analysis = create_morning_analysis(data, self.settings)
            except Exception:
                logging.exception("OpenAI morning analysis failed; using data-only fallback.")
                analysis = fallback_morning_analysis(data)
            report = render_morning_report(data, analysis)
            if data.btc_series and not self.state.delivery_sent(delivery_key, "chart"):
                try:
                    message_id = self.telegram.send_photo(
                        render_btc_chart(data.btc_series)
                    )
                    self.state.mark_delivery(
                        delivery_key,
                        "chart",
                        telegram_message_id=message_id,
                    )
                except Exception:
                    logging.exception("BTC chart send failed; continuing with text report.")
            text_message_id = None
            if not self.state.delivery_sent(delivery_key, "text"):
                message_ids = self.telegram.send(
                    report,
                    parse_mode="HTML",
                )
                text_message_id = message_ids[0] if message_ids else None
                self.state.mark_delivery(
                    delivery_key,
                    "text",
                    telegram_message_id=text_message_id,
                    content_hash=hashlib.sha256(
                        report.encode("utf-8")
                    ).hexdigest(),
                )
            self.state.add_market_snapshot(data.quotes)
            self.state.save_message(delivery_key, report, parse_mode="HTML")
            report_facts = [
                {
                    "fact_key": f"asset:{key}",
                    "numeric_value": quote.percent_change,
                    "direction": (
                        1
                        if (quote.percent_change or 0) > 0
                        else -1
                        if (quote.percent_change or 0) < 0
                        else 0
                    ),
                    "official": False,
                }
                for key, quote in data.quotes.items()
                if quote.percent_change is not None
            ]
            report_facts.extend(
                {
                    "fact_key": f"news:{item.topic_key}",
                    "numeric_value": None,
                    "direction": 0,
                    "official": item.official_source,
                }
                for signal in analysis.signals
                if (item := next(
                    (
                        candidate
                        for candidate in data.news
                        if candidate.news_id == signal.candidate_id
                    ),
                    None,
                ))
            )
            self.state.record_report_run(
                delivery_key,
                "morning",
                datetime.now(KST).date().isoformat(),
                "sent",
                text=report,
                facts=report_facts,
                telegram_message_id=text_message_id,
            )
            self.state.finish_job(delivery_key, success=True)
            logging.info("Morning Market Report sent successfully.")
            return "sent"
        except Exception as exc:
            self.state.finish_job(
                delivery_key,
                success=False,
                error=type(exc).__name__,
            )
            logging.exception("Morning Market Report failed.")
            raise

    def preview_morning_report(self) -> str:
        data = self.collect_morning_data()
        missing = critical_data_errors(data.quotes)
        if missing:
            return render_data_health_alert(missing, data.errors)
        try:
            analysis = create_morning_analysis(data, self.settings)
        except Exception:
            logging.exception("OpenAI morning preview failed; using data-only fallback.")
            analysis = fallback_morning_analysis(data)
        return render_morning_report(data, analysis)

    def run_guarded(self, name: str, callback: object) -> None:
        try:
            callback()  # type: ignore[operator]
        except Exception:
            logging.exception("Scheduled job failed: %s", name)

    def start(self) -> None:
        scheduler = BlockingScheduler(timezone=KST)
        if "morning" in self.settings.enabled_reports:
            scheduler.add_job(
                self.send_morning_report,
                CronTrigger(hour=6, minute=50, timezone=KST),
                id="report_morning",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=300,
            )
        if "korea_close" in self.settings.enabled_reports:
            scheduler.add_job(
                lambda: send_session_report(
                    "korea_close",
                    self.settings,
                    self.state,
                    self.telegram,
                    deliver=True,
                ),
                CronTrigger(hour=15, minute=50, timezone=KST),
                id="report_korea_close",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=300,
            )
        if "europe_close" in self.settings.enabled_reports:
            scheduler.add_job(
                lambda: send_session_report(
                    "europe_close",
                    self.settings,
                    self.state,
                    self.telegram,
                    deliver=True,
                ),
                CronTrigger(hour=18, minute=5, timezone=PARIS),
                id="report_europe_close",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=300,
            )

        scheduler.add_job(
            lambda: self.run_guarded(
                "pre_event_reminders",
                lambda: send_due_pre_event_reminders(
                    self.settings, self.state, self.telegram
                ),
            ),
            "interval",
            minutes=30,
            id="pre_event_reminders",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            lambda: self.run_guarded(
                "event_baselines",
                lambda: capture_due_event_baselines(self.settings, self.state),
            ),
            "interval",
            minutes=5,
            id="event_baselines",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            lambda: self.run_guarded(
                "event_results",
                lambda: send_due_event_results(
                    self.settings, self.state, self.telegram
                ),
            ),
            "interval",
            minutes=10,
            id="event_results",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        if self.settings.enable_emergency_alerts:
            scheduler.add_job(
                lambda: self.run_guarded(
                    "emergency_alerts",
                    lambda: monitor_breaking_alerts(
                        self.settings,
                        self.state,
                        self.telegram,
                        check_prices=(
                            18 <= datetime.now(KST).minute <= 27
                            or 48 <= datetime.now(KST).minute <= 57
                        ),
                        disable_notification=bool(
                            self.state.preferences(
                                self.settings.telegram_chat_id
                            ).get("overnight_silent", True)
                            and datetime.now(KST).hour < 6
                        ),
                    ),
                ),
                "interval",
                minutes=15,
                id="emergency_alerts",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )

        logging.info(
            "JIN Market Pulse v2 started. Reports=%s emergency=%s state=%s",
            ",".join(self.settings.enabled_reports),
            self.settings.enable_emergency_alerts,
            self.settings.state_file,
        )
        scheduler.start()


def main() -> None:
    setup_logging()
    settings = Settings.from_env(require_secrets=True)
    app = MarketPulseApp(settings)
    if settings.run_on_start:
        app.send_morning_report()
    app.start()
