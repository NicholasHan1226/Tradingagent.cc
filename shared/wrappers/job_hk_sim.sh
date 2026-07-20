#!/bin/bash
set -euo pipefail
WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${WRAPPER_DIR}/_common.sh"
block_retired_legacy_runtime "job_hk_sim"
