#!/bin/bash
# Append simulated equity snapshots for the dashboard. No order, signal, email or real-capital writes.
set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${WRAPPER_DIR}/_common.sh"

ROOT="${TRADINGAGENT_ROOT:-/opt/investment/tradingagent}"
PYTHON_BIN="${TRADINGAGENT_PYTHON:-/opt/tradingagent/venv/bin/python3}"
TIMEOUT="${TRADINGAGENT_EQUITY_SNAPSHOT_TIMEOUT:-120}"
TARGET_RETURN_PCT="${TRADINGAGENT_DASHBOARD_TARGET_RETURN_PCT:-8}"
MARKETS="crypto,pm,us,cn_futures"
LOG_DIR="${ROOT}/shared/logs/cron"
LOCK_DIR="${ROOT}/shared/logs/locks"
LOG_FILE="${LOG_DIR}/equity_snapshots.log"
LOCK_FILE="${LOCK_DIR}/equity_snapshots.lock"

mkdir -p "${LOG_DIR}" "${LOCK_DIR}"

exec 200>"${LOCK_FILE}"
if ! flock -n 200; then
  echo "[$(date -Iseconds)] SKIP equity_snapshots already running" >> "${LOG_FILE}"
  exit 0
fi

cd "${ROOT}"

export SHAREDSIGNALS_API_URL="${SHAREDSIGNALS_API_URL:-http://127.0.0.1:8082}"
export PYTHONPATH="${ROOT}:${PYTHONPATH:-}"

{
  echo "[$(date -Iseconds)] START equity_snapshots"
  timeout "${TIMEOUT}" "${PYTHON_BIN}" shared/runtime_test/write_equity_snapshots.py --markets "${MARKETS}" --target-return-pct "${TARGET_RETURN_PCT}"
  echo "[$(date -Iseconds)] OK equity_snapshots"
} >> "${LOG_FILE}" 2>&1
