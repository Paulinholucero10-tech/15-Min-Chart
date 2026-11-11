"""Send alerts to Telegram, ntfy (phone push, no account) and/or Discord. Always logs to console."""
from __future__ import annotations

import logging

import requests

from .config import Config

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    @property
    def channels(self) -> list[str]:
        out = []
        if self.cfg.telegram_token and self.cfg.telegram_chat_id:
            out.append("telegram")
        if self.cfg.ntfy_topic:
            out.append("ntfy")
        if self.cfg.discord_webhook:
            out.append("discord")
        return out

    def send(self, title: str, body: str, priority: str = "default") -> None:
        print(f"\n===== {title} =====\n{body}\n", flush=True)
        for ch in self.channels:
            try:
                getattr(self, f"_send_{ch}")(title, body, priority)
            except Exception as exc:
                log.error("Failed to send via %s: %s", ch, exc)

    def _send_telegram(self, title: str, body: str, priority: str) -> None:
        r = requests.post(
            f"https://api.telegram.org/bot{self.cfg.telegram_token}/sendMessage",
            json={"chat_id": self.cfg.telegram_chat_id, "text": f"{title}\n\n{body}"},
            timeout=15,
        )
        r.raise_for_status()

    def _send_ntfy(self, title: str, body: str, priority: str) -> None:
        r = requests.post(
            f"{self.cfg.ntfy_server}/{self.cfg.ntfy_topic}",
            data=body.encode("utf-8"),
            headers={"Title": title.encode("utf-8"), "Priority": priority},
            timeout=15,
        )
        r.raise_for_status()

    def _send_discord(self, title: str, body: str, priority: str) -> None:
        r = requests.post(self.cfg.discord_webhook, json={"content": f"**{title}**\n{body}"}, timeout=15)
        r.raise_for_status()
