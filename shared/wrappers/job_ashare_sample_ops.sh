#!/bin/bash
set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(cd "${WRAPPER_DIR}/.." && pwd)"

# shellcheck disable=SC1091
source "${SHARED_DIR}/env_loader.sh"
# shellcheck disable=SC1091
source "${WRAPPER_DIR}/_common.sh"

JOB_NAME="job_ashare_sample_ops"
PHASE="review"
LEVEL3_TARGET="ashare_sample_ops"
TRADE_DATE="${TRADING_DATE:-$(date +%Y%m%d)}"
AS_OF="${ASHARE_SAMPLE_AS_OF:-$(date '+%Y-%m-%dT%H:%M:%S%z')}"
JOURNAL_PATH="${ASHARE_SAMPLE_JOURNAL_PATH:-${TRADINGAGENT_ROOT}/shared/review/ashare/sample_journal.jsonl}"
REVIEW_DIR="${ASHARE_SAMPLE_REVIEW_DIR:-${TRADINGAGENT_ROOT}/shared/review/ashare}"

# Do not pre-block on a source-status gate: the Python operation persists an
# explicit warning when SharedSignals evidence is unavailable or incomplete.
run_job "${JOB_NAME}" "${PHASE}" "${LEVEL3_TARGET}" \
  "${PYTHON_BIN}" -m shared.runtime_test.ashare_sample_ops \
  --journal-path "${JOURNAL_PATH}" \
  --review-dir "${REVIEW_DIR}" \
  --trade-date "${TRADE_DATE}" \
  --as-of "${AS_OF}" \
  --pretty
