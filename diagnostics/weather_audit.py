#!/usr/bin/env python3
"""Is the weather estimator's distribution actually coherent?

A forecast bot is only as good as its probabilities, and a probability is easy
to get wrong in ways that look fine one market at a time. This prints, for one
weather event, EVERY market side by side:

    what Kalshi says the strike is   ->   how we parsed it
    what we think the probability is ->   what the market is paying
    and the ensemble evidence behind our number

Then it checks the distribution against itself:

  * thresholds must be monotonic - P(>=82) can never exceed P(>=81)
  * mutually exclusive buckets must not sum above 1
  * a bucket cannot be less likely than a threshold it fully contains

Those checks are what separate "our model disagrees with the market" from
"our model disagrees with arithmetic". Only the first is a trading edge.

    python diagnostics/weather_audit.py KXHIGHLAX
    python diagnostics/weather_audit.py KXHIGHNY --date 26SEP14
    python diagnostics/weather_audit.py              # every configured city
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.estimators.registry import ESTIMATORS    # noqa: E402
from kalshibot.auth import Signer                       # noqa: E402
from kalshibot.coherence import Claim, check            # noqa: E402
from kalshibot.clients import MarketDataClient          # noqa: E402
from kalshibot.config import load_settings              # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
B, D, G, R, Y, C, X = ("\033[1m", "\033[2m", "\033[32m", "\033[31m",
                       "\033[33m", "\033[36m", "\033[0m")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("series", nargs="*", default=[],
                    help="e.g. KXHIGHLAX KXHIGHNY. Default: every city, in "
                         "the order the estimator lists them.")
    ap.add_argument("--date", default="",
                    help="event date fragment, e.g. 26SEP14")
    args = ap.parse_args()

    s = load_settings(str(ROOT / "config.yaml"))
    client = MarketDataClient(s.rest_base, Signer.from_settings(s))
    estimator = ESTIMATORS["weather_temperature"](s)

    wanted = args.series or estimator.candidate_series()
    exit_code = 0

    for series in wanted:
        markets = [m for m in client.get_markets(status="open",
                                                 series_ticker=series)
                   if args.date in m.event_ticker]
        if not markets:
            print(f"\n{Y}{series}: no markets returned{X}")
            continue

        by_event: dict[str, list] = {}
        for m in markets:
            by_event.setdefault(m.event_ticker, []).append(m)

        for event, ms in sorted(by_event.items()):
            exit_code |= _audit_event(client, estimator, event, ms)
    return exit_code


def _audit_event(client, estimator, event, markets) -> int:
    print(f"\n{C}{B}{'=' * 78}{X}")
    print(f"{C}{B}{event}{X}  ({len(markets)} markets)")
    print(f"{C}{B}{'=' * 78}{X}")

    # Kalshi's own settlement rule, verbatim. Our forecast is only as good as
    # the station it is aimed at: if Kalshi settles on a different station
    # than config.yaml points at, every probability is for the wrong place -
    # and an "already observed" high from the wrong station is worse than no
    # observation at all, because it looks like certainty.
    rules = (markets[0].raw.get("rules_primary")
             or markets[0].raw.get("rules_secondary") or "")
    if rules:
        print(f"\n{D}  settles on : {rules.strip()[:300]}{X}")

    rows, evidence_shown = [], False
    for m in sorted(markets, key=lambda x: x.ticker):
        lo, hi, label = estimator.parse_strike(m)
        est = estimator.estimate(m)
        book = client.get_depth(m.ticker)
        yes_ask = book.best_price("yes", "buy") if book else None

        if est.usable and not evidence_shown and rules:
            for line in _station_verdict(_cfg_station(estimator, m), rules):
                print(f"    {line}")

        if est.usable and not evidence_shown:
            ev = est.evidence
            print(f"\n{D}  ensemble : {ev.get('ensemble_members')} members, "
                  f"mean {ev.get('ensemble_mean_f')}F, "
                  f"spread {ev.get('ensemble_spread_f')}F, "
                  f"sigma {ev.get('sigma_used_f')}F{X}")
            print(f"{D}  observed so far: {ev.get('observed_so_far_f')}F   "
                  f"lead {ev.get('lead_hours')}h   "
                  f"source {est.source}{X}\n")
            evidence_shown = True

        print(f"  {m.ticker:<28} {D}strike_type={m.strike_type or '-':<10} "
              f"floor={_n(m.floor_strike):<7} cap={_n(m.floor_strike if False else m.cap_strike):<7}{X}")
        print(f"    {D}subtitle : {m.subtitle or '(none)'}{X}")
        print(f"    parsed   : {label:<18} "
              f"{'[UNPARSED]' if label == 'unparsed' else ''}")
        if est.usable:
            mkt = f"{yes_ask}c" if yes_ask else "no book"
            print(f"    ours     : {G}{est.probability_yes * 100:5.1f}% YES{X}"
                  f"   market: {mkt:<8}"
                  f"   confidence {est.confidence:.2f}")
            rows.append((m.ticker, lo, hi, label, est.probability_yes))
        else:
            print(f"    {Y}skipped  : {est.skip_reason}{X}")

    return _coherence(rows)


def _coherence(rows) -> int:
    """The part that catches real bugs. Uses the SAME module the live strategy
    uses, so the diagnostic and the bot can never disagree."""
    print(f"\n{B}  coherence checks{X}")
    if not rows:
        print(f"  {Y}nothing priced, nothing to check{X}")
        return 0

    claims = [Claim(t, lo, hi, p) for t, lo, hi, _label, p in rows]
    problems = check(claims)
    if problems:
        print(f"  {R}{B}INCOHERENT - the bot will refuse this whole event{X}")
        for pr in problems:
            print(f"    {R}- {pr}{X}")
        print(f"\n  {D}An incoherent distribution means the strike parsing or "
              f"the probability\n  model is wrong, not that the market is "
              f"mispriced. Fix it before trading.{X}")
        return 1

    buckets = [c for c in claims if c.is_bucket]
    if buckets:
        total = sum(c.probability for c in buckets)
        print(f"  {G}OK{X}  {len(buckets)} buckets sum to {total * 100:.1f}%")
    thresholds = [c for c in claims if c.is_threshold]
    if thresholds:
        print(f"  {G}OK{X}  {len(thresholds)} thresholds are monotonic")
    return 0


#: Kalshi's settlement rules name CLIMATE report identifiers (CLIMDW), not
#: the METAR station codes our config uses (KMDW). They are not always the
#: same airport: Chicago settles on CLIMDW = MIDWAY, while the obvious guess
#: is O'Hare. Getting this wrong makes every probability for that city a
#: forecast for somewhere else.
CLI_TO_STATION = {
    "CLINYC": "KNYC", "CLIMDW": "KMDW", "CLIORD": "KORD", "CLIDEN": "KDEN",
    "CLIAUS": "KAUS", "CLIATT": "KATT", "CLIMIA": "KMIA", "CLILAX": "KLAX",
    "CLIPHL": "KPHL", "CLIBOS": "KBOS", "CLIDCA": "KDCA", "CLIHOU": "KHOU",
    "CLIIAH": "KIAH", "CLIPHX": "KPHX", "CLISEA": "KSEA", "CLISFO": "KSFO",
    "CLIATL": "KATL", "CLIDFW": "KDFW", "CLILAS": "KLAS", "CLIMSP": "KMSP",
}


def _station_verdict(ours: str, rules: str) -> list[str]:
    """Compare the station we forecast for against the one Kalshi settles on.

    Silence is not allowed here. If the rule text names no station we can
    recognise, that is itself a finding - it means this check is not
    protecting anything and nobody would know.
    """
    import re as _re
    cli = _re.findall(r"\b(CLI[A-Z]{3})\b", rules)
    metar = _re.findall(r"\b(K[A-Z]{3})\b", rules)
    expected = {CLI_TO_STATION.get(c) for c in cli} | set(metar)
    expected.discard(None)

    out = []
    if not ours:
        return [f"{Y}no station configured for this series{X}"]
    if not cli and not metar:
        return [f"{Y}could not find a station in the settlement rule - "
                f"this check verified NOTHING{X}"]
    if cli and not expected:
        return [f"{Y}settlement names {', '.join(cli)}, which is not in "
                f"CLI_TO_STATION - add it before trusting this city{X}"]
    if ours not in expected:
        out.append(f"{R}{B}STATION MISMATCH{X} {R}we forecast {ours}, Kalshi "
                   f"settles on {', '.join(sorted(expected))} "
                   f"({', '.join(cli or metar)}){X}")
        out.append(f"{R}every probability for this city is for the wrong "
                   f"place until config.yaml is corrected{X}")
    else:
        out.append(f"{G}station {ours} matches settlement "
                   f"({', '.join(cli or metar)}){X}")

    # The settlement PROVIDER matters too, and it is not the one we read.
    if "weather company" in rules.lower():
        out.append(f"{D}settles per The Weather Company; our observations come "
                   f"from NWS api.weather.gov - same station, different "
                   f"provider, so treat an observed extreme as close, not exact{X}")
    return out


def _cfg_station(estimator, market) -> str:
    cfg = estimator._series_for(market) or {}
    return str(cfg.get("station") or "")


def _n(v):
    return "-" if v is None else f"{v:g}"


if __name__ == "__main__":
    raise SystemExit(main())
