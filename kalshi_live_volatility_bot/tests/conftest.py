import time

import pytest

from klvb.config import load_config
from klvb.engine.fees import FeeModel
from klvb.engine.states import classify
from klvb.engine.volatility import VolatilityEngine
from klvb.models import BookLevel, MarketMeta, MarketSnapshot, OrderBook
from klvb.storage.db import Database
from klvb.strategies import StrategyContext

T0 = 1_780_000_000.0


@pytest.fixture
def cfg(tmp_path):
    return load_config(env={"KLVB_DATA_DIR": str(tmp_path / "data"), "KLVB_LOG_DIR": str(tmp_path / "logs")})


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.sqlite3")
    yield d
    d.close()


@pytest.fixture
def fees(cfg):
    return FeeModel.from_config(cfg.fees)


def snap(ticker, ts, mid, spread=1.0, size=500.0, volume=None, book=True, status="active"):
    bid = mid - spread / 2
    ask = mid + spread / 2
    b = None
    if book:
        b = OrderBook(ts=ts, yes_bids=[BookLevel(bid - i, size) for i in range(3)],
                      no_bids=[BookLevel(100 - ask - i, size) for i in range(3)])
    return MarketSnapshot(ticker=ticker, ts=ts, yes_bid=bid, yes_ask=ask, last_price=bid, volume=volume,
                          volume_24h=50_000, open_interest=10_000, yes_bid_size=size, yes_ask_size=size,
                          status=status, book=b)


def feed(engine: VolatilityEngine, ticker: str, prices: list[float], start=T0, step=2.0, vol_per_step=20.0,
         vol_boost_from: int | None = None, spread=1.0, size=500.0):
    v = 0.0
    for i, p in enumerate(prices):
        v += vol_per_step * (3.0 if vol_boost_from is not None and i >= vol_boost_from else 1.0)
        s = snap(ticker, start + i * step, p, spread=spread, size=size, volume=v)
        engine.on_snapshot(s)
    return start + (len(prices) - 1) * step


def ramp(a, b, n):
    return [a + (b - a) * i / max(n - 1, 1) for i in range(n)]


def make_ctx(cfg, fees, engine, ticker, now, meta=None):
    f = engine.features(ticker, now)
    meta = meta or MarketMeta(ticker=ticker, event_ticker="EV", sport="BASKETBALL", category="Sports",
                              close_ts=now + 6 * 3600, fee_type="quadratic", fee_multiplier=1.0)
    st = classify(f, cfg.volatility, cfg.eligibility, cfg.volatility.min_samples, 6 * 3600, 900)
    return StrategyContext(now=now, meta=meta, features=f, state=st, series=engine.get(ticker), fees=fees,
                           costs_cfg=cfg.costs, eligibility_cfg=cfg.eligibility, vol_cfg=cfg.volatility,
                           min_score=cfg.strategies.min_signal_score, take_profit_maker=True)
