"""Train / validation / out-of-sample evaluation.

  1. Split the recorded period by TIME into train (60%), validation (20%) and
     out-of-sample (20%). Later data is never used to choose parameters.
  2. For each parameter set in a small grid, replay the TRAIN window.
  3. Keep sets with at least `min_trades` trades; rank by expectancy per trade.
  4. Confirm the best few on VALIDATION; pick the best validated set.
  5. Replay the OUT-OF-SAMPLE window exactly once with the chosen set (and the
     default config as a baseline) and record the result.

The grid is intentionally small: many combinations over a short history is how
overfitting happens.
"""
from __future__ import annotations

import itertools
import sqlite3
from typing import Any

from ..config import Config
from ..storage.db import Database
from .replay import ReplayEngine, cfg_with, load_recorded

DEFAULT_GRID: dict[str, list[Any]] = {
    "strategies.dip.min_drop_cents": [6, 8],
    "strategies.momentum.min_move_cents": [4, 6],
    "costs.min_net_edge_cents": [1.0, 2.0],
}


def time_bounds(db_path: str, allow_synthetic: bool = False) -> tuple[float, float] | None:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    q = "SELECT MIN(ts), MAX(ts) FROM snapshots" + ("" if allow_synthetic else " WHERE COALESCE(source,'')!='synthetic'")
    lo, hi = conn.execute(q).fetchone()
    conn.close()
    return None if lo is None else (lo, hi)


async def _run(cfg: Config, db_path: str, start: float, end: float, allow_synthetic: bool):
    metas, events = load_recorded(db_path, start, end, allow_synthetic=allow_synthetic)
    return await ReplayEngine(cfg).run(metas, events)


async def walk_forward(cfg: Config, db_path: str, grid: dict[str, list[Any]] | None = None, min_trades: int = 30,
                       record_db: Database | None = None, allow_synthetic: bool = False,
                       splits: tuple[float, float] = (0.6, 0.8)) -> dict[str, Any]:
    grid = grid or DEFAULT_GRID
    b = time_bounds(db_path, allow_synthetic)
    if b is None:
        return {"ok": False, "error": "no recorded snapshots"}
    lo, hi = b
    t_train, t_val = lo + (hi - lo) * splits[0], lo + (hi - lo) * splits[1]
    keys = list(grid)
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*(grid[k] for k in keys))]

    train = []
    for ov in combos:
        r = await _run(cfg_with(cfg, ov), db_path, lo, t_train, allow_synthetic)
        s = r.summary
        train.append({"overrides": ov, "trades": s.get("total_trades", 0), "expectancy": s.get("expectancy", 0.0),
                      "net": s.get("net_pnl", 0.0), "profit_factor": s.get("profit_factor")})
    eligible = sorted([t for t in train if t["trades"] >= min_trades], key=lambda t: t["expectancy"], reverse=True)
    validated = []
    for t in eligible[:3]:
        r = await _run(cfg_with(cfg, t["overrides"]), db_path, t_train, t_val, allow_synthetic)
        validated.append({**t, "val_trades": r.summary.get("total_trades", 0),
                          "val_expectancy": r.summary.get("expectancy", 0.0)})
    validated.sort(key=lambda t: t["val_expectancy"], reverse=True)
    chosen = validated[0]["overrides"] if validated else {}

    oos = await _run(cfg_with(cfg, chosen), db_path, t_val, hi, allow_synthetic)
    base = await _run(cfg, db_path, t_val, hi, allow_synthetic)
    contracts = oos.summary.get("contracts", 0) or 0
    oos_exp_cents = (100 * oos.summary.get("net_pnl", 0.0) / contracts) if contracts else None
    passed = bool(validated) and oos.summary.get("total_trades", 0) >= min_trades and (oos_exp_cents or 0) > 0
    result = {
        "ok": True, "window": {"start": lo, "train_end": t_train, "validation_end": t_val, "end": hi},
        "grid_size": len(combos), "train": train, "validated": validated, "chosen_overrides": chosen,
        "oos_summary": oos.summary, "oos_baseline_summary": base.summary,
        "oos_expectancy_cents": round(oos_exp_cents, 4) if oos_exp_cents is not None else None,
        "passed": passed, "synthetic_data_included": allow_synthetic,
    }
    if record_db is not None and not allow_synthetic:
        record_db.record_validation("OUT_OF_SAMPLE", passed, result)
    return result
