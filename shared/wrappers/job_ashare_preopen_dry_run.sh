#!/bin/bash
set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(cd "${WRAPPER_DIR}/.." && pwd)"

# shellcheck disable=SC1091
source "${SHARED_DIR}/env_loader.sh"
# shellcheck disable=SC1091
source "${WRAPPER_DIR}/_common.sh"

JOB_NAME="job_ashare_preopen_dry_run"
PHASE="preopen_dry_run"
LEVEL3_TARGET="ashare_preopen_dry_run"
ENTRYPOINT="shared.runtime_test.ashare_preopen_dry_run"
LEVEL1_RETRIES="${ASHARE_PREOPEN_DRY_RUN_RETRIES:-1}"
SCORE_LIMIT="${ASHARE_PREOPEN_DRY_RUN_SCORE_LIMIT:-10}"

if command -v timeout >/dev/null 2>&1; then
    run_job "${JOB_NAME}" "${PHASE}" "${LEVEL3_TARGET}" timeout 90s "${PYTHON_BIN}" -m "${ENTRYPOINT}" --score-limit "${SCORE_LIMIT}" --send-on warn --exit-zero
else
    run_job "${JOB_NAME}" "${PHASE}" "${LEVEL3_TARGET}" "${PYTHON_BIN}" -m "${ENTRYPOINT}" --score-limit "${SCORE_LIMIT}" --send-on warn --exit-zero
fi
