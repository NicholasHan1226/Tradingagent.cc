#!/usr/bin/env python3
"""US P1 forward validation for shadow strategies."""

from __future__ import annotations

from typing import Any

from shared.markets.config_schema import MarketToolConfig
from shared.markets.safety import assert_no_live_broker, assert_no_real_execution, reject_real_execution_payload
from US.common import USConfig


class USForwardValidation:
    """Out-of-sample validation for US earnings and momentum funnels."""

    def __init__(self, config: MarketToolConfig | None = None) -> None:
        self.config = config or USConfig()
        assert_no_real_execution(self.config)
        assert_no_live_broker(self.config)
        self.market = "us"
        self.currency = "USD"

    def validate(self, records: list[dict[str, Any]], *, as_of: str) -> dict[str, Any]:
        checked = list(records or [])
        for record in checked:
            reject_real_execution_payload(record, context="USForwardValidation.record")

        returns = [_to_float(record.get("return_pct")) for record in checked]
        positive = [value for value in returns if value > 0]
        funnel = {
            "earnings": sum(1 for record in checked if "earning" in _strategy(record)),
            "momentum": sum(1 for record in checked if "momentum" in _strategy(record)),
            "other": 0,
        }
        funnel["other"] = len(checked) - funnel["earnings"] - funnel["momentum"]

        return {
            "market": "us",
            "as_of": as_of,
            "status": "ok",
            "validation_type": "out_of_sample",
            "currency": "USD",
            "capital_layer": "shadow",
            "total": len(checked),
            "positive": len(positive),
            "positive_rate": len(positive) / len(checked) if checked else 0.0,
            "avg_return_pct": round(sum(returns) / len(returns), 6) if returns else 0.0,
            "funnel": funnel,
            "passed": bool(checked) and len(positive) / len(checked) >= self.config.promotion.min_positive_days_pct,
            "real_execution": False,
        }


def _strategy(record: dict[str, Any]) -> str:
    return str(record.get("strategy_name") or record.get("strategy") or "").strip().lower()


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)
