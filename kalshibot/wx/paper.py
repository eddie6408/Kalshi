"""WX-6: matching, shadow paper trading and class-separated validation.

For every modelled contract (extended and hourly), each model run is matched
against the live production quote:

    net edge (YES) = p - yes_ask - taker fee
    net edge (NO)  = (1 - p) - (1 - yes_bid) - taker fee
    required edge  = base + k * (half the model's sensitivity band) + spread term

A wide band means an uncertain model, which raises the bar; past `max_band`
the risk rule rejects it outright ("model uncertainty"). One notional
contract per decision, at most one open paper position per market, settled on
Kalshi's own published result. Nothing is ordered - this table is the paper
record that WX-7 reads.

Also here: the WX-3 daily shadow (the sharpened daily model priced beside the
live one on the same markets) and the class-separated reports - prediction
metrics (Brier, log loss, reliability vs the market) kept apart from trading
metrics (paper P&L with a confidence interval).
"""
from __future__ import annotations

import logging
import math
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import mean

from ..fees import taker_fee_usd
from .calibration import Calibration, contract_bounds, prob_integer
from .store import utcnow

log = logging.getLogger(__name__)


class PaperConfig:
    def __init__(self, d: dict | None = None):
        d = d or {}
        self.base_edge = float(d.get("base_edge", 0.05))
        self.band_k = float(d.get("band_k", 0.5))
        self.spread_k = float(d.get("spread_k", 0.25))
        self.max_band = float(d.get("max_band", 0.30))
        self.min_price = float(d.get("min_price", 0.03))
        self.max_price = float(d.get("max_price", 0.97))
        self.cooldown_hours = float(d.get("cooldown_hours", 6))


def _f(x):
    try:
        return None if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return None


def quotes_of(markets: list[dict]) -> dict:
    return {m["ticker"]: {"yes_bid": _f(m.get("yes_bid_dollars")),
                          "yes_ask": _f(m.get("yes_ask_dollars")),
                          "status": m.get("status"), "result": m.get("result")}
            for m in markets if m.get("ticker")}


class PaperBook:
    def __init__(self, store, cfg: PaperConfig | None = None):
        self.store = store
        self.cfg = cfg or PaperConfig()

    def decide(self, run: dict, contract: dict) -> dict:
        """One decision for one model run (pure; recorded by record())."""
        cfg = self.cfg
        p = run["prob"]
        band = (run.get("prob_hi") or p) - (run.get("prob_lo") or p)
        bid, ask = run.get("market_yes_bid"), run.get("market_yes_ask")
        base = {"ticker": run["ticker"], "cls": run["cls"], "prob": p, "ci_width": band,
                "run_id": run.get("id")}
        if contract.get("status") == "DISABLED" or "RULES CHANGED" in (contract.get("status_reason") or ""):
            return {**base, "decision": "PASS", "reason": "contract disabled or rules changed"}
        if band > cfg.max_band:
            return {**base, "decision": "PASS", "reason": f"model uncertainty: band {band:.2f}"}
        if bid is None or ask is None or ask <= 0 or bid >= ask:
            return {**base, "decision": "PASS", "reason": "no two-sided quote"}
        spread = ask - bid
        required = cfg.base_edge + cfg.band_k * band / 2 + cfg.spread_k * spread
        options = []
        if cfg.min_price <= ask <= cfg.max_price:
            fee = taker_fee_usd(ask * 100, 1)
            options.append(("YES", ask, fee, p - ask - fee))
        no_ask = 1.0 - bid
        if cfg.min_price <= no_ask <= cfg.max_price:
            fee = taker_fee_usd(no_ask * 100, 1)
            options.append(("NO", no_ask, fee, (1 - p) - no_ask - fee))
        if not options:
            return {**base, "decision": "PASS", "reason": "price outside tradable range"}
        side, price, fee, edge = max(options, key=lambda o: o[3])
        out = {**base, "side": side, "price": price, "fee": fee, "edge_net": edge,
               "required_edge": required}
        if edge < required:
            return {**out, "decision": "PASS",
                    "reason": f"edge {edge:+.3f} below required {required:.3f}"}
        return {**out, "decision": f"PAPER_{side}", "reason": "edge clears the uncertainty-adjusted bar"}

    def record(self, runs: list[dict], contracts: dict) -> int:
        n = 0
        now = datetime.now(timezone.utc)
        for run in runs:
            c = contracts.get(run["ticker"]) or {}
            d = self.decide(run, c)
            if d["decision"].startswith("PAPER_"):
                held = self.store.one("SELECT id FROM wx_paper WHERE ticker=? AND decision LIKE "
                                      "'PAPER_%' AND settled_yes IS NULL", (run["ticker"],))
                if held:
                    d = {**d, "decision": "PASS", "reason": "already holding a paper position"}
            last = self.store.one("SELECT at FROM wx_paper WHERE ticker=? ORDER BY id DESC LIMIT 1",
                                  (run["ticker"],))
            if d["decision"] == "PASS" and last and (
                    now - datetime.fromisoformat(last["at"])).total_seconds() < self.cfg.cooldown_hours * 3600:
                continue      # do not log the same pass every few minutes
            self.store.execute(
                "INSERT INTO wx_paper (at, ticker, cls, side, prob, price, fee, edge_net, "
                "required_edge, ci_width, decision, reason, run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (utcnow(), d["ticker"], d["cls"], d.get("side"), d["prob"], d.get("price"),
                 d.get("fee"), d.get("edge_net"), d.get("required_edge"), d["ci_width"],
                 d["decision"], d["reason"], d.get("run_id")))
            n += 1
        return n

    def resolve(self, results: dict[str, str]) -> int:
        """results: {ticker: 'yes'|'no'} from Kalshi's settled markets."""
        n = 0
        for t, res in results.items():
            if res not in ("yes", "no"):
                continue
            y = 1 if res == "yes" else 0
            for row in self.store.query("SELECT id, side, price, fee FROM wx_paper WHERE ticker=? "
                                        "AND decision LIKE 'PAPER_%' AND settled_yes IS NULL", (t,)):
                win = (row["side"] == "YES") == bool(y)
                pnl = (1.0 if win else 0.0) - row["price"] - (row["fee"] or 0.0)
                self.store.execute("UPDATE wx_paper SET settled_yes=?, pnl=?, settled_at=? WHERE id=?",
                                   (y, round(pnl, 4), utcnow(), row["id"]))
                n += 1
            self.store.execute("UPDATE wx_paper SET settled_yes=? WHERE ticker=? AND settled_yes IS NULL",
                               (y, t))
            self.store.execute("UPDATE wx_model_runs SET settled_yes=? WHERE ticker=? AND settled_yes IS NULL",
                               (y, t))
            self.store.execute("UPDATE wx_daily_shadow SET settled_yes=? WHERE ticker=? AND settled_yes IS NULL",
                               (y, t))
            self.store.execute("UPDATE wx_contracts SET result=?, market_status='settled' WHERE ticker=?",
                               (res, t))
        return n


# ------------------------------------------------------------ daily shadow
def daily_shadow(store, pit, calib: Calibration, contracts: list[dict], stations: dict,
                 journal_path: str | None = None, min_gap_minutes: int = 60) -> int:
    """Price live daily markets with the sharpened model; log beside the live
    probability from the journal. Never feeds a decision."""
    if not calib.fitted:
        return 0
    live = {}
    if journal_path:
        try:
            jc = sqlite3.connect(f"file:{journal_path}?mode=ro", uri=True, timeout=10)
            tick = [c["ticker"] for c in contracts]
            since = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()
            for i in range(0, len(tick), 400):
                part = tick[i:i + 400]
                for t, p, b, a, lead in jc.execute(
                        "SELECT ticker, our_probability, market_yes_bid_cents, market_yes_cents, "
                        f"lead_hours FROM probability_log WHERE ticker IN ({','.join('?' * len(part))}) "
                        "AND ts > ? ORDER BY id", part + [since]):
                    live[t] = (p, b, a, lead)      # the newest row wins
            jc.close()
        except sqlite3.Error as exc:
            log.debug("WX SHADOW  journal unavailable: %s", exc)
    by_cli = {(s.get("cli") or "").upper(): icao for icao, s in stations.items()}
    n = 0
    now = datetime.now(timezone.utc)
    for c in contracts:
        icao = by_cli.get((c.get("cli") or "").upper())
        if not icao or not c.get("window_start"):
            continue
        last = store.one("SELECT at FROM wx_daily_shadow WHERE ticker=? ORDER BY id DESC LIMIT 1",
                         (c["ticker"],))
        if last and (now - datetime.fromisoformat(last["at"])).total_seconds() < min_gap_minutes * 60:
            continue
        var = "temp_max" if c["cls"] == "DAILY_HIGH" else "temp_min"
        tz = stations[icao].get("timezone") or "America/New_York"
        from zoneinfo import ZoneInfo
        today = now.astimezone(ZoneInfo(tz)).date()
        target = date.fromisoformat(c["window_start"])
        lead_days = (target - today).days
        if lead_days < 0:
            continue
        values = pit.latest_forecast(icao, var, c["window_start"])
        centre = calib.centre(var, max(1, lead_days), icao, values) if values else None
        sd = calib.sd(var, max(1, lead_days), icao, target)
        if centre is None or not sd:
            continue
        op, lo, hi = contract_bounds(c)
        try:
            p_sharp = prob_integer(op, lo, hi, centre, sd)
        except (ValueError, TypeError):
            continue
        lv = live.get(c["ticker"])
        mid = ((lv[1] + lv[2]) / 200.0) if lv and lv[1] is not None and lv[2] is not None else None
        lead_h = ((datetime.combine(target, datetime.min.time(), ZoneInfo(tz)) + timedelta(days=1))
                  - now).total_seconds() / 3600
        store.execute(
            "INSERT OR IGNORE INTO wx_daily_shadow (at, ticker, series, target_date, kind, lead_hours, "
            "centre_f, p_live, sigma_live, p_sharp, sigma_sharp, calib_version, market_mid) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (utcnow(), c["ticker"], c["series"], c["window_start"],
             "high" if var == "temp_max" else "low", round(lead_h, 1), round(centre, 2),
             lv[0] if lv else None, None, round(p_sharp, 5), round(sd, 3), calib.version, mid))
        n += 1
    return n


# ------------------------------------------------------------- reporting
def _brier(p, y):
    return (p - y) ** 2


def _logloss(p, y):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def _ci95(xs):
    if len(xs) < 2:
        return None
    m = mean(xs)
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
    h = 1.96 * sd / math.sqrt(len(xs))
    return [m - h, m + h]


def class_report(store) -> dict:
    """Prediction metrics and trading metrics, separately, per class.
    Prediction uses each contract's LAST run before it closed (one per
    contract - no double counting of a market re-priced 200 times)."""
    out: dict = defaultdict(dict)
    rows = store.query(
        "SELECT r.cls, r.ticker, r.prob, r.market_yes_bid b, r.market_yes_ask a, r.settled_yes y "
        "FROM wx_model_runs r JOIN (SELECT ticker, MAX(id) mid FROM wx_model_runs r2 "
        "JOIN wx_contracts c USING (ticker) WHERE r2.at <= c.close_time "
        "AND r2.method NOT LIKE 'VOID%' GROUP BY ticker) last "
        "ON last.mid = r.id WHERE r.settled_yes IS NOT NULL")
    by: dict[str, list] = defaultdict(list)
    for r in rows:
        by[r["cls"]].append(r)
    for cls, rs in by.items():
        brier = [_brier(r["prob"], r["y"]) for r in rs]
        mkt = [_brier((r["b"] + r["a"]) / 2, r["y"]) for r in rs if r["b"] is not None and r["a"] is not None]
        skill = [_brier((r["b"] + r["a"]) / 2, r["y"]) - _brier(r["prob"], r["y"]) for r in rs
                 if r["b"] is not None and r["a"] is not None]
        bins = defaultdict(list)
        for r in rs:
            bins[min(9, int(r["prob"] * 10))].append(r)
        rel = [{"bin": k / 10, "n": len(v), "mean_p": mean(x["prob"] for x in v),
                "freq": mean(x["y"] for x in v)} for k, v in sorted(bins.items())]
        out[cls]["prediction"] = {
            "n": len(rs), "brier": mean(brier), "logloss": mean(_logloss(r["prob"], r["y"]) for r in rs),
            "market_brier": mean(mkt) if mkt else None,
            "skill_vs_market": mean(skill) if skill else None,
            "skill_ci95": _ci95(skill), "reliability": rel,
            "max_reliability_gap": max((abs(b["mean_p"] - b["freq"]) for b in rel if b["n"] >= 10),
                                       default=None)}
    for cls, n, open_n in [(r["cls"], r["n"], r["open_n"]) for r in store.query(
            "SELECT cls, SUM(settled_yes IS NOT NULL) n, SUM(settled_yes IS NULL) open_n FROM wx_paper "
            "WHERE decision LIKE 'PAPER_%' GROUP BY cls")]:
        pnl = [r["pnl"] for r in store.query(
            "SELECT pnl FROM wx_paper WHERE cls=? AND decision LIKE 'PAPER_%' AND pnl IS NOT NULL", (cls,))]
        edges = [r["e"] for r in store.query(
            "SELECT edge_net e FROM wx_paper WHERE cls=? AND decision LIKE 'PAPER_%'", (cls,))]
        out[cls]["trading"] = {"settled": n or 0, "open": open_n or 0,
                               "pnl_total": sum(pnl) if pnl else 0.0,
                               "pnl_mean": mean(pnl) if pnl else None, "pnl_ci95": _ci95(pnl),
                               "win_rate": (sum(p > 0 for p in pnl) / len(pnl)) if pnl else None,
                               "mean_expected_edge": mean(edges) if edges else None}
    sh = store.query("SELECT p_live, p_sharp, settled_yes y, lead_hours FROM wx_daily_shadow "
                     "WHERE settled_yes IS NOT NULL AND p_live IS NOT NULL")
    if sh:
        out["DAILY (shadow: sharp vs live)"]["prediction"] = {
            "n": len(sh), "brier_live": mean(_brier(r["p_live"], r["y"]) for r in sh),
            "brier_sharp": mean(_brier(r["p_sharp"], r["y"]) for r in sh),
            "sharp_minus_live_ci95": _ci95([_brier(r["p_sharp"], r["y"]) - _brier(r["p_live"], r["y"])
                                            for r in sh])}
    return dict(out)
