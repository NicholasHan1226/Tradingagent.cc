#!/usr/bin/env python3
"""PM P2 risk control: single-market and correlated-topic caps."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from PM.common import PMConfig, load_pm_config
from shared.markets.analytics import safe_float
from shared.markets.safety import assert_no_real_execution, reject_real_execution_payload


class PMRiskControl:
    """Apply prediction-market exposure caps for shadow/sim portfolios."""

    def __init__(
        self,
        config: PMConfig | None = None,
        *,
        single_market_cap: float = 0.05,
        topic_cap: float = 0.15,
    ) -> None:
        self.config = config or load_pm_config()
        assert_no_real_execution(self.config)
        self.single_market_cap = float(single_market_cap)
        self.topic_cap = float(topic_cap)

    def evaluate(
        self,
        positions: list[dict[str, Any]],
        *,
        correlated_topics: dict[str, str] | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        for row in positions:
            reject_real_execution_payload(row, context="PMRiskControl.position")

        topic_map = correlated_topics or {}
        approved: list[dict[str, Any]] = []
        violations: list[dict[str, Any]] = []
        topic_exposure: dict[str, float] = {}
        for row in positions:
            market_id = str(row.get("market_id") or row.get("symbol") or "")
            exposure = abs(safe_float(row.get("exposure_pct", row.get("weight"))))
            topic = str(row.get("topic") or topic_map.get(market_id) or "uncategorized")
            group = str(topic_map.get(topic) or topic)
            if exposure > self.single_market_cap:
                violations.append({
                    "market_id": market_id,
                    "reason": "single_market_cap",
                    "exposure_pct": round(exposure, 6),
                    "cap": self.single_market_cap,
                })
                continue
            projected = topic_exposure.get(group, 0.0) + exposure
            if projected > self.topic_cap:
                violations.append({
                    "market_id": market_id,
                    "reason": "correlated_topic_cap",
                    "topic": group,
                    "projected_exposure_pct": round(projected, 6),
                    "cap": self.topic_cap,
                })
                continue
            topic_exposure[group] = projected
            approved.append(dict(row, capital_layer="shadow", account_type="shadow"))

        return {
            "market": "pm",
            "currency": self.config.capital.currency,
            "as_of": as_of,
            "capital_layer": "shadow",
            "account_type": "shadow",
            "real_execution": False,
            "single_market_cap": self.single_market_cap,
            "topic_cap": self.topic_cap,
            "topic_exposure": {key: round(value, 6) for key, value in topic_exposure.items()},
            "approved": approved,
            "violations": violations,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }


__all__ = ["PMRiskControl"]

