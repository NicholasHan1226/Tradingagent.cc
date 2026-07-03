#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOG="${ROOT}/shared/logs/cron/evolution.log"
mkdir -p "$(dirname "$LOG")"
cd "${ROOT}"
{
  echo "[$(date -Iseconds)] evolution start"
  PYTHONPATH="${ROOT}" /opt/marketgraph/venv/bin/python3 -c '
from shared.markets.evolution_engine import EvolutionEngine
e = EvolutionEngine()
r = e.evaluate_all()
import json; print(json.dumps(r, indent=2, ensure_ascii=False))
'
  echo "[$(date -Iseconds)] evolution complete"
} >> "$LOG" 2>&1
