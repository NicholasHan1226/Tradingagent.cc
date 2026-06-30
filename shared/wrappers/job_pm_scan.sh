#!/bin/bash
set -euo pipefail
WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(cd "${WRAPPER_DIR}/.." && pwd)"
source "${SHARED_DIR}/env_loader.sh"
source "${WRAPPER_DIR}/_common.sh"
run_job "job_pm_shadow" "intraday_5min" "degrade" "${PYTHON_BIN}" "${WRAPPER_DIR}/tradings_cron_entry.py" --job "job_pm_shadow"
