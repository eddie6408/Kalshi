#!/bin/bash
# Non-interactive entry point for launchd. Not for double-clicking.
#
# run_bot.sh is the human path: seven preflight checks, then a Ctrl+C restart
# prompt at the end. That prompt blocks forever under launchd, so this runs
# the checks that matter without it and then hands over to the bot.
#
# The self-test runs FIRST and a failure exits non-zero. launchd throttles the
# restart, so a broken build becomes a slow retry loop with the reason in
# logs/launchd.err.log - rather than a bot trading on code that does not pass
# its own tests for a fortnight.
set -u
cd "$(dirname "$0")/.." || exit 1

PY="./.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
[ -n "$PY" ] || { echo "no python found"; exit 1; }

echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) supervised start ==="

# 1. Self-test. The startup gate, same as run_bot.sh uses.
OUT="$("$PY" tests/run_tests.py 2>&1 | tail -1)"
case "$OUT" in
  *"0 failed"*) echo "self-test: $OUT" ;;
  *) echo "SELF-TEST FAILED: $OUT"; echo "refusing to start"; exit 2 ;;
esac

# 2. Credentials and the demo lockout. Cheap, and catches a half-edited .env.
"$PY" - <<'PYEOF' || exit 3
import sys
sys.path.insert(0, ".")
from kalshibot.config import load_settings
from kalshibot.env_guard import assert_demo_environment, assert_demo_url
s = load_settings("config.yaml")
assert_demo_environment(s.kalshi_env)
assert_demo_url(s.rest_base, "configured REST endpoint")
if not s.key_id or not s.private_key_path.exists():
    print("missing demo credentials"); raise SystemExit(1)
print(f"environment: {s.kalshi_env}  endpoint ok  credentials present")
PYEOF

# 3. Hand over. exec so launchd supervises the bot itself, not this shell.
echo "starting bot: --mode DEMO_LIVE --loop --forecast"
exec "$PY" arb.py --mode DEMO_LIVE --loop --forecast
