#!/bin/bash
export SIM_MARKET=us
cd /opt/investment/tradingagent
PYTHON_BIN="${TRADINGAGENT_PYTHON:-/opt/tradingagent/venv/bin/python3}"
PYTHONPATH=/opt/investment/tradingagent "${PYTHON_BIN}" shared/wrappers/run_sim.py
