#!/bin/bash
# Legacy A-share cron entrypoint tombstone. The shared guard exits with code 78
# before loading environment files or creating runtime paths.
set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${WRAPPER_DIR}/_common.sh"

# Defensive fallback: the central guard above must already have terminated.
block_retired_ashare_runtime "job_ashare_sim_exec"
exit 78
