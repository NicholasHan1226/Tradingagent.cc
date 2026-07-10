#!/bin/bash
set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(cd "${WRAPPER_DIR}/.." && pwd)"

# shellcheck disable=SC1091
source "${SHARED_DIR}/env_loader.sh"
# shellcheck disable=SC1091
source "${WRAPPER_DIR}/_common.sh"

JOB_NAME="job_ashare_formal_close_refresh"
PHASE="postclose"
LEVEL3_TARGET="ashare_formal_close_refresh"
ENTRYPOINT="Ashare.formal_close_refresh"
TRADE_DATE="${TRADING_DATE:-$(date +%Y%m%d)}"

sharedsignals_source_gate "${JOB_NAME}" "${PHASE}" "ashare"
run_job "${JOB_NAME}" "${PHASE}" "${LEVEL3_TARGET}" \
    "${PYTHON_BIN}" -m "${ENTRYPOINT}" --trade-date "${TRADE_DATE}" --pretty
