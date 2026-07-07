#!/bin/bash
set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(cd "${WRAPPER_DIR}/.." && pwd)"

# shellcheck disable=SC1091
source "${SHARED_DIR}/env_loader.sh"
# shellcheck disable=SC1091
source "${WRAPPER_DIR}/_common.sh"

JOB_NAME="job_opening_acceptance"
PHASE="opening_acceptance"
LEVEL3_TARGET="opening_acceptance_summary"

run_job "${JOB_NAME}" "${PHASE}" "${LEVEL3_TARGET}" "${PYTHON_BIN}" "${TRADINGAGENT_ROOT}/shared/runtime_test/opening_acceptance.py" --send-on warn --exit-zero
