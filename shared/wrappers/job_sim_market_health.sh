#!/bin/bash
# Retired provider-specific health probe tombstone. It must remain unscheduled.
set -euo pipefail

echo "BLOCKED: job_sim_market_health is retired; use a future TradingDatas catalog/query health projection after fresh handoff" >&2
exit 78
