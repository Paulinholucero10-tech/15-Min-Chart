# 15-Min-Chart: BTC Alert Bot

Watches the Bitcoin chart (15-minute candles) **24/7**. Every **15 minutes from 8:00 AM to 2:00 PM**
it sends your phone an alert:

```
🟢⬆️ BTC 9:15 AM: GO UP (71%)
Price: $97,412.55
Candle: 9:15 AM - 9:30 AM
Confidence: 71%
Why: Price has been climbing steadily for six hours and the last few candles closed strong on higher volume.
Track record: signals like this were right 187/331 times in the last 30 days
Indicators: 6h trend t=+3.4; EMA9 above EMA21; MACD hist +rising; RSI 62; 3-candle move +180 (vol x1.3); price above 12h trend
Today so far: 3/4 correct
```

- **GO UP / GO DOWN**: the bot thinks the next 15-minute candle will close above or below the current price,
  and its confidence is at or above `MIN_CONFIDENCE` (default 60%).
- **SKIP**: the signals are mixed or weak, so it says which way it leans and tells you to sit out.
- **Live dashboard** in your browser (http://localhost:8080): candlestick chart with today's calls marked,
  what the bot thinks right now, today's and all-time hit rate, and accuracy by confidence level.
- **Plain-English explanations (optional)**: with an Anthropic API key, Claude writes the "Why" line in
  each alert. The AI only explains; the UP/DOWN/SKIP call always comes from the indicators below.
- At about 2:00 PM you get a **daily scorecard** with every call and whether it was right. All calls are
  saved in `data/predictions.jsonl`, so you can see the real hit rate over time.

> ⚠️ **Read this first.** No bot can reliably predict a 15-minute Bitcoin candle. Prices that short-term
> are close to random. This bot uses standard technical indicators and is calibrated so that random noise
> mostly gives SKIP, but a 70% confidence is the model's own estimate, not a guarantee. Run
> `python -m btcbot backtest` and watch the scorecards for a few weeks before you trade real money.
> This is not financial advice.

## How it gets its confidence (it learns from the chart)

A fixed formula can't know whether Bitcoin is currently trending or chopping back and forth, so the
bot checks itself against recent history (on by default, `ADAPTIVE=true`):

1. Every 6 hours it downloads the last 30 days of 15-minute candles (~2,900).
2. For every one of those candles it computes the same 7 indicators and records whether the next
   candle went up or down.
3. It **learns how much to trust each indicator** right now (a small logistic regression). In a
   choppy market it learns that trend-following works backwards.
4. It **tests itself only on data it didn't train on** (walk-forward), and compares against a naive
   guess. These are the numbers on the dashboard, and from `python -m btcbot learn`.
5. The confidence in each alert is **how often similar signals were actually right** in that test
   ("Track record: right 187/331 times"), pulled toward 50% when there are few cases.

So if the last month gave the indicators no real edge, the bot says SKIP instead of pretending.
Expect honest numbers in the 50-60% range: that's what short-term Bitcoin really offers. If you see
mostly SKIPs, that is the bot telling the truth. You can lower `MIN_CONFIDENCE` to 55 to get more
calls, knowing each one is only slightly better than a coin flip.

Ideas borrowed from other open-source projects: replaying every past time a setup fired
(day-trader-skill's Time Machine), retraining on the latest data and grading its own predictions
(coinpusher), and always comparing against a naive baseline (btc-lstm-prediction).

## How it decides

On every 15-minute mark it looks at the last ~50 hours of closed 15m candles (Coinbase, falling back
to Kraken; no API key needed) and scores 7 indicators from -1 (bearish) to +1 (bullish):

| Indicator | Weight | What it measures |
|---|---|---|
| 6h trend t-stat | 25% | Is the recent drift statistically real, or just noise? |
| EMA 9 vs 21 | 17% | Short-term trend |
| Price vs EMA 50 | 15% | ~12-hour trend |
| MACD histogram | 13% | Momentum, and whether it is speeding up |
| 3-candle momentum | 12% | Last 45 min move, weighted by volume |
| RSI 14 | 10% | Momentum, fades overbought/oversold extremes |
| Last candle close | 8% | Did it close near its high (buyers) or low (sellers)? |

With learning off (or before it has finished), the weighted score becomes a confidence from 50%
(coin flip) to 85% (cap). If the indicators disagree,
confidence drops. After a sudden volatility spike, the bot always says SKIP.

## Setup (about 5 minutes)

1. **Get Python 3.10+**, then:
   ```bash
   git clone <this repo> && cd 15-Min-Chart
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   ```
2. **Pick how you want to be notified** and fill it in `.env`:
   - **ntfy (easiest)**: install the free *ntfy* app (iOS/Android), tap *Subscribe*, and choose a
     hard-to-guess topic such as `btc-alerts-7f3k9q`. Put the same name in `NTFY_TOPIC`.
   - **Telegram**: message `@BotFather` → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
     Send your new bot any message, then get your chat id from `@userinfobot` → `TELEGRAM_CHAT_ID`.
   - **Discord**: Channel settings → Integrations → Webhooks → copy URL → `DISCORD_WEBHOOK_URL`.
3. *(Optional)* **AI explanations**: put an Anthropic API key (from console.anthropic.com) in
   `ANTHROPIC_API_KEY`. It costs about a cent or two a day. Without a key, alerts list the indicators instead.
4. **Set your timezone** in `.env` (`TIMEZONE=America/Chicago`, `Europe/London`, `America/Sao_Paulo`, ...).
   8 AM to 2 PM is measured in this timezone.
5. **Test it:**
   ```bash
   python -m btcbot test-notify   # you should get a message on your phone
   python -m btcbot learn         # learn from the last 30 days and show the honest track record
   python -m btcbot now           # see what the bot says right now
   python -m btcbot backtest --days 60   # how accurate would it have been?
   ```
6. **Run it 24/7:**
   ```bash
   python -m btcbot run
   ```
   Then open **http://localhost:8080** for the dashboard. To open it from your phone on the same Wi-Fi,
   set `DASHBOARD_HOST=0.0.0.0` and visit `http://<your-computer's-IP>:8080`. Don't expose it to the
   open internet.

## Keeping it running 24/7

The bot has to run on a computer that is always on and online:

- **Docker** (any server or VPS): `docker compose up -d --build`. It restarts automatically, and the
  dashboard is on port 8080 of that machine (only reachable from the machine itself by default; use an
  SSH tunnel `ssh -L 8080:localhost:8080 you@server` to view it).
- **Raspberry Pi / Linux server**: use `deploy/btcbot.service` with systemd (instructions are in the file).
- **Your own PC**: works, but alerts stop when the computer sleeps.
- Cheap cloud options: any $4-6/month VPS (DigitalOcean, Hetzner, Lightsail) or a container host such
  as Railway or Fly.io running the Dockerfile.

## Tuning

| Setting | Default | Effect |
|---|---|---|
| `MIN_CONFIDENCE` | 60 | Raise to 65-70 for fewer but stronger calls (more SKIPs) |
| `WINDOW_START` / `WINDOW_END` | 08:00 / 14:00 | Alert window (end is exclusive) |
| `INTERVAL_MINUTES` | 15 | Minutes between alerts |
| `WEEKDAYS_ONLY` | false | Skip weekends |
| `ADAPTIVE` | true | Learn from recent history and use its real hit rate as the confidence |
| `LEARN_DAYS` / `LEARN_EVERY_HOURS` | 30 / 6 | How much history to learn from, and how often to re-learn |
| `ANTHROPIC_API_KEY` | empty | Turns on AI-written explanations |
| `DASHBOARD` / `DASHBOARD_PORT` / `DASHBOARD_HOST` | true / 8080 / 127.0.0.1 | Web dashboard |

Use the backtest to choose `MIN_CONFIDENCE`. It prints accuracy for each confidence bucket. If higher
confidence does not mean higher accuracy on recent data, treat the alerts as noise.

## Development

```bash
python -m unittest -v
```
