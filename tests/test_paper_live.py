"""Packages 6/7: the fake-money account on production prices.

Pinned: weather entries are priced by the live strategy's own code on the
production book and pass the same profitability gate; the market-skill gate
is recorded, not obeyed (and can be obeyed); one position per market, caps
on exposure and cash; settlement books payout - cost - fee; an arbitrage the
sweep saw is re-checked on production and paper-traded only if it exists
there; nothing can send an order.
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kalshibot.gate import ProfitabilityGate
from kalshibot.journal import Journal
from kalshibot.models import Market
from kalshibot.paper_live import PaperConfig, PaperLiveAccount, ProdArbCheck
from kalshibot.prodwatch import Quote, RateLimiter
from kalshibot.strategies.arbitrage import StructuralArbitrageScanner
from kalshibot.strategies.forecast import ForecastStrategy

import test_arbitrage as ta
from test_forecast import FEES, FakeEstimator, book, cfg, mkt

GATE = ProfitabilityGate({"require_guaranteed": False, "allow_forecast": True,
                          "min_probability_edge": 0.05, "min_confidence": 0.5,
                          "min_ev_usd": 0.01, "max_opportunity_age_seconds": 600})


class Skill:
    def __init__(self, ok):
        self.ok = ok

    def may_enter(self, series, lead):
        return self.ok, "" if self.ok else "market forecasts better here"


def account(tmp, skill_ok=True, **kw):
    j = Journal(Path(tmp) / "j.db")
    est = FakeEstimator(probability=0.72)
    clone = ForecastStrategy(est, FEES, cfg(), journal=None)
    acct = PaperLiveAccount(j, GATE, clone, Skill(skill_ok), PaperConfig.from_dict(kw))
    return acct, j, est


def info(est, m=None):
    m = m or mkt()
    return {"market": m, "estimate": est.estimate(m), "at": time.time(), "q": 0.72}


def quote(b=None):
    return Quote.of("KXFAKE-1", b or book(), time.time())


def test_a_weather_entry_is_priced_by_the_live_code_on_the_production_book():
    with tempfile.TemporaryDirectory() as tmp:
        acct, j, est = account(tmp)
        acct.on_book("KXFAKE-1", quote(), info(est))
        (r,) = j.paper_rows()
        assert (r["kind"], r["side"], r["state"], r["gate_blocked"]) == \
            ("FORECAST", "yes", "OPEN", 0)
        assert r["price_cents"] == 61 and r["contracts"] > 0
        assert r["cost_usd"] == r["contracts"] * 0.61
        # one position per market
        acct.on_book("KXFAKE-1", quote(), info(est))
        assert len(j.paper_rows()) == 1


def test_the_market_skill_gate_is_recorded_not_obeyed_unless_configured():
    with tempfile.TemporaryDirectory() as tmp:
        acct, j, est = account(tmp, skill_ok=False)
        acct.on_book("KXFAKE-1", quote(), info(est))
        (r,) = j.paper_rows()
        assert r["gate_blocked"] == 1 and "market" in r["note"]
    with tempfile.TemporaryDirectory() as tmp:
        acct, j, est = account(tmp, skill_ok=False, include_gate_blocked=False)
        acct.on_book("KXFAKE-1", quote(), info(est))
        assert j.paper_rows() == [] and acct.declined == {"market-skill gate": 1}


def test_the_live_rules_still_apply_on_production_prices():
    with tempfile.TemporaryDirectory() as tmp:
        acct, j, est = account(tmp)
        acct.on_book("KXFAKE-1", quote(book(yes_ask=70)), info(est))   # 2pt edge
        assert j.paper_rows() == [] and "edge too small" in acct.declined


def test_caps_on_exposure_and_open_positions():
    with tempfile.TemporaryDirectory() as tmp:
        acct, j, est = account(tmp, max_total_exposure_usd=1.0)
        acct.on_book("KXFAKE-1", quote(), info(est))
        assert j.paper_rows() == [] and acct.declined == {"exposure cap": 1}


def test_stale_or_missing_verdicts_are_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        acct, j, est = account(tmp)
        acct.on_book("KXFAKE-1", quote(), {})
        old = info(est)
        old["at"] -= 3600
        acct.on_book("KXFAKE-1", quote(), old)
        assert j.paper_rows() == []


def test_settlement_books_payout_less_cost_and_fee():
    with tempfile.TemporaryDirectory() as tmp:
        acct, j, est = account(tmp)
        acct.on_book("KXFAKE-1", quote(), info(est))
        (r,) = j.paper_rows()
        m = Market(ticker="KXFAKE-1", event_ticker="EV", series_ticker="KXFAKE",
                   status="settled", raw={"status": "settled", "result": "yes"})

        class Client:
            def get_market(self, t):
                return m
        assert acct.resolve(Client())["settled"] == 1
        (s,) = j.paper_rows()
        assert s["state"] == "SETTLED" and s["payout_usd"] == r["contracts"]
        assert abs(s["pnl_usd"] - (r["contracts"] - r["cost_usd"] - r["fee_usd"])) < 1e-6
        st = acct.state()
        assert st["open"] == 0 and abs(st["cash"] - (500 + s["pnl_usd"])) < 1e-6


# ------------------------------------------------------------ arbitrage
class Reader:
    def __init__(self, books):
        self.books = books

    def orderbook(self, t, depth=50):
        return self.books.get(t)


def _arb_check(tmp, prod_books):
    j = Journal(Path(tmp) / "j.db")
    acct = PaperLiveAccount(j, ProfitabilityGate({}), None, None, PaperConfig())
    scanner = StructuralArbitrageScanner(ta.FEES, {"max_contracts_per_trade": 500})
    chk = ProdArbCheck(Reader(prod_books), scanner, ProfitabilityGate({}), j, acct,
                       PaperConfig(), limiter=RateLimiter(0, sleep=lambda s: None))
    return chk, j


def test_an_arbitrage_that_exists_on_production_is_paper_traded():
    with tempfile.TemporaryDirectory() as tmp:
        arb = ta.book("T", yes_ask=40, yes_ask_qty=100, no_ask=52, no_ask_qty=100)
        chk, j = _arb_check(tmp, {"T": arb})
        chk.observe("EV", [ta.mkt("T")], ["T"], [], False)
        assert chk.run_pass() == 1
        (c,) = [dict(r) for r in j.conn.execute("SELECT * FROM prod_arb_checks")]
        assert c["prod_found"] >= 1 and c["prod_accepted"] >= 1 and c["demo_found"] == 0
        legs = j.paper_rows()
        assert {r["kind"] for r in legs} == {"ARB"} and len({r["group_id"] for r in legs}) == 1
        assert {r["side"] for r in legs} == {"yes", "no"}


def test_a_demo_arbitrage_missing_on_production_is_recorded_not_traded():
    with tempfile.TemporaryDirectory() as tmp:
        tight = ta.book("T", yes_ask=50, yes_ask_qty=100, no_ask=51, no_ask_qty=100)
        chk, j = _arb_check(tmp, {"T": tight})

        class DemoOp:
            net_edge = 1.25
        chk.observe("EV", [ta.mkt("T")], ["T"], [DemoOp()], False)
        chk.run_pass()
        (c,) = [dict(r) for r in j.conn.execute("SELECT * FROM prod_arb_checks")]
        assert (c["demo_found"], c["demo_best_edge"], c["prod_found"]) == (1, 1.25, 0)
        assert j.paper_rows() == []


def test_the_module_cannot_send_an_order():
    src = (Path(__file__).resolve().parents[1] / "kalshibot" / "paper_live.py").read_text()
    for word in ("place_order", "place_demo_order", "create_order", "cancel_order",
                 "DemoExecutionClient", "ExecutionManager"):
        assert word not in src
