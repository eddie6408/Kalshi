"""Risk limits, sizing, daily loss, kill switch, positions, exits, restart recovery."""
import time
from types import SimpleNamespace

from conftest import T0

from klvb.engine.volatility import Features
from klvb.execution.base import ExecutionEvent
from klvb.models import Direction, Fill, MarketMeta, OrderStatus, Position, Signal
from klvb.portfolio.exits import evaluate_exit
from klvb.portfolio.positions import PositionManager
from klvb.portfolio.reconcile import reconcile_simulated
from klvb.risk.engine import RiskEngine


def sig(ticker="T1", score=80.0, entry=50.0, stop=45.0, strategy="DIP"):
    return Signal(ts=T0, ticker=ticker, strategy=strategy, strategy_version="V1", direction=Direction.UP,
                  action="BUY", entry_ref=entry, target=entry + 6, stop=stop, score=score, expected_gross=6,
                  expected_costs=2, expected_net=4, max_hold_seconds=600)


def feat(ticker="T1", bid=49.0, ask=50.0, depth=500.0, vs=0.0, vm=0.0, age=0.5):
    return Features(ticker=ticker, ts=T0, samples=60, price=(bid + ask) / 2, bid=bid, ask=ask, spread=ask - bid,
                    bid_depth=depth, ask_depth=depth, data_age=age, velocity_short=vs, velocity_medium=vm)


META = MarketMeta(ticker="T1", event_ticker="E1", close_ts=T0 + 6 * 3600)


def test_position_sizing_respects_every_cap(cfg):
    r = RiskEngine(cfg.risk, cfg.app.timezone)
    d = r.check_entry(sig(), META, feat(), [], 0.0, T0)
    assert d.approved
    risk_pc = (50 - 45) + 2
    assert d.qty <= cfg.risk.max_risk_per_trade * 100 / risk_pc
    assert d.qty * 50 / 100 <= cfg.risk.max_position_notional
    assert d.qty <= cfg.risk.max_position_contracts
    thin = r.check_entry(sig(), META, feat(depth=120), [], 0.0, T0)
    assert thin.qty <= 120 * cfg.risk.max_depth_participation


def test_lower_score_means_smaller_size(cfg):
    r = RiskEngine(cfg.risk, cfg.app.timezone)
    hi = r.check_entry(sig(score=100, stop=48), META, feat(), [], 0.0, T0).qty
    lo = r.check_entry(sig(score=60, stop=48), META, feat(), [], 0.0, T0).qty
    assert hi > lo >= 1


def test_drawdown_shrinks_size(cfg):
    r = RiskEngine(cfg.risk, cfg.app.timezone)
    base = r.check_entry(sig(stop=48), META, feat(), [], 0.0, T0).qty
    r.peak_equity = r.starting_equity * 1.25   # 20% drawdown
    assert r.check_entry(sig(stop=48), META, feat(), [], 0.0, T0).qty < base


def test_daily_loss_limit_stops_new_positions_and_does_not_chase(cfg):
    r = RiskEngine(cfg.risk, cfg.app.timezone)
    r.check_entry(sig(), META, feat(), [], 0.0, T0)  # roll the day
    r.realized_today = -cfg.risk.max_daily_loss
    d = r.check_entry(sig(), META, feat(), [], 0.0, T0)
    assert not d.approved and any("daily loss" in x for x in d.reasons)
    # unrealized losses count too
    r2 = RiskEngine(cfg.risk, cfg.app.timezone)
    assert not r2.check_entry(sig(), META, feat(), [], -cfg.risk.max_daily_loss - 1, T0).approved
    # a later profit today does NOT re-enable trading ("no win it back")
    r.realized_today = 0
    assert not r.check_entry(sig(), META, feat(), [], 0.0, T0).approved


def test_strategy_daily_loss(cfg):
    r = RiskEngine(cfg.risk, cfg.app.timezone)
    r.check_entry(sig(), META, feat(), [], 0.0, T0)
    r.strategy_today["DIP"] = -cfg.risk.max_strategy_daily_loss
    assert not r.check_entry(sig(), META, feat(), [], 0.0, T0).approved
    assert r.check_entry(sig(strategy="MOMENTUM"), META, feat(), [], 0.0, T0).approved


def test_kill_switch_and_halt(cfg):
    r = RiskEngine(cfg.risk, cfg.app.timezone)
    r.set_kill_switch(True, "operator")
    assert not r.check_entry(sig(), META, feat(), [], 0.0, T0).approved
    r.set_kill_switch(False)
    r.halt("unknown order")
    assert not r.check_entry(sig(), META, feat(), [], 0.0, T0).approved
    r.clear_halt()
    assert r.check_entry(sig(), META, feat(), [], 0.0, T0).approved


def test_frequency_cooldown_concurrency(cfg):
    r = RiskEngine(cfg.risk, cfg.app.timezone)
    for i in range(cfg.risk.max_trades_per_market_per_hour):
        r.on_entry("T1", T0)
    assert not r.check_entry(sig(), META, feat(), [], 0.0, T0).approved
    r2 = RiskEngine(cfg.risk, cfg.app.timezone)
    p = Position(env="PAPER", ticker="T1", event_ticker="E1", side="yes", direction=Direction.UP, strategy="DIP",
                 strategy_version="V1", signal_id=None, target=56, stop=45, max_hold_seconds=600)
    r2.on_position_closed(p, 1.0, T0)
    d = r2.check_entry(sig(), META, feat(), [], 0.0, T0 + 10)
    assert not d.approved and any("cooldown" in x for x in d.reasons)
    many = [Position(env="PAPER", ticker=f"X{i}", event_ticker=f"EX{i}", side="yes", direction=Direction.UP,
                     strategy="DIP", strategy_version="V1", signal_id=None, target=1, stop=1, max_hold_seconds=1,
                     qty=1, avg_entry=10) for i in range(cfg.risk.max_concurrent_positions)]
    assert not r2.check_entry(sig("T9"), MarketMeta(ticker="T9", event_ticker="E9"), feat("T9"), many, 0.0,
                              T0 + 1000).approved


def _fill(order, qty, price, action="buy", fee=2.0, ref=None):
    return Fill(order_id=order.id, env="PAPER", ticker=order.ticker, side=order.side, action=action, count=qty,
                price=price, fee=fee, is_taker=True, ts=T0, ref_price=ref)


def test_position_lifecycle_pnl_and_trade_record(cfg, db):
    risk = RiskEngine(cfg.risk, cfg.app.timezone)
    pm = PositionManager(db, "PAPER", risk, "run1", cfg.app.timezone)
    s = sig()
    pos, entry = pm.open_from_signal(s, META, 10, "HIGH_VOLATILITY", feat(), 3, T0)
    assert entry.limit_price == 53.0 and entry.purpose == "ENTRY"
    entry.status, entry.filled = OrderStatus.FILLED, 10
    pm.on_event(ExecutionEvent(entry, [_fill(entry, 10, 50.0, ref=50.0)]), T0)
    assert pos.status == "OPEN" and pos.qty == 10 and pos.avg_entry == 50.0
    ex = pm.exit_order(pos, "FIXED_PROFIT_TARGET: test", 55.0, T0 + 60)
    assert ex.action == "sell" and ex.count == 10 and pos.status == "CLOSING"
    ex.status, ex.filled = OrderStatus.CANCELED, 4
    pm.on_event(ExecutionEvent(ex, [_fill(ex, 4, 56.0, "sell", 1.0)]), T0 + 61)
    assert pos.qty == 6 and pos.status == "OPEN"          # partial exit: keep managing
    ex2 = pm.exit_order(pos, "FIXED_PROFIT_TARGET: test", 55.0, T0 + 62)
    ex2.status, ex2.filled = OrderStatus.FILLED, 6
    closed = pm.on_event(ExecutionEvent(ex2, [_fill(ex2, 6, 55.0, "sell", 1.0)]), T0 + 63)
    assert pos.status == "CLOSED" and len(closed) == 1
    t = closed[0]
    assert t["gross_pnl"] == 4 * 6 + 6 * 5 and t["fees"] == 4.0 and t["net_pnl"] == 50.0
    assert db.query("SELECT COUNT(*) AS n FROM trades")[0]["n"] == 1
    assert risk.realized_total == 0.5 and risk.realized_today == 0.5


def test_unfilled_entry_closes_without_trade(cfg, db):
    pm = PositionManager(db, "PAPER", RiskEngine(cfg.risk, cfg.app.timezone), "r")
    pos, entry = pm.open_from_signal(sig(), META, 10, "", feat(), 3, T0)
    entry.status = OrderStatus.MISSED
    pm.on_event(ExecutionEvent(entry, []), T0 + 2)
    assert pos.status == "CLOSED" and "ENTRY_NOT_FILLED" in pos.exit_reason
    assert db.scalar("SELECT COUNT(*) FROM trades") == 0


def _pos(**kw):
    p = Position(env="PAPER", ticker="T1", event_ticker="E1", side=kw.get("side", "yes"), direction=Direction.UP,
                 strategy=kw.get("strategy", "DIP"), strategy_version="V1", signal_id=None,
                 target=kw.get("target", 56), stop=kw.get("stop", 45), max_hold_seconds=600, qty=10, avg_entry=50,
                 opened_ts=kw.get("opened", T0))
    p.peak_mark = kw.get("peak")
    return p



def test_exit_engine_priorities(cfg):
    e, r = cfg.exits, cfg.risk
    assert evaluate_exit(_pos(), feat(bid=44, ask=45), META, T0, e, r).reason == "MAX_LOSS_EXIT"
    assert evaluate_exit(_pos(), feat(bid=57, ask=58), META, T0, e, r).reason == "FIXED_PROFIT_TARGET"
    assert evaluate_exit(_pos(peak=55), feat(bid=52, ask=53), META, T0, e, r).reason == "TRAILING_PROFIT_TARGET"
    assert evaluate_exit(_pos(), feat(bid=49, ask=50), META, T0 + 700, e, r).reason == "TIME_EXIT"
    closing = MarketMeta(ticker="T1", close_ts=T0 + 100)
    assert evaluate_exit(_pos(), feat(), closing, T0, e, r).reason == "SETTLEMENT_EXIT"
    assert evaluate_exit(_pos(), feat(bid=45.5, ask=53), META, T0, e, r).reason == "LIQUIDITY_EXIT"
    assert evaluate_exit(_pos(), feat(depth=1), META, T0, e, r).reason == "LIQUIDITY_EXIT"
    assert evaluate_exit(_pos(), feat(), META, T0, e, r, flatten="kill").reason == "EMERGENCY_EXIT"
    # momentum exit needs short AND medium trend against plus an adverse move
    assert evaluate_exit(_pos(strategy="MOMENTUM"), feat(bid=49, ask=50, vs=-3, vm=-1), META, T0, e, r).reason \
        == "MOMENTUM_EXIT"
    assert evaluate_exit(_pos(strategy="MOMENTUM"), feat(bid=51, ask=52, vs=-3, vm=1), META, T0, e, r) is None
    assert evaluate_exit(_pos(strategy="DIP"), feat(bid=47, ask=48, vs=-3, vm=-1), META, T0, e, r).reason \
        == "REVERSAL_EXIT"
    assert evaluate_exit(_pos(), feat(bid=50, ask=51), META, T0, e, r) is None   # hold
    # NO side is evaluated on the NO price (100 - YES ask)
    no = _pos(side="no", target=56, stop=45)
    assert evaluate_exit(no, feat(bid=42, ask=43), META, T0, e, r).reason == "FIXED_PROFIT_TARGET"


def test_restart_recovery_and_simulated_reconciliation(cfg, db):
    risk = RiskEngine(cfg.risk, cfg.app.timezone)
    pm = PositionManager(db, "PAPER", risk, "r1")
    pos, entry = pm.open_from_signal(sig(), META, 10, "", feat(), 3, T0)
    entry.status, entry.filled = OrderStatus.FILLED, 10
    pm.on_event(ExecutionEvent(entry, [_fill(entry, 10, 50.0)]), T0)
    tp = pm.take_profit_order(pos, T0)
    tp.status = OrderStatus.RESTING
    pm.persist_order(tp)
    # "crash" -> new process
    risk2 = RiskEngine(cfg.risk, cfg.app.timezone)
    pm2 = PositionManager(db, "PAPER", risk2, "r2")
    loaded = pm2.load_open()
    assert len(loaded) == 1 and loaded[0].qty == 10 and loaded[0].avg_entry == 50.0
    res = reconcile_simulated(db, "PAPER", loaded, restart=True)
    assert res.ok and res.info["orphaned_orders_canceled"] == 1
    assert db.scalar("SELECT status FROM orders WHERE id=?", (tp.id,)) == "CANCELED"
    # corrupt the ledger -> mismatch detected
    loaded[0].qty = 7
    assert not reconcile_simulated(db, "PAPER", loaded).ok


def test_risk_counters_restored_after_restart(cfg, db):
    risk = RiskEngine(cfg.risk, cfg.app.timezone)
    pm = PositionManager(db, "PAPER", risk, "r1")
    now = time.time()
    db.insert("trades", {"id": "t1", "env": "PAPER", "ticker": "T1", "strategy": "DIP", "net_pnl": -2500.0,
                         "gross_pnl": -2400.0, "fees": 100.0, "qty": 10, "opened_ts": now - 100, "closed_ts": now - 50})
    pm.restore_risk(now)
    assert risk.realized_today == -25.0 and risk.strategy_today["DIP"] == -25.0
