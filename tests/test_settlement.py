"""Scoring forecasts against what actually happened.

This is the pass that makes calibration mean anything, and it has one failure
mode worse than all the others: scoring a market the WRONG way. A forecast
marked correct when it was wrong does not show up as an error - it shows up
as a strategy that has earned the right to trade.

So the reading of a resolution is deliberately conservative: anything it
cannot read with confidence is left unsettled for next time.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.journal import Journal
from kalshibot.settlement import resolution_of, settle_forecasts


class FakeMarket:
    def __init__(self, **raw):
        self.raw = raw
        self.status = raw.get("status", "")


class FakeMarkets:
    def __init__(self, markets=None, raise_on=None):
        self.markets = markets or {}
        self.raise_on = raise_on or set()
        self.asked = []

    def get_market(self, ticker):
        self.asked.append(ticker)
        if ticker in self.raise_on:
            raise RuntimeError("no such market")
        return self.markets[ticker]


def _journal(tmp):
    return Journal(Path(tmp) / "j.db")


def _forecast(j, ticker, p, **kw):
    j.log_probability(strategy="forecast", estimator="weather_temperature",
                      series="KXHIGHNY", ticker=ticker, our_probability=p, **kw)


# ----------------------- reading a resolution -----------------------
def test_an_open_market_has_no_resolution():
    assert resolution_of(FakeMarket(status="active", result="")) is None


def test_result_yes_and_no():
    assert resolution_of(FakeMarket(status="settled", result="yes")) is True
    assert resolution_of(FakeMarket(status="settled", result="no")) is False


def test_the_field_name_has_moved_before_so_all_spellings_are_read():
    # Same lesson as position_fp and the v1 order endpoint: read the result,
    # do not trust one key name.
    assert resolution_of(FakeMarket(status="finalized",
                                    settlement_result="yes")) is True
    assert resolution_of(FakeMarket(status="determined",
                                    market_result="no")) is False


def test_a_voided_market_is_never_scored():
    # Nobody was right or wrong. Scoring it would punish a good forecast.
    assert resolution_of(FakeMarket(status="settled", result="void")) is None
    assert resolution_of(FakeMarket(status="settled", result="cancelled")) is None


def test_a_final_price_stands_in_for_a_missing_result():
    assert resolution_of(FakeMarket(status="settled", settlement_value=100)) is True
    assert resolution_of(FakeMarket(status="settled", last_price=0)) is False


def test_a_final_price_that_is_not_final_is_not_a_resolution():
    # 97c is a market that is nearly sure, not a market that has paid.
    assert resolution_of(FakeMarket(status="settled", last_price=97)) is None
    assert resolution_of(FakeMarket(status="settled", last_price=4)) is None


def test_an_unreadable_price_is_ignored_rather_than_guessed():
    assert resolution_of(FakeMarket(status="settled", last_price="n/a")) is None


def test_a_settled_market_with_nothing_readable_stays_unsettled():
    assert resolution_of(FakeMarket(status="settled")) is None


# ----------------------- the pass itself -----------------------
def test_settled_forecasts_are_scored_and_open_ones_are_left_alone():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp)
        _forecast(j, "A", 0.80)
        _forecast(j, "B", 0.30)
        markets = FakeMarkets({"A": FakeMarket(status="settled", result="yes"),
                               "B": FakeMarket(status="active", result="")})
        rep = settle_forecasts(j, markets)
        assert rep.checked == 2
        assert rep.settled == 1
        assert rep.still_open == 1
        # 0.80 on a YES that happened: (0.8 - 1)^2
        assert abs(rep.scores[0] - 0.04) < 1e-9


def test_a_market_is_resolved_once_no_matter_how_many_forecasts_it_has():
    # We re-price the same market hourly. Asking the exchange once per ROW
    # would be dozens of calls for one answer.
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp)
        for i, p in enumerate((0.6, 0.65, 0.7)):
            j.log_probability(strategy="forecast", estimator="e", series="S",
                              ticker="A", our_probability=p,
                              target_date=f"d{i}")
            # UNIQUE(ticker, ts) - separate the rows in time
            j.conn.execute("UPDATE probability_log SET ts=? WHERE target_date=?",
                           (f"2026-09-1{i}T00:00:00+00:00", f"d{i}"))
        j.conn.commit()
        markets = FakeMarkets({"A": FakeMarket(status="settled", result="yes")})
        rep = settle_forecasts(j, markets)
        assert rep.settled == 3
        assert markets.asked == ["A"]


def test_running_it_twice_does_not_double_score():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp)
        _forecast(j, "A", 0.80)
        markets = FakeMarkets({"A": FakeMarket(status="settled", result="yes")})
        assert settle_forecasts(j, markets).settled == 1
        second = settle_forecasts(j, markets)
        assert second.checked == 0
        assert second.settled == 0


def test_a_market_the_exchange_cannot_return_is_left_for_next_time():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp)
        _forecast(j, "GONE", 0.5)
        rep = settle_forecasts(j, FakeMarkets(raise_on={"GONE"}))
        assert rep.errors == 1
        assert rep.settled == 0
        assert j.unsettled_probabilities()      # still there to retry


def test_a_settled_but_unreadable_market_is_counted_as_unknown():
    # This is the signal that a field name changed again. It has to be loud
    # enough to notice and harmless enough not to corrupt the record.
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp)
        _forecast(j, "A", 0.5)
        rep = settle_forecasts(j, FakeMarkets({"A": FakeMarket(status="settled")}))
        assert rep.unknown == 1
        assert rep.settled == 0


def test_a_broken_journal_does_not_take_the_scan_down_with_it():
    class Broken:
        def unsettled_probabilities(self, limit=2000):
            raise RuntimeError("database is locked")
    rep = settle_forecasts(Broken(), FakeMarkets())
    assert rep.errors == 1
    assert rep.settled == 0


# ----------------------- end to end -----------------------
def test_scores_reach_the_calibration_query():
    # The whole point: forecast -> settle -> a number the gate can read.
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp)
        answers = {}
        for i, (p, yes) in enumerate([(0.9, True), (0.8, True),
                                      (0.2, False), (0.1, False)]):
            t = f"T{i}"
            _forecast(j, t, p)
            answers[t] = FakeMarket(status="settled",
                                    result="yes" if yes else "no")
        settle_forecasts(j, FakeMarkets(answers))
        stats = j.calibration(estimator="weather_temperature")
        assert stats["n"] == 4
        # (0.1^2 + 0.2^2 + 0.2^2 + 0.1^2) / 4
        assert abs(stats["brier"] - 0.025) < 1e-9
        assert abs(stats["base_rate"] - 0.5) < 1e-9


def test_the_report_summary_says_what_happened():
    rep_text = settle_forecasts(
        type("J", (), {"unsettled_probabilities": lambda self, limit=2000: []})(),
        FakeMarkets()).summary()
    assert "checked 0" in rep_text


# ============== what the first live run actually did ==============
def test_a_closed_market_is_not_a_settled_one():
    """On Kalshi a market closes when trading stops and settles later.

    The first live run logged 19 "the result is unreadable" warnings every
    single pass, for markets that were simply waiting their turn - and the one
    genuine version of that warning, the one meaning a field name changed
    again, would have been lost among them.
    """
    m = FakeMarket(status="closed", result="")
    assert resolution_of(m) is None

    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp)
        _forecast(j, "A", 0.5)
        rep = settle_forecasts(j, FakeMarkets({"A": m}))
        assert rep.pending == 1
        assert rep.unknown == 0, "a closed market must not be reported as broken"
        assert "awaiting settlement" in rep.summary()


def test_a_settled_market_with_an_empty_result_is_still_the_loud_case():
    m = FakeMarket(status="settled", result="")
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp)
        _forecast(j, "A", 0.5)
        rep = settle_forecasts(j, FakeMarkets({"A": m}))
        assert rep.unknown == 1
        assert "UNREADABLE" in rep.summary()


def test_the_dollar_spelling_of_the_final_price_is_read():
    # Same fixed-point migration as orders ("0.4300") and positions
    # (position_fp). A live payload carries last_price_dollars, not
    # last_price - reading only the old name scores nothing, silently.
    assert resolution_of(FakeMarket(status="settled",
                                    last_price_dollars="1.0000")) is True
    assert resolution_of(FakeMarket(status="settled",
                                    last_price_dollars="0.0000")) is False
    assert resolution_of(FakeMarket(status="settled",
                                    last_price_dollars="0.9700")) is None


def test_expiration_value_is_read_as_cents():
    assert resolution_of(FakeMarket(status="settled", expiration_value=100)) is True
    assert resolution_of(FakeMarket(status="settled", expiration_value=0)) is False


# ---- 2026-10-13: 12 days of forecasts never scored ----------------------
def test_far_future_markets_cannot_starve_ones_that_have_resolved():
    """The bug: the pass read the 2,000 oldest unsettled ROWS. From 1 Oct
    those were all inflation forecasts for releases weeks away, so no
    weather forecast after 30 Sep was ever scored."""
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp)
        for i in range(2500):          # old rows that cannot resolve yet
            _forecast(j, f"CPI-{i % 5}", 0.4, target_date="2026-11-01")
        _forecast(j, "WX-NEW", 0.9, target_date="2026-10-05")
        markets = FakeMarkets({**{f"CPI-{k}": FakeMarket(status="active", result="")
                                  for k in range(5)},
                               "WX-NEW": FakeMarket(status="settled", result="yes")})
        rep = settle_forecasts(j, markets)
        assert rep.settled == 1
        assert markets.asked[0] == "WX-NEW"        # finished day first
        row = j.conn.execute("SELECT settled_yes, brier FROM probability_log "
                             "WHERE ticker='WX-NEW'").fetchone()
        assert row[0] == 1 and abs(row[1] - 0.01) < 1e-9


def test_every_row_of_a_resolved_market_settles_at_once():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp)
        for p in (0.2, 0.5, 0.7):
            _forecast(j, "A", p, target_date="2026-10-01")
        rep = settle_forecasts(j, FakeMarkets({"A": FakeMarket(status="settled", result="no")}))
        assert rep.settled == 3
        assert j.conn.execute("SELECT COUNT(*) FROM probability_log WHERE settled_yes IS NULL"
                              ).fetchone()[0] == 0


def test_a_market_still_open_is_not_asked_again_until_its_recheck_is_due():
    with tempfile.TemporaryDirectory() as tmp:
        j = _journal(tmp)
        _forecast(j, "OPEN", 0.5, target_date="2099-01-01")
        markets = FakeMarkets({"OPEN": FakeMarket(status="active", result="")})
        recheck, t = {}, [1000.0]
        settle_forecasts(j, markets, recheck=recheck, clock=lambda: t[0])
        settle_forecasts(j, markets, recheck=recheck, clock=lambda: t[0])
        assert markets.asked == ["OPEN"]            # deferred on the second pass
        t[0] += 6 * 3600 + 1
        settle_forecasts(j, markets, recheck=recheck, clock=lambda: t[0])
        assert markets.asked == ["OPEN", "OPEN"]
