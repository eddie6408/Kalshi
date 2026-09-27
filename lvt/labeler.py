"""Signal outcome labeler (section 67).

For EVERY recorded signal - taken, rejected or only watched - measure what
the market did next, from the tape, at several horizons:

  mid_change   side-relative mid at +h minus mid at the signal
  mfe / mae    best / worst side-relative BID move vs the signal ASK
               (what a taker could actually have realised)
  first_hit    TARGET if the bid reached the signal's target before its
               stop, STOP if the stop came first, NEITHER otherwise

This is what answers "which dips reversed, which kept collapsing", "which
momentum entries continued" and "did we chase too late" - including for the
setups the bot correctly (or wrongly) refused to trade. Labels use only data
recorded after the signal, and are written only once the horizon has fully
elapsed, so they never feed back into a decision.
"""
from __future__ import annotations

from lvt.db import Database


def label_signals(db: Database, tape: Database, cfg, now: float, limit: int = 500) -> int:
    horizons = list(cfg.labeler.horizons_s)
    hmax = max(horizons)
    rows = db.query(
        "SELECT s.id, s.ts, s.ticker, s.side, s.mid, s.ask, s.target, s.stop FROM signals s "
        "WHERE s.ts <= ? AND s.mid IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM signal_outcomes o WHERE o.signal_id = s.id) ORDER BY s.ts LIMIT ?",
        (now - hmax - 5, limit))
    out = []
    for s in rows:
        qs = tape.query("SELECT ts, yes_bid, yes_ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? ORDER BY ts",
                        (s["ticker"], s["ts"], s["ts"] + hmax))
        for h in horizons:
            path = [(q["ts"], q["yes_bid"], q["yes_ask"]) for q in qs
                    if q["ts"] <= s["ts"] + h and q["yes_bid"] is not None and q["yes_ask"] is not None]
            if not path:
                out.append((s["id"], h, None, None, None, "NO_DATA", 0, now))
                continue
            if s["side"] == "yes":
                bids = [b for _, b, _ in path]
                mids = [(b + a) / 2 for _, b, a in path]
            else:
                bids = [100 - a for _, _, a in path]
                mids = [100 - (b + a) / 2 for _, b, a in path]
            ref_ask = s["ask"] if s["ask"] is not None else s["mid"]
            first = "NEITHER"
            for b in bids:
                if s["target"] is not None and b >= s["target"]:
                    first = "TARGET"
                    break
                if s["stop"] is not None and b <= s["stop"]:
                    first = "STOP"
                    break
            out.append((s["id"], h, round(mids[-1] - s["mid"], 3), round(max(bids) - ref_ask, 3),
                        round(min(bids) - ref_ask, 3), first, len(path), now))
    if out:
        db.executemany("INSERT OR REPLACE INTO signal_outcomes VALUES (?,?,?,?,?,?,?,?)", out)
    return len(rows)
