from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import requests


def main() -> None:
    target = os.getenv("CRON_TARGET_URL", "").strip().rstrip("/")
    secret = os.getenv("CRON_SECRET", "").strip()
    if not target.startswith("https://") or len(secret) < 16:
        raise RuntimeError("CRON_TARGET_URL and CRON_SECRET are required")
    slot = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    response = requests.post(
        f"{target}/jobs/tick",
        headers={
            "Authorization": f"Bearer {secret}",
            "X-Idempotency-Key": f"railway-{slot}",
        },
        timeout=90,
    )
    response.raise_for_status()
    print("JIN Market Pulse tick completed.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Tick failed: {type(exc).__name__}", file=sys.stderr)
        raise
