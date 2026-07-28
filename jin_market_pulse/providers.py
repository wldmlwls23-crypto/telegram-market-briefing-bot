from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import quote as url_quote

import requests

from .config import KST, Settings
from .models import AssetQuote, PricePoint, PriceSeries


UTC = timezone.utc
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
TREASURY_XML_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/"
    "interest-rates/pages/xml"
)
FMP_QUOTE_URL = "https://financialmodelingprep.com/stable/quote"

YAHOO_ASSETS: dict[str, dict[str, str]] = {
    "sp500": {"symbol": "^GSPC", "name": "S&P 500", "kind": "index", "unit": "pt"},
    "nasdaq100": {"symbol": "^NDX", "name": "Nasdaq 100", "kind": "index", "unit": "pt"},
    "dow": {"symbol": "^DJI", "name": "Dow Jones", "kind": "index", "unit": "pt"},
    "kospi": {"symbol": "^KS11", "name": "KOSPI", "kind": "index", "unit": "pt"},
    "kosdaq": {"symbol": "^KQ11", "name": "KOSDAQ", "kind": "index", "unit": "pt"},
    "usdkrw": {"symbol": "KRW=X", "name": "원/달러", "kind": "fx", "unit": "원"},
    "dxy": {"symbol": "DX-Y.NYB", "name": "DXY", "kind": "fx", "unit": ""},
    "gold": {"symbol": "GC=F", "name": "금", "kind": "commodity", "unit": "USD"},
    "wti": {"symbol": "CL=F", "name": "WTI 유가", "kind": "commodity", "unit": "USD"},
}

YAHOO_CRYPTO_ASSETS: dict[str, dict[str, str]] = {
    "btc": {"symbol": "BTC-USD", "name": "BTC", "kind": "crypto", "unit": "USD"},
    "eth": {"symbol": "ETH-USD", "name": "ETH", "kind": "crypto", "unit": "USD"},
}

FMP_SYMBOLS = {
    "sp500": "^GSPC",
    "nasdaq100": "^NDX",
    "dow": "^DJI",
    "usdkrw": "USDKRW",
    "gold": "GCUSD",
    "wti": "CLUSD",
}


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (compatible; JIN-Market-Pulse/2.0; "
                "+https://github.com/wldmlwls23-crypto/telegram-market-briefing-bot)"
            )
        }
    )
    return session


def _changes(current: float, previous: float | None) -> tuple[float | None, float | None]:
    if previous in {None, 0}:
        return None, None
    absolute = current - previous
    return absolute, absolute / previous * 100


def _is_stale(as_of: datetime, kind: str, market_state: str) -> bool:
    age = datetime.now(UTC) - as_of.astimezone(UTC)
    if kind == "crypto":
        return age > timedelta(hours=2)
    if market_state.upper() in {"CLOSED", "PRE", "POST"}:
        return age > timedelta(days=4)
    return age > timedelta(hours=36)


def fetch_crypto_quotes(settings: Settings) -> dict[str, AssetQuote]:
    response = _session().get(
        COINGECKO_URL,
        params={
            "ids": "bitcoin,ethereum",
            "vs_currencies": "usd",
            "include_24hr_change": "true",
            "include_last_updated_at": "true",
        },
        timeout=settings.request_timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    result: dict[str, AssetQuote] = {}
    definitions = {
        "bitcoin": ("btc", "BTC"),
        "ethereum": ("eth", "ETH"),
    }
    for provider_id, (key, name) in definitions.items():
        item = payload.get(provider_id, {})
        current = float(item["usd"])
        percent_change = float(item.get("usd_24h_change") or 0)
        previous = current / (1 + percent_change / 100) if percent_change != -100 else None
        absolute, _ = _changes(current, previous)
        updated_at = datetime.fromtimestamp(
            int(item.get("last_updated_at") or datetime.now(UTC).timestamp()),
            UTC,
        )
        result[key] = AssetQuote(
            key=key,
            name_ko=name,
            kind="crypto",
            current=current,
            previous=previous,
            absolute_change=absolute,
            percent_change=percent_change,
            as_of=updated_at,
            market_state="OPEN",
            source="CoinGecko",
            comparison_label="24시간 전",
            stale=_is_stale(updated_at, "crypto", "OPEN"),
            unit="USD",
        )
    return result


def fetch_yahoo_quote(
    key: str,
    definition: dict[str, str],
    settings: Settings,
) -> AssetQuote:
    symbol = url_quote(definition["symbol"], safe="")
    response = _session().get(
        YAHOO_CHART_URL.format(symbol=symbol),
        params={"range": "5d", "interval": "1d", "includePrePost": "true"},
        timeout=settings.request_timeout_seconds,
    )
    response.raise_for_status()
    result = response.json()["chart"]["result"][0]
    meta = result["meta"]
    current_raw = meta.get("regularMarketPrice")
    previous_raw = meta.get("chartPreviousClose") or meta.get("previousClose")
    if current_raw is None:
        closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        current_raw = next((value for value in reversed(closes) if value is not None), None)
    if current_raw is None:
        raise RuntimeError(f"Yahoo returned no current price for {definition['symbol']}")
    current = float(current_raw)
    previous = float(previous_raw) if previous_raw not in {None, 0} else None
    absolute, percent = _changes(current, previous)
    timestamp = int(meta.get("regularMarketTime") or datetime.now(UTC).timestamp())
    as_of = datetime.fromtimestamp(timestamp, UTC)
    market_state = str(meta.get("marketState") or "CLOSED")
    return AssetQuote(
        key=key,
        name_ko=definition["name"],
        kind=definition["kind"],  # type: ignore[arg-type]
        current=current,
        previous=previous,
        absolute_change=absolute,
        percent_change=percent,
        as_of=as_of,
        market_state=market_state,
        source="Yahoo Finance",
        comparison_label="전일 종가",
        stale=_is_stale(as_of, definition["kind"], market_state),
        unit=definition["unit"],
    )


def fetch_yahoo_crypto_quotes(settings: Settings) -> dict[str, AssetQuote]:
    result: dict[str, AssetQuote] = {}
    for key, definition in YAHOO_CRYPTO_ASSETS.items():
        quote = fetch_yahoo_quote(key, definition, settings)
        quote.comparison_label = "previous UTC close"
        if not quote.stale:
            result[key] = quote
    return result


def _fetch_yahoo_intraday(
    settings: Settings,
    *,
    interval: str,
) -> PriceSeries:
    response = _session().get(
        YAHOO_CHART_URL.format(symbol="BTC-USD"),
        params={"range": "2d", "interval": interval, "includePrePost": "true"},
        timeout=settings.request_timeout_seconds,
    )
    response.raise_for_status()
    result = response.json()["chart"]["result"][0]
    timestamps = result.get("timestamp") or []
    closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
    points = [
        PricePoint(
            timestamp=datetime.fromtimestamp(int(timestamp), UTC),
            value=float(value),
        )
        for timestamp, value in zip(timestamps, closes)
        if value is not None
    ]
    if points:
        cutoff = points[-1].timestamp - timedelta(hours=24)
        points = [point for point in points if point.timestamp >= cutoff]
    return PriceSeries(
        key="btc",
        name="BTC",
        points=points,
        source="Yahoo Finance",
    )


def fetch_btc_intraday_series(settings: Settings) -> PriceSeries:
    errors: list[str] = []
    for interval in ("5m", "15m"):
        try:
            series = _fetch_yahoo_intraday(settings, interval=interval)
            if len(series.points) >= 20:
                return series
            errors.append(f"{interval}: {len(series.points)} valid points")
        except Exception as exc:
            errors.append(f"{interval}: {exc}")
    raise RuntimeError("BTC intraday series unavailable: " + "; ".join(errors))


def btc_quote_from_series(series: PriceSeries) -> AssetQuote:
    if len(series.points) < 2:
        raise ValueError("BTC quote requires at least two price points")
    previous = series.points[0].value
    current = series.points[-1].value
    absolute, percent = _changes(current, previous)
    as_of = series.points[-1].timestamp
    return AssetQuote(
        key="btc",
        name_ko="BTC",
        kind="crypto",
        current=current,
        previous=previous,
        absolute_change=absolute,
        percent_change=percent,
        as_of=as_of,
        market_state="OPEN",
        source=series.source,
        comparison_label="24시간 전",
        stale=_is_stale(as_of, "crypto", "OPEN"),
        unit="USD",
    )


def fetch_fmp_quote(
    key: str,
    symbol: str,
    definition: dict[str, str],
    settings: Settings,
) -> AssetQuote | None:
    if not settings.fmp_api_key:
        return None
    try:
        response = _session().get(
            FMP_QUOTE_URL,
            params={"symbol": symbol, "apikey": settings.fmp_api_key},
            timeout=settings.request_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        item = payload[0] if isinstance(payload, list) and payload else payload
        if not isinstance(item, dict) or item.get("price") is None:
            return None
        current = float(item["price"])
        previous_raw = item.get("previousClose")
        if previous_raw is None and item.get("change") is not None:
            previous_raw = current - float(item["change"])
        previous = float(previous_raw) if previous_raw not in {None, 0} else None
        absolute, percent = _changes(current, previous)
        timestamp = int(item.get("timestamp") or datetime.now(UTC).timestamp())
        as_of = datetime.fromtimestamp(timestamp, UTC)
        market_state = "CLOSED" if item.get("isMarketOpen") is False else "OPEN"
        return AssetQuote(
            key=key,
            name_ko=definition["name"],
            kind=definition["kind"],  # type: ignore[arg-type]
            current=current,
            previous=previous,
            absolute_change=absolute,
            percent_change=percent,
            as_of=as_of,
            market_state=market_state,
            source="Financial Modeling Prep",
            comparison_label="전일 종가",
            stale=_is_stale(as_of, definition["kind"], market_state),
            unit=definition["unit"],
        )
    except Exception:
        logging.warning("FMP quote failed for %s; using Yahoo fallback.", key, exc_info=True)
        return None


def fetch_market_quotes(settings: Settings) -> tuple[dict[str, AssetQuote], list[str]]:
    quotes: dict[str, AssetQuote] = {}
    errors: list[str] = []
    try:
        quotes.update(fetch_crypto_quotes(settings))
    except Exception as exc:
        errors.append(f"CoinGecko: {exc}")
        logging.exception("CoinGecko quote fetch failed; using Yahoo crypto fallback.")
        try:
            quotes.update(fetch_yahoo_crypto_quotes(settings))
        except Exception as fallback_exc:
            errors.append(f"Yahoo crypto fallback: {fallback_exc}")
            logging.exception("Yahoo crypto fallback failed.")

    def fetch_one(key: str, definition: dict[str, str]) -> AssetQuote:
        fmp_symbol = FMP_SYMBOLS.get(key)
        if fmp_symbol:
            fmp_quote = fetch_fmp_quote(key, fmp_symbol, definition, settings)
            if fmp_quote is not None:
                return fmp_quote
        return fetch_yahoo_quote(key, definition, settings)

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(fetch_one, key, definition): key
            for key, definition in YAHOO_ASSETS.items()
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                quote = future.result()
                if not quote.stale:
                    quotes[key] = quote
                else:
                    errors.append(f"{key}: stale data at {quote.as_of.isoformat()}")
            except Exception as exc:
                errors.append(f"{key}: {exc}")
                logging.exception("Market quote fetch failed for %s.", key)

    try:
        quotes.update(fetch_treasury_quotes(settings))
    except Exception as exc:
        errors.append(f"US Treasury: {exc}")
        logging.exception("US Treasury quote fetch failed.")
    return quotes, errors


def fetch_treasury_quotes(settings: Settings) -> dict[str, AssetQuote]:
    year = datetime.now(KST).year
    params = {
        "data": "daily_treasury_yield_curve",
        "field_tdr_date_value": str(year),
    }
    session = _session()
    response: requests.Response | None = None
    last_error: requests.RequestException | None = None
    for attempt in range(2):
        try:
            response = session.get(
                TREASURY_XML_URL,
                params=params,
                timeout=max(settings.request_timeout_seconds, 10),
            )
            break
        except requests.RequestException as exc:
            last_error = exc
            if attempt == 0:
                logging.warning("US Treasury request delayed; retrying once.")
    if response is None:
        raise RuntimeError("US Treasury request failed after retry") from last_error
    response.raise_for_status()
    root = ET.fromstring(response.content)
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata",
        "d": "http://schemas.microsoft.com/ado/2007/08/dataservices",
    }
    rows: list[dict[str, str]] = []
    for entry in root.findall("atom:entry", ns):
        properties = entry.find("atom:content/m:properties", ns)
        if properties is None:
            continue
        row = {node.tag.rsplit("}", 1)[-1]: (node.text or "") for node in properties}
        if row.get("NEW_DATE"):
            rows.append(row)
    rows.sort(key=lambda row: row["NEW_DATE"])
    if len(rows) < 2:
        raise RuntimeError("Treasury feed returned fewer than two observations")

    current_row, previous_row = rows[-1], rows[-2]
    as_of_date = date.fromisoformat(current_row["NEW_DATE"][:10])
    as_of = datetime.combine(as_of_date, time(17, 0), tzinfo=KST)
    definitions = {
        "us2y": ("미국채 2년물", "BC_2YEAR"),
        "us10y": ("미국채 10년물", "BC_10YEAR"),
    }
    result: dict[str, AssetQuote] = {}
    for key, (name, field) in definitions.items():
        current = float(current_row[field])
        previous = float(previous_row[field])
        absolute = current - previous
        percent = absolute / previous * 100 if previous else None
        result[key] = AssetQuote(
            key=key,
            name_ko=name,
            kind="yield",
            current=current,
            previous=previous,
            absolute_change=absolute,
            percent_change=percent,
            as_of=as_of,
            market_state="OFFICIAL_DAILY",
            source="U.S. Department of the Treasury",
            comparison_label="직전 고시",
            stale=datetime.now(KST) - as_of > timedelta(days=7),
            unit="%",
        )
    return {key: value for key, value in result.items() if not value.stale}


def critical_data_errors(quotes: dict[str, AssetQuote]) -> list[str]:
    missing: list[str] = []
    for key, label in {
        "btc": "BTC",
        "nasdaq100": "Nasdaq 100",
        "dxy": "DXY",
    }.items():
        if key not in quotes:
            missing.append(label)
    if not ({"us2y", "us10y"} & quotes.keys()):
        missing.append("미국채 금리")
    return missing


def provider_health_summary(
    quotes: dict[str, AssetQuote],
    errors: list[str],
) -> dict[str, Any]:
    return {
        "valid_quote_count": len(quotes),
        "critical_missing": critical_data_errors(quotes),
        "errors": errors,
        "sources": sorted({quote.source for quote in quotes.values()}),
    }
