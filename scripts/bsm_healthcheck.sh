#!/usr/bin/env bash
set -euo pipefail

WEB_URL="${1:-http://127.0.0.1:5001/}"
NETWORK_PAT="${2:-bsm_network.py}"
WEB_PAT="${3:-bsm_web.py}"
LOG_TAG="bsm-healthcheck"

log() {
  logger -t "${LOG_TAG}" "$*"
}

web_ok=0
if curl -fsS --max-time 5 "${WEB_URL}" >/dev/null; then
  web_ok=1
fi

network_ok=0
if pgrep -f "${NETWORK_PAT}" >/dev/null; then
  network_ok=1
fi

web_proc_ok=0
if pgrep -f "${WEB_PAT}" >/dev/null; then
  web_proc_ok=1
fi

if [[ "${web_ok}" -eq 1 && "${network_ok}" -eq 1 && "${web_proc_ok}" -eq 1 ]]; then
  log "OK web=${WEB_URL} network_proc=1 web_proc=1"
  exit 0
fi

log "UNHEALTHY web_ok=${web_ok} network_proc=${network_ok} web_proc=${web_proc_ok}; restarting bsm services"
systemctl restart bsm-network.service bsm-web.service
sleep 2

if curl -fsS --max-time 5 "${WEB_URL}" >/dev/null && pgrep -f "${NETWORK_PAT}" >/dev/null && pgrep -f "${WEB_PAT}" >/dev/null; then
  log "RECOVERED after restart"
  exit 0
fi

log "FAILED recovery after restart"
exit 1
