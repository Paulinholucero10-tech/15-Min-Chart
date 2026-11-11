"""Configuration loaded from environment variables (and an optional .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo


def load_dotenv(path: str | os.PathLike = ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines, '#' comments. Existing env vars win."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _parse_time(s: str) -> time:
    hh, _, mm = s.strip().partition(":")
    return time(int(hh), int(mm or 0))


def _bool(s: str) -> bool:
    return s.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("America/New_York"))
    window_start: time = time(8, 0)
    window_end: time = time(12, 0)  # exclusive: last alert is at 11:45 for the 11:45-12:00 candle
    interval_minutes: int = 15
    min_confidence: float = 60.0  # below this -> SKIP
    weekdays_only: bool = False
    product: str = "BTC-USD"
    poll_seconds: int = 60
    data_dir: Path = Path("data")

    telegram_token: str = ""
    telegram_chat_id: str = ""
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    discord_webhook: str = ""

    adaptive: bool = True          # learn from recent history and calibrate confidence
    learn_days: int = 30
    learn_every_hours: float = 6.0

    ai_explanations: bool = False
    dashboard: bool = True
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        e = os.environ.get
        return cls(
            timezone=ZoneInfo(e("TIMEZONE", "America/New_York")),
            window_start=_parse_time(e("WINDOW_START", "08:00")),
            window_end=_parse_time(e("WINDOW_END", "12:00")),
            interval_minutes=int(e("INTERVAL_MINUTES", "15")),
            min_confidence=float(e("MIN_CONFIDENCE", "60")),
            weekdays_only=_bool(e("WEEKDAYS_ONLY", "false")),
            product=e("PRODUCT", "BTC-USD"),
            poll_seconds=int(e("POLL_SECONDS", "60")),
            data_dir=Path(e("DATA_DIR", "data")),
            telegram_token=e("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=e("TELEGRAM_CHAT_ID", ""),
            ntfy_topic=e("NTFY_TOPIC", ""),
            ntfy_server=e("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
            discord_webhook=e("DISCORD_WEBHOOK_URL", ""),
            adaptive=_bool(e("ADAPTIVE", "true")),
            learn_days=int(e("LEARN_DAYS", "30")),
            learn_every_hours=float(e("LEARN_EVERY_HOURS", "6")),
            ai_explanations=bool(e("ANTHROPIC_API_KEY")) and _bool(e("AI_EXPLANATIONS", "true")),
            dashboard=_bool(e("DASHBOARD", "true")),
            dashboard_host=e("DASHBOARD_HOST", "127.0.0.1"),
            dashboard_port=int(e("DASHBOARD_PORT", "8080")),
        )
