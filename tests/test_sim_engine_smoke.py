"""End-to-end smoke test with a fake exchange and a fake forecast.

No network, no credentials. Proves the whole pipeline runs:
discover -> estimate -> edge -> risk -> paper fill -> journal row.
"""
import datetime as dt
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.sim_engine import Engine
from kalshibot.estimators.weather import WeatherTemperatureEstimator
from kalshibot.execution import PaperBroker
from kalshibot.fees import FeeModel
from kalshibot.journal import Journal
from kalshibot.models import Market, Orderbook, OrderbookLevel
from kalshibot.risk import RiskManager
from kalshibot.weatherdata import DailyTempForecast

TOMORROW = dt.date.today() + dt.timedelta(days=1)
STAMP = TOMORROW.strftime("%y%b%d").upper()          # e.g. 26SEP13


class FakeSettings:
    kalshi_env = "demo"
    raw = {
        "execution": {"mode": "paper", "paper_fill_model": "cross"},
        "risk": {"bankroll_usd": 1000.0, "max_cost_per_trade_usd": 50.0,
                 "max_exposure_per_market_usd": 250.0, "max_total_open_exposure_usd": 1000.0,
                 "max_fraction_of_bankroll_at_risk": 0.05, "max_open_positions": 10,
                 "max_orders_per_hour": 20, "kelly_fraction": 0.25,
                 "allow_averaging_down": False},
        "edge": {"min_edge_after_fees": 0.05, "min_confidence": 0.50,
                 "min_price_cents": 5, "max_price_cents": 95,
                 "min_resting_contracts_at_our_price": 10, "max_spread_cents": 4,
                 "min_minutes_to_close": 45, "max_hours_to_close": 60},
        "safety": {"min_account_balance_usd": 50.0, "daily_loss_limit_usd": 100.0,
                   "max_consecutive_losses": 5},
        "exit": {"exit_when_edge_below": 0.01, "stop_loss_fraction": 0.5,
                 "take_profit_edge_remaining": 0.015, "flatten_minutes_before_close": 20},
        "loop": {"scan_interval_seconds": 300, "manage_interval_seconds": 60},
    }
    mode = "paper"

    def section(self, name):
        return self.raw.get(name, {})

    def get(self, dotted, default=None):
        node = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


class FakeClient:
    """A two-market exchange. The 72-73 bucket is badly underpriced."""

    def __init__(self):
        close = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=20)
        self.markets = [
            Market(ticker=f"KXHIGHNY-{STAMP}-B72.5", event_ticker=f"KXHIGHNY-{STAMP}",
                   series_ticker="KXHIGHNY", title="NYC high 72-73",
                   status="open", yes_bid=18, yes_ask=20, no_bid=80, no_ask=82,
                   close_time=close, strike_type="between",
                   floor_strike=72, cap_strike=73),
            Market(ticker=f"KXHIGHNY-{STAMP}-B88.5", event_ticker=f"KXHIGHNY-{STAMP}",
                   series_ticker="KXHIGHNY", title="NYC high 88-89",
                   status="open", yes_bid=40, yes_ask=42, no_bid=58, no_ask=60,
                   close_time=close, strike_type="between",
                   floor_strike=88, cap_strike=89),
        ]

    def get_markets(self, series_ticker=None, status="open", **kw):
        return [m for m in self.markets if m.series_ticker == series_ticker]

    def get_market(self, ticker):
        return next(m for m in self.markets if m.ticker == ticker)

    def get_orderbook(self, ticker, depth=20):
        return Orderbook(ticker,
                         yes=[OrderbookLevel(18, 900)],
                         no=[OrderbookLevel(80, 900)])

    def get_positions(self):
        return []

    def get_balance_cents(self):
        return 100_000

    def get_orders(self, status=None):
        return []


class FakeWeather:
    """Ensemble says it will be 72.4F, tightly clustered."""

    def daily_extreme_forecast(self, **kw):
        members = [71.8, 72.1, 72.3, 72.4, 72.5, 72.6, 72.2, 72.9, 71.9, 72.4,
                   72.7, 72.0, 72.5, 72.3, 72.6, 72.1, 72.8, 72.2, 72.4, 72.5]
        return DailyTempForecast(kw["target_date"], "high", members,
                                 "fake-ensemble", None, 18.0)


def _build(tmpdir):
    s = FakeSettings()
    fees = FeeModel({"safety_multiplier": 1.0})
    est = WeatherTemperatureEstimator(
        {"series": [{"ticker_prefix": "KXHIGHNY", "label": "NYC", "station": "KNYC",
                     "latitude": 40.78, "longitude": -73.97, "kind": "high",
                     "timezone": "America/New_York"}],
         "station_representativeness_sigma_f": 1.6, "min_sigma_f": 1.0,
         "fallback_sigma_by_lead_hours": {24: 2.8}},
        data=FakeWeather())
    journal = Journal(Path(tmpdir) / "j.db")
    return Engine(s, FakeClient(), est, PaperBroker(fees), fees,
                  RiskManager(s.raw, fees), journal), journal


def test_engine_finds_and_takes_the_mispriced_market():
    with tempfile.TemporaryDirectory() as tmp:
        engine, journal = _build(tmp)
        placed = engine.scan_once()
        assert placed == 1, f"expected exactly 1 order, got {placed}"

        trades = list(journal.conn.execute("SELECT * FROM trades"))
        assert len(trades) == 1
        t = trades[0]
        assert t["ticker"].endswith("B72.5")
        assert t["side"] == "yes"          # bought the underpriced 72-73 bucket
        assert t["price_cents"] == 20
        assert t["contracts"] > 0
        assert t["our_probability"] > 0.40


def test_engine_records_a_decision_for_every_market():
    with tempfile.TemporaryDirectory() as tmp:
        engine, journal = _build(tmp)
        engine.scan_once()
        rows = list(journal.conn.execute("SELECT ticker, action FROM decisions"))
        assert len(rows) == 2
        actions = {r["ticker"].split("-")[-1]: r["action"] for r in rows}
        assert actions["B72.5"] == "traded"
        assert actions["B88.5"] in ("skipped", "rejected")


def test_paper_mode_never_calls_the_trading_api():
    with tempfile.TemporaryDirectory() as tmp:
        engine, _ = _build(tmp)

        def boom(*a, **k):
            raise AssertionError("paper mode must not create orders")

        engine.client.create_order = boom
        engine.scan_once()


def test_position_tracking_after_a_paper_fill():
    with tempfile.TemporaryDirectory() as tmp:
        engine, _ = _build(tmp)
        engine.scan_once()
        assert len(engine.paper.positions) == 1
        pos = next(iter(engine.paper.positions.values()))
        assert pos.contracts > 0
        assert engine.paper.cash_spent_usd > 0


def test_exposure_caps_stop_a_runaway_loop():
    with tempfile.TemporaryDirectory() as tmp:
        engine, _ = _build(tmp)
        for _ in range(8):
            engine.scan_once()
        limit = FakeSettings.raw["risk"]["max_exposure_per_market_usd"]
        # The cap applies to cost basis. Fees are charged on top of it.
        cost_basis = sum(p.total_cost_cents for p in engine.paper.positions.values()) / 100.0
        assert cost_basis <= limit, f"cost basis ${cost_basis:.2f} exceeds ${limit} cap"
        assert cost_basis > 0, "bot never traded at all"
        # And it must have stopped, not kept buying forever.
        fees_paid = engine.paper.cash_spent_usd - cost_basis
        assert fees_paid < 0.10 * limit, f"fees ${fees_paid:.2f} look wrong"


def test_cooldown_stops_rebuying_the_same_market_every_scan():
    """Regression: the bot used to buy the same contract on every single
    scan, paying an entry fee each time."""
    with tempfile.TemporaryDirectory() as tmp:
        engine, journal = _build(tmp)
        engine.s.raw["edge"]["min_minutes_between_entries_same_market"] = 60
        placed = sum(engine.scan_once() for _ in range(5))
        assert placed == 1, f"cooldown should allow exactly 1 entry, got {placed}"


def test_every_entry_gets_closed_not_just_the_last_one():
    """Regression: a position built from several entries only ever had its
    most recent trade marked closed, so exposure and P/L drifted."""
    with tempfile.TemporaryDirectory() as tmp:
        engine, journal = _build(tmp)
        engine.s.raw["edge"]["min_minutes_between_entries_same_market"] = 0
        engine.s.raw["risk"]["allow_averaging_down"] = True
        for _ in range(3):
            engine.scan_once()

        ticker = next(iter(engine.paper.positions))
        entries = list(journal.conn.execute(
            "SELECT COUNT(*) n FROM trades WHERE ticker=? AND status!='closed'",
            (ticker,)))[0]["n"]
        assert entries >= 2, "test needs a multi-entry position"

        n_closed, pnl = journal.close_open_trades(ticker, 100, 0, outcome="yes")
        assert n_closed == entries
        left = list(journal.conn.execute(
            "SELECT COUNT(*) n FROM trades WHERE ticker=? AND status!='closed'",
            (ticker,)))[0]["n"]
        assert left == 0, f"{left} entries still look open after settlement"


def test_settlement_pnl_matches_hand_calculation():
    with tempfile.TemporaryDirectory() as tmp:
        journal = Journal(Path(tmp) / "j.db")
        # Bought 100 YES at 40c, paid $1.68 in fees. YES settles true.
        journal.log_trade(decision_id=None, mode="paper", ticker="T", side="yes",
                          action="buy", contracts=100, price_cents=40,
                          fee_usd=1.68, our_probability=0.55)
        n, pnl = journal.close_open_trades("T", 100, 0, outcome="yes")
        # 100 * (1.00 - 0.40) - 1.68 = $58.32
        assert n == 1
        assert abs(pnl - 58.32) < 1e-6, pnl


def test_settlement_pnl_on_a_loser():
    with tempfile.TemporaryDirectory() as tmp:
        journal = Journal(Path(tmp) / "j.db")
        journal.log_trade(decision_id=None, mode="paper", ticker="T", side="yes",
                          action="buy", contracts=100, price_cents=40,
                          fee_usd=1.68, our_probability=0.55)
        n, pnl = journal.close_open_trades("T", 0, 100, outcome="no")
        # lose the whole 40c stake plus fees
        assert abs(pnl - (-41.68)) < 1e-6, pnl


def test_no_side_settles_as_the_mirror_image():
    with tempfile.TemporaryDirectory() as tmp:
        journal = Journal(Path(tmp) / "j.db")
        journal.log_trade(decision_id=None, mode="paper", ticker="T", side="no",
                          action="buy", contracts=50, price_cents=30,
                          fee_usd=0.74, our_probability=0.35)
        # YES loses, so NO pays out
        n, pnl = journal.close_open_trades("T", 0, 100, outcome="no")
        assert abs(pnl - (50 * 0.70 - 0.74)) < 1e-6, pnl
