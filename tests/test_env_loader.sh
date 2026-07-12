#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_LOADER="${ROOT_DIR}/shared/env_loader.sh"

export TRADINGAGENT_ROOT="${ROOT_DIR}"
export TRADINGAGENT_SHARED_ROOT="${ROOT_DIR}/shared"
export TRADINGS_WRAPPERS_ROOT="${ROOT_DIR}/shared/wrappers"

# shellcheck disable=SC1090
source "${ENV_LOADER}"

required_vars=(
    TRADINGAGENT_ROOT
    TRADINGAGENT_SHARED_ROOT
    TRADINGS_WRAPPERS_ROOT
    PYTHON_BIN
    TRADINGS_RUNTIME_ROOT
    TRADINGS_LOG_ROOT
    TRADINGS_CRON_LOG_ROOT
    TRADINGS_REPAIR_QUEUE
    REAL_TRADING_ENABLED
    ASHARE_SIM_HERMES_ENABLED
    ASHARE_SIM_WEBHOOK_ENABLED
    TZ
)

for var_name in "${required_vars[@]}"; do
    value="${!var_name:-}"
    if [[ -z "${value}" ]]; then
        echo "missing required env var: ${var_name}" >&2
        exit 1
    fi
done

[[ "${REAL_TRADING_ENABLED}" == "false" ]]
[[ "${ASHARE_SIM_HERMES_ENABLED}" == "0" ]]
[[ "${ASHARE_SIM_WEBHOOK_ENABLED}" == "0" ]]
[[ "${TZ}" == "Asia/Shanghai" ]]

echo "env_loader smoke ok"

tmp_dir="$(mktemp -d)"
trap 'chmod -R u+rwX "${tmp_dir}" >/dev/null 2>&1 || true; rm -rf "${tmp_dir}"' EXIT
blocked_env="${tmp_dir}/blocked.env"
printf 'export TRADINGAGENT_BLOCKED_ENV_SHOULD_NOT_LOAD=1\n' > "${blocked_env}"
chmod 000 "${blocked_env}"

TRADINGAGENT_ENV_FILE="${blocked_env}" \
FINANCE_SHARED_ENV_FILE="${tmp_dir}/missing.env" \
TRADINGAGENT_ROOT="${ROOT_DIR}" \
TRADINGAGENT_SHARED_ROOT="${tmp_dir}/shared" \
bash -c "source '${ENV_LOADER}'; [[ -z \"\${TRADINGAGENT_BLOCKED_ENV_SHOULD_NOT_LOAD:-}\" ]]"

echo "env_loader unreadable env skip ok"
