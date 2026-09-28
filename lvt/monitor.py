"""Degradation monitor and stop-trading conditions (V2 brief sections 67, 74).

States: OK -> DEGRADED (keep trading, alert, visible on the dashboard) ->
SUSPENDED (new entries blocked via the fail-closed flags; exits keep
running). Every transition writes an incident snapshot to data/incidents/
so the data around it is preserved for analysis.

SUSPEND (fail closed):
  * DATA_CORRUPTION   crossed / out-of-range quotes in a large share of a cycle
  * MODEL_MALFUNCTION the reference model produced impossible values
  * API_INSTABILITY   request error rate > 20% over 5 minutes
DEGRADE (alert, continue):
  * SLOW_CYCLES       5 consecutive cycles over the latency budget
  * FILL_COLLAPSE     maker entry fill rate < 15% over the last 50 entries
  * PERSISTENT_LOSSES 20 consecutive losing trades in one account
  * DRIFT             spread / fill rate / score age far from their 24 h norms
Loss-based conditions never suspend paper research accounts (the user
removed paper limits deliberately); they alert and mark DEGRADED.
"""
from __future__ import annotations

import json
import logging
import time
from collections import deque
from pathlib import Path

log = logging.getLogger("lvt.monitor")


def corrupt_quotes(quotes: dict) -> list[str]:
    bad = []
    for t, q in quotes.items():
        b, a = q.yes_bid, q.yes_ask
        if (b is not None and not 0 <= b <= 100) or (a is not None and not 0 <= a <= 100) or \
                (b is not None and a is not None and b >= a):
            bad.append(t)
    return bad


class Monitor:
    def __init__(self, cfg, db, alerter, data_dir: Path):
        self.cfg = cfg
        self.db = db
        self.alerter = alerter
        self.incident_dir = Path(data_dir) / "incidents"
        self.state = "OK"
        self.reasons: dict[str, str] = {}
        self.req_window: deque = deque()               # (ts, requests, errors)
        self.slow_cycles = 0
        self.last_drift: dict = {}

    # -- per cycle ------------------------------------------------------------------
    def on_quotes(self, quotes: dict, engines) -> dict:
        """Drops corrupt quotes; suspends when a large share is corrupt."""
        bad = corrupt_quotes(quotes)
        share = len(bad) / len(quotes) if quotes else 0.0
        if bad:
            self.alerter.fire("DATA_CORRUPTION", f"{len(bad)} crossed/out-of-range quotes dropped (e.g. {bad[0]})",
                              "WARNING", key="quotes")
        self._flag(engines, "DATA_CORRUPTION", share > 0.1, f"{share:.0%} of quotes corrupt this cycle")
        return {t: q for t, q in quotes.items() if t not in bad}

    def on_cycle(self, cycle_ms: float) -> None:
        budget = float(self.cfg.discovery.quote_interval_s) * 1000 * 0.75
        self.slow_cycles = self.slow_cycles + 1 if cycle_ms > budget else 0
        self._degrade("SLOW_CYCLES", self.slow_cycles >= 5, f"{self.slow_cycles} cycles over {budget:.0f} ms")

    # -- periodic ----------------------------------------------------------------------
    def periodic(self, now: float, http_stats, engines, edge=None) -> None:
        self.req_window.append((now, http_stats.requests, http_stats.errors))
        while self.req_window and now - self.req_window[0][0] > 300:
            self.req_window.popleft()
        t0, r0, e0 = self.req_window[0]
        dr, de = http_stats.requests - r0, http_stats.errors - e0
        rate = de / dr if dr else 0.0
        self._flag(engines, "API_INSTABILITY", dr >= 20 and rate > 0.2, f"error rate {rate:.0%} over {dr} requests (5 min)")

        if edge is not None:
            bad = getattr(edge, "malfunctions", 0)
            self._flag([e for e in engines if e.edge is not None], "MODEL_MALFUNCTION", bad > 0,
                       f"{bad} impossible reference values")
            edge.malfunctions = 0

        for e in engines:
            rows = self.db.query("SELECT status FROM orders WHERE env=? AND purpose='ENTRY' ORDER BY created_ts DESC LIMIT 50", (e.env,))
            done = [r for r in rows if r["status"] in ("FILLED", "PARTIAL", "NO_FILL", "CANCELLED")]
            fr = sum(1 for r in done if r["status"] in ("FILLED", "PARTIAL")) / len(done) if len(done) >= 30 else None
            self._degrade(f"FILL_COLLAPSE:{e.env}", fr is not None and fr < 0.15, f"{e.env} maker fill rate {fr}")
            pnl = [r["realized_pnl_usd"] for r in self.db.query(
                "SELECT realized_pnl_usd FROM positions WHERE env=? AND status='CLOSED' ORDER BY closed_ts DESC LIMIT 20", (e.env,))]
            streak = len(pnl) == 20 and all(x <= 0 for x in pnl)
            self._degrade(f"PERSISTENT_LOSSES:{e.env}", streak, f"{e.env}: 20 consecutive losing trades")
        self._drift(now, engines)

    def _drift(self, now: float, engines) -> None:
        def window(a, b):
            r = self.db.one("SELECT AVG(spread) sp, AVG(score_age_s) age, COUNT(*) n FROM dislocations WHERE ts>=? AND ts<?", (a, b))
            f = self.db.query("SELECT status FROM orders WHERE purpose='ENTRY' AND created_ts>=? AND created_ts<?", (a, b))
            done = [x for x in f if x["status"] in ("FILLED", "PARTIAL", "NO_FILL", "CANCELLED")]
            fill = sum(1 for x in done if x["status"] in ("FILLED", "PARTIAL")) / len(done) if len(done) >= 20 else None
            return {"spread": r["sp"] if r else None, "score_age": r["age"] if r else None, "fill_rate": fill,
                    "n": r["n"] if r else 0}
        recent, norm = window(now - 3600, now), window(now - 86400, now - 3600)
        drift = {}
        if recent["n"] >= 50 and norm["n"] >= 200:
            for k in ("spread", "score_age"):
                if recent[k] and norm[k]:
                    drift[k] = round(recent[k] / norm[k], 2)
        if recent["fill_rate"] is not None and norm["fill_rate"]:
            drift["fill_rate"] = round(recent["fill_rate"] / norm["fill_rate"], 2)
        self.last_drift = drift
        bad = [k for k, v in drift.items() if (k == "fill_rate" and v < 0.5) or (k != "fill_rate" and v > 2.0)]
        self._degrade("DRIFT", bool(bad), f"last hour vs 24 h: {drift}")

    # -- state ------------------------------------------------------------------------
    def _flag(self, engines, code: str, on: bool, detail: str) -> None:
        for e in engines:
            if on:
                e.risk.fail_closed[code] = detail
            else:
                e.risk.fail_closed.pop(code, None)
        self._set(code, on, detail, suspend=True)

    def _degrade(self, code: str, on: bool, detail: str) -> None:
        self._set(code, on, detail, suspend=False)

    def _set(self, code: str, on: bool, detail: str, suspend: bool) -> None:
        was = self.state
        if on:
            new = code not in self.reasons
            self.reasons[code] = ("SUSPEND: " if suspend else "DEGRADED: ") + detail
            if new:
                self.alerter.fire("DEGRADED" if not suspend else "SUSPENDED", f"{code}: {detail}",
                                  "CRITICAL" if suspend else "WARNING", key=code)
        else:
            self.reasons.pop(code, None)
        self.state = ("SUSPENDED" if any(v.startswith("SUSPEND") for v in self.reasons.values())
                      else "DEGRADED" if self.reasons else "OK")
        if self.state != was:
            self._snapshot(was, code)

    def _snapshot(self, was: str, code: str) -> None:
        try:
            self.incident_dir.mkdir(parents=True, exist_ok=True)
            now = time.time()
            snap = {"ts": now, "from": was, "to": self.state, "trigger": code, "reasons": self.reasons,
                    "drift": self.last_drift,
                    "status": self.db.query("SELECT * FROM status"),
                    "alerts": self.db.query("SELECT * FROM alerts ORDER BY ts DESC LIMIT 50"),
                    "health": self.db.query("SELECT * FROM health ORDER BY ts DESC LIMIT 30"),
                    "orders": self.db.query("SELECT * FROM orders ORDER BY created_ts DESC LIMIT 100"),
                    "signals": self.db.query("SELECT id, ts, env, ticker, strategy, action, decision, score FROM signals "
                                             "ORDER BY ts DESC LIMIT 200")}
            path = self.incident_dir / f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime(now))}-{self.state}-{code.split(':')[0]}.json"
            path.write_text(json.dumps(snap, default=str))
            log.warning("monitor %s -> %s (%s); snapshot %s", was, self.state, code, path.name)
        except Exception as exc:                          # noqa: BLE001
            log.error("incident snapshot failed: %s", exc)

    def status(self) -> dict:
        return {"state": self.state, "reasons": dict(self.reasons), "drift": self.last_drift}
