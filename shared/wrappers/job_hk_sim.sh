#!/bin/bash
export SIM_MARKET=hk
cd /opt/investment/tradingagent
PYTHONPATH=/opt/investment/tradingagent:/opt/investment/SharedSignals /opt/marketgraph/venv/bin/python3 shared/wrappers/run_sim.py
