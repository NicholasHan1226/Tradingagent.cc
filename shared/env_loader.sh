#!/bin/bash
# tradingagent unified cron environment loader.
# This file is sourced by crontab via BASH_ENV and by every wrapper explicitly.

_tradingagent_require_sim_only() {
    local raw_value="${REAL_TRADING_ENABLED:-}"
    local normalized_value=""
    normalized_value="$(printf '%s' "${raw_value}" | tr '[:upper:]' '[:lower:]')"
    case "${normalized_value}" in
        1|true|yes|on|enabled|live|real|production)
            printf 'TradingAgent cron blocked: REAL_TRADING_ENABLED=%q requests live execution\n' \
                "${raw_value}" >&2
            return 2
            ;;
        ""|0|false|no|off|disabled)
            export REAL_TRADING_ENABLED=false
            return 0
            ;;
        *)
            printf 'TradingAgent cron blocked: REAL_TRADING_ENABLED=%q is not an accepted sim-only value\n' \
                "${raw_value}" >&2
            return 2
            ;;
    esac
}

_tradingagent_require_disabled_sim_bridge() {
    local variable_name="$1"
    local raw_value="${!variable_name:-}"
    local normalized_value=""
    normalized_value="$(printf '%s' "${raw_value}" | tr '[:upper:]' '[:lower:]')"
    case "${normalized_value}" in
        ""|0|false|no|off|disabled)
            export "${variable_name}=0"
            return 0
            ;;
        1|true|yes|on|enabled|live|real|production)
            printf 'TradingAgent cron blocked: %s=%q enables the external Mini/Hermes simulation bridge\n' \
                "${variable_name}" "${raw_value}" >&2
            return 2
            ;;
        *)
            printf 'TradingAgent cron blocked: %s=%q is not an accepted disabled bridge value\n' \
                "${variable_name}" "${raw_value}" >&2
            return 2
            ;;
    esac
}

if [[ -n "${TRADINGAGENT_ENV_LOADER_READY:-}" ]]; then
    if ! _tradingagent_require_sim_only \
        || ! _tradingagent_require_disabled_sim_bridge ASHARE_SIM_HERMES_ENABLED \
        || ! _tradingagent_require_disabled_sim_bridge ASHARE_SIM_WEBHOOK_ENABLED; then
        exit 2
    fi
    export TZ=Asia/Shanghai
    return 0 2>/dev/null || exit 0
fi

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

# Re-check after both env files are sourced. A truthy or unrecognized value is
# an operator/configuration error and must stop the job; never overwrite a live
# request with a simulated value and continue silently.
if ! _tradingagent_require_sim_only \
    || ! _tradingagent_require_disabled_sim_bridge ASHARE_SIM_HERMES_ENABLED \
    || ! _tradingagent_require_disabled_sim_bridge ASHARE_SIM_WEBHOOK_ENABLED; then
    unset TRADINGAGENT_ENV_LOADER_READY
    exit 2
fi
export TZ=Asia/Shanghai
export TRADINGAGENT_ENV_LOADER_READY=1

# SharedSignals/ShareChannel API is the production data entry for TradingAgent.
# Tests that need isolated data inject an explicit reader instead of opening the
# SharedSignals database.
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
