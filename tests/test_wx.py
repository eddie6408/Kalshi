"""WX-1..7: weather taxonomy, point-in-time data, calibration, extended and
hourly models, shadow paper, eligibility.

Pinned: each market's settlement source is read from ITS OWN rules (TWC,
Synoptic/Kalshi index, AccuWeather, NWS all occur live); lows are not read
as highs from the boilerplate; rules changes are versioned, never
overwritten; the quota guard refuses past the budget; preliminary and final
observations are both kept; the Monte Carlo keeps temporal dependence and the
contracts' own rounding / strictness / missing-hour rules; observed days are
fixed; uncertain models do not paper-trade; eligibility is a label; and the
package cannot reach an order path.
"""
import json
import math
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pytest

from kalshibot.wx import calibration as cal
from kalshibot.wx import taxonomy as tx
from kalshibot.wx.eligibility import evaluate
from kalshibot.wx.extended import (DayInput, ExtendedEngine, ar1_paths, longest_run,
                                   round_half_up, simulate)
from kalshibot.wx.hourly import sd_for_lead, target_utc
from kalshibot.wx.paper import PaperBook, PaperConfig
from kalshibot.wx.pit import local_day_aggregates, om_weight, parse_twc_day
from kalshibot.wx.quota import QuotaGuard
from kalshibot.wx.registry import WeatherRegistry
from kalshibot.wx.store import WxStore

ROOT = Path(__file__).resolve().parents[1]
FIX = json.loads((ROOT / "tests" / "fixtures" / "wx_rules_sample.json").read_text())


def _market(series):
    return next(m for m in FIX["markets"] if m["series_ticker"] == series)


def _c(series):
    return tx.classify(_market(series), FIX["series"].get(series))


def _store():
    d = tempfile.mkdtemp()
    return WxStore(Path(d) / "wx.db")


# ------------------------------------------------------------------ WX-1
def test_daily_high_and_low_are_told_apart_and_settle_on_the_named_source():
    hi, lo = _c("KXHIGHNY"), _c("KXLOWTNYC")
    assert hi.cls == "DAILY_HIGH" and hi.variable == "temp_max"
    # the secondary boilerplate says "maximum/minimum" on BOTH - must not
    # turn every low into a high
    assert lo.cls == "DAILY_LOW" and lo.variable == "temp_min"
    for c in (hi, lo):
        assert c.source == "TWC" and c.cli == "NYC" and c.window_start == c.window_end
        assert c.parse_ok


def test_each_hourly_market_uses_the_source_its_own_rules_name():
    syn = _c("KXTEMPNYCHS")
    acc = tx.classify(_market("KXTEMPNYCH"), FIX["series"]["KXTEMPNYCH"])
    assert syn.cls == acc.cls == "HOURLY_TEMP"
    assert syn.source == "SYNOPTIC_KALSHI_INDEX" and syn.variable == "temp_index"
    # the SERIES metadata says The Weather Company; the market rules say
    # AccuWeather, and the rules win
    assert acc.source == "ACCUWEATHER"
    assert syn.at_time == "7 AM EDT" and acc.at_time == "3 PM EDT"


def test_run_length_rule_is_read_exactly():
    c = _c("KXAVGTKPHX")
    assert c.cls == "RUN_LENGTH" and c.variable == "temp_mean_hourly"
    assert c.run_k == 2 and c.operator == ">" and c.threshold == 90.0
    assert c.rounding == "nearest_whole_degree" and c.min_hours == 18
    assert c.revision_policy == "as_published_at_expiration"
    assert (c.window_start, c.window_end) == ("2026-09-28", "2026-10-04") and c.station == "KPHX"


def test_monthly_hourly_minimum_and_degree_days_and_precip_and_other():
    sjc = _c("KXSJCLOWT")
    assert sjc.cls == "MONTHLY_MIN" and sjc.aggregation == "ANY_HOURLY"
    assert sjc.operator == "<" and sjc.threshold == 40.0 and sjc.station == "KSJC"
    hdd = _c("KXHDDCVG")
    assert hdd.cls == "SEASONAL" and hdd.variable == "hdd" and hdd.station == "KCVG"
    assert hdd.operator == "<" and hdd.threshold == 1250.0 and hdd.window_days == 92
    assert _c("KXRAINNYCW").cls == "PRECIPITATION"
    assert _c("KXSTOWEOPEN").cls == "OTHER"          # ski opening, not precipitation
    assert _c("KXTXURI").cls == "CLIMATE_ONE_OFF"


def test_statuses():
    live = {"KXHIGHNY": "NYC"}
    assert tx.initial_status(_c("KXHIGHNY"), live)[0] == "PAPER_TRADING"
    assert tx.initial_status(_c("KXHIGHNY"), {"KXHIGHNY": "LGA"})[0] == "DISABLED"
    assert tx.initial_status(_c("KXLOWTNYC"), live)[0] == "CLASSIFIED"
    assert tx.initial_status(_c("KXAVGTKPHX"), live)[0] == "RESEARCH_ONLY"
    assert tx.initial_status(_c("KXSTOWEOPEN"), live)[0] == "DISABLED"
    m = dict(_market("KXAVGTKPHX"), rules_primary="If the average temperature is above 90, then the market resolves to Yes.",
             rules_secondary="")
    c = tx.classify(m, {"settlement_sources": []})
    st, why = tx.initial_status(c, live)
    assert st == "RESEARCH_ONLY" and not c.parse_ok and "no settlement source" in why


class FakeReader:
    def __init__(self, markets, series):
        self.markets, self.series = markets, series

    def series_list(self, category):
        return list(self.series.values())

    def events_page(self, status="open", cursor=None, series_ticker=None, limit=200):
        evs = {}
        for m in self.markets:
            evs.setdefault(m["event_ticker"], {"event_ticker": m["event_ticker"],
                                               "series_ticker": m["series_ticker"], "markets": []})
            evs[m["event_ticker"]]["markets"].append(m)
        return {"events": list(evs.values()), "cursor": ""}

    def markets_by_ticker(self, tickers):
        return [m for m in self.markets if m["ticker"] in tickers]


def test_registry_versions_rules_never_overwrites_and_blocks_a_mismatched_series():
    s = _store()
    ms = [dict(m) for m in FIX["markets"]]
    reader = FakeReader(ms, FIX["series"])
    reg = WeatherRegistry(s, reader, QuotaGuard(s, sleep=lambda x: None), {"KXHIGHNY": "NYC"})
    out = reg.refresh()
    assert out["ok"] and out["markets"] == len(ms)
    hi = next(m for m in ms if m["series_ticker"] == "KXHIGHNY")
    hi["rules_primary"] = hi["rules_primary"].replace("(CLINYC)", "(CLILGA)")
    reg.refresh()
    v = s.query("SELECT version, rules_primary FROM wx_rules WHERE ticker=? ORDER BY version", (hi["ticker"],))
    assert [r["version"] for r in v] == [1, 2] and "CLINYC" in v[0]["rules_primary"]
    row = s.one("SELECT status, status_reason FROM wx_contracts WHERE ticker=?", (hi["ticker"],))
    assert row["status"] == "DISABLED" and "RULES CHANGED" in row["status_reason"]
    assert "KXHIGHNY" in reg.blocked_series()
    assert s.one("SELECT COUNT(*) n FROM wx_alerts WHERE kind='rules_changed'")["n"] == 1


def test_the_daily_estimator_skips_a_blocked_series():
    from kalshibot.estimators.weather import WeatherTemperatureEstimator
    from kalshibot.models import Market
    est = WeatherTemperatureEstimator({"series": [{"ticker_prefix": "KXHIGHNY", "station": "KNYC",
                                                   "latitude": 40.7, "longitude": -74.0}]},
                                      data=object())
    est.blocked_series = lambda: {"KXHIGHNY"}
    m = Market(ticker="KXHIGHNY-26SEP29-T76", event_ticker="KXHIGHNY-26SEP29",
               series_ticker="KXHIGHNY", title="t", subtitle="77 or above")
    e = est.estimate(m)
    assert e.skip_reason and "settlement mismatch" in e.skip_reason


# ------------------------------------------------------------------ WX-2
def test_quota_guard_stops_at_the_budget_and_counts_the_live_strategy():
    s = _store()
    shared = {"n": 0}
    q = QuotaGuard(s, {"open_meteo": 10}, shared_usage=lambda p: shared["n"], sleep=lambda x: None)
    assert q.allow("open_meteo", 4)
    shared["n"] = 5                      # the live strategy made 5 calls since
    assert q.allow("open_meteo", 1)      # 4 + 5 + 1 = 10
    assert not q.allow("open_meteo", 1)
    assert s.one("SELECT refused FROM wx_quota WHERE provider='open_meteo'")["refused"] == 1
    assert om_weight(42, 90) == 5 * 7


def test_local_day_aggregates_need_enough_hours():
    times = [f"2026-09-29T{h:02d}:00" for h in range(24)] + ["2026-09-30T00:00"]
    vals = list(range(24)) + [50]
    agg = local_day_aggregates(times, vals)
    assert agg["2026-09-29"]["temp_max"] == 23 and agg["2026-09-29"]["temp_min"] == 0
    assert agg["2026-09-29"]["temp_mean_hourly"] == pytest.approx(11.5)
    assert "temp_max" not in agg["2026-09-30"]           # one hour is not a day


def test_twc_rows_keep_preliminary_and_final_with_quality_flags():
    s = _store()
    base = {"date": "2026-09-27", "results": [{"station": {"icao": "KPHX"}, "status": "preliminary",
             "data": {"maxTemp": 97, "minTemp": 79, "precipitation": "T"}, "avgTemp": 87.2,
             "avgHours": 17, "dailyMean": 88, "hdd": 0}]}
    rows = parse_twc_day(base, "t1", date(2026, 9, 28))
    q = {r[3]: (r[4], r[5], r[6]) for r in rows}
    assert q["temp_mean_hourly"] == (87.2, "PRELIMINARY", "SUSPECT")   # 17 hours < 18
    assert q["precip"][0] == pytest.approx(0.0001)                      # trace
    final = json.loads(json.dumps(base))
    final["results"][0]["status"] = "official"
    final["results"][0]["data"]["maxTemp"] = 98
    ins = ("INSERT OR IGNORE INTO wx_observations (source, station, obs_date, variable, value, "
           "status, quality, hours, retrieved_at) VALUES (?,?,?,?,?,?,?,?,?)")
    s.executemany(ins, rows)
    s.executemany(ins, parse_twc_day(final, "t2", date(2026, 9, 28)))
    mx = s.query("SELECT value, status FROM wx_observations WHERE variable='temp_max' ORDER BY id")
    assert [(r["value"], r["status"]) for r in mx] == [(97, "PRELIMINARY"), (98, "FINAL")]
    stale = parse_twc_day(base, "t3", date(2026, 10, 5))
    assert {r[3]: r[6] for r in stale}["temp_max"] == "STALE"


# ------------------------------------------------------------------ WX-3
def _synthetic_history(s, rho=0.6, days=240, stations=("KAAA", "KBBB", "KCCC")):
    rng = np.random.default_rng(1)
    start = date(2025, 6, 1)
    fc_rows, clim = [], []
    for st in stations:
        truth = 70 + 10 * np.sin(np.arange(days) / 30)
        for d in range(days):
            clim.append((st, (start + timedelta(days=d)).isoformat(), float(truth[d]), float(truth[d]) - 15, None))
        # errors: AR(1) along valid days within one issue, good model sd 1.5, bad sd 4, bias +2 on bad
        for issue in range(days):
            z = 0.0
            for lead in range(1, 8):
                vd = issue + lead
                if vd >= days:
                    break
                z = rho * z + math.sqrt(1 - rho * rho) * rng.standard_normal() if lead > 1 else rng.standard_normal()
                e_good = 1.5 * z
                e_bad = 2.0 + 4.0 * rng.standard_normal()
                day = (start + timedelta(days=vd)).isoformat()
                fc_rows.append(("open_meteo_previous_runs", "good", st, day, "temp_max",
                                float(truth[vd] + e_good), f"{issue}~{lead}", lead, "x", 1))
                fc_rows.append(("open_meteo_previous_runs", "bad", st, day, "temp_max",
                                float(truth[vd] + e_bad), f"{issue}~{lead}", lead, "x", 1))
    s.executemany("INSERT OR IGNORE INTO wx_forecasts (source, model, station, valid_date, variable, "
                  "value, issued_at, lead_days, retrieved_at, approx_issue) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  fc_rows)
    s.executemany("INSERT INTO wx_climatology (station, obs_date, maxt, mint, avgt) VALUES (?,?,?,?,?)", clim)


def test_calibration_recovers_bias_weights_and_temporal_dependence():
    s = _store()
    _synthetic_history(s, rho=0.6)
    params, report = cal.fit(s, test_frac=0.25)
    b = params["blocks"]["temp_max|3"]
    assert b["global_bias"]["bad"] == pytest.approx(2.0, abs=0.4)
    assert b["weights"]["good"] > 0.8                # ~1/2.25 vs 1/16
    assert b["sd"] == pytest.approx(1.45, abs=0.3)
    assert params["rho"]["temp_max"]["rho"] == pytest.approx(0.6, abs=0.12)
    oos = report["out_of_sample"]["temp_max|3"]
    assert oos["rmse_blend"] < oos["rmse_equal"]
    assert 0.7 < oos["cover80"] < 0.9 and 0.83 < oos["cover90"] < 0.96


def test_integer_contract_probabilities():
    # "greater than 76" on a whole-number report: needs 77+  => X >= 76.5
    assert cal.prob_integer(">", 76, None, 76.5, 1.0) == pytest.approx(0.5)
    assert cal.prob_integer("<", None, 69, 68.5, 1.0) == pytest.approx(0.5)
    p = cal.prob_integer("between", 72, 73, 72.5, 1.0)
    assert p == pytest.approx(cal.norm_cdf(1.0) - cal.norm_cdf(-1.0))


# ------------------------------------------------------------------ WX-4
def test_ar1_paths_keep_the_dependence_they_are_given():
    z = ar1_paths(40000, 7, 0.8, np.random.default_rng(0))
    r = np.corrcoef(z[:, 2], z[:, 3])[0, 1]
    assert r == pytest.approx(0.8, abs=0.02)
    assert np.std(z[:, 6]) == pytest.approx(1.0, abs=0.02)


def test_run_length_probability_depends_on_dependence():
    days = [DayInput(f"2026-09-{28 + i}", "forecast", 90.0, 2.0) for i in range(3)] + \
           [DayInput(f"2026-10-0{i + 1}", "forecast", 90.0, 2.0) for i in range(4)]

    def stat(v, _):
        return longest_run(round_half_up(v) > 90)
    ind = simulate(days, stat, 20000, 3, rho_forecast=0.0, event=lambda s: s >= 4).prob
    dep = simulate(days, stat, 20000, 3, rho_forecast=0.85, event=lambda s: s >= 4).prob
    # a hot spell persists: long runs are much likelier than independence says.
    # If the engine silently dropped rho, these two would be equal.
    assert dep > ind + 0.08


def test_rounding_strictness_missing_hours_and_fixed_past_days():
    # 90.4 rounds to 90 (not > 90); 90.5 rounds to 91 (counts)
    assert round_half_up(90.5) == 91 and round_half_up(90.4) == 90
    s = _store()
    eng = ExtendedEngine(s, None, cal.Calibration({}), {"KPHX": {"timezone": "America/Phoenix"}},
                         n_paths=2000)
    c = {"ticker": "T", "cls": "RUN_LENGTH", "aggregation": "RUN_LENGTH", "variable": "temp_mean_hourly",
         "operator": ">", "threshold": 90.0, "run_k": 2, "min_hours": 18, "station": "KPHX"}
    obs = [DayInput("d1", "observed", 91.0, hours=24), DayInput("d2", "observed", 92.0, hours=17),
           DayInput("d3", "observed", 90.0, hours=24), DayInput("d4", "observed", 91.0, hours=24)]
    eng.day_inputs = lambda _c, _v: obs
    eng.calib = cal.Calibration({})
    res, _, _ = eng.price(c)
    # d2 has 17 hours -> breaks the streak; d3 is exactly 90 -> does not count.
    assert res.prob == 0.0 and res.prob_lo == res.prob_hi == 0.0
    obs[1] = DayInput("d2", "observed", 92.0, hours=24)
    res, _, _ = eng.price(c)
    assert res.prob == 1.0                       # all observed: certain, no noise


def test_degree_days_use_the_published_figure_for_observed_days():
    s = _store()
    eng = ExtendedEngine(s, None, cal.Calibration({}), {}, n_paths=1000)
    days = [DayInput("2026-10-01", "observed", 60.0), DayInput("2026-10-02", "forecast", 60.0, 0.01)]
    eng.day_inputs = lambda _c, _v: days
    eng._observed_degree_days = lambda st, d, heating: 7.0     # TWC says 7, recompute says 5
    c = {"ticker": "H", "cls": "SEASONAL", "aggregation": "CUMULATIVE", "variable": "hdd",
         "operator": "<", "threshold": 12.5, "station": "KCVG"}
    res, _, _ = eng.price(c)
    assert res.prob == 1.0                        # 7 + 5 = 12 < 12.5
    c["threshold"] = 11.5
    assert eng.price(c)[0].prob == 0.0            # 12 is not < 11.5


# ------------------------------------------------------------------ WX-5
def test_hourly_target_and_sd():
    t = target_utc({"at_time": "7 AM EDT", "window_start": "2026-09-29"}, "America/New_York")
    assert t == datetime(2026, 9, 29, 11, 0, tzinfo=timezone.utc)
    assert sd_for_lead(0.5) < sd_for_lead(5) < sd_for_lead(30)


# ------------------------------------------------------------------ WX-6
def test_paper_decisions_respect_uncertainty_and_settle():
    s = _store()
    pb = PaperBook(s, PaperConfig())
    run = {"id": 1, "ticker": "X", "cls": "RUN_LENGTH", "prob": 0.80, "prob_lo": 0.75,
           "prob_hi": 0.85, "market_yes_bid": 0.40, "market_yes_ask": 0.45}
    d = pb.decide(run, {})
    assert d["decision"] == "PAPER_YES" and d["edge_net"] > d["required_edge"]
    wide = dict(run, prob_lo=0.4, prob_hi=0.95)
    assert "model uncertainty" in pb.decide(wide, {})["reason"]
    assert pb.decide(run, {"status": "DISABLED"})["decision"] == "PASS"
    pb.record([run], {"X": {}})
    pb.record([dict(run, id=2)], {"X": {}})            # already holding: no second position
    assert s.one("SELECT COUNT(*) n FROM wx_paper WHERE decision LIKE 'PAPER_%'")["n"] == 1
    s.execute("INSERT INTO wx_contracts (ticker, cls) VALUES ('X','RUN_LENGTH')")
    pb.resolve({"X": "yes"})
    r = s.one("SELECT pnl, settled_yes FROM wx_paper WHERE decision='PAPER_YES'")
    assert r["settled_yes"] == 1 and r["pnl"] == pytest.approx(1 - 0.45 - d["fee"], abs=1e-4)


# ------------------------------------------------------------------ WX-7
def test_eligibility_is_a_label_that_needs_operator_signoff():
    s = _store()
    out = evaluate(s, "/nonexistent/reviews.json")
    assert out["RUN_LENGTH"]["status"] == "RESEARCH_ONLY"
    assert "operator_authorization" in out["RUN_LENGTH"]["blockers"]
    assert "locked" in out["_note"]


def test_the_wx_package_cannot_reach_an_order_path():
    src = "\n".join(p.read_text() for p in (ROOT / "kalshibot" / "wx").glob("*.py"))
    for forbidden in ("DemoExecutionClient", "PortfolioClient", "place_demo_order", "create_order",
                      "/portfolio", "Signer", "KALSHI_API_KEY", "prod_marketdata"):
        assert forbidden not in src, forbidden
    assert not re.search(r"\.post\([^)]*kalshi", src, re.I)


def test_new_reader_methods_are_public_market_data_paths():
    from kalshibot.env_guard import is_public_market_data_path
    for p in ("/series", "/events", "/markets"):
        assert is_public_market_data_path(p)


def test_kalshi_reads_back_off_on_429():
    from kalshibot.wx.registry import with_backoff

    class E(Exception):
        def __init__(self, code):
            self.response = type("R", (), {"status_code": code})()
    calls = {"n": 0}
    waits = []

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise E(429)
        return "ok"
    assert with_backoff(flaky, sleep=waits.append) == "ok" and waits == [2.0, 4.0]
    with pytest.raises(E):
        with_backoff(lambda: (_ for _ in ()).throw(E(403)), sleep=waits.append)
