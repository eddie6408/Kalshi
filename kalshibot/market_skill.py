"""Does the model beat the MARKET, where it trades?

The calibration gate asks whether a forecaster beats climatology - the naive
base rate. That is the minimum bar for knowing anything about the weather.
It is not the bar for making money. A trade pays only if our probability is
closer to the outcome than the price we trade against, and on 26 September,
in the band the bot actually trades, it was not:

    lead     ours    market
    0-6h     0.126   0.097      the market was better
    6-12h    0.129   0.072      the market was much better
    12-24h   0.160   0.161      even

...while the calibration gate reported CALIBRATED, because 0.126 beats
climatology comfortably. The weather strategy had lost $144 by then.

So this compares the two directly, per (city, lead bucket):

    diff = our squared error - the market's squared error

averaged per CONTRACT first (a contract re-priced six times a day is one fact,
not six) and then across contracts, with a standard error across contracts.

    BEHIND    diff > margin and significant
    AHEAD     diff < -margin and significant
    EVEN      neither
    UNPROVEN  fewer than `min_contracts` contracts

`require` decides what may enter:

    ahead       only a cell that has shown it beats the market (default).
                "A strategy should have to earn the right to trade." A model
                that merely ties the market loses the fees and the spread.
    not_behind  anything not significantly worse than the market.

A city with too little data of its own inherits the verdict of all cities
pooled at the same lead time. Exits are never gated.

The market's probability is the mid of bid and ask when both were recorded,
otherwise the ask. The ask overstates YES, and most temperature buckets
resolve NO, so the fallback mostly flatters US, not the market - which makes
a BEHIND verdict conservative. `mid_share` reports how much of a cell had a
true mid.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger(__name__)

BUCKETS = ((0.0, 6.0, "0-6h"), (6.0, 12.0, "6-12h"),
           (12.0, 24.0, "12-24h"), (24.0, math.inf, "24h+"))
POOLED = "ALL"

AHEAD, EVEN, BEHIND, UNPROVEN = "AHEAD", "EVEN", "BEHIND", "UNPROVEN"


def bucket_of(lead_hours: float | None) -> str | None:
    if lead_hours is None:
        return None
    for lo, hi, name in BUCKETS:
        if lo <= float(lead_hours) < hi:
            return name
    return None


@dataclass
class MarketSkillConfig:
    enabled: bool = True
    #: "ahead" (must beat the market) or "not_behind" (must not lose to it)
    require: str = "ahead"
    #: forecasts made before this are ignored (a different forecaster)
    since: str = ""
    #: contracts a cell needs before it can be judged on its own
    min_contracts: int = 15
    #: how far behind (Brier points) before it counts, and how sure
    margin: float = 0.005
    z: float = 2.0
    refresh_seconds: float = 300.0

    @classmethod
    def from_dict(cls, d: dict | None) -> "MarketSkillConfig":
        d = d or {}
        return cls(
            enabled=bool(d.get("enabled", True)),
            require=_require(d.get("require", "ahead")),
            since=str(d.get("since", "") or ""),
            min_contracts=int(d.get("min_contracts", 15)),
            margin=float(d.get("margin", 0.005)),
            z=float(d.get("z", 2.0)),
            refresh_seconds=float(d.get("refresh_seconds", 300.0)),
        )


@dataclass
class Cell:
    series: str
    bucket: str
    rows: int
    contracts: int
    our_brier: float
    market_brier: float
    diff: float                 # ours - market; positive = market better
    se: float | None
    mid_share: float
    verdict: str

    @property
    def zscore(self) -> float | None:
        if self.se is None or self.se == 0:
            return None
        return self.diff / self.se

    def describe(self) -> str:
        z = self.zscore
        return (f"{self.series} {self.bucket}: {self.verdict} - ours "
                f"{self.our_brier:.4f} vs market {self.market_brier:.4f} over "
                f"{self.contracts} contracts"
                + (f", z={z:+.1f}" if z is not None else "")
                + (f", {self.mid_share:.0%} with a true mid" if self.mid_share < 1 else ""))


def market_probability(ask, bid) -> float | None:
    if ask is None:
        return None
    if bid is not None:
        return (float(ask) + float(bid)) / 200.0
    return float(ask) / 100.0


def fetch_rows(conn, estimator: str | None = None, since: str = "") -> list[dict]:
    """Settled forecasts that have a market price to compare against."""
    where = ["settled_yes IS NOT NULL", "market_yes_cents IS NOT NULL",
             "lead_hours IS NOT NULL"]
    args: list = []
    if estimator:
        where.append("estimator = ?"); args.append(estimator)
    if since:
        where.append("ts >= ?"); args.append(since)
    sql = ("SELECT series, ticker, lead_hours, our_probability, settled_yes, "
           "market_yes_cents, market_yes_bid_cents FROM probability_log WHERE "
           + " AND ".join(where))
    out = []
    for r in conn.execute(sql, args):
        r = dict(r) if not isinstance(r, dict) else r
        out.append(r)
    return out


def _judge(diffs_by_contract: dict[str, list[float]], rows: int, ours: list[float],
           mkt: list[float], mids: int, series: str, bucket: str,
           cfg: MarketSkillConfig) -> Cell:
    per_contract = [sum(v) / len(v) for v in diffs_by_contract.values()]
    k = len(per_contract)
    mean = sum(per_contract) / k if k else 0.0
    se = None
    if k >= 2:
        var = sum((x - mean) ** 2 for x in per_contract) / (k - 1)
        se = math.sqrt(var / k)
    if k < cfg.min_contracts:
        verdict = UNPROVEN
    else:
        z = (mean / se) if se else (math.inf if mean > 0 else -math.inf if mean < 0 else 0)
        if mean > cfg.margin and z >= cfg.z:
            verdict = BEHIND
        elif mean < -cfg.margin and z <= -cfg.z:
            verdict = AHEAD
        else:
            verdict = EVEN
    return Cell(series, bucket, rows, k,
                sum(ours) / len(ours) if ours else 0.0,
                sum(mkt) / len(mkt) if mkt else 0.0,
                mean, se, (mids / rows) if rows else 0.0, verdict)


def compute_cells(rows: list[dict], cfg: MarketSkillConfig | None = None
                  ) -> dict[tuple[str, str], Cell]:
    """Every (series, bucket) cell, plus (ALL, bucket) pooled across series."""
    cfg = cfg or MarketSkillConfig()
    acc: dict[tuple[str, str], dict] = {}
    for r in rows:
        b = bucket_of(r.get("lead_hours"))
        p_mkt = market_probability(r.get("market_yes_cents"), r.get("market_yes_bid_cents"))
        if b is None or p_mkt is None or r.get("our_probability") is None:
            continue
        y = float(r["settled_yes"])
        e_ours = (float(r["our_probability"]) - y) ** 2
        e_mkt = (p_mkt - y) ** 2
        for key in ((r.get("series") or "?", b), (POOLED, b)):
            a = acc.setdefault(key, {"d": {}, "rows": 0, "ours": [], "mkt": [], "mids": 0})
            a["d"].setdefault(r["ticker"], []).append(e_ours - e_mkt)
            a["rows"] += 1
            a["ours"].append(e_ours)
            a["mkt"].append(e_mkt)
            a["mids"] += 1 if r.get("market_yes_bid_cents") is not None else 0
    return {key: _judge(a["d"], a["rows"], a["ours"], a["mkt"], a["mids"],
                        key[0], key[1], cfg)
            for key, a in acc.items()}


def _require(value) -> str:
    v = str(value or "ahead").lower()
    if v not in ("ahead", "not_behind"):
        raise ValueError(f"market_skill.require must be 'ahead' or 'not_behind', got {v!r}")
    return v


class MarketSkillGate:
    """Refreshes the cells from the journal and answers may_enter()."""

    def __init__(self, journal, cfg: MarketSkillConfig | None = None,
                 estimator: str | None = None):
        self.journal = journal
        self.cfg = cfg or MarketSkillConfig()
        self.estimator = estimator
        self._cells: dict[tuple[str, str], Cell] = {}
        self._at: datetime | None = None

    def cells(self, force: bool = False) -> dict[tuple[str, str], Cell]:
        now = datetime.now(timezone.utc)
        if (not force and self._at is not None
                and (now - self._at).total_seconds() < self.cfg.refresh_seconds):
            return self._cells
        try:
            rows = fetch_rows(self.journal.conn, self.estimator, self.cfg.since)
            self._cells = compute_cells(rows, self.cfg)
        except Exception as exc:  # noqa: BLE001
            # A read that fails must not stop trading, and must not pretend.
            log.error("MARKET SKILL  could not read scores: %s", exc)
            self._cells = {}
        self._at = now
        for c in sorted(self._cells.values(), key=lambda c: (c.bucket, c.series)):
            if c.verdict in (BEHIND, AHEAD) or c.series == POOLED:
                log.info("MARKET SKILL  %s", c.describe())
        return self._cells

    def verdict_for(self, series: str, lead_hours: float | None) -> Cell | None:
        b = bucket_of(lead_hours)
        if b is None:
            return None
        cells = self.cells()
        own = cells.get((series, b))
        if own is not None and own.verdict != UNPROVEN:
            return own
        return cells.get((POOLED, b)) or own

    def may_enter(self, series: str, lead_hours: float | None) -> tuple[bool, str]:
        if not self.cfg.enabled:
            return True, "market-skill gate disabled"
        cell = self.verdict_for(series, lead_hours)
        if cell is None:
            if self.cfg.require == "ahead":
                return False, (f"no market comparison yet at this lead time - "
                               f"an edge has to be shown before it is traded")
            return True, "no market comparison yet"
        scope = "" if cell.series == series else f" (all cities; {series} has too few of its own)"
        if self.cfg.require == "ahead" and cell.verdict != AHEAD:
            return False, cell.describe() + scope
        if cell.verdict == BEHIND:
            return False, cell.describe() + scope
        return True, cell.describe() + scope
