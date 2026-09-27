"""The Kalshi market-data feed.

Pushes order book updates into `MarketState` so the workers stop asking REST
for prices that have not moved.

    wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2

confirmed against Kalshi's own documentation rather than assumed - the same
caution the v1 order endpoint earned when it started returning 410.

What the exchange sends (channel `orderbook_delta`):

    {"type":"orderbook_snapshot","sid":2,"seq":2,
     "msg":{"market_ticker":"...","yes_dollars_fp":[["0.0800","300.00"]],
                                   "no_dollars_fp":[["0.5400","20.00"]]}}

    {"type":"orderbook_delta","sid":2,"seq":3,
     "msg":{"market_ticker":"...","price_dollars":"0.9600",
            "delta_fp":"-54.00","side":"yes"}}

Two things to notice. Prices are DOLLAR STRINGS and counts are fixed-point
strings - the same migration that moved orders to `"0.4300"` and positions to
`position_fp`. And unlike the order endpoint, the book here really does have
separate yes and no sides, so no YES-book translation is needed.

THE FEED IS ALWAYS OPTIONAL. Every failure - no credentials, a refused
handshake, a dropped socket, a message that will not parse - ends the same
way: the affected books are marked untrusted and the bot reads REST exactly as
it did before. Nothing here can make the bot trade on worse information than
it had without it, and nothing here can stop it trading.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

from .env_guard import ProductionBlocked, assert_demo_url
from .wsproto import WebSocket, WebSocketClosed, WebSocketError

log = logging.getLogger(__name__)

DEFAULT_DEMO_WS = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"


@dataclass
class FeedConfig:
    enabled: bool = False
    url: str = DEFAULT_DEMO_WS
    #: how many markets to follow. The exchange has 80,000; we care about the
    #: ones a strategy is actually looking at.
    max_subscriptions: int = 500
    #: seconds between our own pings, so a quiet connection is not dropped
    ping_seconds: float = 20.0
    #: no message at all for this long means the connection is dead even if
    #: the socket has not said so
    idle_timeout: float = 60.0
    reconnect_min: float = 2.0
    reconnect_max: float = 60.0
    recv_timeout: float = 5.0
    #: When every subscription slot is taken and the markets being looked at
    #: have moved on (tomorrow's weather tickers, a new ladder), reset to the
    #: current working set - at most this often, since it means a reconnect.
    rollover_seconds: float = 3600.0

    @classmethod
    def from_dict(cls, d: dict | None) -> "FeedConfig":
        d = d or {}
        return cls(
            enabled=bool(d.get("enabled", False)),
            url=str(d.get("url") or DEFAULT_DEMO_WS),
            max_subscriptions=int(d.get("max_subscriptions", 500)),
            ping_seconds=float(d.get("ping_seconds", 20.0)),
            idle_timeout=float(d.get("idle_timeout", 60.0)),
            reconnect_min=float(d.get("reconnect_min", 2.0)),
            reconnect_max=float(d.get("reconnect_max", 60.0)),
            recv_timeout=float(d.get("recv_timeout", 5.0)),
            rollover_seconds=float(d.get("rollover_seconds", 3600.0)),
        )


@dataclass
class FeedStats:
    connects: int = 0
    disconnects: int = 0
    messages: int = 0
    snapshots: int = 0
    deltas: int = 0
    resnapshots: int = 0
    #: real gaps in a subscription's message sequence (each costs a reconnect)
    gaps: int = 0
    errors: int = 0
    unparsed: int = 0
    connected_since: datetime | None = None
    last_message_at: datetime | None = None
    last_error: str = ""

    def summary(self) -> str:
        state = "connected" if self.connected_since else "disconnected"
        return (f"{state}, {self.connects} connects, {self.messages} messages "
                f"({self.snapshots} snapshots, {self.deltas} deltas), "
                f"{self.resnapshots} resnapshots, {self.gaps} gaps, {self.errors} errors")


def cents_from_dollars(value) -> int | None:
    """'0.9600' -> 96. Returns None rather than guessing."""
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return None


def count_from_fp(value) -> int | None:
    """'-54.00' -> -54. Fixed point, two decimals, sometimes negative."""
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


class KalshiFeed:
    """Keeps a WebSocket open and pours order book updates into MarketState."""

    def __init__(self, state, signer=None, cfg: FeedConfig | None = None,
                 env: str = "demo"):
        self.state = state
        self.signer = signer
        self.cfg = cfg or FeedConfig()
        self.env = env
        self.stats = FeedStats()
        self.ws: WebSocket | None = None
        self._lock = threading.Lock()
        self._next_id = 1
        #: tickers we have asked for, and the subscription id they came back on
        self._wanted: set[str] = set()
        self._sid_seq: dict[int, int] = {}
        self._pending_resnapshot: set[str] = set()
        #: when each ticker last had a snapshot requested, so a market the
        #: exchange will not re-snapshot is not asked for on every message
        self._resnap_at: dict[str, float] = {}
        self._last_rollover = time.monotonic()
        # The store serves a quiet book this feed maintains for as long as
        # the connection is up (MarketState._fresh).
        if hasattr(state, "feed_live"):
            state.feed_live = lambda: self.ws is not None

    # ------------------------------------------------------------------
    def follow(self, tickers) -> None:
        """Ask for these markets. Safe to call repeatedly and before connecting.

        `tickers` is the current working set, most recent first. Subscriptions
        used to only ever grow: once the slots were full (one arbitrage sweep
        could do it) nothing new was followed again - including the next
        day's weather markets. Now, when full and the working set has moved
        on, the feed resets to the working set and reconnects, at most once
        per rollover_seconds.
        """
        tickers = [t for t in tickers if t]
        rolled = False
        with self._lock:
            new = [t for t in tickers if t not in self._wanted]
            if not new:
                return
            room = self.cfg.max_subscriptions - len(self._wanted)
            if room < len(new) and (time.monotonic() - self._last_rollover
                                    >= self.cfg.rollover_seconds):
                keep = tickers[:self.cfg.max_subscriptions]
                self._wanted = set(keep)
                self.state.subscribed.clear()
                self.state.subscribed.update(keep)
                self._last_rollover = time.monotonic()
                rolled = True
            else:
                new = new[:max(0, room)]
                if not new:
                    return
                self._wanted.update(new)
                self.state.subscribed.update(new)
            ws = self.ws
        if rolled:
            # Reconnecting resubscribes to exactly the new set.
            log.info("FEED  working set moved on; following %d current markets",
                     len(self._wanted))
            self._drop("resubscribing to the current working set")
        elif ws is not None:
            self._send_subscribe(new)

    # ------------------------------------------------------------------
    def run_forever(self, stop: threading.Event) -> None:
        """Connect, pump, and reconnect until told to stop. Never raises."""
        if not self.cfg.enabled:
            log.info("FEED  disabled; every price will be read over REST")
            return
        delay = self.cfg.reconnect_min
        while not stop.is_set():
            try:
                self._connect()
                delay = self.cfg.reconnect_min      # a good connection resets it
                self._pump(stop)
            except ProductionBlocked:
                # Never retried. This is the lockout doing its job and a retry
                # loop against it would be a retry loop against production.
                raise
            except Exception as exc:  # noqa: BLE001
                self.stats.errors += 1
                self.stats.last_error = str(exc)
                log.warning("FEED  %s; falling back to REST and retrying in "
                            "%.0fs", exc, delay)
            finally:
                self._drop("connection ended")
            if stop.wait(delay):
                break
            delay = min(delay * 2, self.cfg.reconnect_max)
        log.info("FEED  stopped")

    # ------------------------------------------------------------------
    def _connect(self) -> None:
        # The lockout applies to the socket exactly as it applies to REST. A
        # market-data feed pointed at production is still production.
        #
        # Note the argument order: assert_demo_url(URL, description). Getting
        # it backwards checks the string "demo" as if it were a hostname,
        # which refuses everything - including the real demo URL - while
        # looking like the guard is working. A loopback test caught exactly
        # that here.
        assert_demo_url(self.cfg.url, "market data feed")
        headers = self._auth_headers()
        log.info("FEED  connecting to %s", self.cfg.url)
        ws = WebSocket.connect(self.cfg.url, headers=headers,
                               timeout=self.cfg.recv_timeout * 3)
        with self._lock:
            self.ws = ws
            self._sid_seq.clear()
            wanted = sorted(self._wanted)
        self.stats.connects += 1
        self.stats.connected_since = datetime.now(timezone.utc)
        log.info("FEED  connected; following %d markets", len(wanted))
        if wanted:
            self._send_subscribe(wanted)

    def _auth_headers(self) -> dict[str, str]:
        if self.signer is None:
            return {}
        # The handshake is signed the same way a GET is, over the socket path.
        path = urlparse(self.cfg.url).path or "/trade-api/ws/v2"
        headers = dict(self.signer.sign("GET", path))
        # Those two belong on a JSON request, not on an Upgrade. Some servers
        # are fussy about a Content-Type on a GET that carries no body.
        headers.pop("Content-Type", None)
        headers.pop("Accept", None)
        return headers

    def _send_subscribe(self, tickers) -> None:
        self._send({"id": self._take_id(), "cmd": "subscribe",
                    "params": {"channels": ["orderbook_delta"],
                               "market_tickers": list(tickers)}})

    def _send(self, payload: dict) -> None:
        with self._lock:
            ws = self.ws
        if ws is None:
            return
        try:
            ws.send_text(json.dumps(payload))
        except Exception as exc:  # noqa: BLE001
            log.debug("FEED  could not send %s: %s", payload.get("cmd"), exc)

    def _take_id(self) -> int:
        with self._lock:
            self._next_id += 1
            return self._next_id

    # ------------------------------------------------------------------
    def _pump(self, stop: threading.Event) -> None:
        last_ping = last_message = time.time()
        while not stop.is_set():
            ws = self.ws
            if ws is None:
                return
            try:
                raw = ws.recv()
            except WebSocketClosed:
                raise
            except WebSocketError:
                raise

            now = time.time()
            if raw is None:
                # A quiet socket is normal. A socket quiet for longer than the
                # exchange's own heartbeat is not - treat it as dead rather
                # than serving increasingly old books from it.
                if now - last_message > self.cfg.idle_timeout:
                    raise WebSocketError(
                        f"no message for {now - last_message:.0f}s")
                if now - last_ping > self.cfg.ping_seconds:
                    ws.ping()
                    last_ping = now
                continue

            last_message = now
            self.stats.messages += 1
            self.stats.last_message_at = datetime.now(timezone.utc)
            self._handle(raw)
            if self._pending_resnapshot:
                self._request_resnapshots()

    # ------------------------------------------------------------------
    def _handle(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except ValueError:
            self.stats.unparsed += 1
            log.debug("FEED  unparsable message: %.120s", raw)
            return
        if not isinstance(payload, dict):
            self.stats.unparsed += 1
            return

        # EVERY message that carries a subscription sequence is checked, not
        # only book messages: the "ok" that acknowledges markets added to a
        # live subscription takes the next number on it ({"type": "ok",
        # "sid": 1, "seq": 51}). Ignoring it read as a gap and cost a
        # reconnect every time the feed followed something new.
        if not self._in_order(payload):
            return

        kind = payload.get("type")
        if kind == "orderbook_snapshot":
            self._on_snapshot(payload)
        elif kind == "orderbook_delta":
            self._on_delta(payload)
        elif kind == "error":
            msg = payload.get("msg") or {}
            self.stats.errors += 1
            self.stats.last_error = str(msg)
            log.warning("FEED  exchange refused something: %s", msg)
        elif kind in ("subscribed", "ok", "unsubscribed", "pong"):
            log.debug("FEED  %s", kind)
        else:
            log.debug("FEED  ignoring %r", kind)

    def _on_snapshot(self, payload: dict) -> None:
        msg = payload.get("msg") or {}
        ticker = msg.get("market_ticker")
        if not ticker:
            self.stats.unparsed += 1
            return
        yes = self._levels(msg, "yes")
        no = self._levels(msg, "no")
        # seq=None: the store must not check it per market - see _in_order.
        self.state.apply_snapshot(ticker, yes, no, seq=None)
        self._pending_resnapshot.discard(ticker)
        self.stats.snapshots += 1

    @staticmethod
    def _levels(msg: dict, side: str) -> list[tuple[int, int]]:
        """Read one side of a snapshot, in whichever spelling arrived.

        `yes_dollars_fp` is current. The older `yes` (integer cents) is still
        handled because reading only the new shape against an older deployment
        gives an empty book for every market, which looks exactly like a dead
        exchange and is not.
        """
        for key, scale in ((f"{side}_dollars_fp", "dollars"),
                           (f"{side}_dollars", "dollars"),
                           (side, "cents")):
            rows = msg.get(key)
            if rows is None:
                continue
            out = []
            for row in rows:
                if not isinstance(row, (list, tuple)) or len(row) < 2:
                    continue
                price = (cents_from_dollars(row[0]) if scale == "dollars"
                         else count_from_fp(row[0]))
                qty = count_from_fp(row[1])
                if price is None or qty is None:
                    continue
                if 0 < price < 100 and qty > 0:
                    out.append((price, qty))
            return out
        return []

    def _on_delta(self, payload: dict) -> None:
        msg = payload.get("msg") or {}
        ticker = msg.get("market_ticker")
        side = str(msg.get("side") or "").lower()
        price = (cents_from_dollars(msg.get("price_dollars"))
                 if "price_dollars" in msg else count_from_fp(msg.get("price")))
        delta = (count_from_fp(msg.get("delta_fp"))
                 if "delta_fp" in msg else count_from_fp(msg.get("delta")))
        if not ticker or price is None or delta is None:
            self.stats.unparsed += 1
            log.debug("FEED  unusable delta: %.200s", msg)
            return

        ok = self.state.apply_delta(ticker, side, price, delta, seq=None)
        if ok:
            self.stats.deltas += 1
        else:
            # The store refused it. Whatever the reason - a gap, a level that
            # went negative, no snapshot behind it - the cure is the same and
            # it is the only cure: get a fresh snapshot.
            self._pending_resnapshot.add(ticker)

    def _in_order(self, payload: dict) -> bool:
        """Is this the next message on its subscription?

        Kalshi numbers messages per SUBSCRIPTION (sid), one counter shared by
        every market on it - not per market. Checking per market read every
        update to a neighbouring market as a gap: on 27 September, 920 false
        gaps and 1,085 resnapshot requests in ten minutes, 0 deltas applied.

        A repeat (seq already seen) is dropped. A real gap may have hidden an
        update to ANY market on the subscription, so every book is distrusted
        and the feed reconnects, which re-snapshots everything it follows.
        """
        sid = _int(payload.get("sid"))
        seq = _int(payload.get("seq"))
        if sid is None or seq is None:
            return True
        with self._lock:
            last = self._sid_seq.get(sid)
            if last is not None and seq <= last:
                return False                       # a repeat; already applied
            gap = last is not None and seq != last + 1
            self._sid_seq[sid] = seq
        if gap:
            self.stats.gaps += 1
            why = f"subscription {sid} jumped from {last} to {seq}"
            log.warning("FEED  %s - a message was missed; reconnecting for "
                        "fresh snapshots", why)
            self.state.invalidate_all(f"sequence gap ({why})")
            self._drop(f"sequence gap ({why})")
            return False
        return True

    def _request_resnapshots(self) -> None:
        now = time.monotonic()
        with self._lock:
            tickers = sorted(t for t in self._pending_resnapshot
                             if now - self._resnap_at.get(t, -1e9) >= 300)
            self._pending_resnapshot.clear()
            for t in tickers:
                self._resnap_at[t] = now
        if not tickers:
            return
        self.stats.resnapshots += len(tickers)
        log.info("FEED  asking for fresh snapshots of %d market(s): %s",
                 len(tickers), ", ".join(tickers[:5]))
        self._send({"id": self._take_id(), "cmd": "subscribe",
                    "params": {"channels": ["orderbook_delta"],
                               "market_tickers": tickers}})

    # ------------------------------------------------------------------
    def _drop(self, why: str) -> None:
        with self._lock:
            ws, self.ws = self.ws, None
        if ws is None:
            return
        self.stats.disconnects += 1
        self.stats.connected_since = None
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
        # Everything cached came from a feed that is no longer running. It is
        # not wrong yet, but nothing will tell us when it becomes wrong.
        self.state.invalidate_all(why)
        log.info("FEED  disconnected (%s); every book is REST-backed again", why)

    def health(self) -> dict:
        return {
            "enabled": self.cfg.enabled,
            "connected": self.ws is not None,
            "connects": self.stats.connects,
            "messages": self.stats.messages,
            "snapshots": self.stats.snapshots,
            "deltas": self.stats.deltas,
            "resnapshots": self.stats.resnapshots,
            "errors": self.stats.errors,
            "following": len(self._wanted),
            "last_error": self.stats.last_error,
        }


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
