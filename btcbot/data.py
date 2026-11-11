"""Market data: 15-minute BTC candles from public exchange APIs (no API key needed)."""
from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

GRANULARITY = 900  # 15 minutes in seconds
HEADERS = {"User-Agent": "btc-15m-alert-bot/1.0"}


@dataclass(frozen=True)
class Candle:
    start: int  # unix seconds, candle open time
    open: float
    high: float
    low: float
    close: float
    volume: float


def fetch_coinbase(product: str = "BTC-USD", start: int | None = None, end: int | None = None) -> list[Candle]:
    """Up to 300 candles, oldest first. Includes the in-progress candle when end is None."""
    params: dict = {"granularity": GRANULARITY}
    if start is not None and end is not None:
        params["start"] = _iso(start)
        params["end"] = _iso(end)
    r = requests.get(
        f"https://api.exchange.coinbase.com/products/{product}/candles",
        params=params, headers=HEADERS, timeout=15,
    )
    r.raise_for_status()
    # Coinbase rows: [time, low, high, open, close, volume], newest first
    rows = sorted(r.json(), key=lambda x: x[0])
    return [Candle(int(t), float(o), float(h), float(lo), float(c), float(v)) for t, lo, h, o, c, v in rows]


def fetch_kraken(product: str = "BTC-USD") -> list[Candle]:
    pair = {"BTC-USD": "XBTUSD", "BTC-EUR": "XBTEUR"}.get(product, product.replace("-", ""))
    r = requests.get(
        "https://api.kraken.com/0/public/OHLC",
        params={"pair": pair, "interval": 15}, headers=HEADERS, timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("error"):
        raise RuntimeError(f"Kraken error: {body['error']}")
    key = next(k for k in body["result"] if k != "last")
    # Kraken rows: [time, open, high, low, close, vwap, volume, count]
    return [
        Candle(int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[6]))
        for row in body["result"][key]
    ]


def fetch_candles(product: str = "BTC-USD") -> list[Candle]:
    """Latest candles from Coinbase, falling back to Kraken."""
    errors = []
    for name, fn in (("coinbase", fetch_coinbase), ("kraken", fetch_kraken)):
        try:
            candles = fn(product)
            if candles:
                return candles
        except Exception as exc:  # network errors, bad JSON, etc.
            errors.append(f"{name}: {exc}")
            log.warning("Data source %s failed: %s", name, exc)
    raise RuntimeError("All data sources failed: " + "; ".join(errors))


def closed_candles(candles: list[Candle], now: float | None = None) -> list[Candle]:
    """Drop the candle that is still forming."""
    now = _time.time() if now is None else now
    return [c for c in candles if c.start + GRANULARITY <= now]


def fetch_history(product: str, start: int, end: int) -> list[Candle]:
    """Page through Coinbase history (300 candles per request) for backtesting."""
    out: dict[int, Candle] = {}
    step = 300 * GRANULARITY
    t = start
    while t < end:
        chunk_end = min(t + step, end)
        for attempt in range(4):
            try:
                for c in fetch_coinbase(product, t, chunk_end):
                    out[c.start] = c
                break
            except requests.RequestException as exc:
                log.warning("history fetch failed (%s), retrying", exc)
                _time.sleep(2 ** attempt)
        _time.sleep(0.35)  # stay under public rate limits
        t = chunk_end
    return [out[k] for k in sorted(out)]


def fetch_recent_history(product: str, days: int) -> list[Candle]:
    """Last `days` of closed 15m candles: Coinbase (paged), else Kraken (max ~7.5 days)."""
    end = int(_time.time()) // GRANULARITY * GRANULARITY
    try:
        candles = fetch_history(product, end - days * 86400, end)
        if len(candles) > 500:
            return closed_candles(candles)
    except Exception as exc:
        log.warning("Coinbase history failed: %s", exc)
    return closed_candles(fetch_kraken(product))


def _iso(ts: int) -> str:
    return _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(ts))
