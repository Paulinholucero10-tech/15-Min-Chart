"""Adaptive layer: learns from the recent chart and turns signals into honest odds.

Ideas borrowed from other projects:
  * "Time Machine" replay (day-trader-skill): before trusting a signal, look at
    every recent time a similar signal fired and count how often it was right.
  * Retrain on the latest data (coinpusher): re-learn every few hours.
  * Always compare against a naive baseline (btc-lstm-prediction): a model only
    counts if it beats chance on data it did not see while training.

How it works
  1. Download the last N days of 15m candles (default 30, about 2,900 candles).
  2. For every candle, compute the same 7 indicator votes the live bot uses and
     record whether the NEXT candle closed higher.
  3. Fit a small logistic regression (learned weights for the 7 votes).
  4. Walk-forward test: train only on the past, predict the future chunk, repeat.
     These out-of-sample predictions are the bot's real track record.
  5. Calibrate: group out-of-sample predictions by strength and measure how often
     each group was actually right (shrunk toward 50% when there are few cases,
     and forced to never go down as strength goes up).
  6. Live: the learned model scores the current votes, and the calibration table
     turns that score into the confidence you see. If recent history shows the
     signals had no edge, confidence stays near 50% and the bot says SKIP.
"""
from __future__ import annotations

import logging
import math
import time as _time
from dataclasses import dataclass, field

from .data import GRANULARITY, Candle
from .strategy import MAX_CONFIDENCE, Signal, analyze

log = logging.getLogger(__name__)

FEATURES = ["trend_t", "trend_ema", "macd", "rsi", "momentum", "higher_tf", "candle"]
LOOKBACK = 120            # candles of history used to compute each sample's indicators
FOLDS = 5                 # walk-forward chunks
L2 = 2.0                  # regularisation: keeps the learned weights modest
PRIOR = 30                # shrink each calibration bucket toward 50% as if it had 30 extra coin flips
BIN_EDGES = [0.50, 0.51, 0.52, 0.53, 0.54, 0.555, 0.57, 0.59, 0.62, 1.01]
MIN_SAMPLES = 400


# --- tiny logistic regression (Newton's method, no numpy needed) -------------

def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1 / (1 + math.exp(-z))
    e = math.exp(z)
    return e / (1 + e)


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting for a small dense system."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[piv] = m[piv], m[col]
        if abs(m[col][col]) < 1e-12:
            continue
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            for c in range(col, n + 1):
                m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))
        x[r] = s / m[r][r] if abs(m[r][r]) > 1e-12 else 0.0
    return x


def fit_logistic(xs: list[list[float]], ys: list[int], l2: float = L2, iters: int = 15) -> list[float]:
    """Returns weights [bias, w1..wk]. The bias is not regularised."""
    k = len(xs[0]) + 1
    w = [0.0] * k
    rows = [[1.0] + x for x in xs]
    for _ in range(iters):
        grad = [0.0] * k
        hess = [[0.0] * k for _ in range(k)]
        for r, y in zip(rows, ys):
            p = _sigmoid(sum(wi * ri for wi, ri in zip(w, r)))
            d = p - y
            s = p * (1 - p)
            for i in range(k):
                grad[i] += d * r[i]
                ri_s = r[i] * s
                for j in range(i, k):
                    hess[i][j] += ri_s * r[j]
        for i in range(k):
            for j in range(i):
                hess[i][j] = hess[j][i]
            if i > 0:
                grad[i] += l2 * w[i]
                hess[i][i] += l2
        step = _solve(hess, grad)
        w = [wi - si for wi, si in zip(w, step)]
        if max(abs(s) for s in step) < 1e-6:
            break
    return w


def predict_up(w: list[float], x: list[float]) -> float:
    return _sigmoid(w[0] + sum(wi * xi for wi, xi in zip(w[1:], x)))


# --- dataset -------------------------------------------------------------------

@dataclass
class Sample:
    start: int        # start time of the candle being predicted
    x: list[float]    # indicator votes
    rule_score: float
    up: int           # 1 if that candle closed above the previous close


def build_samples(candles: list[Candle], lookback: int = LOOKBACK) -> list[Sample]:
    out = []
    for i in range(lookback, len(candles) - 1):
        nxt, cur = candles[i + 1], candles[i]
        if nxt.start - cur.start != GRANULARITY or nxt.close == cur.close:
            continue  # gap in the data, or a perfectly flat candle
        window = candles[i - lookback + 1:i + 1]
        if window[-1].start - window[0].start != (lookback - 1) * GRANULARITY:
            continue
        sig = analyze(window)
        out.append(Sample(nxt.start, [sig.votes[f] for f in FEATURES], sig.score, int(nxt.close > cur.close)))
    return out


# --- calibration ----------------------------------------------------------------

def _pav(values: list[float], weights: list[float]) -> list[float]:
    """Pool-adjacent-violators: smallest change that makes values non-decreasing."""
    blocks = [[v, w, 1] for v, w in zip(values, weights)]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] > blocks[i + 1][0]:
            v1, w1, c1 = blocks[i]
            v2, w2, c2 = blocks[i + 1]
            tw = w1 + w2
            blocks[i] = [(v1 * w1 + v2 * w2) / tw if tw else (v1 + v2) / 2, tw, c1 + c2]
            del blocks[i + 1]
            i = max(i - 1, 0)
        else:
            i += 1
    out = []
    for v, _, c in blocks:
        out.extend([v] * c)
    return out


def calibrate(strengths: list[float], correct: list[bool]) -> list[dict]:
    """Bucket by raw strength (max(p, 1-p)), measure real accuracy, shrink and smooth."""
    rows = []
    for lo, hi in zip(BIN_EDGES, BIN_EDGES[1:]):
        hits = [c for s, c in zip(strengths, correct) if lo <= s < hi]
        n, wins = len(hits), sum(hits)
        rows.append({"lo": lo, "hi": hi, "n": n, "wins": wins,
                     "raw": wins / n if n else 0.5,
                     "acc": (wins + PRIOR / 2) / (n + PRIOR)})
    smooth = _pav([r["acc"] for r in rows], [r["n"] + PRIOR for r in rows])
    for r, s in zip(rows, smooth):
        r["acc"] = max(0.5, s)  # never claim to be worse than a coin flip: that just means SKIP
    return rows


# --- the model -------------------------------------------------------------------

@dataclass
class AdaptiveModel:
    weights: list[float] = field(default_factory=list)
    table: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    trained_at: float = 0.0

    @property
    def ready(self) -> bool:
        return bool(self.weights and self.table)

    def lookup(self, strength: float) -> tuple[float, int, int]:
        for r in self.table:
            if r["lo"] <= strength < r["hi"]:
                return r["acc"], r["n"], r["wins"]
        r = self.table[-1]
        return r["acc"], r["n"], r["wins"]

    def apply(self, sig: Signal, min_confidence: float) -> Signal:
        """Replace the rule-based confidence with the learned, history-checked one."""
        x = [sig.votes[f] for f in FEATURES]
        p_up = predict_up(self.weights, x)
        strength = max(p_up, 1 - p_up)
        acc, n, wins = self.lookup(strength)
        sig.rule_confidence = sig.confidence
        sig.lean = "UP" if p_up >= 0.5 else "DOWN"
        sig.confidence = round(min(100 * acc, MAX_CONFIDENCE), 1)
        sig.p_up = round(p_up, 4)
        sig.history = {"n": n, "wins": wins, "days": self.stats.get("days")}
        spike = any("volatility spike" in r for r in sig.reasons)
        if spike:
            sig.confidence = min(sig.confidence, min_confidence - 0.1)
        sig.direction = sig.lean if sig.confidence >= min_confidence else "SKIP"
        if n:
            sig.reasons.append(f"last {self.stats.get('days')}d: similar signals right {wins}/{n}")
        return sig

    def summary(self) -> dict:
        return {
            "ready": self.ready,
            "trained_at": self.trained_at,
            "weights": dict(zip(["bias"] + FEATURES, [round(w, 3) for w in self.weights])),
            "table": [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()} for r in self.table],
            **self.stats,
        }


def train(candles: list[Candle], days: float | None = None, min_confidence: float = 60.0) -> AdaptiveModel:
    samples = build_samples(candles)
    if len(samples) < MIN_SAMPLES:
        raise ValueError(f"not enough history to learn from ({len(samples)} samples, need {MIN_SAMPLES})")

    # Walk-forward: for each chunk, train only on the chunks before it
    size = len(samples) // FOLDS
    oos_p: list[float] = []
    oos: list[Sample] = []
    for k in range(1, FOLDS):
        past = samples[:k * size]
        future = samples[k * size:(k + 1) * size if k < FOLDS - 1 else len(samples)]
        w = fit_logistic([s.x for s in past], [s.up for s in past])
        oos_p.extend(predict_up(w, s.x) for s in future)
        oos.extend(future)

    strengths = [max(p, 1 - p) for p in oos_p]
    correct = [(p >= 0.5) == bool(s.up) for p, s in zip(oos_p, oos)]
    table = calibrate(strengths, correct)

    model = AdaptiveModel(weights=fit_logistic([s.x for s in samples], [s.up for s in samples]),
                          table=table, trained_at=_time.time())

    # Honest scorecard, all on data the model had not seen
    calls = [(c, acc) for c, st in zip(correct, strengths)
             for acc in [model.lookup(st)[0]] if 100 * acc >= min_confidence]
    rule_right = [(s.rule_score >= 0) == bool(s.up) for s in oos]
    up_rate = sum(s.up for s in oos) / len(oos)
    model.stats = {
        "days": round(days) if days else round(len(candles) * GRANULARITY / 86400),
        "samples": len(samples),
        "tested": len(oos),
        "oos_accuracy": round(100 * sum(correct) / len(correct), 1),
        "rule_accuracy": round(100 * sum(rule_right) / len(rule_right), 1),
        "naive_accuracy": round(100 * max(up_rate, 1 - up_rate), 1),
        "calls_at_threshold": len(calls),
        "calls_accuracy": round(100 * sum(c for c, _ in calls) / len(calls), 1) if calls else None,
        "best_bucket": round(100 * max(r["acc"] for r in table), 1),
    }
    return model


def report(model: AdaptiveModel, min_confidence: float) -> str:
    s = model.stats
    lines = [
        f"Learned from {s['samples']} candles ({s['days']} days). Tested on {s['tested']} it had not seen:",
        f"  learned model accuracy (every candle): {s['oos_accuracy']}%",
        f"  fixed rules accuracy (every candle):   {s['rule_accuracy']}%",
        f"  naive baseline (always guess the more common direction): {s['naive_accuracy']}%",
    ]
    if s["calls_at_threshold"]:
        lines.append(f"  calls at >= {min_confidence:.0f}% confidence: {s['calls_at_threshold']}, "
                     f"right {s['calls_accuracy']}%")
    else:
        lines.append(f"  no signal reached {min_confidence:.0f}% recently -> the bot would SKIP everything")
    lines += ["", "Strength bucket   cases   raw hit   confidence shown"]
    for r in model.table:
        if r["n"]:
            lines.append(f"  {r['lo']:.3f}-{min(r['hi'], 1):.3f}   {r['n']:6d}   {100 * r['raw']:6.1f}%   {100 * r['acc']:6.1f}%")
    lines += ["", "Learned weights (positive = pushes toward UP):"]
    lines += [f"  {k:10s} {v:+.3f}" for k, v in zip(["bias"] + FEATURES, model.weights)]
    return "\n".join(lines)
