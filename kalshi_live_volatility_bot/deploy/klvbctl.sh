#!/usr/bin/env bash
# Convenience wrapper: start | stop | restart | status | logs | kill | unkill | report | readiness
# Operates ONLY on kalshi-live-volatility.service.
set -euo pipefail
SVC=kalshi-live-volatility.service
KLVB="sudo -u klvb env KLVB_DATA_DIR=/var/lib/kalshi-live-volatility KLVB_LOG_DIR=/var/log/kalshi-live-volatility /opt/kalshi-live-volatility/venv/bin/klvb"
case "${1:-}" in
  start|stop|restart|status) sudo systemctl "$1" "$SVC" ;;
  logs) sudo journalctl -u "$SVC" -f -n 200 ;;
  kill) shift; $KLVB kill --reason "${*:-manual}" ;;
  unkill) $KLVB unkill ;;
  report) shift; $KLVB report "$@" ;;
  readiness) $KLVB readiness ;;
  *) echo "usage: $0 {start|stop|restart|status|logs|kill [reason]|unkill|report [--env PAPER]|readiness}"; exit 1 ;;
esac
