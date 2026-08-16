#!/usr/bin/env python3
"""Evidence-gated automatic Champion promotion for the A-share simulation domain.

A promotion executes only when an evolution decision carries
``promotion_evidence_ready=True`` and ``execute_automatic_promotion`` as its
recommended action.  The selected challenger is materialized as a
ModelReleaseManifest + ValidationPlan + LifecycleRecord triple and recorded in
the durable Champion selection registry by the AUTOMATION actor, bound to a
content-addressed promotion evidence reference.  There is no human review gate
inside the simulation domain.

Every artifact stays simulation-only: ``real_trading_enabled=False``,
``live_transition_authorized=False`` and
``automatic_risk_expansion_enabled=False`` are enforced by the manifest,
lifecycle record, receipt and registry contracts.  A missing or unqualified
challenger is an explicit no-op with a recorded reason; a promotion is never
fabricated.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

from shared.models.champion_registry import (
    ChampionSelectionReceipt,
    ChampionSelectionRegistry,
)
from shared.models.lifecycle import (
    LifecycleActor,
    LifecycleRecord,
    ModelLifecycleState,
    ValidationPlan,
    promotion_evidence_reference,
    transition_model,
)
from shared.models.release_manifest import ModelReleaseManifest


class PromotionExecutionError(RuntimeError):
    """Raised when an automatic promotion input cannot be trusted."""


_SHA256_HEX = frozenset("0123456789abcdef")

_CANDIDATE_TEXT_FIELDS = (
    "challenger_id",
    "challenger_version",
    "training_data_version",
    "feature_contract_version",
    "catalog_version",
    "source_commit",
    "created_by",
)
_CANDIDATE_SHA256_FIELDS = (
    "artifact_sha256",
    "research_snapshot_sha256",
    "validation_evidence_sha256",
)
_DECISION_SAFETY_FIELDS = (
    "real_trading_enabled",
    "live_transition_authorized",
    "automatic_risk_expansion_enabled",
)


def _canonical_decision_sha256(decision: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            decision,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PromotionExecutionError("promotion_decision_not_canonical") from exc
    return hashlib.sha256(encoded).hexdigest()


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PromotionExecutionError("challenger_%s_invalid" % field_name)
    return value


def _require_sha256(value: Any, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in _SHA256_HEX for char in value)
    ):
        raise PromotionExecutionError("challenger_%s_invalid" % field_name)
    return value


def _validate_decision(decision: Any) -> Mapping[str, Any]:
    if not isinstance(decision, Mapping):
        raise PromotionExecutionError("promotion_decision_invalid")
    for field_name in _DECISION_SAFETY_FIELDS:
        if decision.get(field_name) is not False:
            raise PromotionExecutionError("promotion_decision_not_simulation_only")
    policy = decision.get("policy")
    policy_map = policy if isinstance(policy, Mapping) else {}
    for field_name in ("real_trading_enabled", "automatic_risk_expansion_enabled"):
        if policy_map.get(field_name) is not False:
            raise PromotionExecutionError("promotion_decision_not_simulation_only")
    return decision


def _materialize_challenger(
    candidate: Any,
    *,
    evidence_reference: str,
    recorded_at: datetime,
) -> Tuple[ModelReleaseManifest, ValidationPlan, LifecycleRecord]:
    """Materialize one challenger as a manifest/plan/lifecycle triple."""

    if not isinstance(candidate, Mapping):
        raise PromotionExecutionError("challenger_candidate_invalid")
    fields = {
        name: _require_text(candidate.get(name), name)
        for name in _CANDIDATE_TEXT_FIELDS
    }
    fields.update(
        {
            name: _require_sha256(candidate.get(name), name)
            for name in _CANDIDATE_SHA256_FIELDS
        }
    )
    plan = candidate.get("validation_plan")
    if not isinstance(plan, ValidationPlan):
        raise PromotionExecutionError("challenger_validation_plan_invalid")
    created_at = candidate.get("created_at")
    if created_at is None:
        created_at = recorded_at
    if (
        not isinstance(created_at, datetime)
        or created_at.tzinfo is None
        or created_at.utcoffset() is None
    ):
        raise PromotionExecutionError("challenger_created_at_invalid")
    if not (plan.frozen_at <= created_at <= recorded_at):
        raise PromotionExecutionError("challenger_created_at_invalid")

    manifest = ModelReleaseManifest(
        manifest_id="ashare-challenger-%s-%s"
        % (fields["challenger_id"], fields["challenger_version"]),
        model_id=fields["challenger_id"],
        model_version=fields["challenger_version"],
        artifact_sha256=fields["artifact_sha256"],
        training_data_version=fields["training_data_version"],
        feature_contract_version=fields["feature_contract_version"],
        validation_plan_sha256=plan.sha256(),
        research_snapshot_sha256=fields["research_snapshot_sha256"],
        catalog_version=fields["catalog_version"],
        validation_evidence_sha256=fields["validation_evidence_sha256"],
        source_commit=fields["source_commit"],
        created_at=created_at,
        created_by=fields["created_by"],
        intended_mode="paper",
    )
    record = LifecycleRecord.draft(manifest=manifest, recorded_at=created_at)
    for target in (
        ModelLifecycleState.BACKTEST,
        ModelLifecycleState.SHADOW,
        ModelLifecycleState.REVIEW,
    ):
        record = transition_model(
            record,
            target=target,
            actor=LifecycleActor.AUTOMATION,
            recorded_at=recorded_at,
            reason="promotion_evidence_ready",
        )
    record = transition_model(
        record,
        target=ModelLifecycleState.CURRENT,
        actor=LifecycleActor.AUTOMATION,
        recorded_at=recorded_at,
        reason="promotion_evidence_ready",
        approval_reference=evidence_reference,
    )
    return manifest, plan, record


def _no_op(reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "status": "no_op",
        "reason": reason,
        "actor": LifecycleActor.AUTOMATION.value,
        "simulation_only": True,
        "real_trading_enabled": False,
        "live_transition_authorized": False,
        "automatic_risk_expansion_enabled": False,
        **extra,
    }


def _promoted(
    receipt: ChampionSelectionReceipt,
    *,
    evidence_reference: str,
    status: str,
) -> dict[str, Any]:
    return {
        "status": status,
        "actor": LifecycleActor.AUTOMATION.value,
        "selection_id": receipt.selection_id,
        "receipt_sha256": receipt.receipt_sha256,
        "selected_manifest_id": receipt.selected_manifest_id,
        "selected_manifest_sha256": receipt.selected_manifest_sha256,
        "selected_model_id": receipt.selected_model_id,
        "selected_model_version": receipt.selected_model_version,
        "promotion_evidence_reference": evidence_reference,
        "recorded_at": receipt.recorded_at.isoformat(),
        "simulation_only": True,
        "real_trading_enabled": False,
        "live_transition_authorized": False,
        "automatic_risk_expansion_enabled": False,
    }


def execute_automatic_promotion(
    decision: Mapping[str, Any],
    *,
    registry_root: Path | str,
    challenger_candidates: Optional[Sequence[Mapping[str, Any]]] = None,
    recorded_at: Optional[datetime] = None,
) -> dict[str, Any]:
    """Execute the evidence-gated automatic Champion promotion.

    Returns a ``promoted``/``already_promoted`` result carrying the durable
    receipt identity, or an explicit ``no_op`` result with the reason.  Unsafe
    or untrusted inputs raise :class:`PromotionExecutionError` (fail closed);
    registry contract violations propagate as
    :class:`shared.models.champion_registry.ChampionRegistryError`.
    """

    decision = _validate_decision(decision)
    if not (
        decision.get("promotion_evidence_ready") is True
        and decision.get("recommended_action") == "execute_automatic_promotion"
    ):
        return _no_op("promotion_evidence_not_ready")

    if recorded_at is None:
        recorded_at = datetime.now(timezone.utc)
    if not isinstance(recorded_at, datetime) or recorded_at.tzinfo is None:
        raise PromotionExecutionError("promotion_recorded_at_invalid")
    if not isinstance(registry_root, (str, os.PathLike)) or not os.fspath(
        registry_root
    ):
        raise PromotionExecutionError("promotion_registry_root_must_be_explicit")

    evidence_reference = promotion_evidence_reference(
        _canonical_decision_sha256(decision)
    )

    materialized: Optional[
        Tuple[ModelReleaseManifest, ValidationPlan, LifecycleRecord]
    ] = None
    rejections: list[str] = []
    for candidate in challenger_candidates or ():
        try:
            materialized = _materialize_challenger(
                candidate,
                evidence_reference=evidence_reference,
                recorded_at=recorded_at,
            )
            break
        except PromotionExecutionError as exc:
            rejections.append(str(exc))
    if materialized is None:
        return _no_op("no_qualified_challenger", challenger_rejections=rejections)

    manifest, plan, lifecycle = materialized
    registry = ChampionSelectionRegistry(Path(registry_root))
    history = registry.load_history()
    selection_id = "ashare-auto-promotion-%s-%s" % (
        evidence_reference.rsplit(":", 1)[-1][:16],
        manifest.sha256()[:16],
    )
    for receipt in history:
        if receipt.selection_id == selection_id:
            if receipt.selected_manifest_sha256 != manifest.sha256():
                raise PromotionExecutionError("promotion_selection_id_conflict")
            return _promoted(
                receipt,
                evidence_reference=evidence_reference,
                status="already_promoted",
            )
    expected_current = history[-1].selected_manifest_sha256 if history else None
    receipt = registry.record_selection(
        selection_id=selection_id,
        action="activate",
        manifest=manifest,
        validation_plan=plan,
        lifecycle=lifecycle,
        actor=LifecycleActor.AUTOMATION,
        human_approval_reference=evidence_reference,
        recorded_at=recorded_at,
        expected_current_manifest_sha256=expected_current,
    )
    return _promoted(
        receipt,
        evidence_reference=evidence_reference,
        status="promoted",
    )


__all__ = [
    "PromotionExecutionError",
    "execute_automatic_promotion",
]
