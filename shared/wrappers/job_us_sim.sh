#!/bin/bash
set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(cd "${WRAPPER_DIR}/.." && pwd)"

# shellcheck disable=SC1091
source "${SHARED_DIR}/env_loader.sh"
# shellcheck disable=SC1091
source "${WRAPPER_DIR}/_common.sh"

export SIM_MARKET=us
sharedsignals_source_gate "us_sim" "intraday" "us"
cd "${TRADINGAGENT_ROOT}"
PYTHONPATH="${TRADINGAGENT_ROOT}" "${PYTHON_BIN}" shared/wrappers/run_sim.py
