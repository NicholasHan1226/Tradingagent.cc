#!/bin/bash
set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(cd "${WRAPPER_DIR}/.." && pwd)"

# shellcheck disable=SC1091
source "${WRAPPER_DIR}/_common.sh"


JOB_NAME="job_ashare_pre_open_validation"
PHASE="pre_open"
LEVEL3_TARGET="ashare_pre_open_validation"
ENTRYPOINT="shared.runtime_test.ashare_opening_validator"

run_job "${JOB_NAME}" "${PHASE}" "${LEVEL3_TARGET}" "${PYTHON_BIN}" -m "${ENTRYPOINT}" --pre-open --pretty
