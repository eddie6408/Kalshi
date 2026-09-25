"""Measuring what an order would really cost.

Demo liquidity is synthetic, so every execution number this bot has produced
describes a fiction. This compares the same market at the same instant on
both books. The rules worth pinning are the ones that stop a measurement
quietly lying: never compare against a book that is not there, and never
record a half-comparison as a whole one.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.depth import DepthBook, Level
from kalshibot.execution_probe import compare, summarise
from kalshibot.journal import Journal


def book(yes_bids, no_bids):
    return DepthBook("T-1", [Level(p, q) for p, q in yes_bids],
                     [Level(p, q) for p, q in no_bids])


#: A synthetic demo book: one enormous rung, so nothing ever slips.
DEEP = book([(40, 5000)], [(58, 5000)])
#: A real one: a thin top rung and worse prices behind it.
THIN = book([(38, 5), (37, 40)], [(55, 4), (52, 30), (48, 200)])


def test_a_deep_book_shows_no_slippage_and_a_thin_one_does():
    r = compare("T-1", DEEP, THIN, size=25, side="yes", action="buy")
    assert r["demo_slippage_cents"] == 0            # the illusion
    assert r["live_slippage_cents"] > 0             # the reality
    assert r["cost_gap_cents"] > 0


def test_the_cost_gap_is_live_minus_demo():
    r = compare("T-1", DEEP, THIN, size=25, side="yes", action="buy")
    assert abs(r["cost_gap_cents"]
               - (r["live_vwap_cents"] - r["demo_vwap_cents"])) < 1e-9


def test_a_book_that_cannot_fill_the_size_is_recorded_as_incomplete():
    tiny = book([(38, 1)], [(55, 3)])
    r = compare("T-1", DEEP, tiny, size=25, side="yes", action="buy")
    assert r["demo_complete"] is True
    assert r["live_complete"] is False
    assert r["live_filled"] < 25


def test_a_missing_book_is_not_compared():
    """A half-comparison recorded as a whole one is how a measurement starts
    lying. Both sides or nothing."""
    assert compare("T-1", DEEP, None, size=25) is None
    assert compare("T-1", None, THIN, size=25) is None


def test_an_empty_side_is_not_compared():
    one_sided = book([(38, 10)], [])        # no offers at all
    assert compare("T-1", DEEP, one_sided, size=25,
                   side="yes", action="buy") is None


def test_both_sides_of_the_market_can_be_probed():
    y = compare("T-1", DEEP, THIN, size=25, side="yes", action="buy")
    n = compare("T-1", DEEP, THIN, size=25, side="no", action="buy")
    assert y["side"] == "yes" and n["side"] == "no"
    assert y["demo_best_cents"] != n["demo_best_cents"]


def test_spreads_are_reported_for_both_books():
    r = compare("T-1", DEEP, THIN, size=25, side="yes", action="buy")
    assert r["demo_spread_cents"] == 2          # 40 bid / 42 ask
    assert r["live_spread_cents"] == 7          # 38 bid / 45 ask


# ---------------- the summary ----------------
def test_the_summary_reports_the_fill_rate():
    tiny = book([(38, 1)], [(55, 2)])
    rows = [compare("T-1", DEEP, THIN, size=25),
            compare("T-2", DEEP, tiny, size=25)]
    su = summarise([r for r in rows if r])
    assert su["n"] == 2
    assert su["live_could_not_fill"] == 1
    assert su["live_fill_rate"] == 0.5


def test_an_empty_sample_is_not_a_crash():
    assert summarise([])["n"] == 0


# ---------------- it round-trips through the journal ----------------
def test_a_comparison_survives_the_journal():
    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(Path(tmp) / "j.db")
        r = compare("T-1", DEEP, THIN, size=25, side="yes", action="buy")
        j.log_book_quality(r)
        back = j.book_quality()
        assert len(back) == 1
        assert back[0]["ticker"] == "T-1"
        assert abs(back[0]["cost_gap_cents"] - r["cost_gap_cents"]) < 1e-9
        assert back[0]["live_complete"] == 1     # bool stored as int


def test_the_probe_table_starts_empty_rather_than_missing():
    with tempfile.TemporaryDirectory() as tmp:
        assert Journal(Path(tmp) / "j.db").book_quality() == []
