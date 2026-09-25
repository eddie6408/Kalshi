"""The calibration math is the number that decides whether to risk money.
If it is wrong, everything downstream is wrong in a way that looks fine.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.journal import Journal

ROOT = Path(__file__).resolve().parents[1]


def _journal(tmp):
    return Journal(Path(tmp) / "j.db")


def _report(db) -> str:
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "report.py"),
                        "--db", str(db)], capture_output=True, text=True)
    return r.stdout


def _bet(j, i, side, p_yes, side_wins):
    """One settled bet. side_wins says whether the side we bought paid out."""
    j.log_trade(decision_id=None, mode="paper", ticker=f"T{i}", side=side,
                action="buy", contracts=10, price_cents=50, fee_usd=0.18,
                our_probability=p_yes)
    if side == "yes":
        yes_won = side_wins
    else:
        yes_won = not side_wins
    j.close_open_trades(f"T{i}", 100 if yes_won else 0, 0 if yes_won else 100,
                        outcome="yes" if yes_won else "no")


def test_a_perfectly_calibrated_forecaster_shows_no_gap():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp)
        for i in range(100):
            _bet(j, i, "yes", 0.70, side_wins=(i < 70))
        out = _report(j.path)
        assert "0.6-0.8" in out
        line = [l for l in out.splitlines() if l.strip().startswith("0.6-0.8")][0]
        assert "0.700" in line and "-0.000" in line or "+0.000" in line, line


def test_buying_no_is_scored_as_the_no_side_probability():
    """The bug this replaced: a NO bet at P(YES)=0.10 was filed in the
    0.0-0.2 bucket and then 'won' 90% of the time, which read as wild
    overperformance instead of a correctly priced 90% bet."""
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp)
        for i in range(50):
            _bet(j, i, "no", p_yes=0.10, side_wins=(i < 45))   # 90% of the time
        out = _report(j.path)
        assert "0.8-1.0" in out, out
        assert "0.0-0.2" not in out.split("CALIBRATION")[1].split("Brier")[0]


def test_an_overconfident_model_is_flagged():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp)
        # Says 90%, right only half the time. That is a broken model.
        for i in range(60):
            _bet(j, i, "yes", 0.90, side_wins=(i < 30))
        out = _report(j.path)
        assert "<-- off" in out, out


def test_brier_score_matches_the_textbook_value():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp)
        for i in range(100):
            _bet(j, i, "yes", 0.80, side_wins=(i < 80))
        out = _report(j.path)
        # A perfectly calibrated 80% forecaster scores 0.8*0.2 = 0.16
        line = [l for l in out.splitlines() if "Brier score" in l][0]
        value = float(line.split(":")[1].split()[0])
        assert abs(value - 0.16) < 0.005, line


def test_small_samples_carry_a_warning():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp)
        for i in range(12):
            _bet(j, i, "yes", 0.60, side_wins=(i < 7))
        assert "at least 50" in _report(j.path)


def test_open_trades_are_excluded_from_calibration():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp)
        for i in range(60):
            _bet(j, i, "yes", 0.70, side_wins=(i < 42))
        # an open position with a wild probability must not move the table
        j.log_trade(decision_id=None, mode="paper", ticker="OPEN", side="yes",
                    action="buy", contracts=10, price_cents=50, fee_usd=0.18,
                    our_probability=0.99)
        out = _report(j.path)
        line = [l for l in out.splitlines() if l.strip().startswith("0.6-0.8")][0]
        assert line.split()[1] == "60", line
