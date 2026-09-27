"""The single place that decides which execution connection exists.

    PAPER  -> PaperExecution   (no credentials touched)
    SHADOW -> ShadowExecution  (no credentials touched)
    LIVE   -> KalshiLiveExecution, only if the readiness gate passes and the
              production credentials load. Otherwise LiveModeUnavailable is
              raised and the process exits. There is no fallback to PAPER, no
              guessing of credentials, no alternative account.
"""
from __future__ import annotations

from ..config import TradingMode
from ..engine.fees import FeeModel
from .base import ExecutionClient
from .paper import PaperExecution, ShadowExecution


class LiveModeUnavailable(RuntimeError):
    pass


def build_execution(cfg, fees: FeeModel, data_provider, db, env: dict[str, str] | None = None) -> ExecutionClient:
    mode = cfg.mode
    if mode is TradingMode.PAPER:
        return PaperExecution(cfg.paper, fees, seed=cfg.app.random_seed)
    if mode is TradingMode.SHADOW:
        return ShadowExecution(cfg.paper, fees, data_provider, seed=cfg.app.random_seed, live_cfg=cfg.live)
    if mode is TradingMode.LIVE:
        from .. import readiness
        rep = readiness.evaluate(cfg, db, env)
        if not rep.ready:
            raise LiveModeUnavailable("LIVE MODE UNAVAILABLE:\n" + "\n".join(rep.lines()))
        from .kalshi_auth import CredentialError, KalshiSigner
        from .live import KalshiLiveExecution
        try:
            signer = KalshiSigner.from_env("prod", env)
        except CredentialError as e:
            raise LiveModeUnavailable(f"LIVE MODE UNAVAILABLE: {e}") from e
        return KalshiLiveExecution(cfg.live, fees, signer, cfg.kalshi.rest_base_url, env_name="LIVE")
    raise LiveModeUnavailable(f"unknown mode {mode}")
