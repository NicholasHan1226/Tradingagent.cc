#!/bin/bash
set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(cd "${WRAPPER_DIR}/.." && pwd)"

# shellcheck disable=SC1091
source "${SHARED_DIR}/env_loader.sh"
# shellcheck disable=SC1091
source "${WRAPPER_DIR}/_common.sh"

export SIM_MARKET=crypto
sharedsignals_v1_runtime_gate "crypto_sim" "intraday" "crypto"
block_unmigrated_sharedsignals_consumer "crypto_sim" "crypto"
