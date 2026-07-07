#!/bin/bash
# Read-only simulated market health probe. Writes logs only; does not place or modify orders.
set -euo pipefail

ROOT="${TRADINGAGENT_ROOT:-/opt/investment/tradingagent}"
PYTHON_BIN="${TRADINGAGENT_PYTHON:-/opt/marketgraph/venv/bin/python3}"
TIMEOUT="${TRADINGAGENT_SIM_HEALTH_TIMEOUT:-120}"
LOG_DIR="${ROOT}/shared/logs/cron"
LOCK_DIR="${ROOT}/shared/logs/locks"
LOG_FILE="${LOG_DIR}/sim_market_health.log"
LOCK_FILE="${LOCK_DIR}/sim_market_health.lock"

mkdir -p "${LOG_DIR}" "${LOCK_DIR}"

exec 200>"${LOCK_FILE}"
if ! flock -n 200; then
  echo "[$(date -Iseconds)] SKIP sim_market_health already running" >> "${LOG_FILE}"
  exit 0
fi

cd "${ROOT}"

if [ -f "${ROOT}/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT}/.env"
  set +a
fi

export SHAREDSIGNALS_API_URL="${SHAREDSIGNALS_API_URL:-http://127.0.0.1:8082}"
export SHARED_SIGNALS_DB="${SHARED_SIGNALS_DB:-/opt/investment/MarketGraphRuntime/read_model/marketdata.sqlite}"
export PYTHONPATH="${ROOT}:/opt/investment/SharedSignals:${PYTHONPATH:-}"

{
  echo "[$(date -Iseconds)] START sim_market_health"
  timeout "${TIMEOUT}" "${PYTHON_BIN}" shared/runtime_test/market_health.py --market sim --write-latest
  echo "[$(date -Iseconds)] OK sim_market_health"
} >> "${LOG_FILE}" 2>&1
