#!/usr/bin/env python3
"""Dump what the bot actually sees, so a human can look at it.

    python scripts/diagnose.py

Writes data/diagnostics.json and prints a summary. Contains no credentials -
safe to share. This is the thing to run when the bot says it found markets but
never trades.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.auth import Signer                                   # noqa: E402
from kalshibot.client import KalshiClient, KalshiError              # noqa: E402
from kalshibot.config import load_settings                          # noqa: E402
from kalshibot.estimators.weather import WeatherTemperatureEstimator  # noqa: E402
from kalshibot.weatherdata import WeatherData                       # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    s = load_settings(str(ROOT / "config.yaml"))
    client = KalshiClient(s.rest_base, Signer.from_settings(s))
    est = WeatherTemperatureEstimator(s.section("weather"), WeatherData())

    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "environment": s.kalshi_env,
        "mode": s.mode,
        "series": [],
    }

    print(f"\nInspecting {s.kalshi_env} environment...\n")
    total = quoted = 0

    for cfg in s.get("weather.series") or []:
        prefix = cfg["ticker_prefix"]
        try:
            markets = client.get_markets(series_ticker=prefix, status="open")
        except KalshiError as exc:
            report["series"].append({"prefix": prefix, "error": str(exc)})
            print(f"  {prefix:<12} ERROR {exc}")
            continue

        entry = {"prefix": prefix, "count": len(markets), "markets": []}
        with_book = 0
        for m in markets:
            lo, hi, label = est.parse_strike(m)
            date = est.parse_event_date(m, cfg.get("timezone", "America/New_York"))
            has_book = bool(m.yes_bid or m.yes_ask or m.no_bid or m.no_ask)
            if has_book:
                with_book += 1
            entry["markets"].append({
                "ticker": m.ticker,
                "subtitle": m.subtitle,
                "title": m.title,
                "status": m.status,
                "strike_type": m.strike_type,
                "floor_strike": m.floor_strike,
                "cap_strike": m.cap_strike,
                "parsed_lo": lo,
                "parsed_hi": hi,
                "parsed_label": label,
                "parsed_date": date.isoformat() if date else None,
                "yes_bid": m.yes_bid, "yes_ask": m.yes_ask,
                "no_bid": m.no_bid, "no_ask": m.no_ask,
                "volume": m.volume,
                "open_interest": m.open_interest,
                "liquidity": m.liquidity,
                "close_time": m.close_time.isoformat() if m.close_time else None,
                "minutes_to_close": round(m.minutes_to_close(), 1),
                "has_orderbook": has_book,
            })
        total += len(markets)
        quoted += with_book
        entry["with_orderbook"] = with_book
        report["series"].append(entry)
        print(f"  {prefix:<12} {len(markets):>3} markets, {with_book:>3} with any quote")

    # Sample one real orderbook so we can see actual depth.
    report["sample_orderbooks"] = []
    for entry in report["series"]:
        for m in entry.get("markets", [])[:2]:
            try:
                ob = client.get_orderbook(m["ticker"])
                report["sample_orderbooks"].append({
                    "ticker": m["ticker"],
                    "yes_levels": [[l.price_cents, l.contracts] for l in ob.yes],
                    "no_levels": [[l.price_cents, l.contracts] for l in ob.no],
                })
            except KalshiError as exc:
                report["sample_orderbooks"].append({"ticker": m["ticker"],
                                                    "error": str(exc)})
        break

    # Can we actually get a forecast?
    report["forecast_check"] = {}
    first = next((c for c in (s.get("weather.series") or [])), None)
    if first:
        try:
            import datetime as dt
            fc = est.data.daily_extreme_forecast(
                latitude=first["latitude"], longitude=first["longitude"],
                tz_name=first.get("timezone", "America/New_York"),
                target_date=dt.date.today(), kind="high", station=first.get("station"))
            report["forecast_check"] = {
                "ok": fc is not None,
                "source": fc.source if fc else None,
                "members": fc.n_members if fc else 0,
                "mean_f": round(fc.mean, 2) if fc else None,
                "spread_f": round(fc.spread, 2) if fc else None,
            }
        except Exception as exc:  # noqa: BLE001
            report["forecast_check"] = {"ok": False, "error": str(exc)}

    out = ROOT / "data" / "diagnostics.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2))

    print(f"\n  {total} markets total, {quoted} with any quote at all")
    f = report["forecast_check"]
    print(f"  weather forecast: {'OK - ' + str(f.get('members')) + ' ensemble members' if f.get('ok') else 'FAILED: ' + str(f.get('error', 'no data'))}")
    print(f"\n  Written to {out}")
    print("  It contains no credentials. Share it and I can tell you what is wrong.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
