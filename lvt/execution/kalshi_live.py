"""LIVE execution on the Kalshi production exchange.

This module is imported ONLY after lvt.live_gate has approved LIVE mode
(every readiness item, a human authorization bound to this exact code and
config, and valid production credentials). Nothing in PAPER or SHADOW
constructs it, and the public market-data client cannot sign requests.

Kalshi is the source of truth for orders, fills, positions and balance
(section 39). Any exception while submitting leaves the order UNKNOWN; the
engine then fails closed until reconciliation resolves it by
client_order_id.

UNVERIFIED AGAINST PRODUCTION: this bot has never had a key. The V2 response
field names below follow Kalshi's published changes and are parsed
tolerantly, but `lvt live-probe` (read-only) and the EXECUTION_TESTED
readiness step must be completed by a human before LIVE is allowed.
"""
from __future__ import annotations

import base64
import logging
import time
from pathlib import Path

import requests

from lvt.data.http import USER_AGENT
from lvt.execution.base import Venue
from lvt.execution.encoding import ORDER_PATH, encode_order
from lvt.models import NO, YES, FillEvent, OrderRequest, OrderUpdate, Snapshot, dollars_to_cents, fnum

log = logging.getLogger("lvt.live")

API_PREFIX = "/trade-api/v2"


class CredentialError(Exception):
    pass


class Signer:
    """RSA-PSS SHA256 over timestamp_ms + METHOD + /trade-api/v2/path (no query)."""

    def __init__(self, key_id: str, private_key_path: str):
        if not key_id:
            raise CredentialError("KALSHI_PROD_KEY_ID is not set")
        p = Path(private_key_path or "")
        if not private_key_path or not p.is_file():
            raise CredentialError("KALSHI_PROD_PRIVATE_KEY_PATH does not point to a readable file")
        from cryptography.hazmat.primitives import serialization
        try:
            self._key = serialization.load_pem_private_key(p.read_bytes(), password=None)
        except Exception as exc:                       # noqa: BLE001
            raise CredentialError(f"private key could not be loaded: {type(exc).__name__}") from None
        self.key_id = key_id

    def headers(self, method: str, path: str) -> dict:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + API_PREFIX + path.split("?")[0]).encode()
        sig = self._key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                              salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}

    def __repr__(self) -> str:                         # never leak key material in logs
        return "Signer(<redacted>)"


class AuthedHttp:
    def __init__(self, base_url: str, signer: Signer, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.signer = signer
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json", "User-Agent": USER_AGENT})

    def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        h = self.signer.headers(method, path)
        r = self.s.request(method, self.base_url + path, params=params, json=body, headers=h, timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"Kalshi {method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}


def parse_order(o: dict, side: str) -> tuple[str, int, float | None, str]:
    """(status, filled, avg side-relative price, exchange order id)."""
    filled = int(fnum(o.get("fill_count", o.get("fill_count_fp", o.get("filled_count"))), 0))
    remaining = fnum(o.get("remaining_count", o.get("remaining_count_fp")), -1)
    px = dollars_to_cents(o.get("average_fill_price") or o.get("average_fill_price_dollars"))
    if px is None and o.get("yes_price_dollars"):
        px = dollars_to_cents(o.get("yes_price_dollars"))
    if px is not None and side == NO:
        px = 100.0 - px                                  # V2 prices are on the YES book
    raw = str(o.get("status") or "").lower()
    if raw in ("resting", "pending", "open") and remaining > 0:
        status = "ACKED"
    elif filled > 0 and remaining == 0:
        status = "FILLED"
    elif filled > 0:
        status = "PARTIAL"
    elif raw in ("canceled", "cancelled", "expired", "executed"):
        status = "NO_FILL"
    else:
        status = "ACKED"
    return status, filled, px, str(o.get("order_id") or "")


class KalshiLiveVenue(Venue):
    name = "kalshi_live"
    env = "LIVE"
    is_real = True

    def __init__(self, http: AuthedHttp, markets):
        self.http = http
        self.markets = markets
        self.working: dict[str, tuple[OrderRequest, str]] = {}    # coid -> (req, exchange id)
        self.reported: dict[str, set] = {}                         # coid -> fill ids already reported
        self.reported_count: dict[str, int] = {}                   # coid -> contracts already reported

    # ---- account (reconciliation sources) --------------------------------
    def balance_usd(self) -> float | None:
        d = self.http.request("GET", "/portfolio/balance")
        bal = d.get("balance_dollars")
        return float(bal) if bal is not None else fnum(d.get("balance")) / 100.0

    def positions(self) -> dict[str, tuple[str, int]]:
        out: dict[str, tuple[str, int]] = {}
        cursor = None
        for _ in range(20):
            params = {"limit": 200, "count_filter": "position"}
            if cursor:
                params["cursor"] = cursor
            d = self.http.request("GET", "/portfolio/positions", params=params)
            for p in d.get("market_positions") or []:
                n = fnum(p.get("position_fp", p.get("position")))
                if abs(n) >= 1:
                    out[p["ticker"]] = (YES if n > 0 else NO, int(abs(n)))
            cursor = d.get("cursor")
            if not cursor:
                break
        return out

    def resting_orders(self) -> list[dict]:
        return self.http.request("GET", "/portfolio/orders", params={"status": "resting", "limit": 200}).get("orders") or []

    def fills_for(self, order_id: str) -> list[dict]:
        return self.http.request("GET", "/portfolio/fills", params={"order_id": order_id, "limit": 200}).get("fills") or []

    # ---- orders ----------------------------------------------------------
    def submit(self, req: OrderRequest, now: float) -> OrderUpdate:
        body = encode_order(req)
        # An exception here propagates: the caller marks the order UNKNOWN.
        d = self.http.request("POST", ORDER_PATH, body=body)
        o = d.get("order", d) or {}
        status, filled, px, oid = parse_order(o, req.side)
        self.working[req.client_order_id] = (req, oid)
        up = OrderUpdate(req.client_order_id, "ACKED", now, exchange_order_id=oid)
        if status in ("FILLED", "PARTIAL", "NO_FILL"):
            up = self._final(req, oid, now)
        return up

    def _new_fills(self, req: OrderRequest, oid: str, now: float) -> list[FillEvent]:
        """Exchange fill records for this order not reported yet (resting maker
        orders fill a bit at a time)."""
        seen = self.reported.setdefault(req.client_order_id, set())
        fills = []
        for f in (self.fills_for(oid) if oid else []):
            fid = str(f.get("trade_id") or f.get("fill_id") or "")
            if fid in seen:
                continue
            seen.add(fid)
            c = int(fnum(f.get("count_fp", f.get("count"))))
            yp = dollars_to_cents(f.get("yes_price_dollars")) or fnum(f.get("yes_price"))
            px = yp if req.side == YES else 100.0 - yp
            fee = fnum(f.get("fee_cost_dollars", f.get("fee_cost")), 0.0)
            fills.append(FillEvent(req.client_order_id, now, px, c, fee, fid))
        return fills

    def _final(self, req: OrderRequest, oid: str, now: float) -> OrderUpdate:
        """Terminal update from the exchange's own fill records."""
        fills = self._new_fills(req, oid, now)
        self.reported.pop(req.client_order_id, None)
        total = sum(f.count for f in fills) + self.reported_count.pop(req.client_order_id, 0)
        status = "FILLED" if total >= req.count else ("PARTIAL" if total > 0 else "NO_FILL")
        self.working.pop(req.client_order_id, None)
        return OrderUpdate(req.client_order_id, status, now, fills=fills, exchange_order_id=oid)

    def on_market_data(self, snaps: dict[str, Snapshot], now: float) -> list[OrderUpdate]:
        out = []
        for coid, (req, oid) in list(self.working.items()):
            try:
                o = self.http.request("GET", f"/portfolio/orders/{oid}").get("order") or {}
            except Exception as exc:                   # noqa: BLE001
                out.append(OrderUpdate(coid, "UNKNOWN", now, reason=str(exc)[:200]))
                continue
            status, _, _, _ = parse_order(o, req.side)
            if status != "ACKED":
                out.append(self._final(req, oid, now))
                continue
            expired = (req.tif == "GTC" and req.expire_ts and now >= req.expire_ts) or \
                      (req.tif != "GTC" and now - req.created_ts > 15)     # an IOC should never rest
            if expired:
                out.append(self.cancel(coid, now))            # cancel reports the final fills itself
                continue
            fills = self._new_fills(req, oid, now)             # resting maker order filling in pieces
            if fills:
                self.reported_count[coid] = self.reported_count.get(coid, 0) + sum(f.count for f in fills)
                out.append(OrderUpdate(coid, "PARTIALLY_FILLED", now, fills=fills, exchange_order_id=oid))
        return out

    def cancel(self, client_order_id: str, now: float) -> OrderUpdate:
        hit = self.working.get(client_order_id)
        if not hit:
            return OrderUpdate(client_order_id, "REJECTED", now, reason="unknown order")
        req, oid = hit
        self.http.request("DELETE", f"{ORDER_PATH}/{oid}", params={"market_ticker": req.ticker})
        if client_order_id not in self.working:
            return OrderUpdate(client_order_id, "CANCELLED", now, exchange_order_id=oid)
        fin = self._final(req, oid, now)                   # fills that landed before the cancel
        return OrderUpdate(client_order_id, "CANCELLED", now, fills=fin.fills, exchange_order_id=oid)

    def lookup(self, client_order_id: str) -> OrderUpdate | None:
        d = self.http.request("GET", "/portfolio/orders", params={"client_order_id": client_order_id, "limit": 5})
        orders = [o for o in d.get("orders") or [] if o.get("client_order_id") == client_order_id]
        if not orders:
            return None
        o = orders[0]
        side = YES if o.get("side") in ("yes", "bid") else NO
        status, filled, _, oid = parse_order(o, side)
        return OrderUpdate(client_order_id, status, time.time(), exchange_order_id=oid)

    def working_orders(self) -> list[str]:
        return list(self.working)
