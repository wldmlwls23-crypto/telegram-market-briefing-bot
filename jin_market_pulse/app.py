from __future__ import annotations

import logging
import sys
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .alerts import (
    capture_due_event_baselines,
    monitor_emergency_alerts,
    send_due_event_results,
    send_due_pre_event_reminders,
)
from .calendar import fetch_economic_events
from .config import KST, Settings
from .models import MarketData
from .news import fetch_news
from .providers import critical_data_errors, fetch_market_quotes
from .reports import (
    create_morning_analysis,
    fallback_morning_analysis,
    render_data_health_alert,
    render_morning_report,
)
from .state import StateStore
from .telegram import TelegramClient


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


class MarketPulseApp:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.state = StateStore(settings.state_file)
        self.telegram = TelegramClient(settings)

    def collect_morning_data(self) -> MarketData:
        quotes, errors = fetch_market_quotes(self.settings)
        events = fetch_economic_events(self.settings, days_ahead=4)
        news = fetch_news()
        return MarketData(
            generated_at_kst=datetime.now(KST),
            quotes=quotes,
            events=events,
            news=news,
            errors=errors,
        )

    def send_morning_report(self) -> str:
        try:
            data = self.collect_morning_data()
            missing = critical_data_errors(data.quotes)
            if missing:
                self.telegram.send(render_data_health_alert(missing, data.errors))
                logging.error("Morning report withheld. Missing critical data: %s", missing)
                return "withheld"
            try:
                analysis = create_morning_analysis(data, self.settings)
            except Exception:
                logging.exception("OpenAI morning analysis failed; using data-only fallback.")
                analysis = fallback_morning_analysis(data)
            report = render_morning_report(data, analysis)
            self.telegram.send(report)
            self.state.add_market_snapshot(data.quotes)
            logging.info("Morning Market Report sent successfully.")
            return "sent"
        except Exception:
            logging.exception("Morning Market Report failed.")
            raise

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
                    lambda: monitor_emergency_alerts(
                        self.settings, self.state, self.telegram
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
