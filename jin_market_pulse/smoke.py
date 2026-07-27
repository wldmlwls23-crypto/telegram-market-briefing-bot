from __future__ import annotations

import json

from .calendar import fetch_economic_events
from .config import Settings
from .news import fetch_news
from .providers import fetch_market_quotes, provider_health_summary


def main() -> None:
    settings = Settings.from_env(require_secrets=False)
    quotes, errors = fetch_market_quotes(settings)
    events = fetch_economic_events(settings, days_ahead=4)
    news = fetch_news()
    payload = {
        "provider_health": provider_health_summary(quotes, errors),
        "quotes": {
            key: {
                "current": quote.current,
                "previous": quote.previous,
                "percent_change": quote.percent_change,
                "as_of": quote.as_of.isoformat(),
                "source": quote.source,
            }
            for key, quote in quotes.items()
        },
        "future_event_count": len(events),
        "trusted_news_count": len(news),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
