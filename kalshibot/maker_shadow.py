"""Package C2, phase M0: would a resting (maker) order have filled?

Every trade so far crossed the spread and paid the taker fee. A maker order
rests on the book and waits - it captures part of the spread instead of
paying it, but it fills only when someone else wants the other side, and
disproportionately when the price is about to move against it. Both halves
have to be MEASURED before a single real maker order is built (M1-M3).

The demo tape is empty, so this runs on PRODUCTION public data:

  1. When the production book for a market the forecast strategy priced is
     read (prodwatch), post a SIMULATED buy at the C2 price:

         price = min(best_bid + improve,              join / improve the bid
                     fair - min_edge - maker_fee,     never give the edge away
                     best_ask - 1)                    never cross

     on whichever side our probability favours. One open order per market.
  2. Replay the production prints after it against it. A print on our side
     of the book at a price BETTER for the taker than ours ("through") means
     our order, resting ahead, would have been hit first. A print AT our
     price eats the displayed queue ahead of us first ("queue"). Queue
     cancellations are ignored, so fills are, if anything, understated.
  3. After a fill, record where the production mid went 5/30/60 minutes
     later (adverse selection), and at settlement what the contract paid -
     for the maker fill, and for crossing at the ask instead.

Nothing here sends an order or can: it writes rows to `maker_shadow`.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

from .fees import maker_fee_usd, taker_fee_usd
from .settlement import resolution_of

log = logging.getLogger(__name__)

OPEN, FILLED, PARTIAL, EXPIRED = "OPEN", "FILLED", "PARTIAL", "EXPIRED"
INTERRUPTED = "INTERRUPTED"


@dataclass
class MakerShadowConfig:
    enabled: bool = True
    contracts: int = 10
    improve_cents: int = 1
    min_edge_cents: float = 3.0
    max_rest_seconds: float = 900.0
    close_within_minutes: float = 30.0
    #: wider than the strategy's own 6h so fill rates are measured sooner;
    #: every row carries its lead and stage, and the report splits on both
    max_lead_hours: float = 24.0
    drift_minutes: tuple = (5, 30, 60)
    #: ignore a strategy verdict older than this
    info_max_age_seconds: float = 300.0

    @classmethod
    def from_dict(cls, d: dict | None) -> "MakerShadowConfig":
        d = d or {}
        return cls(
            enabled=bool(d.get("enabled", True)),
            contracts=int(d.get("contracts", 10)),
            improve_cents=int(d.get("improve_cents", 1)),
            min_edge_cents=float(d.get("min_edge_cents", 3.0)),
            max_rest_seconds=float(d.get("max_rest_seconds", 900.0)),
            close_within_minutes=float(d.get("close_within_minutes", 30.0)),
            max_lead_hours=float(d.get("max_lead_hours", 24.0)),
            drift_minutes=tuple(int(x) for x in d.get("drift_minutes", (5, 30, 60))),
            info_max_age_seconds=float(d.get("info_max_age_seconds", 300.0)),
        )


def _fee_cents(fn, price: int, n: int, rate: float, safety: float) -> float:
    return fn(price, n, rate, safety) * 100.0 / max(1, n)


def price_order(q: float, book, side: str, cfg: MakerShadowConfig, fees,
                maker_fee=None) -> dict | None:
    """The C2 3.2 price for a resting buy of `side`, or None if no price
    leaves the edge. `book` is a production DepthBook. `maker_fee(price,
    contracts) -> usd` is the series' published maker fee (Package 8); without
    it, config.yaml's maker_rate."""
    bid = book.best_price(side, "sell")
    ask = book.best_price(side, "buy")
    if bid is None or ask is None:
        return None
    fair = (q if side == "yes" else 1.0 - q) * 100.0
    n = cfg.contracts
    # The fee depends on the price and the price on the fee: take the fee
    # at the highest price we could post, which over-states it slightly.
    ceiling = min(bid + cfg.improve_cents, ask - 1)
    if ceiling < 1:
        return None
    def mfee_at(px: int) -> float:
        if maker_fee is not None:
            return maker_fee(px, n) * 100.0 / max(1, n)
        return _fee_cents(maker_fee_usd, px, n, fees.maker_rate, fees.safety)
    mfee = mfee_at(ceiling)
    price = min(ceiling, math.floor(fair - cfg.min_edge_cents - mfee))
    if price < 1 or price > 99:
        return None
    queue = 0
    if price <= bid:
        for lv in book.ladder(side, "sell"):
            if lv.price_cents == price:
                queue = lv.contracts
                break
    return {"side": side, "price_cents": int(price), "fair_cents": round(fair, 2),
            "best_bid": bid, "best_ask": ask, "queue_ahead": int(queue),
            "maker_fee_cents": round(mfee_at(price), 3),
            "taker_fee_cents": round(_fee_cents(taker_fee_usd, ask, n,
                                                fees.taker_rate, fees.safety), 3),
            "edge_cents": fair - price}


def choose(q: float, book, cfg: MakerShadowConfig, fees, maker_fee=None) -> dict | None:
    """The side with the most edge left at its maker price, if any."""
    plans = [p for p in (price_order(q, book, s, cfg, fees, maker_fee)
                         for s in ("yes", "no")) if p]
    return max(plans, key=lambda p: p["edge_cents"]) if plans else None


def apply_trades(order: dict, trades) -> bool:
    """Fill `order` (a maker_shadow row as a dict, with `queue_left`) from
    production prints. Mutates it; True if anything filled."""
    side, price = order["side"], order["price_cents"]
    changed = False
    for tr in sorted(trades, key=lambda t: t.ts):
        remaining = order["contracts"] - (order.get("filled") or 0)
        if remaining <= 0:
            break
        if tr.ts < order["created_at"] or tr.ts > order["expires_at"]:
            continue
        # A taker who BOUGHT our side was lifting offers, not hitting bids.
        if tr.taker_side == side:
            continue
        px = tr.price(side)
        if px is None or px > price:
            continue            # hit a better bid than ours
        if px < price:
            take, kind = min(remaining, tr.count), "through"
        else:
            ahead = order.get("queue_left") or 0
            eaten = min(ahead, tr.count)
            order["queue_left"] = ahead - eaten
            take, kind = min(remaining, tr.count - eaten), "queue"
        if take <= 0:
            continue
        order["filled"] = (order.get("filled") or 0) + take
        if order.get("first_fill_at") is None:
            order["first_fill_at"] = tr.ts
            order["fill_kind"] = kind
        if order["filled"] >= order["contracts"]:
            order["full_fill_at"] = tr.ts
        changed = True
    return changed


class MakerShadow:
    """Listener on ProductionWatch. Writes `maker_shadow` rows; sends nothing."""

    def __init__(self, journal, fees, cfg: MakerShadowConfig | None = None,
                 watch=None, clock=time.time, versions: dict | None = None,
                 fee_schedule=None):
        self.journal = journal
        #: Package 8: the PRODUCTION fee schedule - the simulated fills are on
        #: production, so its maker fee is the one that applies.
        self.fee_schedule = fee_schedule
        self.fees = fees
        self.cfg = cfg or MakerShadowConfig()
        self.watch = watch
        self.clock = clock
        self.versions = versions or {}
        self.open: dict[str, dict] = {}
        self.pending_drift: dict[int, dict] = {}
        self.posted = 0
        self.fills = 0

    def load(self) -> None:
        """Pick up after a restart. Orders that were resting are closed as
        INTERRUPTED and left out of every statistic: the prints seen before
        the restart are not remembered, so carrying on would either miss
        fills or count them twice. Recent fills still get their drift where
        the history allows."""
        try:
            for r in self.journal.maker_shadow_rows("state=?", (OPEN,)):
                self.journal.maker_shadow_update(
                    r["id"], {"state": INTERRUPTED, "closed_at": self.clock()})
            since = self.clock() - (max(self.cfg.drift_minutes, default=0) * 60 + 900)
            for r in self.journal.maker_shadow_rows(
                    "first_fill_at IS NOT NULL AND first_fill_at > ?", (since,)):
                self.pending_drift[r["id"]] = r
        except Exception as exc:  # noqa: BLE001
            log.error("MAKER SHADOW  could not reload open orders: %s", exc)

    # ---- ProductionWatch listener ---------------------------------------
    def on_book(self, ticker: str, quote, info: dict) -> None:
        now = self.clock()
        self._drift(now)
        if not self.cfg.enabled or ticker in self.open or not info:
            return
        q = info.get("q")
        if q is None or now - float(info.get("at") or 0) > self.cfg.info_max_age_seconds:
            return
        lead = info.get("lead")
        if lead is not None and float(lead) > self.cfg.max_lead_hours:
            return
        expires = now + self.cfg.max_rest_seconds
        close_ts = info.get("close_ts")
        if close_ts:
            latest = float(close_ts) - self.cfg.close_within_minutes * 60
            if latest <= now + 60:
                return
            expires = min(expires, latest)
        maker_fee = None
        if self.fee_schedule is not None:
            try:
                sf = self.fee_schedule.get(ticker.split("-")[0])
                if sf.known:
                    maker_fee = sf.maker_fee_usd
            except Exception:  # noqa: BLE001
                maker_fee = None
        plan = choose(float(q), quote.book, self.cfg, self.fees, maker_fee)
        if plan is None:
            return
        row = {
            "created_at": now, "ticker": ticker, "series": info.get("series"),
            "probability_yes": float(q), "stage": info.get("stage"),
            "reason": (info.get("reason") or "")[:200], "lead_hours": lead,
            "book_age_s": round(now - quote.read_at, 1),
            "contracts": self.cfg.contracts, "expires_at": expires, "state": OPEN,
            "filled": 0, **{k: plan[k] for k in (
                "side", "price_cents", "fair_cents", "best_bid", "best_ask",
                "queue_ahead", "maker_fee_cents", "taker_fee_cents")},
            **{k: self.versions.get(k) for k in ("code_version", "config_version")},
        }
        row["id"] = self.journal.maker_shadow_insert(row)
        row["queue_left"] = row["queue_ahead"]
        self.open[ticker] = row
        self.posted += 1

    def on_trades(self, ticker: str, trades, complete_until: float) -> None:
        o = self.open.get(ticker)
        if o is None:
            return
        if apply_trades(o, trades):
            self.fills += 1
        filled = o.get("filled") or 0
        # Still resting (state OPEN, even part-filled) until it is complete or
        # the tape has been read past its expiry.
        if filled >= o["contracts"] or complete_until >= o["expires_at"]:
            o["state"] = (FILLED if filled >= o["contracts"]
                          else PARTIAL if filled else EXPIRED)
            o["closed_at"] = o.get("full_fill_at") or o["expires_at"]
            del self.open[ticker]
            if o.get("first_fill_at") is not None:
                self.pending_drift[o["id"]] = o
        self.journal.maker_shadow_update(o["id"], {
            k: o.get(k) for k in ("state", "filled", "first_fill_at", "full_fill_at",
                                  "fill_kind", "closed_at")})
        self._drift(self.clock())

    # ---- adverse selection ----------------------------------------------
    def _drift(self, now: float) -> None:
        if self.watch is None or not self.pending_drift:
            return
        for rid, o in list(self.pending_drift.items()):
            fields, outstanding = {}, False
            for m in self.cfg.drift_minutes:
                col = f"drift_{m}m"
                if col not in ("drift_5m", "drift_30m", "drift_60m") or o.get(col) is not None:
                    continue
                target = o["first_fill_at"] + m * 60
                if now < target:
                    outstanding = True
                    continue
                mid = self.watch.mid_at(o["ticker"], target, o["side"])
                if mid is not None:
                    fields[col] = o[col] = round(mid - o["price_cents"], 2)
                elif now < target + 600:
                    outstanding = True       # a read near the target may still come
            if fields:
                self.journal.maker_shadow_update(rid, fields)
            if not outstanding:
                del self.pending_drift[rid]

    # ---- settlement (hourly, from the scoring worker) -------------------
    def resolve(self, market_client, now: float | None = None, max_tickers: int = 100) -> dict:
        now = self.clock() if now is None else now
        rows = self.journal.maker_shadow_rows(
            "state IN (?,?,?) AND settled_yes IS NULL AND closed_at < ?",
            (FILLED, PARTIAL, EXPIRED, now - 3600))
        by_ticker: dict[str, list[dict]] = {}
        for r in rows:
            by_ticker.setdefault(r["ticker"], []).append(r)
        stats = {"settled": 0, "pending": 0}
        for ticker in list(by_ticker)[:max_tickers]:
            try:
                yes = resolution_of(market_client.get_market(ticker))
            except Exception as exc:  # noqa: BLE001
                log.debug("MAKER SHADOW  could not read %s: %s", ticker, exc)
                yes = None
            if yes is None:
                stats["pending"] += len(by_ticker[ticker])
                continue
            for r in by_ticker[ticker]:
                pays = 100.0 if (yes if r["side"] == "yes" else not yes) else 0.0
                fields = {"settled_yes": 1 if yes else 0,
                          "taker_markout_cents": round(
                              pays - r["best_ask"] - (r["taker_fee_cents"] or 0), 3)}
                if (r["filled"] or 0) > 0:
                    fields["maker_markout_cents"] = round(
                        pays - r["price_cents"] - (r["maker_fee_cents"] or 0), 3)
                self.journal.maker_shadow_update(r["id"], fields)
                stats["settled"] += 1
        if stats["settled"]:
            log.info("MAKER SHADOW  settled %d simulated orders (%d still pending)",
                     stats["settled"], stats["pending"])
        return stats

    def summary(self) -> str:
        return (f"{self.posted} posted, {self.fills} fill events, "
                f"{len(self.open)} resting")
