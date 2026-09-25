#!/usr/bin/env python3
"""Run this FIRST. It proves, one step at a time, that your setup works.

    python scripts/check_connection.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.auth import AuthError, Signer               # noqa: E402
from kalshibot.client import KalshiClient, KalshiError     # noqa: E402
from kalshibot.config import ENDPOINTS, ConfigError, load_settings  # noqa: E402

OK, BAD, WARN = "  [OK]  ", " [FAIL] ", " [WARN] "


def identify_environment(signer: Signer, fix_env: bool = False) -> int:
    """Try the same key against BOTH Kalshi servers and report which one
    it belongs to.

    Demo and production are separate accounts with separate keys. A key from
    one will always 401 against the other, and the error message does not say
    so. This removes the guesswork.
    """
    print("\n=== which environment does this key belong to? ===\n")
    results = {}
    for env, urls in ENDPOINTS.items():
        client = KalshiClient(urls["rest"], signer)
        try:
            cents = client.get_balance_cents()
            results[env] = ("works", cents)
            print(OK, f"{env:<5} {urls['rest']}")
            print(f"         authenticated. balance ${cents/100:,.2f}")
        except KalshiError as exc:
            results[env] = ("failed", exc)
            label = "rejected the key" if exc.status in (401, 403) else f"error {exc.status}"
            print(BAD, f"{env:<5} {urls['rest']}")
            print(f"         {label}")

    working = [e for e, (state, _) in results.items() if state == "works"]
    print()
    if len(working) == 1:
        env = working[0]
        print(f"  ==> This is a {env.upper()} key.")
        if env == "prod":
            print("      That is your REAL MONEY account.")
            print("      I will not switch you to it automatically. If you mean to")
            print("      use it, set KALSHI_ENV=prod in .env yourself, and set")
            print("      execution.mode back to 'paper' in config.yaml first so")
            print("      nothing can be spent while you test.")
            return 2
        if fix_env:
            _write_env_value("KALSHI_ENV", "demo")
            print("      KALSHI_ENV=demo written to .env")
        else:
            print("      Set KALSHI_ENV=demo in .env")
        return 0
    if not working:
        print("  ==> Neither environment accepted this key. Most likely causes:")
        print("      1. The Key ID and the .pem file are not a matching pair.")
        print("         (Deleting and recreating BOTH on Kalshi fixes this - the")
        print("         Key ID and the .pem are issued together and must match.)")
        print("      2. The key was revoked or deleted on Kalshi.")
        print("      3. Your computer's clock is off by more than a few seconds.")
        print("         Check: System Settings > General > Date & Time > Set automatically")
        return 1
    print("  ==> Both worked, which is unusual. Staying on demo.")
    if fix_env:
        _write_env_value("KALSHI_ENV", "demo")
    return 0


def _write_env_value(key: str, value: str) -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    out, found = [], False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    env_path.write_text("\n".join(out) + "\n")
    env_path.chmod(0o600)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--try-both", action="store_true",
                    help="test the key against demo AND prod to see which it is")
    ap.add_argument("--fix-env", action="store_true",
                    help="with --try-both: write the detected environment into .env. "
                         "Only ever writes 'demo'; switching to real money stays manual.")
    args = ap.parse_args()

    print("\n=== kalshibot connection check ===\n")

    try:
        s = load_settings(str(Path(__file__).resolve().parents[1] / "config.yaml"))
    except ConfigError as exc:
        print(BAD, "config:", exc)
        return 1
    print(OK, f"config.yaml loaded. mode={s.mode}  env={s.kalshi_env}")
    print("       REST base:", s.rest_base)

    if not s.key_id:
        print(BAD, "KALSHI_KEY_ID is empty. Copy .env.example to .env and fill it in.")
        return 1
    print(OK, f"Key ID present ({s.key_id[:8]}...)")

    try:
        signer = Signer.from_settings(s)
        signer.sign("GET", "/trade-api/v2/portfolio/balance")
    except AuthError as exc:
        print(BAD, "private key:", exc)
        return 1
    print(OK, "private key loads and signs")

    if args.try_both:
        return identify_environment(signer, fix_env=args.fix_env)

    client = KalshiClient(s.rest_base, signer)

    try:
        status = client.exchange_status()
        print(OK, f"public endpoint reachable. exchange status: {status}")
    except KalshiError as exc:
        print(BAD, "cannot reach Kalshi:", exc)
        print("       Check your internet connection / firewall / VPN.")
        return 1

    try:
        cents = client.get_balance_cents()
        print(OK, f"AUTHENTICATED. Account balance: ${cents/100:,.2f}")
    except KalshiError as exc:
        print(BAD, "authenticated call failed:", exc)
        if exc.status in (401, 403):
            print("       Most common causes:")
            print("       1. KALSHI_ENV does not match where the key was created")
            print("          (a production key will NOT work against demo, and vice versa).")
            print("       2. The .pem file is not the one paired with this Key ID.")
            print("       3. Your computer's clock is off by more than a few seconds.")
            print()
            print("       Run this to find out which environment the key belongs to:")
            print("           python scripts/check_connection.py --try-both")
        return 1

    try:
        positions = client.get_positions()
        print(OK, f"positions endpoint works ({len(positions)} open)")
    except KalshiError as exc:
        print(WARN, "positions call failed:", exc)

    prefixes = [x["ticker_prefix"] for x in (s.get("weather.series") or [])]
    total = 0
    for p in prefixes:
        try:
            markets = client.get_markets(series_ticker=p, status="open")
        except KalshiError as exc:
            print(WARN, f"series {p}: {exc}")
            continue
        total += len(markets)
        flag = OK if markets else WARN
        print(flag, f"series {p:<12} {len(markets):>3} open markets")
        if markets:
            m = markets[0]
            print(f"          e.g. {m.ticker}  {m.subtitle or m.title}  "
                  f"yes {m.yes_bid}/{m.yes_ask}c")

    if total == 0:
        print(WARN, "No markets found. Ticker prefixes in config.yaml may have changed.")
        print("       Run: python scripts/find_series.py weather")

    print("\nAll green. Next: python run.py --once\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
