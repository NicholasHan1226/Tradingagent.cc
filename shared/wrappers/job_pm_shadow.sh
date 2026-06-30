#!/bin/bash
set -euo pipefail
WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(cd "${WRAPPER_DIR}/.." && pwd)"
source "${SHARED_DIR}/env_loader.sh"
source "${WRAPPER_DIR}/_common.sh"
JOB_NAME="job_pm_shadow"
PHASE="intraday_5min"
run_job "${JOB_NAME}" "${PHASE}" "escalate_to_alert" "${PYTHON_BIN}" "${WRAPPER_DIR}/tradings_cron_entry.py" --job "${JOB_NAME}" --market "PM"
