"""Main loop: watches the BTC chart 24/7 and sends alerts on the 15-minute marks inside the window."""
from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timedelta

from .ai import Explainer
from .config import Config
from .data import GRANULARITY, Candle, closed_candles, fetch_candles
from .notifier import Notifier
from .strategy import Signal, analyze
from .tracker import Tracker

log = logging.getLogger(__name__)

SEND_DELAY = 8         # seconds after the mark, so the exchange has closed the previous candle
STALE_AFTER = 5 * 60   # don't send an alert more than 5 min late


def slots_for_day(cfg: Config, day: datetime) -> list[datetime]:
    """Alert times for a given local day, e.g. 08:00, 08:15, ... 11:45."""
    start = day.replace(hour=cfg.window_start.hour, minute=cfg.window_start.minute, second=0, microsecond=0)
    end = day.replace(hour=cfg.window_end.hour, minute=cfg.window_end.minute, second=0, microsecond=0)
    out, t = [], start
    while t < end:
        out.append(t)
        t += timedelta(minutes=cfg.interval_minutes)
    return out


def fmt_time(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def format_signal(sig: Signal, slot: datetime, interval: int, today: tuple[int, int],
                  explanation: str | None = None) -> tuple[str, str, str]:
    end = slot + timedelta(minutes=interval)
    icon = {"UP": "🟢⬆️", "DOWN": "🔴⬇️", "SKIP": "⚪⏸️"}[sig.direction]
    action = {"UP": "GO UP", "DOWN": "GO DOWN", "SKIP": "SKIP"}[sig.direction]
    title = f"{icon} BTC {fmt_time(slot)}: {action} ({sig.confidence:.0f}%)"
    lines = [
        f"Price: ${sig.price:,.2f}",
        f"Candle: {fmt_time(slot)} - {fmt_time(end)}",
    ]
    if sig.direction == "SKIP":
        lines.append(f"Leaning {sig.lean} but only {sig.confidence:.0f}% sure - sit this one out.")
    else:
        lines.append(f"Confidence: {sig.confidence:.0f}%")
    if explanation:
        lines.append(f"Why: {explanation}")
        lines.append("Indicators: " + "; ".join(sig.reasons))
    else:
        lines.append("Why: " + "; ".join(sig.reasons))
    wins, calls = today
    if calls:
        lines.append(f"Today so far: {wins}/{calls} correct")
    priority = "default" if sig.direction == "SKIP" else "high"
    return title, "\n".join(lines), priority


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.notifier = Notifier(cfg)
        self.tracker = Tracker(cfg.data_dir)
        self.explainer = Explainer(cfg.ai_explanations)
        self.candles: list[Candle] = []
        self.live_signal: Signal | None = None  # what the bot thinks right now (for the dashboard)
        self.summary_sent: set[str] = set()
        self.last_fetch = 0.0

    # -- data -------------------------------------------------------------
    def refresh(self) -> None:
        self.candles = closed_candles(fetch_candles(self.cfg.product))
        self.last_fetch = _time.time()
        try:
            self.live_signal = analyze(self.candles, self.cfg.min_confidence)
        except ValueError:
            self.live_signal = None
        for r in self.tracker.resolve(self.candles):
            if r["correct"] is not None:
                log.info("%s %s call was %s (close %.2f vs %.2f)", r["slot"], r["direction"],
                         "RIGHT" if r["correct"] else "WRONG", r["close"], r["price"])

    def signal_now(self) -> Signal:
        self.refresh()
        return analyze(self.candles, self.cfg.min_confidence)

    # -- scheduling -------------------------------------------------------
    def active_day(self, now: datetime) -> bool:
        return not (self.cfg.weekdays_only and now.weekday() >= 5)

    def due_slot(self, now: datetime) -> datetime | None:
        if not self.active_day(now):
            return None
        for slot in slots_for_day(self.cfg, now):
            age = (now - slot).total_seconds()
            if SEND_DELAY <= age < STALE_AFTER and not self.tracker.has(int(slot.timestamp())):
                return slot
        return None

    def next_alert(self, now: datetime) -> datetime | None:
        for day_offset in range(8):
            day = now + timedelta(days=day_offset)
            if not self.active_day(day):
                continue
            for slot in slots_for_day(self.cfg, day):
                if slot > now:
                    return slot
        return None

    def seconds_until_next_event(self, now: datetime) -> float:
        wait = float(self.cfg.poll_seconds)
        for day_offset in (0, 1):
            day = now + timedelta(days=day_offset)
            for slot in slots_for_day(self.cfg, day):
                delta = (slot - now).total_seconds() + SEND_DELAY
                if delta > 0:
                    return max(1.0, min(wait, delta))
        return wait

    def handle_slot(self, slot: datetime) -> bool:
        """Returns True when the alert was sent (or skipped as impossible)."""
        slot_ts = int(slot.timestamp())
        self.refresh()
        if not self.candles or self.candles[-1].start + GRANULARITY < slot_ts:
            log.info("Candle before %s not closed yet on the exchange, retrying", fmt_time(slot))
            return False
        # Only use candles that closed before the slot (in case we are a little late)
        history = [c for c in self.candles if c.start + GRANULARITY <= slot_ts]
        sig = analyze(history, self.cfg.min_confidence)
        day = slot.strftime("%Y-%m-%d")
        explanation = self.explainer.explain(sig)
        title, body, prio = format_signal(sig, slot, self.cfg.interval_minutes,
                                          Tracker.score(self.tracker.day_records(day)), explanation)
        self.notifier.send(title, body, prio)
        self.tracker.add(day, fmt_time(slot), sig, explanation)
        return True

    def maybe_send_summary(self, now: datetime) -> None:
        day = now.strftime("%Y-%m-%d")
        slots = slots_for_day(self.cfg, now)
        if not slots or day in self.summary_sent or not self.active_day(now):
            return
        window_over = now >= slots[-1] + timedelta(minutes=self.cfg.interval_minutes, seconds=SEND_DELAY)
        recs = self.tracker.day_records(day)
        if not window_over or not recs or self.tracker.pending(day):
            return
        wins, calls = Tracker.score(recs)
        skips = sum(1 for r in recs if r["direction"] == "SKIP")
        aw, ac = self.tracker.all_time()
        lines = [f"{'✅' if r['correct'] else '❌' if r['correct'] is False else '⚪'} {r['slot']}: "
                 f"{r['direction']} ({r['confidence']:.0f}%) -> {r['outcome']}" for r in recs]
        lines.append("")
        lines.append(f"Today: {wins}/{calls} correct, {skips} skipped")
        if ac:
            lines.append(f"All time: {aw}/{ac} ({100 * aw / ac:.0f}%)")
        self.notifier.send(f"📊 BTC daily scorecard {day}", "\n".join(lines))
        self.summary_sent.add(day)

    # -- main loop --------------------------------------------------------
    def run(self) -> None:
        tz = self.cfg.timezone
        if self.cfg.dashboard:
            from .dashboard import start_dashboard
            start_dashboard(self)
        log.info("AI explanations: %s", "on" if self.explainer.active else "off (set ANTHROPIC_API_KEY to enable)")
        log.info("Watching %s 24/7. Alerts every %d min from %s to %s (%s). Channels: %s",
                 self.cfg.product, self.cfg.interval_minutes, self.cfg.window_start.strftime("%H:%M"),
                 self.cfg.window_end.strftime("%H:%M"), tz.key, ", ".join(self.notifier.channels) or "console only")
        while True:
            try:
                now = datetime.now(tz)
                slot = self.due_slot(now)
                if slot is not None:
                    if not self.handle_slot(slot):
                        _time.sleep(5)
                        continue
                elif _time.time() - self.last_fetch >= self.cfg.poll_seconds:
                    self.refresh()
                    if self.candles:
                        log.debug("BTC last close %.2f", self.candles[-1].close)
                self.maybe_send_summary(now)
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("Loop error, will retry")
                _time.sleep(10)
                continue
            _time.sleep(self.seconds_until_next_event(datetime.now(tz)))
