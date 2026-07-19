#!/bin/bash
set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(cd "${WRAPPER_DIR}/.." && pwd)"

# shellcheck disable=SC1091
source "${SHARED_DIR}/env_loader.sh"
# shellcheck disable=SC1091
source "${WRAPPER_DIR}/_common.sh"

JOB_NAME="job_cn_futures_sim"
PHASE="intraday"

sharedsignals_v1_runtime_gate "${JOB_NAME}" "${PHASE}" "cn_futures"
block_unmigrated_sharedsignals_consumer "${JOB_NAME}" "cn_futures"
