#!/bin/bash
# Tradings unified cron environment loader.
# This file is sourced by crontab via BASH_ENV and by every wrapper explicitly.

if [[ -n "${TRADINGS_ENV_LOADER_READY:-}" ]]; then
    return 0 2>/dev/null || exit 0
fi
export TRADINGS_ENV_LOADER_READY=1

export TRADINGS_ROOT="${TRADINGS_ROOT:-/opt/investment/Tradings}"
export TRADINGS_SHARED_ROOT="${TRADINGS_SHARED_ROOT:-${TRADINGS_ROOT}/shared}"
export TRADINGS_WRAPPERS_ROOT="${TRADINGS_WRAPPERS_ROOT:-${TRADINGS_SHARED_ROOT}/wrappers}"
export SHARED_SIGNALS_ROOT="${SHARED_SIGNALS_ROOT:-/opt/investment/SharedSignals}"
export MARKETGRAPH_ROOT="${MARKETGRAPH_ROOT:-/opt/investment/MarketGraph}"
export MARKETGRAPH_RUNTIME_ROOT="${MARKETGRAPH_RUNTIME_ROOT:-/opt/investment/MarketGraphRuntime}"

export MARKETGRAPH_CRON_LOADER="${MARKETGRAPH_CRON_LOADER:-${MARKETGRAPH_ROOT}/deploy/marketgraph_cron_loader.sh}"
export MARKETGRAPH_CRON_ENV="${MARKETGRAPH_CRON_ENV:-${MARKETGRAPH_ROOT}/deploy/marketgraph_cron.env}"

# Chain upstream MarketGraph cron env first so existing secrets/token wiring stays intact.
if [[ -f "${MARKETGRAPH_CRON_LOADER}" ]]; then
    # shellcheck disable=SC1090
    source "${MARKETGRAPH_CRON_LOADER}"
elif [[ -f "${MARKETGRAPH_CRON_ENV}" ]]; then
    # shellcheck disable=SC1090
    source "${MARKETGRAPH_CRON_ENV}"
fi

export MARKETGRAPH_VENV_ROOT="${MARKETGRAPH_VENV_ROOT:-/opt/marketgraph/venv}"
export PYTHON_VENV_ROOT="${PYTHON_VENV_ROOT:-${MARKETGRAPH_VENV_ROOT}}"
if [[ -z "${VIRTUAL_ENV:-}" && -d "${PYTHON_VENV_ROOT}" ]]; then
    export VIRTUAL_ENV="${PYTHON_VENV_ROOT}"
fi

if [[ -z "${PYTHON_BIN:-}" ]]; then
    if [[ -x "${PYTHON_VENV_ROOT}/bin/python3" ]]; then
        export PYTHON_BIN="${PYTHON_VENV_ROOT}/bin/python3"
    else
        export PYTHON_BIN="$(command -v python3)"
    fi
fi
export PIP_BIN="${PIP_BIN:-${PYTHON_VENV_ROOT}/bin/pip}"

export TRADINGS_RUNTIME_ROOT="${TRADINGS_RUNTIME_ROOT:-${MARKETGRAPH_RUNTIME_ROOT}/tradings}"
export TRADINGS_STATE_ROOT="${TRADINGS_STATE_ROOT:-${TRADINGS_RUNTIME_ROOT}/state}"
export TRADINGS_TMP_ROOT="${TRADINGS_TMP_ROOT:-${TRADINGS_RUNTIME_ROOT}/tmp}"
export TRADINGS_LOG_ROOT="${TRADINGS_LOG_ROOT:-${TRADINGS_SHARED_ROOT}/logs}"
export TRADINGS_CRON_LOG_ROOT="${TRADINGS_CRON_LOG_ROOT:-${TRADINGS_LOG_ROOT}/cron}"
export TRADINGS_REPAIR_QUEUE="${TRADINGS_REPAIR_QUEUE:-${TRADINGS_LOG_ROOT}/repair_queue.jsonl}"
export TRADINGS_GATE_ROOT="${TRADINGS_GATE_ROOT:-${TRADINGS_SHARED_ROOT}/risk/gate}"

export PYTHONPATH="${TRADINGS_ROOT}:${SHARED_SIGNALS_ROOT}:${MARKETGRAPH_ROOT}:${PYTHONPATH:-}"

# Secret references only. Do not place plaintext secrets in this file.
export TRADINGS_OPENAI_API_KEY="${TRADINGS_OPENAI_API_KEY:-${OPENAI_API_KEY:-}}"
export TRADINGS_TUSHARE_TOKEN="${TRADINGS_TUSHARE_TOKEN:-${TUSHARE_TOKEN:-}}"
export TRADINGS_ALPACA_API_KEY="${TRADINGS_ALPACA_API_KEY:-${ALPACA_API_KEY:-}}"
export TRADINGS_ALPACA_SECRET_KEY="${TRADINGS_ALPACA_SECRET_KEY:-${ALPACA_SECRET_KEY:-}}"
export TRADINGS_POLYMARKET_API_KEY="${TRADINGS_POLYMARKET_API_KEY:-${POLYMARKET_API_KEY:-}}"
export TRADINGS_POLYMARKET_SECRET="${TRADINGS_POLYMARKET_SECRET:-${POLYMARKET_SECRET:-}}"
export TRADINGS_COZE_API_KEY="${TRADINGS_COZE_API_KEY:-${COZE_API_KEY:-}}"
export TRADINGS_DEEPSEEK_API_KEY="${TRADINGS_DEEPSEEK_API_KEY:-${DEEPSEEK_API_KEY:-}}"
export TRADINGS_FINNHUB_API_KEY="${TRADINGS_FINNHUB_API_KEY:-${FINNHUB_API_KEY:-}}"
export TRADINGS_FMP_API_KEY="${TRADINGS_FMP_API_KEY:-${FMP_API_KEY:-}}"

mkdir -p \
    "${TRADINGS_RUNTIME_ROOT}" \
    "${TRADINGS_STATE_ROOT}" \
    "${TRADINGS_TMP_ROOT}" \
    "${TRADINGS_LOG_ROOT}" \
    "${TRADINGS_CRON_LOG_ROOT}" \
    "${TRADINGS_GATE_ROOT}"
