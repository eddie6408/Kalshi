"""Execution venue interface. The engine talks to a venue; which venue is
plugged in is decided once, at startup, from TRADING_MODE:

    PAPER  -> PaperVenue(shadow=False)
    SHADOW -> PaperVenue(shadow=True)   builds + validates the exact Kalshi
                                        payload, runs the live order path and
                                        reconciliation, never sends anything
    LIVE   -> KalshiLiveVenue           only after lvt.live_gate approves
"""
from __future__ import annotations

import abc

from lvt.models import OrderRequest, OrderUpdate, Snapshot


class Venue(abc.ABC):
    name = "abstract"
    env = "PAPER"
    is_real = False

    @abc.abstractmethod
    def submit(self, req: OrderRequest, now: float) -> OrderUpdate:
        """Must never raise for a business rejection; raising means the
        order state is UNKNOWN and the caller fails closed."""

    @abc.abstractmethod
    def on_market_data(self, snaps: dict[str, Snapshot], now: float) -> list[OrderUpdate]:
        """Advance working orders given fresh data (paper resolves fills
        here; live polls the exchange)."""

    @abc.abstractmethod
    def cancel(self, client_order_id: str, now: float) -> OrderUpdate:
        ...

    @abc.abstractmethod
    def lookup(self, client_order_id: str) -> OrderUpdate | None:
        """Exchange-side truth for one order (reconciliation)."""

    @abc.abstractmethod
    def positions(self) -> dict[str, tuple[str, int]]:
        """ticker -> (side, contracts) as the venue sees it."""

    @abc.abstractmethod
    def working_orders(self) -> list[str]:
        ...

    def balance_usd(self) -> float | None:
        return None

    def on_trades(self, trades, covered=None) -> None:
        """Public trade prints (used by simulated maker fills). Live ignores them."""
