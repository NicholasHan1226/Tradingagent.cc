#!/bin/bash
# Run HK shadow scan without touching execution queues.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${TRADINGAGENT_PYTHON:-python3}"
AS_OF="${TRADINGAGENT_AS_OF_DATE:-$(date +%Y-%m-%d)}"
LOG_DIR="${ROOT}/shared/logs/cron"
LOCK_DIR="${ROOT}/shared/logs/locks"
LOG_FILE="${LOG_DIR}/shadow_hk.log"
LOCK_FILE="${LOCK_DIR}/shadow_hk.lock"

mkdir -p "${LOG_DIR}" "${LOCK_DIR}"

exec 200>"${LOCK_FILE}"
if ! flock -n 200; then
  echo "[$(date -Iseconds)] SKIP shadow_hk already running" >> "${LOG_FILE}"
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
  echo "[$(date -Iseconds)] START shadow_hk as_of=${AS_OF}"
  TRADINGAGENT_AS_OF_DATE="${AS_OF}" PYTHONPATH="${ROOT}" "${PYTHON_BIN}" -c 'import json, os; from HK.workflow import run_hk_shadow_cycle; print(json.dumps(run_hk_shadow_cycle(os.environ["TRADINGAGENT_AS_OF_DATE"]), ensure_ascii=False))'
  echo "[$(date -Iseconds)] OK shadow_hk"
} >> "${LOG_FILE}" 2>&1
