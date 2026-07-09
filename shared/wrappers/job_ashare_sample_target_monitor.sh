#!/bin/bash
set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(cd "${WRAPPER_DIR}/.." && pwd)"

# shellcheck disable=SC1091
source "${SHARED_DIR}/env_loader.sh"
# shellcheck disable=SC1091
source "${WRAPPER_DIR}/_common.sh"

JOB_NAME="job_ashare_sample_target_monitor"
PHASE="intraday"
LEVEL3_TARGET="ashare_sample_target_monitor"
ENTRYPOINT="Ashare.sample_target_monitor"

run_job "${JOB_NAME}" "${PHASE}" "${LEVEL3_TARGET}" "${PYTHON_BIN}" -m "${ENTRYPOINT}" --pretty
