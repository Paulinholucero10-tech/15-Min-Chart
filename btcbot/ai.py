"""Optional plain-English explanations of each signal, written by Claude.

The AI never makes the call: the UP / DOWN / SKIP decision and the confidence
come from the indicator engine in strategy.py. Claude only explains that
decision in a sentence or two so the alert is easier to read.

Enabled when ANTHROPIC_API_KEY is set (and AI_EXPLANATIONS is not "false").
Any failure (no key, timeout, network) falls back silently to the plain
indicator list, so an alert is never delayed or lost because of the AI.
"""
from __future__ import annotations

import logging

from .strategy import Signal

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"

SYSTEM = (
    "You explain short-term Bitcoin trading signals to a non-expert. A rule-based "
    "indicator engine has already decided the signal; do not change it, second-guess "
    "it, or give financial advice. In at most two short sentences (under 45 words), "
    "explain in plain English why the indicators point this way, mentioning the one "
    "or two strongest reasons. For a SKIP, explain why the picture is unclear. "
    "No greetings, no disclaimers, no markdown."
)


class Explainer:
    def __init__(self, enabled: bool, model: str = MODEL, timeout: float = 25.0):
        self.client = None
        self.model = model
        if not enabled:
            return
        try:
            import anthropic
        except ImportError:
            log.warning("AI explanations enabled but the 'anthropic' package is not installed")
            return
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(timeout=timeout, max_retries=1)

    @property
    def active(self) -> bool:
        return self.client is not None

    def explain(self, sig: Signal) -> str | None:
        if not self.client:
            return None
        anthropic = self._anthropic
        votes = ", ".join(f"{k}={v:+.2f}" for k, v in sig.votes.items())
        prompt = (
            f"Signal: {sig.direction} (leaning {sig.lean}), confidence {sig.confidence:.0f}%.\n"
            f"BTC price: ${sig.price:,.2f}. Horizon: the next 15-minute candle.\n"
            f"Indicator readings: {'; '.join(sig.reasons)}.\n"
            f"Indicator votes (-1 bearish .. +1 bullish): {votes}."
        )
        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=2000,
                system=SYSTEM,
                output_config={"effort": "low"},
                # If the request is ever declined, retry on a fallback model automatically
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.AuthenticationError:
            log.error("AI explanation disabled: invalid ANTHROPIC_API_KEY")
            self.client = None
            return None
        except anthropic.RateLimitError:
            log.warning("AI explanation skipped: rate limited")
            return None
        except anthropic.APIStatusError as exc:
            log.warning("AI explanation failed (%s): %s", exc.status_code, exc.message)
            return None
        except anthropic.APIConnectionError as exc:
            log.warning("AI explanation failed (network): %s", exc)
            return None
        if resp.stop_reason == "refusal":
            return None
        text = " ".join(b.text for b in resp.content if b.type == "text").strip()
        return text or None
