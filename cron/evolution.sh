#!/bin/bash
set -euo pipefail
ROOT=/opt/investment/tradingagent
LOG="$ROOT/shared/logs/cron/evolution.log"
mkdir -p "$(dirname "$LOG")"
cd "$ROOT"
{
  echo "[$(date -Iseconds)] evolution start"
  PYTHONPATH="$ROOT" /opt/marketgraph/venv/bin/python3 -c '
import json; from shared.markets.evolution_engine import evaluate_all_markets
r = evaluate_all_markets()
print(json.dumps(r, default=str))
'
  echo "[$(date -Iseconds)] evolution complete"
} >> "$LOG" 2>&1
