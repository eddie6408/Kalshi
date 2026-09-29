"""WX-1: the live registry of every Kalshi weather market.

Refreshed by the research worker. Reads production's public market data
(GET only, no credential - livebooks.LiveBookReader), classifies each market
from its own rules, versions the rules text by hash, and keeps a lifecycle
status per market:

    DISCOVERED -> CLASSIFIED -> RESEARCH_ONLY -> MODEL_READY -> PAPER_TRADING
               -> PRODUCTION_ELIGIBLE (a label; production stays locked) | DISABLED

What it can change in trading: only REMOVE. A live daily series whose rules
now name a different station or source is DISABLED here and the daily
estimator skips it (`blocked_series`). Nothing here adds a series to trading.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from . import taxonomy as tx
from .store import utcnow

log = logging.getLogger(__name__)

CATEGORY = "Climate and Weather"
RANK = {s: i for i, s in enumerate(tx.STATUSES)}


def market_state(status: str | None) -> str:
    """Kalshi's market status -> open / closed / settled (it says "active"
    for a market that is trading)."""
    s = (status or "").lower()
    if s in ("", "active", "open", "initialized", "unopened"):
        return "open"
    if s in ("settled", "finalized", "determined"):
        return "settled"
    return "closed"


def with_backoff(fn, tries: int = 4, base: float = 2.0, sleep=time.sleep):
    """Retry a Kalshi read on 429 / 5xx with exponential backoff. Kalshi's
    public API is shared with the bot's own production reads."""
    for i in range(tries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            code = getattr(getattr(exc, "response", None), "status_code", None)
            if code not in (429, 500, 502, 503, 504) or i == tries - 1:
                raise
            sleep(base * (2 ** i))
    return None


def _f(x):
    try:
        return None if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return None


class WeatherRegistry:
    def __init__(self, store, reader, quota, live_series: dict | None = None,
                 max_pages: int = 80):
        self.store = store
        self.reader = reader
        self.quota = quota
        #: {series prefix: CLI code} of the daily series the strategy trades.
        self.live_series = {k.upper(): (v or "").upper() for k, v in (live_series or {}).items()}
        self.max_pages = max_pages
        self.last_summary: dict = {}
        self.partial = False

    # ------------------------------------------------------------------ io
    def _series(self) -> dict[str, dict]:
        if not self.quota.allow("kalshi_public"):
            return {}
        rows = with_backoff(lambda: self.reader.series_list(CATEGORY))
        now = utcnow()
        known = {r["series"] for r in self.store.query("SELECT series FROM wx_series")}
        new = []
        for s in rows:
            t = s.get("ticker")
            if not t:
                continue
            if known and t not in known:
                new.append(t)
            self.store.execute(
                "INSERT INTO wx_series (series, title, frequency, contract_url, "
                "settlement_sources, first_seen, last_seen) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(series) DO UPDATE SET title=excluded.title, "
                "frequency=excluded.frequency, contract_url=excluded.contract_url, "
                "settlement_sources=excluded.settlement_sources, last_seen=excluded.last_seen",
                (t, s.get("title"), s.get("frequency"), s.get("contract_url"),
                 json.dumps(s.get("settlement_sources") or []), now, now))
        for t in new:
            log.warning("WX REGISTRY  new weather series listed: %s", t)
            self.store.alert("new_series", t, "listed on Kalshi; classified on this pass")
        return {s["ticker"]: s for s in rows if s.get("ticker")}

    def _open_events(self, series: dict) -> list[dict]:
        events, cursor = [], None
        for _ in range(self.max_pages):
            if not self.quota.allow("kalshi_public"):
                break
            try:
                page = with_backoff(lambda: self.reader.events_page(status="open", cursor=cursor))
            except Exception as exc:  # noqa: BLE001
                log.warning("WX REGISTRY  event paging stopped early (%s); classified what was read",
                            exc)
                self.partial = True
                break
            events += [e for e in page.get("events") or [] if e.get("series_ticker") in series]
            cursor = page.get("cursor")
            if not cursor:
                break
        return events

    # ------------------------------------------------------------ classify
    def class_status(self) -> dict[str, str]:
        return {r["cls"]: r["status"] for r in
                self.store.query("SELECT cls, status FROM wx_class_status")}

    def status_for(self, c: tx.Contract, class_status: dict) -> tuple[str, str]:
        status, reason = tx.initial_status(c, self.live_series)
        cs = class_status.get(c.cls)
        # A validated class lifts its RESEARCH_ONLY markets; never past
        # PAPER_TRADING automatically, and never a DISABLED market.
        if status == "RESEARCH_ONLY" and c.parse_ok and c.cls in tx.MODELLED and cs in (
                "MODEL_READY", "PAPER_TRADING", "PRODUCTION_ELIGIBLE"):
            status = cs if cs != "PRODUCTION_ELIGIBLE" else "PAPER_TRADING"
            reason = f"class {c.cls} validated to {cs} (WX-7)"
        return status, reason

    def upsert(self, market: dict, series: dict | None, class_status: dict) -> dict:
        c = tx.classify(market, series)
        sha = tx.rules_hash(market.get("rules_primary"), market.get("rules_secondary"))
        now = utcnow()
        last = self.store.one("SELECT version, sha256 FROM wx_rules WHERE ticker=? "
                              "ORDER BY version DESC LIMIT 1", (c.ticker,))
        changed = False
        if last is None or last["sha256"] != sha:
            version = 1 if last is None else last["version"] + 1
            self.store.execute(
                "INSERT INTO wx_rules (ticker, version, sha256, rules_primary, rules_secondary, "
                "retrieved_at, parser_version) VALUES (?,?,?,?,?,?,?)",
                (c.ticker, version, sha, market.get("rules_primary"),
                 market.get("rules_secondary"), now, tx.PARSER_VERSION))
            changed = last is not None
        else:
            version = last["version"]
        status, reason = self.status_for(c, class_status)
        if changed:
            reason = f"RULES CHANGED (v{version}) - revalidate; " + reason
            self.store.alert("rules_changed", c.ticker, {"version": version, "sha256": sha})
            log.warning("WX REGISTRY  rules changed for %s (now v%d)", c.ticker, version)
            if status not in ("DISABLED",):
                status = "RESEARCH_ONLY" if c.cls != "OTHER" else "DISABLED"
        prev = self.store.one("SELECT status FROM wx_contracts WHERE ticker=?", (c.ticker,))
        d = c.as_dict()
        self.store.execute(
            "INSERT INTO wx_contracts (ticker, event_ticker, series, title, cls, variable, "
            "aggregation, operator, threshold, threshold_hi, run_k, unit, window_start, "
            "window_end, at_time, station, cli, location, source, source_text, rounding, "
            "min_hours, revision_policy, strike_type, floor_strike, cap_strike, close_time, "
            "parse_ok, notes, status, status_reason, rules_version, parser_version, "
            "market_status, yes_bid, yes_ask, volume, first_seen, last_seen, classified_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(ticker) DO UPDATE SET cls=excluded.cls, variable=excluded.variable, "
            "aggregation=excluded.aggregation, operator=excluded.operator, "
            "threshold=excluded.threshold, threshold_hi=excluded.threshold_hi, "
            "run_k=excluded.run_k, window_start=excluded.window_start, "
            "window_end=excluded.window_end, at_time=excluded.at_time, station=excluded.station, "
            "cli=excluded.cli, location=excluded.location, source=excluded.source, "
            "source_text=excluded.source_text, rounding=excluded.rounding, "
            "min_hours=excluded.min_hours, revision_policy=excluded.revision_policy, "
            "close_time=excluded.close_time, parse_ok=excluded.parse_ok, notes=excluded.notes, "
            "status=excluded.status, status_reason=excluded.status_reason, "
            "rules_version=excluded.rules_version, parser_version=excluded.parser_version, "
            "market_status=excluded.market_status, yes_bid=excluded.yes_bid, "
            "yes_ask=excluded.yes_ask, volume=excluded.volume, last_seen=excluded.last_seen, "
            "classified_at=excluded.classified_at",
            (d["ticker"], d["event_ticker"], d["series"], d["title"], d["cls"], d["variable"],
             d["aggregation"], d["operator"], d["threshold"], d["threshold_hi"], d["run_k"],
             d["unit"], d["window_start"], d["window_end"], d["at_time"], d["station"], d["cli"],
             d["location"], d["source"], d["source_text"], d["rounding"], d["min_hours"],
             d["revision_policy"], d["strike_type"], d["floor_strike"], d["cap_strike"],
             d["close_time"], int(d["parse_ok"]), json.dumps(d["notes"]), status, reason,
             version, tx.PARSER_VERSION, market_state(market.get("status")),
             _f(market.get("yes_bid_dollars")), _f(market.get("yes_ask_dollars")),
             _f(market.get("volume_fp") or market.get("volume")), now, now, now))
        if prev is None or prev["status"] != status:
            self.store.execute(
                "INSERT INTO wx_status_log (at, ticker, cls, old, new, reason) VALUES (?,?,?,?,?,?)",
                (now, c.ticker, c.cls, prev["status"] if prev else "DISCOVERED", status, reason))
        return {"cls": c.cls, "status": status, "series": c.series, "source": c.source}

    # ---------------------------------------------------------------- pass
    def refresh(self) -> dict:
        self.partial = False
        series = self._series()
        if not series:
            return {"ok": False, "reason": "series list unavailable or quota"}
        events = self._open_events(series)
        cs = self.class_status()
        counts: dict = {}
        seen = set()
        for e in events:
            s = series.get(e.get("series_ticker"))
            for m in e.get("markets") or []:
                m.setdefault("event_ticker", e.get("event_ticker"))
                m.setdefault("series_ticker", e.get("series_ticker"))
                try:
                    r = self.upsert(m, s, cs)
                except Exception as exc:  # noqa: BLE001
                    log.error("WX REGISTRY  could not classify %s: %s", m.get("ticker"), exc)
                    continue
                seen.add(m.get("ticker"))
                key = f"{r['cls']}/{r['status']}"
                counts[key] = counts.get(key, 0) + 1
        # Markets that dropped off the open list: closed or settled. The
        # resolution pass asks for their results.
        now = datetime.now(timezone.utc).isoformat()
        self.store.execute(
            "UPDATE wx_contracts SET market_status='closed' WHERE market_status='open' "
            "AND close_time < ?", (now,))
        self.last_summary = {"ok": True, "partial": self.partial, "at": utcnow(), "series": len(series),
                             "events": len(events), "markets": len(seen), "by_class": counts}
        log.info("WX REGISTRY  %d series, %d open events, %d markets classified",
                 len(series), len(events), len(seen))
        return self.last_summary

    # ------------------------------------------------------------ queries
    def blocked_series(self) -> set[str]:
        """Live daily series the registry has DISABLED (settlement mismatch):
        the daily estimator skips them."""
        rows = self.store.query(
            "SELECT DISTINCT series FROM wx_contracts WHERE status='DISABLED' "
            "AND cls IN ('DAILY_HIGH','DAILY_LOW') AND market_status='open'")
        return {r["series"].upper() for r in rows}

    def open_contracts(self, classes=None, statuses=None) -> list[dict]:
        sql = "SELECT * FROM wx_contracts WHERE market_status='open'"
        args: list = []
        if classes:
            sql += f" AND cls IN ({','.join('?' * len(classes))})"
            args += list(classes)
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        return self.store.query(sql, args)
