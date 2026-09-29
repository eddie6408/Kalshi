"""Phase A: the model has to beat the MARKET where it trades, not climatology.

On 26 September the weather strategy was $144 down while the calibration gate
reported CALIBRATED. It was beating climatology comfortably; the market was
beating it (0-6h Brier 0.127 vs the market's 0.070). These pin the gate that
asks the question that pays.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml

import pytest
from kalshibot.config import _StrictLoader
from kalshibot.journal import Journal
from kalshibot.market_skill import (AHEAD, BEHIND, EVEN, POOLED, UNPROVEN,
                                    MarketSkillConfig, MarketSkillGate,
                                    bucket_of, compute_cells, market_probability)
from kalshibot.strategies.forecast import ForecastStrategy

from test_forecast import FEES, FakeEstimator, book, cfg, mkt

ROOT = Path(__file__).resolve().parents[1]


def _rows(series, n_contracts, ours, market, outcome=0, lead=3.0, per_contract=3,
          bid=True):
    """n_contracts contracts, each re-priced `per_contract` times."""
    out = []
    for i in range(n_contracts):
        for _ in range(per_contract):
            out.append({"series": series, "ticker": f"{series}-{i}",
                        "lead_hours": lead, "our_probability": ours,
                        "settled_yes": outcome,
                        "market_yes_cents": market * 100 + (1 if bid else 0),
                        "market_yes_bid_cents": (market * 100 - 1) if bid else None})
    return out


def _vary(rows, jitter=0.02):
    """Give contracts slightly different errors so a standard error exists."""
    for i, r in enumerate(rows):
        r["our_probability"] = min(0.99, max(0.01, r["our_probability"] + (i % 5 - 2) * jitter))
    return rows


# --------------------------------------------------------------- basics
def test_lead_buckets():
    assert [bucket_of(x) for x in (0, 5.9, 6, 11.9, 12, 30, None)] == \
        ["0-6h", "0-6h", "6-12h", "6-12h", "12-24h", "24h+", None]


def test_market_probability_is_the_mid_when_there_is_one():
    assert market_probability(60, 50) == 0.55
    assert market_probability(60, None) == 0.60
    assert market_probability(None, 50) is None


def test_the_shipped_config_requires_beating_the_market():
    raw = yaml.load(open(ROOT / "config.yaml"), Loader=_StrictLoader)
    c = MarketSkillConfig.from_dict(raw["market_skill"])
    assert c.enabled and c.require == "ahead" and c.since


def test_an_unknown_require_mode_is_refused():
    with pytest.raises(ValueError):
        MarketSkillConfig.from_dict({"require": "vibes"})


# ------------------------------------------------------------- verdicts
def test_a_model_the_market_beats_is_behind():
    cells = compute_cells(_vary(_rows("KXA", 20, ours=0.40, market=0.10)))
    assert cells[("KXA", "0-6h")].verdict == BEHIND
    assert cells[(POOLED, "0-6h")].verdict == BEHIND


def test_a_model_that_beats_the_market_is_ahead():
    cells = compute_cells(_vary(_rows("KXA", 20, ours=0.05, market=0.40)))
    assert cells[("KXA", "0-6h")].verdict == AHEAD


def test_too_few_contracts_is_unproven_however_many_rows():
    """Six re-pricings of one contract are one fact, not six."""
    cells = compute_cells(_rows("KXA", 3, ours=0.40, market=0.10, per_contract=50))
    c = cells[("KXA", "0-6h")]
    assert c.rows == 150 and c.contracts == 3 and c.verdict == UNPROVEN


def test_noise_either_way_is_even():
    rows = _rows("KXA", 10, ours=0.30, market=0.20) + _rows("KXB", 10, ours=0.20, market=0.30)
    for r in rows:
        r["series"] = "KXA"
    assert compute_cells(rows)[("KXA", "0-6h")].verdict == EVEN


def test_mid_share_is_reported():
    c = compute_cells(_vary(_rows("KXA", 20, 0.4, 0.1, bid=False)))[("KXA", "0-6h")]
    assert c.mid_share == 0.0


# ------------------------------------------------------------ the gate
class _J:
    def __init__(self, conn):
        self.conn = conn


def _gate(rows, **cfg):
    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(Path(tmp) / "j.db")
        for i, r in enumerate(rows):
            j.conn.execute(
                """INSERT INTO probability_log (ts, strategy, estimator, series,
                   ticker, our_probability, settled_yes, market_yes_cents,
                   market_yes_bid_cents, lead_hours)
                   VALUES (?, 'forecast:w', 'w', ?, ?, ?, ?, ?, ?, ?)""",
                (f"2026-09-25T{i:08d}", r["series"], r["ticker"],
                 r["our_probability"], r["settled_yes"],
                 r["market_yes_cents"], r["market_yes_bid_cents"], r["lead_hours"]))
        j.conn.commit()
        g = MarketSkillGate(j, MarketSkillConfig(**cfg), estimator="w")
        g.cells(force=True)
        return g


def test_require_ahead_blocks_a_tie_and_a_loss_but_not_a_proven_edge():
    behind = _gate(_vary(_rows("KXA", 20, 0.40, 0.10)))
    assert behind.may_enter("KXA", 3.0)[0] is False
    ahead = _gate(_vary(_rows("KXA", 20, 0.05, 0.40)))
    assert ahead.may_enter("KXA", 3.0)[0] is True
    none = _gate([])
    ok, why = none.may_enter("KXA", 3.0)
    assert ok is False and "edge has to be shown" in why


def test_not_behind_only_blocks_a_clear_loss():
    rows = _rows("KXA", 10, ours=0.30, market=0.20) + _rows("KXB", 10, ours=0.20, market=0.30)
    for r in rows:
        r["series"] = "KXA"
    even = _gate(rows, require="not_behind")
    assert even.may_enter("KXA", 3.0)[0] is True
    behind = _gate(_vary(_rows("KXA", 20, 0.40, 0.10)), require="not_behind")
    assert behind.may_enter("KXA", 3.0)[0] is False


def test_a_thin_city_inherits_the_all_cities_verdict():
    rows = _vary(_rows("KXBIG", 20, 0.40, 0.10)) + _rows("KXTHIN", 2, 0.05, 0.40)
    g = _gate(rows)
    ok, why = g.may_enter("KXTHIN", 3.0)
    assert ok is False and "all cities" in why


def test_a_disabled_gate_allows_everything():
    g = _gate(_vary(_rows("KXA", 20, 0.40, 0.10)), enabled=False)
    assert g.may_enter("KXA", 3.0)[0] is True


# ------------------------------------------------- inside the strategy
class _Gate:
    def __init__(self, ok):
        self.ok = ok

    def may_enter(self, series, lead):
        return self.ok, "test verdict"


class LeadEstimator(FakeEstimator):
    def estimate(self, market):
        e = super().estimate(market)
        e.evidence = {"lead_hours": 3.0}
        return e


def test_the_strategy_skips_where_the_market_forecasts_better():
    s = ForecastStrategy(LeadEstimator(), FEES, cfg(), market_skill=_Gate(False))
    assert s.find([mkt()], {"KXFAKE-1": book()}, 500.0) == []
    assert s.skips[-1].reason == "market forecasts better here"


def test_the_strategy_trades_where_it_has_shown_an_edge():
    s = ForecastStrategy(LeadEstimator(), FEES, cfg(), market_skill=_Gate(True))
    assert len(s.find([mkt()], {"KXFAKE-1": book()}, 500.0)) == 1


# ------------------------------------ single-leg partial fills (phase A)
def test_a_single_leg_partial_fill_is_a_smaller_position_not_an_exposure():
    from test_execution import FakeExec, FakeMarket, deep, manager, opportunity
    from kalshibot.execution_manager import TradeOutcome
    ex = FakeExec(fill_plan={"T": 4})
    r = manager(ex, mk=FakeMarket({"T": deep("T")})).execute(
        opportunity(("T",), sets=10, sides=("yes",)))
    assert r.outcome is TradeOutcome.PARTIAL_FILLED
    assert not r.exposed
    assert not [s for s in ex.sent if s[1] == "sell"], "a single leg must not be sold back"
    assert any("nothing to hedge" in n for n in r.notes)


def test_the_dashboard_panel_uses_the_same_numbers_as_the_gate():
    sys.path.insert(0, str(ROOT / "scripts"))
    import dashboard
    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(Path(tmp) / "j.db")
        for i, r in enumerate(_vary(_rows("KXHIGHNY", 20, 0.40, 0.10))):
            j.conn.execute(
                """INSERT INTO probability_log (ts, strategy, estimator, series,
                   ticker, our_probability, settled_yes, market_yes_cents,
                   market_yes_bid_cents, lead_hours)
                   VALUES (?, 'forecast:weather_temperature', 'weather_temperature',
                           ?, ?, ?, ?, ?, ?, ?)""",
                (f"2099-01-01T{i:08d}", r["series"], r["ticker"], r["our_probability"],
                 r["settled_yes"], r["market_yes_cents"], r["market_yes_bid_cents"],
                 r["lead_hours"]))
        j.conn.commit()
        panel = dashboard._market_skill(j.conn)
    assert "error" not in panel, panel
    cells = {(c["series"], c["bucket"]): c for c in panel["cells"]}
    assert cells[("KXHIGHNY", "0-6h")]["verdict"] == BEHIND
    assert panel["require"] == "ahead"


def test_rows_from_a_retired_model_version_can_be_left_out():
    from kalshibot.market_skill import fetch_rows
    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(Path(tmp) / "j.db")
        for ts, model in (("2026-09-27T10:00", "nowcast"), ("2026-09-30T10:00", "nowcast"),
                          ("2026-09-27T10:00", "ensemble")):
            j.conn.execute(
                "INSERT INTO probability_log (ts, estimator, series, ticker, our_probability, "
                "market_yes_cents, lead_hours, settled_yes, model) VALUES (?,?,?,?,?,?,?,?,?)",
                (ts, "w", "KXHIGHNY", f"T-{ts}-{model}", 0.5, 40, 3.0, 1, model))
        j.conn.commit()
        assert len(fetch_rows(j.conn, "w")) == 3
        kept = fetch_rows(j.conn, "w", model_since={"nowcast": "2026-09-29T12:00"})
        assert sorted(r["ticker"] for r in kept) == ["T-2026-09-27T10:00-ensemble",
                                                      "T-2026-09-30T10:00-nowcast"]
        cfg = MarketSkillConfig.from_dict({"model_since": {"nowcast": "2026-09-29T12:00"}})
        assert cfg.model_since == {"nowcast": "2026-09-29T12:00"}
