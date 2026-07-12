#!/bin/bash
set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(cd "${WRAPPER_DIR}/.." && pwd)"

# shellcheck disable=SC1091
source "${SHARED_DIR}/env_loader.sh"
# shellcheck disable=SC1091
source "${WRAPPER_DIR}/_common.sh"

if [[ "${REAL_TRADING_ENABLED:-false}" != "false" && "${REAL_TRADING_ENABLED:-0}" != "0" ]]; then
    printf '[%s] job_cn_futures_sample_ops blocked=sim_only\n' "$(timestamp)" >&2
    exit 2
fi
export REAL_TRADING_ENABLED=false

JOB_NAME="job_cn_futures_sample_ops"
PHASE="review"
LEVEL3_TARGET="cn_futures_sample_ops"
AS_OF="${CN_FUTURES_SAMPLE_AS_OF:-$(date '+%Y-%m-%dT%H:%M:%S%z')}"
REVIEW_PATH="${CN_FUTURES_REVIEW_PATH:-${TRADINGAGENT_ROOT}/shared/review/data/cn_futures_sim_reviews.jsonl}"
REVIEW_DIR="${CN_FUTURES_SAMPLE_REVIEW_DIR:-${TRADINGAGENT_ROOT}/shared/review/cn_futures}"
CAPITAL_ROOT="${TRADINGAGENT_CN_FUTURES_CAPITAL_ROOT:-${TRADINGAGENT_ROOT}/shared/logs/capital/cn_futures}"

if [[ -n "${TRADING_DATE:-}" ]]; then
    TRADE_DATE="${TRADING_DATE}"
else
    TRADE_DATE="$(PYTHONPATH="${TRADINGAGENT_ROOT}" "${PYTHON_BIN}" - "${AS_OF}" <<'PY'
import sys

from shared.capital.market_ledger import (
    _parse_timestamp,
    _reconcile_trade_date_for_pit,
)

pit = _parse_timestamp(sys.argv[1], field="wrapper_pit_timestamp")
print(_reconcile_trade_date_for_pit("cn_futures", pit))
PY
)"
fi

run_job "${JOB_NAME}" "${PHASE}" "${LEVEL3_TARGET}" \
    "${PYTHON_BIN}" -m shared.runtime_test.cn_futures_sample_ops \
    --review-path "${REVIEW_PATH}" \
    --review-dir "${REVIEW_DIR}" \
    --trade-date "${TRADE_DATE}" \
    --as-of "${AS_OF}" \
    --capital-root "${CAPITAL_ROOT}" \
    --pretty
