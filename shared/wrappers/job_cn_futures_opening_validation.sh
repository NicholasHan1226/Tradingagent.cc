#!/bin/bash
set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${WRAPPER_DIR}/_common.sh"

JOB_NAME="job_cn_futures_opening_validation"
PHASE="intraday"
LEVEL3_TARGET="cn_futures_opening_validation"
ENTRYPOINT="CNFutures.opening_validator"

run_job "${JOB_NAME}" "${PHASE}" "${LEVEL3_TARGET}" "${PYTHON_BIN}" -m "${ENTRYPOINT}" --pretty
