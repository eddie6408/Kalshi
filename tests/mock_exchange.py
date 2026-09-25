"""A mock Kalshi exchange that speaks the real HTTP API.

This exists so the complete DEMO trading path can be exercised end to end -
real requests, real signing, real JSON, real order placement and fills -
without a network connection to Kalshi.

It runs on loopback, which env_guard only accepts when
KALSHIBOT_ALLOW_LOOPBACK_FOR_TESTS=1. Production hostnames stay refused.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

API = "/trade-api/v2"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def market(ticker, event, yes_bid, yes_ask, *, hours=6, series="MOCK",
           strike_type="", floor_strike=None, cap_strike=None, subtitle=""):
    """yes_bid/yes_ask are the DISPLAYED top of book."""
    return {
        "ticker": ticker,
        "event_ticker": event,
        "series_ticker": series,
        "title": f"{ticker} title",
        "yes_sub_title": subtitle or ticker,
        "status": "active",
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": 100 - yes_ask,
        "no_ask": 100 - yes_bid,
        "last_price": (yes_bid + yes_ask) // 2,
        "volume": 500,
        "open_interest": 900,
        "liquidity": 10000,
        "close_time": _iso(datetime.now(timezone.utc) + timedelta(hours=hours)),
        "strike_type": strike_type,
        "floor_strike": floor_strike,
        "cap_strike": cap_strike,
    }


class MockKalshi:
    """Holds state. The HTTP handler just reads and writes this."""

    def __init__(self):
        self.markets: dict[str, dict] = {}
        self.books: dict[str, dict] = {}
        self.events: list[dict] = []
        self.balance_cents = 49_200
        self.orders: dict[str, dict] = {}
        self.positions: dict[str, int] = {}
        self.fills: list[dict] = []
        self.requests: list[tuple[str, str]] = []
        self._seq = 0
        #: ticker -> contracts that will fill (None = fill everything)
        self.fill_limits: dict[str, int] = {}

    # ---------------- seeding ----------------
    def add_market(self, m: dict, yes_levels, no_levels) -> None:
        self.markets[m["ticker"]] = m
        self.books[m["ticker"]] = {"yes": [list(x) for x in yes_levels],
                                   "no": [list(x) for x in no_levels]}

    def add_event(self, event_ticker: str, mutually_exclusive: bool = False,
                  title: str = "") -> None:
        self.events.append({"event_ticker": event_ticker,
                            "series_ticker": "MOCK",
                            "title": title or event_ticker,
                            "mutually_exclusive": mutually_exclusive})

    # ---------------- behaviour ----------------
    def create_order(self, body: dict) -> dict:
        """Speaks the V2 wire model, because that is what Kalshi speaks.

        V2 quotes ONE book - the YES book - with side bid/ask, fixed-point
        STRING counts, and prices in DOLLARS. The old endpoint took yes/no,
        buy/sell and integer cents. Testing against the old shape would have
        proved nothing about the endpoint the bot actually calls.
        """
        self._seq += 1
        oid = f"mock-order-{self._seq}"
        ticker = body.get("ticker", "")

        # These are required by V2; a caller that omits them is broken.
        for required in ("side", "count", "price", "time_in_force",
                         "self_trade_prevention_type"):
            if body.get(required) in (None, ""):
                raise ValueError(f"V2 create_order missing {required}")

        book_side = body["side"]
        if book_side not in ("bid", "ask"):
            raise ValueError(f"V2 side must be bid|ask, got {book_side!r}")
        want = int(float(body["count"]))
        yes_price_cents = round(float(body["price"]) * 100)
        if not 1 <= yes_price_cents <= 99:
            raise ValueError(f"price {body['price']!r} out of range")

        cap = self.fill_limits.get(ticker)
        filled = want if cap is None else min(want, cap)

        order = {
            "order_id": oid,
            "client_order_id": body.get("client_order_id", ""),
            "ticker": ticker,
            "side": book_side,
            "initial_count_fp": f"{want}.00",
            "fill_count": f"{filled}.00",
            "remaining_count": f"{want - filled}.00",
            "average_fill_price": f"{yes_price_cents / 100.0:.4f}" if filled else "",
            "status": "executed" if filled >= want else "resting",
            "ts_ms": int(datetime.now(timezone.utc).timestamp() * 1000),
        }
        self.orders[oid] = order

        if filled:
            # A bid buys YES; an ask sells YES. Positions are signed in YES.
            signed = filled if book_side == "bid" else -filled
            self.positions[ticker] = self.positions.get(ticker, 0) + signed
            cost = filled * (yes_price_cents if book_side == "bid"
                             else 100 - yes_price_cents)
            self.balance_cents -= cost
            self.fills.append({"order_id": oid, "ticker": ticker,
                               "side": book_side, "count": filled,
                               "price": yes_price_cents})
        return order

    def cancel(self, order_id: str) -> dict:
        o = self.orders.get(order_id)
        if o:
            o["status"] = "canceled"
        return {"order_id": order_id, "reduced_by": "0.00",
                "ts_ms": int(datetime.now(timezone.utc).timestamp() * 1000)}

    def positions_payload(self) -> dict:
        return {"market_positions": [
            {"ticker": t, "position": n, "market_exposure": abs(n) * 50,
             "realized_pnl": 0, "resting_orders_count": 0}
            for t, n in self.positions.items() if n]}


class _Handler(BaseHTTPRequestHandler):
    state: MockKalshi = None  # type: ignore[assignment]

    def log_message(self, *a):  # silence
        pass

    # ----------------------------------------------------------
    def _send(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _path(self):
        u = urlparse(self.path)
        return u.path, parse_qs(u.query)

    # ----------------------------------------------------------
    def do_GET(self):
        path, query = self._path()
        st = self.state
        st.requests.append(("GET", path))

        if path == f"{API}/exchange/status":
            return self._send({"exchange_active": True, "trading_active": True})

        if path == f"{API}/markets":
            wanted = query.get("series_ticker", [None])[0]
            ms = [m for m in st.markets.values()
                  if wanted is None or m["series_ticker"] == wanted
                  or m["ticker"].startswith(wanted)]
            return self._send({"markets": ms, "cursor": ""})

        if path == f"{API}/events":
            return self._send({"events": st.events, "cursor": ""})

        if path.startswith(f"{API}/markets/") and path.endswith("/orderbook"):
            ticker = path[len(f"{API}/markets/"):-len("/orderbook")]
            return self._send({"orderbook": st.books.get(ticker, {"yes": [], "no": []})})

        if path.startswith(f"{API}/markets/"):
            ticker = path[len(f"{API}/markets/"):]
            m = st.markets.get(ticker)
            return self._send({"market": m} if m else {}, 200 if m else 404)

        if path == f"{API}/portfolio/balance":
            return self._send({"balance": st.balance_cents})

        if path == f"{API}/portfolio/positions":
            return self._send(st.positions_payload())

        if path == f"{API}/portfolio/fills":
            return self._send({"fills": st.fills})

        if path.startswith(f"{API}/portfolio/orders/"):
            oid = path.rsplit("/", 1)[-1]
            o = st.orders.get(oid)
            return self._send({"order": o} if o else {}, 200 if o else 404)

        if path == f"{API}/portfolio/orders":
            return self._send({"orders": list(st.orders.values())})

        return self._send({"error": "not found"}, 404)

    def do_POST(self):
        path, _ = self._path()
        st = self.state
        st.requests.append(("POST", path))
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")

        # The retired path must behave exactly as the live exchange now does,
        # so a regression back to it fails loudly in tests instead of silently
        # in production.
        if path == f"{API}/portfolio/orders":
            return self._send({"error": {
                "code": "deprecated_v1_order_endpoint",
                "message": "Please switch to the V2 endpoints"}}, 410)

        if path == f"{API}/portfolio/events/orders":
            for header in ("KALSHI-ACCESS-KEY", "KALSHI-ACCESS-TIMESTAMP",
                           "KALSHI-ACCESS-SIGNATURE"):
                if not self.headers.get(header):
                    return self._send({"error": f"missing {header}"}, 401)
            try:
                return self._send(st.create_order(body), 201)
            except ValueError as exc:
                return self._send({"error": str(exc)}, 400)

        return self._send({"error": "not found"}, 404)

    def do_DELETE(self):
        path, _ = self._path()
        self.state.requests.append(("DELETE", path))
        if path.startswith(f"{API}/portfolio/events/orders/"):
            return self._send(self.state.cancel(path.rsplit("/", 1)[-1]))
        if path.startswith(f"{API}/portfolio/orders/"):
            return self._send({"error": {
                "code": "deprecated_v1_order_endpoint"}}, 410)
        return self._send({"error": "not found"}, 404)


class MockServer:
    """Context manager. `with MockServer(state) as url:`"""

    def __init__(self, state: MockKalshi):
        self.state = state
        handler = type("Bound", (_Handler,), {"state": state})
        self.httpd = HTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}{API}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self._thread.start()
        return self.url

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


# ==================================================================
def seeded_exchange(with_arbitrage: bool = True) -> MockKalshi:
    """A board with a handful of efficiently priced markets and, optionally,
    one genuine same-market arbitrage."""
    st = MockKalshi()
    st.add_event("EFFICIENT", mutually_exclusive=False)

    # Efficiently priced: YES ask 45 + NO ask 56 = 101c. No edge.
    for i in range(4):
        t = f"EFF-{i}"
        st.add_market(market(t, "EFFICIENT", yes_bid=44, yes_ask=45),
                      yes_levels=[[44, 400]], no_levels=[[55, 400]])

    if with_arbitrage:
        st.add_event("ARBEVENT", mutually_exclusive=False)
        # YES offered at 35 (NO bid 65), NO offered at 45 (YES bid 55).
        # 80c for a pair that must pay exactly $1.00.
        st.add_market(market("ARB-A", "ARBEVENT", yes_bid=55, yes_ask=35),
                      yes_levels=[[55, 300]], no_levels=[[65, 300]])
    return st
