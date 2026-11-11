"""CLI entry point.

  python -m btcbot run           # run 24/7, send alerts in the window, serve the dashboard
  python -m btcbot now           # print the signal for the next candle right now
  python -m btcbot test-notify   # send a test message to your configured channels
  python -m btcbot learn         # learn from recent history and show the honest track record
  python -m btcbot backtest      # check how accurate the fixed rules were historically
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime

from .bot import Bot, format_signal
from .config import Config
from .notifier import Notifier


def main() -> None:
    p = argparse.ArgumentParser(prog="btcbot", description="BTC 15-minute UP/DOWN/SKIP alert bot")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("now")
    sub.add_parser("test-notify")
    lp = sub.add_parser("learn")
    lp.add_argument("--days", type=int, default=None)
    bt = sub.add_parser("backtest")
    bt.add_argument("--days", type=int, default=30)
    bt.add_argument("--csv", help="use a local CSV (start,open,high,low,close,volume) instead of downloading")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.from_env()

    if args.cmd == "run":
        Bot(cfg).run()
    elif args.cmd == "now":
        bot = Bot(cfg)
        if cfg.adaptive:
            try:
                bot.learn()
            except Exception as exc:
                print(f"(Could not learn from history: {exc}; using fixed rules)")
        sig = bot.signal_now()
        slot = datetime.fromtimestamp(sig.candle_start, cfg.timezone)
        title, body, _ = format_signal(sig, slot, cfg.interval_minutes, (0, 0), bot.explainer.explain(sig))
        print(f"{title}\n{body}\nVotes: {sig.votes}")
    elif args.cmd == "learn":
        from .learn import report
        if args.days:
            cfg.learn_days = args.days
        print(f"Downloading {cfg.learn_days} days of 15m candles and learning...")
        model = Bot(cfg).learn()
        print(report(model, cfg.min_confidence))
    elif args.cmd == "test-notify":
        n = Notifier(cfg)
        if not n.channels:
            print("No channels configured - set NTFY_TOPIC, TELEGRAM_* or DISCORD_WEBHOOK_URL in .env")
        n.send("✅ BTC bot test", "If you can read this on your phone, notifications work.")
    elif args.cmd == "backtest":
        from .backtest import run_backtest
        run_backtest(cfg, days=args.days, csv_path=args.csv)


if __name__ == "__main__":
    main()
