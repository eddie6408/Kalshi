"""Check the bot's idea of "what happened" against what Kalshi settles on.

Kalshi's rules name the settlement source for every contract the bot trades
(read 2026-09-29):

  Weather   "the maximum temperature recorded at <station> (CLINYC) ...
            according to The Weather Company" - weather.com/kalshi, whose
            climate portal serves the official daily reports as JSON
            (/kalshi/api/climate/primary?date=...), free and keyless.
  CPI       the one-decimal percent change BLS publishes: m/m seasonally
            adjusted, y/y NOT seasonally adjusted; first release only.
  PCE       the one-decimal percent change BEA publishes; first release.

Scoring has used the NWS climate report (the origin of the same number) and
the Cleveland archive. This module adds the named sources themselves:

  TWC       every scoring pass, the last three days' official reports for the
            bot's stations are recorded as provenance, and compared with the
            NWS climate report already recorded - a mismatch is alerted,
            because it means the scoring truth and the settlement truth
            disagree.
  Inflation the official one-decimal figures are recomputed from the index
            levels (FRED, redistributing BLS / BEA; BLS rounds its index to
            three decimals and computes the change from that) and compared
            with how Kalshi actually resolved each settled market. A
            disagreement is alerted; one within 0.01 of a rounding boundary
            is reported as too close to call, not as an error.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import requests

from . import sources as _sources
from .alerts import ALERTS, today_key

log = logging.getLogger(__name__)

TWC_PRIMARY = "https://weather.com/kalshi/api/climate/primary?date={date}"
FINAL = {"official", "revised"}

#: Kalshi inflation series -> (FRED index, kind). m/m on the seasonally
#: adjusted index, y/y on the NOT seasonally adjusted one (the rule PDFs).
INFLATION_OFFICIAL = {
    "KXCPI": ("CPIAUCSL", "mom"),
    "KXCPICORE": ("CPILFESL", "mom"),
    "KXCPIYOY": ("CPIAUCNS", "yoy"),
    "KXCPICOREYOY": ("CPILFENS", "yoy"),
    "KXPCECORE": ("PCEPILFE", "mom"),
    "KXPCEHEAD": ("PCEPI", "mom"),
}


# ------------------------------------------------------------------ weather
def parse_twc_primary(payload: dict) -> dict[str, dict]:
    """{ICAO: {"status", "max", "min", "cli"}} for one day's report."""
    out = {}
    for r in (payload or {}).get("results") or []:
        st = r.get("station") or {}
        icao = str(st.get("icao") or "").upper()
        if not icao:
            continue
        data = r.get("data") or {}
        out[icao] = {"status": str(r.get("status") or ""), "cli": st.get("cliId"),
                     "max": data.get("maxTemp"), "min": data.get("minTemp")}
    return out


class TWCSettlement:
    def __init__(self, session=None, user_agent: str = "kalshibot/1.0 (personal research)",
                 timeout: float = 30.0):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
        self.timeout = timeout
        self._seen: set[tuple] = set()

    def fetch(self, day: date) -> dict[str, dict]:
        with _sources.MONITOR.track("twc_settlement") as call:
            r = self.session.get(TWC_PRIMARY.format(date=day.isoformat()), timeout=self.timeout)
            call.status = r.status_code
            r.raise_for_status()
            call.data_time = day.isoformat()
            return parse_twc_primary(r.json())

    def check(self, conn, stations: dict[str, str], today: date | None = None,
              days: int = 3) -> dict:
        """Record the last `days` days' final reports for `stations`
        ({ICAO: CLI location}) and compare them with the NWS climate report.
        Returns counts; never raises."""
        today = today or datetime.now(timezone.utc).date()
        stats = {"recorded": 0, "matched": 0, "mismatched": 0, "pending": 0, "errors": 0}
        for k in range(1, days + 1):
            day = today - timedelta(days=k)
            try:
                report = self.fetch(day)
            except Exception as exc:  # noqa: BLE001
                log.debug("SETTLE CHECK  TWC %s unavailable: %s", day, exc)
                stats["errors"] += 1
                continue
            for icao, cli in stations.items():
                row = report.get(icao.upper())
                if row is None or row["status"] not in FINAL or row["max"] is None:
                    stats["pending"] += 1
                    continue
                key = (icao, day, row["status"], row["max"])
                if key not in self._seen:
                    self._seen.add(key)
                    _sources.MONITOR.observe(
                        "twc_settlement", data_type="settlement_daily_max_f",
                        subject=icao.upper(), target_date=day.isoformat(),
                        value=float(row["max"]),
                        value_json={"status": row["status"], "min": row["min"], "cli": row["cli"]},
                        data_time=day.isoformat(),
                        retrieved_at=datetime.now(timezone.utc).isoformat())
                    stats["recorded"] += 1
                nws = _latest_cli(conn, cli, day)
                if nws is None:
                    continue
                if abs(float(nws) - float(row["max"])) < 0.5:
                    stats["matched"] += 1
                else:
                    stats["mismatched"] += 1
                    msg = (f"{icao} {day}: The Weather Company (settles Kalshi) says "
                           f"{row['max']}F, the NWS climate report says {nws:g}F. Scoring uses "
                           f"the climate report - check which the market resolved on.")
                    log.warning("SETTLE CHECK  %s", msg)
                    ALERTS.send(today_key("twc-mismatch", f"{icao}:{day}"),
                                f"Weather settlement mismatch: {icao}", msg,
                                priority="high", tags="warning")
        if stats["recorded"] or stats["mismatched"]:
            log.info("SETTLE CHECK  weather: %s", stats)
        return stats


def _latest_cli(conn, cli: str | None, day: date) -> float | None:
    if not cli:
        return None
    try:
        row = conn.execute(
            "SELECT value FROM source_observations WHERE source_id='nws_climate_report' "
            "AND data_type='official_daily_max_f' AND subject=? AND target_date=? "
            "ORDER BY id DESC LIMIT 1", ("CLI" + cli, day.isoformat())).fetchone()
    except Exception:  # noqa: BLE001
        return None
    return float(row[0]) if row and row[0] is not None else None


# ------------------------------------------------------------------ inflation
def official_change(index: dict[str, float], month: str, kind: str) -> tuple[float, float] | None:
    """(unrounded, published) percent change for `month` ("YYYY-MM") from an
    index keyed by "YYYY-MM", the way BLS computes it: from the index as
    published (three decimals), rounded to one decimal."""
    y, m = (int(x) for x in month.split("-"))
    back = 1 if kind == "mom" else 12
    m0 = y * 12 + (m - 1) - back
    prev = f"{m0 // 12:04d}-{m0 % 12 + 1:02d}"
    if month not in index or prev not in index:
        return None
    cur, old = round(index[month], 3), round(index[prev], 3)
    raw = 100.0 * (cur / old - 1.0)
    return raw, round(raw + 1e-9, 1)


def check_inflation(conn, econ, market_client, limit: int = 60) -> dict:
    """Compare Kalshi's resolution of every settled inflation market the bot
    priced with the official figure recomputed from the index."""
    from .estimators.inflation import target_month
    from .inflation_model import split_threshold
    from .settlement import resolution_of
    stats = {"checked": 0, "agree": 0, "disagree": 0, "too_close": 0, "unknown": 0}
    try:
        tickers = [r[0] for r in conn.execute(
            "SELECT DISTINCT ticker FROM probability_log WHERE estimator='inflation_nowcast' "
            "AND settled_yes IS NOT NULL ORDER BY ticker DESC LIMIT ?", (limit,))]
    except Exception:  # noqa: BLE001
        return stats
    indexes: dict[str, dict[str, float]] = {}
    for t in tickers:
        prefix = t.split("-")[0]
        spec = INFLATION_OFFICIAL.get(prefix)
        strike = split_threshold(t)[1]
        if spec is None or strike is None:
            continue
        try:
            m = market_client.get_market(t)
            yes = resolution_of(m)
            month = target_month(m)
            if spec[0] not in indexes:
                indexes[spec[0]] = {d.strftime("%Y-%m"): v for d, v in econ.fred(spec[0])}
        except Exception as exc:  # noqa: BLE001
            log.debug("SETTLE CHECK  %s: %s", t, exc)
            stats["unknown"] += 1
            continue
        got = official_change(indexes[spec[0]], month, spec[1]) if month else None
        if yes is None or got is None:
            stats["unknown"] += 1
            continue
        raw, published = got
        stats["checked"] += 1
        expected = published > strike + 1e-9
        boundary = round(raw * 10 - 0.5) / 10 + 0.05
        if expected == yes:
            stats["agree"] += 1
        elif abs(raw - boundary) < 0.01:
            stats["too_close"] += 1
        else:
            stats["disagree"] += 1
            msg = (f"{t}: Kalshi resolved {'YES' if yes else 'NO'}, but the official "
                   f"{spec[1]} figure recomputed from {spec[0]} is {published:.1f}% "
                   f"(raw {raw:.3f}) against a {strike:g}% strike.")
            log.warning("SETTLE CHECK  %s", msg)
            ALERTS.send(today_key("infl-settle", t), "Inflation settlement mismatch", msg,
                        priority="high", tags="warning")
    if stats["checked"]:
        log.info("SETTLE CHECK  inflation: %s", stats)
    return stats


def stations_from_config(series_cfg: list[dict]) -> dict[str, str]:
    return {str(s["station"]).upper(): str(s.get("climate_report") or "")
            for s in series_cfg or [] if s.get("station")}

