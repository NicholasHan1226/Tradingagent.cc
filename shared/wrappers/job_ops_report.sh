#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/_common.sh"
cd "${SCRIPT_DIR}/../.."
run_job "job_ops_report" "ops" "job_self_heal" python3 shared/wrappers/tradings_cron_entry.py --job job_ops_report
