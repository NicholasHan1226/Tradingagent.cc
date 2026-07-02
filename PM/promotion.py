#!/usr/bin/env python3
"""PM P1 strategy promotion scorecard."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from PM.common import PMConfig, load_pm_config
from PM.validation import PMForwardValidation
from shared.markets.safety import reject_real_execution_payload


class PMStrategyPromotion:
    """Five-tier research-to-shadow-to-sim gate for PM strategies."""

    TIERS = (
        "research",
        "shadow_candidate",
        "shadow",
        "sim_candidate",
        "sim",
    )

    def __init__(
        self,
        config: PMConfig | None = None,
        *,
        records: Iterable[dict[str, Any]] | None = None,
        train_end: str | None = None,
    ) -> None:
        self.config = config or load_pm_config()
        self.config.validate()
        self.records = [dict(row) for row in (records or [])]
        self.train_end = train_end
        for row in self.records:
            reject_real_execution_payload(row, context="PMStrategyPromotion.records")

    def score(self, strategy_name: str, as_of: str | None = None) -> dict[str, Any]:
        rows = [
            row
            for row in self.records
            if str(row.get("strategy") or row.get("strategy_name") or "") == strategy_name
        ]
        validation = PMForwardValidation(
            self.config,
            records=rows,
            train_end=self.train_end,
        ).evaluate(as_of=as_of)
        tier = self._tier(validation)
        return {
            "market": "pm",
            "strategy_name": strategy_name,
            "capital_layer": "shadow",
            "target_layer": "simulated" if tier == "sim" else "shadow",
            "tier": tier,
            "eligible_for_sim": tier == "sim",
            "real_execution": False,
            "validation": validation,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def _tier(self, validation: dict[str, Any]) -> str:
        count = int(validation.get("resolved_count") or 0)
        brier = validation.get("brier_score")
        pnl = float(validation.get("pnl") or 0.0)
        min_shadow = int(self.config.promotion.min_shadow_trades)
        if count <= 0:
            return self.TIERS[0]
        if brier is None or float(brier) > 0.25:
            return self.TIERS[1]
        if count < min_shadow:
            return self.TIERS[2]
        if pnl <= 0:
            return self.TIERS[3]
        return self.TIERS[4]


__all__ = ["PMStrategyPromotion"]
