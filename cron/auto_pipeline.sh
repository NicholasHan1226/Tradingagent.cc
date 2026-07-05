#!/bin/bash
# Run TradingAgent simulated auto pipeline.
TIMEOUT="${TRADINGAGENT_AUTO_PIPELINE_TIMEOUT:-3600}"
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_LOADER="${ROOT}/shared/env_loader.sh"
TRADE_DATE="${TRADINGAGENT_AUTO_PIPELINE_DATE:-}"
STAGE="${TRADINGAGENT_AUTO_PIPELINE_STAGE:-all}"
MARKETS="${TRADINGAGENT_AUTO_PIPELINE_MARKETS:-}"
REVIEW_ROOT="${TRADINGAGENT_REVIEW_ROOT:-${ROOT}/shared/review}"
MAX_CANDIDATES="${TRADINGAGENT_AUTO_PIPELINE_MAX_CANDIDATES:-25}"
LOG_DIR="${ROOT}/shared/logs/cron"
LOCK_DIR="${ROOT}/shared/logs/locks"
LOG_FILE="${LOG_DIR}/auto_pipeline.log"
LOCK_FILE="${LOCK_DIR}/auto_pipeline.lock"

mkdir -p "${LOG_DIR}" "${LOCK_DIR}"
exec 2>>"${LOG_FILE}"

exec 200>"${LOCK_FILE}"
if ! flock -n 200; then
  echo "[$(date -Iseconds)] SKIP auto_pipeline already running" >> "${LOG_FILE}"
  exit 0
fi

cd "${ROOT}"

if [ -f "${ENV_LOADER}" ]; then
  # shellcheck disable=SC1090
  source "${ENV_LOADER}"
fi

if [ -f "${ROOT}/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT}/.env"
  set +a
fi

PYTHON_BIN="${TRADINGAGENT_PYTHON:-${PYTHON_BIN:-python3}}"

ARGS=(--stage "${STAGE}" --review-root "${REVIEW_ROOT}" --max-candidates "${MAX_CANDIDATES}")
if [ -n "${TRADE_DATE}" ]; then
  ARGS+=(--date "${TRADE_DATE}")
fi
if [ -n "${MARKETS}" ]; then
  IFS=',' read -r -a MARKET_LIST <<< "${MARKETS}"
  for market in "${MARKET_LIST[@]}"; do
    market="$(printf '%s' "${market}" | xargs)"
    if [ -n "${market}" ]; then
      ARGS+=(--market "${market}")
    fi
  done
fi

{
  echo "[$(date -Iseconds)] START auto_pipeline stage=${STAGE} markets=${MARKETS:-default} review_root=${REVIEW_ROOT}"
  PYTHONPATH="${ROOT}:${PYTHONPATH:-}" timeout "${TIMEOUT}" "${PYTHON_BIN}" - "${ARGS[@]}" <<'PY'
import sys

from shared.execution.auto_pipeline import main

raise SystemExit(main(sys.argv[1:]))
PY
  echo "[$(date -Iseconds)] OK auto_pipeline"
} >> "${LOG_FILE}" 2>&1
