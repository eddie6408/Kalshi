"""How far ahead of Kalshi's live score does the market move, and what would
knowing each point L seconds early be worth after costs? (upper bound)"""
import json
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, "/opt/kalshi/live-volatility")
from lvt.db import Database
from lvt.edge.model_study import _Quotes
from lvt.edge.reference import MATCH_SERIES, TennisMatch, map_player, parse_state
from lvt.edge.tennis_model import p_match
from lvt.fees import fee_usd
from lvt.tape import load_event_meta, load_markets

tape = Database("/opt/kalshi/live-volatility/data/tape.db", readonly=True)
markets, meta = load_markets(tape), load_event_meta(tape)
legs = defaultdict(dict)
for t, mi in markets.items():
    if mi.sport == "TENNIS" and MATCH_SERIES.search(mi.series_ticker) and mi.event_ticker in meta:
        pl = map_player(t, mi.event_ticker, mi.sub_title or mi.title, meta[mi.event_ticker].get("title") or "")
        if pl is not None:
            legs[mi.event_ticker][pl] = t
LEADS = (0, 2, 5, 10, 20)
PRE = (-30, -20, -10, -5, -3, -2, 0, 5, 10, 30)
events = []
for ev, lg in legs.items():
    m = meta[ev]
    grp = list(dict.fromkeys([ev] + list(m.get("group") or [])))
    rows = tape.conn.execute(f"SELECT ts, summary FROM context WHERE event_ticker IN ({','.join('?' * len(grp))}) "
                             "ORDER BY ts", grp).fetchall()
    if len(rows) < 10:
        continue
    det = m.get("details") or {}
    bo = int(det.get("best_of") or 3) or 3
    base = TennisMatch._base_serve(det, {})
    q = _Quotes(tape, lg, rows[0][0] - 60, rows[-1][0] + 60)
    prev, prev_ts = None, None
    for ts, s in rows:
        st = parse_state((json.loads(s or "{}").get("raw")) or {}, bo)
        if st is None:
            prev = None
            continue
        if prev is not None and (st.sets, st.games, st.points) != (prev.sets, prev.games, prev.points) and ts - prev_ts < 12:
            imp = 100 * (p_match(base, base, st) - p_match(base, base, prev))    # model impact on P(c1), cents
            if abs(imp) >= 0.2:
                path = {k: q.at(ts + k) for k in PRE}
                if path[-30] and path[30] and path[0]:
                    events.append({"ev": ev, "ts": ts, "imp": imp, "path": path})
        prev, prev_ts = st, ts
print(f"score changes with quotes around them: {len(events)} across {len({e['ev'] for e in events})} matches")

BUCKETS = (("all", 0.2, 999), ("impact 0.2-2c", 0.2, 2), ("impact 2-5c", 2, 5), ("impact >=5c", 5, 999))
print("\nMarket move toward the point winner, measured from -30s; cents already moved at each offset "
      "(0 = when Kalshi's score feed showed the point):")
for name, lo, hi in BUCKETS:
    es = [e for e in events if lo <= abs(e["imp"]) < hi]
    if not es:
        continue
    sg = lambda e: 1 if e["imp"] > 0 else -1
    line = f"{name:<14} n={len(es):>6} | "
    for k in (-10, -5, -3, -2, 0, 5, 10, 30):
        done = [100 * sg(e) * (e["path"][k][0] - e["path"][-30][0]) for e in es if e["path"].get(k)]
        line += f"{k:+}s {statistics.mean(done):+.2f}  "
    print(line)

print("\nUpper bound: knew the point L s before Kalshi's feed; taker in at the ask, taker out at the bid +30s "
      "after the feed (c/contract net of fees):")
for name, lo, hi in BUCKETS:
    out = f"{name:<14}"
    for L in LEADS:
        nets = []
        for e in events:
            if not lo <= abs(e["imp"]) < hi:
                continue
            a, b = e["path"].get(-L), e["path"][30]
            if a is None:
                continue
            (p1, sp), (p1x, spx) = a, b
            up = e["imp"] > 0
            entry = 100 * (p1 if up else 1 - p1) + sp / 2
            exit_ = 100 * (p1x if up else 1 - p1x) - spx / 2
            nets.append(exit_ - entry - 100 * (fee_usd(1, entry) + fee_usd(1, max(exit_, 1))))
        if nets:
            out += f"  L={L:>2}s {statistics.mean(nets):+.2f} (n={len(nets)}, win {100 * sum(x > 0 for x in nets) / len(nets):.0f}%)"
    print(out)
