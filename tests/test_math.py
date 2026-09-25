"""Verification of the money math. Run: python -m pytest tests -q"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.edge import contracts_from_kelly, evaluate, ev_usd, kelly_fraction
from kalshibot.fees import FeeModel, taker_fee_usd, maker_fee_usd


# ----------------------------- fees -----------------------------------
def test_fee_peaks_at_fifty_cents():
    f50 = taker_fee_usd(50, 1)
    assert f50 == 0.02, f50            # ceil(0.07*0.5*0.5)=ceil($0.0175)=$0.02
    assert taker_fee_usd(50, 100) == 1.75   # ceil(0.07*100*0.25)=$1.75


def test_fee_is_symmetric_around_fifty():
    assert taker_fee_usd(20, 500) == taker_fee_usd(80, 500)


def test_fee_shrinks_at_the_extremes():
    assert taker_fee_usd(5, 1000) < taker_fee_usd(50, 1000)
    assert taker_fee_usd(95, 1000) < taker_fee_usd(50, 1000)


def test_fee_rounds_up_to_the_cent():
    # 0.07 * 1 * 0.3 * 0.7 = $0.0147 -> rounds UP to $0.02
    assert taker_fee_usd(30, 1) == 0.02


def test_zero_contracts_is_free():
    assert taker_fee_usd(50, 0) == 0.0
    assert maker_fee_usd(50, 0) == 0.0


def test_safety_multiplier_inflates_the_estimate():
    plain = FeeModel({"safety_multiplier": 1.0})
    padded = FeeModel({"safety_multiplier": 1.10})
    assert padded.entry_fee_usd(50, 100) > plain.entry_fee_usd(50, 100)


# ----------------------------- edge -----------------------------------
def test_picks_the_cheaper_side():
    fees = FeeModel({"safety_multiplier": 1.0})
    # We think YES is 30%. YES costs 60c (terrible), NO costs 42c.
    # Buying NO wins 70% of the time for 42c -> big edge.
    r = evaluate(0.30, yes_ask_cents=60, no_ask_cents=42, fees=fees)
    assert r.side == "no"
    assert r.win_probability == 0.70
    assert r.gross_edge == 0.70 - 0.42


def test_no_edge_when_market_agrees():
    fees = FeeModel({"safety_multiplier": 1.0})
    r = evaluate(0.50, 50, 50, fees)
    assert r.edge_after_fees < 0          # fees make a fair price a losing trade


def test_edge_is_net_of_fees():
    fees = FeeModel({"safety_multiplier": 1.0})
    r = evaluate(0.70, yes_ask_cents=60, no_ask_cents=41, fees=fees)
    assert r.side == "yes"
    assert math.isclose(r.gross_edge, 0.10, abs_tol=1e-9)
    assert r.edge_after_fees < r.gross_edge


def test_one_sided_market_is_untradable():
    fees = FeeModel()
    r = evaluate(0.7, 0, 0, fees)
    assert r.tradable is False and r.side == "none"


# ----------------------------- kelly ----------------------------------
def test_kelly_zero_when_no_edge():
    assert kelly_fraction(0.50, 50) == 0.0
    assert kelly_fraction(0.40, 50) == 0.0


def test_kelly_known_value():
    # q=0.60, p=0.50 -> f* = (0.60-0.50)/(1-0.50) = 0.20
    assert math.isclose(kelly_fraction(0.60, 50), 0.20, abs_tol=1e-9)


def test_kelly_bigger_edge_bigger_bet():
    assert kelly_fraction(0.80, 50) > kelly_fraction(0.60, 50)


def test_quarter_kelly_sizing():
    # bankroll 1000, q=0.60 at 50c, quarter kelly -> 0.20*0.25 = 5% = $50 -> 100 contracts
    n = contracts_from_kelly(0.60, 50, 1000.0, 0.25, confidence=1.0)
    assert n == 100


def test_low_confidence_shrinks_the_bet():
    full = contracts_from_kelly(0.60, 50, 1000.0, 0.25, confidence=1.0)
    half = contracts_from_kelly(0.60, 50, 1000.0, 0.25, confidence=0.5)
    assert half == full // 2


def test_negative_edge_sizes_to_zero():
    assert contracts_from_kelly(0.40, 50, 1000.0, 0.25) == 0


# ----------------------------- ev -------------------------------------
def test_ev_matches_hand_calculation():
    fees = FeeModel({"safety_multiplier": 1.0})
    # 100 contracts of YES at 50c, we think 60%.
    # gross = 100 * (0.60 - 0.50) = $10.00 ; fee = $1.75 ; EV = $8.25
    assert math.isclose(ev_usd(0.60, 50, 100, fees), 8.25, abs_tol=1e-9)


def test_ev_negative_when_we_are_wrong_side():
    fees = FeeModel()
    assert ev_usd(0.40, 50, 100, fees) < 0


# ------------------- orderbook sides (regression) ---------------------
# A bug here is silent and expensive: you can enter a position and then
# never exit it, because selling hits a completely different side of the
# book from buying.
from kalshibot.models import Orderbook, OrderbookLevel   # noqa: E402


def _book():
    # Resting YES bids at 60c/59c, resting NO bids at 38c/37c.
    # A NO bid at 38c is the same thing as a YES offer at 62c.
    return Orderbook("T",
                     yes=[OrderbookLevel(60, 100), OrderbookLevel(59, 200)],
                     no=[OrderbookLevel(38, 150), OrderbookLevel(37, 300)])


def test_buying_yes_consumes_the_no_bids():
    assert _book().yes_ask_depth(62) == 150            # only the 38c NO bid
    assert _book().yes_ask_depth(63) == 450            # both NO levels


def test_selling_yes_consumes_the_yes_bids():
    assert _book().yes_bid_depth(60) == 100
    assert _book().yes_bid_depth(59) == 300


def test_buy_and_sell_depth_are_different_sides():
    b = _book()
    assert b.depth_for("yes", "buy", 62) != b.depth_for("yes", "sell", 60)


def test_depth_for_matches_the_explicit_methods():
    b = _book()
    assert b.depth_for("yes", "buy", 62) == b.yes_ask_depth(62)
    assert b.depth_for("no", "buy", 41) == b.no_ask_depth(41)
    assert b.depth_for("yes", "sell", 59) == b.yes_bid_depth(59)
    assert b.depth_for("no", "sell", 37) == b.no_bid_depth(37)


def test_a_position_we_can_enter_we_can_also_exit():
    """The exact failure the simulation caught: entry found liquidity,
    exit found none, and the position was stuck forever."""
    b = _book()
    assert b.depth_for("yes", "buy", 62) > 0
    assert b.depth_for("yes", "sell", 60) > 0


# ------------------- journal on a bad filesystem -----------------------
from kalshibot.journal import Journal, JournalError   # noqa: E402


def test_journal_gives_a_useful_error_on_a_locking_filesystem():
    """SQLite on iCloud/Dropbox/a network share fails with a bare
    'disk I/O error'. That message sends people down the wrong path."""
    import sqlite3 as _sq
    import tempfile
    from pathlib import Path as _P
    from unittest import mock

    with tempfile.TemporaryDirectory() as tmp:
        real = _sq.connect

        class _Broken:
            row_factory = None

            def executescript(self, _):
                raise _sq.OperationalError("disk I/O error")

        with mock.patch("sqlite3.connect", return_value=_Broken()):
            try:
                Journal(_P(tmp) / "j.db")
            except JournalError as exc:
                msg = str(exc)
                assert "network or cloud-synced drive" in msg
                assert "config.yaml" in msg
                return
            raise AssertionError("expected JournalError")


# ------------------- key path resolution -------------------------------
def test_relative_key_path_resolves_against_the_project_folder():
    """So the same .env works on a Mac and on a Linux VPS."""
    import os
    import tempfile
    from pathlib import Path as _P
    from unittest import mock
    import yaml as _yaml

    from kalshibot.config import load_settings

    with tempfile.TemporaryDirectory() as tmp:
        root = _P(tmp)
        (root / "keys").mkdir()
        (root / "keys" / "k.pem").write_text("x")
        _yaml.safe_dump({"execution": {"mode": "paper"},
                         "risk": {"max_cost_per_trade_usd": 1,
                                  "max_exposure_per_market_usd": 2,
                                  "max_total_open_exposure_usd": 3,
                                  "kelly_fraction": 0.25}},
                        open(root / "config.yaml", "w"))
        env = {"KALSHI_KEY_ID": "abc",
               "KALSHI_PRIVATE_KEY_PATH": "keys/k.pem",
               "KALSHI_ENV": "demo"}
        with mock.patch.dict(os.environ, env, clear=False):
            s = load_settings(str(root / "config.yaml"))
        assert s.private_key_path == (root / "keys" / "k.pem").resolve()
        assert s.private_key_path.exists()


def test_absolute_key_path_is_left_alone():
    import os
    import tempfile
    from pathlib import Path as _P
    from unittest import mock
    import yaml as _yaml

    from kalshibot.config import load_settings

    with tempfile.TemporaryDirectory() as tmp:
        root = _P(tmp)
        key = root / "abs.pem"
        key.write_text("x")
        _yaml.safe_dump({"execution": {"mode": "paper"},
                         "risk": {"max_cost_per_trade_usd": 1,
                                  "max_exposure_per_market_usd": 2,
                                  "max_total_open_exposure_usd": 3,
                                  "kelly_fraction": 0.25}},
                        open(root / "config.yaml", "w"))
        env = {"KALSHI_KEY_ID": "abc", "KALSHI_PRIVATE_KEY_PATH": str(key),
               "KALSHI_ENV": "demo"}
        with mock.patch.dict(os.environ, env, clear=False):
            s = load_settings(str(root / "config.yaml"))
        assert s.private_key_path == key
