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

    def test_downtrend_says_down(self):
        sig = analyze(make_candles(120, drift=-15, noise=10))
        self.assertEqual(sig.direction, "DOWN")

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

    def test_sixteen_slots_8_to_12(self):
        day = datetime(2026, 11, 11, 3, 0, tzinfo=self.cfg.timezone)
        slots = slots_for_day(self.cfg, day)
        self.assertEqual(len(slots), 16)
        self.assertEqual((slots[0].hour, slots[0].minute), (8, 0))
        self.assertEqual((slots[-1].hour, slots[-1].minute), (11, 45))

    def test_due_slot(self):
        with tempfile.TemporaryDirectory() as d:
            self.cfg.data_dir = Path(d)
            bot = Bot(self.cfg)
            tz = self.cfg.timezone
            self.assertIsNone(bot.due_slot(datetime(2026, 11, 11, 7, 59, tzinfo=tz)))
            self.assertEqual(bot.due_slot(datetime(2026, 11, 11, 8, 15, 10, tzinfo=tz)).minute, 15)
            self.assertIsNone(bot.due_slot(datetime(2026, 11, 11, 12, 1, tzinfo=tz)))
            self.assertIsNone(bot.due_slot(datetime(2026, 11, 11, 8, 10, tzinfo=tz)))  # stale

    def test_weekdays_only(self):
        with tempfile.TemporaryDirectory() as d:
            self.cfg.data_dir = Path(d)
            self.cfg.weekdays_only = True
            bot = Bot(self.cfg)
            sat = datetime(2026, 11, 14, 8, 0, 30, tzinfo=self.cfg.timezone)
            self.assertIsNone(bot.due_slot(sat))

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
