"""Main loop: watches the BTC chart 24/7 and sends alerts on the 15-minute marks inside the window."""
from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timedelta

import threading

from .ai import Explainer
from .config import Config
from .data import GRANULARITY, Candle, closed_candles, fetch_candles, fetch_recent_history
from .learn import AdaptiveModel, train
from .notifier import Notifier
from .strategy import Signal, analyze
from .tracker import Tracker

log = logging.getLogger(__name__)

SEND_DELAY = 8         # seconds after the mark, so the exchange has closed the previous candle
STALE_AFTER = 5 * 60   # don't send an alert more than 5 min late


def crosses_midnight(cfg: Config) -> bool:
    return cfg.window_end <= cfg.window_start


def slots_for_day(cfg: Config, day: datetime) -> list[datetime]:
    """Alert times for the session that STARTS on `day`, e.g. 08:00, 08:15, ... 01:45 the next morning.

    If WINDOW_END is earlier than (or equal to) WINDOW_START, the window runs past midnight.
    """
    start = day.replace(hour=cfg.window_start.hour, minute=cfg.window_start.minute, second=0, microsecond=0)
    end = day.replace(hour=cfg.window_end.hour, minute=cfg.window_end.minute, second=0, microsecond=0)
    if crosses_midnight(cfg):
        end += timedelta(days=1)
    out, t = [], start
    while t < end:
        out.append(t)
        t += timedelta(minutes=cfg.interval_minutes)
    return out


def session_day(cfg: Config, dt: datetime) -> datetime:
    """The day whose session `dt` belongs to (a 1 AM alert belongs to the previous day's session)."""
    if crosses_midnight(cfg) and dt.time() < cfg.window_start:
        return dt - timedelta(days=1)
    return dt


def in_window(cfg: Config, t) -> bool:
    if crosses_midnight(cfg):
        return t >= cfg.window_start or t < cfg.window_end
    return cfg.window_start <= t < cfg.window_end


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
    if sig.history and sig.history.get("n"):
        h = sig.history
        lines.append(f"Track record: signals like this were right {h['wins']}/{h['n']} times "
                     f"in the last {h['days']} days")
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
        self.explainer = Explainer(cfg.ai_explanations, cfg.ai_model)
        self.candles: list[Candle] = []
        self.live_signal: Signal | None = None  # what the bot thinks right now (for the dashboard)
        self.model: AdaptiveModel | None = None  # learned weights + calibration, refreshed every few hours
        self.learn_error: str | None = None
        self.summary_sent: set[str] = set()
        self.last_fetch = 0.0

    # -- data -------------------------------------------------------------
    def refresh(self) -> None:
        self.candles = closed_candles(fetch_candles(self.cfg.product))
        self.last_fetch = _time.time()
        try:
            self.live_signal = self.score(self.candles)
        except ValueError:
            self.live_signal = None
        for r in self.tracker.resolve(self.candles):
            if r["correct"] is not None:
                log.info("%s %s call was %s (close %.2f vs %.2f)", r["slot"], r["direction"],
                         "RIGHT" if r["correct"] else "WRONG", r["close"], r["price"])

    def score(self, candles: list[Candle]) -> Signal:
        """Indicator signal, then (if trained) learned weights + history-based confidence."""
        sig = analyze(candles, self.cfg.min_confidence)
        model = self.model
        if model is not None and model.ready:
            sig = model.apply(sig, self.cfg.min_confidence)
        return sig

    # -- learning ---------------------------------------------------------
    def learn(self) -> AdaptiveModel:
        candles = fetch_recent_history(self.cfg.product, self.cfg.learn_days)
        days = len(candles) * GRANULARITY / 86400
        model = train(candles, days=days, min_confidence=self.cfg.min_confidence)
        self.model = model
        self.learn_error = None
        s = model.stats
        log.info("Learned from %d days: unseen-data accuracy %.1f%% (rules %.1f%%, naive %.1f%%); "
                 "%d calls at >=%.0f%% confidence", s["days"], s["oos_accuracy"], s["rule_accuracy"],
                 s["naive_accuracy"], s["calls_at_threshold"], self.cfg.min_confidence)
        return model

    def _learn_loop(self) -> None:
        while True:
            try:
                self.learn()
                wait = self.cfg.learn_every_hours * 3600
            except Exception as exc:
                self.learn_error = str(exc)
                log.warning("Learning from history failed (%s); using fixed rules for now", exc)
                wait = 15 * 60
            _time.sleep(wait)

    def signal_now(self) -> Signal:
        self.refresh()
        return self.score(self.candles)

    # -- scheduling -------------------------------------------------------
    def active_day(self, now: datetime) -> bool:
        return not (self.cfg.weekdays_only and now.weekday() >= 5)

    def sessions(self, now: datetime, offsets) -> list[tuple[datetime, list[datetime]]]:
        """(session start day, its alert times) for the given day offsets, skipping inactive days."""
        out = []
        for off in offsets:
            day = now + timedelta(days=off)
            if self.active_day(day):
                out.append((day, slots_for_day(self.cfg, day)))
        return out

    def due_slot(self, now: datetime) -> datetime | None:
        for _, slots in self.sessions(now, (-1, 0)):
            for slot in slots:
                age = (now - slot).total_seconds()
                if SEND_DELAY <= age < STALE_AFTER and not self.tracker.has(int(slot.timestamp())):
                    return slot
        return None

    def next_alert(self, now: datetime) -> datetime | None:
        for _, slots in self.sessions(now, range(-1, 8)):
            for slot in slots:
                if slot > now:
                    return slot
        return None

    def seconds_until_next_event(self, now: datetime) -> float:
        wait = float(self.cfg.poll_seconds)
        for _, slots in self.sessions(now, (-1, 0, 1)):
            for slot in slots:
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
        sig = self.score(history)
        day = session_day(self.cfg, slot).strftime("%Y-%m-%d")
        explanation = self.explainer.explain(sig)
        title, body, prio = format_signal(sig, slot, self.cfg.interval_minutes,
                                          Tracker.score(self.tracker.day_records(day)), explanation)
        self.notifier.send(title, body, prio)
        self.tracker.add(day, fmt_time(slot), sig, explanation)
        return True

    def maybe_send_summary(self, now: datetime) -> None:
        for start_day, slots in self.sessions(now, (-1, 0)):
            self._summary_for(now, start_day.strftime("%Y-%m-%d"), slots)

    def _summary_for(self, now: datetime, day: str, slots: list[datetime]) -> None:
        if not slots or day in self.summary_sent:
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
        lines.append(f"Session: {wins}/{calls} correct, {skips} skipped")
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
        if self.cfg.adaptive:
            threading.Thread(target=self._learn_loop, daemon=True, name="learn").start()
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
