"""Remembers every alert and checks afterwards whether it was right."""
from __future__ import annotations

import json
from pathlib import Path

from .data import GRANULARITY, Candle
from .strategy import Signal


class Tracker:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "predictions.jsonl"
        self.records: list[dict] = []
        if self.path.exists():
            self.records = [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in self.records))
        tmp.replace(self.path)

    def has(self, candle_start: int) -> bool:
        return any(r["candle_start"] == candle_start for r in self.records)

    def add(self, day: str, slot_label: str, sig: Signal, explanation: str | None = None) -> None:
        self.records.append({
            "day": day,
            "slot": slot_label,
            "candle_start": sig.candle_start,
            "direction": sig.direction,
            "lean": sig.lean,
            "confidence": sig.confidence,
            "price": sig.price,
            "reasons": sig.reasons,
            "explanation": explanation,
            "outcome": None,
            "close": None,
            "correct": None,
        })
        self._save()

    def resolve(self, candles: list[Candle]) -> list[dict]:
        """Fill in outcomes for predictions whose candle has closed. Returns newly resolved records."""
        by_start = {c.start: c for c in candles}
        newly = []
        for r in self.records:
            if r["outcome"] is not None:
                continue
            c = by_start.get(r["candle_start"])
            if c is None:
                continue
            r["close"] = c.close
            r["outcome"] = "UP" if c.close > r["price"] else "DOWN" if c.close < r["price"] else "FLAT"
            r["correct"] = None if r["direction"] == "SKIP" else r["direction"] == r["outcome"]
            newly.append(r)
        if newly:
            self._save()
        return newly

    def day_records(self, day: str) -> list[dict]:
        return [r for r in self.records if r["day"] == day]

    @staticmethod
    def score(records: list[dict]) -> tuple[int, int]:
        calls = [r for r in records if r["correct"] is not None]
        return sum(1 for r in calls if r["correct"]), len(calls)

    def all_time(self) -> tuple[int, int]:
        return self.score(self.records)

    def pending(self, day: str) -> bool:
        return any(r["outcome"] is None for r in self.day_records(day))


__all__ = ["Tracker", "GRANULARITY"]
