"""Reactive strategies (sections 19-21, 30, 31, 46).

Two detectors, each run on either side of the book:

    detector   side  subtype     meaning
    --------   ----  ---------   ----------------------------------------------
    dip        YES   DIP         buy the dip: decline -> stabilise -> reversal
    dip        NO    REVERSAL    sell into an increase: YES spiked and turned
    momentum   YES   MOMENTUM    buy a confirmed, persistent, volume-backed rise
    momentum   NO    DOWNTREND   sell into a decline: confirmed YES fall

Kalshi has no short sale. "Selling" YES exposure you don't own is done by
BUYING NO, which is exactly the order a live account can place, so paper
results for REVERSAL and DOWNTREND correspond to real exchange mechanics.

A detector returns:
  * None                       - nothing meaningful is happening
  * Signal(action="WATCH")     - a real setup exists but is NOT tradeable yet
                                 (falling knife, chasing, costs too high ...)
                                 Recorded so we can learn which setups later
                                 worked (section 67).
  * Signal(action="BUY")       - every confirmation passed.
"""
from __future__ import annotations

from typing import Optional

from lvt.engine import scoring
from lvt.engine.states import MarketState
from lvt.engine.volatility import MarketSeries, Metrics, linreg, side_metrics
from lvt.fees import fee_per_contract_cents
from lvt.models import MarketInfo, Signal, new_id

EST_COUNT = 10          # contracts assumed when estimating per-contract fees


def edge_math(entry_ask: float, target_bid: float, stop_bid: float, info: MarketInfo,
              slippage_buffer: float) -> tuple[float, float, float]:
    """(expected net cents/contract at target, risk cents/contract at stop, reward:risk)."""
    fin = fee_per_contract_cents(entry_ask, info.fee_type, info.fee_multiplier, EST_COUNT)
    fout_t = fee_per_contract_cents(target_bid, info.fee_type, info.fee_multiplier, EST_COUNT)
    fout_s = fee_per_contract_cents(max(stop_bid, 1.0), info.fee_type, info.fee_multiplier, EST_COUNT)
    net = target_bid - entry_ask - fin - fout_t - slippage_buffer
    risk = entry_ask - stop_bid + fin + fout_s + slippage_buffer
    rr = net / risk if risk > 0 else 0.0
    return round(net, 3), round(risk, 3), round(rr, 3)


class Strategy:
    detector = "abstract"

    def __init__(self, name: str, cfg, version: str, sig_cfg, ecfg, rcfg):
        self.name = name
        self.cfg = cfg
        self.side = cfg.side
        self.version = version
        self.sig_cfg = sig_cfg
        self.ecfg = ecfg
        self.rcfg = rcfg

    def evaluate(self, series: MarketSeries, m: Metrics, state: MarketState,
                 info: MarketInfo, now: float) -> Optional[Signal]:
        raise NotImplementedError

    def _signal(self, now, info, m, state, anchor, **kw) -> Signal:
        return Signal(id=new_id("sig"), ts=now, ticker=info.ticker, event_ticker=info.event_ticker,
                      strategy=self.name, strategy_version=self.version, side=self.side, action="", stage="",
                      state=state.primary, regime=state.regime, anchor=anchor,
                      data_ts=m.ts - m.data_age_s, category=info.category, sport=info.sport, **kw)

    def _finish(self, sig: Signal, failures: list[str], passes: list[str], components: dict) -> Signal:
        sig.components = {k: round(v, 3) for k, v in components.items()}
        sig.score = scoring.score(components)
        if not failures and sig.score < self.cfg.min_score:
            failures.append(f"score {sig.score:.0f} < {self.cfg.min_score}")
        if failures:
            sig.action, sig.stage = "WATCH", "SETUP"
            sig.reasons = failures + passes
        else:
            sig.action, sig.stage = "BUY", "CONFIRMED"
            sig.reasons = passes
        return sig


class DipStrategy(Strategy):
    """LOCAL HIGH -> MEANINGFUL DECLINE -> DECLINE SLOWS -> STABILISATION ->
    REVERSAL CONFIRMATION -> BUY. Distinguishes a dip from a collapse."""
    detector = "dip"

    def evaluate(self, series, m, state, info, now):
        c = self.cfg
        ts, mid, bid, ask = series.view(self.side, now - c.lookback_s)
        if len(ts) < 8:
            return None
        hi = max(mid)
        i_h = max(i for i, y in enumerate(mid) if y == hi)          # latest touch of the high
        if i_h >= len(mid) - 2:
            return None                                             # still at the high
        after = mid[i_h:]
        lo = min(after)
        i_l = i_h + max(i for i, y in enumerate(after) if y == lo)  # latest touch of the low
        drop = hi - lo
        if drop < c.min_drop_cents or hi <= 0 or drop / hi < c.min_drop_pct:
            return None
        decline_min = max((ts[i_l] - ts[i_h]) / 60.0, 1.0 / 60.0)
        decline_vel = drop / decline_min
        if decline_vel < c.min_decline_velocity:
            return None                                             # slow grind, not a dip
        cur_mid, cur_bid, cur_ask = mid[-1], bid[-1], ask[-1]
        bounce = cur_mid - lo
        stabilised_s = now - ts[i_l]
        sm = side_metrics(m, self.side)
        vs = sm["velocity_short"]

        target = min(lo + c.target_retrace_frac * drop, hi - 0.5)
        stop = lo - c.stop_buffer_cents
        net, risk, rr = edge_math(cur_ask, target, stop, info, self.sig_cfg.slippage_buffer_cents)

        failures, passes = [], []

        def check(ok, good, bad):
            (passes if ok else failures).append(good if ok else bad)

        check(stabilised_s >= c.min_stabilize_s,
              f"no new low for {stabilised_s:.0f}s",
              f"falling knife: new low only {stabilised_s:.0f}s ago (need {c.min_stabilize_s}s)")
        check(bounce >= c.min_bounce_cents,
              f"reversed {bounce:.1f}c off the low",
              f"no reversal yet: {bounce:.1f}c off the low (need {c.min_bounce_cents}c)")
        check(bounce <= c.max_retrace_frac * drop,
              f"retraced {bounce / drop:.0%} of the drop, room left",
              f"too late: already retraced {bounce / drop:.0%} of the drop")
        check(vs >= c.max_short_decline_velocity,
              f"short-term slope {vs:+.2f}c/min (decline has stopped)",
              f"continued collapse: short-term slope {vs:+.2f}c/min")
        check(c.min_entry_price <= cur_ask <= c.max_entry_price,
              f"entry {cur_ask:.1f}c inside the price band",
              f"entry {cur_ask:.1f}c outside {c.min_entry_price}-{c.max_entry_price}c")
        check(net >= self.sig_cfg.min_net_edge_cents,
              f"expected net {net:.1f}c/contract after fees and slippage",
              f"edge too thin: {net:.1f}c/contract after costs (need {self.sig_cfg.min_net_edge_cents}c)")
        check(rr >= self.sig_cfg.min_reward_risk,
              f"reward:risk {rr:.2f}",
              f"reward:risk {rr:.2f} < {self.sig_cfg.min_reward_risk}")

        comps = scoring.common_components(m, state.regime, self.ecfg, self.rcfg, sm["imbalance"],
                                          net, self.sig_cfg.min_net_edge_cents)
        comps.update({
            "magnitude": drop / (3.0 * c.min_drop_cents),
            "velocity": decline_vel / (3.0 * c.min_decline_velocity),
            "confirmation": bounce / (2.0 * c.min_bounce_cents),
            "persistence": stabilised_s / (3.0 * c.min_stabilize_s),
        })
        sig = self._signal(now, info, m, state, anchor=f"{self.side}:{ts[i_h]:.0f}:{hi:.1f}",
                           bid=cur_bid, ask=cur_ask, mid=cur_mid, spread=m.spread,
                           target=round(target, 2), stop=round(stop, 2),
                           expected_net_cents=net, reward_risk=rr)
        sig.explanation = {
            "recent_high": f"{hi:.1f}c", "recent_low": f"{lo:.1f}c",
            "decline_cents": round(drop, 2), "decline_pct": f"-{drop / hi:.1%}",
            "decline_velocity": f"{decline_vel:.2f}c/min",
            "reversal_pct": f"+{bounce / lo:.1%}" if lo > 0 else "n/a",
            "stabilised_s": round(stabilised_s, 1), "velocity": f"{vs:+.2f}c/min",
            "volatility": state.regime, "liquidity": f"${m.liquidity_usd:.0f}",
            "volume": round(m.volume_long, 1), "risk_cents": risk,
        }
        return self._finish(sig, failures, passes, comps)


class MomentumStrategy(Strategy):
    """PRICE INCREASE -> PERSISTENCE -> VELOCITY -> ACCELERATION -> VOLUME
    CONFIRMATION -> ENTRY, and never chase an exhausted move."""
    detector = "momentum"

    def evaluate(self, series, m, state, info, now):
        c = self.cfg
        ts, mid, bid, ask = series.view(self.side, now - c.window_s)
        if len(ts) < 8 or ts[-1] - ts[0] < 0.6 * c.window_s:
            return None
        i_lo = min(range(len(mid)), key=lambda i: (mid[i], -i))
        rise = mid[-1] - mid[i_lo]
        if rise < c.min_rise_cents or i_lo >= len(mid) - 3:
            return None
        run_ts, run_mid = ts[i_lo:], mid[i_lo:]
        slope, r2 = linreg(run_ts, run_mid)
        diffs = [b - a for a, b in zip(run_mid, run_mid[1:])]
        ups = sum(1 for d in diffs if d > 1e-9)
        downs = sum(1 for d in diffs if d < -1e-9)
        up_frac = ups / (ups + downs) if (ups + downs) else 0.0
        tail = diffs[-c.min_confirm_obs:]
        confirmed = len(tail) >= c.min_confirm_obs and all(d >= -1e-9 for d in tail) and mid[-1] >= max(run_mid) - 0.5
        half = len(run_ts) // 2
        s1, _ = linreg(run_ts[:half], run_mid[:half]) if half >= 3 else (slope, 0)
        s2, _ = linreg(run_ts[half:], run_mid[half:]) if len(run_ts) - half >= 3 else (slope, 0)
        accel_ok = s2 >= c.accel_ratio * s1 if s1 > 0 else s2 > 0
        vol = series.volume_between(run_ts[0], now)
        sm = side_metrics(m, self.side)
        vs = sm["velocity_short"]
        exhausted = vs < 0.3 * slope

        cur_mid, cur_bid, cur_ask = mid[-1], bid[-1], ask[-1]
        proj = min(max(slope * c.target_minutes, c.min_target_cents), c.max_target_cents)
        target = min(cur_bid + proj, 99.0)
        stop = cur_ask - c.stop_cents
        net, risk, rr = edge_math(cur_ask, target, stop, info, self.sig_cfg.slippage_buffer_cents)

        failures, passes = [], []

        def check(ok, good, bad):
            (passes if ok else failures).append(good if ok else bad)

        check(rise <= c.max_chase_cents, f"rise {rise:.1f}c not yet exhausted",
              f"chasing: already up {rise:.1f}c (max {c.max_chase_cents}c)")
        check(slope >= c.min_velocity, f"velocity {slope:+.2f}c/min",
              f"velocity {slope:+.2f}c/min < {c.min_velocity}")
        check(r2 >= c.min_r2, f"persistent move (r2 {r2:.2f})", f"choppy move (r2 {r2:.2f} < {c.min_r2})")
        check(up_frac >= c.min_up_fraction, f"{up_frac:.0%} of changes were up",
              f"only {up_frac:.0%} of changes were up")
        check(confirmed, f"{c.min_confirm_obs} confirming observations at the high",
              "not confirmed: price not holding its high")
        check(accel_ok, f"accelerating ({s1:+.2f} -> {s2:+.2f}c/min)",
              f"decelerating ({s1:+.2f} -> {s2:+.2f}c/min)")
        check(not exhausted, f"short-term slope {vs:+.2f}c/min still pushing",
              f"exhaustion: short-term slope {vs:+.2f}c/min vs run {slope:+.2f}")
        check(vol >= c.min_volume, f"{vol:.0f} contracts traded in the run",
              f"no volume confirmation: {vol:.0f} contracts (need {c.min_volume})")
        check(c.min_entry_price <= cur_ask <= c.max_entry_price,
              f"entry {cur_ask:.1f}c inside the price band",
              f"entry {cur_ask:.1f}c outside {c.min_entry_price}-{c.max_entry_price}c")
        check(net >= self.sig_cfg.min_net_edge_cents,
              f"expected net {net:.1f}c/contract after fees and slippage",
              f"edge too thin: {net:.1f}c/contract after costs (need {self.sig_cfg.min_net_edge_cents}c)")
        check(rr >= self.sig_cfg.min_reward_risk, f"reward:risk {rr:.2f}",
              f"reward:risk {rr:.2f} < {self.sig_cfg.min_reward_risk}")

        comps = scoring.common_components(m, state.regime, self.ecfg, self.rcfg, sm["imbalance"],
                                          net, self.sig_cfg.min_net_edge_cents)
        comps.update({
            "magnitude": rise / (3.0 * c.min_rise_cents),
            "velocity": slope / (3.0 * c.min_velocity),
            "confirmation": (0.5 if confirmed else 0.0) + (0.5 if accel_ok else 0.0),
            "persistence": 0.5 * r2 + 0.5 * up_frac,
        })
        sig = self._signal(now, info, m, state, anchor=f"{self.side}:{run_ts[0]:.0f}",
                           bid=cur_bid, ask=cur_ask, mid=cur_mid, spread=m.spread,
                           target=round(target, 2), stop=round(stop, 2),
                           expected_net_cents=net, reward_risk=rr)
        sig.explanation = {
            "run_low": f"{mid[i_lo]:.1f}c", "rise_cents": round(rise, 2),
            "velocity": f"{slope:+.2f}c/min", "r2": round(r2, 2), "up_fraction": round(up_frac, 2),
            "acceleration": f"{s1:+.2f}->{s2:+.2f}c/min", "short_velocity": f"{vs:+.2f}c/min",
            "volume": round(vol, 1), "volatility": state.regime,
            "liquidity": f"${m.liquidity_usd:.0f}", "risk_cents": risk,
        }
        return self._finish(sig, failures, passes, comps)


DETECTORS = {"DIP": DipStrategy, "REVERSAL": DipStrategy,
             "MOMENTUM": MomentumStrategy, "DOWNTREND": MomentumStrategy}


def build_strategies(cfg) -> list[Strategy]:
    out = []
    for name, cls in DETECTORS.items():
        sc = cfg.strategies[name]
        if not sc.enabled:
            continue
        out.append(cls(name, sc, cfg.strategy_version(name), cfg.signals, cfg.eligibility, cfg.risk))
    return out
