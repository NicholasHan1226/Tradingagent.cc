#!/usr/bin/env python3
"""Shared execution primitives and fail-closed real-trading safety gates.

Market-specific paper and future live broker adapters live in their own market
domains. Importing this package never creates a live-order path.
"""

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
__all__ = [
    "GateResult",
    "emergency_stop_check",
    "require_explicit_approval",
    "run_real_order_gates",
    "validate_capital_limits",
    "validate_market_hours",
    "validate_real_trading_enabled",
    "validate_t1_settlement",
]
