#!/usr/bin/env bash
# Install KALSHI LIVE VOLATILITY TRADER as its own isolated service.
#
# It creates ONLY these paths and touches nothing else on the host:
#   /opt/kalshi-live-volatility/{app,venv}   code + virtualenv
#   /etc/kalshi-live-volatility/             klvb.env (+ keys/ for LIVE later)
#   /var/lib/kalshi-live-volatility/         database, kill-switch file
#   /var/log/kalshi-live-volatility/         JSON logs
#   /etc/systemd/system/kalshi-live-volatility.service
# and a dedicated system user `klvb`. It never reads, stops, restarts or edits any
# other service, directory, database or env file.
#
# Usage (as root, from the repository checkout):
#   sudo bash kalshi_live_volatility_bot/deploy/install.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX=/opt/kalshi-live-volatility
ETC=/etc/kalshi-live-volatility
DATA=/var/lib/kalshi-live-volatility
LOGS=/var/log/kalshi-live-volatility
UNIT=/etc/systemd/system/kalshi-live-volatility.service

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }
command -v python3 >/dev/null || { echo "python3 required"; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'

if [[ -e "$UNIT" ]] && ! grep -q "Kalshi Live Volatility Trader" "$UNIT"; then
  echo "refusing: $UNIT exists and does not belong to this bot"; exit 1
fi

id klvb >/dev/null 2>&1 || useradd --system --home "$PREFIX" --shell /usr/sbin/nologin klvb
install -d -o root -g root -m 755 "$PREFIX"
install -d -o klvb -g klvb -m 750 "$DATA" "$LOGS"
install -d -o root -g klvb -m 750 "$ETC" "$ETC/keys"

rsync -a --delete --exclude data --exclude logs --exclude 'config/local.toml' --exclude '.env' \
      --exclude '__pycache__' "$SRC/" "$PREFIX/app/"
python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install -q --upgrade pip
"$PREFIX/venv/bin/pip" install -q "$PREFIX/app[live]"

if [[ ! -f "$ETC/klvb.env" ]]; then
  install -o root -g klvb -m 640 "$SRC/.env.example" "$ETC/klvb.env"
  echo "created $ETC/klvb.env (TRADING_MODE=PAPER)"
fi

install -m 644 "$SRC/deploy/kalshi-live-volatility.service" "$UNIT"
systemctl daemon-reload
systemctl enable kalshi-live-volatility.service
systemctl restart kalshi-live-volatility.service
sleep 3
systemctl --no-pager status kalshi-live-volatility.service | head -15
echo
echo "Installed. Dashboard: ssh -L 8765:127.0.0.1:8765 <you>@<vps>  then open http://localhost:8765"
echo "Logs:     journalctl -u kalshi-live-volatility -f     (or $LOGS/klvb.jsonl)"
