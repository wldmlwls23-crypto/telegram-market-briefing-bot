from __future__ import annotations

from jin_market_pulse.config import Settings
from jin_market_pulse.telegram import TelegramClient


def main() -> None:
    settings = Settings.from_env(require_secrets=False)
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    if not settings.public_base_url:
        raise RuntimeError("PUBLIC_BASE_URL is required")
    if len(settings.telegram_webhook_secret) < 16:
        raise RuntimeError("TELEGRAM_WEBHOOK_SECRET must be at least 16 characters")

    telegram = TelegramClient(settings)
    telegram.set_commands()
    telegram.set_webhook(
        settings.public_base_url,
        settings.telegram_webhook_secret,
    )
    info = telegram.webhook_info()
    expected_url = (
        f"{settings.public_base_url.rstrip('/')}/telegram/webhook"
    )
    if info.get("url") != expected_url:
        raise RuntimeError("Telegram webhook verification failed")

    pending = int(info.get("pending_update_count") or 0)
    print(f"Telegram commands and webhook configured. Pending updates: {pending}")


if __name__ == "__main__":
    main()
