#!/usr/bin/env python3
"""HK P1 strategy promotion gates."""

from __future__ import annotations

from typing import Any

from shared.markets.config_schema import MarketToolConfig
from shared.markets.safety import assert_no_live_broker, assert_no_real_execution, reject_real_execution_payload
from HK.common import HKConfig


class HKStrategyPromotion:
    """Classify HK strategies through research -> shadow -> sim tiers."""

    tiers = ("research", "shadow_candidate", "shadow", "sim_candidate", "sim")

    def __init__(self, config: MarketToolConfig | None = None) -> None:
        self.config = config or HKConfig()
        assert_no_real_execution(self.config)
        assert_no_live_broker(self.config)
        self.market = "hk"
        self.currency = "HKD"

    def evaluate(self, metrics: dict[str, Any]) -> dict[str, Any]:
        reject_real_execution_payload(metrics, context="HKStrategyPromotion.metrics")
        shadow_trades = int(_to_float(metrics.get("shadow_trades")))
        positive_days_pct = _to_float(metrics.get("positive_days_pct"))
        oos_return_pct = _to_float(metrics.get("oos_return_pct"))
        drawdown_pct = _to_float(metrics.get("drawdown_pct"))

        if shadow_trades <= 0:
            tier = "research"
        elif shadow_trades < 5:
            tier = "shadow_candidate"
        elif shadow_trades < self.config.promotion.min_shadow_trades:
            tier = "shadow"
        elif positive_days_pct >= self.config.promotion.min_positive_days_pct and oos_return_pct > 0 and drawdown_pct >= -0.2:
            tier = "sim_candidate"
        else:
            tier = "shadow"

        next_tier = self.tiers[min(self.tiers.index(tier) + 1, len(self.tiers) - 1)]
        return {
            "market": "hk",
            "strategy_name": str(metrics.get("strategy_name") or "unknown"),
            "currency": "HKD",
            "current_tier": tier,
            "next_tier": next_tier,
            "eligible_for_sim": tier == "sim_candidate",
            "eligible_for_real": False,
            "real_execution": False,
            "capital_layer": "shadow" if tier != "sim" else "simulated",
            "metrics": dict(metrics),
        }


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)
