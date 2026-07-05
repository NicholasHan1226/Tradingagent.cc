#!/bin/bash
set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(cd "${WRAPPER_DIR}/.." && pwd)"

# shellcheck disable=SC1091
source "${SHARED_DIR}/env_loader.sh"
# shellcheck disable=SC1091
source "${WRAPPER_DIR}/_common.sh"

JOB_NAME="job_cn_futures_calibration_report"
PHASE="review"
LEVEL3_TARGET="cn_futures_win_rate_calibration"
ENTRYPOINT="CNFutures.calibration"
REPORT_JSON="${CN_FUTURES_CALIBRATION_REPORT_PATH:-${SHARED_DIR}/review/cn_futures/win_rate_calibration_report.json}"
REPORT_MD="${CN_FUTURES_CALIBRATION_REPORT_MD_PATH:-${SHARED_DIR}/review/cn_futures/win_rate_calibration_report.md}"

run_job "${JOB_NAME}" "${PHASE}" "${LEVEL3_TARGET}" "${PYTHON_BIN}" -m "${ENTRYPOINT}" \
  --write-json "${REPORT_JSON}" \
  --write-md "${REPORT_MD}"
