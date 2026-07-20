#!/bin/bash
set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${WRAPPER_DIR}/_common.sh"

export SIM_MARKET=us
tradingdatas_v1_runtime_gate "us_sim" "intraday" "us"
block_unmigrated_tradingdatas_consumer "us_sim" "us"
