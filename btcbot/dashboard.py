"""Small built-in web dashboard: live chart, today's calls and the bot's track record.

Served from the same process as the bot (no extra dependencies). Open
http://localhost:8080 while `python -m btcbot run` is running.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

from .bot import session_day
from .tracker import Tracker

if TYPE_CHECKING:
    from .bot import Bot

log = logging.getLogger(__name__)
HTML = (Path(__file__).parent / "dashboard.html").read_bytes()
CHART_CANDLES = 192  # 48 hours of 15m candles


def build_state(bot: "Bot") -> dict:
    cfg = bot.cfg
    tz = cfg.timezone
    now = datetime.now(tz)
    candles = list(bot.candles)[-CHART_CANDLES:]
    records = list(bot.tracker.records)
    today = session_day(cfg, now).strftime("%Y-%m-%d")  # current (or most recent) alert session
    today_recs = [r for r in records if r["day"] == today]

    def local_ts(ts: int) -> int:
        # The chart library shows UTC; shift so the axis reads in the user's timezone
        off = datetime.fromtimestamp(ts, tz).utcoffset()
        return ts + int(off.total_seconds()) if off else ts

    sig = bot.live_signal
    wins, calls = Tracker.score(today_recs)
    aw, ac = Tracker.score(records)
    # Accuracy by confidence bucket across all resolved calls
    buckets = []
    for lo, hi in ((60, 65), (65, 70), (70, 75), (75, 101)):
        b = [r for r in records if r["correct"] is not None and lo <= r["confidence"] < hi]
        if b:
            buckets.append({"label": f"{lo}-{min(hi, 100)}%", "n": len(b),
                            "acc": round(100 * sum(r["correct"] for r in b) / len(b), 1)})
    nxt = bot.next_alert(now)
    return {
        "product": cfg.product,
        "timezone": tz.key,
        "window": f"{cfg.window_start.strftime('%H:%M')}-{cfg.window_end.strftime('%H:%M')}",
        "min_confidence": cfg.min_confidence,
        "updated": datetime.fromtimestamp(bot.last_fetch, tz).isoformat() if bot.last_fetch else None,
        "next_alert": nxt.isoformat() if nxt else None,
        "channels": bot.notifier.channels,
        "ai": bot.explainer.active,
        "price": candles[-1].close if candles else None,
        "candles": [{"time": local_ts(c.start), "open": c.open, "high": c.high,
                     "low": c.low, "close": c.close} for c in candles],
        "live": None if sig is None else {
            "direction": sig.direction, "lean": sig.lean, "confidence": sig.confidence,
            "reasons": sig.reasons, "votes": sig.votes, "history": sig.history,
            "rule_confidence": sig.rule_confidence,
        },
        "model": (bot.model.summary() if bot.model is not None and bot.model.ready
                  else {"ready": False, "adaptive": cfg.adaptive, "error": bot.learn_error}),
        "today": [{**r, "chart_time": local_ts(r["candle_start"])} for r in today_recs],
        "today_score": [wins, calls],
        "all_time": [aw, ac],
        "skips_all_time": sum(1 for r in records if r["direction"] == "SKIP"),
        "buckets": buckets,
        "history_days": _history(records),
    }


def _history(records: list[dict]) -> list[dict]:
    days: dict[str, list[dict]] = {}
    for r in records:
        days.setdefault(r["day"], []).append(r)
    out = []
    for day in sorted(days)[-14:]:
        w, c = Tracker.score(days[day])
        out.append({"day": day, "wins": w, "calls": c})
    return out


def make_handler(bot: "Bot"):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", HTML)
            elif self.path == "/api/state":
                try:
                    body = json.dumps(build_state(bot)).encode()
                    self._send(200, "application/json", body)
                except Exception as exc:  # never crash the bot because of the dashboard
                    log.exception("dashboard state failed")
                    self._send(500, "application/json", json.dumps({"error": str(exc)}).encode())
            else:
                self._send(404, "text/plain", b"not found")

        def _send(self, code: int, ctype: str, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep the console quiet
            pass

    return Handler


def start_dashboard(bot: "Bot") -> ThreadingHTTPServer | None:
    cfg = bot.cfg
    try:
        server = ThreadingHTTPServer((cfg.dashboard_host, cfg.dashboard_port), make_handler(bot))
    except OSError as exc:
        log.error("Dashboard could not start on %s:%s (%s)", cfg.dashboard_host, cfg.dashboard_port, exc)
        return None
    threading.Thread(target=server.serve_forever, daemon=True, name="dashboard").start()
    shown = "localhost" if cfg.dashboard_host in ("127.0.0.1", "0.0.0.0") else cfg.dashboard_host
    log.info("Dashboard: http://%s:%d", shown, cfg.dashboard_port)
    return server
