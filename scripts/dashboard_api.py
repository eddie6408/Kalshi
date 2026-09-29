"""Everything the live dashboard shows, as one JSON snapshot.

Read-only. The journal is opened `mode=ro` with a busy timeout (a long-running
server cannot use `immutable=1`: it would never see new rows), each section
is built on its own and fails on its own - a broken panel says why instead of
taking the page down - and nothing here writes to the journal or talks to the
bot's threads. The bot's own view of itself comes from data/control/
status.json, which it writes every heartbeat (kalshibot/control.py).

The exchange is read (balance and positions, GET only, cached) so the page
can show what Kalshi says next to what the journal says.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import dashboard as legacy  # noqa: E402  (scripts/dashboard.py: calibration, skill, sources)

SERVICES = ("kalshibot", "kalshi-live-volatility")


# ---------------------------------------------------------------- helpers
def open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def rows(conn, sql, *args) -> list[dict]:
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    except sqlite3.Error:
        return []


def first(conn, sql, *args, default=None):
    r = rows(conn, sql, *args)
    return r[0] if r else default


def _ts(value) -> float | None:
    """ISO text or unix seconds -> unix seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


INFLATION_PREFIXES = ("KXCPI", "KXPCE")
#: dashboard label -> the strategy name the engine and the pause file use
STRATEGIES = {"Weather": "weather_temperature", "Inflation": "inflation_nowcast",
              "Arbitrage": "arbitrage"}


def strategy_of(ticker: str) -> str:
    t = (ticker or "").upper()
    if t.startswith("KXHIGH"):
        return "Weather"
    if t.startswith(INFLATION_PREFIXES):
        return "Inflation"
    return "Arbitrage"


def paused(control_dir: Path | None = None) -> dict:
    try:
        base = control_dir or (ROOT / "data" / "control")
        raw = json.loads((base / "paused.json").read_text(encoding="utf-8"))
        return {k: bool(v) for k, v in (raw.get("paused") or {}).items()}
    except (OSError, ValueError):
        return {}


def strike_of(ticker: str) -> float | None:
    from kalshibot.inflation_model import split_threshold
    return split_threshold(ticker)[1]


def event_of(ticker: str) -> str:
    from kalshibot.inflation_model import split_threshold
    return split_threshold(ticker)[0]


def inflation(conn) -> dict:
    """The Inflation page: the estimator's latest nowcasts (status file) and,
    per event, our probability for each strike beside the market's."""
    status = {}
    try:
        status = json.loads((ROOT / "data" / "control" / "inflation.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    since = (datetime.now(timezone.utc) - timedelta(hours=36)).isoformat()
    rows_ = rows(conn, "SELECT ticker, series, target_date, our_probability p, model, "
                       "market_yes_cents da, market_yes_bid_cents db, prod_yes_ask_cents pa, "
                       "prod_yes_bid_cents pb, lead_hours, ts FROM probability_log "
                       "WHERE estimator='inflation_nowcast' AND ts >= ? ORDER BY id", since)
    latest: dict[str, dict] = {}
    for r in rows_:
        latest[r["ticker"]] = r
    events: dict[str, dict] = {}
    for t, r in latest.items():
        k = strike_of(t)
        if k is None:
            continue
        ev = events.setdefault(event_of(t), {
            "event": event_of(t), "series": r["series"], "target": r["target_date"],
            "lead_hours": r["lead_hours"], "model": r["model"], "points": []})
        mid = ((r["da"] + r["db"]) / 200.0) if r["da"] is not None and r["db"] is not None else None
        pmid = ((r["pa"] + r["pb"]) / 200.0) if r["pa"] is not None and r["pb"] is not None else None
        ev["points"].append({"strike": k, "model": r["p"], "demo": mid, "prod": pmid})
    out = []
    for ev in events.values():
        ev["points"].sort(key=lambda p: p["strike"])
        out.append(ev)
    out.sort(key=lambda e: (e["lead_hours"] if e["lead_hours"] is not None else 1e9))
    return {"status": status, "events": out[:12],
            "fit": _fit_summary()}


WX_CLASS_GROUP = {"DAILY_HIGH": "daily", "DAILY_LOW": "daily", "DAILY_AVG": "daily",
                  "HOURLY_TEMP": "hourly", "WEEKLY_MAX": "weekly", "WEEKLY_MIN": "weekly",
                  "RUN_LENGTH": "weekly", "MONTHLY_MAX": "monthly", "MONTHLY_MIN": "monthly",
                  "SEASONAL": "seasonal", "CLIMATE_ONE_OFF": "seasonal",
                  "PRECIPITATION": "other", "OTHER": "other"}


def wx_research(cfg: dict) -> dict | None:
    """The Weather research page (WX-1..7): lifecycle by class, every
    modelled contract with its latest probability, quote and paper decision,
    the simulated distributions, data quality, quota, calibration and the
    eligibility checklist. Read-only on data/weather_research.db."""
    wx = (cfg or {}).get("wx_research") or {}
    path = ROOT / wx.get("db_path", "data/weather_research.db")
    if not path.exists():
        return {"enabled": bool(wx.get("enabled")), "ready": False}
    c = open_ro(path)
    try:
        life = rows(c, "SELECT cls, status, source, COUNT(*) n FROM wx_contracts "
                       "WHERE market_status='open' GROUP BY cls, status, source")
        latest = rows(c, "SELECT r.* FROM wx_model_runs r JOIN (SELECT ticker, MAX(id) mid FROM "
                         "wx_model_runs WHERE method NOT LIKE 'VOID%' GROUP BY ticker) l "
                         "ON l.mid=r.id")
        runs = {r["ticker"]: r for r in latest}
        contracts = rows(c, "SELECT ticker, event_ticker, series, cls, station, location, source, "
                            "operator, threshold, threshold_hi, run_k, window_start, window_end, "
                            "at_time, status, status_reason, rules_version, yes_bid, yes_ask, close_time, "
                            "market_status, result FROM wx_contracts WHERE cls NOT IN "
                            "('OTHER','DAILY_HIGH','DAILY_LOW') AND (market_status='open' OR ticker IN "
                            "(SELECT ticker FROM wx_model_runs)) ORDER BY close_time LIMIT 400")
        paper = {r["ticker"]: r for r in rows(
            c, "SELECT p.* FROM wx_paper p JOIN (SELECT ticker, MAX(id) mid FROM wx_paper GROUP BY ticker) l "
               "ON l.mid=p.id")}
        out_contracts = []
        for k in contracts:
            r = runs.get(k["ticker"]) or {}
            p = paper.get(k["ticker"]) or {}
            inputs = {}
            try:
                inputs = json.loads(r.get("inputs") or "{}")
            except ValueError:
                pass
            out_contracts.append({
                **k, "group": WX_CLASS_GROUP.get(k["cls"], "other"),
                "prob": r.get("prob"), "prob_lo": r.get("prob_lo"), "prob_hi": r.get("prob_hi"),
                "mc_se": r.get("mc_se"), "run_at": r.get("at"), "method": r.get("method"),
                "model_version": r.get("model_version"), "n_paths": r.get("n_paths"),
                "q_bid": r.get("market_yes_bid", k.get("yes_bid")),
                "q_ask": r.get("market_yes_ask", k.get("yes_ask")),
                "hist": json.loads(r["stat_hist"]) if r.get("stat_hist") else None,
                "days": [{"day": d.get("day"), "kind": d.get("kind"), "mean": d.get("mean"),
                          "sd": d.get("sd")} for d in (inputs.get("days") or [])],
                "paper": {k2: p.get(k2) for k2 in ("decision", "reason", "side", "price", "edge_net",
                                                   "required_edge", "pnl", "at")} if p else None})
        today = datetime.now(timezone.utc).date().isoformat()
        quota = rows(c, "SELECT provider, calls, refused FROM wx_quota WHERE day=?", today)
        dq = rows(c, "SELECT variable, status, quality, COUNT(*) n FROM wx_observations "
                     "WHERE obs_date >= date('now','-4 days') GROUP BY variable, status, quality")
        def one(sql):
            return (first(c, sql) or {}).get("v")
        fc = one("SELECT MAX(retrieved_at) v FROM wx_forecasts WHERE source='open_meteo'")
        fc_n = one("SELECT COUNT(*) v FROM wx_forecasts WHERE source='open_meteo'") or 0
        hist_n = one("SELECT COUNT(*) v FROM wx_forecasts WHERE source='open_meteo_previous_runs'") or 0
        clim_n = one("SELECT COUNT(DISTINCT station) v FROM wx_climatology") or 0
        elig = rows(c, "SELECT e.cls, e.status, e.criteria, e.at FROM wx_eligibility e JOIN "
                       "(SELECT cls, MAX(id) mid FROM wx_eligibility GROUP BY cls) l ON l.mid=e.id")
        for e in elig:
            try:
                e["criteria"] = json.loads(e["criteria"] or "{}")
            except ValueError:
                e["criteria"] = {}
        alerts = rows(c, "SELECT at, kind, subject, detail FROM wx_alerts ORDER BY id DESC LIMIT 25")
        shadow = one("SELECT COUNT(*) v FROM wx_daily_shadow") or 0
        try:
            from kalshibot.wx.paper import class_report

            class _RO:
                def query(self, sql, params=()):
                    return rows(c, sql, *params)

                def one(self, sql, params=()):
                    r_ = rows(c, sql, *params)
                    return r_[0] if r_ else None
            report = class_report(_RO())
        except Exception as exc:  # noqa: BLE001
            report = {"error": str(exc)}
    finally:
        c.close()
    calib = None
    try:
        raw = json.loads((ROOT / wx.get("calibration_path", "data/wx_calibration.json")).read_text())
        p, rep = raw.get("params") or {}, raw.get("report") or {}
        calib = {"fitted_at": p.get("fitted_at"), "rows": rep.get("rows"), "dates": rep.get("dates"),
                 "split": rep.get("split"),
                 "rho": {k: v.get("rho") for k, v in (p.get("rho") or {}).items()},
                 "oos": [{"key": k, **v} for k, v in sorted((rep.get("out_of_sample") or {}).items())]}
    except (OSError, ValueError):
        pass
    status = {}
    try:
        status = (json.loads((ROOT / "data" / "control" / "status.json").read_text())
                  .get("wx_research") or {})
    except (OSError, ValueError):
        pass
    return {"enabled": bool(wx.get("enabled")), "ready": True, "lifecycle": life,
            "contracts": out_contracts, "quota": quota,
            "quota_limits": wx.get("quota_per_day") or {}, "data_quality": dq,
            "forecasts": {"last_snapshot": fc, "rows": fc_n, "history_rows": hist_n,
                          "climatology_stations": clim_n},
            "eligibility": elig, "alerts": alerts, "calibration": calib, "report": report,
            "daily_shadow_rows": shadow, "tasks": status.get("tasks") or {},
            "registry": status.get("registry") or {}}


def _fit_summary() -> dict | None:
    try:
        raw = json.loads((ROOT / "data" / "inflation_model.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    out = {"fitted_at": raw.get("fitted_at"), "since": raw.get("since"), "series": {}}
    for k, rep in (raw.get("report") or {}).items():
        oos = rep.get("out_of_sample") or {}
        out["series"][k] = {"brier_oos": oos.get("brier"), "n_oos": oos.get("n"),
                            "brier_normal": rep.get("out_of_sample_normal_brier"),
                            "rows": (rep.get("fit") or {}).get("n")}
    return out


def load_config() -> dict:
    import yaml
    from kalshibot.config import _StrictLoader
    with open(ROOT / "config.yaml", encoding="utf-8") as fh:
        return yaml.load(fh, Loader=_StrictLoader) or {}


# ---------------------------------------------------------------- sections
def account(conn) -> dict:
    snaps = rows(conn, "SELECT ts, balance_usd FROM account_snapshots "
                       "ORDER BY id DESC LIMIT 4000")[::-1]
    # The demo exchange sometimes answers with a stale balance for a few
    # seconds (seen 28 Sep: 454.35 vs 489.67 with no positions or orders).
    # The most common of the last five readings is the balance; a lone
    # stale read does not move it.
    recent = [round(s["balance_usd"], 2) for s in snaps[-5:] if s["balance_usd"] is not None]
    cash = Counter(recent).most_common(1)[0][0] if recent else None
    series, bucket, vals = [], None, []
    for s in snaps:
        t = _ts(s["ts"])
        if t is None or s["balance_usd"] is None:
            continue
        h = int(t // 3600)
        if bucket is not None and h != bucket and vals:
            series.append({"t": bucket * 3600, "v": Counter(vals).most_common(1)[0][0]})
            vals = []
        bucket = h
        vals.append(round(s["balance_usd"], 2))
    if vals:
        series.append({"t": bucket * 3600, "v": Counter(vals).most_common(1)[0][0]})
    return {"cash": cash, "as_of": snaps[-1]["ts"] if snaps else None,
            "flicker": len(set(recent)) > 1, "series": series[-24 * 30:]}


def pnl(conn) -> dict:
    """Realised P&L from fills and settlements, as a running series."""
    legs = rows(conn, "SELECT ts, ticker, side, action, filled, avg_price_cents, "
                      "limit_price_cents, fee_usd, state FROM leg_orders "
                      "WHERE filled > 0 ORDER BY id")
    pos: dict = defaultdict(lambda: {"qty": 0, "cost": 0.0})
    events = []
    fees = 0.0
    for x in legs:
        n = int(x["filled"] or 0)
        px = float(x["avg_price_cents"] if x["avg_price_cents"] is not None
                   else (x["limit_price_cents"] or 0)) / 100.0
        fees += float(x["fee_usd"] or 0)
        p = pos[(x["ticker"], x["side"])]
        if x["action"] == "buy":
            p["qty"] += n
            p["cost"] += n * px
        elif p["qty"] > 0:
            take = min(n, p["qty"])
            avg = p["cost"] / p["qty"]
            events.append({"t": _ts(x["ts"]), "ticker": x["ticker"],
                           "strategy": strategy_of(x["ticker"]),
                           "pnl": take * (px - avg),
                           "settled": str(x["state"] or "") == "SETTLED"})
            p["qty"] -= take
            p["cost"] -= take * avg
    events.sort(key=lambda e: e["t"] or 0)
    cum, series = 0.0, []
    for e in events:
        cum += e["pnl"]
        series.append({"t": e["t"], "v": round(cum, 4)})
    now = time.time()
    day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                             microsecond=0).timestamp()
    by_strategy = defaultdict(lambda: {"pnl": 0.0, "closed": 0, "wins": 0})
    daily = defaultdict(float)
    for e in events:
        s = by_strategy[e["strategy"]]
        s["pnl"] += e["pnl"]
        s["closed"] += 1
        s["wins"] += 1 if e["pnl"] > 0 else 0
        daily[datetime.fromtimestamp(e["t"], timezone.utc).strftime("%Y-%m-%d")] += e["pnl"]
    try:
        from kalshibot.journal import pnl_summary
        summ = pnl_summary(conn, datetime.fromtimestamp(day, timezone.utc).isoformat())
    except Exception:  # noqa: BLE001
        summ = {}
    return {
        "realised": summ.get("realised", cum), "today": summ.get("realised_since"),
        "closed": summ.get("closed"), "wins": summ.get("wins"),
        "win_rate": (summ["wins"] / summ["closed"]) if summ.get("closed") else None,
        "open": summ.get("open"), "open_cost": summ.get("open_cost"),
        "last_7d": sum(e["pnl"] for e in events if (e["t"] or 0) >= now - 7 * 86400),
        "fees": fees, "series": series[-2000:],
        "by_strategy": [{"strategy": k, **v} for k, v in sorted(by_strategy.items())],
        "daily": [{"day": k, "pnl": round(v, 4)} for k, v in sorted(daily.items())][-60:],
    }


def trades(conn) -> dict:
    legs = rows(conn, "SELECT id, ts, ticker, side, action, requested, filled, "
                      "limit_price_cents, avg_price_cents, fee_usd, state, error "
                      "FROM leg_orders ORDER BY id DESC LIMIT 250")
    for x in legs:
        x["strategy"] = strategy_of(x["ticker"])
    execs = rows(conn, "SELECT id, ts, strategy, event_id, outcome, sets_completed, "
                       "total_cost_usd, total_fees_usd, exposed FROM executions "
                       "ORDER BY id DESC LIMIT 60")
    return {"legs": legs, "executions": execs}


def weather(conn, cfg: dict) -> dict:
    """One entry per station (high and low side by side), from a handful of
    grouped queries - 48 series one query at a time was ~340 per refresh."""
    from kalshibot.weather_stations import load_series
    series_cfg = load_series(cfg.get("weather") or {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    fc = {r["series"]: r for r in rows(conn, """
        SELECT f.series, f.ts, f.target_date, f.predicted_mean_f mean, f.predicted_sigma_f sigma,
               f.lead_hours, f.source, f.actual_f FROM forecasts f
        JOIN (SELECT series, MAX(id) id FROM forecasts GROUP BY series) m ON m.id = f.id""")}
    latest = {r["series"]: r for r in rows(conn, """
        SELECT p.series, p.model, p.lead_hours FROM probability_log p
        JOIN (SELECT series, MAX(id) id FROM probability_log
              WHERE ts >= ? GROUP BY series) m ON m.id = p.id""", since)}
    scan = {r["pfx"]: r for r in rows(conn, """
        SELECT substr(markets, 1, instr(markets, '-') - 1) pfx, COUNT(DISTINCT markets) n,
               SUM(decision='ACCEPTED') acc FROM opportunity_ledger
        WHERE detected_at >= ? AND markets LIKE 'KX%' GROUP BY pfx""", since)}
    reasons: dict[str, list] = {}
    for r in rows(conn, """
        SELECT substr(markets, 1, instr(markets, '-') - 1) pfx,
               COALESCE(rejection_reason, '') reason, COUNT(*) n FROM opportunity_ledger
        WHERE detected_at >= ? AND decision='REJECTED' AND markets LIKE 'KX%'
        GROUP BY pfx, reason ORDER BY n DESC""", since):
        if len(reasons.setdefault(r["pfx"], [])) < 3:
            reasons[r["pfx"]].append({"reason": r["reason"], "n": r["n"]})

    def latest_obs(source_id, data_type):
        return {r["subject"]: r for r in rows(conn, """
            SELECT o.subject, o.value, o.target_date, o.data_time FROM source_observations o
            JOIN (SELECT subject, MAX(id) id FROM source_observations
                  WHERE source_id=? AND data_type=? GROUP BY subject) m ON m.id = o.id""",
            source_id, data_type)}
    obs = latest_obs("nws_observations", "observed_high_so_far_f")
    cli = {"high": latest_obs("nws_climate_report", "official_daily_max_f"),
           "low": latest_obs("nws_climate_report", "official_daily_min_f")}
    twc = {"high": latest_obs("twc_settlement", "settlement_daily_max_f"),
           "low": latest_obs("twc_settlement", "settlement_daily_min_f")}

    stations: dict[str, dict] = {}
    for sc in series_cfg:
        st = str(sc.get("station") or "").upper()
        kind = sc.get("kind", "high")
        prefix = sc.get("ticker_prefix")
        e = stations.setdefault(st, {"station": st,
                                     "label": str(sc.get("label") or st).replace(" daily high", "")
                                     .replace(" daily low", ""),
                                     "timezone": sc.get("timezone"),
                                     "observed": obs.get(st), "kinds": {}})
        sn = scan.get(prefix) or {}
        e["kinds"][kind] = {
            "series": prefix, "forecast": fc.get(prefix),
            "official": cli[kind].get("CLI" + str(sc.get("climate_report") or "")),
            "settlement": twc[kind].get(st),
            "scanned_24h": sn.get("n") or 0, "accepted_24h": sn.get("acc") or 0,
            "top_reasons": reasons.get(prefix, []), "model": (latest.get(prefix) or {}).get("model")}
    out = sorted(stations.values(), key=lambda e: -sum(k["scanned_24h"] for k in e["kinds"].values()))
    return {"stations": out, "today": today,
            "blend": _blend_summary()}


def _blend_summary() -> dict | None:
    try:
        raw = json.loads((ROOT / "data" / "weather_blend.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    oos = (raw.get("report") or {}).get("out_of_sample") or {}
    out = {"fitted_at": raw.get("fitted_at"), "window": raw.get("window"), "kinds": {}}
    for kind, k in oos.items():
        if not isinstance(k, dict):
            continue
        models = {m: v.get("rmse_raw") for m, v in k.items()
                  if isinstance(v, dict) and "rmse_raw" in v}
        out["kinds"][kind] = {"n": k.get("n"), "blend_rmse": (k.get("blend") or {}).get("rmse"),
                              "within_1f": (k.get("blend") or {}).get("within_1f"),
                              "equal_rmse": (k.get("equal_average") or {}).get("rmse"),
                              "models": models,
                              "weights": ((raw.get("params") or {}).get("weights") or {}).get(kind)}
    return out


def decisions(conn) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    recent = rows(conn, "SELECT detected_at, strategy, markets, direction, probability_yes, "
                        "market_probability, touch_json, decision, decision_stage, "
                        "rejection_reason, net_ev, final_state FROM opportunity_ledger "
                        "ORDER BY rowid DESC LIMIT 60")
    for r in recent:
        try:
            r["touch"] = json.loads(r.pop("touch_json") or "{}")
        except ValueError:
            r["touch"] = {}
    stages = rows(conn, "SELECT CASE WHEN decision='ACCEPTED' THEN 'ACCEPTED' "
                        "ELSE COALESCE(decision_stage,'?') END stage, COUNT(*) n "
                        "FROM opportunity_ledger WHERE detected_at >= ? GROUP BY stage "
                        "ORDER BY n DESC", since)
    reasons = rows(conn, "SELECT COALESCE(rejection_reason,'') reason, decision_stage stage, "
                         "COUNT(*) n FROM opportunity_ledger WHERE detected_at >= ? AND "
                         "decision='REJECTED' GROUP BY reason ORDER BY n DESC LIMIT 12", since)
    per = rows(conn, "SELECT strategy, COUNT(*) n, SUM(decision='ACCEPTED') acc, "
                     "SUM(final_state IN ('FILLED','PARTIALLY_FILLED','EXITED','SETTLED')) traded "
                     "FROM opportunity_ledger WHERE detected_at >= ? GROUP BY strategy", since)
    by_strategy: dict[str, dict] = {}
    for r in per:
        st = str(r["strategy"] or "")
        label = ("Weather" if "weather" in st else "Inflation" if "inflation" in st
                 else "Arbitrage")
        b = by_strategy.setdefault(label, {"evaluated": 0, "accepted": 0, "traded": 0})
        b["evaluated"] += r["n"] or 0
        b["accepted"] += r["acc"] or 0
        b["traded"] += r["traded"] or 0
    return {"recent": recent, "stages_24h": stages, "reasons_24h": reasons,
            "by_strategy_24h": by_strategy}


def model_vs_market(conn) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    pts = rows(conn, "SELECT our_probability p, market_yes_cents da, market_yes_bid_cents db, "
                     "prod_yes_ask_cents pa, prod_yes_bid_cents pb, settled_yes y, model, series "
                     "FROM probability_log WHERE ts >= ? AND lead_hours <= 12 "
                     "ORDER BY id DESC LIMIT 800", since)
    out = []
    for r in pts:
        if r["pa"] is not None and r["pb"] is not None:
            m, src = (r["pa"] + r["pb"]) / 200.0, "production"
        elif r["da"] is not None and r["db"] is not None:
            m, src = (r["da"] + r["db"]) / 200.0, "demo"
        else:
            continue
        out.append({"model": round(r["p"], 4), "market": round(m, 4), "source": src,
                    "settled": r["y"], "kind": r["model"] or "ensemble"})
    return {"points": out}


def paper(conn, cfg: dict) -> dict:
    start = float(((cfg.get("paper_live") or {}).get("starting_balance_usd")) or 500.0)
    pos = rows(conn, "SELECT * FROM paper_positions ORDER BY id")
    settled = [p for p in pos if p["state"] == "SETTLED"]
    open_ = [p for p in pos if p["state"] == "OPEN"]
    cum, series = 0.0, []
    for p in sorted(settled, key=lambda p: p["settled_at"] or 0):
        cum += p["pnl_usd"] or 0
        series.append({"t": p["settled_at"], "v": round(start + cum, 2)})

    def part(sel):
        s = [p for p in settled if sel(p)]
        return {"settled": len(s), "pnl": sum(p["pnl_usd"] or 0 for p in s),
                "wins": sum(1 for p in s if (p["pnl_usd"] or 0) > 0),
                "expected": sum(p["expected_edge_usd"] or 0 for p in s),
                "open": sum(1 for p in open_ if sel(p))}
    tied = sum((p["cost_usd"] or 0) + (p["fee_usd"] or 0) for p in open_)
    realised = sum(p["pnl_usd"] or 0 for p in settled)
    checks = rows(conn, "SELECT * FROM prod_arb_checks ORDER BY id DESC LIMIT 400")
    return {
        "starting": start, "realised": realised, "cash": start + realised - tied,
        "equity": start + realised, "tied": tied, "series": series,
        "allowed": part(lambda p: p["kind"] == "FORECAST" and not p["gate_blocked"]),
        "blocked": part(lambda p: p["kind"] == "FORECAST" and p["gate_blocked"]),
        "arb": part(lambda p: p["kind"] == "ARB"),
        "open": open_[-50:][::-1], "recent": settled[-30:][::-1],
        "arb_checks": {
            "n": len(checks),
            "demo_found": sum(1 for c in checks if c["demo_found"]),
            "demo_and_prod": sum(1 for c in checks if c["demo_found"] and c["prod_found"]),
            "prod_accepted": sum(1 for c in checks if c["prod_accepted"]),
            "median_gap": (sorted(g)[len(g) // 2] if (g := [c["prod_near_miss_gap"] for c in checks
                                                            if c["prod_near_miss_gap"] is not None])
                           else None),
            "recent": checks[:25]},
    }


def maker(conn) -> dict:
    by = {r["state"]: r for r in rows(conn, "SELECT state, COUNT(*) n, SUM(filled>0) f "
                                            "FROM maker_shadow GROUP BY state")}
    closed = sum((by.get(s) or {}).get("n", 0) for s in ("FILLED", "PARTIAL", "EXPIRED"))
    filled = sum((by.get(s) or {}).get("f", 0) or 0 for s in ("FILLED", "PARTIAL"))
    drift = first(conn, "SELECT AVG(drift_5m) d5, AVG(drift_30m) d30, AVG(drift_60m) d60, "
                        "AVG(maker_markout_cents) mk, AVG(taker_markout_cents) tk FROM maker_shadow "
                        "WHERE state IN ('FILLED','PARTIAL','EXPIRED')") or {}
    return {"by_state": {k: v["n"] for k, v in by.items()}, "closed": closed,
            "filled": filled, "fill_rate": (filled / closed) if closed else None,
            **{k: drift.get(k) for k in ("d5", "d30", "d60", "mk", "tk")},
            "recent": rows(conn, "SELECT created_at, ticker, side, price_cents, best_bid, "
                                 "best_ask, state, filled, fill_kind, stage FROM maker_shadow "
                                 "ORDER BY id DESC LIMIT 20")}


def risk(conn, cfg: dict, open_cost: float | None, today_pnl: float | None) -> dict:
    lim = cfg.get("limits") or {}
    safety = cfg.get("safety") or {}
    try:
        from kalshibot.journal import Journal
        # A Journal without __init__: its read methods on OUR read-only
        # connection, with no schema creation or migration (which write).
        j = Journal.__new__(Journal)
        j.conn, j.lock = conn, threading.RLock()
        run = j.consecutive_losses()
        last = j.last_breaker_reset()
    except Exception as exc:  # noqa: BLE001
        run, last = None, f"unavailable: {exc}"
    limit = int(safety.get("max_consecutive_losses") or 0)
    fees = cfg.get("fees") or {}
    return {
        "exposure": open_cost or 0.0,
        "max_total_exposure_usd": lim.get("max_total_exposure_usd"),
        "max_event_exposure_usd": lim.get("max_event_exposure_usd"),
        "max_market_exposure_usd": lim.get("max_market_exposure_usd"),
        "max_trades_per_hour": lim.get("max_trades_per_hour"),
        "daily_loss_limit_usd": safety.get("daily_loss_limit_usd"),
        "today_pnl": today_pnl,
        "breaker": {"losses": run, "limit": limit, "last_reset": last,
                    "tripped": bool(limit) and run is not None and run >= limit},
        "fees": {"taker_rate": fees.get("taker_rate"), "maker_rate": fees.get("maker_rate"),
                 "safety": fees.get("safety_multiplier"),
                 "schedule": (fees.get("schedule") or {})},
        "market_skill": {k: (cfg.get("market_skill") or {}).get(k)
                         for k in ("require", "since", "min_contracts", "model_since")},
        "forecast": {k: (cfg.get("forecast") or {}).get(k)
                     for k in ("max_lead_hours", "min_probability_edge", "min_confidence",
                               "max_cost_per_trade_usd", "max_contracts_per_trade")},
    }


# ---------------------------------------------------------------- system
def service(name: str) -> dict:
    try:
        out = subprocess.run(
            ["systemctl", "show", name, "-p", "ActiveState", "-p", "SubState",
             "-p", "ActiveEnterTimestamp", "-p", "NRestarts", "-p", "MainPID"],
            capture_output=True, text=True, timeout=5).stdout
        kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        return {"name": name, "active": kv.get("ActiveState"), "sub": kv.get("SubState"),
                "since": kv.get("ActiveEnterTimestamp"), "restarts": kv.get("NRestarts"),
                "pid": kv.get("MainPID")}
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "active": "unknown", "error": str(exc)}


def log_tail(path: Path, n: int = 600) -> dict:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 400_000))
            lines = fh.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError as exc:
        return {"error": str(exc), "problems": [], "heartbeat": None}
    problems = [ln[:300] for ln in lines if " WARNING " in ln or " ERROR " in ln][-25:][::-1]
    beat = next((ln for ln in reversed(lines) if "HEARTBEAT" in ln), None)
    return {"problems": problems, "heartbeat": beat[:400] if beat else None,
            "last_line": lines[-1][:300] if lines else None}


def freshness(conn, status: dict | None, status_age: float | None) -> list[dict]:
    now = time.time()
    items = [
        ("Bot status report", status_age, 180),
        ("Account balance", _age(first(conn, "SELECT ts FROM account_snapshots ORDER BY id DESC LIMIT 1"), now), 900),
        ("Forecasts priced", _age(first(conn, "SELECT ts FROM probability_log ORDER BY id DESC LIMIT 1"), now), 5400),
        ("Opportunity ledger", _age(first(conn, "SELECT detected_at ts FROM opportunity_ledger ORDER BY rowid DESC LIMIT 1"), now), 5400),
        ("Source health", _age(first(conn, "SELECT MAX(updated_at) ts FROM source_health"), now), 900),
    ]
    feed = (status or {}).get("feed") or {}
    if feed.get("enabled"):
        items.append(("Market feed message", _age({"ts": feed.get("last_message_at")}, now), 300))
    return [{"name": n, "age": a, "limit": lim,
             "ok": a is not None and a <= lim} for n, a, lim in items]


def _age(row, now) -> float | None:
    t = _ts((row or {}).get("ts"))
    return None if t is None else max(0.0, now - t)


def system(conn, reconcile: dict | None) -> dict:
    ctl = ROOT / "data" / "control"
    status, status_age = None, None
    try:
        status = json.loads((ctl / "status.json").read_text(encoding="utf-8"))
        status_age = time.time() - (ctl / "status.json").stat().st_mtime
    except (OSError, ValueError):
        pass
    du = shutil.disk_usage("/")
    ticks = ROOT / "data" / "ticks"
    tick_bytes = sum(p.stat().st_size for p in ticks.rglob("*.gz")) if ticks.exists() else 0
    audit = []
    try:
        audit = [json.loads(line) for line in
                 (ctl / "audit.jsonl").read_text(encoding="utf-8").splitlines()[-30:]][::-1]
    except (OSError, ValueError):
        pass
    run = first(conn, "SELECT id, started_ts, mode, note, code_version, config_version "
                      "FROM runs ORDER BY id DESC LIMIT 1")
    return {
        "services": [service(s) for s in SERVICES],
        "status": status, "status_age": status_age,
        "freshness": freshness(conn, status, status_age),
        "sources": rows(conn, "SELECT * FROM source_health ORDER BY tier, source_id"),
        "log": log_tail(ROOT / "logs" / "kalshibot.log"),
        "disk": {"free_gb": du.free / 1e9, "total_gb": du.total / 1e9,
                 "ticks_mb": tick_bytes / 1e6},
        "audit": audit, "run": run, "reconcile": reconcile,
    }


# ---------------------------------------------------------------- exchange
class Exchange:
    """Balance and positions from Kalshi (demo), GET only, cached."""

    def __init__(self, ttl: float = 30.0):
        self.ttl = ttl
        self._at = 0.0
        self._data: dict | None = None
        self._lock = threading.Lock()

    def read(self) -> dict:
        with self._lock:
            if self._data is not None and time.time() - self._at < self.ttl:
                return self._data
            try:
                from kalshibot.auth import Signer
                from kalshibot.clients import PortfolioClient
                from kalshibot.config import load_settings
                s = load_settings(ROOT / "config.yaml")
                c = PortfolioClient(s.rest_base, Signer.from_settings(s))._c
                bal = c._request("GET", "/portfolio/balance") or {}
                pos = c._request("GET", "/portfolio/positions") or {}
                held = []
                for p in pos.get("market_positions") or []:
                    qty = float(p.get("position_fp") or p.get("position") or 0)
                    if qty:
                        held.append({"ticker": p.get("ticker"), "position": qty,
                                     "exposure": float(p.get("market_exposure_dollars") or 0),
                                     "fees": float(p.get("fees_paid_dollars") or 0),
                                     "realised": float(p.get("realized_pnl_dollars") or 0)})
                self._data = {"ok": True, "balance": float(bal.get("balance_dollars")
                                                          or (bal.get("balance") or 0) / 100),
                              "portfolio_value": (bal.get("portfolio_value") or 0) / 100,
                              "positions": held, "read_at": time.time()}
            except Exception as exc:  # noqa: BLE001
                self._data = {"ok": False, "error": str(exc)[:300], "read_at": time.time()}
            self._at = time.time()
            return self._data


# ---------------------------------------------------------------- snapshot
def snapshot(db_path: Path, exchange: Exchange | None = None) -> dict:
    t0 = time.time()
    out: dict = {"generated": datetime.now(timezone.utc).isoformat(), "errors": {}}
    conn = open_ro(db_path)
    try:
        cfg = {}
        try:
            cfg = load_config()
        except Exception as exc:  # noqa: BLE001
            out["errors"]["config"] = str(exc)

        def sec(name, fn):
            try:
                out[name] = fn()
            except Exception as exc:  # noqa: BLE001
                out[name] = None
                out["errors"][name] = f"{type(exc).__name__}: {exc}"

        sec("core", lambda: legacy.gather(conn))
        sec("account", lambda: account(conn))
        sec("pnl", lambda: pnl(conn))
        sec("trades", lambda: trades(conn))
        sec("weather", lambda: weather(conn, cfg))
        sec("decisions", lambda: decisions(conn))
        sec("model_vs_market", lambda: model_vs_market(conn))
        sec("paper", lambda: paper(conn, cfg))
        sec("maker", lambda: maker(conn))
        sec("inflation", lambda: inflation(conn))
        sec("wx", lambda: wx_research(cfg))
        out["paused"] = paused()
        out["strategies"] = STRATEGIES
        p = out.get("pnl") or {}
        sec("risk", lambda: risk(conn, cfg, p.get("open_cost"), p.get("today")))
        sec("exchange", lambda: exchange.read() if exchange else None)
        ex = out.get("exchange") or {}
        journal_open = {(r["ticker"]) for r in ((out.get("core") or {}).get("open") or [])
                        if r.get("qty", 0) > 0}
        recon = None
        if ex.get("ok"):
            held = {p["ticker"] for p in ex.get("positions") or []}
            recon = {"exchange_only": sorted(held - journal_open),
                     "journal_only": sorted(journal_open - held),
                     "matched": len(held & journal_open),
                     "ok": held == journal_open}
        sec("system", lambda: system(conn, recon))
    finally:
        conn.close()
    out["build_ms"] = round((time.time() - t0) * 1000)
    return out
