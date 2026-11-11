import math
import random
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from btcbot.backtest import evaluate, report
from btcbot.bot import Bot, format_signal, slots_for_day
from btcbot.config import Config
from btcbot.data import Candle, closed_candles
from btcbot.strategy import analyze, rsi
from btcbot.tracker import Tracker

T0 = 1_762_000_200 // 900 * 900


def make_candles(n, drift=0.0, noise=20.0, seed=1, start=T0, price=100_000.0):
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        o = price
        c = o + drift + rnd.gauss(0, noise)
        h, l = max(o, c) + abs(rnd.gauss(0, noise / 2)), min(o, c) - abs(rnd.gauss(0, noise / 2))
        out.append(Candle(start + i * 900, o, h, l, c, 10 + rnd.random()))
        price = c
    return out


class StrategyTests(unittest.TestCase):
    def test_uptrend_says_up(self):
        sig = analyze(make_candles(120, drift=15, noise=10))
        self.assertEqual(sig.lean, "UP")
        self.assertEqual(sig.direction, "UP")
        self.assertGreaterEqual(sig.confidence, 60)

    def test_downtrend_leans_down(self):
        for seed in range(5):
            sig = analyze(make_candles(120, drift=-15, noise=10, seed=seed))
            self.assertEqual(sig.lean, "DOWN")
            self.assertGreater(sig.confidence, 55)

    def test_mirror_symmetry(self):
        cs = make_candles(120, drift=15, noise=10)
        p = 200_000
        mirrored = [Candle(c.start, p - c.open, p - c.low, p - c.high, p - c.close, c.volume) for c in cs]
        a, b = analyze(cs), analyze(mirrored)
        self.assertEqual(a.confidence, b.confidence)
        self.assertEqual({a.lean, b.lean}, {"UP", "DOWN"})

    def test_noise_is_skip_or_low_confidence(self):
        sig = analyze(make_candles(120, drift=0, noise=20, seed=7), min_confidence=60)
        self.assertTrue(50 <= sig.confidence <= 85)

    def test_threshold_forces_skip(self):
        sig = analyze(make_candles(120, drift=15, noise=10), min_confidence=99)
        self.assertEqual(sig.direction, "SKIP")

    def test_predicts_next_candle(self):
        cs = make_candles(100)
        self.assertEqual(analyze(cs).candle_start, cs[-1].start + 900)

    def test_needs_enough_data(self):
        with self.assertRaises(ValueError):
            analyze(make_candles(10))

    def test_rsi_bounds(self):
        closes = [c.close for c in make_candles(100, drift=50, noise=1)]
        self.assertGreater(rsi(closes), 90)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(timezone=ZoneInfo("America/New_York"))

    def test_72_slots_8am_to_2am_next_morning(self):
        day = datetime(2026, 11, 11, 15, 0, tzinfo=self.cfg.timezone)
        slots = slots_for_day(self.cfg, day)
        self.assertEqual(len(slots), 72)
        self.assertEqual((slots[0].day, slots[0].hour, slots[0].minute), (11, 8, 0))
        self.assertEqual((slots[-1].day, slots[-1].hour, slots[-1].minute), (12, 1, 45))

    def test_daytime_window_still_works(self):
        from datetime import time as dtime
        self.cfg.window_end = dtime(14, 0)
        slots = slots_for_day(self.cfg, datetime(2026, 11, 11, 3, 0, tzinfo=self.cfg.timezone))
        self.assertEqual(len(slots), 24)
        self.assertEqual((slots[-1].hour, slots[-1].minute), (13, 45))

    def test_due_slot(self):
        with tempfile.TemporaryDirectory() as d:
            self.cfg.data_dir = Path(d)
            bot = Bot(self.cfg)
            tz = self.cfg.timezone
            due = lambda *a: bot.due_slot(datetime(2026, 11, *a, tzinfo=tz))
            self.assertIsNone(due(11, 7, 59))
            self.assertEqual(due(11, 8, 15, 10).minute, 15)
            self.assertEqual(due(11, 14, 0, 10).hour, 14)
            self.assertEqual(due(11, 23, 45, 10).hour, 23)
            self.assertEqual(due(12, 0, 0, 10).hour, 0)      # midnight
            self.assertEqual(due(12, 1, 45, 10).hour, 1)     # last alert of the night
            self.assertIsNone(due(12, 2, 1))                 # window over
            self.assertIsNone(due(12, 5, 0))
            self.assertIsNone(due(11, 8, 10))                # stale

    def test_session_day_and_next_alert(self):
        from btcbot.bot import in_window, session_day
        from datetime import time as dtime
        tz = self.cfg.timezone
        self.assertEqual(session_day(self.cfg, datetime(2026, 11, 12, 1, 30, tzinfo=tz)).day, 11)
        self.assertEqual(session_day(self.cfg, datetime(2026, 11, 12, 9, 0, tzinfo=tz)).day, 12)
        self.assertTrue(in_window(self.cfg, dtime(0, 30)))
        self.assertFalse(in_window(self.cfg, dtime(3, 0)))
        with tempfile.TemporaryDirectory() as d:
            self.cfg.data_dir = Path(d)
            bot = Bot(self.cfg)
            nxt = bot.next_alert(datetime(2026, 11, 12, 0, 50, tzinfo=tz))
            self.assertEqual((nxt.day, nxt.hour, nxt.minute), (12, 1, 0))
            nxt = bot.next_alert(datetime(2026, 11, 12, 3, 0, tzinfo=tz))
            self.assertEqual((nxt.day, nxt.hour, nxt.minute), (12, 8, 0))
            self.assertAlmostEqual(bot.seconds_until_next_event(datetime(2026, 11, 12, 0, 59, 30, tzinfo=tz)), 38, delta=1)

    def test_night_alert_recorded_under_session_and_summary_after_2am(self):
        tz = self.cfg.timezone
        slot = datetime(2026, 11, 12, 1, 45, tzinfo=tz)
        candles = make_candles(150, drift=10, start=int(slot.timestamp()) - 150 * 900)
        with tempfile.TemporaryDirectory() as d:
            self.cfg.data_dir = Path(d)
            bot = Bot(self.cfg)
            sent = []
            bot.notifier.send = lambda t, b, p="default": sent.append(t)
            bot.refresh = lambda: setattr(bot, "candles", candles)
            self.assertTrue(bot.handle_slot(slot))
            self.assertEqual(bot.tracker.records[0]["day"], "2026-11-11")
            nxt = Candle(int(slot.timestamp()), 1, 1, 1, candles[-1].close + 5, 1)
            bot.tracker.resolve(candles + [nxt])
            bot.maybe_send_summary(datetime(2026, 11, 12, 1, 59, tzinfo=tz))
            self.assertEqual(len(sent), 1)  # not yet
            bot.maybe_send_summary(datetime(2026, 11, 12, 2, 1, tzinfo=tz))
            self.assertIn("scorecard 2026-11-11", sent[-1])

    def test_weekdays_only(self):
        with tempfile.TemporaryDirectory() as d:
            self.cfg.data_dir = Path(d)
            self.cfg.weekdays_only = True
            bot = Bot(self.cfg)
            tz = self.cfg.timezone
            self.assertIsNone(bot.due_slot(datetime(2026, 11, 14, 8, 0, 30, tzinfo=tz)))   # Saturday
            # Friday's session still runs past midnight into Saturday morning
            self.assertIsNotNone(bot.due_slot(datetime(2026, 11, 14, 1, 0, 30, tzinfo=tz)))
            # ...but Sunday's late-night slots (Saturday session) don't fire
            self.assertIsNone(bot.due_slot(datetime(2026, 11, 15, 1, 0, 30, tzinfo=tz)))

    def test_wait_until_next_slot(self):
        with tempfile.TemporaryDirectory() as d:
            self.cfg.data_dir = Path(d)
            bot = Bot(self.cfg)
            now = datetime(2026, 11, 11, 8, 14, 30, tzinfo=self.cfg.timezone)
            self.assertAlmostEqual(bot.seconds_until_next_event(now), 38, delta=1)


class BotFlowTests(unittest.TestCase):
    def test_full_slot_and_resolution(self):
        tz = ZoneInfo("America/New_York")
        slot = datetime(2026, 11, 11, 8, 0, tzinfo=tz)
        slot_ts = int(slot.timestamp())
        candles = make_candles(150, drift=10, start=slot_ts - 150 * 900)
        with tempfile.TemporaryDirectory() as d:
            cfg = Config(timezone=tz, data_dir=Path(d))
            bot = Bot(cfg)
            sent = []
            bot.notifier.send = lambda t, b, p="default": sent.append((t, b))
            bot.refresh = lambda: setattr(bot, "candles", candles)
            self.assertTrue(bot.handle_slot(slot))
            self.assertIn("8:00 AM", sent[0][0])
            self.assertTrue(bot.tracker.has(slot_ts))
            # next candle closes higher than price -> resolve
            nxt = Candle(slot_ts, candles[-1].close, 1e9, 0, candles[-1].close + 50, 1)
            resolved = bot.tracker.resolve(candles + [nxt])
            self.assertEqual(resolved[0]["outcome"], "UP")
            # persisted
            self.assertEqual(len(Tracker(Path(d)).records), 1)

    def test_waits_for_closed_candle(self):
        tz = ZoneInfo("America/New_York")
        slot = datetime(2026, 11, 11, 8, 0, tzinfo=tz)
        candles = make_candles(150, start=int(slot.timestamp()) - 151 * 900)  # last one ends 07:45
        with tempfile.TemporaryDirectory() as d:
            bot = Bot(Config(timezone=tz, data_dir=Path(d)))
            bot.refresh = lambda: setattr(bot, "candles", candles)
            self.assertFalse(bot.handle_slot(slot))

    def test_closed_candles_drops_forming(self):
        cs = make_candles(3)
        self.assertEqual(len(closed_candles(cs, now=cs[-1].start + 100)), 2)

    def test_format(self):
        sig = analyze(make_candles(120, drift=15, noise=10))
        slot = datetime(2026, 11, 11, 9, 30, tzinfo=ZoneInfo("UTC"))
        title, body, _ = format_signal(sig, slot, 15, (2, 3))
        self.assertIn("GO UP", title)
        self.assertIn("9:30 AM - 9:45 AM", body)
        self.assertIn("2/3", body)


class BacktestTests(unittest.TestCase):
    def test_backtest_runs(self):
        cfg = Config(timezone=ZoneInfo("UTC"))
        res = evaluate(cfg, make_candles(96 * 5, drift=2, noise=20))
        self.assertTrue(res)
        self.assertIn("Accuracy", report(res, 60))


if __name__ == "__main__":
    unittest.main()


class DashboardTests(unittest.TestCase):
    def test_state_and_http(self):
        import json
        import urllib.request
        from btcbot.dashboard import start_dashboard
        tz = ZoneInfo("America/New_York")
        with tempfile.TemporaryDirectory() as d:
            bot = Bot(Config(timezone=tz, data_dir=Path(d), dashboard_port=0))
            bot.candles = make_candles(150, drift=10)
            bot.live_signal = analyze(bot.candles)
            sig = analyze(bot.candles[:-1])
            from btcbot.bot import session_day
            day = session_day(bot.cfg, datetime.now(tz)).strftime("%Y-%m-%d")
            bot.tracker.add(day, "8:00 AM", sig, "test reason")
            bot.tracker.resolve(bot.candles)
            server = start_dashboard(bot)
            try:
                port = server.server_address[1]
                html = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read()
                self.assertIn(b"BTC 15-Min Bot", html)
                state = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state").read())
            finally:
                server.shutdown()
            self.assertEqual(len(state["candles"]), 150)
            self.assertEqual(state["today"][0]["explanation"], "test reason")
            self.assertEqual(state["live"]["direction"], bot.live_signal.direction)
            self.assertIsNotNone(state["next_alert"])


class ExplainerTests(unittest.TestCase):
    def test_disabled_returns_none(self):
        from btcbot.ai import Explainer
        self.assertIsNone(Explainer(False).explain(analyze(make_candles(100))))

    def test_uses_model_text_and_falls_back(self):
        from types import SimpleNamespace
        from unittest import mock
        from btcbot.ai import Explainer
        ex = Explainer(False)
        import anthropic
        ex._anthropic = anthropic
        ex.client = mock.Mock()
        ex.client.beta.messages.create.return_value = SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text="Trend is up.")])
        sig = analyze(make_candles(100))
        self.assertEqual(ex.explain(sig), "Trend is up.")
        kwargs = ex.client.beta.messages.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "claude-opus-5-5")
        self.assertEqual(kwargs["fallbacks"], "default")
        cheap = Explainer(False, model="claude-haiku-5-5")
        cheap._anthropic, cheap.client = anthropic, mock.Mock()
        cheap.client.messages.create.return_value = SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Up.")])
        self.assertEqual(cheap.explain(sig), "Up.")
        self.assertNotIn("fallbacks", cheap.client.messages.create.call_args.kwargs)
        ex.client.beta.messages.create.side_effect = anthropic.APIConnectionError(request=mock.Mock())
        self.assertIsNone(ex.explain(sig))

    def test_explanation_in_message(self):
        sig = analyze(make_candles(120, drift=15, noise=10))
        slot = datetime(2026, 11, 11, 9, 30, tzinfo=ZoneInfo("UTC"))
        _, body, _ = format_signal(sig, slot, 15, (0, 0), "Buyers are in control.")
        self.assertIn("Why: Buyers are in control.", body)
        self.assertIn("Indicators:", body)


def ar_candles(n, phi, seed=1):
    """Synthetic market where each 15m return carries `phi` of the previous one."""
    rnd = random.Random(seed)
    p, r, out = 100_000.0, 0.0, []
    for i in range(n):
        r = phi * r + rnd.gauss(0, 60)
        o, c = p, p + r
        out.append(Candle(T0 + i * 900, o, max(o, c) + abs(rnd.gauss(0, 20)),
                          min(o, c) - abs(rnd.gauss(0, 20)), c, 10 + rnd.random()))
        p = c
    return out


class LearnTests(unittest.TestCase):
    def test_logistic_recovers_signal(self):
        from btcbot.learn import fit_logistic, predict_up
        rnd = random.Random(0)
        xs = [[rnd.uniform(-1, 1), rnd.uniform(-1, 1)] for _ in range(3000)]
        ys = [int(rnd.random() < 1 / (1 + math.exp(-(2 * a)))) for a, _ in xs]
        w = fit_logistic(xs, ys, l2=0.1)
        self.assertGreater(w[1], 1.5)
        self.assertLess(abs(w[2]), 0.3)
        self.assertGreater(predict_up(w, [1, 0]), 0.8)

    def test_calibration_is_monotonic_and_never_below_half(self):
        from btcbot.learn import calibrate
        rnd = random.Random(2)
        st = [rnd.uniform(0.5, 0.7) for _ in range(2000)]
        correct = [rnd.random() < s for s in st]
        table = calibrate(st, correct)
        accs = [r["acc"] for r in table]
        self.assertEqual(accs, sorted(accs))
        self.assertTrue(all(a >= 0.5 for a in accs))

    def test_random_walk_learns_no_edge(self):
        from btcbot.learn import train
        m = train(ar_candles(2000, 0.0), days=21)
        self.assertLess(m.stats["best_bucket"], 58)
        sig = m.apply(analyze(ar_candles(200, 0.0, seed=5)), 60)
        self.assertEqual(sig.direction, "SKIP")
        self.assertIsNotNone(sig.rule_confidence)

    def test_momentum_market_learns_an_edge(self):
        from btcbot.learn import train
        m = train(ar_candles(2900, 0.2), days=30)
        self.assertGreater(m.stats["oos_accuracy"], 52)
        self.assertGreater(m.stats["best_bucket"], 53)

    def test_bot_uses_model_when_ready(self):
        from btcbot.learn import train
        with tempfile.TemporaryDirectory() as d:
            bot = Bot(Config(data_dir=Path(d)))
            cs = ar_candles(2000, 0.2)
            plain = bot.score(cs[-200:])
            self.assertIsNone(plain.history)
            bot.model = train(cs, days=21)
            sig = bot.score(cs[-200:])
            self.assertIsNotNone(sig.history)
            _, body, _ = format_signal(sig, datetime(2026, 11, 11, 9, 0, tzinfo=ZoneInfo("UTC")), 15, (0, 0))
            if sig.history["n"]:
                self.assertIn("Track record:", body)


class ParserTests(unittest.TestCase):
    def _mock(self, payload):
        from unittest import mock
        resp = mock.Mock()
        resp.json.return_value = payload
        resp.raise_for_status.return_value = None
        return mock.patch("btcbot.data.requests.get", return_value=resp)

    def test_coinbase_format(self):
        from btcbot.data import fetch_coinbase
        # [time, low, high, open, close, volume], newest first
        rows = [[1800, 9, 12, 10, 11, 5.0], [900, 8, 11, 9, 10, 4.0]]
        with self._mock(rows):
            cs = fetch_coinbase()
        self.assertEqual([c.start for c in cs], [900, 1800])
        self.assertEqual((cs[1].open, cs[1].high, cs[1].low, cs[1].close), (10, 12, 9, 11))

    def test_kraken_format(self):
        from btcbot.data import fetch_kraken
        payload = {"error": [], "result": {"XXBTZUSD": [[900, "9", "11", "8", "10", "9.5", "4.0", 3]], "last": 900}}
        with self._mock(payload):
            cs = fetch_kraken()
        self.assertEqual((cs[0].open, cs[0].high, cs[0].low, cs[0].close, cs[0].volume), (9, 11, 8, 10, 4))
