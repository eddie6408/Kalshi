#!/usr/bin/env bash
# =====================================================================
#  ONE COMMAND. Verifies everything, then runs the bot continuously.
#
#      bash run_bot.sh                        # SCAN_ONLY (finds, never trades)
#      bash run_bot.sh DEMO_LIVE              # submits orders to Kalshi DEMO
#      bash run_bot.sh SCAN_ONLY forecast     # + forecast strategies, no orders
#      bash run_bot.sh DEMO_LIVE forecast     # + forecast strategies, real demo orders
#
#  "forecast" adds opinion-based trades, which have NO guaranteed payout. It
#  applies to that run only - config.yaml is never modified.
#
#  Demo only. Production hosts are refused by the application itself.
# =====================================================================
set -uo pipefail
cd "$(dirname "$0")"

MODE="${1:-${TRADING_MODE:-SCAN_ONLY}}"
FORECAST=""
[ "${2:-}" = "forecast" ] && FORECAST="--forecast"
B=$'\033[1m'; D=$'\033[2m'; G=$'\033[32m'; R=$'\033[31m'
Y=$'\033[33m'; C=$'\033[36m'; O=$'\033[0m'

ok()   { printf "  ${G}OK${O}    %s\n" "$*"; }
warn() { printf "  ${Y}--${O}    %s\n" "$*"; }
die()  { printf "\n${R}${B}STOPPED${O}  %s\n\n" "$*"; printf "Press Return to close."; read -r _; exit 1; }
hdr()  { printf "\n${C}${B}%s${O}\n" "$*"; }

clear
printf "${C}${B}%s${O}\n" "=================================================="
printf "${C}${B}%s${O}\n" "   KALSHI BOT  -  startup"
printf "${C}${B}%s${O}\n" "=================================================="

# ---------------------------------------------------- 1/7 python
hdr "1/7  Python"
command -v python3 >/dev/null || die "python3 not found. Install from python.org"
python3 - <<'PY' || die "Python 3.10 or newer is required."
import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)
PY
ok "python3 $(python3 -c 'import platform;print(platform.python_version())')"

# ---------------------------------------------------- 2/7 venv
hdr "2/7  Environment"
if [ -d .venv ] && .venv/bin/python -c "import sys" >/dev/null 2>&1; then
  ok "virtual environment present"
else
  [ -d .venv ] && { warn "existing .venv does not run; rebuilding"; rm -rf .venv; }
  python3 -m venv .venv || die "could not create the virtual environment"
  ok "virtual environment created"
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# ---------------------------------------------------- 3/7 deps
hdr "3/7  Dependencies"
if python -c "import requests,cryptography,yaml,dotenv" >/dev/null 2>&1; then
  ok "all dependencies present"
else
  warn "installing (first run, takes a minute)"
  python -m pip install --upgrade pip --quiet >/dev/null 2>&1
  if python -m pip install -r requirements.txt --quiet >/tmp/kalshibot_pip.log 2>&1; then
    ok "dependencies installed"
  else
    printf "${D}%s${O}\n" "$(tail -3 /tmp/kalshibot_pip.log)"
    die "could not install dependencies.
        Most likely no internet, or a VPN/firewall blocking pypi.org.
        Full log: /tmp/kalshibot_pip.log"
  fi
fi

# ---------------------------------------------------- 4/7 self-test
hdr "4/7  Self-test"
TESTOUT=$(python tests/run_tests.py 2>&1 | tail -1)
case "$TESTOUT" in
  *"0 failed"*) ok "$TESTOUT" ;;
  *) printf "  ${R}!!${O}    %s\n" "$TESTOUT"
     die "the self-test failed. Do not trade. Run: bash tests/run_tests.sh" ;;
esac

# ---------------------------------------------------- 5/7 credentials
hdr "5/7  Credentials and safety"
[ -f .env ] || die "no .env file. Copy .env.example to .env and fill it in."
grep -qE "^KALSHI_ENV=demo" .env || die "KALSHI_ENV must be demo in .env"
grep -qE "^KALSHI_KEY_ID=[0-9a-fA-F]{8}-" .env \
  || die "KALSHI_KEY_ID in .env is not a UUID. Paste the Key ID from Kalshi."
ok "KALSHI_ENV=demo"
python - <<'PY' || die "the production lockout self-check failed"
import sys; sys.path.insert(0, ".")
from kalshibot.env_guard import ProductionBlocked, assert_demo_url
for host in ("external-api.kalshi.com", "api.elections.kalshi.com", "kalshi.com"):
    try:
        assert_demo_url(f"https://{host}/trade-api/v2"); raise SystemExit(1)
    except ProductionBlocked:
        pass
assert_demo_url("https://external-api.demo.kalshi.co/trade-api/v2")
PY
ok "production endpoints are blocked"

# ---------------------------------------------------- 6/7 connection
hdr "6/7  Kalshi demo connection"
BAL=$(python - <<'PY'
import sys; sys.path.insert(0, ".")
try:
    from kalshibot.auth import Signer
    from kalshibot.clients import PortfolioClient
    from kalshibot.config import load_settings
    s = load_settings("config.yaml")
    print(f"{PortfolioClient(s.rest_base, Signer.from_settings(s)).balance_usd():.2f}")
except Exception as exc:
    print(f"ERR {exc}")
PY
)
case "$BAL" in
  ERR*) die "could not reach the Kalshi demo account.
        ${BAL#ERR }
        If this is a 401, the Key ID and the .pem may not be a matching pair." ;;
  *) ok "authenticated. demo balance \$$BAL" ;;
esac

# ---------------------------------------------------- 7/7 go
hdr "7/7  Starting"
if [ "$MODE" = "DEMO_LIVE" ]; then
  printf "\n${R}${B}%s${O}\n" "=========================================="
  printf "${R}${B}%s${O}\n"   "   KALSHI DEMO LIVE TRADING"
  printf "${R}${B}%s${O}\n"   "   SIMULATED FUNDS ONLY"
  printf "${R}${B}%s${O}\n\n" "=========================================="
else
  ok "mode SCAN_ONLY - it will find opportunities but never place an order"
  printf "${D}        to let it trade demo money: close this and run\n"
  printf "        'START BOT (DEMO LIVE).command' instead${O}\n"
fi
if [ -n "$FORECAST" ]; then
  printf "\n${Y}${B}  FORECAST STRATEGIES ON${O}\n"
  printf "${Y}  The bot will estimate probabilities from live data and trade\n"
  printf "  where it disagrees with the market. These trades are NOT\n"
  printf "  guaranteed - unlike structural arbitrage, they can lose.${O}\n"
  printf "${D}  This run only. config.yaml is unchanged.${O}\n"
fi
printf "\n${D}  Open 'WATCH BOT.command' in another window to follow along.\n"
printf "  Press Ctrl+C here to stop the bot.${O}\n\n"
sleep 2

# Ctrl+C stops the bot. There is no keystroke that can un-stop it - the
# process is gone by then and nothing is left listening. So the window stays
# open and offers to start it again, which is the same thing one keypress
# later and survives a crash as well as a deliberate stop.
RUNS=0
while true; do
  RUNS=$((RUNS + 1))
  TRADING_MODE="$MODE" python arb.py --mode "$MODE" --loop $FORECAST
  CODE=$?

  printf "\n${D}────────────────────────────────────────────────────────${O}\n"
  if [ "$CODE" -eq 0 ] || [ "$CODE" -eq 130 ]; then
    printf "${D}  Bot stopped (run #%d).${O}\n" "$RUNS"
  else
    printf "${R}  Bot exited with code %d (run #%d).${O}\n" "$CODE" "$RUNS"
    printf "${D}  Check logs/kalshibot.log before restarting.${O}\n"
  fi
  printf "\n  ${B}R${O} then Return  restart          "
  printf "${B}W${O} then Return  watch the dashboard\n"
  printf "  ${B}Q${O} then Return  quit            "
  printf "${D}(Return on its own also quits)${O}\n\n"

  read -r -p "  > " ANSWER
  case "$(printf "%s" "$ANSWER" | tr "[:upper:]" "[:lower:]")" in
    r|restart)
      printf "\n${G}  restarting...${O}\n\n"
      sleep 1
      ;;
    w|watch)
      python scripts/monitor.py --db data/journal.db --interval 3
      ;;
    *)
      printf "\n${D}  closing.${O}\n"
      break
      ;;
  esac
done
