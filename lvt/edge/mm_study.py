"""EXP-008: does passive market-making in tennis match-winner markets pay?

Offline and read-only; no fair-value claim. Each market is replayed from the
tape (quotes on change + 6 s keepalive, exchange trade prints) with one-lot
post-only BUY orders resting on both sides (buy YES at the YES bid, buy NO at
the NO bid = sell YES at the YES ask). Fills come from the same queue model
paper trading uses (lvt/execution/maker_model.py): we join BEHIND the
displayed size, only trades or a book that moves through us fill us, and
nobody ahead of us ever cancels.

Operating assumptions (what a REST-polling bot could actually do):
* an order reaches the exchange `latency_s` after the snapshot that
  triggered it; trades before that cannot fill it;
* we only react at the next snapshot: trades that print between snapshots
  fill a resting order even if the book has moved - that is the adverse
  selection this study exists to measure;
* orders are cancelled when the touch leaves our price, when quoting
  conditions fail, or when the market's quotes stop (gap > `max_gap_s`).

Each fill is scored by its MARKOUT: side mid h seconds later minus fill
price minus maker fee (cents per contract). Holding to settlement is shown
too (high variance). Future prices label fills only; they never drive a
quoting decision.

Pre-registered decision (EXP-008): choose on DEV (first half of matches by
start) the variant with the best mean +60 s net markout; KILL if none is > 0
on DEV, or if on TEST the chosen variant's mean is <= 0 or its 95% match-
bootstrap CI reaches 0. Floor: 300 fills across 30 matches per split.
"""
from __future__ import annotations

import bisect
import json
import math
import random
from collections import defaultdict

from lvt.db import Database
from lvt.edge.reference import MATCH_SERIES
from lvt.execution.maker_model import RestingOrder, activate, advance
from lvt.models import OrderRequest, Quote, Snapshot, Trade
from lvt.tape import load_event_meta, load_markets

VARIANTS = {                     # name -> (improve by 1c, min spread c, score guard)
    "JOIN": (False, 2.0, False),
    "IMPROVE_1C": (True, 3.0, False),
    "JOIN+GUARD": (False, 2.0, True),
    "IMPROVE_1C+GUARD": (True, 3.0, True),
}
HORIZONS = (10, 60, 300)
PRIMARY = 60
SIDES = ("yes", "no")


class _Mids:
    def __init__(self, rows):
        self.ts = [r[0] for r in rows]
        self.mid = [(r[1] + r[2]) / 2.0 for r in rows]

    def at(self, t: float, max_age: float = 9.0):
        i = bisect.bisect_right(self.ts, t) - 1
        if i < 0 or t - self.ts[i] > max_age:
            return None
        return self.mid[i]


def simulate_market(ticker: str, info, quotes: list, trades: list[Trade], score_ts: list[float],
                    latency_s: float = 0.5, max_gap_s: float = 10.0, guard_s: float = 15.0,
                    mid_range=(5.0, 95.0), variants=VARIANTS) -> tuple[list[dict], dict]:
    """Replay one market. quotes: (ts, yes_bid, yes_ask, bid_size, ask_size, volume, status)
    rows in time order. Returns (fills, seconds quoted per variant)."""
    orders = {v: {s: None for s in SIDES} for v in variants}
    guard_until = 0.0
    fills: list[dict] = []
    quoted = defaultdict(float)
    ti = si = 0
    prev_ts = None
    seq = 0
    spread_at: dict[str, float] = {}
    for ts, yb, ya, bsz, asz, vol, status in quotes:
        gap = prev_ts is not None and ts - prev_ts > max_gap_s
        if gap:                                   # not watched: we would not know the book -> cancelled
            for v in variants:
                orders[v] = {s: None for s in SIDES}
        batch = []
        while ti < len(trades) and trades[ti].ts <= ts:
            if not gap and prev_ts is not None and trades[ti].ts > prev_ts:
                batch.append(trades[ti])
            ti += 1
        snap = Snapshot(ticker, ts, Quote(ticker, ts, yb, ya, bsz or 0.0, asz or 0.0, None, vol or 0.0, 0.0,
                                          status or "active"))
        for v in variants:
            for s in SIDES:
                o = orders[v][s]
                if o is None:
                    continue
                for f in advance(o, snap, batch, info, True, 0.5):
                    fills.append({"variant": v, "ticker": ticker, "side": s, "ts": f.ts, "price": f.price,
                                  "fee_c": 100.0 * f.fee_usd / f.count,
                                  "why": f.exchange_fill_id.rsplit("-", 1)[-1], "quoted_at": o.arrival_ts,
                                  "spread": spread_at[o.req.client_order_id]})
                if o.remaining <= 0:
                    orders[v][s] = None
            if prev_ts is not None and not gap and any(orders[v][s] for s in SIDES):
                quoted[v] += ts - prev_ts
        while si < len(score_ts) and score_ts[si] <= ts:      # score updates we have now seen
            guard_until = max(guard_until, score_ts[si] + guard_s)
            si += 1
        ok_book = (yb is not None and ya is not None and ya > yb and (status or "active") == "active"
                   and mid_range[0] <= (yb + ya) / 2 <= mid_range[1])
        for v, (improve, min_spread, guarded) in variants.items():
            live = ok_book and ya - yb >= min_spread and not (guarded and ts < guard_until)
            for s in SIDES:
                want = None
                if live:
                    bid = yb if s == "yes" else 100.0 - ya
                    want = bid + (1.0 if improve else 0.0)
                o = orders[v][s]
                if o is not None and (want is None or abs(o.price - want) > 1e-9):
                    orders[v][s] = o = None               # cancel: touch moved or conditions failed
                if o is None and want is not None:
                    seq += 1
                    req = OrderRequest(f"mm-{seq}", ticker, info.event_ticker, "buy", s, 1, want, "ENTRY", want,
                                       ts, post_only=True, tif="GTC")
                    spread_at[req.client_order_id] = ya - yb      # spread when we quoted (for reporting)
                    o = RestingOrder(req, ts + latency_s)
                    if activate(o, snap, 1e9) is None:
                        orders[v][s] = o
        prev_ts = ts
    return fills, dict(quoted)


def _boot_ci(vals_by_match: dict, n_boot: int = 1000, seed: int = 8):
    keys = sorted(vals_by_match)
    if len(keys) < 5:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        tot, n = 0.0, 0
        for k in (rng.choice(keys) for _ in keys):
            v = vals_by_match[k]
            tot, n = tot + sum(v), n + len(v)
        means.append(tot / n)
    means.sort()
    return round(means[int(0.025 * n_boot)], 3), round(means[int(0.975 * n_boot) - 1], 3)


def _summ(fills: list[dict], quoted_s: float) -> dict:
    if not fills:
        return {"fills": 0}
    out = {"fills": len(fills), "matches": len({f["match"] for f in fills}),
           "fills_per_quoted_hour": round(len(fills) / max(quoted_s / 3600.0, 1e-9), 1),
           "by_fill_type": dict(sorted(((k, sum(1 for f in fills if f["why"] == k)) for k in {f["why"] for f in fills}))),
           "mean_spread_c": round(sum(f["spread"] for f in fills) / len(fills), 2)}
    for h in HORIZONS:
        vals = [f[f"net_{h}"] for f in fills if f.get(f"net_{h}") is not None]
        out[f"net_markout_{h}s_c"] = round(sum(vals) / len(vals), 3) if vals else None
    by = defaultdict(list)
    for f in fills:
        if f.get(f"net_{PRIMARY}") is not None:
            by[f["match"]].append(f[f"net_{PRIMARY}"])
    out[f"ci95_{PRIMARY}s"] = _boot_ci(by)
    settled = [f["net_settle"] for f in fills if f.get("net_settle") is not None]
    out["net_to_settlement_c"] = round(sum(settled) / len(settled), 2) if settled else None
    typ = defaultdict(list)
    for f in fills:
        if f.get(f"net_{PRIMARY}") is not None:
            typ[f["why"]].append(f[f"net_{PRIMARY}"])
    out[f"net_{PRIMARY}s_by_fill_type_c"] = {k: round(sum(v) / len(v), 2) for k, v in sorted(typ.items())}
    return out


def _decide(dev: dict, test: dict, min_fills: int, min_matches: int) -> dict:
    key = f"net_markout_{PRIMARY}s_c"

    def enough(s):
        return s.get("fills", 0) >= min_fills and s.get("matches", 0) >= min_matches

    cand = {v: s for v, s in dev.items() if enough(s) and s.get(key) is not None}
    if not cand:
        return {"verdict": "INSUFFICIENT", "why": f"no variant has >= {min_fills} fills across >= {min_matches} matches on DEV"}
    best = max(cand, key=lambda v: cand[v][key])
    if cand[best][key] <= 0:
        return {"verdict": "KILL", "why": f"no variant earns a positive +{PRIMARY}s net markout on DEV "
                                          f"(best {best}: {cand[best][key]:+.3f}c)"}
    t = test.get(best, {})
    if not enough(t) or t.get(key) is None:
        return {"verdict": "INSUFFICIENT", "chosen": best, "why": f"{best} positive on DEV; TEST lacks fills"}
    ci = t.get(f"ci95_{PRIMARY}s")
    ok = t[key] > 0 and ci is not None and ci[0] > 0
    return {"verdict": "SURVIVES" if ok else "KILL", "chosen": best,
            "why": f"{best} on TEST: {t[key]:+.3f}c/contract, 95% CI {ci}"}


def mm_study(tape: Database, start: float, end: float, min_fills: int = 300, min_matches: int = 30,
             **sim_kw) -> dict:
    markets = load_markets(tape)
    meta = load_event_meta(tape)
    res = {r["ticker"]: r["result"] for r in tape.query("SELECT ticker, result FROM market_results")}
    tickers = sorted(t for t, mi in markets.items() if mi.sport == "TENNIS" and MATCH_SERIES.search(mi.series_ticker))
    all_fills, quoted, first_ts = [], defaultdict(lambda: defaultdict(float)), {}
    for t in tickers:
        info = markets[t]
        ev = info.event_ticker
        q = tape.conn.execute("SELECT ts, yes_bid, yes_ask, bid_size, ask_size, volume, status FROM quotes "
                              "WHERE ticker=? AND ts>=? AND ts<? ORDER BY ts", (t, start, end)).fetchall()
        if len(q) < 10:
            continue
        tr = [Trade(t, r[0], r[1], r[2], r[3], r[4] or "") for r in tape.conn.execute(
            "SELECT ts, yes_price, count, taker_side, trade_id FROM trades WHERE ticker=? AND ts>=? AND ts<? ORDER BY ts",
            (t, start, end))]
        if not tr:
            continue                                  # no trade feed for this market: skip, never guess fills
        group = list(dict.fromkeys([ev] + list((meta.get(ev) or {}).get("group") or [])))
        sc = [r[0] for r in tape.conn.execute(
            f"SELECT ts FROM context WHERE event_ticker IN ({','.join('?' * len(group))}) AND ts>=? AND ts<? ORDER BY ts",
            (*group, start, end))]
        fills, qs = simulate_market(t, info, q, tr, sc, **sim_kw)
        first_ts[ev] = min(first_ts.get(ev, math.inf), q[0][0])
        for v, s in qs.items():
            quoted[ev][v] += s
        mids = _Mids([r for r in q if r[1] is not None and r[2] is not None])
        for f in fills:
            f["match"] = ev
            for h in HORIZONS:
                m = mids.at(f["ts"] + h)
                f[f"net_{h}"] = None if m is None else round((m if f["side"] == "yes" else 100 - m) - f["price"] - f["fee_c"], 3)
            r = res.get(t)
            if r in ("yes", "no"):
                val = 100.0 if (r == "yes") == (f["side"] == "yes") else 0.0
                f["net_settle"] = round(val - f["price"] - f["fee_c"], 3)
        all_fills += fills
    if not all_fills:
        return {"fills": 0, "note": "no tennis match-winner markets with quotes and trade prints in the window"}
    order = sorted(first_ts, key=first_ts.get)
    dev_set = set(order[:len(order) // 2])
    out = {"experiment": "EXP-008", "markets_simulated": len({f["ticker"] for f in all_fills}),
           "matches": len(order), "dev_matches": len(dev_set), "test_matches": len(order) - len(dev_set),
           "settings": {"variants": {k: {"improve_1c": a, "min_spread_c": b, "score_guard": c} for k, (a, b, c) in VARIANTS.items()},
                        "primary": f"+{PRIMARY}s net markout (side mid - fill - maker fee), cents/contract",
                        "floor": f">= {min_fills} fills across >= {min_matches} matches per split", **{k: v for k, v in sim_kw.items()}},
           "scores": {}}
    for split, keep in (("DEV", lambda m: m in dev_set), ("TEST", lambda m: m not in dev_set), ("ALL", lambda m: True)):
        out["scores"][split] = {}
        for v in VARIANTS:
            fs = [f for f in all_fills if f["variant"] == v and keep(f["match"])]
            qsec = sum(quoted[m][v] for m in quoted if keep(m))
            out["scores"][split][v] = _summ(fs, qsec)
    out["decision"] = _decide(out["scores"]["DEV"], out["scores"]["TEST"], min_fills, min_matches)
    out["note"] = ("Markout to the MID ignores the cost of getting out: realised spread capture needs the opposite "
                   "side to fill passively too, so a positive markout is necessary, not sufficient.")
    return out


def format_mm_study(rep: dict) -> str:
    if not rep.get("scores"):
        return f"=== STUDY: mm (EXP-008) ===\n{json.dumps(rep, indent=1, default=str)}"
    L = ["=== STUDY: mm (EXP-008) ===",
         f"markets {rep['markets_simulated']}  matches {rep['matches']} (dev {rep['dev_matches']} / test {rep['test_matches']})",
         f"primary: {rep['settings']['primary']};  floor: {rep['settings']['floor']}", "",
         f"{'split':<5} {'variant':<17} {'fills':>7} {'mtch':>5} {'/q-hr':>6} {'sprd':>5} {'+10s':>7} {'+60s':>7} "
         f"{'95% CI +60s':>18} {'+300s':>7} {'settle':>7}  +60s by fill type"]
    for split in ("DEV", "TEST", "ALL"):
        for v in VARIANTS:
            s = rep["scores"][split][v]
            if not s.get("fills"):
                L.append(f"{split:<5} {v:<17} {0:>7}")
                continue
            L.append(f"{split:<5} {v:<17} {s['fills']:>7} {s['matches']:>5} {s['fills_per_quoted_hour']:>6} "
                     f"{s['mean_spread_c']:>5} {str(s['net_markout_10s_c']):>7} {str(s['net_markout_60s_c']):>7} "
                     f"{str(s['ci95_60s']):>18} {str(s['net_markout_300s_c']):>7} {str(s['net_to_settlement_c']):>7}  "
                     f"{s['net_60s_by_fill_type_c']}")
        L.append("")
    d = rep["decision"]
    L.append(f"DECISION (pre-registered): {d['verdict']} - {d['why']}")
    L.append(rep["note"])
    return "\n".join(L)
