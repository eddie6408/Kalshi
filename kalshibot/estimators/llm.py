"""Claude-powered estimator - the rendition-2 hook.

This is deliberately NOT wired into config.yaml yet. It exists so you can see
exactly where an LLM fits: it produces a probability and a confidence, and
nothing else. It never sees your balance, never picks a size, and never
places an order. The deterministic risk engine still has the final say.

To turn it on later:
  1. pip install anthropic
  2. put ANTHROPIC_API_KEY in .env
  3. register it in kalshibot/estimators/registry.py ESTIMATORS
  4. add it to forecast.estimators in config.yaml

Two hard rules if you do:
  * Force JSON output and parse it. Never act on prose.
  * Log the full prompt and response in the journal so a bad estimate can be
    traced back to the information that produced it.
"""
from __future__ import annotations

import json
import logging
import os
import re

from ..models import Market
from .base import Estimate

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a forecasting analyst pricing a prediction market contract.
You will be given a market question and a set of gathered evidence.

Return ONLY a JSON object, no prose, in exactly this shape:
{
  "probability_yes": <number between 0 and 1>,
  "confidence": <number between 0 and 1>,
  "key_drivers": ["<short phrase>", ...],
  "what_would_change_my_mind": "<one sentence>"
}

Rules:
- probability_yes is your honest belief the contract resolves YES.
- confidence reflects the QUALITY AND RECENCY of the evidence, not how strong
  your opinion feels. Thin or stale evidence means low confidence, even if
  your probability is far from 50%.
- Do not anchor on the market price. If the price is shown, treat it as one
  weak piece of evidence among many.
- If the evidence does not let you price this, return confidence 0.0.
"""


class ClaudeEstimator:
    name = "claude_llm"

    def __init__(self, cfg: dict, evidence_fn=None, model: str = "claude-sonnet-4-6",
                 api_key: str | None = None):
        self.cfg = cfg or {}
        self.evidence_fn = evidence_fn or (lambda m: {})
        self.model = model
        self._key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self._client = None

    def candidate_series(self) -> list[str]:
        return list(self.cfg.get("series_tickers", []))

    def _client_or_none(self):
        if self._client is not None:
            return self._client
        if not self._key:
            return None
        try:
            import anthropic
        except ImportError:
            log.error("pip install anthropic to use ClaudeEstimator")
            return None
        self._client = anthropic.Anthropic(api_key=self._key)
        return self._client

    def estimate(self, market: Market) -> Estimate:
        client = self._client_or_none()
        if client is None:
            return Estimate.skip("Anthropic client unavailable", self.name)

        evidence = self.evidence_fn(market)
        user = json.dumps({
            "market_question": market.title or market.subtitle,
            "ticker": market.ticker,
            "closes_at_utc": market.close_time.isoformat() if market.close_time else None,
            "evidence": evidence,
        }, indent=2, default=str)

        try:
            resp = client.messages.create(
                model=self.model,
                max_tokens=800,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        except Exception as exc:  # noqa: BLE001
            log.error("Claude call failed: %s", exc)
            return Estimate.skip(f"LLM error: {exc}", self.name)

        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return Estimate.skip("LLM did not return JSON", self.name)
        try:
            data = json.loads(m.group(0))
            p = float(data["probability_yes"])
            c = float(data.get("confidence", 0.0))
        except (ValueError, KeyError) as exc:
            return Estimate.skip(f"unparseable LLM output: {exc}", self.name)

        if not (0.0 <= p <= 1.0) or not (0.0 <= c <= 1.0):
            return Estimate.skip("LLM returned out-of-range values", self.name)

        return Estimate(probability_yes=p, confidence=c, source=f"{self.name}:{self.model}",
                        evidence={"llm_output": data, "inputs": evidence})
