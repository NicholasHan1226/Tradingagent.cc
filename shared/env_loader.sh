#!/bin/bash
# Unified environment loader for all Tradings modules
# Sourced by crontab via BASH_ENV or explicitly in wrapper scripts
#
# Usage in crontab:
#   BASH_ENV=/opt/investment/Tradings/shared/env_loader.sh
# Or in scripts:
#   source /opt/investment/Tradings/shared/env_loader.sh

export TRADINGS_ROOT=/opt/investment/Tradings
export SHARED_SIGNALS_ROOT=/opt/investment/SharedSignals
export MARKETGRAPH_ROOT=/opt/investment/MarketGraph
export MARKETGRAPH_RUNTIME_ROOT=/opt/investment/MarketGraphRuntime

# Source MarketGraph cron environment (API keys, Tushare tokens, venv paths)
if [ -f /opt/investment/MarketGraph/deploy/marketgraph_cron.env ]; then
    source /opt/investment/MarketGraph/deploy/marketgraph_cron.env
fi

# Future: source /opt/investment/.env when centralized secrets are ready
