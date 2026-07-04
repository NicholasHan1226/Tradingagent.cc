#!/bin/bash
# Run simulated multi-market style evolution.
TIMEOUT="${TRADINGAGENT_EVOLUTION_TIMEOUT:-1800}"
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${TRADINGAGENT_PYTHON:-python3}"
MARKETS="${TRADINGAGENT_EVOLUTION_MARKETS:-crypto,pm,us}"
ENV_LOADER="${ROOT}/shared/env_loader.sh"
if [ -f "${ENV_LOADER}" ]; then
  source "${ENV_LOADER}"
fi
REVIEW_ROOT="${TRADINGAGENT_REVIEW_ROOT:-${ROOT}/shared/review}"
LOG_DIR="${ROOT}/shared/logs/cron"
LOCK_DIR="${ROOT}/shared/logs/locks"
LOG_FILE="${LOG_DIR}/evolution.log"
LOCK_FILE="${LOCK_DIR}/evolution.lock"

mkdir -p "${LOG_DIR}" "${LOCK_DIR}"

exec 200>"${LOCK_FILE}"
if ! flock -n 200; then
  echo "[$(date -Iseconds)] SKIP evolution already running" >> "${LOG_FILE}"
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
  echo "[$(date -Iseconds)] START evolution markets=${MARKETS}"
  TRADINGAGENT_EVOLUTION_MARKETS="${MARKETS}" \
  TRADINGAGENT_REVIEW_ROOT="${REVIEW_ROOT}" \
  PYTHONPATH="${ROOT}" timeout "${TIMEOUT}" "${PYTHON_BIN}" - <<'PY'
import json
import os

from shared.markets.evolution_engine import evaluate_all_markets


markets = tuple(
    item.strip().lower()
    for item in os.environ.get("TRADINGAGENT_EVOLUTION_MARKETS", "crypto,pm,us").split(",")
    if item.strip()
)
review_root = os.environ.get("TRADINGAGENT_REVIEW_ROOT") or None
result = evaluate_all_markets(markets, review_root=review_root)
print(json.dumps(result, ensure_ascii=False, indent=2))
PY
  echo "[$(date -Iseconds)] OK evolution"
} >> "${LOG_FILE}" 2>&1
