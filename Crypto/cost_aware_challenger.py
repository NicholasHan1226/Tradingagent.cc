"""Frozen, simulation-only cost-floor Challenger for future paper evidence.

The existing G5 Champion remains immutable.  This module defines exactly one
separate Challenger whose entry gate exceeds the deterministic two-leg fee and
slippage model by a fixed margin.  It neither selects parameters from results
nor grants any promotion, execution, or capital authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from Crypto.fixture_auto_sim import FROZEN_CHAMPION, evaluate_frozen_champion
from Crypto.fixture_sim.contracts import (
    DECISION_CONTRACT,
    CryptoSafetyError,
    QualifiedFixtureEvidence,
    TimeframeDecision,
    _canonical_value,
    _sha256,
)


COST_AWARE_CHALLENGER_CONTRACT = "tradingagent.crypto.cost_aware_challenger.v1"
ROUND_TRIP_FEE_RATE = Decimal("0.001")
ROUND_TRIP_SLIPPAGE_BPS = Decimal("2")
ROUND_TRIP_HALF_SPREAD_BPS = Decimal("1")
ENTRY_MARGIN_RETURN = Decimal("0.0006")
ROUND_TRIP_COST_FLOOR_RETURN = Decimal("0.0032")


@dataclass(frozen=True)
class CostAwareChallenger:
    """One pre-registered Candidate; it is not a Champion replacement."""

    contract: str = COST_AWARE_CHALLENGER_CONTRACT
    challenger_id: str = "crypto-spot-15m-momentum-cost-floor-v1"
    version: int = 1
    baseline_champion_id: str = FROZEN_CHAMPION.champion_id
    baseline_champion_sha256: str = FROZEN_CHAMPION.sha256
    entry_fee_rate: Decimal = ROUND_TRIP_FEE_RATE
    exit_fee_rate: Decimal = ROUND_TRIP_FEE_RATE
    entry_slippage_bps: Decimal = ROUND_TRIP_SLIPPAGE_BPS
    exit_slippage_bps: Decimal = ROUND_TRIP_SLIPPAGE_BPS
    entry_half_spread_bps: Decimal = ROUND_TRIP_HALF_SPREAD_BPS
    exit_half_spread_bps: Decimal = ROUND_TRIP_HALF_SPREAD_BPS
    entry_margin_return: Decimal = ENTRY_MARGIN_RETURN
    minimum_decision_return: Decimal = ROUND_TRIP_COST_FLOOR_RETURN
    status: str = "preregistered_shadow_challenger"
    manual_promotion_required: bool = True
    promotion_authorized: bool = False
    real_trading_enabled: bool = False

    def __post_init__(self) -> None:
        expected = (
            COST_AWARE_CHALLENGER_CONTRACT,
            "crypto-spot-15m-momentum-cost-floor-v1",
            1,
            FROZEN_CHAMPION.champion_id,
            FROZEN_CHAMPION.sha256,
            ROUND_TRIP_FEE_RATE,
            ROUND_TRIP_FEE_RATE,
            ROUND_TRIP_SLIPPAGE_BPS,
            ROUND_TRIP_SLIPPAGE_BPS,
            ROUND_TRIP_HALF_SPREAD_BPS,
            ROUND_TRIP_HALF_SPREAD_BPS,
            ENTRY_MARGIN_RETURN,
            ROUND_TRIP_COST_FLOOR_RETURN,
            "preregistered_shadow_challenger",
            True,
            False,
            False,
        )
        actual = (
            self.contract,
            self.challenger_id,
            self.version,
            self.baseline_champion_id,
            self.baseline_champion_sha256,
            self.entry_fee_rate,
            self.exit_fee_rate,
            self.entry_slippage_bps,
            self.exit_slippage_bps,
            self.entry_half_spread_bps,
            self.exit_half_spread_bps,
            self.entry_margin_return,
            self.minimum_decision_return,
            self.status,
            self.manual_promotion_required,
            self.promotion_authorized,
            self.real_trading_enabled,
        )
        if actual != expected:
            raise ValueError("cost_aware_challenger_fields_are_immutable")

    @property
    def sha256(self) -> str:
        return _sha256(self)

    def to_payload(self) -> dict[str, object]:
        return _canonical_value(
            {
                "contract": self.contract,
                "challenger_id": self.challenger_id,
                "version": self.version,
                "baseline_champion_id": self.baseline_champion_id,
                "baseline_champion_sha256": self.baseline_champion_sha256,
                "entry_fee_rate": self.entry_fee_rate,
                "exit_fee_rate": self.exit_fee_rate,
                "entry_slippage_bps": self.entry_slippage_bps,
                "exit_slippage_bps": self.exit_slippage_bps,
                "entry_half_spread_bps": self.entry_half_spread_bps,
                "exit_half_spread_bps": self.exit_half_spread_bps,
                "entry_margin_return": self.entry_margin_return,
                "minimum_decision_return": self.minimum_decision_return,
                "status": self.status,
                "manual_promotion_required": self.manual_promotion_required,
                "promotion_authorized": self.promotion_authorized,
                "real_trading_enabled": self.real_trading_enabled,
                "sha256": self.sha256,
            }
        )


COST_AWARE_CHALLENGER = CostAwareChallenger()


def _assert_canonical_challenger(challenger: CostAwareChallenger) -> None:
    if (
        type(challenger) is not CostAwareChallenger
        or challenger != COST_AWARE_CHALLENGER
    ):
        raise CryptoSafetyError("cost_aware_challenger_not_canonical")


def evaluate_cost_aware_challenger(
    evidence: QualifiedFixtureEvidence,
    challenger: CostAwareChallenger = COST_AWARE_CHALLENGER,
) -> TimeframeDecision:
    """Issue one causal shadow decision under the fixed full-cost entry floor."""

    _assert_canonical_challenger(challenger)
    baseline = evaluate_frozen_champion(evidence, FROZEN_CHAMPION)
    if (
        baseline.champion_id != challenger.baseline_champion_id
        or baseline.champion_sha256 != challenger.baseline_champion_sha256
    ):
        raise CryptoSafetyError("cost_aware_challenger_baseline_binding_invalid")
    if (
        baseline.regime == "risk_on"
        and baseline.decision_return >= challenger.minimum_decision_return
    ):
        action = "buy"
        reason = "cost_aware_momentum_cost_floor_passed"
    elif baseline.regime != "risk_on":
        action = "observe"
        reason = "cost_aware_regime_not_risk_on"
    else:
        action = "observe"
        reason = "cost_aware_momentum_cost_floor_not_met"
    material = {
        "contract": DECISION_CONTRACT,
        "challenger_sha256": challenger.sha256,
        "symbol": baseline.symbol,
        "execution_slot": baseline.execution_slot,
        "regime_return": baseline.regime_return,
        "decision_return": baseline.decision_return,
        "action": action,
        "evidence_receipt_id": baseline.evidence_receipt_id,
        "market_evidence_sha256": baseline.market_evidence_sha256,
    }
    return TimeframeDecision(
        contract=DECISION_CONTRACT,
        decision_id=f"crypto-cost-aware-decision-{_sha256(material)[:24]}",
        champion_id=challenger.challenger_id,
        champion_sha256=challenger.sha256,
        symbol=baseline.symbol,
        regime_interval=baseline.regime_interval,
        decision_interval=baseline.decision_interval,
        execution_interval=baseline.execution_interval,
        execution_slot=baseline.execution_slot,
        decision_observed_at=baseline.decision_observed_at,
        regime_return=baseline.regime_return,
        decision_return=baseline.decision_return,
        regime=baseline.regime,
        action=action,
        reason=reason,
        evidence_receipt_id=baseline.evidence_receipt_id,
        market_evidence_sha256=baseline.market_evidence_sha256,
    )


__all__ = [
    "COST_AWARE_CHALLENGER",
    "COST_AWARE_CHALLENGER_CONTRACT",
    "CostAwareChallenger",
    "ENTRY_MARGIN_RETURN",
    "ROUND_TRIP_COST_FLOOR_RETURN",
    "ROUND_TRIP_FEE_RATE",
    "ROUND_TRIP_HALF_SPREAD_BPS",
    "ROUND_TRIP_SLIPPAGE_BPS",
    "evaluate_cost_aware_challenger",
]
