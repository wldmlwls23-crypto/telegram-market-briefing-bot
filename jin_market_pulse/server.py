from __future__ import annotations

import hmac
import logging
from threading import Lock

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool

from .app import MarketPulseApp, setup_logging
from .bot_queries import answer_market_query
from .config import Settings
from .state import StateStore
from .telegram import TelegramClient


def create_app(settings: Settings | None = None) -> FastAPI:
    api = FastAPI(
        title="JIN Market Pulse",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    morning_lock = Lock()
    telegram_lock = Lock()

    def current_settings() -> Settings:
        return settings or Settings.from_env(require_secrets=True)

    @api.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "jin-market-pulse", "mode": "serverless"}

    @api.post("/jobs/morning")
    async def morning_job(
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        job_settings = current_settings()
        expected = f"Bearer {job_settings.cron_secret}"
        if (
            len(job_settings.cron_secret) < 16
            or authorization is None
            or not hmac.compare_digest(authorization, expected)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized",
            )
        if not morning_lock.acquire(blocking=False):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Morning report is already running",
            )
        try:
            result = await run_in_threadpool(
                MarketPulseApp(job_settings).send_morning_report
            )
            return {"status": result}
        except Exception:
            logging.exception("Serverless morning job failed.")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Morning job failed",
            ) from None
        finally:
            morning_lock.release()

    @api.post("/telegram/webhook")
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: str | None = Header(default=None),
    ) -> dict[str, str]:
        job_settings = current_settings()
        expected = job_settings.telegram_webhook_secret
        if (
            len(expected) < 16
            or x_telegram_bot_api_secret_token is None
            or not hmac.compare_digest(
                x_telegram_bot_api_secret_token,
                expected,
            )
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized",
            )
        payload = await request.json()
        update_id = payload.get("update_id")
        message = payload.get("message") or {}
        chat = message.get("chat") or {}
        text = str(message.get("text") or "").strip()
        if not isinstance(update_id, int) or not text:
            return {"status": "ignored"}
        if str(chat.get("id")) != str(job_settings.telegram_chat_id):
            return {"status": "ignored"}

        store = StateStore(job_settings.state_file)
        if not store.claim_telegram_update(update_id):
            return {"status": "duplicate"}
        if not telegram_lock.acquire(blocking=False):
            store.forget_telegram_update(update_id)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Another Telegram query is running",
            )
        try:
            answer = await run_in_threadpool(
                answer_market_query,
                text,
                job_settings,
            )
            await run_in_threadpool(
                lambda: TelegramClient(job_settings).send(
                    answer,
                    parse_mode="HTML",
                )
            )
            return {"status": "sent"}
        except Exception:
            store.forget_telegram_update(update_id)
            logging.exception("Telegram query failed.")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Telegram query failed",
            ) from None
        finally:
            telegram_lock.release()

    return api


setup_logging()
app = create_app()
