"""Packages 6/7 (first cut): a FAKE-MONEY account on the REAL exchange's prices.

Why. On 28 September the production weather markets traded 1,000-5,400 times
a day per city at 1c spreads; the same markets on demo traded 6-32 times at
spreads up to 94c. Demo cannot say whether this strategy makes money against
a real market, and the arbitrage it has profited from lives in demo's broken
books. This runs ALONGSIDE demo trading (which is unchanged) and answers the
question on real prices, with nothing at risk.

  Forecast entries   the SAME pricing code as the live strategy - a second
                     ForecastStrategy on the same estimator and config, handed
                     the production book instead of the demo one - so every
                     rule (edge, confidence, price band, spread, depth, Kelly
                     sizing, caps) is identical; then the same profitability
                     gate. The market-skill gate is recorded, not obeyed:
                     `gate_blocked` rows are what the model WOULD have done if
                     allowed, and the report keeps them apart - that is the
                     evidence the gate needs.
  Arbitrage          ProdArbCheck re-runs every event the arbitrage sweep
                     inspected against the PRODUCTION books (read only, own
                     budget) and records whether the structure exists there
                     too; one the gate accepts is paper-traded.

  Fills              taker, walking the production book at the moment it was
                     read (no queue, no latency - an upper bound for taker
                     execution; the maker shadow measures resting orders).
  Exits              held to settlement (v1). The demo strategy's exits are
                     not replicated; this measures the entry decision.
  Money              starting_balance_usd, fake; exposure and position caps.

Nothing here can send an order: production is reached only through
LiveBookReader (GET, unauthenticated, allowlisted), and this module has no
execution client.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass

from .settlement import resolution_of

log = logging.getLogger(__name__)

OPEN, SETTLED, VOID = "OPEN", "SETTLED", "VOID"


@dataclass
class PaperConfig:
    enabled: bool = True
    starting_balance_usd: float = 500.0
    max_open_positions: int = 30
    max_total_exposure_usd: float = 250.0
    #: paper-trade entries the market-skill gate blocks (labelled)
    include_gate_blocked: bool = True
    info_max_age_seconds: float = 300.0
    arb_enabled: bool = True
    arb_max_events_per_sweep: int = 60
    arb_requests_per_second: float = 1.0

    @classmethod
    def from_dict(cls, d: dict | None) -> "PaperConfig":
        d = d or {}
        a = d.get("arb_check") or {}
        return cls(
            enabled=bool(d.get("enabled", True)),
            starting_balance_usd=float(d.get("starting_balance_usd", 500.0)),
            max_open_positions=int(d.get("max_open_positions", 30)),
            max_total_exposure_usd=float(d.get("max_total_exposure_usd", 250.0)),
            include_gate_blocked=bool(d.get("include_gate_blocked", True)),
            info_max_age_seconds=float(d.get("info_max_age_seconds", 300.0)),
            arb_enabled=bool(a.get("enabled", True)),
            arb_max_events_per_sweep=int(a.get("max_events_per_sweep", 60)),
            arb_requests_per_second=float(a.get("requests_per_second", 1.0)),
        )


class PaperLiveAccount:
    def __init__(self, journal, gate, clone=None, market_skill=None,
                 cfg: PaperConfig | None = None, clock=time.time,
                 versions: dict | None = None):
        """`clone` is a ForecastStrategy with no journal and no market-skill
        gate, sharing the live strategy's estimator and config. `gate` is the
        engine's ProfitabilityGate."""
        self.journal = journal
        self.gate = gate
        self.clone = clone
        self.market_skill = market_skill
        self.cfg = cfg or PaperConfig()
        self.clock = clock
        self.versions = versions or {}
        self._lock = threading.Lock()
        self.entered = 0
        self.declined: dict[str, int] = {}

    # ---- the account ----------------------------------------------------
    def state(self) -> dict:
        rows = self.journal.paper_rows()
        realised = sum(r["pnl_usd"] or 0.0 for r in rows if r["state"] == SETTLED)
        open_rows = [r for r in rows if r["state"] == OPEN]
        tied = sum((r["cost_usd"] or 0) + (r["fee_usd"] or 0) for r in open_rows)
        cash = self.cfg.starting_balance_usd + realised - tied
        return {"cash": cash, "realised": realised, "open": len(open_rows),
                "exposure": tied, "open_tickers": {r["ticker"] for r in open_rows},
                "open_groups": len({r["group_id"] for r in open_rows})}

    def _decline(self, why: str) -> None:
        self.declined[why] = self.declined.get(why, 0) + 1

    def _room(self, st: dict, cost: float) -> str | None:
        if st["open_groups"] >= self.cfg.max_open_positions:
            return "max open positions"
        if st["exposure"] + cost > self.cfg.max_total_exposure_usd:
            return "exposure cap"
        if cost > st["cash"]:
            return "not enough paper cash"
        return None

    def _book(self, op, kind: str, extra: dict) -> None:
        group = uuid.uuid4().hex[:12]
        now = self.clock()
        per_leg_fee = (op.estimated_fees or 0.0) / max(1, len(op.legs))
        for leg in op.legs:
            self.journal.paper_insert({
                "opened_at": now, "kind": kind, "group_id": group,
                "strategy": op.strategy_type, "event_id": op.event_id,
                "ticker": leg.ticker, "side": leg.side, "contracts": leg.quantity,
                "price_cents": round(leg.vwap_cents, 3),
                "cost_usd": round(leg.cost_usd, 4), "fee_usd": round(per_leg_fee, 4),
                "expected_edge_usd": round((op.net_edge or 0.0) / len(op.legs), 4),
                "state": OPEN, **extra,
                **{k: self.versions.get(k) for k in ("code_version", "config_version")},
            })
        self.entered += 1

    # ---- forecast entries (ProductionWatch listener) ---------------------
    def on_book(self, ticker: str, quote, info: dict) -> None:
        if not self.cfg.enabled or self.clone is None or not info:
            return
        m, est = info.get("market"), info.get("estimate")
        if m is None or est is None or not getattr(est, "usable", False):
            return
        if self.clock() - float(info.get("at") or 0) > self.cfg.info_max_age_seconds:
            return
        with self._lock:
            st = self.state()
            if ticker in st["open_tickers"]:
                return
            self.clone.skips = []
            op = self.clone._price_one(m, quote.book, max(0.0, st["cash"]), est)
            if op is None:
                why = self.clone.skips[-1].reason if self.clone.skips else "declined"
                self._decline(why)
                return
            decision = self.gate.evaluate(op)
            if not decision.accepted:
                self._decline(f"gate: {decision.reason.value}")
                return
            blocked, why_blocked = False, ""
            if self.market_skill is not None:
                ok, why_blocked = self.market_skill.may_enter(
                    m.series_ticker or ticker.split("-")[0],
                    (est.evidence or {}).get("lead_hours"))
                blocked = not ok
            if blocked and not self.cfg.include_gate_blocked:
                self._decline("market-skill gate")
                return
            room = self._room(st, op.gross_cost + (op.estimated_fees or 0.0))
            if room:
                self._decline(room)
                return
            ev = est.evidence or {}
            self._book(op, "FORECAST", {
                "probability_yes": est.probability_yes,
                "confidence": est.confidence, "lead_hours": ev.get("lead_hours"),
                "model": ev.get("model"), "gate_blocked": 1 if blocked else 0,
                "note": (why_blocked or "")[:200],
                "book_age_s": round(self.clock() - quote.read_at, 1)})
            log.info("PAPER LIVE  %s %d %s %s @ %.1fc (fake money%s)",
                     "BUY", op.legs[0].quantity, op.legs[0].side.upper(), ticker,
                     op.legs[0].vwap_cents, "; gate would block" if blocked else "")

    # ---- arbitrage entries (ProdArbCheck) --------------------------------
    def enter_arb(self, op) -> bool:
        if not self.cfg.enabled:
            return False
        with self._lock:
            st = self.state()
            if any(leg.ticker in st["open_tickers"] for leg in op.legs):
                self._decline("arb: already holding a leg")
                return False
            room = self._room(st, op.gross_cost + (op.estimated_fees or 0.0))
            if room:
                self._decline(f"arb: {room}")
                return False
            self._book(op, "ARB", {"gate_blocked": 0})
            log.info("PAPER LIVE  ARBITRAGE %s on production books: %d sets, "
                     "net edge $%.2f (fake money)", op.event_id, op.quantity,
                     op.net_edge or 0.0)
            return True

    # ---- settlement (hourly, scoring worker) -----------------------------
    def resolve(self, market_client, max_tickers: int = 150) -> dict:
        rows = self.journal.paper_rows("state=?", (OPEN,))
        by_ticker: dict[str, list[dict]] = {}
        for r in rows:
            by_ticker.setdefault(r["ticker"], []).append(r)
        stats = {"settled": 0, "pending": 0}
        for ticker in list(by_ticker)[:max_tickers]:
            try:
                yes = resolution_of(market_client.get_market(ticker))
            except Exception as exc:  # noqa: BLE001
                log.debug("PAPER LIVE  could not read %s: %s", ticker, exc)
                yes = None
            if yes is None:
                stats["pending"] += len(by_ticker[ticker])
                continue
            for r in by_ticker[ticker]:
                pays = yes if r["side"] == "yes" else not yes
                payout = r["contracts"] * (1.0 if pays else 0.0)
                self.journal.paper_update(r["id"], {
                    "state": SETTLED, "settled_at": self.clock(),
                    "settled_yes": 1 if yes else 0, "payout_usd": payout,
                    "pnl_usd": round(payout - r["cost_usd"] - r["fee_usd"], 4)})
                stats["settled"] += 1
        if stats["settled"]:
            st = self.state()
            log.info("PAPER LIVE  settled %d position(s); fake balance $%.2f, "
                     "realised %+.2f", stats["settled"], st["cash"] + st["exposure"],
                     st["realised"])
        return stats

    def summary(self) -> str:
        st = self.state()
        top = ", ".join(f"{k} {v}" for k, v in
                        sorted(self.declined.items(), key=lambda x: -x[1])[:4])
        return (f"fake balance ${st['cash'] + st['exposure']:.2f} (cash ${st['cash']:.2f}, "
                f"{st['open']} open), realised {st['realised']:+.2f}, "
                f"{self.entered} entered" + (f"; declined: {top}" if top else ""))


class ProdArbCheck:
    """Does an arbitrage the sweep sees on demo exist on PRODUCTION?"""

    def __init__(self, reader, scanner, gate, journal, paper=None,
                 cfg: PaperConfig | None = None, limiter=None, clock=time.time):
        from .prodwatch import RateLimiter
        self.reader = reader
        self.scanner = scanner
        self.gate = gate
        self.journal = journal
        self.paper = paper
        self.cfg = cfg or PaperConfig()
        self.limiter = limiter or RateLimiter(self.cfg.arb_requests_per_second)
        self.clock = clock
        self._q: deque = deque(maxlen=max(1, self.cfg.arb_max_events_per_sweep))
        self._lock = threading.Lock()
        self.checked = 0
        self.found_prod = 0

    def observe(self, event_id: str, markets, tickers, demo_found, exclusive: bool) -> None:
        """Called by the sweep for every event it inspected. Never blocks."""
        if not self.cfg.arb_enabled:
            return
        best = max((op.net_edge or 0.0) for op in demo_found) if demo_found else None
        with self._lock:
            self._q.append((event_id, list(markets), list(tickers), len(demo_found),
                            best, bool(exclusive)))

    def run_pass(self) -> int:
        done = 0
        while True:
            with self._lock:
                if not self._q:
                    break
                item = self._q.popleft()
            self._check(*item)
            done += 1
        return done

    def _check(self, event_id, markets, tickers, demo_n, demo_best, exclusive) -> None:
        books = {}
        for t in tickers:
            self.limiter.wait()
            try:
                b = self.reader.orderbook(t)
            except Exception as exc:  # noqa: BLE001
                log.debug("PROD ARB  %s: %s", t, exc)
                b = None
            if b is not None and (b.best_price("yes", "buy") or b.best_price("no", "buy")):
                books[t] = b
        found, accepted = [], []
        gap = None
        if books:
            try:
                self.scanner.reset_near_misses()
                found = self.scanner.scan_event(event_id, markets, books, exclusive)
                nm = self.scanner.near_misses(1)
                gap = nm[0].gap_cents if nm else None
            except Exception as exc:  # noqa: BLE001
                log.error("PROD ARB  scan of %s failed: %s", event_id, exc)
                found = []
            for op in found:
                op.market_price_source = "production"
                if self.gate.evaluate(op).accepted:
                    accepted.append(op)
        self.checked += 1
        self.found_prod += len(accepted)
        self.journal.prod_arb_insert({
            "ts": self.clock(), "event_id": event_id, "markets": len(tickers),
            "prod_books": len(books), "demo_found": demo_n, "demo_best_edge": demo_best,
            "prod_found": len(found), "prod_accepted": len(accepted),
            "prod_best_edge": max((op.net_edge or 0.0) for op in found) if found else None,
            "prod_near_miss_gap": gap})
        for op in accepted:
            if self.paper is not None:
                self.paper.enter_arb(op)

    def summary(self) -> str:
        return f"{self.checked} events checked on production, {self.found_prod} tradable there"
