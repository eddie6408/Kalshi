"""Finding out whether we were right.

A forecast is only worth something once it has been checked against what
happened. This is the pass that does the checking, and it uses BOTH sources
on purpose:

    the observed temperature  -> was the DISTRIBUTION right?
                                 (mean on target, sigma wide enough)
    Kalshi's own resolution   -> was the CONTRACT right?
                                 (20% meant 20%)

Keeping both matters because the interesting case is when they DISAGREE. A
distribution that scores well against the observed high while its contracts
keep resolving the wrong way means we are forecasting the right number at the
wrong place - which is precisely how Chicago was found to be pricing O'Hare
for a market that settles on Midway.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: Statuses that mean "this is over AND the answer is known".
#:
#: `closed` is deliberately NOT here. On Kalshi a market closes when trading
#: stops and settles some time later, so a closed market carries a `result`
#: field that is still empty. Treating closed as settled made the first live
#: run log 19 "the result is unreadable" warnings per pass for markets that
#: were simply waiting their turn - and the one genuine version of that
#: warning, the one that means a field name changed again, would have been
#: lost among them.
SETTLED_STATUSES = {"settled", "finalized", "determined", "resolved"}

#: Trading has stopped but the outcome is not known yet. Normal, and quiet.
PENDING_STATUSES = {"closed", "inactive", "paused"}


@dataclass
class SettlementReport:
    checked: int = 0
    settled: int = 0
    still_open: int = 0
    #: closed, waiting to settle. Expected, and not a problem.
    pending: int = 0
    unknown: int = 0
    errors: int = 0
    #: positions closed out at what settlement paid
    booked: int = 0
    scores: list[float] = field(default_factory=list)

    @property
    def mean_brier(self) -> float | None:
        return sum(self.scores) / len(self.scores) if self.scores else None

    def summary(self) -> str:
        parts = [f"checked {self.checked}", f"settled {self.settled}",
                 f"still open {self.still_open}"]
        if self.pending:
            parts.append(f"{self.pending} closed, awaiting settlement")
        if self.booked:
            parts.append(f"{self.booked} position(s) booked at settlement")
        if self.unknown:
            parts.append(f"{self.unknown} SETTLED BUT UNREADABLE")
        if self.errors:
            parts.append(f"{self.errors} errors")
        if self.mean_brier is not None:
            parts.append(f"Brier {self.mean_brier:.4f} on this batch")
        return ", ".join(parts)


def resolution_of(market) -> bool | None:
    """Did this market resolve YES? None if it is not settled or unreadable.

    Kalshi has used several spellings for this over time, so read the result
    rather than trusting one field name - the same lesson as the order and
    position endpoints.
    """
    raw = getattr(market, "raw", None) or {}
    status = str(raw.get("status") or getattr(market, "status", "") or "").lower()
    if status and status not in SETTLED_STATUSES:
        return None

    for key in ("result", "settlement_result", "market_result"):
        value = str(raw.get(key) or "").strip().lower()
        if value in ("yes", "y", "true", "1"):
            return True
        if value in ("no", "n", "false", "0"):
            return False
        if value in ("void", "voided", "cancelled", "canceled"):
            return None     # nobody was right or wrong; do not score it

    # Some payloads only carry a final price. A contract that paid is worth
    # 100c; one that did not is worth 0.
    #
    # Note the `_dollars` spellings. The same fixed-point migration that moved
    # orders to "0.4300" and positions to position_fp reaches settlement too:
    # a live payload carries `last_price_dollars`, not `last_price`. Reading
    # only the old name finds nothing and silently scores nothing.
    for key, scale in (("settlement_value", "cents"),
                       ("expiration_value", "cents"),
                       ("settlement_value_dollars", "dollars"),
                       ("last_price_dollars", "dollars"),
                       ("close_price_dollars", "dollars"),
                       ("last_price", "cents"),
                       ("close_price", "cents")):
        value = raw.get(key)
        if value in (None, ""):
            continue
        try:
            price = float(value) * (100.0 if scale == "dollars" else 1.0)
        except (TypeError, ValueError):
            continue
        if price >= 99:
            return True
        if price <= 1:
            return False
    return None


def settle_forecasts(journal, market_client, limit: int = 2000,
                     exchange_tickers: set[str] | None = None
                     ) -> SettlementReport:
    """Score every forecast whose market has now resolved.

    Safe to run repeatedly: a settled row is never revisited, and a market
    that has not resolved is simply left for next time.
    """
    report = SettlementReport()
    try:
        rows = journal.unsettled_probabilities(limit=limit)
    except Exception as exc:  # noqa: BLE001
        log.error("SETTLE  could not read unsettled forecasts: %s", exc)
        report.errors += 1
        return report

    # One market can carry many forecast rows (we re-price it hourly), so
    # resolve each ticker once rather than once per row.
    resolutions: dict[str, bool | None] = {}
    for row in rows:
        ticker = row["ticker"]
        report.checked += 1
        if ticker not in resolutions:
            resolutions[ticker] = _resolve(market_client, ticker, report)
        outcome = resolutions[ticker]
        if outcome is None:
            report.still_open += 1
            continue
        try:
            brier = journal.settle_probability(row["id"], outcome)
            report.scores.append(brier)
            report.settled += 1
        except Exception as exc:  # noqa: BLE001
            log.error("SETTLE  could not score forecast %s: %s", row["id"], exc)
            report.errors += 1

    book_settled_positions(journal, market_client, report, resolutions,
                           exchange_tickers=exchange_tickers)
    log.info("SETTLE  %s", report.summary())
    return report


def book_settled_positions(journal, market_client, report: SettlementReport,
                           resolutions: dict[str, bool | None] | None = None,
                           exchange_tickers: set[str] | None = None
                           ) -> SettlementReport:
    """Close out fills whose market has resolved.

    This runs from the JOURNAL side, deliberately.

    The position worker also books settlements, but it can only act on what
    /portfolio/positions still lists - and the exchange drops a position the
    moment it pays out. If settlement happens between two reviews, the
    position simply vanishes and is never written down. That is not the
    unlikely case: of fifteen positions that settled between 16 and 24
    September, twelve were lost this way, leaving the fill record claiming
    we still held markets that had resolved days earlier.

    Shorts are booked too. The four the bot opened on 19-20 September by
    selling into positions it had already closed are real positions that
    really settled, and leaving them open forever was never going to make
    them go away. They close with a BUY at the settlement price.

    Working from leg_orders instead means a payout is booked whether or not
    the exchange was still showing the position when we looked.
    """
    resolutions = {} if resolutions is None else dict(resolutions)
    try:
        lines = journal.open_lines()
    except Exception as exc:  # noqa: BLE001
        log.error("SETTLE  could not read open lines: %s", exc)
        report.errors += 1
        return report

    for ticker, side, contracts in lines:
        # Never book a line the exchange still lists. If it is still there it
        # has not settled for us yet, and the position worker owns it. We only
        # write down what the exchange has already let go of.
        if exchange_tickers is not None and ticker in exchange_tickers:
            continue
        if ticker not in resolutions:
            resolutions[ticker] = _resolve(market_client, ticker, report)
        outcome = resolutions[ticker]
        if outcome is None:
            continue                      # not resolved, or unreadable
        won = outcome if side == "yes" else not outcome
        try:
            if journal.log_settlement(ticker=ticker, side=side,
                                      contracts=contracts, won=won):
                report.booked += 1
                log.info("SETTLE  booked %s %s%d %s at %dc - resolved %s%s",
                         ticker, "+" if contracts > 0 else "", contracts,
                         side.upper(), 100 if won else 0,
                         "YES" if outcome else "NO",
                         "" if contracts > 0 else "  (closing a short)")
        except Exception as exc:  # noqa: BLE001
            log.error("SETTLE  could not book %s %s: %s", ticker, side, exc)
            report.errors += 1
    return report


def _resolve(market_client, ticker: str, report: SettlementReport) -> bool | None:
    try:
        market = market_client.get_market(ticker)
    except Exception as exc:  # noqa: BLE001
        log.debug("SETTLE  could not read %s: %s", ticker, exc)
        report.errors += 1
        return None
    outcome = resolution_of(market)
    if outcome is None:
        raw = getattr(market, "raw", None) or {}
        status = str(raw.get("status") or getattr(market, "status", "") or "").lower()
        if status in SETTLED_STATUSES:
            # Settled, and we still could not read which way. THIS is the one
            # worth shouting about: it means a field name changed again. The
            # values are logged, not just the key names - knowing `result` is
            # present tells you nothing if it is present and empty.
            report.unknown += 1
            interesting = {k: raw.get(k) for k in
                           ("result", "settlement_result", "market_result",
                            "settlement_value", "expiration_value",
                            "last_price_dollars", "last_price")
                           if k in raw}
            log.warning("SETTLE  %s is %s but the result is unreadable: %s "
                        "(all keys: %s)", ticker, status, interesting,
                        sorted(raw.keys()))
        elif status in PENDING_STATUSES:
            report.pending += 1
    return outcome
