#!/bin/bash
# Run US shadow scan through the existing TradingAgent wrapper.
TIMEOUT="${TRADINGAGENT_CRON_TIMEOUT:-1800}"
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOG_DIR="${ROOT}/shared/logs/cron"
LOCK_DIR="${ROOT}/shared/logs/locks"
LOG_FILE="${LOG_DIR}/shadow_us.log"
LOCK_FILE="${LOCK_DIR}/shadow_us.lock"

mkdir -p "${LOG_DIR}" "${LOCK_DIR}"

exec 200>"${LOCK_FILE}"
if ! flock -n 200; then
  echo "[$(date -Iseconds)] SKIP shadow_us already running" >> "${LOG_FILE}"
  exit 0
fi

cd "${ROOT}"

if [ -f "${ROOT}/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT}/.env"
  set +a
fi

"${ROOT}/shared/wrappers/job_us_shadow.sh" >> "${LOG_FILE}" 2>&1
