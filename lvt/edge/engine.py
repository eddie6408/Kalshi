"""EDGE_ENGINE_V1 - event-aware market-dislocation trading (phases 2, 4-8).

"Do not simply trade movement. Trade the difference between observed market
movement and justified market movement."

For every modelled market (tennis match-winner and current-set markets)
and each side (YES, and NO as its mirror - buying NO is the only downside
position Kalshi offers):

    justified move  = change of the reference value (score-driven only)
    observed move   = change of the market mid
    dislocation     = reference - market mid       (in the side's favour)

Primary classification, exactly one per evaluation:

    DISLOCATION_REVERSAL  price fell further than the score justifies, the
                          fall has stopped and flow is no longer pushing it
    UNFINISHED_MOMENTUM   the score moved the reference, the price has only
                          done part of that move and flow still pushes
    FLOW_REVERSAL         no score change, a temporary order-flow shock
                          pushed the price away from the reference, now
                          stabilising
    EVENT_REPRICING       the price moved about as much as the score
                          justifies -> NO_EDGE
    NO_EDGE               everything else (with the reason)

Every trade must clear MIN_REQUIRED_MOVE (fees + latency + risk buffer; the
spread is paid inside the entry price) and both a STRATEGIC EDGE score and
a TRADE QUALITY score. Signals are re-evaluated from scratch every cycle
(edge decay): stale score data, a moved market or a changed reference kill
them automatically.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import deque
from typing import Optional

from lvt.edge.order_flow import FlowTracker
from lvt.edge.reference import ReferenceEngine, TennisState, describe
from lvt.engine.exits import ExitDecision
from lvt.engine.strategies import maker_entry_price
from lvt.engine.volatility import linreg
from lvt.fees import fee_per_contract_cents
from lvt.models import NO, YES, MarketInfo, Signal, Snapshot, new_id

KINDS = ("DISLOCATION_REVERSAL", "UNFINISHED_MOMENTUM", "FLOW_REVERSAL")
STRATEGIC_W = {"dislocation": 0.25, "alignment": 0.20, "confirmation": 0.20, "order_flow": 0.10,
               "confidence": 0.15, "remaining_move": 0.10}
QUALITY_W = {"liquidity": 0.25, "spread": 0.20, "fill_probability": 0.25, "freshness": 0.15, "latency": 0.15}


def clamp01(x: float) -> float:
    return 0.0 if x != x else max(0.0, min(1.0, x))


def severity(impact_c: float) -> str:
    a = abs(impact_c)
    return "ORDINARY" if a < 1 else "MODERATE" if a < 3 else "MAJOR" if a < 8 else "CRITICAL"


def state_tags(st: Optional[TennisState]) -> list[str]:
    """Pressure points in the state BEFORE the next point."""
    if st is None or st.in_tiebreak:
        return ["TIEBREAK"] if st is not None and st.in_tiebreak else []
    tags = []
    srv, rcv = st.server, 1 - st.server
    sp, rp = st.points[srv], st.points[rcv]
    if rp >= 3 and rp > sp:
        tags.append("BREAK_POINT")
    for x in (0, 1):
        mine, theirs = st.points[x], st.points[1 - x]
        game_pt = mine >= 3 and mine > theirs
        if not game_pt:
            continue
        g_me, g_them = st.games[x] + 1, st.games[1 - x]
        wins_set = (g_me >= 6 and g_me - g_them >= 2) or g_me == 7
        if wins_set:
            tags.append("SET_POINT" if st.sets[x] + 1 < st.need else "MATCH_POINT")
    return tags


class EdgeEngine:
    def __init__(self, cfg, db, env: str, run_id: str, series: dict, get_info, get_meta):
        self.cfg = cfg
        self.e = cfg.edge
        self.db = db
        self.env = env
        self.run_id = run_id
        self.series = series
        self.get_info = get_info
        self.get_meta = get_meta
        self.refs = ReferenceEngine(self.e)
        self.flow = FlowTracker(float(self.e.flow_short_s), float(self.e.flow_long_s),
                                float(self.e.large_trade_contracts), float(self.e.flow_shock_mult))
        self.ref_hist: dict[str, deque] = {}               # ticker -> (ts, ref_yes)
        self.last_event: dict[str, dict] = {}              # match event -> latest event summary
        self.event_seq: dict[str, int] = {}
        self.pending_votes: list[tuple] = []               # (due_ts, ticker, expected, mid_at_event)
        self.recorded_ref: dict[str, tuple[float, float]] = {}
        self.no_edge_seen: dict[tuple, float] = {}
        self.fill_prob_cache = (0.0, float(self.e.prior_fill_probability))
        style = cfg.data.get("execution", {})
        blob = json.dumps([cfg.data.get("edge", {}), style.get("entry_style"), style.get("exit_style"),
                           cfg.data.get("maker", {})], sort_keys=True, default=str).encode()
        self.version = f"{self.e.version}+{hashlib.sha256(blob).hexdigest()[:8]}"
        self.maker_entry = style.get("entry_style", "taker") == "maker"
        self.maker_tp = bool(cfg.data.get("maker", {}).get("take_profit", False))

    # ------------------------------------------------------------ tracking
    def track(self, ticker: str) -> bool:
        info = self.get_info(ticker)
        if info is None or info.sport != "TENNIS":
            return False
        return self.refs.register_market(info, self.get_meta(info.event_ticker))

    def match_of(self, ticker: str) -> Optional[str]:
        hit = self.refs.market_match.get(ticker)
        return hit[0] if hit else None

    # --------------------------------------------------- phase 2: events
    def on_context(self, ctx, events: list, now: float) -> None:
        if ctx.sport != "TENNIS":
            return
        m = self.refs.match_for_event(ctx.event_ticker)
        before = m.state if m else None
        prev_poll_ts = m.state_ts if m else 0.0     # the event happened after this poll (old score still current)
        m = self.refs.on_context(ctx, self.get_meta(ctx.event_ticker))
        if m is None or not events:
            return
        after = m.state
        kinds = "+".join(sorted({(e.kind if e.kind != "GAME_WON" else f"GAME_WON:{e.detail.get('game_type')}")
                                 + (f"(P{e.actor})" if e.actor else "") for e in events}))
        tags = state_tags(before)
        flags = sorted({f for e in events for f in e.flags})
        self.event_seq[m.event_ticker] = self.event_seq.get(m.event_ticker, 0) + 1
        impacts: dict[str, float] = {}
        rows = []
        for ticker, (mev, kind, player, _) in list(self.refs.market_match.items()):
            if mev != m.event_ticker or before is None or after is None:
                continue
            rb, _ = self.refs.reference(ticker, now, before)
            ra, _ = self.refs.reference(ticker, now, after)
            if rb is None or ra is None:
                continue
            impact = ra.value - rb.value
            impacts[ticker] = impact
            ser = self.series.get(ticker)
            mid = ser.last_snapshot.mid(YES) if ser and ser.last_snapshot else None
            rows.append((new_id("evr"), now, m.event_ticker, ticker, kinds, ",".join(tags), severity(impact),
                         round(impact, 3), round(rb.value, 3), round(ra.value, 3), mid, ",".join(flags),
                         describe(before), describe(after), self.env))
            if mid is not None:
                self.pending_votes.append((now + float(self.e.mapping_vote_delay_s), ticker, impact, mid))
        if rows:
            self.db.executemany("INSERT INTO event_responses (id, ts, match_event, ticker, kinds, tags, severity, "
                                "expected_impact, ref_before, ref_after, mid_at_event, flags, state_before, "
                                "state_after, env) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        # The market usually reacts BEFORE our next score poll reveals the event,
        # so the reaction is measured from the previous poll, not from now.
        base_ts = (prev_poll_ts if prev_poll_ts else now - float(self.cfg.context.get("tennis_live_interval_s", 4))) - 2.0
        self.last_event[m.event_ticker] = {"ts": now, "base_ts": base_ts, "kinds": kinds, "tags": tags,
                                           "impacts": impacts, "flags": flags, "seq": self.event_seq[m.event_ticker]}

    def on_trades(self, trades) -> None:
        if trades:
            self.flow.add(trades)

    # ---------------------------------------------------- per-cycle upkeep
    def step(self, snaps: dict[str, Snapshot], now: float) -> None:
        for t, s in snaps.items():
            if not self.track(t):
                continue
            mid = s.mid(YES)
            if mid is None:
                continue
            self.refs.sample_anchor(t, mid, s.spread(), now)
            ref, _ = self.refs.reference(t, now)
            if ref is None:
                continue
            h = self.ref_hist.setdefault(t, deque(maxlen=2000))
            if not h or abs(h[-1][1] - ref.value) > 1e-6 or now - h[-1][0] > 30:
                h.append((now, ref.value))
            last = self.recorded_ref.get(t)
            if last is None or abs(last[1] - ref.value) >= 0.5 or now - last[0] >= 60:
                self.recorded_ref[t] = (now, ref.value)
                self.db.insert("reference_values", {
                    "ts": now, "env": self.env, "ticker": t, "kind": ref.kind, "reference": ref.value,
                    "ref_lo": ref.lo, "ref_hi": ref.hi, "confidence": ref.confidence, "market_mid": mid,
                    "dislocation": round(ref.value - mid, 3), "strength_d": ref.d, "samples": ref.samples,
                    "state": ref.state_text, "mapping": ref.mapping, "context_age_s": ref.context_age_s})
        keep = []
        for due, t, expected, mid0 in self.pending_votes:
            if now < due:
                keep.append((due, t, expected, mid0))
                continue
            ser = self.series.get(t)
            mid = ser.last_snapshot.mid(YES) if ser and ser.last_snapshot else None
            if mid is not None:
                self.refs.note_response(t, expected, mid - mid0)
        self.pending_votes = keep

    def _ref_at(self, ticker: str, ts: float) -> Optional[float]:
        h = self.ref_hist.get(ticker)
        if not h:
            return None
        val = h[0][1]
        for t, v in h:
            if t <= ts:
                val = v
            else:
                break
        return val

    def fill_probability(self, now: float) -> float:
        ts, p = self.fill_prob_cache
        if now - ts < 300:
            return p
        rows = self.db.query("SELECT status FROM orders WHERE env=? AND purpose='ENTRY' ORDER BY created_ts DESC LIMIT 200",
                             (self.env,))
        n = sum(1 for r in rows if r["status"] in ("FILLED", "PARTIAL", "NO_FILL", "CANCELLED"))
        f = sum(1 for r in rows if r["status"] in ("FILLED", "PARTIAL"))
        prior, w = float(self.e.prior_fill_probability), 20.0
        p = (f + prior * w) / (n + w)                     # shrunk toward the prior until data accumulates
        self.fill_prob_cache = (now, p)
        return p

    # --------------------------------------------- phases 4-7: evaluation
    def evaluate(self, ticker: str, snap: Snapshot, m, info: MarketInfo, now: float,
                 exposed_matches: set) -> Optional[Signal]:
        """One signal (BUY / WATCH / NO_EDGE) for the side with the gap, or
        None when there is nothing worth recording."""
        e = self.e
        if not self.track(ticker):
            return None
        ref, why = self.refs.reference(ticker, now)
        if ref is None:
            return None                                     # no reference: market-only analysis is the baseline's job
        mid_yes = snap.mid(YES)
        if mid_yes is None:
            return None
        side = YES if ref.value >= mid_yes else NO
        refS, loS, hiS = ref.side(side)
        bid, ask, mid = snap.bid(side), snap.ask(side), snap.mid(side)
        if bid is None or ask is None:
            return None
        disl = refS - mid
        match_ev = self.match_of(ticker)
        ev = self.last_event.get(match_ev, {})
        W = float(e.response_window_s)
        if ev and now - ev["ts"] <= W:
            base_ts = ev["base_ts"]                           # the last poll that still showed the old score
            anchor_txt = f"since {ev['kinds']} (seen {now - ev['ts']:.0f}s ago)"
        else:
            base_ts = now - W
            anchor_txt = f"last {W:.0f}s"
        ser = self.series.get(ticker)
        ts, mids, _, _ = ser.view(side, base_ts - 1)
        if len(mids) < 3:
            return None
        obs = mid - mids[0]
        r0 = self._ref_at(ticker, base_ts)
        if r0 is None or self.ref_hist[ticker][0][0] > base_ts + 5:
            return None                                     # no reference history for the window: cannot judge the move
        just = refS - (r0 if side == YES else 100.0 - r0)
        interesting = abs(disl) >= float(e.record_min_dislocation_cents) or abs(obs) >= float(e.min_move_cents)
        if not interesting:
            return None

        # ---- costs: MIN_REQUIRED_MOVE ---------------------------------------
        entry_px = maker_entry_price(bid, ask, float(self.cfg.data.get("maker", {}).get("improve_if_spread_ge", 2.0))) \
            if self.maker_entry else ask
        fin = fee_per_contract_cents(entry_px, info.fee_type, info.fee_multiplier, 10, maker=self.maker_entry)
        vol = m.realized_vol if m is not None else 1.0
        latency_buf = vol * math.sqrt(float(e.latency_s) / 60.0)
        slip = 0.0 if self.maker_tp else float(self.cfg.signals.slippage_buffer_cents)
        target = entry_px + (refS - entry_px) * float(e.capture_frac)
        fout = fee_per_contract_cents(max(min(target, 99.0), 1.0), info.fee_type, info.fee_multiplier, 10, maker=self.maker_tp)
        required = fin + fout + slip + latency_buf + float(e.edge_buffer_cents)
        remaining = target - entry_px
        net = remaining - (required - float(e.edge_buffer_cents))
        stop = max(1.0, min(entry_px - float(e.stop_min_cents), mid - float(e.stop_vol_mult) * vol))

        # ---- microstructure ----------------------------------------------------
        fl = self.flow.flow(ticker, side, now)
        book = self.flow.book(snap, side, float(self.cfg.discovery.depth_max_age_s))
        recent_low = min(mids)
        low_ts = max(x for x, y in zip(ts, mids) if y == recent_low)     # latest touch of the low
        stabilised = now - low_ts >= float(e.stabilize_s)
        bounce = mid - recent_low
        pre_low = [(x, y) for x, y in zip(ts, mids) if x <= low_ts]
        move_start_ts = max(pre_low, key=lambda xy: (xy[1], xy[0]))[0] if pre_low else low_ts
        match = self.refs.matches.get(match_ev)
        score_after_move = match is not None and match.state_ts >= move_start_ts + 1.0
        # velocity over the recent stabilisation window only - a 60 s slope would
        # still contain the move being judged
        rw = max(2.0 * float(e.stabilize_s), 12.0)
        r_ts = [x for x in ts if x >= now - rw]
        v_short = linreg(r_ts, mids[len(mids) - len(r_ts):])[0] if len(r_ts) >= 3 else 0.0
        completion = (obs / just) if abs(just) >= 0.5 else None

        reasons, tags = [], ["TENNIS", ref.kind] + ev.get("tags", [])
        kind, action = "NO_EDGE", "NO_EDGE"
        confirm = 0.0
        if match_ev in exposed_matches:
            reasons.append("already exposed to this match (correlated markets are one risk)")
        elif ref.confidence < float(e.min_confidence):
            reasons.append(f"reference confidence {ref.confidence:.0%} < {float(e.min_confidence):.0%}")
        elif disl < float(e.min_dislocation_cents):
            reasons.append(f"dislocation {disl:+.1f}c below {e.min_dislocation_cents}c")
            if abs(obs) >= float(e.min_move_cents) and abs(obs - just) < float(e.min_dislocation_cents):
                kind = "EVENT_REPRICING"
                reasons.append(f"moved {obs:+.1f}c vs justified {just:+.1f}c: the score explains the move")
        elif net < 0:
            reasons.append(f"remaining move {remaining:.1f}c does not cover costs {required:.1f}c")
        elif obs <= -float(e.min_move_cents) and obs < just - float(e.min_dislocation_cents):
            kind = "DISLOCATION_REVERSAL" if abs(just) >= 0.5 or ev else "FLOW_REVERSAL"
            if kind == "FLOW_REVERSAL" and not (fl.shock or fl.sell_volume > fl.buy_volume):
                kind = "DISLOCATION_REVERSAL"
            checks = [(v_short >= -float(e.stabilize_velocity), f"downward velocity slowed ({v_short:+.2f}c/min)",
                       f"still falling {v_short:+.2f}c/min"),
                      (not (fl.signed_short < 0 and fl.acceleration < 0), "selling pressure weakening",
                       "selling pressure still accelerating"),
                      (stabilised, f"no new low for {now - low_ts:.0f}s", f"new low only {now - low_ts:.0f}s ago"),
                      (bounce >= float(e.min_bounce_cents), f"reversal confirmed: +{bounce:.1f}c off the low",
                       f"no reversal yet: +{bounce:.1f}c off the low"),
                      (score_after_move, "score re-checked after the move began",
                       "no score update since the move began - a real event cannot be ruled out")]
            confirm = sum(1 for ok, _, _ in checks if ok) / len(checks)
            for ok, good, bad in checks:
                reasons.append(good if ok else bad)
            action = "BUY" if confirm == 1.0 else "WATCH"
            reasons.insert(0, f"fell {obs:+.1f}c, justified {just:+.1f}c: overreaction of {just - obs:.1f}c")
        elif just >= float(e.min_move_cents) and obs >= -0.5 and completion is not None and completion < float(e.max_completion):
            kind = "UNFINISHED_MOMENTUM"
            checks = [(fl.signed >= 0, "order flow still buying", "order flow against the move"),
                      (v_short >= 0, f"price still moving our way ({v_short:+.2f}c/min)", f"price turning ({v_short:+.2f}c/min)")]
            confirm = sum(1 for ok, _, _ in checks if ok) / len(checks)
            for ok, good, bad in checks:
                reasons.append(good if ok else bad)
            action = "BUY" if confirm == 1.0 else "WATCH"
            reasons.insert(0, f"score justified {just:+.1f}c, market moved {obs:+.1f}c ({completion:.0%} complete)")
        else:
            reasons.append(f"gap {disl:+.1f}c without an overreaction or unfinished-move pattern")

        # ---- scores (phase 7) ----------------------------------------------------
        ctx_age = ref.context_age_s
        fillp = self.fill_probability(now) if self.maker_entry else 0.9
        strategic = {
            "dislocation": clamp01(disl / (3.0 * float(e.min_dislocation_cents))),
            "alignment": clamp01((just - obs) / max(abs(obs), 1.0)) if kind in ("DISLOCATION_REVERSAL", "FLOW_REVERSAL")
            else clamp01(1.0 - (completion or 1.0)) if kind == "UNFINISHED_MOMENTUM" else 0.0,
            "confirmation": confirm,
            "order_flow": clamp01(0.5 + (fl.acceleration if kind != "UNFINISHED_MOMENTUM" else fl.signed) / max(abs(fl.signed) + abs(fl.acceleration), 1.0) / 2),
            "confidence": ref.confidence,
            "remaining_move": clamp01(net / max(2.0 * required, 0.5)),
        }
        quality = {
            "liquidity": clamp01(snap.liquidity_usd() / max(5.0 * float(self.cfg.eligibility.min_liquidity_usd), 1.0)),
            "spread": clamp01(1.0 - ((snap.spread() or 9.0) - 1.0) / 4.0),
            "fill_probability": clamp01(fillp),
            "freshness": clamp01(1.0 - ctx_age / float(e.context_stale_s)),
            "latency": clamp01(1.0 - (now - snap.ts) / float(self.cfg.eligibility.max_data_age_s)),
        }
        edge_score = round(100 * sum(STRATEGIC_W[k] * v for k, v in strategic.items()) / sum(STRATEGIC_W.values()), 1)
        quality_score = round(100 * sum(QUALITY_W[k] * v for k, v in quality.items()) / sum(QUALITY_W.values()), 1)
        if action == "BUY" and edge_score < float(e.min_edge_score):
            action = "WATCH"
            reasons.append(f"edge score {edge_score:.0f} < {e.min_edge_score}")
        if action == "BUY" and quality_score < float(e.min_quality_score):
            action = "NO_EDGE"
            reasons.append(f"trade quality {quality_score:.0f} < {e.min_quality_score}")
        if action == "NO_EDGE" and kind in KINDS:
            kind = "NO_EDGE"

        risk_c = max(entry_px - stop, 0.5) + fin + fee_per_contract_cents(max(stop, 1.0), info.fee_type, info.fee_multiplier, 10)
        sig = Signal(id=new_id("sig"), ts=now, ticker=ticker, event_ticker=info.event_ticker,
                     strategy=f"EDGE_{kind}", strategy_version=self.version, side=side, action=action,
                     stage="CONFIRMED" if action == "BUY" else "SETUP", score=edge_score, bid=bid, ask=ask, mid=mid,
                     spread=snap.spread(), target=round(target, 2), stop=round(stop, 2),
                     expected_net_cents=round(net, 3), reward_risk=round(net / risk_c, 3) if risk_c > 0 else 0.0,
                     state=kind, regime="", category=info.category, sport=info.sport,
                     anchor=f"{side}:{self.event_seq.get(match_ev, 0)}:{kind}",
                     data_ts=snap.ts)
        sig.components = {**{f"edge_{k}": round(v, 3) for k, v in strategic.items()},
                          **{f"quality_{k}": round(v, 3) for k, v in quality.items()},
                          "edge_score": edge_score, "quality_score": quality_score}
        sig.reasons = reasons
        last_hi = max(mids)
        sig.explanation = {
            "classification": kind, "tags": tags, "market": f"{info.title or ticker} ({info.sub_title}) [{side.upper()}]",
            "current": round(mid, 2), "reference": round(refS, 2), "reference_range": [round(loS, 2), round(hiS, 2)],
            "recent_high": round(last_hi, 2), "observed_move": round(obs, 2), "justified_move": round(just, 2),
            "overreaction": round(just - obs, 2), "window": anchor_txt,
            "event": ev.get("kinds", "none recent"), "event_impact": severity(ev.get("impacts", {}).get(ticker, 0.0)) if ev else "none",
            "score_state": ref.state_text, "serve_strength": {"pa": ref.pa, "pb": ref.pb, "samples": ref.samples},
            "mapping": ref.mapping, "order_flow": fl.describe(), "order_book": book,
            "reversal": round(bounce, 2), "liquidity": round(snap.liquidity_usd(), 1), "spread": snap.spread(),
            "entry_price": round(entry_px, 2), "expected_remaining_move": round(remaining, 2),
            "min_required_move": round(required, 2), "estimated_net_edge": round(net, 2),
            "move_completion": None if completion is None else round(completion, 3),
            "edge_confidence": ref.confidence, "confidence": ref.confidence, "fill_probability": round(fillp, 3),
            "context_age_s": ctx_age, "risk_cents": round(risk_c, 3), "edge_score": edge_score,
            "quality_score": quality_score,
            "rank": round(max(net, 0.0) * quality_score / 100.0 * ref.confidence * fillp, 4),
        }
        return sig

    # ----------------------------------------------- phase 8: exits & decay
    def entry_still_valid(self, ticker: str, side: str, entry_px: float, now: float) -> tuple[bool, str]:
        """Edge decay for a resting entry: still a reference, and it still
        sits above our price by more than the costs."""
        ref, why = self.refs.reference(ticker, now)
        if ref is None:
            return False, f"edge decayed: {why}"
        refS, _, _ = ref.side(side)
        if refS - entry_px < float(self.e.min_dislocation_cents) / 2:
            return False, f"edge decayed: reference {refS:.1f}c now within reach of entry {entry_px:.1f}c"
        return True, "ok"

    def exit_check(self, p, snap: Snapshot, now: float) -> Optional[ExitDecision]:
        ref, why = self.refs.reference(p.ticker, now)
        if ref is None:
            return None                                     # untrusted -> generic exits only
        refS, _, _ = ref.side(p.side)
        bid = snap.bid(p.side)
        if bid is None:
            return None
        e = self.e
        if refS < p.avg_entry - float(e.thesis_buffer_cents):
            return ExitDecision("THESIS_INVALIDATED", f"reference {refS:.1f}c fell below entry {p.avg_entry:.1f}c "
                                                      f"({ref.state_text})", bid, max(1.0, bid - 5))
        if bid >= refS - float(e.exit_buffer_cents):
            return ExitDecision("REFERENCE_VALUE_REACHED", f"bid {bid:.1f}c reached reference {refS:.1f}c", bid, max(1.0, bid - 2))
        if refS - bid < float(e.exit_min_remaining_cents) and bid > p.avg_entry:
            return ExitDecision("EDGE_COLLAPSED", f"only {refS - bid:.1f}c left to reference {refS:.1f}c", bid, max(1.0, bid - 2))
        return None

    def row_extras(self, ticker: str, now: float, mid_yes: Optional[float]) -> dict:
        if not self.track(ticker):
            return {}
        ref, why = self.refs.reference(ticker, now)
        if ref is None:
            return {"ref_status": why}
        ev = self.last_event.get(self.match_of(ticker) or "", {})
        return {"reference": ref.value, "ref_range": [ref.lo, ref.hi], "edge_confidence": ref.confidence,
                "dislocation": None if mid_yes is None else round(ref.value - mid_yes, 2),
                "event_state": ref.state_text, "event": ev.get("kinds", ""),
                "event_impact": severity(ev.get("impacts", {}).get(ticker, 0.0)) if ev else "",
                "order_flow": self.flow.flow(ticker, YES, now).describe()}


def exposed_matches(edge: EdgeEngine, book) -> set:
    out = set()
    for p in book.open_positions():
        mev = edge.match_of(p.ticker)
        if mev:
            out.add(mev)
    return out

