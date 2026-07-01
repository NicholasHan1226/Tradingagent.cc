#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_LOADER="${ROOT_DIR}/shared/env_loader.sh"

export TRADINGAGENT_ROOT="${ROOT_DIR}"
export TRADINGAGENT_SHARED_ROOT="${ROOT_DIR}/shared"
export TRADINGS_WRAPPERS_ROOT="${ROOT_DIR}/shared/wrappers"
export SHARED_SIGNALS_ROOT="${ROOT_DIR}/signals"
export MARKETGRAPH_ROOT="${ROOT_DIR}/mini"
export MARKETGRAPH_RUNTIME_ROOT="${ROOT_DIR}/shared/runtime_test"

# shellcheck disable=SC1090
source "${ENV_LOADER}"

required_vars=(
    TRADINGAGENT_ROOT
    TRADINGAGENT_SHARED_ROOT
    TRADINGS_WRAPPERS_ROOT
    SHARED_SIGNALS_ROOT
    MARKETGRAPH_ROOT
    MARKETGRAPH_RUNTIME_ROOT
    PYTHON_BIN
    TRADINGS_RUNTIME_ROOT
    TRADINGS_LOG_ROOT
    TRADINGS_CRON_LOG_ROOT
    TRADINGS_REPAIR_QUEUE
)

for var_name in "${required_vars[@]}"; do
    value="${!var_name:-}"
    if [[ -z "${value}" ]]; then
        echo "missing required env var: ${var_name}" >&2
        exit 1
    fi
done

echo "env_loader smoke ok"
