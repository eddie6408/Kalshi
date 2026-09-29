#!/usr/bin/env python3
"""Build config/weather_stations.json: every station Kalshi settles weather on.

    .venv/bin/python scripts/build_weather_stations.py            # write it
    .venv/bin/python scripts/build_weather_stations.py --dry-run  # print it

Three sources, each for what it is authoritative on:
  Kalshi (production, public)   which daily high / low series exist, and the
                                station each settles on - read from the
                                contract rule itself ("... recorded at
                                Atlanta (CLIATL) ...")
  The Weather Company portal    the station list Kalshi's settlement source
                                publishes (ICAO, climate-report id, zone)
  NWS api.weather.gov           the station's exact coordinates and name
Nothing is typed in by hand, so a wrong airport (the Chicago O'Hare vs
Midway mistake of September) cannot come back.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
KALSHI = "https://external-api.kalshi.com/trade-api/v2"
TWC = "https://weather.com/kalshi/api/climate/primary?date={date}"
NWS = "https://api.weather.gov/stations/{icao}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "config" / "weather_stations.json"))
    ap.add_argument("--date", default="2026-09-27", help="a settled day for the TWC station list")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    s = requests.Session()
    s.headers.update({"User-Agent": "kalshibot/1.0 (personal research; station registry)"})

    twc = s.get(TWC.format(date=args.date), timeout=30).json()
    stations = {}
    for r in twc["results"]:
        st = r["station"]
        if not st.get("isDomestic", True):
            continue
        stations[st["icao"].upper()] = {"icao": st["icao"].upper(), "cli": st.get("cliId"),
                                        "city": st.get("city"), "timezone": st.get("timezone")}
    by_cli = {v["cli"]: v for v in stations.values()}

    # every Kalshi series that could be a daily temperature market
    cur, cands = None, []
    while True:
        r = s.get(KALSHI + "/series", params={"limit": 1000, **({"cursor": cur} if cur else {})},
                  timeout=30).json()
        for x in r.get("series") or []:
            t = x.get("ticker", "")
            if t.startswith(("KXHIGH", "KXLOW")) and x.get("frequency") == "daily":
                cands.append(t)
        cur = r.get("cursor")
        if not cur:
            break
    series = []
    for t in sorted(cands):
        for _ in range(3):
            resp = s.get(KALSHI + "/markets", params={"series_ticker": t, "limit": 20}, timeout=30)
            if resp.status_code != 429:
                break
            time.sleep(2)
        mk = resp.json().get("markets") or []
        rule = next((m.get("rules_primary") for m in mk if m.get("rules_primary")), "") or ""
        m = re.search(r"\(CLI([A-Z0-9]+)\)", rule)
        kind = "high" if "maximum" in rule.lower() else "low" if "minimum" in rule.lower() else None
        if not m or not kind or m.group(1) not in by_cli:
            continue
        recent = max((x.get("close_time") or "" for x in mk), default="")
        series.append({"ticker_prefix": t, "cli": m.group(1), "kind": kind, "last_close": recent[:10]})
        time.sleep(0.15)

    # exact coordinates from the NWS station record
    for icao, st in stations.items():
        try:
            j = s.get(NWS.format(icao=icao), timeout=30, headers={"Accept": "application/geo+json"}).json()
            lon, lat = j["geometry"]["coordinates"][:2]
            st.update(latitude=round(lat, 4), longitude=round(lon, 4),
                      name=j["properties"].get("name"),
                      timezone=j["properties"].get("timeZone") or st["timezone"])
        except Exception as exc:  # noqa: BLE001
            print(f"  ! no NWS record for {icao}: {exc}", file=sys.stderr)
        time.sleep(0.2)

    out_series = []
    for x in series:
        st = by_cli[x["cli"]]
        if "latitude" not in st:
            continue
        out_series.append({
            "ticker_prefix": x["ticker_prefix"], "kind": x["kind"],
            "label": f"{st['city']} daily {x['kind']}", "station": st["icao"],
            "climate_report": st["cli"], "latitude": st["latitude"],
            "longitude": st["longitude"], "timezone": st["timezone"],
            "last_market_close": x["last_close"]})
    traded = {x["station"] for x in out_series}
    out = {"built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "sources": ["Kalshi production /series + /markets rules_primary",
                       "weather.com/kalshi climate portal station list",
                       "api.weather.gov/stations"],
           "stations": sorted(stations.values(), key=lambda v: v["icao"]),
           "series": sorted(out_series, key=lambda v: (v["station"], v["kind"]))}
    print(f"{len(stations)} Kalshi settlement stations, {len(traded)} with daily markets, "
          f"{len(out_series)} daily series ({sum(x['kind'] == 'high' for x in out_series)} high, "
          f"{sum(x['kind'] == 'low' for x in out_series)} low)")
    print("no daily markets:", ", ".join(sorted(set(stations) - traded)))
    if args.dry_run:
        print(json.dumps(out["series"][:3], indent=1))
        return 0
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
