from __future__ import annotations

import csv
import io
import logging
import math
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import quote as url_quote
from zoneinfo import ZoneInfo

import requests

from .config import KST, Settings
from .http_client import request
from .models import AssetQuote, PricePoint, PriceSeries
from .state import StateStore


UTC = timezone.utc
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
TREASURY_XML_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/"
    "interest-rates/pages/xml"
)
FMP_QUOTE_URL = "https://financialmodelingprep.com/stable/quote"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
NAVER_REALTIME_URL = "https://polling.finance.naver.com/api/realtime/domestic/{asset_type}/{symbol}"

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

SESSION_ASSETS: dict[str, dict[str, str]] = {
    "samsung": {"symbol": "005930.KS", "name": "삼성전자", "kind": "equity", "unit": "원"},
    "skhynix": {"symbol": "000660.KS", "name": "SK하이닉스", "kind": "equity", "unit": "원"},
    "nasdaq_futures": {"symbol": "NQ=F", "name": "Nasdaq 선물", "kind": "index", "unit": "pt"},
    "eurostoxx50": {"symbol": "^STOXX50E", "name": "Euro Stoxx 50", "kind": "index", "unit": "pt"},
    "dax": {"symbol": "^GDAXI", "name": "DAX", "kind": "index", "unit": "pt"},
    "eurusd": {"symbol": "EURUSD=X", "name": "EUR/USD", "kind": "fx", "unit": ""},
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

PROXY_ASSETS: dict[str, dict[str, str]] = {
    "sp500": {"symbol": "SPY", "name": "S&P 500 대용 ETF", "kind": "index", "unit": "USD"},
    "nasdaq100": {"symbol": "QQQ", "name": "Nasdaq 100 대용 ETF", "kind": "index", "unit": "USD"},
    "dow": {"symbol": "DIA", "name": "Dow 대용 ETF", "kind": "index", "unit": "USD"},
    "dxy": {"symbol": "UUP", "name": "달러 대용 ETF", "kind": "fx", "unit": "USD"},
    "kospi": {"symbol": "EWY", "name": "한국 주식 대용 ETF", "kind": "index", "unit": "USD"},
    "wti": {"symbol": "USO", "name": "WTI 대용 ETF", "kind": "commodity", "unit": "USD"},
    "gold": {"symbol": "GLD", "name": "금 대용 ETF", "kind": "commodity", "unit": "USD"},
    "nasdaq_futures": {"symbol": "QQQ", "name": "Nasdaq 대용 ETF", "kind": "index", "unit": "USD"},
    "eurostoxx50": {"symbol": "FEZ", "name": "유로존 주식 대용 ETF", "kind": "index", "unit": "USD"},
    "dax": {"symbol": "EWG", "name": "독일 주식 대용 ETF", "kind": "index", "unit": "USD"},
    "eurusd": {"symbol": "FXE", "name": "유로 대용 ETF", "kind": "fx", "unit": "USD"},
    "usdkrw": {"symbol": "UUP", "name": "달러 대용 ETF", "kind": "fx", "unit": "USD"},
}

OUTLIER_THRESHOLDS = {
    "btc": 8.0,
    "eth": 10.0,
    "sp500": 3.0,
    "nasdaq100": 3.0,
    "dow": 3.0,
    "kospi": 3.0,
    "kosdaq": 3.0,
    "dxy": 0.7,
    "usdkrw": 0.7,
    "wti": 5.0,
    "gold": 5.0,
    "nasdaq_futures": 3.0,
    "eurostoxx50": 3.0,
    "dax": 3.0,
    "eurusd": 0.7,
    "samsung": 15.0,
    "skhynix": 15.0,
}

KOREA_ASSETS = {
    "kospi": ("index", "KOSPI"),
    "kosdaq": ("index", "KOSDAQ"),
    "samsung": ("stock", "005930"),
    "skhynix": ("stock", "000660"),
}

CALCULATION_VERSION = 2


class DataValidationError(RuntimeError):
    pass


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
    if market_state.upper() in {"CLOSE", "CLOSED", "PRE", "POST"}:
        return age > timedelta(days=4)
    return age > timedelta(hours=36)


def fetch_crypto_quotes(settings: Settings) -> dict[str, AssetQuote]:
    response = request(
        "GET",
        COINGECKO_URL,
        settings,
        provider="coingecko",
        params={
            "ids": "bitcoin,ethereum",
            "vs_currencies": "usd",
            "include_24hr_change": "true",
            "include_last_updated_at": "true",
        },
        session=_session(),
    )
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
            reference_at=updated_at - timedelta(hours=24),
            symbol="BTC-USD" if key == "btc" else "ETH-USD",
            currency="USD",
            price_basis="24h",
            validation_status="verified",
            validation_sources=["CoinGecko"],
            calculation_version=CALCULATION_VERSION,
        )
    return result


def _finite_positive(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise DataValidationError(f"invalid {field}") from exc
    if not math.isfinite(number) or number <= 0:
        raise DataValidationError(f"invalid {field}")
    return number


def _exchange_timezone(meta: dict[str, Any]) -> ZoneInfo:
    try:
        return ZoneInfo(str(meta.get("exchangeTimezoneName") or "UTC"))
    except Exception:
        return ZoneInfo("UTC")


def _completed_daily_bars(
    result: dict[str, Any],
    timezone_info: ZoneInfo,
) -> list[tuple[datetime, float]]:
    timestamps = result.get("timestamp") or []
    closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
    by_date: dict[date, tuple[datetime, float]] = {}
    for raw_timestamp, raw_close in zip(timestamps, closes):
        if raw_close is None:
            continue
        timestamp = datetime.fromtimestamp(int(raw_timestamp), UTC)
        close = _finite_positive(raw_close, field="daily close")
        by_date[timestamp.astimezone(timezone_info).date()] = (timestamp, close)
    return sorted(by_date.values(), key=lambda item: item[0])


def _split_factor(
    result: dict[str, Any],
    *,
    after: datetime,
    through: datetime,
) -> float:
    factor = 1.0
    splits = (result.get("events") or {}).get("splits") or {}
    for item in splits.values():
        timestamp = datetime.fromtimestamp(int(item.get("date") or 0), UTC)
        if not (after < timestamp <= through + timedelta(days=1)):
            continue
        numerator = float(item.get("numerator") or 0)
        denominator = float(item.get("denominator") or 0)
        if numerator > 0 and denominator > 0:
            factor *= numerator / denominator
    return factor


def _yahoo_reference_close(
    result: dict[str, Any],
    *,
    current: float,
    as_of: datetime,
    timezone_info: ZoneInfo,
) -> tuple[float, datetime, list[str]]:
    bars = _completed_daily_bars(result, timezone_info)
    if len(bars) < 2:
        raise DataValidationError("Yahoo returned fewer than two daily closes")

    current_date = as_of.astimezone(timezone_info).date()
    current_index = next(
        (index for index in range(len(bars) - 1, -1, -1)
         if bars[index][0].astimezone(timezone_info).date() == current_date),
        len(bars) - 1,
    )
    if current_index == 0:
        raise DataValidationError("Yahoo daily history has no reference session")
    current_bar_time, current_bar = bars[current_index]
    reference_at, previous = bars[current_index - 1]
    if abs(current - current_bar) / current > 0.01:
        raise DataValidationError("Yahoo market price and daily close disagree")

    flags: list[str] = []
    split_factor = _split_factor(result, after=reference_at, through=current_bar_time)
    if split_factor != 1.0:
        adjusted_previous = previous / split_factor
        raw_gap = abs(current_bar / previous - 1)
        adjusted_gap = abs(current_bar / adjusted_previous - 1)
        if adjusted_gap < raw_gap and adjusted_gap <= 0.4:
            previous = adjusted_previous
            flags.append(f"주식 분할 반영 {split_factor:g}:1")
        else:
            flags.append(f"주식 분할 확인 · 일봉 이미 보정 {split_factor:g}:1")
    return previous, reference_at, flags


def fetch_yahoo_quote(
    key: str,
    definition: dict[str, str],
    settings: Settings,
) -> AssetQuote:
    symbol_raw = definition["symbol"]
    symbol = url_quote(symbol_raw, safe="")
    response = request(
        "GET",
        YAHOO_CHART_URL.format(symbol=symbol),
        settings,
        provider="yahoo",
        params={
            "range": "10d",
            "interval": "1d",
            "includePrePost": "true",
            "events": "div,splits",
        },
        session=_session(),
    )
    result = response.json()["chart"]["result"][0]
    meta = result["meta"]
    current_raw = meta.get("regularMarketPrice")
    if current_raw is None:
        closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        current_raw = next((value for value in reversed(closes) if value is not None), None)
    current = _finite_positive(current_raw, field="current price")
    timestamp = int(meta.get("regularMarketTime") or datetime.now(UTC).timestamp())
    as_of = datetime.fromtimestamp(timestamp, UTC)
    timezone_info = _exchange_timezone(meta)
    market_state = str(meta.get("marketState") or "CLOSED").upper()
    precheck_flags: list[str] = []
    daily_bars = _completed_daily_bars(result, timezone_info)
    if daily_bars:
        latest_bar_time, latest_bar_close = daily_bars[-1]
        same_session = (
            latest_bar_time.astimezone(timezone_info).date()
            == as_of.astimezone(timezone_info).date()
        )
        market_gap = abs(current - latest_bar_close) / current
        if market_state != "REGULAR" and same_session and market_gap > 0.01:
            current = latest_bar_close
            precheck_flags.append("Yahoo 완료 일봉 종가 사용")
    previous, reference_at, flags = _yahoo_reference_close(
        result,
        current=current,
        as_of=as_of,
        timezone_info=timezone_info,
    )
    flags = precheck_flags + flags
    absolute, percent = _changes(current, previous)
    if definition["kind"] == "equity" and percent is not None and abs(percent) > 40:
        raise DataValidationError("unconfirmed equity price discontinuity")
    price_basis = "regular_market" if market_state == "REGULAR" else "regular_close"
    if definition["kind"] == "commodity":
        flags.append(f"연속 선물 {symbol_raw}")
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
        quality_flags=flags,
        reference_at=reference_at,
        symbol=symbol_raw,
        currency=str(meta.get("currency") or definition["unit"]),
        price_basis=price_basis,
        validation_status="verified",
        validation_sources=["Yahoo Finance 일봉"],
        calculation_version=CALCULATION_VERSION,
    )


def fetch_naver_korea_quote(
    key: str,
    definition: dict[str, str],
    settings: Settings,
) -> AssetQuote:
    asset_type, symbol = KOREA_ASSETS[key]
    response = request(
        "GET",
        NAVER_REALTIME_URL.format(asset_type=asset_type, symbol=symbol),
        settings,
        provider="naver_finance",
        session=_session(),
    )
    payload = response.json()
    items = payload.get("datas") or []
    if not items:
        raise DataValidationError(f"Naver returned no data for {symbol}")
    item = items[0]
    current = _finite_positive(item.get("closePriceRaw"), field="Naver current price")
    change = float(item.get("compareToPreviousClosePriceRaw") or 0)
    previous = current - change
    previous = _finite_positive(previous, field="Naver previous close")
    absolute, percent = _changes(current, previous)
    reported_percent = float(item.get("fluctuationsRatioRaw") or 0)
    if percent is None or abs(percent - reported_percent) > 0.05:
        raise DataValidationError("Naver change fields are internally inconsistent")
    raw_as_of = str(item.get("localTradedAt") or "")
    try:
        as_of = datetime.fromisoformat(raw_as_of).astimezone(KST)
    except ValueError as exc:
        raise DataValidationError("Naver returned invalid market timestamp") from exc
    market_state = str(item.get("marketStatus") or "UNKNOWN").upper()
    if market_state == "CLOSE":
        as_of = datetime.combine(as_of.date(), time(15, 30), tzinfo=KST)
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
        source="Naver Finance",
        comparison_label="전일 종가",
        stale=_is_stale(as_of, definition["kind"], market_state),
        unit=definition["unit"],
        symbol=symbol,
        currency="KRW",
        price_basis="regular_close" if market_state == "CLOSE" else "regular_market",
        validation_status="verified",
        validation_sources=["Naver Finance"],
        calculation_version=CALCULATION_VERSION,
    )


def fetch_verified_korea_quote(
    key: str,
    definition: dict[str, str],
    settings: Settings,
) -> AssetQuote:
    naver = fetch_naver_korea_quote(key, definition, settings)
    yahoo = fetch_yahoo_quote(key, definition, settings)
    if naver.as_of.astimezone(KST).date() != yahoo.as_of.astimezone(KST).date():
        raise DataValidationError("Naver and Yahoo session dates disagree")
    price_gap = abs(naver.current - yahoo.current) / naver.current * 100
    naver_percent = float(naver.percent_change or 0)
    yahoo_percent = float(yahoo.percent_change or 0)
    percent_gap = abs(naver_percent - yahoo_percent)
    if price_gap > 0.3 or percent_gap > 0.15:
        raise DataValidationError(
            f"Naver/Yahoo mismatch: price={price_gap:.3f}% change={percent_gap:.3f}pp"
        )
    return naver.model_copy(
        update={
            "source": "Naver Finance · Yahoo 검증",
            "reference_at": yahoo.reference_at,
            "validation_sources": ["Naver Finance", "Yahoo Finance 일봉"],
            "quality_flags": ["Naver/Yahoo 일치"],
        }
    )


def fetch_yahoo_crypto_quotes(settings: Settings) -> dict[str, AssetQuote]:
    result: dict[str, AssetQuote] = {}
    for key, definition in YAHOO_CRYPTO_ASSETS.items():
        quote = fetch_yahoo_quote(key, definition, settings)
        quote.comparison_label = "직전 UTC 종가"
        if not quote.stale:
            result[key] = quote
    return result


def fetch_yahoo_intraday_series(
    key: str,
    definition: dict[str, str],
    settings: Settings,
    *,
    interval: str,
    hours: int = 24,
) -> PriceSeries:
    symbol = url_quote(definition["symbol"], safe="")
    response = request(
        "GET",
        YAHOO_CHART_URL.format(symbol=symbol),
        settings,
        provider="yahoo",
        params={"range": "5d", "interval": interval, "includePrePost": "true"},
        session=_session(),
    )
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
        cutoff = points[-1].timestamp - timedelta(hours=hours)
        points = [point for point in points if point.timestamp >= cutoff]
    return PriceSeries(
        key=key,
        name=definition["name"],
        points=points,
        source="Yahoo Finance",
    )


def fetch_intraday_series(
    key: str,
    settings: Settings,
    *,
    hours: int = 24,
    minimum_points: int = 2,
) -> PriceSeries:
    definition = (
        YAHOO_CRYPTO_ASSETS.get(key)
        or YAHOO_ASSETS.get(key)
        or SESSION_ASSETS.get(key)
    )
    if not definition:
        raise KeyError(f"Unsupported intraday asset key: {key}")
    errors: list[str] = []
    for interval in ("5m", "15m"):
        try:
            series = fetch_yahoo_intraday_series(
                key,
                definition,
                settings,
                interval=interval,
                hours=hours,
            )
            if len(series.points) >= minimum_points:
                return series
            errors.append(f"{interval}: {len(series.points)} valid points")
        except Exception as exc:
            errors.append(f"{interval}: {type(exc).__name__}")
    raise RuntimeError(
        f"{definition['name']} intraday series unavailable: " + "; ".join(errors)
    )


def _fetch_yahoo_intraday(
    settings: Settings,
    *,
    interval: str,
) -> PriceSeries:
    return fetch_yahoo_intraday_series(
        "btc",
        YAHOO_CRYPTO_ASSETS["btc"],
        settings,
        interval=interval,
        hours=24,
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
            errors.append(f"{interval}: {type(exc).__name__}")
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
        reference_at=series.points[0].timestamp,
        symbol="BTC-USD",
        currency="USD",
        price_basis="24h",
        validation_sources=[series.source],
        calculation_version=CALCULATION_VERSION,
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
        response = request(
            "GET",
            FMP_QUOTE_URL,
            settings,
            provider="fmp",
            params={"symbol": symbol, "apikey": settings.fmp_api_key},
            session=_session(),
        )
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
            symbol=symbol,
            currency=definition["unit"],
            price_basis="regular_close" if market_state == "CLOSED" else "regular_market",
            validation_sources=["Financial Modeling Prep"],
            calculation_version=CALCULATION_VERSION,
        )
    except Exception:
        logging.warning("FMP quote failed for %s; using Yahoo fallback.", key, exc_info=True)
        return None


def _quote_ttl_seconds(quote: AssetQuote) -> int:
    if quote.kind == "crypto":
        return 15 * 60
    if quote.kind == "yield":
        return 24 * 60 * 60
    return 30 * 60


def _cache_quote(store: StateStore | None, quote: AssetQuote) -> None:
    if (
        store is None
        or quote.stale
        or not quote.verified
        or quote.validation_status != "verified"
        or quote.calculation_version != CALCULATION_VERSION
    ):
        return
    store.cache_set(
        f"quote:v{CALCULATION_VERSION}:{quote.key}",
        quote.model_dump(mode="json"),
        source=quote.source,
        ttl_seconds=_quote_ttl_seconds(quote),
    )


def _cached_quote(
    store: StateStore | None,
    key: str,
    *,
    max_stale_seconds: int | None = None,
) -> AssetQuote | None:
    if store is None:
        return None
    cached = store.cache_get(
        f"quote:v{CALCULATION_VERSION}:{key}",
        max_stale_seconds=7 * 24 * 60 * 60,
    )
    if not cached or not isinstance(cached.get("payload"), dict):
        return None
    try:
        quote = AssetQuote.model_validate(cached["payload"])
    except Exception:
        return None
    if quote.calculation_version != CALCULATION_VERSION:
        return None
    if max_stale_seconds is None:
        if quote.kind == "crypto" or quote.market_state.upper() in {"OPEN", "REGULAR"}:
            max_stale_seconds = 60 * 60
        elif quote.kind == "yield":
            max_stale_seconds = 7 * 24 * 60 * 60
        else:
            max_stale_seconds = 4 * 24 * 60 * 60
    if datetime.now(UTC) - quote.as_of.astimezone(UTC) > timedelta(
        seconds=max_stale_seconds
    ):
        return None
    flags = list(quote.quality_flags)
    if "cached" not in flags:
        flags.append("cached")
    return quote.model_copy(
        update={
            "source": f"{quote.source} · 마지막 검증값",
            "stale": False,
            "quality_flags": flags,
            "validation_status": "last_verified",
        }
    )


def _record_provider(
    store: StateStore | None,
    provider: str,
    *,
    success: bool,
    error: Exception | None = None,
) -> None:
    if store is None:
        return
    store.record_provider_result(
        provider,
        success=success,
        error=type(error).__name__ if error else "",
    )


def fetch_asset_quote(
    key: str,
    settings: Settings,
    store: StateStore | None = None,
) -> AssetQuote:
    try:
        if key in YAHOO_CRYPTO_ASSETS:
            quote = fetch_yahoo_quote(key, YAHOO_CRYPTO_ASSETS[key], settings)
            quote.comparison_label = "직전 UTC 종가"
        elif key in YAHOO_ASSETS or key in SESSION_ASSETS:
            definition = YAHOO_ASSETS.get(key) or SESSION_ASSETS[key]
            if key in KOREA_ASSETS:
                quote = fetch_verified_korea_quote(key, definition, settings)
            else:
                fmp_symbol = FMP_SYMBOLS.get(key)
                quote = (
                    fetch_fmp_quote(key, fmp_symbol, definition, settings)
                    if fmp_symbol
                    else None
                ) or fetch_yahoo_quote(key, definition, settings)
        elif key in {"us2y", "us10y"}:
            try:
                quote = fetch_treasury_quotes(settings)[key]
            except Exception:
                quote = fetch_fred_treasury_quotes(settings)[key]
        else:
            raise KeyError(f"Unsupported asset key: {key}")
        _cache_quote(store, quote)
        _record_provider(store, quote.source, success=True)
        return quote
    except Exception as exc:
        cached = _cached_quote(store, key)
        if cached is not None:
            _record_provider(store, "market_data", success=False, error=exc)
            return cached
        raise


def fetch_market_quotes(
    settings: Settings,
    store: StateStore | None = None,
) -> tuple[dict[str, AssetQuote], list[str]]:
    quotes: dict[str, AssetQuote] = {}
    errors: list[str] = []
    try:
        crypto_quotes = fetch_crypto_quotes(settings)
        quotes.update(crypto_quotes)
        for quote in crypto_quotes.values():
            _cache_quote(store, quote)
        _record_provider(store, "CoinGecko", success=True)
    except Exception as exc:
        errors.append(f"CoinGecko: {exc}")
        _record_provider(store, "CoinGecko", success=False, error=exc)
        logging.warning("CoinGecko quote fetch failed; using Yahoo crypto fallback: %s", exc)
        try:
            crypto_quotes = fetch_yahoo_crypto_quotes(settings)
            quotes.update(crypto_quotes)
            for quote in crypto_quotes.values():
                _cache_quote(store, quote)
            _record_provider(store, "Yahoo Finance", success=True)
        except Exception as fallback_exc:
            errors.append(f"Yahoo crypto fallback: {fallback_exc}")
            _record_provider(
                store,
                "Yahoo Finance",
                success=False,
                error=fallback_exc,
            )
            logging.exception("Yahoo crypto fallback failed.")
            for key in YAHOO_CRYPTO_ASSETS:
                cached = _cached_quote(store, key)
                if cached is not None:
                    quotes[key] = cached

    def fetch_one(key: str, definition: dict[str, str]) -> AssetQuote:
        if key in KOREA_ASSETS:
            return fetch_verified_korea_quote(key, definition, settings)
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
                    _cache_quote(store, quote)
                else:
                    errors.append(f"{key}: stale data at {quote.as_of.isoformat()}")
            except Exception as exc:
                errors.append(f"{key}: {exc}")
                logging.exception("Market quote fetch failed for %s.", key)
                cached = _cached_quote(store, key)
                if cached is not None:
                    quotes[key] = cached

    try:
        treasury = fetch_treasury_quotes(settings)
        quotes.update(treasury)
        for quote in treasury.values():
            _cache_quote(store, quote)
        _record_provider(store, "U.S. Department of the Treasury", success=True)
    except Exception as exc:
        errors.append(f"US Treasury: {exc}")
        _record_provider(
            store,
            "U.S. Department of the Treasury",
            success=False,
            error=exc,
        )
        logging.warning("US Treasury quote fetch failed; trying FRED fallback.")
        try:
            treasury = fetch_fred_treasury_quotes(settings)
            quotes.update(treasury)
            for quote in treasury.values():
                _cache_quote(store, quote)
            _record_provider(store, "FRED", success=True)
        except Exception as fallback_exc:
            errors.append(f"FRED Treasury fallback: {fallback_exc}")
            _record_provider(store, "FRED", success=False, error=fallback_exc)
            for key in ("us2y", "us10y"):
                cached = _cached_quote(store, key, max_stale_seconds=7 * 24 * 3600)
                if cached is not None:
                    quotes[key] = cached

    _verify_outlier_directions(quotes, settings, errors)
    record_data_quality(store, quotes, errors)
    return quotes, errors


def fetch_treasury_quotes(settings: Settings) -> dict[str, AssetQuote]:
    year = datetime.now(KST).year
    params = {
        "data": "daily_treasury_yield_curve",
        "field_tdr_date_value": str(year),
    }
    response = request(
        "GET",
        TREASURY_XML_URL,
        settings,
        provider="us_treasury",
        attempts=2,
        timeout=max(settings.request_timeout_seconds, 10),
        params=params,
        session=_session(),
    )
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
            reference_at=datetime.combine(
                date.fromisoformat(previous_row["NEW_DATE"][:10]),
                time(17, 0),
                tzinfo=KST,
            ),
            symbol=field,
            currency="%",
            price_basis="official_daily",
            validation_sources=["U.S. Department of the Treasury"],
            calculation_version=CALCULATION_VERSION,
        )
    return {key: value for key, value in result.items() if not value.stale}


def fetch_fred_treasury_quotes(settings: Settings) -> dict[str, AssetQuote]:
    response = request(
        "GET",
        FRED_CSV_URL,
        settings,
        provider="fred",
        params={"id": "DGS2,DGS10"},
        attempts=3,
        session=_session(),
    )
    text = getattr(response, "text", "")
    if not text:
        text = response.content.decode("utf-8-sig")
    rows: list[dict[str, str]] = []
    for row in csv.DictReader(io.StringIO(text)):
        date_value = row.get("DATE") or row.get("observation_date") or ""
        dgs2 = (row.get("DGS2") or "").strip()
        dgs10 = (row.get("DGS10") or "").strip()
        if date_value and dgs2 not in {"", "."} and dgs10 not in {"", "."}:
            rows.append({"date": date_value, "DGS2": dgs2, "DGS10": dgs10})
    if len(rows) < 2:
        raise RuntimeError("FRED returned fewer than two complete observations")
    current_row, previous_row = rows[-1], rows[-2]
    as_of_date = date.fromisoformat(current_row["date"][:10])
    as_of = datetime.combine(as_of_date, time(17, 0), tzinfo=KST)
    definitions = {
        "us2y": ("미국채 2년물", "DGS2"),
        "us10y": ("미국채 10년물", "DGS10"),
    }
    result: dict[str, AssetQuote] = {}
    for key, (name, field) in definitions.items():
        current = float(current_row[field])
        previous = float(previous_row[field])
        absolute, percent = _changes(current, previous)
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
            source="FRED",
            comparison_label="직전 고시",
            stale=datetime.now(KST) - as_of > timedelta(days=7),
            unit="%",
            reference_at=datetime.combine(
                date.fromisoformat(previous_row["date"][:10]),
                time(17, 0),
                tzinfo=KST,
            ),
            symbol=field,
            currency="%",
            price_basis="official_daily",
            validation_sources=["FRED"],
            calculation_version=CALCULATION_VERSION,
        )
    return {key: quote for key, quote in result.items() if not quote.stale}


def _same_direction(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return False
    if abs(left) < 0.01 or abs(right) < 0.01:
        return True
    return (left > 0) == (right > 0)


def _verify_outlier_directions(
    quotes: dict[str, AssetQuote],
    settings: Settings,
    errors: list[str],
) -> None:
    """Prevent anomalous provider values from becoming asserted causes."""
    for key, threshold in OUTLIER_THRESHOLDS.items():
        quote = quotes.get(key)
        if quote is None or quote.percent_change is None:
            continue
        if abs(quote.percent_change) < threshold:
            continue
        if len(quote.validation_sources) >= 2:
            quote.quality_flags.append("독립 공급원 수치 교차검증")
            continue
        try:
            if key in YAHOO_CRYPTO_ASSETS and quote.source != "Yahoo Finance":
                verifier = fetch_yahoo_quote(key, YAHOO_CRYPTO_ASSETS[key], settings)
            elif key in PROXY_ASSETS:
                verifier = fetch_yahoo_quote(
                    f"{key}_proxy",
                    PROXY_ASSETS[key],
                    settings,
                )
            else:
                quote.verified = False
                quote.quality_flags.append("급변 교차검증 공급원 없음")
                continue
            if _same_direction(quote.percent_change, verifier.percent_change):
                quote.quality_flags.append(f"방향 교차검증: {verifier.name_ko}")
            else:
                quote.verified = False
                quote.quality_flags.append(f"급변 방향 불일치: {verifier.name_ko}")
                errors.append(f"{key}: outlier direction mismatch")
        except Exception as exc:
            quote.verified = False
            quote.quality_flags.append("급변 교차검증 실패")
            errors.append(f"{key}: outlier verification failed ({type(exc).__name__})")

    yield_quotes = [
        quote
        for key in ("us2y", "us10y")
        if (quote := quotes.get(key))
        and quote.absolute_change is not None
        and abs(quote.absolute_change * 100) >= 10
    ]
    if not yield_quotes:
        return
    try:
        verifier_quotes = fetch_fred_treasury_quotes(settings)
    except Exception as exc:
        for quote in yield_quotes:
            quote.verified = False
            quote.quality_flags.append("금리 급변 교차검증 실패")
        errors.append(f"yield outlier verification failed ({type(exc).__name__})")
        return
    for quote in yield_quotes:
        verifier = verifier_quotes.get(quote.key)
        if verifier and _same_direction(
            quote.absolute_change,
            verifier.absolute_change,
        ):
            quote.quality_flags.append("FRED 방향 교차검증")
        else:
            quote.verified = False
            quote.quality_flags.append("FRED 금리 방향 불일치")


def verify_outlier_directions(
    quotes: dict[str, AssetQuote],
    settings: Settings,
) -> list[str]:
    errors: list[str] = []
    _verify_outlier_directions(quotes, settings, errors)
    return errors


def critical_data_errors(quotes: dict[str, AssetQuote]) -> list[str]:
    missing: list[str] = []
    for key, label in {
        "btc": "BTC",
        "nasdaq100": "Nasdaq 100",
        "dxy": "DXY",
    }.items():
        quote = quotes.get(key)
        if (
            quote is None
            or quote.stale
            or not quote.verified
            or quote.validation_status == "rejected"
            or quote.calculation_version < 2
        ):
            missing.append(label)
    if not any(
        key in quotes
        and not quotes[key].stale
        and quotes[key].verified
        and quotes[key].validation_status != "rejected"
        and quotes[key].calculation_version >= 2
        for key in ("us2y", "us10y")
    ):
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


def record_data_quality(
    store: StateStore | None,
    quotes: dict[str, AssetQuote],
    errors: list[str],
) -> dict[str, Any]:
    existing_assets: dict[str, Any] = {}
    if store is not None:
        previous = store.runtime_state("data_quality")
        if isinstance(previous.get("assets"), dict):
            existing_assets = dict(previous["assets"])
    current_assets = {
        key: {
            "name": quote.name_ko,
            "current": quote.current,
            "previous": quote.previous,
            "percent_change": quote.percent_change,
            "as_of": quote.as_of.isoformat(),
            "reference_at": quote.reference_at.isoformat() if quote.reference_at else None,
            "source": quote.source,
            "status": quote.validation_status,
            "sources": quote.validation_sources,
            "price_basis": quote.price_basis,
        }
        for key, quote in quotes.items()
    }
    existing_assets.update(current_assets)
    known_keys = set(YAHOO_ASSETS) | set(YAHOO_CRYPTO_ASSETS) | set(SESSION_ASSETS) | {
        "btc", "eth", "us2y", "us10y"
    }
    for error in errors:
        key = str(error).split(":", 1)[0]
        if key not in known_keys or key in current_assets:
            continue
        prior = dict(existing_assets.get(key) or {})
        prior.update({"name": prior.get("name") or key, "status": "rejected", "error": error})
        existing_assets[key] = prior
    payload = {
        "checked_at": datetime.now(UTC).isoformat(),
        "calculation_version": CALCULATION_VERSION,
        "assets": existing_assets,
        "errors": errors[:20],
    }
    if store is not None:
        store.set_runtime_state("data_quality", payload)
    return payload


def audit_market_data(
    settings: Settings,
    store: StateStore,
) -> dict[str, Any]:
    quotes, errors = fetch_market_quotes(settings, store)
    for key in SESSION_ASSETS:
        try:
            quotes[key] = fetch_asset_quote(key, settings, store)
        except Exception as exc:
            errors.append(f"{key}: {type(exc).__name__}")
    return record_data_quality(store, quotes, errors)
