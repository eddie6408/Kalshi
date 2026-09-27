"""Model-free structural arbitrage. Works on any Kalshi market.

These trades need no forecast and no opinion about the world. They look for
prices that contradict each other, where the contracts themselves guarantee
the payoff.

Every number here comes from walking the real order book. Top-of-book pricing
would report arbitrage that does not exist at size.

Detectors, in descending order of how often they appear:

  SameMarketArbitrage      buy YES and NO on one market for under $1.00
  LadderInversionArbitrage nested thresholds priced out of order
  ExclusiveBasketArbitrage buy NO on every outcome of an exclusive event

Strategies NEVER place orders. They identify and price. The gate decides
whether it can be traded and the execution manager decides how.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import timedelta

from ..depth import DepthBook
from ..fees import FeeModel
from ..models import Market
from ..opportunity import (RISK_SCORE, ArbitrageOpportunity, ExecutionRisk, Leg,
                           OpportunityClass)

#: Bump when a detector's pricing logic changes, so results stay attributable.
STRUCTURAL_MODEL_VERSION = "1"
from ..quantity import analyse

log = logging.getLogger(__name__)

#: Any number, so a threshold can be stripped out of a ticker or a title.
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass
class ArbConfig:
    max_legs: int = 14
    allow_yes_basket: bool = False
    require_exclusive_flag: bool = True
    max_contracts_per_trade: int = 1
    min_net_edge_per_contract: float = 0.005
    safety_buffer_usd: float = 0.0
    ladder_close_tolerance_minutes: int = 5
    #: The quantity analyser maximises net edge, which always wants the
    #: largest size the book allows - and that is exactly the size with the
    #: worst execution risk, because risk is graded on depth HEADROOM. Left
    #: alone it hands the gate trades the gate must refuse. When this is set,
    #: _build steps the size down to the largest profitable quantity that the
    #: gate's risk tier will actually accept.
    respect_execution_risk: bool = True
    max_execution_risk: str = "medium"

    @classmethod
    def from_dict(cls, d: dict | None) -> "ArbConfig":
        d = d or {}
        return cls(
            max_legs=int(d.get("max_legs", 14)),
            allow_yes_basket=bool(d.get("allow_yes_basket", False)),
            require_exclusive_flag=bool(d.get("require_exclusive_flag", True)),
            max_contracts_per_trade=int(d.get("max_contracts_per_trade", 1)),
            min_net_edge_per_contract=float(d.get("min_net_edge_per_contract", 0.005)),
            safety_buffer_usd=float(d.get("safety_buffer_usd", 0.0)),
            ladder_close_tolerance_minutes=int(d.get("ladder_close_tolerance_minutes", 5)),
            respect_execution_risk=bool(d.get("respect_execution_risk", True)),
            max_execution_risk=str(d.get("max_execution_risk", "medium")).lower(),
        )


@dataclass
class NearMiss:
    """A structural relationship that was real but not crossed.

    "0 opportunities" is ambiguous on its own - it could mean the board was
    efficient, or it could mean the scanner is broken. Recording how close the
    best structures came turns silence into evidence.
    """
    strategy: str
    event_id: str
    tickers: list[str]
    cost_cents: int
    floor_cents: int

    @property
    def gap_cents(self) -> int:
        """How far the cost would have to fall to cross. Always > 0."""
        return self.cost_cents - self.floor_cents

    def describe(self) -> str:
        return (f"{self.strategy} {'/'.join(self.tickers)} "
                f"{self.cost_cents}c for a {self.floor_cents}c floor "
                f"({self.gap_cents}c away)")


_RISK_ORDER = {ExecutionRisk.NONE: 0, ExecutionRisk.LOW: 1,
               ExecutionRisk.MEDIUM: 2, ExecutionRisk.HIGH: 3}


def _risk_for(n_legs: int, tightest_depth: int, wanted: int) -> ExecutionRisk:
    if n_legs <= 1:
        return ExecutionRisk.NONE
    headroom = (tightest_depth / wanted) if wanted else 0
    if n_legs == 2 and headroom >= 5:
        return ExecutionRisk.LOW
    if n_legs <= 4 and headroom >= 2:
        return ExecutionRisk.MEDIUM
    return ExecutionRisk.HIGH


class _Detector:
    """Shared plumbing: turn a leg plan into a fully priced opportunity."""
    name = "base"

    #: populated by find(); the scanner drains it after each event
    near_misses: list

    def __init__(self, fees: FeeModel, cfg: ArbConfig):
        self.fees = fees
        self.cfg = cfg
        self.near_misses: list[NearMiss] = []

    def _size_within_risk(self, qa, n_legs: int, tightest_depth: int,
                          optimal: int) -> int:
        """The largest still-profitable size whose execution risk is
        acceptable. Trading a real edge smaller beats not trading it."""
        try:
            allowed = _RISK_ORDER[ExecutionRisk(self.cfg.max_execution_risk)]
        except ValueError:
            return optimal
        if _RISK_ORDER[_risk_for(n_legs, tightest_depth, optimal)] <= allowed:
            return optimal
        candidates = sorted((p for p in qa.curve
                             if 0 < p.sets <= optimal and p.net_edge > 0),
                            key=lambda p: -p.sets)
        for p in candidates:
            if _RISK_ORDER[_risk_for(n_legs, tightest_depth, p.sets)] <= allowed:
                log.debug("sized down %d -> %d sets to meet %s execution risk",
                          optimal, p.sets, self.cfg.max_execution_risk)
                return p.sets
        # No size helps. Beyond four legs the risk tier is set by leg count
        # alone, so shrinking cannot rescue a wide basket. Emit it at the
        # optimal size anyway and let the gate refuse it OUT LOUD - a silent
        # drop here would just make the opportunity disappear from the scan
        # report with no reason given.
        return optimal

    def _build(
        self,
        strategy_type: str,
        event_id: str,
        plan: list[tuple[str, str, DepthBook, str]],   # (ticker, side, book, title)
        payout_floor_per_set: float,
        payout_max_per_set: float,
        rationale: str,
        evidence: dict,
    ) -> ArbitrageOpportunity | None:
        if not plan or len(plan) > self.cfg.max_legs:
            return None

        depths = [book.max_fillable(side, "buy") for _, side, book, _ in plan]
        max_exec = min(depths) if depths else 0
        if max_exec <= 0:
            return None
        max_exec = min(max_exec, self.cfg.max_contracts_per_trade)

        def cost_fn(sets: int) -> tuple[float, float]:
            total = 0
            for _, side, book, _ in plan:
                w = book.walk(side, "buy", sets)
                if not w.complete:
                    return (0.0, 0.0)
                total += w.total_cost_cents
            return (total / 100.0, payout_floor_per_set * sets)

        def leg_prices_fn(sets: int):
            out = []
            for _, side, book, _ in plan:
                w = book.walk(side, "buy", sets)
                out.append((w.vwap_cents, sets))
            return out

        qa = analyse(cost_fn, leg_prices_fn, self.fees, max_exec,
                     min_net_per_set=self.cfg.min_net_edge_per_contract)
        if qa.optimal <= 0:
            return None

        sets = qa.optimal
        if self.cfg.respect_execution_risk and len(plan) > 1:
            sets = self._size_within_risk(qa, len(plan), min(depths), sets)
        point = qa.at(sets)
        gross_cost, payout_floor = cost_fn(sets)
        if gross_cost <= 0:
            return None

        legs: list[Leg] = []
        for ticker, side, book, title in plan:
            w = book.walk(side, "buy", sets)
            legs.append(Leg(
                ticker=ticker, side=side, action="buy", quantity=sets,
                limit_price_cents=w.worst_price_cents, vwap_cents=w.vwap_cents,
                available=book.max_fillable(side, "buy"), title=title, walk=w,
                best_ask_cents=book.best_price(side, "buy"),
                best_bid_cents=book.best_price(side, "sell"),
            ))

        net = point.net_edge - self.cfg.safety_buffer_usd
        return ArbitrageOpportunity(
            strategy_type=strategy_type,
            event_id=event_id,
            market_tickers=list(dict.fromkeys(t for t, _, _, _ in plan)),
            legs=legs,
            quantity=sets,
            gross_cost=gross_cost,
            guaranteed_min_payout=payout_floor,
            max_payout=payout_max_per_set * sets,
            gross_edge=point.gross_edge,
            estimated_fees=point.fees,
            net_edge=net,
            net_edge_per_contract=net / sets,
            expected_return_pct=100.0 * net / gross_cost if gross_cost else 0.0,
            execution_risk=_risk_for(len(plan), min(depths), sets),
            guaranteed=True,
            rationale=rationale,
            evidence={**evidence, "quantity_curve": [
                {"sets": p.sets, "net": round(p.net_edge, 4),
                 "fees": round(p.fees, 4)} for p in qa.curve[:12]]},
            max_executable_quantity=qa.max_executable,
            min_profitable_quantity=qa.min_profitable,
            optimal_quantity=qa.optimal,
            fee_drag_pct=point.fee_drag_pct,
            # --- Package 1 ---
            opportunity_class=OpportunityClass.STRUCTURAL_ARB,
            direction=("buy " + legs[0].side) if len(legs) == 1 else f"basket of {len(legs)}",
            liquidity={"depth_at_best": min(
                           (l.walk.levels[0].contracts if l.walk and l.walk.levels else 0)
                           for l in legs),
                       "available": min(depths)},
            data_quality="OK",
            source_quality_tier=1,          # the exchange's own book
            risk_score=RISK_SCORE[_risk_for(len(plan), min(depths), sets)],
            model_version=f"structural/{strategy_type}@{STRUCTURAL_MODEL_VERSION}",
            fee_schedule_version=self.fees.version,
        )


class SameMarketArbitrage(_Detector):
    """Buy YES and NO on the same contract. Exactly one resolves, so the pair
    is worth exactly $1.00. Profitable when the two asks sum to less."""
    name = "same_market"

    def find(self, markets: list[Market], books: dict[str, DepthBook]
             ) -> list[ArbitrageOpportunity]:
        out = []
        for m in markets:
            book = books.get(m.ticker)
            if book is None:
                continue
            ya, na = book.best_price("yes", "buy"), book.best_price("no", "buy")
            if ya is None or na is None:
                continue
            if ya + na >= 100:
                self.near_misses.append(NearMiss(
                    self.name, m.event_ticker or m.ticker, [m.ticker],
                    ya + na, 100))
                continue
            op = self._build(
                strategy_type=self.name,
                event_id=m.event_ticker or m.ticker,
                plan=[(m.ticker, "yes", book, m.subtitle or m.title),
                      (m.ticker, "no", book, m.subtitle or m.title)],
                payout_floor_per_set=1.00,
                payout_max_per_set=1.00,
                rationale=(f"YES best ask {ya}c + NO best ask {na}c = {ya + na}c "
                           f"for a pair that must pay exactly $1.00"),
                evidence={"best_yes_ask": ya, "best_no_ask": na, "sum_cents": ya + na},
            )
            if op:
                out.append(op)
        return out


class LadderInversionArbitrage(_Detector):
    """Nested thresholds priced out of order.

    "70 or above" must be at least as likely as "75 or above" - every world at
    75+ is also at 70+. When the market lets you BUY the lower rung for less
    than it will pay you to SELL the higher rung, the ordering is violated.

        buy YES on the LOWER threshold
        buy NO  on the HIGHER threshold

        T >= 75    low pays 1, NO-on-high pays 0  -> $1
        70<=T<75   low pays 1, NO-on-high pays 1  -> $2
        T <  70    low pays 0, NO-on-high pays 1  -> $1

    Floor $1.00. Doing it the other way round pays ZERO between the rungs.
    """
    name = "ladder"

    @staticmethod
    def threshold_of(m: Market) -> float | None:
        st = (m.strike_type or "").lower()
        if st in ("greater", "greater_or_equal") and m.floor_strike is not None:
            return float(m.floor_strike)
        hit = re.search(
            r"(-?\d+(?:\.\d+)?)\s*(?:°|degrees)?\s*\w*\s*or (?:above|higher|more|greater)",
            f"{m.subtitle} {m.title}", re.I)
        return float(hit.group(1)) if hit else None

    @staticmethod
    def subject_of(m: Market) -> tuple[str, str]:
        """What the threshold is ABOUT, with the threshold itself removed.

        Two rungs of one ladder differ only in their number. Two outcomes of a
        contest differ in their subject:

            KXNFLSPREAD-26SEP17DETBUF-DET4  ->  ("DET", "detroit lions by or more")
            KXNFLSPREAD-26SEP17DETBUF-DET8  ->  ("DET", "detroit lions by or more")
            KXNFLSPREAD-26SEP17DETBUF-BUF8  ->  ("BUF", "buffalo bills by or more")

        Returns (ticker_subject, text_subject). Either may be empty when the
        market carries no such information; compatible() treats an absent
        signal as absence of proof, never as proof of sameness.
        """
        tail = m.ticker.rsplit("-", 1)[-1] if "-" in m.ticker else m.ticker
        ticker_subject = re.sub(r"[^A-Z]", "", _NUMBER.sub("", tail).upper())

        text = f"{m.subtitle or ''} {m.title or ''}".lower()
        text = _NUMBER.sub(" ", text)
        text_subject = " ".join(re.sub(r"[^a-z]+", " ", text).split())
        return ticker_subject, text_subject

    def compatible(self, a: Market, b: Market) -> tuple[bool, str]:
        """Prove the ladder relationship instead of inferring it from names."""
        if not a.event_ticker or a.event_ticker != b.event_ticker:
            return False, "different events"
        if a.series_ticker and b.series_ticker and a.series_ticker != b.series_ticker:
            return False, "different series"
        sa, sb = (a.strike_type or "").lower(), (b.strike_type or "").lower()
        if sa and sb and sa != sb:
            return False, f"incompatible strike types ({sa} vs {sb})"
        if sa and sa not in ("greater", "greater_or_equal"):
            return False, f"not a threshold market ({sa})"
        if a.close_time and b.close_time:
            gap = abs((a.close_time - b.close_time).total_seconds())
            if gap > self.cfg.ladder_close_tolerance_minutes * 60:
                return False, "different settlement times"

        # ---- the subject check -------------------------------------------
        # Same event is NOT enough. An event holds every outcome of one
        # contest, including outcomes that EXCLUDE each other:
        #
        #     "Toronto wins by 2+"  and  "Roma wins by 2+"
        #
        # Treating those as rungs of a ladder invents a payout floor that does
        # not exist. Buy YES on Toronto and NO on Roma and the world where
        # Roma wins by 2 pays NOTHING. This check fails CLOSED: the pair is
        # refused unless the two markets are POSITIVELY PROVEN to describe the
        # same subject.
        at, ax = self.subject_of(a)
        bt, bx = self.subject_of(b)
        if at and bt and at != bt:
            return False, f"different subjects ({at} vs {bt})"
        if ax and bx and ax != bx:
            return False, f"different subjects ({ax!r} vs {bx!r})"
        proven = (bool(at) and at == bt) or (bool(ax) and ax == bx)
        if not proven:
            return False, "subject identity unproven"
        return True, "ok"

    def find(self, markets: list[Market], books: dict[str, DepthBook]
             ) -> list[ArbitrageOpportunity]:
        rungs = []
        for m in markets:
            t = self.threshold_of(m)
            if t is not None and m.ticker in books:
                rungs.append((t, m))
        if len(rungs) < 2:
            return []
        rungs.sort(key=lambda x: x[0])

        out = []
        for i in range(len(rungs) - 1):
            low_t, low = rungs[i]
            for j in range(i + 1, len(rungs)):
                high_t, high = rungs[j]
                ok, why = self.compatible(low, high)
                if not ok:
                    log.debug("ladder %s/%s rejected: %s", low.ticker, high.ticker, why)
                    continue
                lb, hb = books[low.ticker], books[high.ticker]
                low_ask = lb.best_price("yes", "buy")
                high_no_ask = hb.best_price("no", "buy")
                if low_ask is None or high_no_ask is None:
                    continue
                if low_ask + high_no_ask >= 100:
                    self.near_misses.append(NearMiss(
                        self.name, low.event_ticker,
                        [low.ticker, high.ticker], low_ask + high_no_ask, 100))
                    continue
                op = self._build(
                    strategy_type=self.name,
                    event_id=low.event_ticker,
                    plan=[(low.ticker, "yes", lb, low.subtitle or low.title),
                          (high.ticker, "no", hb, high.subtitle or high.title)],
                    payout_floor_per_set=1.00,
                    payout_max_per_set=2.00,
                    rationale=(f"{low_t:g}+ buyable at {low_ask}c and NO on {high_t:g}+ "
                               f"at {high_no_ask}c = {low_ask + high_no_ask}c; the lower "
                               f"threshold cannot be less likely than the higher one"),
                    evidence={"low_threshold": low_t, "high_threshold": high_t,
                              "low_yes_ask": low_ask, "high_no_ask": high_no_ask,
                              "subject": (self.subject_of(low)[0]
                                          or self.subject_of(low)[1])},
                )
                if op:
                    out.append(op)
        return out


class ExclusiveBasketArbitrage(_Detector):
    """Buy NO on every outcome of a mutually exclusive event.

    Kalshi flags events where AT MOST ONE outcome can resolve YES. So at least
    (k-1) of the k NO legs must pay $1.

    "At most one wins" does NOT mean "one must win". Buying YES on every
    outcome is therefore NOT risk-free and stays disabled by default.
    """
    name = "exclusive_basket"

    def find(self, event_id: str, markets: list[Market], books: dict[str, DepthBook],
             is_exclusive: bool) -> list[ArbitrageOpportunity]:
        if self.cfg.require_exclusive_flag and not is_exclusive:
            return []
        usable = [m for m in markets if m.ticker in books]
        k = len(usable)
        if k < 2 or k > self.cfg.max_legs:
            return []
        if any(books[m.ticker].best_price("no", "buy") is None for m in usable):
            return []

        sum_no_ask = sum(books[m.ticker].best_price("no", "buy") for m in usable)
        if sum_no_ask >= (k - 1) * 100:
            self.near_misses.append(NearMiss(
                self.name, event_id, [m.ticker for m in usable],
                sum_no_ask, (k - 1) * 100))
            return []

        op = self._build(
            strategy_type=self.name,
            event_id=event_id,
            plan=[(m.ticker, "no", books[m.ticker], m.subtitle or m.title)
                  for m in usable],
            payout_floor_per_set=float(k - 1),
            payout_max_per_set=float(k),
            rationale=(f"NO on all {k} outcomes costs {sum_no_ask}c at best ask; "
                       f"at most one outcome can win so at least {k - 1} legs "
                       f"must pay $1.00"),
            evidence={"legs": k, "sum_no_ask_cents": sum_no_ask,
                      "sum_yes_bid_cents": k * 100 - sum_no_ask,
                      "exclusive_flag": is_exclusive},
        )
        return [op] if op else []


class StructuralArbitrageScanner:
    """Runs every detector over a normalized universe."""
    name = "structural_arbitrage"
    model_free = True

    def __init__(self, fees: FeeModel, cfg: dict | None = None):
        self.cfg = ArbConfig.from_dict(cfg)
        self.fees = fees
        self.same_market = SameMarketArbitrage(fees, self.cfg)
        self.ladder = LadderInversionArbitrage(fees, self.cfg)
        self.basket = ExclusiveBasketArbitrage(fees, self.cfg)

    @property
    def _detectors(self):
        return (self.same_market, self.ladder, self.basket)

    def reset_near_misses(self) -> None:
        for d in self._detectors:
            d.near_misses = []

    def near_misses(self, limit: int = 5) -> list[NearMiss]:
        """The structures that came closest to crossing, tightest first."""
        every = [nm for d in self._detectors for nm in d.near_misses]
        every.sort(key=lambda nm: nm.gap_cents)
        return every[:limit]

    def near_miss_count(self) -> int:
        return sum(len(d.near_misses) for d in self._detectors)

    def scan_event(self, event_id: str, markets: list[Market],
                   books: dict[str, DepthBook],
                   is_exclusive: bool = False) -> list[ArbitrageOpportunity]:
        found: list[ArbitrageOpportunity] = []
        found += self.same_market.find(markets, books)
        found += self.ladder.find(markets, books)
        found += self.basket.find(event_id, markets, books, is_exclusive)
        # Settlement: the last of the legs' markets to close.
        closes = {m.ticker: getattr(m, "close_time", None) for m in markets}
        for op in found:
            times = [closes.get(t) for t in op.market_tickers if closes.get(t)]
            op.settlement_time = max(times) if times else None
        return found

    def scan(self, universe, books: dict[str, DepthBook]) -> list[ArbitrageOpportunity]:
        self.reset_near_misses()
        out: list[ArbitrageOpportunity] = []
        for event in universe:
            out += self.scan_event(event.event_ticker, event.markets, books,
                                   event.mutually_exclusive)
        out.sort(key=lambda o: o.net_edge, reverse=True)
        return out
