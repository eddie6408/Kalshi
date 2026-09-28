"""Forecast-derived edges.

Structural arbitrage needs no opinion about the world - the contracts
themselves guarantee the payoff. This is the opposite: the entire trade IS an
opinion, so everything here exists to make that opinion explicit, checkable,
and small enough to survive being wrong.

The question, straight from the brief:

    "What is the true probability of this event occurring, and how does that
     compare with Kalshi's current implied probability?"

    Claude estimates : 72%
    Kalshi YES price : 61c
    -> an 11-percentage-point discrepancy

An estimator answers only the first half. It never sees the balance, never
picks a size, and never places an order. This module prices that answer
against the real order book, sizes it with fractional Kelly scaled by the
estimator's own confidence, and emits the same Opportunity object the
structural detectors emit - so the gate, the risk limits and the execution
manager treat both kinds of trade identically.

Two things are deliberately NOT done here:

  * Nothing is ever called guaranteed. `guaranteed=False` on every
    opportunity this module produces, always. A forecast can be wrong; a
    payout floor cannot. The gate knows the difference.
  * No trade is manufactured to prove the code works. An estimator that
    declines to price a market, or an edge that does not survive fees, simply
    produces nothing.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from dataclasses import dataclass

from .. import data_gates
from .. import ledger as _ledger
from ..alerts import ALERTS, today_key
from ..coherence import Claim, check as check_coherence
from ..depth import DepthBook
from ..edge import contracts_from_kelly, evaluate
from ..fees import FeeModel
from ..models import Market
from ..recorder import book_levels
from ..opportunity import (RISK_SCORE, ArbitrageOpportunity, ExecutionRisk, Leg,
                           OpportunityClass)

log = logging.getLogger(__name__)


def _spread_cents(book, side: str) -> int | None:
    """Ask minus bid on one side of the book, or None if it is one-sided."""
    bid = book.best_price(side, "sell")
    ask = book.best_price(side, "buy")
    if bid is None or ask is None:
        return None
    return int(ask) - int(bid)


@dataclass
class ForecastConfig:
    #: fraction of full Kelly. 0.25 is the usual "I do not fully trust my
    #: own model" setting and is what the brief's risk section implies.
    kelly_fraction: float = 0.25
    max_contracts_per_trade: int = 25
    #: never put more than this share of the bankroll into one forecast trade
    max_bankroll_fraction: float = 0.02
    #: an estimate worth acting on has to be better than a coin flip by at
    #: least this much, in probability points, BEFORE fees
    min_probability_edge: float = 0.05
    min_confidence: float = 0.50
    #: Do not trade a market further from settlement than this.
    #:
    #: THE strongest signal in the 21 September audit, and the only proposed
    #: change that survived it. Brier Skill Score against climatology, by how
    #: far ahead the forecast was made:
    #:
    #: Brier Skill Score against climatology, by how far ahead the forecast
    #: was made, with a 95% interval bootstrapped over CONTRACTS rather than
    #: rows (452 rows are only 74 distinct contracts; treating them as 452
    #: independent observations roughly halves the interval and is wrong):
    #:
    #:      0-6h    n=155  33 contracts  BSS +0.350  95% [+0.141, +0.476]
    #:      6-12h   n= 53  30 contracts  BSS +0.005  95% [-0.248, +0.129]
    #:     12-24h   n=209  56 contracts  BSS -0.010  95% [-0.192, +0.098]
    #:      24h+    n= 35                BSS +0.027
    #:      ALL     n=452  74 contracts  BSS +0.110  95% [-0.018, +0.198]
    #:
    #: Only the 0-6h interval excludes zero. Everything past six hours is
    #: statistically indistinguishable from a climate table, and a zero-edge
    #: forecast is not break-even after fees - Kalshi charges
    #: 0.07*n*P*(1-P) per leg, so trading no edge is a guaranteed slow loss.
    #: That is why the gate is 6 and not 12: 12 was the point estimate's
    #: answer, 6 is the confidence interval's.
    #:
    #: Uncertainty is flat across those buckets (0.164 / 0.175 / 0.192 /
    #: 0.191), so this is not "the question got easier near the end of the
    #: day" - the skill really does collapse.
    #:
    #: This is a TRADING gate, not a forecasting one: the estimator still
    #: prices and logs long-lead markets, so the record keeps growing and can
    #: justify widening this later. Raise it only when the 6-12h interval
    #: excludes zero on its own evidence.
    #:
    #: Set to 0 to disable. Discovery still allows 336 hours, so without this
    #: the bot will price a market two weeks out and act on it.
    max_lead_hours: float = 6.0
    #: Refuse to trade on the fallback data source. The NWS gridpoint path
    #: returns ONE number and the estimator invents a spread for it; that
    #: invented spread then flows into the edge, the size and the Brier score
    #: as if it had been measured. On 18 September the ensemble came back
    #: unusable five times and nothing downstream knew.
    require_primary_source: bool = True
    #: refuse markets whose book is too thin to exit
    min_book_depth: int = 10
    #: The widest bid/ask we will cross. A book quoting 14c bid and 83c ask is
    #: not a market, it is two people who disagree about what the contract is
    #: worth. Buying the ask there is an instant 69-cent paper loss - which is
    #: exactly what happened to KXHIGHCHI-26SEP19-T72 on 19 September: bought
    #: NO at 83c, marked at the 14c bid, stopped out THIRTEEN SECONDS later
    #: for -$8.24. Repeated five times, that one market lost $24 - more than
    #: every other trade the bot made that day put together, which were net
    #: positive.
    max_spread_cents: int = 15
    #: The largest disagreement with the market we are willing to act on, at
    #: FULL confidence. The allowance is scaled by the estimator's own
    #: confidence, so a shaky estimate is allowed to disagree far less.
    #:
    #: Past this, a gap is much more likely to be a broken model than a
    #: mispriced market. The Kalshi demo board produced a 30-point "edge" at
    #: 56%% confidence that turned out to be an incoherent distribution.
    max_probability_edge: float = 0.25
    #: refuse a whole event whose probabilities contradict each other
    require_coherent_distribution: bool = True
    #: Stay out of the tails. Below ~5c a market is a lottery ticket: the
    #: absolute edge is tiny, the percentage return looks enormous, and a
    #: forecast distribution's tail is mostly an artifact of the sigma
    #: assumption rather than anything the data supports.
    min_price_cents: int = 5
    max_price_cents: int = 95
    #: Cap disagreement in RATIO as well as in points. 9.5% against a 2%
    #: market is only 7.5 points but it is 4.75x the market's number - the
    #: points cap waves that straight through while the ratio catches it.
    max_relative_disagreement: float = 2.5
    #: A hard dollar cap, because a contract cap is not a risk limit: 25
    #: contracts is $0.50 at 2c and $24.75 at 99c.
    max_cost_per_trade_usd: float = 25.0

    @classmethod
    def from_dict(cls, d: dict | None) -> "ForecastConfig":
        d = d or {}
        return cls(
            kelly_fraction=float(d.get("kelly_fraction", 0.25)),
            max_contracts_per_trade=int(d.get("max_contracts_per_trade", 25)),
            max_bankroll_fraction=float(d.get("max_bankroll_fraction", 0.02)),
            min_probability_edge=float(d.get("min_probability_edge", 0.05)),
            min_confidence=float(d.get("min_confidence", 0.50)),
            max_lead_hours=float(d.get("max_lead_hours", 6.0)),
            require_primary_source=bool(d.get("require_primary_source", True)),
            min_book_depth=int(d.get("min_book_depth", 10)),
            max_spread_cents=int(d.get("max_spread_cents", 15)),
            max_probability_edge=float(d.get("max_probability_edge", 0.25)),
            require_coherent_distribution=bool(
                d.get("require_coherent_distribution", True)),
            min_price_cents=int(d.get("min_price_cents", 5)),
            max_price_cents=int(d.get("max_price_cents", 95)),
            max_relative_disagreement=float(
                d.get("max_relative_disagreement", 2.5)),
            max_cost_per_trade_usd=float(d.get("max_cost_per_trade_usd", 25.0)),
        )


@dataclass
class Skip:
    """Why a market was looked at and not traded. The forecast equivalent of
    a near miss - without it, "no forecast trades" is unreadable."""
    ticker: str
    estimator: str
    reason: str
    detail: str = ""

    def describe(self) -> str:
        return f"{self.ticker} [{self.estimator}] {self.reason}{': ' + self.detail if self.detail else ''}"


class ForecastStrategy:
    """Wraps one estimator and turns its estimates into priced opportunities."""

    model_free = False

    def __init__(self, estimator, fees: FeeModel, cfg: ForecastConfig | None = None,
                 journal=None, gates: data_gates.GateConfig | None = None,
                 monitor=None, market_skill=None):
        self.estimator = estimator
        self.fees = fees
        self.cfg = cfg or ForecastConfig()
        #: Package 14A phase 4 data-quality gates. None = not evaluated.
        self.gates = gates
        self.monitor = monitor
        #: set by the engine; recorded on every ledger row
        self.execution_mode = ""
        #: what _price_one knows about the market it is on, so a skip can be
        #: recorded with the probability and the book it was judged against
        self._ctx: dict = {}
        #: Does the model beat the market for this city and lead time?
        #: (kalshibot/market_skill.py) None = not checked.
        self.market_skill = market_skill
        self._checked: set[tuple] = set()
        self.skips: list[Skip] = []
        #: Where forecasts go to be scored later. Every market we price is
        #: recorded, traded or not - scoring only the traded ones would
        #: measure the gate rather than the forecast, and would throw away
        #: most of the data.
        self.journal = journal
        self._logged: set[tuple] = set()
        #: Package 3a, set by arb.py. The recorder keeps the demo book each
        #: market was priced against; the production watch reads the same
        #: market on production (off the trading path) and hands back its
        #: latest book for the benchmark. None = off.
        self.recorder = None
        self.production_watch = None

    @property
    def name(self) -> str:
        return getattr(self.estimator, "name", "forecast")

    def candidate_series(self) -> list[str]:
        fn = getattr(self.estimator, "candidate_series", None)
        return list(fn()) if callable(fn) else []

    # ------------------------------------------------------------------
    def find(self, markets: list[Market], books: dict[str, DepthBook],
             bankroll_usd: float) -> list[ArbitrageOpportunity]:
        tradable = [m for m in markets if m.ticker in books]
        if not tradable:
            return []

        cached: dict[str, object] = {}
        if self.cfg.require_coherent_distribution:
            problems = self._incoherent(tradable, cached)
            if problems:
                # Refuse the WHOLE event, not the offending market. If the
                # distribution contradicts itself, every probability drawn
                # from it is suspect - including the ones that happen to look
                # reasonable.
                for m in tradable:
                    self._ctx = {"ticker": m.ticker, "book": books.get(m.ticker),
                                 "estimate": cached.get(m.ticker)}
                    self._skip(m, "event distribution is incoherent",
                               problems[0])
                log.warning("FORECAST  %s refused %s: %s", self.name,
                            tradable[0].event_ticker, "; ".join(problems[:3]))
                return []

        out: list[ArbitrageOpportunity] = []
        for m in tradable:
            op = self._price_one(m, books[m.ticker], bankroll_usd,
                                 cached.get(m.ticker))
            self._record(m, cached.get(m.ticker), books[m.ticker],
                         traded=op is not None)
            self._observe(m, books[m.ticker], op)
            if op is not None:
                out.append(op)
        return out

    def _prod_quote(self, ticker: str):
        """The production book for this market if one was read in the last
        five minutes, else None. Never makes a request."""
        if self.production_watch is None:
            return None
        try:
            return self.production_watch.latest(ticker, max_age=300.0)
        except Exception:  # noqa: BLE001
            return None

    def _observe(self, m: Market, book: DepthBook, op) -> None:
        """Package 3a: record the demo book this market was priced against,
        and ask for the same market to be watched on production - with our
        verdict, for the maker shadow (C2 M0). Never blocks, never raises."""
        try:
            ctx = self._ctx if self._ctx.get("ticker") == m.ticker else {}
            est = ctx.get("estimate")
            usable = est is not None and getattr(est, "usable", False)
            # Only this strategy's own markets. The exchange-wide arbitrage
            # sweep hands every event on the exchange to find() - gold, NFL,
            # everything - and watching those spent the production budget on
            # markets this strategy never prices (first hour live, 205 watched).
            series = self.candidate_series()
            name = (m.series_ticker or m.ticker).upper()
            own = any(name.startswith(p.upper()) for p in series) if series else usable
            if not own:
                return
            if self.recorder is not None:
                self.recorder.record("demo_book", {"m": m.ticker, "src": self.name,
                                                   "b": book_levels(book)})
            if self.production_watch is None:
                return
            info = None
            if usable:
                ev = est.evidence or {}
                if op is not None:
                    stage, reason = "ACCEPTED", ""
                else:
                    last = (self.skips[-1] if self.skips
                            and self.skips[-1].ticker == m.ticker else None)
                    reason = last.reason if last else ""
                    st = _ledger.stage_for(reason) if reason else None
                    stage = st.value if st else ("UNMAPPED" if reason else "")
                info = {"q": est.probability_yes, "stage": stage, "reason": reason,
                        "lead": ev.get("lead_hours"),
                        "series": m.series_ticker or m.ticker.split("-")[0],
                        "close_ts": m.close_time.timestamp() if m.close_time else None}
            self.production_watch.watch(m.ticker, info)
            if op is not None:
                q = self._prod_quote(m.ticker)
                if q is not None:
                    op.evidence = dict(op.evidence or {},
                                       prod_touch={"yes_ask": q.yes_ask,
                                                   "yes_bid": q.yes_bid})
        except Exception as exc:  # noqa: BLE001
            log.debug("could not observe %s: %s", m.ticker, exc)

    def _record(self, m: Market, estimate, book: DepthBook, traded: bool) -> None:
        """Write down what we thought, before anyone knows if it was right.

        Deduplicated per ticker per hour: a market re-priced every scan for a
        day would otherwise contribute a hundred near-identical rows and drown
        out everything else in the Brier average.
        """
        if self.journal is None or estimate is None or not estimate.usable:
            return
        key = (m.ticker, datetime.now(timezone.utc).strftime("%Y%m%d%H"))
        if key in self._logged:
            return
        self._logged.add(key)

        ev = estimate.evidence or {}
        # Both sides. The ask alone is what we would PAY, not what the market
        # thinks: on a 40/55 book the ask overstates the market's probability
        # by 7.5 points, and scoring the market on its ask makes the market
        # look worse than it is. The mid is the market's probability, and the
        # audit needs both numbers to compute it.
        yes_ask = book.best_price("yes", "buy") if book else None
        yes_bid = book.best_price("yes", "sell") if book else None
        # Package 3a: the same market on production, for the second benchmark.
        prod = self._prod_quote(m.ticker)
        try:
            self.journal.log_probability(
                strategy=f"forecast:{self.name}", estimator=self.name,
                series=m.series_ticker or "", ticker=m.ticker,
                our_probability=estimate.probability_yes,
                confidence=estimate.confidence,
                market_yes_cents=yes_ask,
                market_yes_bid_cents=yes_bid,
                target_date=str(ev.get("target_date", "")),
                strike=str(ev.get("strike", "")),
                lead_hours=ev.get("lead_hours"),
                traded=traded,
                provenance=ev.get("provenance_ids"),
                model=ev.get("model"),
                alt_probability=ev.get("alt_probability"),
                prod_yes_ask_cents=prod.yes_ask if prod else None,
                prod_yes_bid_cents=prod.yes_bid if prod else None,
                prod_read_at=(datetime.fromtimestamp(prod.read_at, timezone.utc).isoformat()
                              if prod else None),
            )
            # The distribution behind the probability, for the second kind of
            # scoring: was the mean right, was sigma wide enough.
            if "ensemble_mean_f" in ev:
                dist_key = (m.series_ticker, ev.get("target_date"),
                            datetime.now(timezone.utc).strftime("%Y%m%d%H"))
                if dist_key not in self._logged:
                    self._logged.add(dist_key)
                    self.journal.log_forecast(
                        series=m.series_ticker or "",
                        station=str(ev.get("station", "")),
                        target_date=str(ev.get("target_date", "")),
                        mean_f=float(ev.get("ensemble_mean_f", 0.0)),
                        sigma_f=float(ev.get("sigma_used_f", 0.0)),
                        members=int(ev.get("ensemble_members", 0)),
                        lead_hours=float(ev.get("lead_hours", 0.0)),
                        source=ev.get("source"),
                        provenance=ev.get("provenance_ids"),
                    )
        except Exception as exc:  # noqa: BLE001
            log.debug("could not record the forecast for %s: %s", m.ticker, exc)

    def _incoherent(self, markets: list[Market], cached: dict) -> list[str]:
        """Ask the estimator for every probability in this event and check
        them against each other before trusting any of them.

        Estimates are cached for the pricing pass so each market is estimated
        exactly once per scan.
        """
        parse = getattr(self.estimator, "parse_strike", None)
        if not callable(parse):
            return []          # estimator cannot describe its strikes
        claims = []
        for m in markets:
            est = self.estimator.estimate(m)
            cached[m.ticker] = est
            if not est.usable:
                continue
            lo, hi, _label = parse(m)
            if lo is None and hi is None:
                continue
            claims.append(Claim(m.ticker, lo, hi, est.probability_yes))
        return check_coherence(claims)

    def _record_checks(self, m: Market, ev: dict, result) -> None:
        """One journal row per distinct forecast (not per market - every
        strike in a city shares the same forecast), with every gate verdict.
        This is what the conflict threshold is later fitted from."""
        key = (m.series_ticker, ev.get("target_date"),
               tuple(ev.get("provenance_ids") or ()))
        if key in self._checked:
            return
        self._checked.add(key)
        if len(self._checked) > 5000:
            self._checked.clear()
        for v in result.verdicts:
            log.info("GATE  %s %s %s: %s", v.label, m.series_ticker,
                     ev.get("target_date"), v.detail)
        if self.journal is None:
            return
        try:
            self.journal.log_data_check(
                series=m.series_ticker or "", target_date=str(ev.get("target_date", "")),
                lead_hours=ev.get("lead_hours"),
                ensemble_f=ev.get("ensemble_mean_f"),
                cross_check_f=ev.get("cross_check_f"),
                diff_f=ev.get("cross_check_diff_f"),
                forecast_age_seconds=ev.get("forecast_age_seconds"),
                observation_age_seconds=ev.get("observation_age_seconds"),
                observation_suspect=ev.get("observation_suspect"),
                verdicts=[v.label for v in result.verdicts],
                enforced=result.blocked is not None,
                provenance=ev.get("provenance_ids"))
        except Exception as exc:  # noqa: BLE001
            log.debug("could not record data checks for %s: %s", m.ticker, exc)

    def _skip(self, m: Market, reason: str, detail: str = "") -> None:
        self.skips.append(Skip(m.ticker, self.name, reason, detail))
        self._ledger_skip(m, reason, detail)

    def _ledger_skip(self, m: Market, reason: str, detail: str) -> None:
        """Package 2: record the skip - with what was known - once per market,
        reason and hour. Never blocks anything: a skip trades nothing."""
        if self.journal is None or not hasattr(self.journal, "ledger_record"):
            return
        try:
            ctx = self._ctx if self._ctx.get("ticker") == m.ticker else {}
            book, est = ctx.get("book"), ctx.get("estimate")
            touch = {}
            if book is not None:
                for side in ("yes", "no"):
                    touch[f"{side}_ask"] = book.best_price(side, "buy")
                    touch[f"{side}_bid"] = book.best_price(side, "sell")
            prod = self._prod_quote(m.ticker)
            if prod is not None:
                touch["prod_yes_ask"], touch["prod_yes_bid"] = prod.yes_ask, prod.yes_bid
            usable = est is not None and getattr(est, "usable", False)
            ev = (est.evidence or {}) if usable else {}
            stub = ArbitrageOpportunity.stub(
                strategy_type=f"forecast:{self.name}",
                event_id=m.event_ticker or m.ticker, ticker=m.ticker,
                stage=_ledger.stage_for(reason) or "UNMAPPED", reason=reason,
                detail=detail,
                opportunity_class=(OpportunityClass.WEATHER_FORECAST
                                   if self.name.startswith("weather")
                                   else OpportunityClass.FORECAST),
                probability_yes=est.probability_yes if usable else None,
                touch=touch, settlement_time=m.close_time,
                model_version=str(ev.get("model_version") or ""),
                fee_schedule_version=self.fees.version,
                evidence={"lead_hours": ev.get("lead_hours"), "detail": detail[:200]})
            stub.confidence = est.confidence if usable else 0.0
            stub.execution_mode = self.execution_mode
            hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")
            self.journal.ledger_record(stub, dedupe_key=f"{m.ticker}|{reason}|{hour}")
        except Exception as exc:  # noqa: BLE001
            log.debug("could not record the skip of %s: %s", m.ticker, exc)

    def _price_one(self, m: Market, book: DepthBook, bankroll_usd: float,
                   estimate=None) -> ArbitrageOpportunity | None:
        self._ctx = {"ticker": m.ticker, "book": book, "estimate": estimate}
        yes_ask = book.best_price("yes", "buy")
        no_ask = book.best_price("no", "buy")
        if yes_ask is None and no_ask is None:
            self._skip(m, "no book")
            return None

        if estimate is None:
            estimate = self.estimator.estimate(m)
            self._ctx["estimate"] = estimate
        if not estimate.usable:
            self._skip(m, "estimator declined", estimate.skip_reason or "")
            return None
        ev = estimate.evidence or {}
        # Data checks are RECORDED for every forecast the estimator prices,
        # whatever its lead time - the conflict threshold is fitted from
        # these rows, and most of the day every market is past the lead gate.
        gate_result = None
        if self.gates is not None:
            gate_result = data_gates.evaluate(ev, self.gates, self.monitor, self.name)
            self._record_checks(m, ev, gate_result)
        lead = ev.get("lead_hours")
        if (self.cfg.max_lead_hours > 0 and lead is not None
                and float(lead) > self.cfg.max_lead_hours):
            self._skip(m, "too far out to have skill",
                       f"{float(lead):.0f}h lead > {self.cfg.max_lead_hours:.0f}h; "
                       f"measured skill collapses past this")
            return None

        if self.cfg.require_primary_source and ev.get("degraded_source"):
            self._skip(m, "degraded data source",
                       f"{ev.get('source', 'unknown')} with "
                       f"{ev.get('ensemble_members', 0)} members - the spread "
                       f"would be invented, not measured")
            return None

        # --- is the outside data good enough to trade on? (14A phase 4) ---
        # Evaluated and recorded above, for every lead time; acted on here,
        # only for markets inside the window we would actually trade.
        if gate_result is not None:
            blocked = gate_result.blocked
            if blocked is not None:
                self._skip(m, blocked.gate.lower().replace("_", " "), blocked.detail)
                if blocked.gate == "DATA_CONFLICT":
                    series = m.series_ticker or m.ticker.split("-")[0]
                    ALERTS.send(
                        today_key("conflict", series),
                        f"Data conflict: {series}",
                        f"{m.ticker}: {blocked.detail}. Not trading this city "
                        f"until the sources agree. (Further conflicts today are "
                        f"logged, not alerted.)",
                        priority="high", tags="warning")
                return None

        # --- does the model beat the market here? -------------------------
        # Checked before any edge is computed: an "edge" measured by a model
        # the market out-forecasts is the model's error, not the market's.
        if self.market_skill is not None:
            ok, why = self.market_skill.may_enter(
                m.series_ticker or m.ticker.split("-")[0], lead)
            if not ok:
                self._skip(m, "market forecasts better here", why)
                return None

        if estimate.confidence < self.cfg.min_confidence:
            self._skip(m, "low confidence",
                       f"{estimate.confidence:.2f} < {self.cfg.min_confidence:.2f}")
            return None

        q_yes = estimate.probability_yes
        edge = evaluate(q_yes, yes_ask or 0, no_ask or 0, self.fees)
        if edge.side == "none":
            self._skip(m, "no two-sided market")
            return None

        # win_probability is already the probability of the side we would buy.
        implied = edge.price_cents / 100.0
        raw_edge = edge.win_probability - implied
        if raw_edge < self.cfg.min_probability_edge:
            self._skip(m, "edge too small",
                       f"{raw_edge * 100:+.1f}pts < "
                       f"{self.cfg.min_probability_edge * 100:.1f}pts")
            return None
        if edge.edge_after_fees <= 0:
            self._skip(m, "fees eat the edge",
                       f"{raw_edge * 100:+.1f}pts gross, "
                       f"{edge.edge_after_fees * 100:+.1f}pts net")
            return None

        # How far are we allowed to disagree with the market? As far as we
        # actually trust ourselves, and no further. A 56%-confidence estimate
        # claiming a 30-point mispricing is describing its own error, not the
        # market's.
        allowance = self.cfg.max_probability_edge * estimate.confidence
        if raw_edge > allowance:
            self._skip(m, "disagreement too large to trust",
                       f"{raw_edge * 100:+.1f}pts against a "
                       f"{allowance * 100:.1f}pt allowance at "
                       f"{estimate.confidence:.0%} confidence")
            return None

        # --- the tails ---------------------------------------------------
        # A points-based cap does not protect the extremes. The bot bought 25
        # contracts of "Miami 94F or above" at 2c because it put 9.5% on a
        # market priced at 2% - only 7.5 points, so the cap allowed it, but
        # nearly five times what the market said. Out there the model is
        # almost entirely its own sigma assumption, and the market has real
        # money behind its number.
        if not (self.cfg.min_price_cents <= edge.price_cents
                <= self.cfg.max_price_cents):
            self._skip(m, "price is in the tail",
                       f"{edge.price_cents}c outside "
                       f"{self.cfg.min_price_cents}-{self.cfg.max_price_cents}c")
            return None

        ratio = (edge.win_probability / implied) if implied > 0 else float("inf")
        if ratio > self.cfg.max_relative_disagreement:
            self._skip(m, "disagreement too large in relative terms",
                       f"{edge.win_probability * 100:.1f}% against a "
                       f"{implied * 100:.1f}% market is {ratio:.1f}x, over "
                       f"{self.cfg.max_relative_disagreement:.1f}x")
            return None

        available = book.max_fillable(edge.side, "buy")
        if available < self.cfg.min_book_depth:
            self._skip(m, "book too thin to exit",
                       f"{available} < {self.cfg.min_book_depth}")
            return None

        # How far apart are the two sides of THIS side's book? We buy at the
        # ask and can only ever leave at the bid, so the spread is a cost we
        # pay the instant we trade, before being right or wrong about anything.
        spread = _spread_cents(book, edge.side)
        if spread is not None and spread > self.cfg.max_spread_cents:
            self._skip(m, "spread too wide to cross",
                       f"{spread}c wide (bid {book.best_price(edge.side, 'sell')}c "
                       f"/ ask {book.best_price(edge.side, 'buy')}c) > "
                       f"{self.cfg.max_spread_cents}c - buying here is an "
                       f"instant {spread}c paper loss")
            return None

        wanted = contracts_from_kelly(
            edge.win_probability, edge.price_cents, bankroll_usd,
            self.cfg.kelly_fraction, estimate.confidence)
        # Three independent caps: Kelly, the hard per-trade cap, the bankroll
        # share, and what the book can actually fill.
        price_usd = max(0.01, edge.price_cents / 100.0)
        bankroll_cap = int(bankroll_usd * self.cfg.max_bankroll_fraction / price_usd)
        # A contract cap is not a risk limit - 25 contracts is $0.50 at 2c and
        # $24.75 at 99c. Cap the money too.
        dollar_cap = int(self.cfg.max_cost_per_trade_usd / price_usd)
        quantity = min(wanted, self.cfg.max_contracts_per_trade,
                       bankroll_cap, dollar_cap, available)
        if quantity <= 0:
            self._skip(m, "sized to zero",
                       f"kelly {wanted}, bankroll cap {bankroll_cap}, "
                       f"book {available}")
            return None

        walk = book.walk(edge.side, "buy", quantity)
        if not walk.complete or walk.filled <= 0:
            self._skip(m, "book moved while pricing")
            return None

        # Re-price against the BLENDED cost of actually taking that size,
        # not the top of book. Walking deeper always makes the edge worse.
        vwap = walk.vwap_cents
        implied_at_size = vwap / 100.0
        gross_edge = quantity * (edge.win_probability - implied_at_size)
        fee = self.fees.entry_fee_usd(vwap, quantity)
        ev = gross_edge - fee
        if ev <= 0:
            self._skip(m, "edge gone at size",
                       f"vwap {vwap:.1f}c, EV ${ev:+.2f}")
            return None

        cost = quantity * vwap / 100.0
        leg = Leg(ticker=m.ticker, side=edge.side, action="buy",
                  quantity=quantity, limit_price_cents=walk.worst_price_cents,
                  vwap_cents=vwap, available=available,
                  title=m.subtitle or m.title, walk=walk,
                  best_ask_cents=book.best_price(edge.side, "buy"),
                  best_bid_cents=book.best_price(edge.side, "sell"))
        ev_info = estimate.evidence or {}
        problems = [p for p in (
            "degraded source" if ev_info.get("degraded_source") else "",
            f"observation: {ev_info.get('observation_suspect')}"
            if ev_info.get("observation_suspect") else "") if p]

        return ArbitrageOpportunity(
            strategy_type=f"forecast:{self.name}",
            event_id=m.event_ticker or m.ticker,
            market_tickers=[m.ticker],
            legs=[leg],
            quantity=quantity,
            gross_cost=cost,
            # A forecast has NO payout floor. Leaving this at zero is what
            # stops the gate from treating it like an arbitrage.
            guaranteed_min_payout=0.0,
            max_payout=quantity * 1.00,
            gross_edge=gross_edge,
            estimated_fees=fee,
            net_edge=ev,
            net_edge_per_contract=ev / quantity,
            expected_return_pct=100.0 * ev / cost if cost else 0.0,
            execution_risk=ExecutionRisk.NONE,      # single leg
            guaranteed=False,                        # ALWAYS. Never negotiable.
            rationale=(
                f"{self.name} puts {edge.side.upper()} at "
                f"{edge.win_probability * 100:.1f}%; the book prices it at "
                f"{vwap:.1f}c ({implied_at_size * 100:.1f}%), a "
                f"{(edge.win_probability - implied_at_size) * 100:+.1f}-point "
                f"discrepancy worth ${ev:+.2f} after ${fee:.2f} of fees"),
            evidence={**estimate.evidence,
                      "estimator": self.name,
                      "probability_yes": round(q_yes, 4),
                      "confidence": round(estimate.confidence, 3),
                      "top_of_book_cents": edge.price_cents,
                      "vwap_cents": round(vwap, 2)},
            max_executable_quantity=available,
            min_profitable_quantity=1,
            optimal_quantity=quantity,
            fee_drag_pct=100.0 * fee / gross_edge if gross_edge > 0 else 999.0,
            probability_yes=q_yes,
            implied_probability=implied_at_size if edge.side == "yes"
            else 1.0 - implied_at_size,
            confidence=estimate.confidence,
            estimator=self.name,
            ev_usd=ev,
            # --- Package 1 ---
            opportunity_class=(OpportunityClass.WEATHER_FORECAST
                               if self.name.startswith("weather")
                               else OpportunityClass.FORECAST),
            direction=f"buy {edge.side}",
            fair_probability=edge.win_probability,
            market_probability=implied_at_size,
            settlement_time=m.close_time,
            liquidity={"depth_at_best": (walk.levels[0].contracts if walk.levels else 0),
                       "available": available},
            data_quality="OK" if not problems else "DEGRADED: " + "; ".join(problems),
            source_quality_tier=ev_info.get("source_tier"),
            risk_score=RISK_SCORE[ExecutionRisk.NONE],
            model_version=str(ev_info.get("model_version") or f"{self.name}@unversioned"),
            fee_schedule_version=self.fees.version,
        )
