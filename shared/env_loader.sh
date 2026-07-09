#!/bin/bash
# tradingagent unified cron environment loader.
# This file is sourced by crontab via BASH_ENV and by every wrapper explicitly.

if [[ -n "${TRADINGAGENT_ENV_LOADER_READY:-}" ]]; then
    return 0 2>/dev/null || exit 0
fi
export TRADINGAGENT_ENV_LOADER_READY=1

export TRADINGAGENT_ROOT="${TRADINGAGENT_ROOT:-/opt/investment/tradingagent}"
export TRADINGAGENT_SHARED_ROOT="${TRADINGAGENT_SHARED_ROOT:-${TRADINGAGENT_ROOT}/shared}"
export TRADINGAGENT_WRAPPERS_ROOT="${TRADINGAGENT_WRAPPERS_ROOT:-${TRADINGAGENT_SHARED_ROOT}/wrappers}"

export TRADINGAGENT_ENV_FILE="${TRADINGAGENT_ENV_FILE:-${TRADINGAGENT_ROOT}/.env}"
export FINANCE_SHARED_ENV_FILE="${FINANCE_SHARED_ENV_FILE:-/opt/tradingagent/.env}"

# Load TradingAgent-owned env first, then the shared finance env for common
# credentials. Do not source MarketGraph deploy env; the three systems must be
# able to run on separate hosts.
if [[ -r "${TRADINGAGENT_ENV_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${TRADINGAGENT_ENV_FILE}"
fi
if [[ -r "${FINANCE_SHARED_ENV_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${FINANCE_SHARED_ENV_FILE}"
fi

# SharedSignals/ShareChannel API is the default data entry for TradingAgent.
# Direct SQLite reads require TRADINGAGENT_ALLOW_SHARED_SIGNALS_SQLITE=1 and
# an explicit SHARED_SIGNALS_DB for local tests or emergency diagnostics.
export SHAREDSIGNALS_API_URL="${SHAREDSIGNALS_API_URL:-http://127.0.0.1:8082}"
export SHAREDSIGNALS_API_TIMEOUT="${SHAREDSIGNALS_API_TIMEOUT:-10}"
export SHAREDSIGNALS_API_RETRIES="${SHAREDSIGNALS_API_RETRIES:-1}"
export MARKETGRAPH_API_URL="${MARKETGRAPH_API_URL:-http://127.0.0.1:8080}"
export MARKETGRAPH_API_TIMEOUT="${MARKETGRAPH_API_TIMEOUT:-10}"
export MARKETGRAPH_API_RETRIES="${MARKETGRAPH_API_RETRIES:-1}"
export MARKETGRAPH_API_TOKEN="${MARKETGRAPH_API_TOKEN:-${MCP_API_TOKEN:-${MARKETGRAPH_AUTH_TOKEN:-}}}"

# Normalize email/Cloudflare variable names across the three systems.
export CLOUDFLARE_ACCOUNT_ID="${CLOUDFLARE_ACCOUNT_ID:-${CF_EMAIL_ACCOUNT_ID:-}}"
export CLOUDFLARE_EMAIL_API_TOKEN="${CLOUDFLARE_EMAIL_API_TOKEN:-${CF_EMAIL_API_TOKEN:-}}"
export CF_EMAIL_ACCOUNT_ID="${CF_EMAIL_ACCOUNT_ID:-${CLOUDFLARE_ACCOUNT_ID:-}}"
export CF_EMAIL_API_TOKEN="${CF_EMAIL_API_TOKEN:-${CLOUDFLARE_EMAIL_API_TOKEN:-}}"
export EMAIL_FROM_TRADING="${EMAIL_FROM_TRADING:-${EMAIL_TRADING_FROM:-notice@tradingagent.cc}}"
export EMAIL_TO_TRADING="${EMAIL_TO_TRADING:-${EMAIL_TRADING_TO:-tradingadviser@coze.email}}"
export EMAIL_FROM_SYSTEM="${EMAIL_FROM_SYSTEM:-${EMAIL_SYSTEM_FROM:-notice@tradingagent.cc}}"
export EMAIL_TO_SYSTEM="${EMAIL_TO_SYSTEM:-${EMAIL_SYSTEM_TO:-soc@coze.email}}"
export EMAIL_TRADING_FROM="${EMAIL_TRADING_FROM:-${EMAIL_FROM_TRADING}}"
export EMAIL_TRADING_TO="${EMAIL_TRADING_TO:-${EMAIL_TO_TRADING}}"
export EMAIL_SYSTEM_FROM="${EMAIL_SYSTEM_FROM:-${EMAIL_FROM_SYSTEM}}"
export EMAIL_SYSTEM_TO="${EMAIL_SYSTEM_TO:-${EMAIL_TO_SYSTEM}}"

export TRADINGAGENT_VENV_ROOT="${TRADINGAGENT_VENV_ROOT:-/opt/tradingagent/venv}"
if [[ -z "${PYTHON_VENV_ROOT:-}" ]]; then
    if [[ -d "${TRADINGAGENT_VENV_ROOT}" ]]; then
        export PYTHON_VENV_ROOT="${TRADINGAGENT_VENV_ROOT}"
    else
        export PYTHON_VENV_ROOT="${TRADINGAGENT_VENV_ROOT}"
    fi
fi
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

export TRADINGS_RUNTIME_ROOT="${TRADINGS_RUNTIME_ROOT:-${TRADINGAGENT_ROOT}/runtime}"
export TRADINGS_STATE_ROOT="${TRADINGS_STATE_ROOT:-${TRADINGS_RUNTIME_ROOT}/state}"
export TRADINGS_TMP_ROOT="${TRADINGS_TMP_ROOT:-${TRADINGS_RUNTIME_ROOT}/tmp}"
export TRADINGS_LOG_ROOT="${TRADINGS_LOG_ROOT:-${TRADINGAGENT_SHARED_ROOT}/logs}"
export TRADINGS_CRON_LOG_ROOT="${TRADINGS_CRON_LOG_ROOT:-${TRADINGS_LOG_ROOT}/cron}"
export TRADINGS_REPAIR_QUEUE="${TRADINGS_REPAIR_QUEUE:-${TRADINGS_LOG_ROOT}/repair_queue.jsonl}"
export TRADINGS_GATE_ROOT="${TRADINGS_GATE_ROOT:-${TRADINGAGENT_SHARED_ROOT}/risk/gate}"

export PYTHONPATH="${TRADINGAGENT_ROOT}:${PYTHONPATH:-}"

# Secret references only. Do not place plaintext secrets in this file.
# Market-data provider credentials belong to SharedSignals, not TradingAgent.
export TRADINGS_OPENAI_API_KEY="${TRADINGS_OPENAI_API_KEY:-${OPENAI_API_KEY:-}}"
export TRADINGS_COZE_API_KEY="${TRADINGS_COZE_API_KEY:-${COZE_API_KEY:-}}"
export TRADINGS_DEEPSEEK_API_KEY="${TRADINGS_DEEPSEEK_API_KEY:-${DEEPSEEK_API_KEY:-}}"

mkdir -p \
    "${TRADINGS_RUNTIME_ROOT}" \
    "${TRADINGS_STATE_ROOT}" \
    "${TRADINGS_TMP_ROOT}" \
    "${TRADINGS_LOG_ROOT}" \
    "${TRADINGS_CRON_LOG_ROOT}" \
    "${TRADINGS_GATE_ROOT}"
