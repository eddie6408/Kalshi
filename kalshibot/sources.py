"""Where the bot's outside information comes from, and whether it is working.

Package 14A (docs/packages/14A-source-provenance.md). Three things live here:

    SourceRegistry   the approved sources, read from config.yaml `sources:`.
                     Who publishes each one, how authoritative it is, what it
                     is for, how old its data may be, and what - if anything -
                     is allowed to stand in for it.
    SourceMonitor    health per source: latency, HTTP status, error rate,
                     consecutive failures, age of the newest data.
    provenance       one journal row per external fetch that feeds a
                     decision, so "where did this number come from?" always
                     has an answer.

Nothing in this module changes a trading decision. It measures and records.
The gates that act on it (package 14A phase 4) are a separate, later change,
with thresholds set from what this records.

The rule it exists to enforce, eventually: the bot never trades on "the
internet said". Every input has a named publisher, a tier, a timestamp and a
row in the journal - or it is not an input.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

log = logging.getLogger(__name__)

#: Tier 1 is the organisation responsible for the information; tier 4 is the
#: general internet. Section 14A "Source hierarchy".
TIERS = {
    1: "primary / authoritative",
    2: "established institutional",
    3: "secondary",
    4: "general internet",
}

#: What a source may be used for. Every role except `discovery` can reach a
#: trading decision, which is why tier 4 is only ever allowed `discovery`.
ROLES = {
    "market_data",   # the exchange's own books, markets, orders
    "primary",       # the main input to an estimator
    "fallback",      # authorised stand-in when the primary fails
    "cross_check",   # independent second opinion on the primary
    "observation",   # what actually happened (measured, not forecast)
    "settlement",    # what the contract resolves on
    "reference",     # metadata about another source (e.g. model run times)
    "discovery",     # leads only; must be verified before use
}

HEALTHY, DEGRADED, DOWN, UNKNOWN, OFF = "HEALTHY", "DEGRADED", "DOWN", "UNKNOWN", "OFF"


class SourceConfigError(ValueError):
    """The `sources:` section of config.yaml breaks a 14A rule."""


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Source:
    id: str
    publisher: str
    tier: int
    roles: tuple[str, ...]
    endpoint: str
    structured: bool = True
    max_data_age_seconds: float | None = None
    required_by: tuple[str, ...] = ()
    fallback_to: str | None = None
    read_by_bot: bool = True
    notes: str = ""

    @property
    def can_reach_a_decision(self) -> bool:
        return any(r != "discovery" for r in self.roles)


class SourceRegistry:
    def __init__(self, sources: dict[str, Source] | None = None):
        self._sources = dict(sources or {})

    @classmethod
    def from_config(cls, section: dict | None) -> "SourceRegistry":
        raw = ((section or {}).get("registry") or {})
        if not isinstance(raw, dict):
            raise SourceConfigError("sources.registry must be a mapping of id -> source")
        built: dict[str, Source] = {}
        for sid, spec in raw.items():
            if not isinstance(spec, dict):
                raise SourceConfigError(f"source {sid!r}: expected a mapping")
            roles = spec.get("roles") or []
            if isinstance(roles, str):
                roles = [roles]
            age = spec.get("max_data_age_seconds")
            built[sid] = Source(
                id=str(sid),
                publisher=str(spec.get("publisher") or ""),
                tier=int(spec.get("tier", 0) or 0),
                roles=tuple(str(r) for r in roles),
                endpoint=str(spec.get("endpoint") or ""),
                structured=bool(spec.get("structured", True)),
                max_data_age_seconds=float(age) if age is not None else None,
                required_by=tuple(str(x) for x in (spec.get("required_by") or [])),
                fallback_to=(str(spec["fallback_to"])
                             if spec.get("fallback_to") else None),
                read_by_bot=bool(spec.get("read_by_bot", True)),
                notes=str(spec.get("notes") or ""),
            )
        reg = cls(built)
        reg.validate()
        return reg

    # ------------------------------------------------------------------
    def validate(self) -> None:
        """Refuse a registry that breaks the 14A rules. Raises on the first."""
        for s in self._sources.values():
            if not s.publisher:
                raise SourceConfigError(f"source {s.id!r}: publisher is required "
                                        "- WHO published it is the first question")
            if s.tier not in TIERS:
                raise SourceConfigError(f"source {s.id!r}: tier must be 1-4, got {s.tier}")
            if not s.roles:
                raise SourceConfigError(f"source {s.id!r}: at least one role is required")
            unknown = [r for r in s.roles if r not in ROLES]
            if unknown:
                raise SourceConfigError(f"source {s.id!r}: unknown role(s) {unknown}")
            if s.tier == 4 and s.can_reach_a_decision:
                raise SourceConfigError(
                    f"source {s.id!r}: tier 4 (general internet) may only have the "
                    "'discovery' role. It can suggest where to look; it cannot be "
                    "an input to a trade.")
            if s.tier == 4 and s.required_by:
                raise SourceConfigError(
                    f"source {s.id!r}: no strategy may require a tier-4 source")
            if s.fallback_to is not None:
                target = self._sources.get(s.fallback_to)
                if target is None:
                    raise SourceConfigError(
                        f"source {s.id!r}: fallback_to {s.fallback_to!r} is not in "
                        "the registry. Fallbacks are named in advance, never "
                        "improvised.")
                if target.tier == 4:
                    raise SourceConfigError(
                        f"source {s.id!r}: fallback {target.id!r} is tier 4. A "
                        "missing authoritative source means NO TRADE, not a "
                        "lower-quality substitute.")
        for s in self._sources.values():
            self.chain(s.id)            # raises on a cycle

    def chain(self, source_id: str) -> list[str]:
        """PRIMARY -> AUTHORISED FALLBACK -> ... ; after the last, NO DATA."""
        out: list[str] = []
        cur: str | None = source_id
        while cur is not None:
            if cur in out:
                raise SourceConfigError(
                    f"fallback cycle: {' -> '.join(out + [cur])}")
            if cur not in self._sources:
                break
            out.append(cur)
            cur = self._sources[cur].fallback_to
        return out

    def get(self, source_id: str) -> Source | None:
        return self._sources.get(source_id)

    def required_by(self, strategy: str) -> list[Source]:
        return [s for s in self._sources.values() if strategy in s.required_by]

    def __iter__(self) -> Iterator[Source]:
        return iter(self._sources.values())

    def __len__(self) -> int:
        return len(self._sources)

    def __contains__(self, source_id: str) -> bool:
        return source_id in self._sources


# ----------------------------------------------------------------------
# Health
# ----------------------------------------------------------------------
def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def to_utc(value: Any) -> datetime | None:
    """Parse an ISO timestamp (or pass a datetime through) as aware UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


@dataclass
class SourceHealth:
    source_id: str
    window: int = 20
    calls: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_success_at: float | None = None
    last_failure_at: float | None = None
    last_status: int | None = None
    last_error: str = ""
    last_latency_ms: float | None = None
    last_data_time: datetime | None = None
    recent: deque = field(default_factory=deque)

    @property
    def error_rate(self) -> float:
        if not self.recent:
            return 0.0
        return sum(1 for ok in self.recent if not ok) / len(self.recent)


class _Call:
    """What a caller can tell the monitor about one request, from inside it."""

    def __init__(self):
        self.status: int | None = None
        self.data_time: Any = None
        self.ok: bool | None = None       # force a verdict (e.g. 200 but empty)
        self.error: str = ""
        self.latency_ms: float | None = None


class SourceMonitor:
    """Thread-safe health counters and the provenance hand-off to the journal.

    Health is judged on:
      DOWN      `down_after` failures in a row
      DEGRADED  the last call failed, or more than `degraded_error_rate` of the
                last `window` calls failed, or the newest data is older than the
                source's `max_data_age_seconds`
      HEALTHY   otherwise
      UNKNOWN   never called
      OFF       reported by a probe for a source that is deliberately disabled
    """

    def __init__(self, registry: SourceRegistry | None = None, *,
                 down_after: int = 3, degraded_error_rate: float = 0.25,
                 window: int = 20, clock: Callable[[], float] = time.time):
        self.registry = registry or SourceRegistry()
        self.down_after = max(1, int(down_after))
        self.degraded_error_rate = float(degraded_error_rate)
        self.window = max(1, int(window))
        self.clock = clock
        self._health: dict[str, SourceHealth] = {}
        self._probes: dict[str, Callable[[], tuple[str, str]]] = {}
        self._sink: Callable[..., int | None] | None = None
        self._lock = threading.Lock()

    # ---- configuration ------------------------------------------------
    def configure(self, registry: SourceRegistry | None = None, *,
                  down_after: int | None = None,
                  degraded_error_rate: float | None = None,
                  window: int | None = None) -> None:
        with self._lock:
            if registry is not None:
                self.registry = registry
            if down_after is not None:
                self.down_after = max(1, int(down_after))
            if degraded_error_rate is not None:
                self.degraded_error_rate = float(degraded_error_rate)
            if window is not None:
                self.window = max(1, int(window))

    def set_sink(self, sink: Callable[..., int | None] | None) -> None:
        """Where provenance rows go. The journal, in production."""
        self._sink = sink

    def add_probe(self, source_id: str, probe: Callable[[], tuple[str, str]]) -> None:
        """For a source with its own status (the WebSocket feed): a callable
        returning (state, detail) that replaces the counted verdict."""
        self._probes[source_id] = probe

    # ---- recording ----------------------------------------------------
    def _entry(self, source_id: str) -> SourceHealth:
        h = self._health.get(source_id)
        if h is None:
            h = SourceHealth(source_id, window=self.window,
                             recent=deque(maxlen=self.window))
            self._health[source_id] = h
        return h

    def record(self, source_id: str, ok: bool, *, status: int | None = None,
               latency_ms: float | None = None, error: str = "",
               data_time: Any = None) -> None:
        now = self.clock()
        with self._lock:
            h = self._entry(source_id)
            h.calls += 1
            h.recent.append(bool(ok))
            h.last_status = status
            h.last_latency_ms = latency_ms
            if ok:
                h.consecutive_failures = 0
                h.last_success_at = now
                dt_ = to_utc(data_time)
                if dt_ is not None and (h.last_data_time is None
                                        or dt_ > h.last_data_time):
                    h.last_data_time = dt_
            else:
                h.failures += 1
                h.consecutive_failures += 1
                h.last_failure_at = now
                h.last_error = (error or "")[:300]

    @contextmanager
    def track(self, source_id: str) -> Iterator[_Call]:
        """Time one request and record its outcome. Exceptions are recorded
        as failures and re-raised untouched - tracking never changes what the
        caller sees."""
        call = _Call()
        start = self.clock()
        try:
            yield call
        except BaseException as exc:
            call.latency_ms = (self.clock() - start) * 1000.0
            status = call.status
            if status is None:
                resp = getattr(exc, "response", None)
                status = getattr(resp, "status_code", None) or getattr(exc, "status", None)
            # call.ok = True lets a caller say "this error is ours, not the
            # source's" - a 404 for a market that does not exist.
            ok = call.ok is True
            self.record(source_id, ok, status=status,
                        latency_ms=call.latency_ms,
                        error="" if ok else f"{type(exc).__name__}: {exc}")
            raise
        call.latency_ms = (self.clock() - start) * 1000.0
        ok = call.ok if call.ok is not None else (
            call.status is None or call.status < 400)
        self.record(source_id, ok, status=call.status, latency_ms=call.latency_ms,
                    error=call.error, data_time=call.data_time)

    def observe(self, source_id: str, **fields: Any) -> int | None:
        """Write one provenance row. Returns its id, or None if there is no
        sink or the write failed. Never raises: provenance is a record of the
        decision, and losing a record must not stop the decision."""
        if self._sink is None:
            return None
        src = self.registry.get(source_id)
        row = {
            "source_id": source_id,
            "publisher": src.publisher if src else "",
            "tier": src.tier if src else None,
            "endpoint": fields.pop("endpoint", None) or (src.endpoint if src else ""),
            **fields,
        }
        try:
            return self._sink(**row)
        except Exception as exc:  # noqa: BLE001
            log.debug("provenance write failed for %s: %s", source_id, exc)
            return None

    # ---- reading ------------------------------------------------------
    def state(self, source_id: str) -> str:
        probe = self._probes.get(source_id)
        if probe is not None:
            try:
                return probe()[0]
            except Exception:  # noqa: BLE001
                return UNKNOWN
        with self._lock:
            h = self._health.get(source_id)
            return self._judge(h) if h else UNKNOWN

    def _judge(self, h: SourceHealth) -> str:
        if h.calls == 0:
            return UNKNOWN
        if h.consecutive_failures >= self.down_after:
            return DOWN
        if h.consecutive_failures > 0:
            return DEGRADED
        if len(h.recent) >= 4 and h.error_rate > self.degraded_error_rate:
            return DEGRADED
        src = self.registry.get(h.source_id)
        if (src and src.max_data_age_seconds and h.last_data_time is not None):
            age = self.clock() - h.last_data_time.timestamp()
            if age > src.max_data_age_seconds:
                return DEGRADED
        return HEALTHY

    def snapshot(self) -> list[dict]:
        """One row per source the registry knows or the bot has called."""
        with self._lock:
            ids = [s.id for s in self.registry if s.read_by_bot]
            ids += [k for k in self._health if k not in ids]
            ids += [k for k in self._probes if k not in ids]
            rows = []
            for sid in ids:
                h = self._health.get(sid)
                src = self.registry.get(sid)
                row = {
                    "source_id": sid,
                    "publisher": src.publisher if src else "",
                    "tier": src.tier if src else None,
                    "state": self._judge(h) if h else UNKNOWN,
                    "detail": "",
                    "calls": h.calls if h else 0,
                    "failures": h.failures if h else 0,
                    "consecutive_failures": h.consecutive_failures if h else 0,
                    "error_rate": round(h.error_rate, 3) if h else 0.0,
                    "last_success_at": _iso(h.last_success_at) if h else None,
                    "last_failure_at": _iso(h.last_failure_at) if h else None,
                    "last_status": h.last_status if h else None,
                    "last_error": h.last_error if h else "",
                    "last_latency_ms": (round(h.last_latency_ms, 1)
                                        if h and h.last_latency_ms is not None else None),
                    "last_data_time": (h.last_data_time.isoformat()
                                       if h and h.last_data_time else None),
                }
                rows.append(row)
        for row in rows:
            probe = self._probes.get(row["source_id"])
            if probe is not None:
                try:
                    row["state"], row["detail"] = probe()
                except Exception as exc:  # noqa: BLE001
                    row["state"], row["detail"] = UNKNOWN, f"probe failed: {exc}"
        return rows

    def summary(self) -> str:
        return " | ".join(f"{r['source_id']} {r['state']}" for r in self.snapshot())


#: The process-wide monitor. Modules that fetch from the outside world record
#: against this; arb.py configures it with the registry and the journal at
#: startup. Tests build their own SourceMonitor.
MONITOR = SourceMonitor()


def configure_from_settings(settings, journal=None,
                            monitor: SourceMonitor | None = None) -> SourceMonitor:
    """Load the registry from config.yaml and connect the journal as the
    provenance sink. Raises SourceConfigError on a registry that breaks 14A."""
    m = monitor or MONITOR
    section = settings.section("sources") if settings is not None else {}
    health = (section or {}).get("health") or {}
    m.configure(SourceRegistry.from_config(section),
                down_after=health.get("down_after_failures", 3),
                degraded_error_rate=health.get("degraded_error_rate", 0.25),
                window=health.get("window", 20))
    if journal is not None:
        m.set_sink(journal.log_source_observation)
    return m


def nws_user_agent(section: dict | None) -> str:
    """NWS asks API users to identify themselves with a contact address."""
    contact = str(((section or {}).get("nws_contact") or "")).strip()
    base = "kalshibot/1.0 (personal trading research"
    return f"{base}; {contact})" if contact else f"{base})"
