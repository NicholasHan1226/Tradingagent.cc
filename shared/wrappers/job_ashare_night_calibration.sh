#!/bin/bash
set -euo pipefail

# LEGACY / NOT ACTIVE: this wrapper is not registered in crontab.txt or
# shared/crontab.txt. It is retained only for reference or future revival.
# Do not add to production cron without first reviewing its entrypoint and
# confirming it aligns with current night-calibration policies.

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(cd "${WRAPPER_DIR}/.." && pwd)"

# shellcheck disable=SC1091
source "${SHARED_DIR}/env_loader.sh"
# shellcheck disable=SC1091
source "${WRAPPER_DIR}/_common.sh"

JOB_NAME="job_ashare_night_calibration"
PHASE="night_calibration"
LEVEL3_TARGET="next_morning_backfill"
ENTRYPOINT="${WRAPPER_DIR}/tradings_cron_entry.py"

run_job "${JOB_NAME}" "${PHASE}" "${LEVEL3_TARGET}" "${PYTHON_BIN}" "${ENTRYPOINT}" --job "${JOB_NAME}"
