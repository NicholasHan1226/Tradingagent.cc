#!/usr/bin/env python3
"""Record the frozen 科技+医药 paper Champion through the selection registry.

Host ops run this once after deploy against an explicit ``registry_root``.
The only write path is ``ChampionSelectionRegistry.record_selection`` with
the AUTOMATION actor.  An empty registry stays empty until that call
succeeds; this CLI never seeds a handwritten ``current.json``.

This is a first-paper designation of the in-repo hashed
``FrozenChampionSpec``.  It does not fabricate SampleJournal KPI, round
trips, or an evolution ``promotion_evidence_ready`` decision.  Running
``Ashare.promotion_executor`` against today's empty journal remains an
honest no-op.

``REAL_TRADING_ENABLED`` must stay native false.  Live transition, automatic
risk expansion and enabled systemd timers are out of scope.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from shared.models.champion_registry import (
    ChampionRegistryError,
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

from .paper_champion import (
    LIFECYCLE_RECORDED_AT,
    MANIFEST_CREATED_AT,
    PAPER_CHAMPION_BOOTSTRAP_CONTRACT_ID,
    PAPER_CHAMPION_CATALOG_VERSION,
    PAPER_CHAMPION_CREATED_BY,
    PAPER_CHAMPION_SELECTION_ID,
    PHASE1_NUMERIC_FEATURE_NAMESPACE,
    SELECTION_RECORDED_AT,
    frozen_paper_champion_spec,
    paper_champion_designation_sha256,
    paper_champion_research_snapshot_sha256,
    paper_champion_validation_plan_fields,
)


class PaperChampionBootstrapError(RuntimeError):
    """Raised when the first paper Champion cannot be recorded safely."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _require_simulation_only(*, real_trading_enabled: object) -> None:
    if type(real_trading_enabled) is not bool or real_trading_enabled is not False:
        raise PaperChampionBootstrapError("real_trading_enabled_must_be_native_false")
    if os.environ.get("REAL_TRADING_ENABLED", "false").strip().lower() != "false":
        raise PaperChampionBootstrapError("real_trading_must_remain_disabled")


def _require_explicit_registry_root(registry_root: Path | str) -> Path:
    if not isinstance(registry_root, (str, os.PathLike)) or not os.fspath(
        registry_root
    ):
        raise PaperChampionBootstrapError("registry_root_must_be_explicit")
    root = Path(os.path.abspath(os.fspath(registry_root)))
    if not root.is_absolute() or ".." in Path(os.fspath(registry_root)).parts:
        raise PaperChampionBootstrapError("registry_root_must_be_explicit")
    return root


def _result_payload(
    receipt: ChampionSelectionReceipt,
    *,
    status: str,
    evidence_reference: str,
) -> dict[str, Any]:
    spec = frozen_paper_champion_spec()
    return {
        "status": status,
        "actor": LifecycleActor.AUTOMATION.value,
        "selection_id": receipt.selection_id,
        "receipt_sha256": receipt.receipt_sha256,
        "selected_manifest_id": receipt.selected_manifest_id,
        "selected_manifest_sha256": receipt.selected_manifest_sha256,
        "selected_model_id": receipt.selected_model_id,
        "selected_model_version": receipt.selected_model_version,
        "selected_artifact_sha256": receipt.selected_artifact_sha256,
        "frozen_champion_spec_manifest_sha256": spec.manifest_sha256,
        "promotion_evidence_reference": evidence_reference,
        "recorded_at": receipt.recorded_at.isoformat(),
        "contract_id": PAPER_CHAMPION_BOOTSTRAP_CONTRACT_ID,
        "capital_layer": receipt.capital_layer,
        "account_type": receipt.account_type,
        "simulation_only": receipt.simulation_only,
        "real_trading_enabled": receipt.real_trading_enabled,
        "live_transition_authorized": receipt.live_transition_authorized,
        "automatic_risk_expansion_enabled": receipt.automatic_risk_expansion_enabled,
    }


def _materialize_selection() -> tuple[
    ModelReleaseManifest,
    ValidationPlan,
    LifecycleRecord,
    str,
]:
    spec = frozen_paper_champion_spec()
    designation = paper_champion_designation_sha256()
    evidence_reference = promotion_evidence_reference(designation)
    plan = ValidationPlan(**paper_champion_validation_plan_fields())
    manifest = ModelReleaseManifest(
        manifest_id=PAPER_CHAMPION_SELECTION_ID,
        model_id=spec.champion_id,
        model_version=spec.version,
        artifact_sha256=spec.manifest_sha256,
        training_data_version="trained_through-%s" % spec.trained_through,
        feature_contract_version=PHASE1_NUMERIC_FEATURE_NAMESPACE,
        validation_plan_sha256=plan.sha256(),
        research_snapshot_sha256=paper_champion_research_snapshot_sha256(),
        catalog_version=PAPER_CHAMPION_CATALOG_VERSION,
        validation_evidence_sha256=designation,
        source_commit=designation,
        created_at=MANIFEST_CREATED_AT,
        created_by=PAPER_CHAMPION_CREATED_BY,
        intended_mode="paper",
        metadata=(
            ("bootstrap_contract_id", PAPER_CHAMPION_BOOTSTRAP_CONTRACT_ID),
            ("frozen_champion_spec_manifest_sha256", spec.manifest_sha256),
        ),
    )
    record = LifecycleRecord.draft(manifest=manifest, recorded_at=MANIFEST_CREATED_AT)
    for target in (
        ModelLifecycleState.BACKTEST,
        ModelLifecycleState.SHADOW,
        ModelLifecycleState.REVIEW,
    ):
        record = transition_model(
            record,
            target=target,
            actor=LifecycleActor.AUTOMATION,
            recorded_at=LIFECYCLE_RECORDED_AT,
            reason="paper_champion_bootstrap_designation",
        )
    record = transition_model(
        record,
        target=ModelLifecycleState.CURRENT,
        actor=LifecycleActor.AUTOMATION,
        recorded_at=LIFECYCLE_RECORDED_AT,
        reason="paper_champion_bootstrap_designation",
        approval_reference=evidence_reference,
    )
    return manifest, plan, record, evidence_reference


def bootstrap_paper_champion(
    *,
    registry_root: Path | str,
    real_trading_enabled: bool = False,
) -> dict[str, Any]:
    """Record the frozen paper Champion into ``registry_root`` via record_selection."""

    _require_simulation_only(real_trading_enabled=real_trading_enabled)
    root = _require_explicit_registry_root(registry_root)
    manifest, plan, lifecycle, evidence_reference = _materialize_selection()
    registry = ChampionSelectionRegistry(root)
    try:
        history = registry.load_history()
    except ChampionRegistryError as exc:
        raise PaperChampionBootstrapError("champion_registry_unreadable") from exc
    for receipt in history:
        if receipt.selection_id == PAPER_CHAMPION_SELECTION_ID:
            if receipt.selected_manifest_sha256 != manifest.sha256():
                raise PaperChampionBootstrapError(
                    "paper_champion_selection_id_conflict"
                )
            return _result_payload(
                receipt,
                status="already_recorded",
                evidence_reference=evidence_reference,
            )
    expected_current = history[-1].selected_manifest_sha256 if history else None
    if expected_current is not None:
        raise PaperChampionBootstrapError("champion_current_already_present")
    try:
        receipt = registry.record_selection(
            selection_id=PAPER_CHAMPION_SELECTION_ID,
            action="activate",
            manifest=manifest,
            validation_plan=plan,
            lifecycle=lifecycle,
            actor=LifecycleActor.AUTOMATION,
            human_approval_reference=evidence_reference,
            recorded_at=SELECTION_RECORDED_AT,
            expected_current_manifest_sha256=None,
        )
    except ChampionRegistryError as exc:
        raise PaperChampionBootstrapError("paper_champion_record_selection_failed") from exc
    return _result_payload(
        receipt,
        status="recorded",
        evidence_reference=evidence_reference,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Record the frozen simulation-only 科技+医药 paper Champion "
            "through ChampionSelectionRegistry.record_selection."
        )
    )
    parser.add_argument(
        "--registry-root",
        required=True,
        type=Path,
        help="Explicit Champion selection registry root.  No production default.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result = bootstrap_paper_champion(registry_root=args.registry_root)
    print(_canonical_json(result))
    return 0


__all__ = [
    "PaperChampionBootstrapError",
    "bootstrap_paper_champion",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
