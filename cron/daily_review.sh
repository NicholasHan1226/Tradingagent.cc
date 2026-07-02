#!/bin/bash
# Run TradingAgent daily review across all markets represented in shadow logs.
TIMEOUT="${TRADINGAGENT_CRON_TIMEOUT:-1800}"
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${TRADINGAGENT_PYTHON:-python3}"
TRADE_DATE="${TRADINGAGENT_REVIEW_DATE:-$(date +%Y%m%d)}"
SESSION="${TRADINGAGENT_REVIEW_SESSION:-close}"
LOG_DIR="${ROOT}/shared/logs/cron"
LOCK_DIR="${ROOT}/shared/logs/locks"
LOG_FILE="${LOG_DIR}/daily_review.log"
LOCK_FILE="${LOCK_DIR}/daily_review.lock"

mkdir -p "${LOG_DIR}" "${LOCK_DIR}"

exec 200>"${LOCK_FILE}"
if ! flock -n 200; then
  echo "[$(date -Iseconds)] SKIP daily_review already running" >> "${LOG_FILE}"
  exit 0
fi

cd "${ROOT}"

if [ -f "${ROOT}/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT}/.env"
  set +a
fi

{
  echo "[$(date -Iseconds)] START daily_review trade_date=${TRADE_DATE} session=${SESSION}"
  TRADINGAGENT_REVIEW_DATE="${TRADE_DATE}" TRADINGAGENT_REVIEW_SESSION="${SESSION}" PYTHONPATH="${ROOT}" timeout "${TIMEOUT}" "${PYTHON_BIN}" -c 'import json, os; from shared.review.daily_review import run_daily_review; print(json.dumps(run_daily_review(os.environ["TRADINGAGENT_REVIEW_DATE"], session=os.environ["TRADINGAGENT_REVIEW_SESSION"]), ensure_ascii=False))'
  echo "[$(date -Iseconds)] OK daily_review"
} >> "${LOG_FILE}" 2>&1
