#!/bin/bash
set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck disable=SC1091
source "${WRAPPER_DIR}/_common.sh"

JOB_NAME="job_opportunity_funnel_sync"
echo "legacy_opportunity_funnel_writer_retired: use shadow OpportunityLedger; no legacy write projection exists" >&2
exit 78
