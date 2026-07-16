#!/bin/bash
set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(cd "${WRAPPER_DIR}/.." && pwd)"

# shellcheck disable=SC1091
source "${WRAPPER_DIR}/_common.sh"


JOB_NAME="job_daily_brief_day"
PHASE="post_close"
LEVEL3_TARGET="merge_into_night_brief"
ENTRYPOINT="${WRAPPER_DIR}/tradings_cron_entry.py"

run_job "${JOB_NAME}" "${PHASE}" "${LEVEL3_TARGET}" "${PYTHON_BIN}" "${ENTRYPOINT}" --job "${JOB_NAME}"
