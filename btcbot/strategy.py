"""Signal engine: turns recent 15m candles into UP / DOWN / SKIP with a confidence %.

Every indicator is converted to a vote between -1 (bearish) and +1 (bullish).
The votes are combined with weights into a single score, and the score is
mapped to a confidence between 50% (coin flip) and MAX_CONFIDENCE.
If the confidence is below the configured threshold the bot says SKIP.

This is a technical-analysis heuristic, not a crystal ball. Use `python -m
btcbot.backtest` to see how the confidence numbers hold up on real history.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .data import Candle

MIN_CANDLES = 60
MAX_CONFIDENCE = 85.0
GAIN = 1.8

WEIGHTS = {
    "trend_t": 0.25,        # is the last 6h drift statistically real, or just noise?
    "trend_ema": 0.17,      # EMA 9 vs EMA 21 on 15m
    "macd": 0.13,           # MACD histogram
    "rsi": 0.10,            # RSI(14) momentum, fades extremes
    "momentum": 0.12,       # last 3 candles' move
    "higher_tf": 0.15,      # price vs EMA 50 (~12.5h trend)
    "candle": 0.08,         # where the last candle closed inside its range
}
TREND_WINDOW = 24           # candles (6 hours)


@dataclass
class Signal:
    direction: str          # "UP", "DOWN" or "SKIP"
    lean: str               # "UP" or "DOWN": which way the model leans even when skipping
    confidence: float       # 50..MAX_CONFIDENCE
    price: float
    score: float
    votes: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    candle_start: int = 0   # start time of the candle being predicted


def ema(values: list[float], period: int) -> list[float]:
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes: list[float], period: int = 14) -> float:
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    return 100 - 100 / (1 + avg_g / avg_l)


def atr(candles: list[Candle], period: int = 14) -> float:
    trs = []
    for prev, c in zip(candles, candles[1:]):
        trs.append(max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close)))
    recent = trs[-period:]
    return sum(recent) / len(recent) if recent else 0.0


def _squash(x: float) -> float:
    return math.tanh(x)


def analyze(candles: list[Candle], min_confidence: float = 60.0) -> Signal:
    """`candles` must be CLOSED candles, oldest first. Predicts the next candle."""
    if len(candles) < MIN_CANDLES:
        raise ValueError(f"need at least {MIN_CANDLES} candles, got {len(candles)}")

    closes = [c.close for c in candles]
    last = candles[-1]
    price = last.close
    a = atr(candles) or price * 0.001

    votes: dict[str, float] = {}
    reasons: list[str] = []

    # 0. Trend significance: t-statistic of the mean 15m return over the last 6h.
    #    On a random walk |t| is usually < 2; a genuine trend pushes it well above.
    rets = [closes[i] - closes[i - 1] for i in range(len(closes) - TREND_WINDOW, len(closes))]
    mean_r = sum(rets) / len(rets)
    sd_r = math.sqrt(sum((x - mean_r) ** 2 for x in rets) / (len(rets) - 1)) or 1e-9
    t_stat = mean_r / (sd_r / math.sqrt(len(rets)))
    votes["trend_t"] = _squash(t_stat / 3)
    reasons.append(f"6h trend t={t_stat:+.1f}")

    # 1. Short-term trend
    e9, e21 = ema(closes, 9)[-1], ema(closes, 21)[-1]
    votes["trend_ema"] = _squash((e9 - e21) / a)
    reasons.append(f"EMA9 {'above' if e9 > e21 else 'below'} EMA21")

    # 2. MACD histogram (12, 26, 9) and whether it is growing
    macd_line = [f - s for f, s in zip(ema(closes, 12), ema(closes, 26))]
    sig = ema(macd_line, 9)
    hist = macd_line[-1] - sig[-1]
    hist_prev = macd_line[-2] - sig[-2]
    votes["macd"] = _squash(hist / (0.5 * a) + (hist - hist_prev) / a)
    reasons.append(f"MACD hist {'+' if hist >= 0 else '-'}{'rising' if hist > hist_prev else 'falling'}")

    # 3. RSI: momentum in the middle, fade when overbought / oversold
    r = rsi(closes)
    if r >= 75:
        votes["rsi"] = -0.5 * min((r - 75) / 15, 1.0)
        reasons.append(f"RSI {r:.0f} overbought")
    elif r <= 25:
        votes["rsi"] = 0.5 * min((25 - r) / 15, 1.0)
        reasons.append(f"RSI {r:.0f} oversold")
    else:
        votes["rsi"] = _squash((r - 50) / 15)
        reasons.append(f"RSI {r:.0f}")

    # 4. Momentum of the last 3 candles, normalised by volatility
    mom = (closes[-1] - closes[-4]) / a
    vol_avg = sum(c.volume for c in candles[-21:-1]) / 20 or 1.0
    vol_ratio = last.volume / vol_avg
    # Volume above average strengthens the move, below average weakens it
    votes["momentum"] = max(-1.0, min(1.0, _squash(mom / 2) * min(max(vol_ratio, 0.5), 1.5)))
    reasons.append(f"3-candle move {closes[-1] - closes[-4]:+.0f} (vol x{vol_ratio:.1f})")

    # 5. Higher-timeframe trend: price vs EMA50 (~12.5 hours)
    e50 = ema(closes, 50)[-1]
    votes["higher_tf"] = _squash((price - e50) / (2 * a))
    reasons.append(f"price {'above' if price > e50 else 'below'} 12h trend")

    # 6. Last candle structure: close near high = buyers in control
    rng = last.high - last.low
    votes["candle"] = ((last.close - last.low) / rng * 2 - 1) if rng > 0 else 0.0

    score = sum(WEIGHTS[k] * v for k, v in votes.items())  # -1..1

    # Agreement bonus/penalty: mixed signals reduce confidence
    # (weighted share of indicators pointing the same way)
    active = {k: v for k, v in votes.items() if abs(v) > 0.1}
    total_w = sum(WEIGHTS[k] for k in active)
    agreement = abs(sum(WEIGHTS[k] * math.copysign(1, v) for k, v in active.items())) / total_w if total_w else 0.0
    strength = abs(score) * (0.75 + 0.25 * agreement)

    # Calibration: on a pure random walk the indicators still "agree" by chance, with
    # a median strength around 0.3. A cubic curve keeps that chop near 50-55% (SKIP)
    # while clear, aligned trends climb to 60-75%. Tune with the backtest.
    confidence = 50 + (MAX_CONFIDENCE - 50) * _squash(GAIN * strength ** 3)
    lean = "UP" if score >= 0 else "DOWN"

    # Volatility guard: if the last candle was a huge spike, sit out
    if rng > 3.5 * a:
        reasons.append("volatility spike - sitting out")
        confidence = min(confidence, min_confidence - 0.1)

    direction = lean if confidence >= min_confidence else "SKIP"
    return Signal(
        direction=direction,
        lean=lean,
        confidence=round(confidence, 1),
        price=price,
        score=round(score, 4),
        votes={k: round(v, 3) for k, v in votes.items()},
        reasons=reasons,
        candle_start=last.start + 900,
    )
