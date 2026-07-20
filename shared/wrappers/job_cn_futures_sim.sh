#!/bin/bash
set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${WRAPPER_DIR}/_common.sh"

JOB_NAME="job_cn_futures_sim"
PHASE="intraday"

tradingdatas_v1_runtime_gate "${JOB_NAME}" "${PHASE}" "cn_futures"
block_unmigrated_tradingdatas_consumer "${JOB_NAME}" "cn_futures"
