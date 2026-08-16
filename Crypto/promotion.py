#!/usr/bin/env python3
"""Read-only Crypto research scorecard pending the C3 automatic lifecycle gate.

This module intentionally has no lifecycle mutation authority.  It must not
turn the absence of the C3 Champion/Challenger registry into a human approval
gate: evidence remains read-only until the automatic scientific gate and its
durable simulation-only receipt chain are implemented.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from Crypto.common import CryptoConfig, load_crypto_config
from Crypto.validation import CryptoForwardValidation
from shared.markets.safety import (
    assert_no_real_execution,
    reject_real_execution_payload,
)


class CryptoStrategyPromotion:
    """Evaluate historical evidence without authorizing any lifecycle change."""

    TIERS = (
        "research",
        "shadow_candidate",
        "shadow",
        "sim_candidate",
        "sim",
    )

    def __init__(
        self,
        config: CryptoConfig | None = None,
        *,
        records: Iterable[dict[str, Any]] | None = None,
        train_end: str | None = None,
    ) -> None:
        self.config = config or load_crypto_config()
        assert_no_real_execution(self.config)
        self.records = [dict(row) for row in (records or [])]
        self.train_end = train_end
        for row in self.records:
            reject_real_execution_payload(
                row, context="CryptoStrategyPromotion.records"
            )

    def score(self, strategy_name: str, as_of: str | None = None) -> dict[str, Any]:
        rows = [
            row
            for row in self.records
            if str(row.get("strategy") or row.get("strategy_name") or "")
            == strategy_name
        ]
        validation = CryptoForwardValidation(
            self.config,
            records=rows,
            train_end=self.train_end,
        ).evaluate(as_of=as_of)
        evidence_tier = self._evidence_tier(validation)
        return {
            "market": "crypto",
            "strategy_name": strategy_name,
            "capital_layer": "shadow",
            "target_layer": "shadow",
            "tier": evidence_tier,
            # C3 has not yet installed a durable simulation lifecycle authority.
            # Therefore this scorecard cannot register/promote a candidate, but
            # the blocker is machine evidence/implementation -- not a person.
            "eligible_for_sim": False,
            "automatic_promotion_enabled": False,
            "promotion_authority": False,
            "manual_review_required": False,
            "human_approval_required": False,
            "automatic_scientific_gate_required": True,
            "lifecycle_state": "automatic_scientific_gate_pending",
            "lifecycle_blocker": "crypto_c3_registry_not_implemented",
            # Compatibility field retained for readers of the old projection.
            "retirement_reason": "crypto_c3_registry_not_implemented",
            "real_execution": False,
            "validation": validation,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def _evidence_tier(self, validation: dict[str, Any]) -> str:
        """Return a non-authoritative evidence band, capped before ``sim``."""

        count = int(validation.get("oos_count") or 0)
        win_rate = float(validation.get("win_rate") or 0.0)
        total_pnl = float(validation.get("total_pnl") or 0.0)
        quality = validation.get("sample_quality", {})
        quality_score = int(quality.get("score") or 0)
        min_shadow = int(self.config.promotion.min_shadow_trades)
        min_positive_days = float(self.config.promotion.min_positive_days_pct)
        if count <= 0 or total_pnl < 0:
            return self.TIERS[0]
        if quality_score < 45:
            return self.TIERS[1]
        if count < min_shadow:
            return self.TIERS[2]
        if win_rate < min_positive_days:
            return self.TIERS[3]
        return self.TIERS[3]


__all__ = ["CryptoStrategyPromotion"]
