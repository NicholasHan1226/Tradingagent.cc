#!/bin/bash
set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(cd "${WRAPPER_DIR}/.." && pwd)"

# shellcheck disable=SC1091
source "${WRAPPER_DIR}/_common.sh"

if [[ "${REAL_TRADING_ENABLED:-false}" != "false" && "${REAL_TRADING_ENABLED:-0}" != "0" ]]; then
    printf '[%s] job_market_capital_reconcile blocked=sim_only\n' "$(timestamp)" >&2
    exit 2
fi
export REAL_TRADING_ENABLED=false

MARKET="${MARKET_CAPITAL_RECONCILE_MARKET:-${1:-}}"
PHASE="${MARKET_CAPITAL_RECONCILE_PHASE:-${2:-ops}}"
PIT_TIMESTAMP="${MARKET_CAPITAL_RECONCILE_PIT:-$(date '+%Y-%m-%dT%H:%M:%S%z')}"

case "${MARKET}" in
    ashare)
        CAPITAL_ROOT="${TRADINGAGENT_ASHARE_CAPITAL_ROOT:-${TRADINGAGENT_ROOT}/shared/logs/capital/ashare}"
        SOURCE_ROOT="${TRADINGAGENT_ASHARE_EXECUTION_ROOT:-${TRADINGAGENT_ROOT}/shared/logs/execution_lineages/ashare-sim-fresh-20260712-v1}"
        ;;
    cn_futures)
        CAPITAL_ROOT="${TRADINGAGENT_CN_FUTURES_CAPITAL_ROOT:-${TRADINGAGENT_ROOT}/shared/logs/capital/cn_futures}"
        SOURCE_ROOT="${TRADINGAGENT_SIGNALS_ROOT:-${TRADINGAGENT_ROOT}/signals}"
        ;;
    *)
        printf '[%s] job_market_capital_reconcile blocked=unsupported_market value=%q\n' "$(timestamp)" "${MARKET}" >&2
        exit 2
        ;;
esac

if [[ -n "${TRADING_DATE:-}" ]]; then
    TRADE_DATE="${TRADING_DATE}"
elif [[ "${MARKET}" == "cn_futures" ]]; then
    TRADE_DATE="$(PYTHONPATH="${TRADINGAGENT_ROOT}" "${PYTHON_BIN}" - "${PIT_TIMESTAMP}" <<'PY'
import sys

from shared.capital.market_ledger import (
    _parse_timestamp,
    _reconcile_trade_date_for_pit,
)

pit = _parse_timestamp(sys.argv[1], field="wrapper_pit_timestamp")
print(_reconcile_trade_date_for_pit("cn_futures", pit))
PY
)"
else
    TRADE_DATE="$(date +%Y%m%d)"
fi

JOB_NAME="job_market_capital_reconcile_${MARKET}_${PHASE}"
run_job "${JOB_NAME}" "${PHASE}" "market_capital_reconcile_${MARKET}" \
    "${PYTHON_BIN}" -m shared.runtime_test.market_capital_reconcile_ops \
    --market "${MARKET}" \
    --capital-root "${CAPITAL_ROOT}" \
    --source-root "${SOURCE_ROOT}" \
    --trade-date "${TRADE_DATE}" \
    --pit-timestamp "${PIT_TIMESTAMP}" \
    --phase "${PHASE}" \
    --prepare-source \
    --pretty
