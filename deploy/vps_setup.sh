#!/bin/bash
# STEP 3 - run this ON THE DROPLET as the kalshi user, after the transfer.
#
#   ssh kalshi@143.198.124.232
#   bash ~/kalshi-bot/deploy/vps_setup.sh
#
# Builds the Linux venv, fixes permissions on the secrets, runs the self-test,
# and installs the systemd service. It does NOT start the bot - see the note
# about running two bots at the end.
set -euo pipefail

BOT="$HOME/kalshi-bot"
cd "$BOT"

echo "==> 1/5  Python venv (Ubuntu 24.04 ships 3.12; the Mac had 3.14)"
rm -rf .venv
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip --quiet
./.venv/bin/pip install -r requirements.txt --quiet
echo "    $(./.venv/bin/python --version)"

echo "==> 2/5  Secrets"
# rsync preserves modes, but assert them anyway: this is a public server.
chmod 600 .env 2>/dev/null || true
find . -maxdepth 2 -name '*.pem' -exec chmod 600 {} \;
chmod 700 "$BOT"
ls -l .env *.pem 2>/dev/null | awk '{print "    "$1" "$NF}'

echo "==> 3/5  Self-test"
OUT="$(./.venv/bin/python tests/run_tests.py 2>&1 | tail -1)"
case "$OUT" in
  *"0 failed"*) echo "    $OUT" ;;
  *) echo "    FAILED: $OUT"; echo "    Stopping. Do not run a bot that does not pass its own tests."; exit 2 ;;
esac

echo "==> 4/5  Environment and production lockout"
./.venv/bin/python - <<'PYEOF'
import sys
sys.path.insert(0, ".")
from kalshibot.config import load_settings
from kalshibot.env_guard import assert_demo_environment, assert_demo_url, describe_guard
s = load_settings("config.yaml")
assert_demo_environment(s.kalshi_env)
assert_demo_url(s.rest_base, "configured REST endpoint")
if not s.key_id or not s.private_key_path.exists():
    print(f"    !! key file missing: {s.private_key_path}")
    raise SystemExit(1)
print(f"    environment {s.kalshi_env}, endpoint ok, key file present")
print("    " + describe_guard().replace("\n", "\n    "))
PYEOF

echo "==> 5/5  systemd service"
sed -e "s|/home/kalshi|$HOME|g" deploy/kalshibot.service > /tmp/kalshibot.service
sudo cp /tmp/kalshibot.service /etc/systemd/system/kalshibot.service
sudo systemctl daemon-reload
sudo systemctl enable kalshibot
echo "    installed and enabled (not started)"

cat <<'NOTE'

  ---------------------------------------------------------------
  BEFORE YOU START IT: stop the bot on the Mac.

  Two bots against one Kalshi account is not a redundancy, it is a
  double-trade. They would both see room under the per-market limit,
  both buy, and each would keep a journal that disagrees with the
  other and with the exchange. The reduce-only clamp cannot save you
  from that - it protects against selling what you do not hold, not
  against two processes both holding an opinion.

  On the Mac:
      launchctl unload ~/Library/LaunchAgents/com.kalshibot.plist 2>/dev/null
      pkill -f arb.py

  Then here:
      sudo systemctl start kalshibot
      journalctl -u kalshibot -f
  ---------------------------------------------------------------
NOTE
