from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


KST = ZoneInfo("Asia/Seoul")
PARIS = ZoneInfo("Europe/Paris")
NEW_YORK = ZoneInfo("America/New_York")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_csv(name: str, default: str) -> tuple[str, ...]:
    value = os.getenv(name, default)
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: str
    openai_api_key: str
    openai_model: str
    openai_reasoning_effort: str
    openai_web_search: bool
    fmp_api_key: str | None
    state_dir: Path
    enabled_reports: tuple[str, ...]
    enable_emergency_alerts: bool
    run_on_start: bool
    request_timeout_seconds: int
    run_mode: str = "worker"
    cron_secret: str = ""
    telegram_webhook_secret: str = ""
    port: int = 8000
    openai_max_output_tokens: int = 2500

    @property
    def state_file(self) -> Path:
        return self.state_dir / "sent_alerts.json"

    @classmethod
    def from_env(cls, *, require_secrets: bool = True) -> "Settings":
        load_dotenv()

        def secret(name: str) -> str:
            value = os.getenv(name, "").strip()
            if require_secrets and not value:
                raise RuntimeError(f"Missing required environment variable: {name}")
            return value

        state_dir = Path(os.getenv("STATE_DIR", ".")).expanduser()
        return cls(
            telegram_bot_token=secret("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=secret("TELEGRAM_CHAT_ID"),
            openai_api_key=secret("OPENAI_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.6-luna").strip(),
            openai_reasoning_effort=os.getenv("OPENAI_REASONING_EFFORT", "low").strip(),
            openai_web_search=env_bool("OPENAI_WEB_SEARCH", True),
            fmp_api_key=os.getenv("FMP_API_KEY", "").strip() or None,
            state_dir=state_dir,
            enabled_reports=env_csv("ENABLED_REPORTS", "morning"),
            enable_emergency_alerts=env_bool("ENABLE_EMERGENCY_ALERTS", False),
            run_on_start=env_bool("RUN_ON_START", False),
            request_timeout_seconds=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "25")),
            run_mode=os.getenv("RUN_MODE", "worker").strip().lower(),
            cron_secret=os.getenv("CRON_SECRET", "").strip(),
            telegram_webhook_secret=os.getenv(
                "TELEGRAM_WEBHOOK_SECRET", ""
            ).strip(),
            port=int(os.getenv("PORT", "8000")),
            openai_max_output_tokens=int(
                os.getenv("OPENAI_MAX_OUTPUT_TOKENS", "2500")
            ),
        )
