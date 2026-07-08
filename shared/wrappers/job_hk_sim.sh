#!/bin/bash
LOG_DIR="${TRADINGAGENT_LOG_DIR:-/opt/investment/tradingagent/shared/logs/cron}"
mkdir -p "${LOG_DIR}"
if [[ "${TRADINGAGENT_HK_SIM_ENABLED:-0}" != "1" ]]; then
  echo "[$(date -Iseconds)] SKIP hk_sim disabled; set TRADINGAGENT_HK_SIM_ENABLED=1 to run" >> "${LOG_DIR}/hk_sim.log"
  exit 0
fi
export SIM_MARKET=hk
cd /opt/investment/tradingagent
PYTHONPATH=/opt/investment/tradingagent /opt/marketgraph/venv/bin/python3 shared/wrappers/run_sim.py
