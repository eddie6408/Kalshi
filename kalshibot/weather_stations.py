"""Which weather series and stations the bot covers - one answer for every
part of it (estimator, scoring, settlement checks, dashboard).

    weather.series         hand-written entries in config.yaml (win on a clash)
    weather.stations_file  config/weather_stations.json, built from Kalshi's own
                           contract rules, The Weather Company's station list and
                           the NWS station records (scripts/build_weather_stations.py):
                           all 39 settlement stations, 48 daily high/low series.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _file(weather_cfg: dict) -> dict:
    path = (weather_cfg or {}).get("stations_file")
    if not path:
        return {}
    p = Path(path) if Path(path).is_absolute() else ROOT / path
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_series(weather_cfg: dict | None) -> list[dict]:
    cfg = weather_cfg or {}
    inline = list(cfg.get("series") or [])
    seen = {str(s.get("ticker_prefix", "")).upper() for s in inline}
    extra = [s for s in _file(cfg).get("series") or []
             if str(s.get("ticker_prefix", "")).upper() not in seen]
    return inline + extra


def load_stations(weather_cfg: dict | None) -> dict[str, str]:
    """{ICAO: climate-report id} for every settlement station - including the
    ones with no daily market, which are still recorded for scoring and the
    blend fit."""
    out = {str(s["icao"]).upper(): str(s.get("cli") or "")
           for s in _file(weather_cfg or {}).get("stations") or [] if s.get("icao")}
    for s in load_series(weather_cfg):
        if s.get("station"):
            out.setdefault(str(s["station"]).upper(), str(s.get("climate_report") or ""))
    return out
