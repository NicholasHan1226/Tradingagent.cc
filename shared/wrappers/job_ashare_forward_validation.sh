#!/bin/bash
set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(cd "${WRAPPER_DIR}/.." && pwd)"

# shellcheck disable=SC1091
source "${SHARED_DIR}/env_loader.sh"
# shellcheck disable=SC1091
source "${WRAPPER_DIR}/_common.sh"

JOB_NAME="job_ashare_forward_validation"
PHASE="review"
LEVEL3_TARGET="ashare_forward_validation"
ENTRYPOINT="Ashare.forward_validation"
TRADE_DATE="${TRADING_DATE:-$(date +%Y%m%d)}"

run_job "${JOB_NAME}" "${PHASE}" "${LEVEL3_TARGET}" "${PYTHON_BIN}" -m "${ENTRYPOINT}" \
  --date "${TRADE_DATE}"
