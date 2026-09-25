"""Managing what the bot already owns.

Finding a trade and holding a trade are different jobs with different clocks.
A scan sweeps the exchange and takes fifteen minutes; a position needs looking
at far more often than that, and needs almost nothing to do it - the tickers
we hold, not the 1,500 order books we might trade next.

So this runs as its own worker, on its own timer, inside the same process.

    Same process, because risk cannot be split. The limits are TOTAL exposure,
    per-event, per-market. Two processes each believing they own the account
    can enforce none of them, would double the API load, and would each read
    the other's fills as unexplained positions during reconciliation.

The questions it asks, straight from the brief:

    Has my thesis changed?
    Has new information appeared?
    Has the market moved enough that the remaining EV is gone?
    Should I exit? Should I reduce?

One rule matters more than the rest:

    AN EXIT IS NEVER BLOCKED BY THE ENTRY GATE.

The entry gate exists to stop the bot buying something it should not buy. Run
an exit through it and "edge too small" would prevent closing a losing
position - a risk control that traps you in a trade is worse than no risk
control at all. Exits get their own, much smaller, set of checks: a real
quantity, a real bid to sell into, a sane price.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from .depth import DepthBook
from .edge import contracts_from_kelly
from .models import Market
from .settlement import resolution_of

log = logging.getLogger(__name__)

#: Statuses that mean the market has stopped trading. A position in one of
#: these is not "stuck" - there is simply nothing left to decide.
CLOSED_STATUSES = {"closed", "settled", "finalized", "determined", "resolved",
                   "inactive"}


class ExitReason(str, Enum):
    HOLD = "HOLD"
    #: settlement is close - liquidity thins and the book stops being a way out
    CLOSING_SOON = "CLOSING_SOON"
    #: down more than we said we would accept on this position
    STOP_LOSS = "STOP_LOSS"
    #: the estimator declined, or its event no longer coheres: we are blind
    NO_ESTIMATE = "NO_ESTIMATE"
    #: our probability moved against the side we hold
    THESIS_GONE = "THESIS_GONE"
    #: the market came to us; there is no edge left to wait for
    TAKE_PROFIT = "TAKE_PROFIT"
    #: still the right side, but the remaining edge no longer pays for the risk
    EDGE_DECAYED = "EDGE_DECAYED"
    #: we want out and nobody is bidding - reported, not actionable
    NO_EXIT_LIQUIDITY = "NO_EXIT_LIQUIDITY"
    #: the market has stopped trading and has not paid out yet. There is no
    #: decision left to make: no order can be placed on a closed market.
    AWAITING_SETTLEMENT = "AWAITING_SETTLEMENT"
    #: the market has resolved. The outcome is known and gets booked once.
    SETTLED = "SETTLED"
    #: still the right side, but too big for the edge that is left. Sell part.
    REDUCE = "REDUCE"


@dataclass
class PositionConfig:
    review_interval_seconds: float = 30.0
    #: hold only while our probability beats the exit price by this much
    min_remaining_edge_cents: float = 2.0
    #: our probability has to fall this far below entry to call the thesis dead
    thesis_shift: float = 0.08
    #: close out this long before settlement rather than ride it out
    close_within_minutes: float = 45.0
    #: give up on a position down this share of what it cost
    stop_loss_fraction: float = 0.50
    #: A stop may not fire until a position has been held this long. Nothing
    #: is learned in the first minute of a trade; what moves in that window is
    #: the book, not the world. Without this, a wide spread makes every entry
    #: an instant stop-out - thirteen seconds, on 19 September.
    min_hold_minutes_before_stop: float = 20.0
    #: exit when the estimator can no longer price the market at all
    exit_when_blind: bool = True
    #: refuse to sell below this - a 1c bid is not an exit, it is a donation
    min_exit_price_cents: int = 1
    #: how often to repeat the warning about a position we cannot sell. A
    #: warning printed every 30 seconds is not a warning, it is wallpaper.
    stuck_warn_every_minutes: float = 30.0
    #: Never sell more than we can prove we hold.
    #:
    #: On Kalshi a sell order with no position open does not fail and does not
    #: no-op. It opens the OPPOSITE position. Between 19 and 20 September that
    #: happened five times: the bot exited a market, /portfolio/positions still
    #: reported the pre-trade quantity on the next review thirty seconds later,
    #: the bot sold again, and each of those sales opened a short it never
    #: intended and then had to be talked out of. Four of them were still open
    #: at the time of the audit, on markets that had already settled.
    #:
    #: With this on, the quantity we will sell is the MINIMUM of what the
    #: exchange reports and what our own fill record says is left. The fill
    #: record is written the moment an order is accepted, so it is never the
    #: stale one. If our record has never seen this market it abstains rather
    #: than vetoing, so a position opened by hand can still be exited.
    #:
    #: Turning this off restores the behaviour that created the shorts.
    reduce_only: bool = True

    # -------------------- partial reduction --------------------
    #: Trim a position that is still on the right side but has become too big
    #: for the edge that is left. Answers "should I reduce exposure?".
    reduce_enabled: bool = True
    #: Fees round UP to the cent on every leg, so a three-contract trim can
    #: cost more in fees than the risk it removes. Never trim less than this.
    min_reduce_contracts: int = 5
    #: ... and never for less than this much money.
    min_reduce_value_usd: float = 2.00
    #: Reduction may never take a position below this. Closing a position is
    #: the exit rules' job; trimming must not be able to do it by stealth.
    min_hold_contracts: int = 5
    #: Reviews run every 30s. Without this a decaying edge would be trimmed
    #: on every cycle - death by a thousand fees.
    reduce_cooldown_minutes: float = 60.0
    #: Inputs for "what size would I hold at today's price?". Defaults are
    #: overridden from risk: and forecast: at construction.
    bankroll_usd: float = 1000.0
    kelly_fraction: float = 0.25
    max_cost_per_trade_usd: float = 25.0

    @classmethod
    def from_dict(cls, d: dict | None, risk: dict | None = None,
                  forecast: dict | None = None) -> "PositionConfig":
        """`risk` and `forecast` supply the sizing inputs reduction needs.

        They are read from the same config blocks the entry path uses, so a
        position is judged against the size the bot would open TODAY - not
        against a second, drifting set of numbers.
        """
        d = d or {}
        risk = risk or {}
        forecast = forecast or {}
        return cls(
            review_interval_seconds=float(d.get("review_interval_seconds", 30.0)),
            min_remaining_edge_cents=float(d.get("min_remaining_edge_cents", 2.0)),
            thesis_shift=float(d.get("thesis_shift", 0.08)),
            close_within_minutes=float(d.get("close_within_minutes", 45.0)),
            stop_loss_fraction=float(d.get("stop_loss_fraction", 0.50)),
            min_hold_minutes_before_stop=float(
                d.get("min_hold_minutes_before_stop", 20.0)),
            exit_when_blind=bool(d.get("exit_when_blind", True)),
            min_exit_price_cents=int(d.get("min_exit_price_cents", 1)),
            stuck_warn_every_minutes=float(
                d.get("stuck_warn_every_minutes", 30.0)),
            reduce_only=bool(d.get("reduce_only", True)),
            reduce_enabled=bool(d.get("reduce_enabled", True)),
            min_reduce_contracts=int(d.get("min_reduce_contracts", 5)),
            min_reduce_value_usd=float(d.get("min_reduce_value_usd", 2.00)),
            min_hold_contracts=int(d.get("min_hold_contracts", 5)),
            reduce_cooldown_minutes=float(d.get("reduce_cooldown_minutes", 60.0)),
            bankroll_usd=float(d.get("bankroll_usd")
                               or risk.get("bankroll_usd") or 1000.0),
            kelly_fraction=float(d.get("kelly_fraction")
                                 or forecast.get("kelly_fraction") or 0.25),
            max_cost_per_trade_usd=float(
                d.get("max_cost_per_trade_usd")
                or forecast.get("max_cost_per_trade_usd")
                or risk.get("max_cost_per_trade_usd") or 25.0),
        )


@dataclass
class PositionView:
    """Everything needed to judge one holding, gathered in one place."""
    ticker: str
    side: str                      # "yes" | "no" - the side we are long
    quantity: int                  # always positive
    exit_bid_cents: int | None = None      # what we could sell into right now
    entry_price_cents: float | None = None
    our_probability: float | None = None   # P(the side we hold pays)
    confidence: float | None = None
    minutes_to_close: float = float("inf")
    estimate_error: str = ""
    #: what the exchange says about the market itself
    market_status: str = ""
    #: the other side of the book, so a mark can be taken at the mid
    entry_side_ask_cents: int | None = None
    #: when this position was opened, for the minimum-hold rule
    opened_at: datetime | None = None
    #: True/False once the market has resolved, None while it is still open
    resolution: bool | None = None

    @property
    def is_closed(self) -> bool:
        """Has the market stopped trading? An empty status means we do not know.

        Deliberately conservative: only a status the exchange actually gave us
        counts. Inferring "closed" from a missing book would make every thin
        market look settled.
        """
        return bool(self.market_status) and self.market_status.lower() in CLOSED_STATUSES

    @property
    def won(self) -> bool | None:
        """Did the side we hold pay out? None until the market resolves."""
        if self.resolution is None:
            return None
        return self.resolution if self.side == "yes" else not self.resolution

    @property
    def cost_usd(self) -> float:
        if self.entry_price_cents is None:
            return 0.0
        return self.quantity * self.entry_price_cents / 100.0

    @property
    def value_usd(self) -> float:
        if self.exit_bid_cents is None:
            return 0.0
        return self.quantity * self.exit_bid_cents / 100.0

    @property
    def unrealised_usd(self) -> float:
        if self.entry_price_cents is None or self.exit_bid_cents is None:
            return 0.0
        return self.value_usd - self.cost_usd

    @property
    def mark_cents(self) -> float | None:
        """What the position is worth for the purpose of judging a LOSS.

        The mid, when both sides are quoted. NOT the bid.

        We buy at the ask and can only leave at the bid, so marking to the bid
        books the entire spread as a loss the instant the trade fills. On
        19 September that turned a 14c/83c book into an "83% loss" thirteen
        seconds after entry, and the stop loss dutifully sold at the bid -
        converting a paper spread into $8.24 of real money, five times over.

        Exits still SELL at the bid, because that is the only price really
        available. This is only about deciding whether we are losing.
        """
        if self.exit_bid_cents is None:
            return None
        if self.entry_side_ask_cents is None:
            return float(self.exit_bid_cents)
        return (float(self.exit_bid_cents) + float(self.entry_side_ask_cents)) / 2.0

    @property
    def marked_pnl_usd(self) -> float:
        """Unrealised P/L at the mid - the number a stop loss should use."""
        if self.entry_price_cents is None or self.mark_cents is None:
            return 0.0
        return self.quantity * (self.mark_cents - self.entry_price_cents) / 100.0

    @property
    def minutes_held(self) -> float | None:
        if self.opened_at is None:
            return None
        return (datetime.now(timezone.utc) - self.opened_at).total_seconds() / 60.0

    @property
    def remaining_edge_cents(self) -> float | None:
        """Our probability minus what the market will pay us to leave.

        Positive means holding still beats selling. Negative means the market
        is offering more than we think the contract is worth, which is the
        definition of a good time to go.
        """
        if self.our_probability is None or self.exit_bid_cents is None:
            return None
        return self.our_probability * 100.0 - self.exit_bid_cents


@dataclass
class ExitDecision:
    reason: ExitReason
    detail: str = ""
    sell_price_cents: int | None = None
    #: how many to sell. None means the whole position.
    sell_quantity: int | None = None

    @property
    def should_exit(self) -> bool:
        """Whether this decision means "send a sell order".

        NO_EXIT_LIQUIDITY, AWAITING_SETTLEMENT and SETTLED are all states we
        report rather than act on: in every one of them there is no order that
        could be placed.
        """
        return self.reason not in (ExitReason.HOLD,
                                   ExitReason.NO_EXIT_LIQUIDITY,
                                   ExitReason.AWAITING_SETTLEMENT,
                                   ExitReason.SETTLED,
                                   ExitReason.REDUCE)

    @property
    def should_sell(self) -> bool:
        """Whether ANY order goes out - a full exit or a partial trim."""
        return self.should_exit or self.reason is ExitReason.REDUCE


@dataclass
class ReviewResult:
    view: PositionView
    decision: ExitDecision
    sold: int = 0
    error: str = ""
    notes: list[str] = field(default_factory=list)


class PositionWorker:
    """Reviews open positions on its own clock and closes the ones that are done."""

    def __init__(self, market_client, portfolio_client, execution_client,
                 strategies, journal=None, cfg: PositionConfig | None = None,
                 lock: threading.Lock | None = None, dry_run: bool = False):
        self.market = market_client
        self.portfolio = portfolio_client
        self.exec_client = execution_client
        self.strategies = list(strategies or [])
        self.journal = journal
        self.cfg = cfg or PositionConfig()
        #: shared with the scan loop so the two never trade one ticker at once
        self.lock = lock or threading.Lock()
        #: tickers this worker is currently selling; the scan loop skips them
        self.exiting: set[str] = set()
        #: In SCAN_ONLY the worker still reviews and still says what it would
        #: do - it just does not send the order. Watching the reasoning for a
        #: day before letting it act is cheap; being surprised is not.
        self.dry_run = dry_run or execution_client is None
        self.reviews = 0
        self.last_review: datetime | None = None
        #: tickers whose settlement has already been written to the journal,
        #: so a position that lingers in the portfolio is booked once, not
        #: once every thirty seconds
        self.booked: set[str] = set()
        #: when we last warned about each position we cannot sell
        self._warned_at: dict[str, datetime] = {}
        self._ledger_warned_at: dict[str, datetime] = {}
        #: when we last trimmed each ticker, for the reduction cooldown
        self._reduced_at: dict[str, datetime] = {}

    # ------------------------------------------------------------------
    def run_forever(self, stop: threading.Event) -> None:
        """Loop until told to stop. Never lets one bad cycle kill the worker."""
        log.info("POSITIONS  worker started, reviewing every %.0fs",
                 self.cfg.review_interval_seconds)
        while not stop.is_set():
            try:
                self.review_once()
            except Exception as exc:  # noqa: BLE001
                # A failing review must not take down the thread; the next
                # cycle gets a clean attempt.
                log.error("POSITIONS  review failed: %s", exc, exc_info=True)
            stop.wait(self.cfg.review_interval_seconds)
        log.info("POSITIONS  worker stopped")

    # ------------------------------------------------------------------
    def review_once(self) -> list[ReviewResult]:
        positions = [p for p in self._positions() if p.contracts]
        self.reviews += 1
        self.last_review = datetime.now(timezone.utc)
        if not positions:
            return []

        out: list[ReviewResult] = []
        for pos in positions:
            view = self._build_view(pos)
            decision = self.decide(view)
            result = ReviewResult(view=view, decision=decision)
            if decision.reason is ExitReason.SETTLED:
                self._book_settlement(result)
            elif decision.should_sell:
                self._exit(result)
            else:
                log.debug("POSITIONS  hold %s x%d: %s",
                          view.ticker, view.quantity, decision.detail)
            out.append(result)
        self._log_summary(out)
        return out

    # ------------------------------------------------------------------
    def decide(self, v: PositionView) -> ExitDecision:
        """Deterministic. No judgement calls, in priority order."""
        if v.quantity <= 0:
            return ExitDecision(ExitReason.HOLD, "nothing held")

        # 0. Has the market stopped trading? This comes FIRST, before the
        #    liquidity check, because a closed market has no book and would
        #    otherwise be reported as "stuck" every thirty seconds for the
        #    rest of the session - which is exactly what it did. There is no
        #    decision to make here: no order can be placed on a closed market,
        #    and the contract now pays what it pays.
        if v.is_closed:
            if v.resolution is not None:
                won = v.won
                return ExitDecision(
                    ExitReason.SETTLED,
                    f"resolved {'YES' if v.resolution else 'NO'} - our {v.side.upper()} "
                    f"{'paid out' if won else 'expired worthless'}")
            return ExitDecision(
                ExitReason.AWAITING_SETTLEMENT,
                f"market is {v.market_status}; waiting for it to resolve")

        # Can we even get out? Everything below assumes a bid to sell into.
        if v.exit_bid_cents is None or v.exit_bid_cents < self.cfg.min_exit_price_cents:
            return ExitDecision(
                ExitReason.NO_EXIT_LIQUIDITY,
                f"no bid at or above {self.cfg.min_exit_price_cents}c to sell into")

        # 1. Settlement is close. The book thins out near the end and a
        #    position we cannot leave is a position we have to be right about.
        if v.minutes_to_close <= self.cfg.close_within_minutes:
            return ExitDecision(
                ExitReason.CLOSING_SOON,
                f"settles in {v.minutes_to_close:.0f} min, inside the "
                f"{self.cfg.close_within_minutes:.0f} min exit window",
                v.exit_bid_cents)

        # 2. Down more than we said we would take. Checked before any opinion,
        #    because a stop that waits for the model to agree is not a stop.
        held = v.minutes_held
        young = held is not None and held < self.cfg.min_hold_minutes_before_stop
        if (v.cost_usd > 0 and not young
                and v.marked_pnl_usd < -self.cfg.stop_loss_fraction * v.cost_usd):
            return ExitDecision(
                ExitReason.STOP_LOSS,
                f"down ${-v.marked_pnl_usd:.2f} on ${v.cost_usd:.2f} "
                f"({-100 * v.marked_pnl_usd / v.cost_usd:.0f}% at a "
                f"{v.mark_cents:.0f}c mark)"
                + (f", held {held:.0f} min" if held is not None else ""),
                v.exit_bid_cents)

        # 3. We can no longer price it. Holding a forecast position with no
        #    forecast is not holding a view, it is hoping.
        if v.our_probability is None:
            if self.cfg.exit_when_blind:
                return ExitDecision(
                    ExitReason.NO_ESTIMATE,
                    v.estimate_error or "estimator can no longer price this market",
                    v.exit_bid_cents)
            return ExitDecision(ExitReason.HOLD, "no estimate; configured to hold")

        edge = v.remaining_edge_cents
        assert edge is not None

        # 4. Our own view moved against the side we hold.
        if (v.entry_price_cents is not None
                and v.our_probability * 100.0
                < v.entry_price_cents - self.cfg.thesis_shift * 100.0):
            return ExitDecision(
                ExitReason.THESIS_GONE,
                f"our probability fell to {v.our_probability * 100:.1f}% against a "
                f"{v.entry_price_cents:.0f}c entry",
                v.exit_bid_cents)

        # 5/6. Not enough left to be worth the risk of holding. Whether that is
        #      good news or bad depends on which side of the entry we are on.
        if edge < self.cfg.min_remaining_edge_cents:
            in_profit = v.unrealised_usd > 0
            reason = ExitReason.TAKE_PROFIT if in_profit else ExitReason.EDGE_DECAYED
            return ExitDecision(
                reason,
                f"{edge:+.1f}c of edge left at a {v.exit_bid_cents}c bid "
                f"(floor {self.cfg.min_remaining_edge_cents:.1f}c), "
                f"{'up' if in_profit else 'down'} ${abs(v.unrealised_usd):.2f}",
                v.exit_bid_cents)

        # 7. Still the right side and worth holding - but is it worth holding
        #    THIS MUCH? Reduction is considered only here, at the point where
        #    every exit rule has already declined to fire. It is a refinement
        #    of holding, never a softening of leaving: nothing below can turn
        #    an EXIT into a trim.
        trim = self._reduction(v)
        if trim is not None:
            keep, sell, why = trim
            return ExitDecision(
                ExitReason.REDUCE,
                f"{why}; selling {sell} of {v.quantity}, keeping {keep}",
                v.exit_bid_cents, sell_quantity=sell)

        return ExitDecision(
            ExitReason.HOLD,
            f"{edge:+.1f}c of edge left, settles in {v.minutes_to_close:.0f} min")

    # ------------------------------------------------------------------
    def _reduction(self, v: PositionView) -> tuple[int, int, str] | None:
        """How much of this position the current edge no longer justifies.

        The question is deliberately the same one the entry path asks:

            "Would I open this position, at this size, at today's price?"

        If the answer is "yes, but smaller", the difference is exposure we are
        carrying for no reason. Sizing uses the same fractional-Kelly function
        and the same bankroll the entry path uses, so a held position is
        judged against the size the bot would open TODAY rather than against a
        second set of numbers that could drift away from it.

        Returns (keep, sell, why) or None to hold the lot.
        """
        c = self.cfg
        if not c.reduce_enabled:
            return None
        if (v.our_probability is None or v.exit_bid_cents is None
                or v.quantity <= c.min_hold_contracts):
            return None

        last = self._reduced_at.get(v.ticker)
        if last is not None and (datetime.now(timezone.utc) - last).total_seconds() \
                < c.reduce_cooldown_minutes * 60.0:
            return None

        # What Kelly would buy at the price we could actually leave at. Using
        # the BID rather than the mid is the conservative choice: it is the
        # price that is really available to us.
        wanted = contracts_from_kelly(
            v.our_probability, v.exit_bid_cents, c.bankroll_usd,
            c.kelly_fraction, v.confidence if v.confidence is not None else 1.0)
        price_usd = max(0.01, v.exit_bid_cents / 100.0)
        dollar_cap = int(c.max_cost_per_trade_usd / price_usd)
        target = min(wanted, dollar_cap)

        # Reduction may never close a position. Full closure belongs to the
        # exit rules, which have already declined; letting a trim finish the
        # job would be those rules being overruled by a different mechanism.
        keep = max(target, c.min_hold_contracts)
        if keep >= v.quantity:
            return None

        sell = v.quantity - keep
        if sell < c.min_reduce_contracts:
            return None
        if sell * v.exit_bid_cents / 100.0 < c.min_reduce_value_usd:
            return None

        why = (f"edge now justifies {target} contracts at a "
               f"{v.exit_bid_cents}c bid ({v.our_probability * 100:.0f}% vs "
               f"{v.exit_bid_cents}c)")
        return keep, sell, why

    # ------------------------------------------------------------------
    def _exit(self, result: ReviewResult) -> None:
        """Sell the position, or part of it. NOT routed through the entry gate."""
        v, d = result.view, result.decision
        price = d.sell_price_cents or v.exit_bid_cents
        if price is None or not 1 <= price <= 99:
            result.error = f"refusing to sell at {price}c"
            log.error("POSITIONS  %s", result.error)
            return

        count = d.sell_quantity if d.sell_quantity is not None else v.quantity
        partial = count < v.quantity
        if count <= 0 or count > v.quantity:
            result.error = (f"refusing to sell {count} of {v.quantity} "
                            f"contracts in {v.ticker}")
            log.error("POSITIONS  %s", result.error)
            return

        # A sell with nothing behind it is not a no-op on this exchange - it
        # opens the opposite position. Check our own fills, which are current,
        # against the exchange's view, which lags.
        sellable = self._sellable(v)
        if sellable is not None and sellable < count:
            if sellable <= 0:
                result.error = (
                    f"not selling {count} {v.side.upper()} in {v.ticker}: our "
                    f"fill record says the position is already closed while "
                    f"the exchange still reports {v.quantity}. Selling here "
                    f"would open a short.")
                # Rate-limited for the same reason the stuck warning is: at a
                # 30s review this would print 120 times an hour, and a line
                # that repeats that often stops being read.
                self._warn_ledger(v.ticker, "POSITIONS  LEDGER  %s",
                                  result.error)
                return
            self._warn_ledger(
                v.ticker, "POSITIONS  LEDGER  %s: exchange says %d, our fills "
                "say %d - selling the smaller number",
                v.ticker, v.quantity, sellable)
            count = sellable
            partial = count < v.quantity
        # Do not print "REDUCE REDUCE": for a trim the verb IS the reason.
        verb = "REDUCE" if partial else f"EXIT {d.reason.value}"

        if self.dry_run:
            result.notes.append("dry run: no order sent")
            log.info("POSITIONS  WOULD %s  %s x%d of %d %s @ %dc  (%s)",
                     verb, v.ticker, count, v.quantity,
                     v.side.upper(), price, d.detail)
            if partial:
                # Respect the cooldown in dry run too, so the reporting-only
                # log reads the way the live one would instead of repeating
                # the same proposed trim every thirty seconds.
                self._reduced_at[v.ticker] = datetime.now(timezone.utc)
            return

        with self.lock:
            if v.ticker in self.exiting:
                result.error = "already exiting"
                return
            self.exiting.add(v.ticker)
        try:
            log.info("POSITIONS  %s  %s x%d of %d %s @ %dc  (%s)",
                     verb, v.ticker, count, v.quantity,
                     v.side.upper(), price, d.detail)
            self.exec_client.place_demo_order(
                ticker=v.ticker, action="sell", side=v.side,
                count=count, limit_price_cents=price,
            )
            result.sold = count
            # Record the sale where real fills live, so the next review's
            # arithmetic includes it whether or not the exchange has updated
            # the portfolio yet.
            self._record_leg(v, count, price, d)
            if partial:
                self._reduced_at[v.ticker] = datetime.now(timezone.utc)
            else:
                self._record_exit(v, d, price)
        except Exception as exc:  # noqa: BLE001
            result.error = str(exc)
            log.error("POSITIONS  %s failed for %s: %s",
                      "reduction" if partial else "exit", v.ticker, exc)
        finally:
            with self.lock:
                self.exiting.discard(v.ticker)

    def _record_leg(self, v: PositionView, count: int, price: int,
                    d: ExitDecision) -> None:
        """Write the sale into leg_orders - the table that holds real fills.

        Written only after the order was accepted without raising. A sale the
        exchange refused must not appear as one that happened.
        """
        if self.journal is None or not hasattr(self.journal, "log_leg_fill"):
            return
        try:
            self.journal.log_leg_fill(
                ticker=v.ticker, side=v.side, action="sell", filled=count,
                price_cents=price, note=d.reason.value)
        except Exception as exc:  # noqa: BLE001
            log.error("POSITIONS  could not journal the sale of %s: %s",
                      v.ticker, exc)

    def _book_settlement(self, result: ReviewResult) -> None:
        """Write down what a resolved market actually paid. Once.

        A settled contract is worth 100c or 0c - there is no price to
        negotiate and no order to send. What is left is bookkeeping, and
        bookkeeping done every thirty seconds is not bookkeeping.
        """
        v, d = result.view, result.decision
        if v.ticker in self.booked:
            result.notes.append("already booked")
            return
        won = v.won
        result.notes.append(f"settled {'WON' if won else 'LOST'}")
        log.info("POSITIONS  SETTLED %s x%d %s - %s",
                 v.ticker, v.quantity, v.side.upper(), d.detail)
        if self.journal is None:
            self.booked.add(v.ticker)
            return
        try:
            # In YES terms: a YES resolution pays the YES holder 100c.
            yes_px = 100 if v.resolution else 0
            n, pnl = self.journal.close_open_trades(
                v.ticker, yes_px, 100 - yes_px, outcome=ExitReason.SETTLED.value)
            if n:
                log.info("POSITIONS  booked %d journal entr%s on %s, P/L $%+.2f",
                         n, "y" if n == 1 else "ies", v.ticker, pnl)
            self.booked.add(v.ticker)
        except Exception as exc:  # noqa: BLE001
            # Leave it unbooked so the next review tries again. Losing the
            # record of an outcome is worse than logging it twice.
            result.error = str(exc)
            log.error("POSITIONS  could not book the settlement of %s: %s",
                      v.ticker, exc)

    def _record_exit(self, v: PositionView, d: ExitDecision, price: int) -> None:
        if self.journal is None:
            return
        try:
            yes_px = price if v.side == "yes" else 100 - price
            no_px = 100 - yes_px
            n, pnl = self.journal.close_open_trades(
                v.ticker, yes_px, no_px, outcome=d.reason.value)
            if n:
                log.info("POSITIONS  closed %d journal entr%s on %s, P/L $%+.2f",
                         n, "y" if n == 1 else "ies", v.ticker, pnl)
        except Exception as exc:  # noqa: BLE001
            log.error("POSITIONS  could not journal the exit of %s: %s",
                      v.ticker, exc)

    # ------------------------------------------------------------------
    def _positions(self):
        try:
            return self.portfolio.positions() if self.portfolio else []
        except Exception as exc:  # noqa: BLE001
            log.error("POSITIONS  could not read positions: %s", exc)
            return []

    def _warn_ledger(self, ticker: str, fmt: str, *args) -> None:
        """One ledger warning per market per stuck_warn_every_minutes."""
        now = datetime.now(timezone.utc)
        last = self._ledger_warned_at.get(ticker)
        if (last is None
                or (now - last).total_seconds()
                >= self.cfg.stuck_warn_every_minutes * 60.0):
            self._ledger_warned_at[ticker] = now
            log.warning(fmt, *args)
        else:
            log.debug(fmt, *args)

    def _sellable(self, v: PositionView) -> int | None:
        """How many contracts we can prove we still hold. None = no opinion.

        The exchange is authoritative about what exists and stale about what
        just changed; our fill record is the reverse. Taking the smaller of
        the two is safe in both directions: if the exchange has not caught up
        we sell what is really left, and if someone traded this market by hand
        outside the bot we sell only what the exchange confirms.
        """
        if not self.cfg.reduce_only:
            return None
        if self.journal is None:
            return None
        if not hasattr(self.journal, "held_from_fills"):
            # Configured ON, but this journal cannot answer the question - so
            # the clamp is not running, and every exit is unprotected.
            #
            # This is not hypothetical. On 22 September an older installer was
            # double-clicked a second time and put back a journal.py from
            # before the clamp existed. Everything still started, the config
            # still said reduce_only: true, and the protection was simply
            # gone. Only a unit test noticed. Silence is the bug; say it.
            self._warn_ledger(
                "*", "POSITIONS  LEDGER  reduce_only is ON in config but this "
                "journal has no held_from_fills - THE CLAMP IS NOT RUNNING and "
                "exits are unprotected. kalshibot/journal.py is older than the "
                "rest of the bot.")
            return None
        try:
            held = self.journal.held_from_fills(v.ticker, v.side)
        except Exception as exc:  # noqa: BLE001
            # A ledger we cannot read is not a ledger that says zero.
            log.error("POSITIONS  could not read our fill record for %s: %s",
                      v.ticker, exc)
            return None
        if held is None:
            return None
        return min(int(held), v.quantity)

    def _build_view(self, pos) -> PositionView:
        side = "yes" if pos.contracts > 0 else "no"
        v = PositionView(ticker=pos.ticker, side=side, quantity=abs(pos.contracts))

        book = self._book(v.ticker)
        if book is not None:
            # Selling consumes the BID on our side, not the ask.
            v.exit_bid_cents = book.best_price(side, "sell")
            # The other side too, so the mark can be taken at the mid rather
            # than booking the spread as a loss.
            v.entry_side_ask_cents = book.best_price(side, "buy")

        market = self._market(v.ticker)
        if market is not None:
            v.minutes_to_close = market.minutes_to_close()
            v.market_status = str(getattr(market, "status", "") or "")
            if v.is_closed:
                # Reuse the settlement reader rather than inventing a second
                # one. It already knows every spelling Kalshi has used for a
                # result and refuses to guess when it cannot read one.
                v.resolution = resolution_of(market)
            else:
                self._apply_estimate(v, market)

        v.entry_price_cents = self._entry_price(v)
        v.opened_at = self._opened_at(v)
        return v

    def _opened_at(self, v: PositionView):
        if self.journal is None or not hasattr(self.journal, "opened_at"):
            return None
        try:
            raw = self.journal.opened_at(v.ticker, v.side)
        except Exception as exc:  # noqa: BLE001
            log.debug("POSITIONS  no open time for %s: %s", v.ticker, exc)
            return None
        if not raw:
            return None
        try:
            stamp = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)

    def _apply_estimate(self, v: PositionView, market: Market) -> None:
        strategy = self._strategy_for(v.ticker)
        if strategy is None:
            v.estimate_error = "no estimator owns this market"
            return
        try:
            est = strategy.estimator.estimate(market)
        except Exception as exc:  # noqa: BLE001
            v.estimate_error = f"estimator raised: {exc}"
            return
        if not est.usable:
            v.estimate_error = est.skip_reason or "estimator declined"
            return
        # probability_yes is P(YES). We may be long NO.
        v.our_probability = (est.probability_yes if v.side == "yes"
                             else 1.0 - est.probability_yes)
        v.confidence = est.confidence

    def _strategy_for(self, ticker: str):
        for s in self.strategies:
            for series in s.candidate_series():
                if ticker.startswith(series):
                    return s
        return None

    def _entry_price(self, v: PositionView) -> float | None:
        """Average entry price in the frame of the side we hold.

        Asks the journal, which knows that real fills live in `leg_orders` and
        not in `trades`. Without an entry price the stop loss and the thesis
        check cannot fire at all, so this is load-bearing rather than cosmetic.
        """
        if self.journal is None:
            return None
        try:
            _held, price = self.journal.entry_for(v.ticker, v.side)
            return price
        except Exception as exc:  # noqa: BLE001
            log.debug("POSITIONS  no journal entry for %s: %s", v.ticker, exc)
            return None

    def _book(self, ticker: str) -> DepthBook | None:
        try:
            return self.market.get_depth(ticker)
        except Exception as exc:  # noqa: BLE001
            log.debug("POSITIONS  no book for %s: %s", ticker, exc)
            return None

    def _market(self, ticker: str) -> Market | None:
        try:
            return self.market.get_market(ticker)
        except Exception as exc:  # noqa: BLE001
            log.debug("POSITIONS  no market record for %s: %s", ticker, exc)
            return None

    # ------------------------------------------------------------------
    def _log_summary(self, results: list[ReviewResult]) -> None:
        reduced = [r for r in results
                   if r.sold and r.decision.reason is ExitReason.REDUCE]
        exited = [r for r in results
                  if r.sold and r.decision.reason is not ExitReason.REDUCE]
        would = [r for r in results if r.decision.should_exit and not r.sold
                 and not r.error]
        stuck = [r for r in results
                 if r.decision.reason is ExitReason.NO_EXIT_LIQUIDITY]
        waiting = [r for r in results if r.decision.reason in
                   (ExitReason.AWAITING_SETTLEMENT, ExitReason.SETTLED)]
        held = len(results) - len(exited)
        verb = "would exit" if self.dry_run else "exited"
        extra = ""
        if stuck:
            extra += f", {len(stuck)} with no bid to sell into"
        if waiting:
            extra += f", {len(waiting)} awaiting settlement"
        if reduced:
            extra += (f", trimmed {len(reduced)} "
                      f"({sum(r.sold for r in reduced)} contracts)")
        log.info("POSITIONS  reviewed %d, held %d, %s %d%s",
                 len(results), held,
                 verb, len(would) if self.dry_run else len(exited), extra)
        for r in stuck:
            self._warn_stuck(r)

    def _warn_stuck(self, r: ReviewResult) -> None:
        """Warn about an unsellable position, but not on every single cycle.

        At a 30s review interval the old behaviour printed the same warning
        120 times an hour. A line that repeats that often stops being read,
        which means the one time it matters is the one time it is missed.
        """
        now = datetime.now(timezone.utc)
        last = self._warned_at.get(r.view.ticker)
        due = (last is None
               or (now - last).total_seconds()
               >= self.cfg.stuck_warn_every_minutes * 60.0)
        if due:
            self._warned_at[r.view.ticker] = now
            log.warning("POSITIONS  STUCK %s x%d - %s",
                        r.view.ticker, r.view.quantity, r.decision.detail)
        else:
            log.debug("POSITIONS  still stuck %s x%d - %s",
                      r.view.ticker, r.view.quantity, r.decision.detail)
