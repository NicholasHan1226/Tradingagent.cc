"""Deterministic small-account optimizer adapter for DECISION_READY.

The adapter consumes only an already verified simulated account snapshot and
the frozen rank/reduction inputs supplied at construction.  It has no data,
broker, storage, network, or fallback capability.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Iterable

from shared.execution.cost_policy import (
    ASHARE_RESEARCH_COST_POLICY_V1,
    AShareCostPolicy,
)
from shared.portfolio.small_account_optimizer import (
    AccountAuthoritySnapshot,
    AccountAuthorityVerifier,
    CandidateAllocationInput,
    PositionReductionIntent,
    SmallAccountPolicy,
    optimize_small_account,
)
from shared.portfolio.thesis_risk import (
    ThesisRiskRuntimeAuthority,
    apply_group_delta,
)
from shared.portfolio.champion import (
    ChampionContractError,
    ChampionScoreReceipt,
    ChampionSelectionContext,
    ChampionSelectionVerifier,
    FixtureRankEvidence,
    NumericPITFeatureSnapshotVerifier,
    verify_champion_score_receipt,
    verify_fixture_rank_evidence,
)
from shared.capital.market_policy import MarketPolicy

from .day_loop import StageRequest, StageResult
from .run_bundle import ComponentIdentity, RunStage


_NEW_RISK_ACTIONS = frozenset({"open", "increase"})
_REDUCE_ACTIONS = frozenset({"reduce", "exit"})


class SmallAccountStageContractError(RuntimeError):
    """Raised when optimizer evidence cannot be translated without guessing."""


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _aware_utc(value: datetime | str, *, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SmallAccountStageContractError(f"{field_name}_invalid") from exc
    else:
        raise SmallAccountStageContractError(f"{field_name}_invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SmallAccountStageContractError(f"{field_name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _action(*, current_shares: int, target_shares: int, order_shares: int) -> str:
    if order_shares > 0:
        return "open" if current_shares == 0 else "increase"
    if order_shares < 0:
        return "exit" if target_shares == 0 else "reduce"
    return "hold"


class SmallAccountDecisionStagePort:
    """Convert one real optimizer run into the frozen day-loop plan receipt."""

    def __init__(
        self,
        *,
        identity: ComponentIdentity,
        account_snapshot: AccountAuthoritySnapshot,
        candidates: Iterable[CandidateAllocationInput],
        decision_time: datetime,
        account_authority_verifier: AccountAuthorityVerifier,
        thesis_risk_authority: ThesisRiskRuntimeAuthority,
        current_champion_selection_context: ChampionSelectionContext | None = None,
        champion_selection_verifier: ChampionSelectionVerifier | None = None,
        numeric_feature_snapshot_verifier: NumericPITFeatureSnapshotVerifier
        | None = None,
        reduction_intents: Iterable[PositionReductionIntent] = (),
        policy: SmallAccountPolicy | None = None,
        cost_policy: AShareCostPolicy = ASHARE_RESEARCH_COST_POLICY_V1,
        runtime_environment: str = "canonical_simulated",
        promotion_eligible: bool = False,
    ) -> None:
        if (
            not isinstance(identity, ComponentIdentity)
            or identity.stage is not RunStage.DECISION_READY
        ):
            raise ValueError("identity_must_belong_to_decision_ready")
        if not isinstance(account_snapshot, AccountAuthoritySnapshot):
            raise TypeError("verified_account_snapshot_required")
        if not isinstance(thesis_risk_authority, ThesisRiskRuntimeAuthority):
            raise TypeError("thesis_risk_runtime_authority_required")
        if policy is None:
            policy = SmallAccountPolicy.from_market_policy(MarketPolicy.load("ashare"))
        if not isinstance(policy, SmallAccountPolicy):
            raise TypeError("small_account_policy_required")
        if not isinstance(cost_policy, AShareCostPolicy):
            raise TypeError("ashare_cost_policy_required")
        if type(promotion_eligible) is not bool or promotion_eligible:
            raise SmallAccountStageContractError(
                "small_account_stage_promotion_forbidden"
            )
        if (
            thesis_risk_authority.policy_proof.promotion_eligible
            or thesis_risk_authority.exposure_set_proof.promotion_eligible
            or any(
                proof.promotion_eligible
                for proof in thesis_risk_authority.exposure_proofs
            )
        ):
            raise SmallAccountStageContractError(
                "thesis_risk_proof_promotion_forbidden"
            )
        if runtime_environment not in {
            "canonical_simulated",
            "local_candidate",
        }:
            raise SmallAccountStageContractError("runtime_environment_invalid")
        frozen_candidates = tuple(candidates)
        if runtime_environment == "canonical_simulated" and frozen_candidates:
            if (
                not isinstance(
                    current_champion_selection_context,
                    ChampionSelectionContext,
                )
                or not callable(getattr(champion_selection_verifier, "verify", None))
                or not callable(
                    getattr(numeric_feature_snapshot_verifier, "verify", None)
                )
            ):
                raise SmallAccountStageContractError(
                    "current_champion_selection_authority_required"
                )
        source_class = account_snapshot.authority_source_class
        if source_class == "offline_fixture" and runtime_environment != (
            "local_candidate"
        ):
            raise SmallAccountStageContractError(
                "offline_fixture_runtime_environment_invalid"
            )
        if runtime_environment == "local_candidate" and source_class != (
            "offline_fixture"
        ):
            raise SmallAccountStageContractError(
                "local_candidate_requires_offline_fixture_authority"
            )
        self.identity = identity
        self._account_snapshot = account_snapshot
        self._candidates = frozen_candidates
        self._reduction_intents = tuple(reduction_intents)
        self._decision_time = _aware_utc(decision_time, field_name="decision_time")
        if thesis_risk_authority.decision_time != self._decision_time:
            raise SmallAccountStageContractError(
                "thesis_risk_runtime_authority_time_mismatch"
            )
        self._account_authority_verifier = account_authority_verifier
        self._thesis_risk_authority = thesis_risk_authority
        self._current_champion_selection_context = current_champion_selection_context
        self._champion_selection_verifier = champion_selection_verifier
        self._numeric_feature_snapshot_verifier = numeric_feature_snapshot_verifier
        self._policy = policy
        self._cost_policy = cost_policy
        self._runtime_environment = runtime_environment
        self._promotion_eligible = promotion_eligible
        self._results: dict[str, tuple[str, StageResult]] = {}

    @property
    def account_authority_source_class(self) -> str:
        return self._account_snapshot.authority_source_class

    @property
    def account_snapshot(self) -> AccountAuthoritySnapshot:
        """Return the immutable authority input for composition-time binding."""

        return self._account_snapshot

    @property
    def runtime_environment(self) -> str:
        return self._runtime_environment

    @property
    def promotion_eligible(self) -> bool:
        return self._promotion_eligible

    @property
    def thesis_risk_authority(self) -> ThesisRiskRuntimeAuthority:
        return self._thesis_risk_authority

    def execute(self, request: StageRequest) -> StageResult:
        return self._execute_under_runtime_identity(
            request,
            runtime_identity=self.identity,
        )

    def _execute_under_runtime_identity(
        self,
        request: StageRequest,
        *,
        runtime_identity: ComponentIdentity,
    ) -> StageResult:
        if not isinstance(request, StageRequest):
            raise TypeError("stage_request_required")
        if (
            not isinstance(runtime_identity, ComponentIdentity)
            or runtime_identity.stage is not RunStage.DECISION_READY
        ):
            raise SmallAccountStageContractError("decision_runtime_identity_invalid")
        if request.stage is not RunStage.DECISION_READY:
            raise SmallAccountStageContractError("decision_ready_stage_required")
        if request.bundle.next_stage is not RunStage.DECISION_READY:
            raise SmallAccountStageContractError("decision_ready_is_not_next_stage")
        if request.bundle.component_for(RunStage.DECISION_READY) != runtime_identity:
            raise SmallAccountStageContractError("decision_component_identity_mismatch")
        if request.run_id != request.bundle.run_id:
            raise SmallAccountStageContractError("run_identity_mismatch")
        if request.input_bundle_sha256 != request.bundle.bundle_sha256:
            raise SmallAccountStageContractError("input_bundle_identity_mismatch")
        cached = self._results.get(request.idempotency_key)
        if cached is not None:
            cached_input_sha, cached_result = cached
            if cached_input_sha != request.input_bundle_sha256:
                raise SmallAccountStageContractError("idempotency_key_input_conflict")
            return cached_result

        context = request.bundle.context
        if context.real_trading_enabled or context.account_type != "simulated":
            raise SmallAccountStageContractError("simulation_only_boundary_invalid")
        if not request.bundle.position_authority_valid and (
            not request.bundle.stop_new_risk
            or tuple(request.allowed_actions) != ("hold",)
        ):
            raise SmallAccountStageContractError("position_authority_not_verified")
        if (
            context.authority_id != self._account_snapshot.capital_authority_id
            or context.authority_generation
            != self._account_snapshot.authority_generation
        ):
            raise SmallAccountStageContractError("capital_authority_mismatch")
        if _aware_utc(context.decision_as_of, field_name="context_decision_as_of") != (
            self._decision_time
        ):
            raise SmallAccountStageContractError("decision_time_mismatch")

        for candidate in self._candidates:
            try:
                if self._runtime_environment == "canonical_simulated":
                    if not isinstance(
                        candidate.score_evidence,
                        ChampionScoreReceipt,
                    ):
                        raise ChampionContractError(
                            "canonical_champion_score_receipt_required"
                        )
                    verify_champion_score_receipt(
                        candidate.score_evidence,
                        expected_symbol=candidate.symbol,
                        expected_decision_time=self._decision_time,
                        current_selection_context=(
                            self._current_champion_selection_context
                        ),
                        selection_verifier=self._champion_selection_verifier,
                        feature_snapshot_verifier=(
                            self._numeric_feature_snapshot_verifier
                        ),
                    )
                    if (
                        self._current_champion_selection_context is None
                        or context.champion_manifest_sha256
                        != (
                            self._current_champion_selection_context.selection_manifest_sha256
                        )
                    ):
                        raise ChampionContractError(
                            "run_context_champion_selection_mismatch"
                        )
                else:
                    if not isinstance(candidate.score_evidence, FixtureRankEvidence):
                        raise ChampionContractError(
                            "offline_fixture_rank_evidence_required"
                        )
                    verify_fixture_rank_evidence(
                        candidate.score_evidence,
                        expected_symbol=candidate.symbol,
                        expected_decision_time=self._decision_time,
                        expected_champion_selection_manifest_sha256=(
                            context.champion_manifest_sha256
                        ),
                    )
            except ChampionContractError as exc:
                raise SmallAccountStageContractError(
                    "candidate_score_receipt_invalid"
                ) from exc

        new_risk_allowed = _NEW_RISK_ACTIONS.issubset(request.allowed_actions)
        reductions_allowed = bool(
            request.bundle.position_authority_valid
            and _REDUCE_ACTIONS.issubset(request.allowed_actions)
        )
        optimized = optimize_small_account(
            candidates=self._candidates,
            account_snapshot=self._account_snapshot,
            decision_time=self._decision_time,
            account_authority_verifier=self._account_authority_verifier,
            thesis_risk_authority=self._thesis_risk_authority,
            current_champion_selection_context=(
                self._current_champion_selection_context
            ),
            champion_selection_verifier=self._champion_selection_verifier,
            numeric_feature_snapshot_verifier=(self._numeric_feature_snapshot_verifier),
            reduction_intents=(self._reduction_intents if reductions_allowed else ()),
            allow_new_risk=new_risk_allowed,
            policy=self._policy,
            cost_policy=self._cost_policy,
        )
        positions = {
            position.symbol: position for position in self._account_snapshot.positions
        }
        candidates = {candidate.symbol: candidate for candidate in self._candidates}
        translated_group_exposures = {
            (dimension, group_id): exposure_cny
            for dimension, group_id, exposure_cny in (
                optimized.thesis_risk_final_group_exposures
            )
        }
        translated_risk_receipt_shas = set(
            optimized.thesis_risk_exposure_receipt_sha256s
        )
        translated_risk_proof_shas = set(optimized.thesis_risk_exposure_proof_sha256s)
        blocked_effects_by_symbol: dict[str, list[dict[str, Any]]] = {}
        if not new_risk_allowed:
            blocked_receipts = {
                receipt.symbol: receipt
                for receipt in self._thesis_risk_authority.exposure_receipts
                if receipt.exposure_kind == "candidate"
            }
            expected_blocked_symbols = set(candidates)
            if (
                len(blocked_receipts)
                != sum(
                    receipt.exposure_kind == "candidate"
                    for receipt in self._thesis_risk_authority.exposure_receipts
                )
                or set(blocked_receipts) != expected_blocked_symbols
            ):
                raise SmallAccountStageContractError(
                    "blocked_candidate_thesis_risk_receipts_invalid"
                )
            for symbol in sorted(expected_blocked_symbols):
                candidate = candidates[symbol]
                receipt = blocked_receipts[symbol]
                if (
                    receipt.binding_reference_id != candidate.score_receipt_sha256
                    or receipt.binding_sha256 != candidate.score_receipt_sha256
                ):
                    raise SmallAccountStageContractError(
                        "blocked_candidate_thesis_risk_binding_mismatch"
                    )
                proof = next(
                    (
                        row
                        for row in self._thesis_risk_authority.exposure_proofs
                        if row.exposure_receipt_sha256 == receipt.receipt_sha256
                    ),
                    None,
                )
                if proof is None:
                    raise SmallAccountStageContractError(
                        "blocked_candidate_thesis_risk_proof_invalid"
                    )
                effects, cap_exceeded = apply_group_delta(
                    exposures=translated_group_exposures,
                    groups=receipt.groups,
                    requested_delta_cny=0.0,
                    policy=self._thesis_risk_authority.policy,
                    policy_proof_sha256=(optimized.thesis_risk_policy_proof_sha256),
                    enforce_cap=False,
                )
                if cap_exceeded:
                    raise SmallAccountStageContractError(
                        "blocked_candidate_zero_delta_cap_invalid"
                    )
                blocked_effects_by_symbol[symbol] = [
                    asdict(effect) for effect in effects
                ]
                translated_risk_receipt_shas.add(receipt.receipt_sha256)
                translated_risk_proof_shas.add(proof.proof_sha256)
        plan_rows: list[dict[str, Any]] = []
        decision_rows: list[dict[str, Any]] = []
        translated_target_gross = 0.0
        translated_cash_after = float(self._account_snapshot.available_cash_cny)

        for allocation in optimized.decisions:
            action = _action(
                current_shares=allocation.current_shares,
                target_shares=allocation.target_shares,
                order_shares=allocation.order_shares,
            )
            if action not in request.allowed_actions:
                raise SmallAccountStageContractError("optimizer_action_not_allowed")
            if request.bundle.stop_new_risk and action in {"open", "increase"}:
                raise SmallAccountStageContractError("new_risk_blocked_by_bundle")
            if action in {"reduce", "exit"} and allocation.reduction_intent_id is None:
                raise SmallAccountStageContractError("sell_without_reduction_intent")

            position = positions.get(allocation.symbol)
            candidate = candidates.get(allocation.symbol)
            if position is not None:
                valuation_price = position.mark_price_cny
            elif candidate is not None:
                valuation_price = candidate.decision_reference_price
            else:
                raise SmallAccountStageContractError(
                    "optimizer_valuation_source_missing"
                )
            reservation_price = (
                valuation_price
                if action == "hold" and position is not None
                else allocation.conservative_planning_price_cny
            )
            quantity = abs(allocation.order_shares)
            target_notional = allocation.target_notional_cny
            decision_identity = hashlib.sha256(
                f"{optimized.plan_sha256}:{allocation.symbol}".encode("utf-8")
            ).hexdigest()[:24]
            decision_id = f"small-account-{decision_identity}"
            plan_row = {
                "decision_id": decision_id,
                "symbol": allocation.symbol,
                "action": action,
                "current_shares": allocation.current_shares,
                "sellable_shares": allocation.sellable_shares,
                "target_shares": allocation.target_shares,
                "order_quantity": quantity,
                "valuation_price_cny": valuation_price,
                "reservation_price_cny": reservation_price,
                "estimated_order_cost_cny": allocation.estimated_order_cost_cny,
                "target_notional_cny": target_notional,
                "reason_codes": list(allocation.reason_codes),
                "thesis_risk_evaluated_order_shares": (
                    allocation.thesis_risk_evaluated_order_shares
                ),
                "thesis_risk_group_effects": [
                    asdict(effect) for effect in allocation.thesis_risk_group_effects
                ],
            }
            plan_rows.append(plan_row)
            requested_notional = (
                round(quantity * reservation_price, 6) if action != "hold" else 0.0
            )
            decision_row: dict[str, Any] = {
                "decision_id": decision_id,
                "decision_cluster_id": f"small-account-position-{allocation.symbol}",
                "symbol": allocation.symbol,
                "action": action,
                "target_shares": allocation.target_shares,
                "requested_notional_cny": requested_notional,
                "score_semantics": allocation.score_semantics,
                "rank_score": (candidate.rank_score if candidate is not None else 0.0),
                "score_receipt_sha256": (
                    candidate.score_receipt_sha256 if candidate is not None else None
                ),
                "score_evidence_class": (
                    candidate.score_evidence_class
                    if candidate is not None
                    else "existing_position_hold"
                ),
                "sizing_method": "fixed_minimum_economic_probe_v1",
                "reason_codes": list(allocation.reason_codes),
                "thesis_risk_evaluated_order_shares": (
                    allocation.thesis_risk_evaluated_order_shares
                ),
                "thesis_risk_group_effects": [
                    asdict(effect) for effect in allocation.thesis_risk_group_effects
                ],
            }
            if allocation.reduction_intent_id is not None:
                decision_row.update(
                    {
                        "source_reduction_intent_id": allocation.reduction_intent_id,
                        "source_reduction_intent_action": (
                            allocation.reduction_intent_action
                        ),
                    }
                )
            decision_rows.append(decision_row)
            translated_target_gross += target_notional
            if action in {"open", "increase"}:
                translated_cash_after -= (
                    requested_notional + allocation.estimated_order_cost_cny
                )
            elif action in {"reduce", "exit"}:
                translated_cash_after += (
                    requested_notional - allocation.estimated_order_cost_cny
                )

        if not new_risk_allowed:
            if "hold" not in request.allowed_actions:
                raise SmallAccountStageContractError(
                    "blocked_candidate_hold_not_allowed"
                )
            planned_symbols = {row["symbol"] for row in plan_rows}
            for candidate in sorted(self._candidates, key=lambda row: row.symbol):
                if candidate.symbol in planned_symbols:
                    continue
                decision_identity = hashlib.sha256(
                    (
                        f"{optimized.plan_sha256}:blocked-new-risk:{candidate.symbol}"
                    ).encode("utf-8")
                ).hexdigest()[:24]
                decision_id = f"small-account-{decision_identity}"
                blocked_effects = blocked_effects_by_symbol[candidate.symbol]
                plan_rows.append(
                    {
                        "decision_id": decision_id,
                        "symbol": candidate.symbol,
                        "action": "hold",
                        "current_shares": 0,
                        "sellable_shares": 0,
                        "target_shares": 0,
                        "order_quantity": 0,
                        "valuation_price_cny": (candidate.decision_reference_price),
                        "reservation_price_cny": (candidate.decision_reference_price),
                        "estimated_order_cost_cny": 0.0,
                        "target_notional_cny": 0.0,
                        "reason_codes": ["new_risk_blocked_by_bundle"],
                        "thesis_risk_evaluated_order_shares": 0,
                        "thesis_risk_group_effects": blocked_effects,
                    }
                )
                decision_rows.append(
                    {
                        "decision_id": decision_id,
                        "decision_cluster_id": (
                            f"small-account-position-{candidate.symbol}"
                        ),
                        "symbol": candidate.symbol,
                        "action": "hold",
                        "target_shares": 0,
                        "requested_notional_cny": 0.0,
                        "score_semantics": ("uncalibrated_deterministic_rank_score"),
                        "rank_score": candidate.rank_score,
                        "score_receipt_sha256": candidate.score_receipt_sha256,
                        "score_evidence_class": candidate.score_evidence_class,
                        "sizing_method": "fixed_minimum_economic_probe_v1",
                        "reason_codes": ["new_risk_blocked_by_bundle"],
                        "thesis_risk_evaluated_order_shares": 0,
                        "thesis_risk_group_effects": blocked_effects,
                    }
                )

        translated_target_gross = round(translated_target_gross, 6)
        translated_cash_after = round(translated_cash_after, 6)
        starting_gross = self._account_snapshot.current_gross_cny
        target_gross = optimized.target_gross_cny
        cash_after = optimized.cash_after_orders_cny
        current_equity = optimized.current_equity_cny
        risk_budget = optimized.risk_budget_base_cny
        if (
            abs(translated_target_gross - target_gross) > 1e-6
            or abs(translated_cash_after - cash_after) > 1e-6
            or abs(
                current_equity
                - (
                    self._account_snapshot.available_cash_cny
                    + self._account_snapshot.current_gross_cny
                )
            )
            > 1e-6
            or abs(risk_budget - self._policy.risk_budget_base_cny(current_equity))
            > 1e-6
        ):
            raise SmallAccountStageContractError(
                "optimizer_authority_economics_mismatch"
            )
        if cash_after < 0:
            raise SmallAccountStageContractError("translated_plan_cash_negative")
        if target_gross > risk_budget * self._policy.stock_gross_limit_pct + 1e-6:
            raise SmallAccountStageContractError("translated_plan_gross_limit_exceeded")
        if any(
            row["target_notional_cny"]
            > risk_budget * self._policy.single_name_max_pct + 1e-6
            for row in plan_rows
        ):
            raise SmallAccountStageContractError(
                "translated_plan_single_name_limit_exceeded"
            )

        unsigned_plan = {
            "schema_version": "tradingagent.small_account_plan_receipt.v1",
            "policy_id": optimized.policy_id,
            "cost_policy_id": self._cost_policy.policy_id,
            "capital_authority_id": optimized.capital_authority_id,
            "authority_generation": optimized.authority_generation,
            "account_as_of": optimized.account_as_of.isoformat(),
            "position_snapshot_receipt_id": optimized.position_snapshot_receipt_id,
            "position_snapshot_sha256": optimized.position_snapshot_sha256,
            "verification_receipt_sha256": optimized.verification_receipt_sha256,
            "current_equity_cny": current_equity,
            "risk_budget_base_cny": risk_budget,
            "max_positions": optimized.max_positions,
            "starting_available_cash_cny": self._account_snapshot.available_cash_cny,
            "starting_gross_cny": starting_gross,
            "target_gross_cny": target_gross,
            "cash_after_orders_cny": cash_after,
            "plan_decisions": plan_rows,
            "thesis_risk_policy_id": optimized.thesis_risk_policy_id,
            "thesis_risk_policy_sha256": optimized.thesis_risk_policy_sha256,
            "thesis_risk_policy_proof_sha256": (
                optimized.thesis_risk_policy_proof_sha256
            ),
            "thesis_risk_exposure_receipt_sha256s": list(
                sorted(translated_risk_receipt_shas)
            ),
            "thesis_risk_exposure_proof_sha256s": list(
                sorted(translated_risk_proof_shas)
            ),
            "thesis_risk_exposure_set_id": optimized.thesis_risk_exposure_set_id,
            "thesis_risk_exposure_set_sha256": (
                optimized.thesis_risk_exposure_set_sha256
            ),
            "thesis_risk_exposure_set_proof_sha256": (
                optimized.thesis_risk_exposure_set_proof_sha256
            ),
            "thesis_risk_runtime_authority_sha256": (
                optimized.thesis_risk_runtime_authority_sha256
            ),
            "thesis_risk_initial_group_exposures": [
                {
                    "dimension": dimension,
                    "group_id": group_id,
                    "exposure_cny": exposure_cny,
                }
                for dimension, group_id, exposure_cny in (
                    optimized.thesis_risk_initial_group_exposures
                )
            ],
            "thesis_risk_final_group_exposures": [
                {
                    "dimension": dimension,
                    "group_id": group_id,
                    "exposure_cny": exposure_cny,
                }
                for (dimension, group_id), exposure_cny in sorted(
                    translated_group_exposures.items()
                )
            ],
        }
        plan = {**unsigned_plan, "plan_sha256": _canonical_sha256(unsigned_plan)}
        result = StageResult(
            payload={
                "champion_manifest_sha256": context.champion_manifest_sha256,
                "optimizer_policy_version": optimized.policy_id,
                "optimizer_plan_sha256": optimized.plan_sha256,
                "small_account_plan": plan,
                "decisions": decision_rows,
            }
        )
        self._results[request.idempotency_key] = (
            request.input_bundle_sha256,
            result,
        )
        return result
