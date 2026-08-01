from __future__ import annotations

import hmac
import logging
from threading import Lock
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool

from .app import MarketPulseApp, setup_logging
from .bot_queries import handle_market_query
from .config import Settings
from .jobs import process_telegram_update, run_tick
from .links import first_https_url
from .session_reports import REPORT_TYPES, send_session_report
from .state import StateStore
from .telegram import TelegramClient


def _authorized(value: str | None, secret: str) -> bool:
    expected = f"Bearer {secret}"
    return (
        len(secret) >= 16
        and value is not None
        and hmac.compare_digest(value, expected)
    )


def _webhook_chat_id(payload: dict[str, Any]) -> str:
    callback = payload.get("callback_query") or {}
    message = payload.get("message") or callback.get("message") or {}
    return str((message.get("chat") or {}).get("id") or "")


def create_app(settings: Settings | None = None) -> FastAPI:
    api = FastAPI(
        title="JIN Market Pulse",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    morning_lock = Lock()
    tick_lock = Lock()
    report_lock = Lock()

    def current_settings() -> Settings:
        return settings or Settings.from_env(require_secrets=True)

    def state_for(job_settings: Settings) -> StateStore:
        return StateStore(
            job_settings.state_db,
            legacy_json=job_settings.state_file,
        )

    def drain_telegram_queue(job_settings: Settings) -> None:
        store = state_for(job_settings)
        telegram = TelegramClient(job_settings)
        for item in store.pending_telegram_updates(limit=10):
            update_id = int(item["update_id"])
            payload = item["payload"]
            store.mark_telegram_update(update_id, "processing")
            try:
                message = payload.get("message") or {}
                text = str(message.get("text") or "").strip()
                simple_text = bool(
                    text
                    and not payload.get("callback_query")
                    and not message.get("photo")
                    and not message.get("voice")
                    and not message.get("forward_origin")
                    and not first_https_url(text)
                )
                if simple_text:
                    response = handle_market_query(
                        text,
                        job_settings,
                        store,
                    )
                    telegram.send(
                        response.text,
                        parse_mode=response.parse_mode,
                        reply_markup=response.reply_markup,
                    )
                else:
                    process_telegram_update(
                        payload,
                        job_settings,
                        store,
                        telegram,
                    )
                store.mark_telegram_update(update_id, "done")
            except Exception as exc:
                store.mark_telegram_update(
                    update_id,
                    "retry",
                    error=type(exc).__name__,
                )
                logging.exception("Telegram webhook background task failed.")

    @api.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "jin-market-pulse", "mode": "serverless"}

    @api.get("/ready")
    def ready() -> dict[str, Any]:
        job_settings = current_settings()
        readiness = state_for(job_settings).readiness()
        return {
            "status": "ready",
            "database": readiness["database"],
            "schema_version": readiness["schema_version"],
        }

    @api.post("/jobs/morning")
    async def morning_job(
        authorization: str | None = Header(default=None),
        x_idempotency_key: str | None = Header(default=None),
    ) -> dict[str, str]:
        job_settings = current_settings()
        if not _authorized(authorization, job_settings.cron_secret):
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
            app = MarketPulseApp(job_settings)
            if x_idempotency_key:
                result = await run_in_threadpool(
                    lambda: app.send_morning_report(
                        idempotency_key=x_idempotency_key[:100]
                    )
                )
            else:
                result = await run_in_threadpool(app.send_morning_report)
            return {"status": result}
        except Exception:
            logging.exception("Serverless morning job failed.")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Morning job failed",
            ) from None
        finally:
            morning_lock.release()

    @api.post("/jobs/tick")
    async def tick_job(
        authorization: str | None = Header(default=None),
        x_idempotency_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        job_settings = current_settings()
        if not _authorized(authorization, job_settings.cron_secret):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized",
            )
        if not tick_lock.acquire(blocking=False):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Tick is already running",
            )
        try:
            return await run_in_threadpool(
                lambda: run_tick(
                    job_settings,
                    idempotency_key=(
                        x_idempotency_key[:100]
                        if x_idempotency_key
                        else None
                    ),
                )
            )
        except Exception:
            logging.exception("Serverless tick failed.")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Tick failed",
            ) from None
        finally:
            tick_lock.release()

    @api.post("/jobs/report/{report_type}")
    async def report_job(
        report_type: str,
        deliver: bool = False,
        authorization: str | None = Header(default=None),
        x_idempotency_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        job_settings = current_settings()
        if not _authorized(authorization, job_settings.cron_secret):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized",
            )
        if report_type not in REPORT_TYPES | {"morning"}:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Unknown report type",
            )
        if deliver and (not x_idempotency_key or len(x_idempotency_key) < 8):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Delivery requires X-Idempotency-Key",
            )
        if not report_lock.acquire(blocking=False):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Report job is already running",
            )
        try:
            if report_type == "morning":
                market_app = MarketPulseApp(job_settings)
                if deliver:
                    result = await run_in_threadpool(
                        lambda: market_app.send_morning_report(
                            idempotency_key=str(x_idempotency_key)[:100]
                        )
                    )
                    return {"status": result, "report_type": report_type}
                text = await run_in_threadpool(market_app.preview_morning_report)
                return {
                    "status": "preview",
                    "report_type": report_type,
                    "characters": len(text),
                    "text": text,
                }
            store = state_for(job_settings)
            telegram = TelegramClient(job_settings)
            result = await run_in_threadpool(
                lambda: send_session_report(
                    report_type,
                    job_settings,
                    store,
                    telegram,
                    deliver=deliver,
                    idempotency_key=(
                        str(x_idempotency_key)[:100]
                        if x_idempotency_key
                        else None
                    ),
                )
            )
            return {
                "status": result.status,
                "report_type": report_type,
                "characters": len(result.text),
                "skip_reason": result.skip_reason,
                "text": result.text if not deliver else "",
            }
        except Exception:
            logging.exception("Manual report job failed: %s", report_type)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Report job failed",
            ) from None
        finally:
            report_lock.release()

    @api.post("/telegram/webhook")
    async def telegram_webhook(
        request: Request,
        background_tasks: BackgroundTasks,
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
        if not isinstance(update_id, int):
            return {"status": "ignored"}
        if _webhook_chat_id(payload) != str(job_settings.telegram_chat_id):
            return {"status": "ignored"}

        store = state_for(job_settings)
        if not store.claim_telegram_update(update_id, payload):
            return {"status": "duplicate"}
        background_tasks.add_task(drain_telegram_queue, job_settings)
        return {"status": "sent"}

    return api


setup_logging()
app = create_app()
