#!/usr/bin/env python3
"""Execution layer package: Hermes bridge, shadow broker, sim broker, router."""

from .real_trading_gate import (
    GateResult,
    emergency_stop_check,
    require_explicit_approval,
    run_real_order_gates,
    validate_capital_limits,
    validate_market_hours,
    validate_real_trading_enabled,
    validate_t1_settlement,
)
from .signals_real import RealSignalQueue

__all__ = [
    "GateResult",
    "RealSignalQueue",
    "emergency_stop_check",
    "require_explicit_approval",
    "run_real_order_gates",
    "validate_capital_limits",
    "validate_market_hours",
    "validate_real_trading_enabled",
    "validate_t1_settlement",
]
