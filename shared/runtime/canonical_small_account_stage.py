"""Dynamic DECISION_READY adapter backed by canonical simulated capital.

The ordinary small-account stage consumes an immutable account snapshot.  This
adapter deliberately creates that snapshot only when the day loop reaches the
decision stage, after PREOPEN has reconciled the one MarketCapital authority.
Every execution therefore re-reads the ledger and obtains a detached verifier;
no fixture account, cached balance, broker, or fallback path exists here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Iterable, Mapping

from shared.capital.market_policy import MarketPolicy
from shared.execution.cost_policy import (
    ASHARE_RESEARCH_COST_POLICY_V1,
    AShareCostPolicy,
)
from shared.portfolio.small_account_optimizer import (
    CandidateAllocationInput,
    PositionReductionIntent,
    SmallAccountPolicy,
)
from shared.portfolio.champion import (
    ChampionSelectionContext,
    ChampionSelectionVerifier,
    NumericPITFeatureSnapshotVerifier,
)
from shared.portfolio.thesis_risk import (
    ThesisRiskRuntimeAuthority,
)

from .canonical_account_authority import build_canonical_account_authority
from .capital_stages import PaperCapitalAccount
from .day_loop import StageRequest, StageResult
from .run_bundle import ComponentIdentity, RunStage
from .small_account_stage import SmallAccountDecisionStagePort


class CanonicalSmallAccountStageError(RuntimeError):
    """Raised when the dynamic canonical decision boundary is ambiguous."""


def _aware(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CanonicalSmallAccountStageError(f"{field_name}_timezone_required")
    if value.utcoffset() is None:
        raise CanonicalSmallAccountStageError(f"{field_name}_timezone_required")
    return value


def _instant(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _canonical_sha256(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CanonicalSmallAccountStageError(
            "canonical_decision_identity_not_serializable"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _candidate_payload(value: CandidateAllocationInput) -> dict[str, object]:
    if not isinstance(value, CandidateAllocationInput):
        raise TypeError("candidate_allocation_input_required")
    return {
        "symbol": value.symbol,
        "rank_score": value.rank_score,
        "score_receipt_sha256": value.score_receipt_sha256,
        "score_evidence_class": value.score_evidence_class,
        "champion_selection_manifest_sha256": (
            value.champion_selection_manifest_sha256
        ),
        "decision_time": _instant(value.decision_time),
        "price_observed_at": _instant(value.price_observed_at),
        "decision_reference_price": value.decision_reference_price,
    }


def _reduction_payload(value: PositionReductionIntent) -> dict[str, object]:
    if not isinstance(value, PositionReductionIntent):
        raise TypeError("position_reduction_intent_required")
    return {
        "intent_id": value.intent_id,
        "symbol": value.symbol,
        "action": value.action,
        "target_shares": value.target_shares,
        "decision_time": _instant(value.decision_time),
    }


def _normalized_mark_observations(
    values: Mapping[str, datetime],
) -> dict[str, datetime]:
    try:
        rows = dict(values)
    except (TypeError, ValueError) as exc:
        raise CanonicalSmallAccountStageError("mark_observations_invalid") from exc
    normalized: dict[str, datetime] = {}
    for raw_symbol, raw_instant in rows.items():
        if not isinstance(raw_symbol, str):
            raise CanonicalSmallAccountStageError("mark_observation_symbol_invalid")
        symbol = raw_symbol.strip().upper()
        if not symbol or symbol in normalized:
            raise CanonicalSmallAccountStageError("mark_observation_symbol_invalid")
        normalized[symbol] = _aware(
            raw_instant,
            field_name="mark_observed_at",
        )
    return normalized


class CanonicalSmallAccountDecisionStagePort:
    """Build a fresh canonical optimizer input at DECISION_READY execution."""

    account_authority_source_class = "canonical_authority"
    runtime_environment = "canonical_simulated"
    promotion_eligible = False

    def __init__(
        self,
        *,
        account: PaperCapitalAccount,
        candidates: Iterable[CandidateAllocationInput],
        decision_time: datetime,
        trade_date: str,
        mark_observed_at: Mapping[str, datetime],
        thesis_risk_authority: ThesisRiskRuntimeAuthority,
        current_champion_selection_context: ChampionSelectionContext | None = None,
        champion_selection_verifier: ChampionSelectionVerifier | None = None,
        numeric_feature_snapshot_verifier: NumericPITFeatureSnapshotVerifier
        | None = None,
        reduction_intents: Iterable[PositionReductionIntent] = (),
        policy: SmallAccountPolicy | None = None,
        cost_policy: AShareCostPolicy = ASHARE_RESEARCH_COST_POLICY_V1,
    ) -> None:
        if type(account) is not PaperCapitalAccount:
            raise TypeError("paper_capital_account_required")
        decision = _aware(decision_time, field_name="decision_time")
        if not isinstance(trade_date, str) or not trade_date.strip():
            raise CanonicalSmallAccountStageError("trade_date_invalid")
        if policy is None:
            policy = SmallAccountPolicy.from_market_policy(MarketPolicy.load("ashare"))
        if not isinstance(policy, SmallAccountPolicy):
            raise TypeError("small_account_policy_required")
        if not isinstance(cost_policy, AShareCostPolicy):
            raise TypeError("ashare_cost_policy_required")
        if not isinstance(thesis_risk_authority, ThesisRiskRuntimeAuthority):
            raise TypeError("thesis_risk_runtime_authority_required")
        if thesis_risk_authority.decision_time != decision:
            raise CanonicalSmallAccountStageError(
                "thesis_risk_runtime_authority_time_mismatch"
            )

        frozen_candidates = tuple(candidates)
        if frozen_candidates and (
            not isinstance(
                current_champion_selection_context,
                ChampionSelectionContext,
            )
            or not callable(getattr(champion_selection_verifier, "verify", None))
            or not callable(getattr(numeric_feature_snapshot_verifier, "verify", None))
        ):
            raise CanonicalSmallAccountStageError(
                "current_champion_selection_authority_required"
            )
        frozen_reductions = tuple(reduction_intents)
        candidate_payloads = tuple(
            _candidate_payload(candidate) for candidate in frozen_candidates
        )
        reduction_payloads = tuple(
            _reduction_payload(intent) for intent in frozen_reductions
        )
        observations = _normalized_mark_observations(mark_observed_at)
        identity_payload = {
            "contract": "tradingagent.canonical_small_account_decision.v1",
            "account_identity_sha256": account.identity_sha256,
            "candidates": candidate_payloads,
            "current_champion_selection_context_sha256": (
                current_champion_selection_context.context_sha256
                if current_champion_selection_context is not None
                else None
            ),
            "current_champion_selection_receipt_sha256": (
                current_champion_selection_context.selection_receipt_sha256
                if current_champion_selection_context is not None
                else None
            ),
            "reduction_intents": reduction_payloads,
            "decision_time": _instant(decision),
            "trade_date": trade_date.strip(),
            "mark_observed_at": {
                symbol: _instant(observed_at)
                for symbol, observed_at in sorted(observations.items())
            },
            "small_account_policy": asdict(policy),
            "thesis_risk_runtime_authority_sha256": (
                thesis_risk_authority.authority_sha256
            ),
            "cost_policy": asdict(cost_policy),
            "runtime_environment": self.runtime_environment,
            "promotion_eligible": self.promotion_eligible,
        }
        self.identity = ComponentIdentity(
            stage=RunStage.DECISION_READY,
            component_id="canonical-small-account-decision",
            version="1",
            artifact_sha256=_canonical_sha256(identity_payload),
        )
        self._account = account
        self._candidates = frozen_candidates
        self._current_champion_selection_context = current_champion_selection_context
        self._champion_selection_verifier = champion_selection_verifier
        self._numeric_feature_snapshot_verifier = numeric_feature_snapshot_verifier
        self._reduction_intents = frozen_reductions
        self._decision_time = decision
        self._trade_date = trade_date.strip()
        self._mark_observed_at = observations
        self._policy = policy
        self._cost_policy = cost_policy
        self._thesis_risk_authority = thesis_risk_authority

    def is_bound_to(
        self,
        *,
        account: PaperCapitalAccount,
        trade_date: str,
        decision_time: datetime,
    ) -> bool:
        """Prove composition inputs match this immutable decision authority."""

        if (
            type(account) is not PaperCapitalAccount
            or not isinstance(trade_date, str)
            or not trade_date.strip()
        ):
            return False
        try:
            decision = _aware(decision_time, field_name="decision_time")
        except CanonicalSmallAccountStageError:
            return False
        return (
            self._account is account
            and self._trade_date == trade_date.strip()
            and self._decision_time.astimezone(timezone.utc)
            == decision.astimezone(timezone.utc)
        )

    def execute(self, request: StageRequest) -> StageResult:
        return self._execute_under_runtime_identity(
            request,
            runtime_identity=self.identity,
        )

    @property
    def thesis_risk_authority(self) -> ThesisRiskRuntimeAuthority:
        return self._thesis_risk_authority

    def _execute_under_runtime_identity(
        self,
        request: StageRequest,
        *,
        runtime_identity: ComponentIdentity,
    ) -> StageResult:
        """Re-read canonical capital before every optimizer invocation.

        The inner stage is intentionally constructed per call.  Day-loop
        idempotency lives in the durable RunBundle; this adapter must never
        return a process-local cached decision after the capital head changes.
        """

        if not isinstance(request, StageRequest):
            raise TypeError("stage_request_required")
        if (
            not isinstance(runtime_identity, ComponentIdentity)
            or runtime_identity.stage is not RunStage.DECISION_READY
        ):
            raise CanonicalSmallAccountStageError("decision_runtime_identity_invalid")
        snapshot, verifier = build_canonical_account_authority(
            account=self._account,
            decision_time=self._decision_time,
            trade_date=self._trade_date,
            mark_observed_at=self._mark_observed_at,
        )
        inner = SmallAccountDecisionStagePort(
            identity=runtime_identity,
            account_snapshot=snapshot,
            candidates=self._candidates,
            decision_time=self._decision_time,
            account_authority_verifier=verifier,
            thesis_risk_authority=self._thesis_risk_authority,
            current_champion_selection_context=(
                self._current_champion_selection_context
            ),
            champion_selection_verifier=self._champion_selection_verifier,
            numeric_feature_snapshot_verifier=(self._numeric_feature_snapshot_verifier),
            reduction_intents=self._reduction_intents,
            policy=self._policy,
            cost_policy=self._cost_policy,
            runtime_environment=self.runtime_environment,
            promotion_eligible=False,
        )
        return inner._execute_under_runtime_identity(
            request,
            runtime_identity=runtime_identity,
        )


__all__ = [
    "CanonicalSmallAccountDecisionStagePort",
    "CanonicalSmallAccountStageError",
]
