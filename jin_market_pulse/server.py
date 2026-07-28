from __future__ import annotations

import hmac
import logging
from threading import Lock

from fastapi import FastAPI, Header, HTTPException, status
from fastapi.concurrency import run_in_threadpool

from .app import MarketPulseApp, setup_logging
from .config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    api = FastAPI(
        title="JIN Market Pulse",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    morning_lock = Lock()

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

    return api


setup_logging()
app = create_app()
