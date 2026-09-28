"""Maker (post-only) orders: queue model, venue lifecycle, engine flow
(resting entry -> take-profit / stop), encoding, replay and live venue."""
import tempfile
import unittest
from pathlib import Path

from lvt.execution import maker_model
from lvt.execution.encoding import encode_order
from lvt.execution.paper import PaperVenue
from lvt.execution.reconcile import reconcile
from lvt.models import OrderRequest, Trade
from tests.helpers import Harness, T0, cfg, market, mid_to_book, path, snap
from tests.test_strategies import DIP, only

MAKER = {"execution": {"entry_style": "maker", "exit_style": "maker"},
         "maker": {"take_profit": True, "entry_ttl_s": 30, "exit_ttl_s": 10}}


def req(action="buy", side="yes", price=49.0, count=40, coid="m1", expire=0.0, purpose=None):
    return OrderRequest(coid, "X", "E", action, side, count, price,
                        purpose or ("ENTRY" if action == "buy" else "TAKE_PROFIT"), price, T0,
                        post_only=True, tif="GTC", expire_ts=expire)


def book(ts, bid=49.0, ask=51.0, bid_size=100, ask_size=100, volume=0.0, last=None):
    s = snap("X", ts, bid, ask, bid_size=bid_size, ask_size=ask_size, volume=volume)
    s.quote.last = last
    return s


def tr(ts, yes_price, count, taker_side, tid):
    return Trade("X", ts, yes_price, count, taker_side, tid)


class TestQueueModel(unittest.TestCase):
    def setUp(self):
        self.m = market("X")

    def resting(self, r, s):
        ro = maker_model.RestingOrder(r, T0)
        self.assertIsNone(maker_model.activate(ro, s, 6))
        return ro

    def test_post_only_rejects_crossing(self):
        ro = maker_model.RestingOrder(req(price=51), T0)
        up = maker_model.activate(ro, book(T0), 6)
        self.assertEqual(up.status, "REJECTED")
        self.assertIn("post-only", up.reason)
        ro = maker_model.RestingOrder(req(action="sell", price=49), T0)
        self.assertEqual(maker_model.activate(ro, book(T0), 6).status, "REJECTED")

    def test_join_bid_waits_behind_queue_then_fills_from_trades(self):
        ro = self.resting(req(price=49), book(T0))
        self.assertEqual(ro.queue_ahead, 100)
        f = maker_model.advance(ro, book(T0 + 2), [tr(T0 + 1, 49, 60, "no", "a")], self.m, True, 0.5)
        self.assertEqual(f, [])
        self.assertEqual(ro.queue_ahead, 40)
        f = maker_model.advance(ro, book(T0 + 4), [tr(T0 + 3, 49, 70, "no", "b")], self.m, True, 0.5)
        self.assertEqual(sum(x.count for x in f), 30)
        self.assertTrue(all(x.price == 49 and x.fee_usd == 0.0 for x in f))    # maker fee 0 on quadratic
        f = maker_model.advance(ro, book(T0 + 6), [tr(T0 + 5, 48, 1, "no", "c")], self.m, True, 0.5)
        self.assertEqual(sum(x.count for x in f), 10)                            # traded through us
        self.assertEqual(ro.remaining, 0)

    def test_buyers_lifting_the_ask_do_not_fill_a_bid(self):
        ro = self.resting(req(price=50), book(T0))                               # improved: queue 0
        self.assertEqual(ro.queue_ahead, 0)
        f = maker_model.advance(ro, book(T0 + 2), [tr(T0 + 1, 51, 500, "yes", "a")], self.m, True, 0.5)
        self.assertEqual(f, [])

    def test_touch_through_on_snapshot(self):
        ro = self.resting(req(price=49), book(T0))
        f = maker_model.advance(ro, book(T0 + 2, bid=48, ask=49), [], self.m, True, 0.5)
        self.assertEqual(sum(x.count for x in f), 40)

    def test_price_running_away_never_fills(self):
        ro = self.resting(req(price=49), book(T0))
        for i in range(1, 10):
            f = maker_model.advance(ro, book(T0 + 2 * i, bid=49 + i, ask=51 + i), [], self.m, True, 0.5)
            self.assertEqual(f, [])

    def test_no_side_mapping(self):
        # buy NO at 40 == YES ask at 60; consumed by takers buying YES at >= 60
        ro = self.resting(req(side="no", price=40), book(T0, bid=58, ask=62))
        f = maker_model.advance(ro, book(T0 + 2, bid=58, ask=62), [tr(T0 + 1, 61, 5, "yes", "a")], self.m, True, 0.5)
        self.assertEqual(sum(x.count for x in f), 40)                            # YES traded 61 = NO 39 < 40: through
        self.assertEqual(f[0].price, 40)

    def test_take_profit_sell_side(self):
        ro = self.resting(req(action="sell", price=55, count=20), book(T0, bid=50, ask=55, ask_size=30))
        self.assertEqual(ro.queue_ahead, 30)
        f = maker_model.advance(ro, book(T0 + 2, bid=50, ask=55), [tr(T0 + 1, 55, 40, "yes", "a")], self.m, True, 0.5)
        self.assertEqual(sum(x.count for x in f), 10)
        f = maker_model.advance(ro, book(T0 + 4, bid=55, ask=56), [], self.m, True, 0.5)   # bid reached us
        self.assertEqual(ro.remaining, 0)

    def test_maker_fee_series(self):
        m = market("X")
        m.fee_type = "quadratic_with_maker_fees"
        ro = self.resting(req(price=49), book(T0))
        f = maker_model.advance(ro, book(T0 + 2, bid=48, ask=49), [], m, True, 0.5)
        self.assertGreater(f[0].fee_usd, 0)

    def test_old_and_duplicate_trades_ignored(self):
        ro = self.resting(req(price=49), book(T0))
        t = tr(T0 - 5, 48, 99, "no", "old")
        self.assertEqual(maker_model.advance(ro, book(T0 + 2), [t], self.m, True, 0.5), [])
        t = tr(T0 + 1, 49, 60, "no", "dup")
        maker_model.advance(ro, book(T0 + 2), [t], self.m, True, 0.5)
        maker_model.advance(ro, book(T0 + 4), [t], self.m, True, 0.5)
        self.assertEqual(ro.queue_ahead, 40)

    def test_volume_fallback_without_trade_feed(self):
        ro = self.resting(req(price=49), book(T0, volume=1000))
        f = maker_model.advance(ro, book(T0 + 2, volume=1100, last=49), [], self.m, False, 0.5)
        self.assertEqual(f, [])                                                  # 50 attributed, queue 100
        f = maker_model.advance(ro, book(T0 + 4, volume=1300, last=49), [], self.m, False, 0.5)
        self.assertEqual(sum(x.count for x in f), 40)                            # 100 more: 50 left of queue, 50 to us
        ro = self.resting(req(price=49, coid="m2"), book(T0, volume=1000))
        f = maker_model.advance(ro, book(T0 + 2, volume=5000, last=51), [], self.m, False, 0.5)
        self.assertEqual(f, [])                                                  # trades at the ask are not ours


class TestPaperVenueMaker(unittest.TestCase):
    def venue(self):
        return PaperVenue(cfg({"execution": {"latency_ms": 100}}), "PAPER", {"X": market("X")})

    def test_partial_then_expiry(self):
        v = self.venue()
        v.submit(req(price=50, expire=T0 + 20), T0)
        self.assertEqual(v.on_market_data({"X": book(T0 + 2)}, T0 + 2), [])      # activates, queue 0
        v.on_trades([tr(T0 + 3, 50, 15, "no", "a")], {"X"})
        ups = v.on_market_data({"X": book(T0 + 4)}, T0 + 4)
        self.assertEqual((ups[0].status, sum(f.count for f in ups[0].fills)), ("PARTIALLY_FILLED", 15))
        self.assertEqual(v.positions(), {"X": ("yes", 15)})
        ups = v.on_market_data({"X": book(T0 + 22)}, T0 + 22)
        self.assertEqual(ups[0].status, "PARTIAL")

    def test_unfilled_expiry_and_cancel(self):
        v = self.venue()
        v.submit(req(price=50, expire=T0 + 10), T0)
        v.on_market_data({"X": book(T0 + 2)}, T0 + 2)
        self.assertEqual(v.on_market_data({"X": book(T0 + 12)}, T0 + 12)[0].status, "NO_FILL")
        v.submit(req(price=50, coid="m2"), T0 + 20)
        v.on_market_data({"X": book(T0 + 22)}, T0 + 22)
        self.assertEqual(v.cancel("m2", T0 + 23).status, "CANCELLED")
        self.assertEqual(v.working_orders(), [])

    def test_post_only_reject_at_arrival_not_submit(self):
        v = self.venue()
        self.assertEqual(v.submit(req(price=50), T0).status, "ACKED")
        ups = v.on_market_data({"X": book(T0 + 2, bid=49, ask=50)}, T0 + 2)     # ask moved onto our price
        self.assertEqual(ups[0].status, "REJECTED")


def run_until(h, tk, cond, pts):
    for t, mid in pts:
        h.step(t, {tk: mid_to_book(mid)})
        if cond(h):
            return t
    raise AssertionError("condition never met")


class TestEngineMaker(unittest.TestCase):
    def working(self, h):
        return list(h.engine.om.working.values())

    def test_entry_rests_at_bid_then_take_profit_closes_at_target(self):
        h = Harness({**only("DIP"), **MAKER})
        tk = list(h.markets)[0]
        t = run_until(h, tk, lambda h: bool(h.engine.om.working), path(DIP))
        entry = self.working(h)[0]
        self.assertTrue(entry.post_only)
        self.assertEqual(entry.purpose, "ENTRY")
        s = h.last[tk]
        self.assertLess(entry.limit_price, s.ask("yes"))                        # never crosses
        P = entry.limit_price
        same = (s.bid("yes"), s.ask("yes"))
        h.step(t + 2, {tk: same})                                                # order arrives and rests
        h.step(t + 4, {tk: (P - 1, P)})                                          # sellers reach our bid
        h.step(t + 5, {tk: (P - 1, P)})
        pos = h.engine.book.open_positions()[0]
        self.assertEqual(pos.status, "OPEN")
        self.assertEqual(pos.avg_entry, P)
        self.assertEqual(pos.entry_fees_usd, 0.0)
        tp = self.working(h)[0]
        self.assertEqual(tp.purpose, "TAKE_PROFIT")
        self.assertEqual(len(h.engine.om.working), 1)                           # one working order per market
        target = tp.limit_price
        h.step(t + 6, {tk: (P, P + 1)})                                          # take-profit rests
        h.step(t + 8, {tk: (target, target + 1)})                                # bid reaches our resting ask
        h.step(t + 10, {tk: (target, target + 1)})
        p = h.db.one("SELECT * FROM positions")
        self.assertEqual(p["status"], "CLOSED")
        self.assertEqual(p["exit_reason"], "FIXED_PROFIT_TARGET")
        self.assertEqual(p["avg_exit"], target)
        self.assertEqual(p["exit_fees_usd"], 0.0)
        self.assertGreater(p["realized_pnl_usd"], 0)
        self.assertTrue(reconcile(h.engine.book, h.venue, []).ok)

    def test_stop_cancels_take_profit_and_exits_as_taker(self):
        h = Harness({**only("DIP"), **MAKER})
        tk = list(h.markets)[0]
        t = run_until(h, tk, lambda h: bool(h.engine.om.working), path(DIP))
        P = self.working(h)[0].limit_price
        s = h.last[tk]
        h.step(t + 2, {tk: (s.bid("yes"), s.ask("yes"))})
        h.step(t + 4, {tk: (P - 1, P)})
        h.step(t + 5, {tk: (P, P + 1)})
        stop = h.engine.book.open_positions()[0].stop
        h.step(t + 6, {tk: (stop - 2, stop - 1)})
        h.step(t + 8, {tk: (stop - 2, stop - 1)})
        p = h.db.one("SELECT * FROM positions")
        self.assertEqual(p["status"], "CLOSED")
        self.assertEqual(p["exit_reason"], "MAX_LOSS_EXIT")
        orders = h.db.query("SELECT purpose, status, limit_price FROM orders ORDER BY created_ts")
        self.assertEqual([o["purpose"] for o in orders], ["ENTRY", "TAKE_PROFIT", "EXIT"])
        self.assertEqual(orders[1]["status"], "CANCELLED")
        exit_order = h.db.one("SELECT payload FROM orders WHERE purpose='EXIT'")
        self.assertIsNotNone(exit_order)
        self.assertGreater(p["exit_fees_usd"], 0)                                # taker fee on the stop

    def test_entry_expires_when_price_runs_away(self):
        h = Harness({**only("DIP"), **MAKER})
        tk = list(h.markets)[0]
        h.run_path(tk, path(DIP))                                                # price rises after the signal
        p = h.db.one("SELECT * FROM positions")
        self.assertEqual(p["status"], "VOID")
        o = h.db.one("SELECT status, reject_reason FROM orders")
        self.assertEqual(o["status"], "NO_FILL")
        self.assertIn("expired", o["reject_reason"])

    def test_entry_cancelled_if_stop_hit_before_fill(self):
        h = Harness({**only("DIP"), **MAKER, "maker": {"take_profit": True, "entry_ttl_s": 300}})
        tk = list(h.markets)[0]
        t = run_until(h, tk, lambda h: bool(h.engine.om.working), path(DIP))
        stop = h.engine.book.positions[list(h.engine.book.positions)[0]].stop
        # bid collapses below the stop while the ask stays above our price
        h.step(t + 2, {tk: (stop - 1, stop + 30)})
        h.step(t + 4, {tk: (stop - 1, stop + 30)})
        self.assertEqual(h.db.one("SELECT status FROM orders")["status"], "CANCELLED")
        self.assertEqual(h.db.one("SELECT status FROM positions")["status"], "VOID")

    def test_signal_edge_uses_maker_costs(self):
        taker = Harness(only("DIP"))
        maker = Harness({**only("DIP"), **MAKER})
        for h in (taker, maker):
            h.run_path(list(h.markets)[0], path(DIP))
        a = taker.db.one("SELECT expected_net_cents FROM signals WHERE action='BUY'")["expected_net_cents"]
        b = maker.db.one("SELECT expected_net_cents FROM signals WHERE action='BUY'")["expected_net_cents"]
        self.assertGreater(b, a)                                                 # no spread, no maker fee
        self.assertNotEqual(taker.cfg.strategy_version("DIP"), maker.cfg.strategy_version("DIP"))


class TestMakerEncodingAndReplay(unittest.TestCase):
    def test_payload(self):
        body = encode_order(req(side="no", price=40))
        self.assertEqual((body["side"], body["price"], body["time_in_force"], body["post_only"]),
                         ("ask", "0.6000", "good_till_canceled", True))
        body = encode_order(req(action="sell", price=55))
        self.assertTrue(body["reduce_only"])

    def test_replay_uses_recorded_trades(self):
        from lvt.db import open_state_db, open_tape_db
        from lvt.replay import run_replay
        from lvt.tape import TapeRecorder
        from tests.helpers import quote
        tmp = Path(tempfile.mkdtemp())
        tape = open_tape_db(tmp / "tape.db")
        rec = TapeRecorder(tape)
        m = market()
        rec.record_markets([m], T0)
        vol, trades = 0.0, []
        for t, mid in path(DIP):
            b, a = mid_to_book(mid)
            vol += 20
            rec.record_quotes({m.ticker: quote(m.ticker, T0 + t, b, a, volume=vol)}, keepalive_s=6)
            # a seller trades one cent under the bid every step (through any resting bid)
            trades.append(Trade(m.ticker, T0 + t + 1, b - 1, 5, "no", f"t{t}"))
        c = cfg({**only("DIP"), **MAKER})
        s0 = run_replay(c, tape, 0, 1e12, tmp / "bt", label=False)             # no prints recorded yet
        self.assertEqual(open_state_db(s0["db_path"]).scalar("SELECT COUNT(*) FROM fills WHERE action='buy'"), 0)
        rec.record_trades(trades)
        s1 = run_replay(c, tape, 0, 1e12, tmp / "bt", label=False)
        s2 = run_replay(c, tape, 0, 1e12, tmp / "bt", label=False)
        self.assertEqual(s1["metrics"], {**s2["metrics"], "wall_s": s1["metrics"]["wall_s"]})
        db = open_state_db(s1["db_path"])
        filled = db.scalar("SELECT COUNT(*) FROM fills WHERE action='buy'")
        self.assertGreater(filled, 0)


class FakeHttp:
    """Scripted exchange for the live venue's partial-fill bookkeeping."""

    def __init__(self):
        self.fills = []
        self.status = "resting"
        self.deleted = False

    def request(self, method, path, params=None, body=None):
        if method == "POST":
            return {"order": {"order_id": "ex1", "status": "resting", "fill_count": 0, "remaining_count": 40}}
        if method == "DELETE":
            self.deleted = True
            return {}
        if path.startswith("/portfolio/orders/"):
            return {"order": {"order_id": "ex1", "status": self.status, "remaining_count_fp": "10"}}
        if path == "/portfolio/fills":
            return {"fills": list(self.fills)}
        return {}


class TestLiveVenueMakerBookkeeping(unittest.TestCase):
    def test_incremental_fills_then_expiry_without_double_count(self):
        from lvt.execution.kalshi_live import KalshiLiveVenue
        http = FakeHttp()
        v = KalshiLiveVenue(http, {})
        r = req(price=49, expire=T0 + 20)
        self.assertEqual(v.submit(r, T0).status, "ACKED")
        http.fills = [{"trade_id": "f1", "count_fp": "15", "yes_price_dollars": "0.4900", "fee_cost_dollars": "0"}]
        ups = v.on_market_data({}, T0 + 2)
        self.assertEqual((ups[0].status, sum(f.count for f in ups[0].fills)), ("PARTIALLY_FILLED", 15))
        self.assertEqual(v.on_market_data({}, T0 + 4), [])                      # nothing new
        http.fills.append({"trade_id": "f2", "count_fp": "5", "yes_price_dollars": "0.4900", "fee_cost_dollars": "0"})
        ups = v.on_market_data({}, T0 + 25)                                      # expired: cancel + final
        self.assertTrue(http.deleted)
        self.assertEqual(len(ups), 1)
        self.assertEqual(ups[0].status, "CANCELLED")
        self.assertEqual(sum(f.count for f in ups[0].fills), 5)                  # only the unreported fill


if __name__ == "__main__":
    unittest.main()
