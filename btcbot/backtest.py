"""Replay history: what would the bot have said at each alert time, and was it right?"""
from __future__ import annotations

import csv
import time as _time
from bisect import bisect_left
from datetime import datetime

from .config import Config
from .data import GRANULARITY, Candle, fetch_history
from .strategy import analyze

LOOKBACK = 200
BUCKETS = [(50, 55), (55, 60), (60, 65), (65, 70), (70, 75), (75, 101)]


def load_csv(path: str) -> list[Candle]:
    with open(path) as f:
        rows = csv.DictReader(f)
        return [Candle(int(float(r["start"])), float(r["open"]), float(r["high"]), float(r["low"]),
                       float(r["close"]), float(r["volume"])) for r in rows]


def evaluate(cfg: Config, candles: list[Candle]) -> list[dict]:
    starts = [c.start for c in candles]
    results = []
    for i, c in enumerate(candles):
        if i < LOOKBACK:
            continue
        local = datetime.fromtimestamp(c.start, cfg.timezone)
        t = local.time()
        if not (cfg.window_start <= t < cfg.window_end) or local.minute % cfg.interval_minutes:
            continue
        if cfg.weekdays_only and local.weekday() >= 5:
            continue
        prev = candles[i - 1]
        if prev.start + GRANULARITY != c.start:
            continue  # gap in data
        sig = analyze(candles[i - LOOKBACK:i], cfg.min_confidence)
        # Outcome measured over the configured interval (15m = one candle)
        j = bisect_left(starts, c.start + (cfg.interval_minutes * 60 - GRANULARITY))
        if j >= len(candles):
            break
        outcome = "UP" if candles[j].close > sig.price else "DOWN"
        results.append({"time": local, "lean": sig.lean, "direction": sig.direction,
                        "confidence": sig.confidence, "correct": sig.lean == outcome})
    return results


def report(results: list[dict], min_conf: float) -> str:
    if not results:
        return "No alert slots found in the data."
    out = [f"Alert slots evaluated: {len(results)}", "", "Confidence   Signals   Accuracy"]
    for lo, hi in BUCKETS:
        b = [r for r in results if lo <= r["confidence"] < hi]
        if b:
            acc = 100 * sum(r["correct"] for r in b) / len(b)
            out.append(f"{lo:>3}-{min(hi, 100):<3}%   {len(b):>7}   {acc:6.1f}%")
    calls = [r for r in results if r["direction"] != "SKIP"]
    out.append("")
    if calls:
        acc = 100 * sum(r["correct"] for r in calls) / len(calls)
        out.append(f"With MIN_CONFIDENCE={min_conf:.0f}: {len(calls)} calls, "
                   f"{len(results) - len(calls)} skips, accuracy {acc:.1f}%")
    else:
        out.append(f"With MIN_CONFIDENCE={min_conf:.0f}: every slot was a SKIP")
    out.append("(50% = coin flip. Past accuracy does not guarantee future results.)")
    return "\n".join(out)


def run_backtest(cfg: Config, days: int = 30, csv_path: str | None = None) -> None:
    if csv_path:
        candles = load_csv(csv_path)
    else:
        end = int(_time.time()) // GRANULARITY * GRANULARITY
        start = end - (days * 86400) - LOOKBACK * GRANULARITY
        print(f"Downloading {days} days of 15m candles from Coinbase...")
        candles = fetch_history(cfg.product, start, end)
    print(report(evaluate(cfg, candles), cfg.min_confidence))
