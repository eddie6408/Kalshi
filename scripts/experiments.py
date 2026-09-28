"""Replay experiments on the recorded tape (run from the repo root):

    nice -n 19 .venv/bin/python scripts/experiments.py [--train A B] [--valid C D]

Every variant is replayed on the TRAIN window; the best few (by net
expectancy per trade, min 30 trades) plus the baseline are replayed on the
VALIDATION window, which none of them was chosen on. Results go to
backtest_runs (kind='experiment') and data/experiments/<stamp>/summary.md.

The true out-of-sample TEST is forward paper trading after the chosen
variant is deployed - one night of data is not enough to call anything final.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lvt.config import load_config            # noqa: E402
from lvt.db import Database, open_state_db     # noqa: E402
from lvt.replay import run_replay              # noqa: E402

ALL = ("DIP", "REVERSAL", "MOMENTUM", "DOWNTREND")


def per_strategy(**kv) -> dict:
    return {"strategies": {s: dict(kv) for s in ALL}}


def merge(*ds: dict) -> dict:
    out: dict = {}
    for d in ds:
        for k, v in d.items():
            out[k] = merge(out.get(k, {}), v) if isinstance(v, dict) else v
    return out


VOLSTOP = per_strategy(stop_vol_mult=2.5)
CAP6 = per_strategy(max_target_distance_cents=6.0)
BIG = merge({"strategies": {"DIP": {"min_drop_cents": 12.0}, "REVERSAL": {"min_drop_cents": 12.0},
                            "MOMENTUM": {"min_rise_cents": 8.0, "min_velocity": 3.0},
                            "DOWNTREND": {"min_rise_cents": 8.0, "min_velocity": 3.0}}})
COOL = {"signals": {"market_cooldown_after_stop_s": 1800}}
TAKER = {"execution": {"entry_style": "taker", "exit_style": "taker"}, "maker": {"take_profit": False}}
MAKER = {"execution": {"entry_style": "maker", "exit_style": "maker"}, "maker": {"take_profit": True}}

VARIANTS: dict[str, dict] = {
    "baseline": {},
    "volstop_2.0": per_strategy(stop_vol_mult=2.0),
    "volstop_2.5": VOLSTOP,
    "volstop_3.5": per_strategy(stop_vol_mult=3.5),
    "target_cap_4": per_strategy(max_target_distance_cents=4.0),
    "target_cap_6": CAP6,
    "bigger_moves": BIG,
    "cooldown_after_stop_30m": COOL,
    "volstop_2.5+cap6": merge(VOLSTOP, CAP6),
    "volstop_2.5+cap6+cool": merge(VOLSTOP, CAP6, COOL),
    "bigger+volstop_2.5+cap6+cool": merge(BIG, VOLSTOP, CAP6, COOL),
    "bigger+cool": merge(BIG, COOL),
    # maker execution on the same windows. The 2026-09-27 tape has no trade
    # prints, so maker fills there use the conservative volume fallback.
    "maker": MAKER,
    "maker+volstop_2.5+cap6+cool": merge(MAKER, VOLSTOP, CAP6, COOL),
}


def ts(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", nargs=2, default=["2026-09-27T17:12", "2026-09-27T20:29"])
    ap.add_argument("--valid", nargs=2, default=["2026-09-27T20:29", "2026-09-28T04:55"])
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--only", nargs="*", default=None)
    a = ap.parse_args()

    base = load_config(ROOT)
    tape = Database(base.path(base.app.tape_db), readonly=True)
    state = open_state_db(base.path(base.app.state_db))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    outdir = base.path(base.app.data_dir) / "experiments" / stamp
    outdir.mkdir(parents=True, exist_ok=True)
    bt = outdir / "runs"
    log = open(outdir / "progress.log", "a")

    def say(msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        log.write(line + "\n")
        log.flush()

    names = a.only or list(VARIANTS)
    results: dict[str, dict] = {}
    for name in names:
        cfg = load_config(ROOT, overrides=merge(TAKER, VARIANTS[name]))
        s = run_replay(cfg, tape, ts(a.train[0]), ts(a.train[1]), bt, "experiment", f"train:{name}", state,
                       VARIANTS[name], label=False)
        results[name] = {"train": s["metrics"]}
        m = s["metrics"]
        say(f"TRAIN {name:<32} trades {m.get('trades', 0):>4} exp ${m.get('expectancy_usd', 0):+.3f} "
            f"net ${m.get('net_pnl_usd', 0):+.2f} win {m.get('win_rate', 0):.0%} ({s['metrics'].get('wall_s')}s)")

    ranked = sorted((n for n in names if results[n]["train"].get("trades", 0) >= 30),
                    key=lambda n: -results[n]["train"].get("expectancy_usd", -1e9))
    chosen = list(dict.fromkeys(["baseline"] + ranked[: a.top])) if "baseline" in names else ranked[: a.top]
    for name in chosen:
        cfg = load_config(ROOT, overrides=merge(TAKER, VARIANTS[name]))
        s = run_replay(cfg, tape, ts(a.valid[0]), ts(a.valid[1]), bt, "experiment", f"valid:{name}", state,
                       VARIANTS[name], label=False)
        results[name]["valid"] = s["metrics"]
        m = s["metrics"]
        say(f"VALID {name:<32} trades {m.get('trades', 0):>4} exp ${m.get('expectancy_usd', 0):+.3f} "
            f"net ${m.get('net_pnl_usd', 0):+.2f} win {m.get('win_rate', 0):.0%}")

    def row(n, seg):
        m = results[n].get(seg)
        if not m:
            return "| – | – | – | – | – |"
        return (f"| {m.get('trades', 0)} | {m.get('win_rate', 0):.0%} | ${m.get('expectancy_usd', 0):+.3f} "
                f"| ${m.get('net_pnl_usd', 0):+.2f} | ${m.get('fees_usd', 0):.2f} |")

    lines = [f"# Replay experiments {stamp}", "",
             f"TRAIN {a.train[0]} -> {a.train[1]} UTC; VALIDATION {a.valid[0]} -> {a.valid[1]} UTC.",
             "Replay is ~0.7 $/trade more optimistic than live paper (2026-09-28 fidelity check). Replays are",
             "deterministic (seeded ids); maker rows on this tape use the volume fallback (no trade prints).", "",
             "| variant | train trades | win | exp/trade | net | fees | valid trades | win | exp/trade | net | fees |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for n in names:
        lines.append(f"| {n} " + row(n, "train") + row(n, "valid")[1:])
    (outdir / "summary.md").write_text("\n".join(lines) + "\n")
    (outdir / "results.json").write_text(json.dumps({"variants": {n: VARIANTS[n] for n in names},
                                                     "results": results}, indent=1, default=str))
    say(f"DONE -> {outdir / 'summary.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
