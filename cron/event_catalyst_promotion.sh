#!/bin/bash
# Daily automatic promotion-gate run for the event-catalyst shadow factor.
# Research-only: reads the A-share SampleJournal, evaluates the frozen
# pre-registered policy, and journals the decision. No market data
# collection, no broker access, no capital authority.
TIMEOUT="${TRADINGAGENT_CRON_TIMEOUT:-900}"
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck disable=SC1091
source "${ROOT}/shared/wrappers/_common.sh"
PYTHON_BIN="${TRADINGAGENT_PYTHON:-python3}"
LOG_DIR="${ROOT}/shared/logs/cron"
LOCK_DIR="${ROOT}/shared/logs/locks"
LOG_FILE="${LOG_DIR}/event_catalyst_promotion.log"
LOCK_FILE="${LOCK_DIR}/event_catalyst_promotion.lock"

mkdir -p "${LOG_DIR}" "${LOCK_DIR}"
exec 2>>"${LOG_FILE}"

exec 200>"${LOCK_FILE}"
if ! flock -n 200; then
  echo "[$(date -Iseconds)] SKIP event_catalyst_promotion already running" >> "${LOG_FILE}"
  exit 0
fi

cd "${ROOT}"

if [ -f "${ROOT}/.env" ] && head -c 0 "${ROOT}/.env" >/dev/null 2>&1; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT}/.env"
  set +a
fi

{
  echo "[$(date -Iseconds)] START event_catalyst_promotion"
  PYTHONPATH="${ROOT}" timeout "${TIMEOUT}" "${PYTHON_BIN}" -m Ashare.event_catalyst_promotion_runner
  echo "[$(date -Iseconds)] OK event_catalyst_promotion"
} >> "${LOG_FILE}" 2>&1
