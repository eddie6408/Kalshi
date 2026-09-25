"""Run the simulator end to end as a real subprocess.

Unit tests do not catch a NameError in a rarely-taken print statement. This
does, because it actually runs the program the way you will.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "scripts" / "simulate.py"


def _run(*extra, timeout=180):
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [sys.executable, str(SIM), "--quiet",
               "--db", str(Path(tmp) / "s.db"), *extra]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r


def test_simulation_completes_without_crashing():
    r = _run("--days", "3", "--seed", "7")
    assert "Traceback" not in r.stdout + r.stderr, (r.stdout + r.stderr)[-2000:]
    assert r.returncode == 0
    assert "RESULT" in r.stdout


def test_the_circuit_breaker_path_does_not_crash():
    """Seed 2 trips the daily loss limit - which is exactly the code path
    that shipped broken once."""
    r = _run("--days", "10", "--seed", "2")
    assert "Traceback" not in r.stdout + r.stderr, (r.stdout + r.stderr)[-2000:]
    assert r.returncode == 0


def test_ignore_breakers_runs_the_full_length():
    r = _run("--days", "6", "--seed", "2", "--ignore-breakers")
    assert "Traceback" not in r.stdout + r.stderr, (r.stdout + r.stderr)[-2000:]
    assert "DAY 6/6" in r.stdout, r.stdout[-1500:]


def test_several_seeds_all_survive():
    for seed in (1, 3, 5, 11, 23):
        r = _run("--days", "4", "--seed", str(seed))
        combined = r.stdout + r.stderr
        assert "Traceback" not in combined, f"seed {seed}:\n{combined[-1500:]}"
        assert r.returncode == 0, f"seed {seed} exited {r.returncode}"


def test_entries_and_closed_positions_reconcile():
    """Every position opened must eventually be accounted for."""
    import sqlite3
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "s.db"
        subprocess.run([sys.executable, str(SIM), "--quiet", "--days", "6",
                        "--seed", "7", "--db", str(db)],
                       capture_output=True, text=True, timeout=180)
        conn = sqlite3.connect(db)
        opened = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE action='buy'").fetchone()[0]
        closed = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE action='buy' AND status='closed'"
        ).fetchone()[0]
        assert opened > 0, "simulation never traded"
        assert closed == opened, f"{opened - closed} entries never closed"


def _pnl(out: str) -> str:
    line = [l for l in out.splitlines() if "net P/L" in l]
    return line[0] if line else ""


def test_same_seed_reproduces_the_same_run():
    a = _run("--days", "4", "--seed", "99")
    b = _run("--days", "4", "--seed", "99")
    assert _pnl(a.stdout) == _pnl(b.stdout), "seeded runs must be identical"
    assert _pnl(a.stdout), a.stdout[-800:]


def test_no_seed_gives_a_different_run_each_time():
    """Regression: the simulator used a fixed default seed, so every demo
    replayed the identical week of weather."""
    results = {_pnl(_run("--days", "4").stdout) for _ in range(4)}
    assert len(results) > 1, f"all runs identical: {results}"


def test_the_seed_is_printed_so_a_run_can_be_replayed():
    out = _run("--days", "3").stdout
    assert "seed" in out
    assert "--seed" in out, "must tell the user how to reproduce the run"
