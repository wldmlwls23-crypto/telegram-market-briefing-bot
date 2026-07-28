import os


def main() -> None:
    run_mode = os.getenv("RUN_MODE", "worker").strip().lower()
    if run_mode == "serverless":
        import uvicorn

        port = int(os.getenv("PORT", "8000"))
        uvicorn.run(
            "jin_market_pulse.server:app",
            host="0.0.0.0",
            port=port,
            proxy_headers=True,
        )
        return

    from jin_market_pulse.app import main as run_worker

    run_worker()


if __name__ == "__main__":
    main()
