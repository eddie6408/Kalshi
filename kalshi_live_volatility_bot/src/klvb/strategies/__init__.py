from ..models import Direction
from .base import Strategy, StrategyContext
from .dip import DipStrategy
from .momentum import MomentumStrategy


def build_strategies(cfg) -> list[Strategy]:
    """Instantiate enabled strategies. REVERSAL/DOWNTREND are the NO-side mirrors of DIP/MOMENTUM."""
    out: list[Strategy] = []
    for name in cfg.strategies.enabled:
        params = cfg.strategy_params(name)
        if name == "DIP":
            out.append(DipStrategy(params, Direction.UP))
        elif name == "REVERSAL":
            out.append(DipStrategy(params, Direction.DOWN))
        elif name == "MOMENTUM":
            out.append(MomentumStrategy(params, Direction.UP))
        elif name == "DOWNTREND":
            out.append(MomentumStrategy(params, Direction.DOWN))
        else:
            raise ValueError(f"unknown strategy {name}")
    return out


__all__ = ["Strategy", "StrategyContext", "DipStrategy", "MomentumStrategy", "build_strategies"]
