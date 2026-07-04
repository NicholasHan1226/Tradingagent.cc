#!/usr/bin/env python3
"""Fail-closed placeholder for future CN futures real trading.

This module documents the future CTP/SimNow boundary without connecting to a
broker, reading credentials, or transforming rejected real orders into
simulated orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from shared.markets.safety import SafetyViolation

from . import MARKET


@dataclass(frozen=True)
class CNFuturesLiveGatewayStatus:
    market: str = MARKET
    real_trading_enabled: bool = False
    broker_adapter_ready: bool = False
    reason: str = "cn_futures_real_trading_not_implemented"


def get_live_gateway_status() -> dict[str, Any]:
    """Return explicit fail-closed status for dashboards and docs."""

    status = CNFuturesLiveGatewayStatus()
    return {
        "market": status.market,
        "capital_layer": "real",
        "account_type": "real",
        "real_trading_enabled": status.real_trading_enabled,
        "broker_adapter_ready": status.broker_adapter_ready,
        "reason": status.reason,
        "required_before_enablement": [
            "licensed futures-company broker adapter",
            "CTP or approved broker API credentials outside Git",
            "contract metadata and margin source validation",
            "pre-trade risk limits",
            "manual approval workflow",
            "signed order/receipt reconciliation",
            "emergency halt and rollback procedure",
        ],
    }


def submit_real_order(order: dict[str, Any] | None = None, *, approval_token: str | None = None) -> dict[str, Any]:
    """Reject every CN futures real order until a reviewed broker adapter exists."""

    del order, approval_token
    status = get_live_gateway_status()
    raise SafetyViolation(
        "cn_futures_live_gateway: real futures trading is fail-closed; "
        f"{status['reason']}; simulated fallback is forbidden"
    )


__all__ = ["CNFuturesLiveGatewayStatus", "get_live_gateway_status", "submit_real_order"]
