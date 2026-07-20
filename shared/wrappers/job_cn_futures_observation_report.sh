#!/bin/bash
set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${WRAPPER_DIR}/_common.sh"

JOB_NAME="job_cn_futures_observation_report"
PHASE="review"
LEVEL3_TARGET="cn_futures_5min_observation"
ENTRYPOINT="CNFutures.observation_report"
OUTPUT_PATH="${CN_FUTURES_OBSERVATION_REPORT_PATH:-${SHARED_DIR}/review/cn_futures/observation_report.json}"

run_job "${JOB_NAME}" "${PHASE}" "${LEVEL3_TARGET}" "${PYTHON_BIN}" -m "${ENTRYPOINT}" \
  --write-json "${OUTPUT_PATH}"
