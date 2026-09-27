#!/bin/bash
# Install / update the service. Run as root on the droplet:
#   sudo bash /opt/kalshi/live-volatility/deploy/install.sh
# Touches ONLY kalshi-live-volatility.service - never kalshibot.service.
set -euo pipefail
APP=/opt/kalshi/live-volatility
UNIT=kalshi-live-volatility.service
install -m 0644 "$APP/deploy/$UNIT" "/etc/systemd/system/$UNIT"
systemctl daemon-reload
systemctl enable "$UNIT"
echo "installed $UNIT (start with: systemctl start $UNIT)"
