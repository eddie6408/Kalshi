"""A synthetic Kalshi, so you can watch the bot work before it touches money.

WHAT THIS IS
------------
A fake exchange and a fake weather model. Every number is generated. It runs
the REAL estimator, the REAL edge math, the REAL risk engine and the REAL
paper broker - only the data source and the counterparty are invented.

WHAT THIS IS NOT
----------------
Evidence that the strategy works. The simulation is built so the bot's model
is usually a bit more accurate than the simulated market, because a world
where the bot has no edge would be boring to watch. Real markets do not owe
you that. Treat the P/L here as a demonstration of the machinery, never as a
backtest, and never as a reason to raise a risk limit.

HOW A SIMULATED DAY WORKS
-------------------------
1. Draw one true daily high temperature for the city. Nobody sees it.
2. Build a board of temperature buckets, the way Kalshi does.
3. Price the board from a "market view" of the truth that carries its own
   bias - so the market is sometimes right and sometimes wrong.
4. Give the bot an ensemble forecast: noisy members around the truth, with
   the noise shrinking as the day gets closer.
5. Step the clock forward. The bot scans, decides, sizes, and trades.
6. At settlement, resolve every contract at $1.00 or $0.00 and book the P/L.
"""
from __future__ import annotations

import datetime as dt
import math
import random
from dataclasses import dataclass, field

from .models import Market, Orderbook, OrderbookLevel
from .weatherdata import DailyTempForecast

CITY = {
    "ticker_prefix": "SIMHIGHNY",
    "label": "Simulated NYC high",
    "station": "KSIM",
    "latitude": 40.7789,
    "longitude": -73.9692,
    "kind": "high",
    "timezone": "America/New_York",
}


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class SimDay:
    date: dt.date
    true_high: float
    market_bias: float          # how wrong the market's view is today
    model_bias: float           # how wrong our ensemble is today
    buckets: list[tuple[int, int]] = field(default_factory=list)
    lead_hours: float = 36.0
    settled: bool = False


class SimulatedExchange:
    """Quacks like KalshiClient, but every price is invented."""

    def __init__(self, seed: int = 7, days: int = 4, start_lead_hours: float = 30.0,
                 seasonal_mean: float = 72.0, seasonal_sd: float = 7.0):
        self.rng = random.Random(seed)
        self.days = days
        self.start_lead_hours = start_lead_hours
        self.seasonal_mean = seasonal_mean
        self.seasonal_sd = seasonal_sd
        self.day_index = -1
        self.day: SimDay | None = None
        self.balance_cents = 100_000
        self.settlements: list[dict] = []
        self._next_day()

    # ---------------- day lifecycle ----------------
    def _next_day(self) -> bool:
        self.day_index += 1
        if self.day_index >= self.days:
            self.day = None
            return False
        r = self.rng
        true_high = round(r.gauss(self.seasonal_mean, self.seasonal_sd))
        # The market's view of the truth is off by a couple of degrees on a
        # typical day, and badly off now and then. That is where edge comes from.
        market_bias = r.gauss(0.0, 2.2) + (r.choice([-4.0, 4.0]) if r.random() < 0.18 else 0.0)
        model_bias = r.gauss(0.0, 0.7)

        centre = int(round(true_high + r.gauss(0, 1.5)))
        buckets = [(t, t + 1) for t in range(centre - 7, centre + 8, 2)]

        self.day = SimDay(
            date=dt.date.today() + dt.timedelta(days=self.day_index),
            true_high=float(true_high),
            market_bias=market_bias,
            model_bias=model_bias,
            buckets=buckets,
            lead_hours=self.start_lead_hours,
        )
        return True

    def advance(self, minutes: float) -> bool:
        """Move the clock. Returns False when the whole simulation is over."""
        if self.day is None:
            return False
        self.day.lead_hours -= minutes / 60.0
        if self.day.lead_hours <= 0:
            self._settle_day()
            return self._next_day()
        return True

    def _settle_day(self) -> None:
        d = self.day
        if d is None or d.settled:
            return
        d.settled = True
        for lo, hi in d.buckets:
            won = lo - 0.5 <= d.true_high < hi + 0.5
            self.settlements.append({
                "ticker": self._ticker(lo, hi),
                "date": d.date.isoformat(),
                "true_high": d.true_high,
                "yes_won": won,
            })

    # ---------------- pricing ----------------
    def _market_sigma(self) -> float:
        """The market's uncertainty shrinks as settlement approaches."""
        lead = max(0.5, self.day.lead_hours)
        return max(1.2, 1.1 + lead / 14.0)

    def _forecast_sigma(self) -> float:
        lead = max(0.5, self.day.lead_hours)
        return max(0.9, 0.9 + lead / 18.0)

    def _market_probability(self, lo: int, hi: int) -> float:
        d = self.day
        mu = d.true_high + d.market_bias
        s = self._market_sigma()
        return _norm_cdf((hi + 0.5 - mu) / s) - _norm_cdf((lo - 0.5 - mu) / s)

    def _ticker(self, lo: int, hi: int) -> str:
        stamp = self.day.date.strftime("%y%b%d").upper()
        return f"{CITY['ticker_prefix']}-{stamp}-B{lo}.{hi}"

    def _event(self) -> str:
        return f"{CITY['ticker_prefix']}-{self.day.date.strftime('%y%b%d').upper()}"

    # ---------------- KalshiClient-shaped interface ----------------
    def get_markets(self, series_ticker=None, status="open", **kw) -> list[Market]:
        if self.day is None or series_ticker != CITY["ticker_prefix"]:
            return []
        close = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=self.day.lead_hours)
        out = []
        for lo, hi in self.day.buckets:
            p = self._market_probability(lo, hi)
            mid = max(2, min(98, int(round(p * 100))))
            half = 1 if mid > 12 else 2          # tighter spread in the busy middle
            out.append(Market(
                ticker=self._ticker(lo, hi),
                event_ticker=self._event(),
                series_ticker=CITY["ticker_prefix"],
                title=f"{CITY['label']} {lo}-{hi}F",
                subtitle=f"{lo} to {hi}",
                status="open",
                yes_bid=max(1, mid - half),
                yes_ask=min(99, mid + half),
                no_bid=max(1, 100 - mid - half),
                no_ask=min(99, 100 - mid + half),
                last_price=mid,
                volume=self.rng.randint(200, 4000),
                open_interest=self.rng.randint(500, 9000),
                close_time=close,
                strike_type="between",
                floor_strike=float(lo),
                cap_strike=float(hi),
            ))
        return out

    def get_market(self, ticker: str) -> Market:
        for m in self.get_markets(CITY["ticker_prefix"]):
            if m.ticker == ticker:
                return m
        raise KeyError(ticker)

    def get_orderbook(self, ticker: str, depth: int = 20) -> Orderbook:
        m = self.get_market(ticker)
        size = self.rng.randint(150, 900)
        return Orderbook(
            ticker,
            yes=[OrderbookLevel(m.yes_bid, size),
                 OrderbookLevel(max(1, m.yes_bid - 1), size * 2)],
            no=[OrderbookLevel(m.no_bid, size),
                OrderbookLevel(max(1, m.no_bid - 1), size * 2)],
        )

    def get_positions(self):
        return []

    def get_balance_cents(self) -> int:
        return self.balance_cents

    def get_orders(self, status=None):
        return []

    def cancel_order(self, order_id):
        return {}


class SimulatedWeatherData:
    """Quacks like WeatherData. Members cluster around the truth, and tighten
    as settlement approaches - which is what a real ensemble does."""

    def __init__(self, exchange: SimulatedExchange, n_members: int = 28):
        self.ex = exchange
        self.n = n_members

    def daily_extreme_forecast(self, **kw) -> DailyTempForecast | None:
        d = self.ex.day
        if d is None:
            return None
        rng = random.Random(hash((d.date.toordinal(), int(d.lead_hours * 4))) & 0xFFFF)
        mu = d.true_high + d.model_bias
        s = self.ex._forecast_sigma()
        members = [rng.gauss(mu, s) for _ in range(self.n)]
        return DailyTempForecast(
            date=d.date, kind="high", members=members,
            source="SIMULATED-ensemble", observed_extreme_so_far=None,
            lead_hours=d.lead_hours,
        )


def simulation_weather_config() -> dict:
    return {
        "series": [dict(CITY)],
        "ensemble_models": ["simulated"],
        "station_representativeness_sigma_f": 1.3,
        "min_sigma_f": 0.9,
        "fallback_sigma_by_lead_hours": {6: 1.6, 24: 2.8, 72: 5.0},
    }
