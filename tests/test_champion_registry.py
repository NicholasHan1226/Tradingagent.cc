from __future__ import annotations

import hashlib
import importlib
import json
import multiprocessing
import os
import stat
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from shared.models.lifecycle import (
    LifecycleActor,
    LifecycleRecord,
    ModelLifecycleState,
    ValidationPlan,
    transition_model,
)
from shared.models.release_manifest import ModelReleaseManifest


NOW = datetime(2026, 7, 16, 2, 30, tzinfo=timezone.utc)


def _api():
    try:
        return importlib.import_module("shared.models.champion_registry")
    except ModuleNotFoundError:
        pytest.fail("champion registry module is not implemented")


def _validation_plan(*, suffix: str = "a") -> ValidationPlan:
    return ValidationPlan(
        train_start=date(2025, 1, 1),
        train_end=date(2025, 1, 31),
        validation_start=date(2025, 2, 10),
        validation_end=date(2025, 2, 28),
        test_start=date(2025, 3, 10),
        test_end=date(2025, 3, 31),
        purge_days=5,
        embargo_days=5,
        label_horizon_days=5,
        max_feature_lookback_days=5,
        event_cluster_embargo_days=5,
        decision_cluster_key="decision_cluster_id",
        decision_cluster_deduplicated=True,
        registered_trial_count=1,
        multiple_testing_trial_budget=20,
        pbo_required=True,
        deflated_sharpe_required=True,
        oos_reuse_count=0,
        max_oos_reuse_count=1,
        oos_used_for_tuning=False,
        oos_authority_receipt_sha256=suffix * 64,
        experiment_family_id=f"champion-family-{suffix}",
        experiment_id=f"champion-experiment-{suffix}",
        frozen_test_set_id=f"champion-oos-{suffix}",
        frozen_at=NOW - timedelta(days=1),
    )


def _manifest(
    plan: ValidationPlan,
    *,
    suffix: str = "a",
) -> ModelReleaseManifest:
    return ModelReleaseManifest(
        manifest_id=f"manifest-{suffix}",
        model_id=f"model-{suffix}",
        model_version=f"1.0.{ord(suffix) - ord('a')}",
        artifact_sha256=suffix * 64,
        training_data_version=f"training-{suffix}",
        feature_contract_version="features-v1",
        validation_plan_sha256=plan.sha256(),
        research_snapshot_sha256="b" * 64,
        catalog_version="catalog-v1",
        validation_evidence_sha256="c" * 64,
        source_commit=f"source-{suffix}",
        created_at=NOW,
        created_by="offline-research",
        intended_mode="paper",
    )


def _current_lifecycle(
    manifest: ModelReleaseManifest,
    *,
    approval_reference: str,
    recorded_at: datetime,
) -> LifecycleRecord:
    record = LifecycleRecord.draft(manifest=manifest, recorded_at=recorded_at)
    for target in (
        ModelLifecycleState.BACKTEST,
        ModelLifecycleState.SHADOW,
        ModelLifecycleState.REVIEW,
    ):
        record = transition_model(
            record,
            target=target,
            actor=LifecycleActor.HUMAN_REVIEWER,
            recorded_at=recorded_at,
            reason="manual_gate_passed",
        )
    return transition_model(
        record,
        target=ModelLifecycleState.CURRENT,
        actor=LifecycleActor.HUMAN_REVIEWER,
        recorded_at=recorded_at,
        reason="manual_champion_selection",
        approval_reference=approval_reference,
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _receipt_sha256(payload: dict) -> str:
    unsigned = dict(payload)
    unsigned.pop("receipt_sha256", None)
    return hashlib.sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest()


def _write_json(path: Path, payload: dict) -> None:
    if path.exists() and not path.is_symlink():
        path.chmod(0o600)
    path.write_text(_canonical_json(payload) + "\n", encoding="utf-8")


def _record_two_activations(api, root: Path):
    registry = api.ChampionSelectionRegistry(root)
    plan_a = _validation_plan(suffix="a")
    manifest_a = _manifest(plan_a, suffix="a")
    lifecycle_a = _current_lifecycle(
        manifest_a,
        approval_reference="approval-chain-a",
        recorded_at=NOW + timedelta(minutes=1),
    )
    first = registry.record_selection(
        selection_id="selection-chain-a",
        action="activate",
        manifest=manifest_a,
        validation_plan=plan_a,
        lifecycle=lifecycle_a,
        actor=LifecycleActor.HUMAN_REVIEWER,
        human_approval_reference="approval-chain-a",
        recorded_at=NOW + timedelta(minutes=2),
        expected_current_manifest_sha256=None,
    )
    plan_b = _validation_plan(suffix="b")
    manifest_b = _manifest(plan_b, suffix="b")
    lifecycle_b = _current_lifecycle(
        manifest_b,
        approval_reference="approval-chain-b",
        recorded_at=NOW + timedelta(minutes=3),
    )
    second = registry.record_selection(
        selection_id="selection-chain-b",
        action="activate",
        manifest=manifest_b,
        validation_plan=plan_b,
        lifecycle=lifecycle_b,
        actor=LifecycleActor.HUMAN_REVIEWER,
        human_approval_reference="approval-chain-b",
        recorded_at=NOW + timedelta(minutes=4),
        expected_current_manifest_sha256=first.selected_manifest_sha256,
    )
    return registry, first, second


def _concurrent_first_selection_worker(
    root: str,
    suffix: str,
    barrier,
    results,
) -> None:
    api = importlib.import_module("shared.models.champion_registry")
    plan = _validation_plan(suffix=suffix)
    manifest = _manifest(plan, suffix=suffix)
    lifecycle = _current_lifecycle(
        manifest,
        approval_reference=f"approval-concurrent-{suffix}",
        recorded_at=NOW + timedelta(minutes=1),
    )
    barrier.wait()
    try:
        receipt = api.ChampionSelectionRegistry(Path(root)).record_selection(
            selection_id=f"selection-concurrent-{suffix}",
            action="activate",
            manifest=manifest,
            validation_plan=plan,
            lifecycle=lifecycle,
            actor=LifecycleActor.HUMAN_REVIEWER,
            human_approval_reference=f"approval-concurrent-{suffix}",
            recorded_at=NOW + timedelta(minutes=2),
            expected_current_manifest_sha256=None,
        )
    except api.ChampionRegistryError as exc:
        results.put(("error", str(exc)))
    except Exception as exc:  # pragma: no cover - reported as an assertion failure
        results.put(("unexpected", repr(exc)))
    else:
        results.put(("ok", receipt.receipt_sha256))


def test_manual_activation_persists_content_addressed_simulation_receipt(
    tmp_path: Path,
) -> None:
    api = _api()
    root = tmp_path / "champion-registry"
    plan = _validation_plan()
    manifest = _manifest(plan)
    lifecycle = _current_lifecycle(
        manifest,
        approval_reference="approval-001",
        recorded_at=NOW + timedelta(minutes=1),
    )

    registry = api.ChampionSelectionRegistry(root)
    receipt = registry.record_selection(
        selection_id="selection-001",
        action="activate",
        manifest=manifest,
        validation_plan=plan,
        lifecycle=lifecycle,
        actor=LifecycleActor.HUMAN_REVIEWER,
        human_approval_reference="approval-001",
        recorded_at=NOW + timedelta(minutes=2),
        expected_current_manifest_sha256=None,
    )

    assert receipt.schema_version == registry.SCHEMA_VERSION
    assert receipt.selection_id == "selection-001"
    assert receipt.sequence == 0
    assert receipt.action == "activate"
    assert receipt.selected_manifest_id == manifest.manifest_id
    assert receipt.selected_manifest_sha256 == manifest.sha256()
    assert receipt.selected_model_id == manifest.model_id
    assert receipt.selected_model_version == manifest.model_version
    assert receipt.selected_artifact_sha256 == manifest.artifact_sha256
    assert receipt.validation_plan_sha256 == plan.sha256()
    assert receipt.validation_evidence_sha256 == manifest.validation_evidence_sha256
    assert len(receipt.lifecycle_record_sha256) == 64
    assert receipt.human_approval_reference == "approval-001"
    assert receipt.expected_current_manifest_sha256 is None
    assert receipt.previous_receipt_sha256 is None
    assert len(receipt.receipt_sha256) == 64
    assert receipt.capital_layer == "simulated"
    assert receipt.account_type == "simulated"
    assert receipt.simulation_only is True
    assert receipt.real_trading_enabled is False
    assert receipt.live_transition_authorized is False
    assert receipt.automatic_promotion_enabled is False
    assert receipt.automatic_risk_expansion_enabled is False
    assert registry.load_current() == receipt
    assert registry.load_history() == (receipt,)
    assert (
        root / "receipts" / f"{receipt.sequence:020d}-{receipt.receipt_sha256}.json"
    ).is_file()
    assert (root / "current.json").is_file()


def test_automation_cannot_activate_champion(tmp_path: Path) -> None:
    api = _api()
    root = tmp_path / "champion-registry"
    plan = _validation_plan()
    manifest = _manifest(plan)
    lifecycle = _current_lifecycle(
        manifest,
        approval_reference="approval-automation-rejected",
        recorded_at=NOW + timedelta(minutes=1),
    )

    registry = api.ChampionSelectionRegistry(root)
    with pytest.raises(api.ChampionRegistryError, match="human_reviewer"):
        registry.record_selection(
            selection_id="selection-automation",
            action="activate",
            manifest=manifest,
            validation_plan=plan,
            lifecycle=lifecycle,
            actor=LifecycleActor.AUTOMATION,
            human_approval_reference="approval-automation-rejected",
            recorded_at=NOW + timedelta(minutes=2),
            expected_current_manifest_sha256=None,
        )

    assert not list((root / "receipts").glob("*.json"))
    assert not (root / "current.json").exists()


def test_selection_requires_current_lifecycle_state(tmp_path: Path) -> None:
    api = _api()
    plan = _validation_plan()
    manifest = _manifest(plan)
    draft = LifecycleRecord.draft(
        manifest=manifest,
        recorded_at=NOW + timedelta(minutes=1),
    )
    registry = api.ChampionSelectionRegistry(tmp_path / "champion-registry")

    with pytest.raises(api.ChampionRegistryError, match="lifecycle_must_be_current"):
        registry.record_selection(
            selection_id="selection-draft",
            action="activate",
            manifest=manifest,
            validation_plan=plan,
            lifecycle=draft,
            actor=LifecycleActor.HUMAN_REVIEWER,
            human_approval_reference="approval-not-current",
            recorded_at=NOW + timedelta(minutes=2),
            expected_current_manifest_sha256=None,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "manifest_identity",
        "validation_plan",
        "lifecycle_model_id",
        "lifecycle_model_version",
        "lifecycle_research_snapshot",
        "lifecycle_catalog",
        "lifecycle_validation_plan",
        "lifecycle_validation_evidence",
        "approval_reference",
    ],
)
def test_selection_requires_exact_manifest_plan_lifecycle_and_approval_binding(
    tmp_path: Path,
    mutation: str,
) -> None:
    api = _api()
    plan = _validation_plan()
    manifest = _manifest(plan)
    lifecycle = _current_lifecycle(
        manifest,
        approval_reference="approval-exact-binding",
        recorded_at=NOW + timedelta(minutes=1),
    )
    approval_reference = "approval-exact-binding"

    if mutation == "manifest_identity":
        manifest = replace(manifest, model_id="forged-model")
    elif mutation == "validation_plan":
        plan = replace(plan, experiment_id="forged-plan")
    elif mutation == "lifecycle_model_id":
        lifecycle = replace(lifecycle, model_id="forged-model")
    elif mutation == "lifecycle_model_version":
        lifecycle = replace(lifecycle, model_version="forged-version")
    elif mutation == "lifecycle_research_snapshot":
        lifecycle = replace(lifecycle, research_snapshot_sha256="d" * 64)
    elif mutation == "lifecycle_catalog":
        lifecycle = replace(lifecycle, catalog_version="forged-catalog")
    elif mutation == "lifecycle_validation_plan":
        lifecycle = replace(lifecycle, validation_plan_sha256="e" * 64)
    elif mutation == "lifecycle_validation_evidence":
        lifecycle = replace(lifecycle, validation_evidence_sha256="f" * 64)
    elif mutation == "approval_reference":
        approval_reference = "different-approval"

    registry = api.ChampionSelectionRegistry(tmp_path / f"champion-registry-{mutation}")
    with pytest.raises(api.ChampionRegistryError, match="binding_mismatch"):
        registry.record_selection(
            selection_id=f"selection-{mutation}",
            action="activate",
            manifest=manifest,
            validation_plan=plan,
            lifecycle=lifecycle,
            actor=LifecycleActor.HUMAN_REVIEWER,
            human_approval_reference=approval_reference,
            recorded_at=NOW + timedelta(minutes=2),
            expected_current_manifest_sha256=None,
        )


@pytest.mark.parametrize(
    "scenario",
    [
        "naive_selection_time",
        "selection_precedes_lifecycle",
        "lifecycle_precedes_manifest",
        "manifest_precedes_plan_freeze",
    ],
)
def test_selection_requires_causal_timezone_aware_timestamps(
    tmp_path: Path,
    scenario: str,
) -> None:
    api = _api()
    plan = _validation_plan()
    manifest = _manifest(plan)
    lifecycle_at = NOW + timedelta(minutes=1)
    selection_at = NOW + timedelta(minutes=2)

    if scenario == "naive_selection_time":
        selection_at = selection_at.replace(tzinfo=None)
    elif scenario == "selection_precedes_lifecycle":
        selection_at = NOW
    elif scenario == "lifecycle_precedes_manifest":
        manifest = replace(manifest, created_at=NOW + timedelta(minutes=2))
    elif scenario == "manifest_precedes_plan_freeze":
        plan = replace(plan, frozen_at=NOW + timedelta(minutes=1))
        manifest = replace(manifest, validation_plan_sha256=plan.sha256())
        lifecycle_at = NOW + timedelta(minutes=2)
        selection_at = NOW + timedelta(minutes=3)

    lifecycle = _current_lifecycle(
        manifest,
        approval_reference="approval-time-causal",
        recorded_at=lifecycle_at,
    )
    registry = api.ChampionSelectionRegistry(tmp_path / f"champion-registry-{scenario}")

    with pytest.raises(api.ChampionRegistryError, match="time"):
        registry.record_selection(
            selection_id=f"selection-{scenario}",
            action="activate",
            manifest=manifest,
            validation_plan=plan,
            lifecycle=lifecycle,
            actor=LifecycleActor.HUMAN_REVIEWER,
            human_approval_reference="approval-time-causal",
            recorded_at=selection_at,
            expected_current_manifest_sha256=None,
        )


def test_current_manifest_compare_and_swap_is_mandatory(tmp_path: Path) -> None:
    api = _api()
    root = tmp_path / "champion-registry"
    registry = api.ChampionSelectionRegistry(root)
    plan_a = _validation_plan(suffix="a")
    manifest_a = _manifest(plan_a, suffix="a")
    lifecycle_a = _current_lifecycle(
        manifest_a,
        approval_reference="approval-cas-a",
        recorded_at=NOW + timedelta(minutes=1),
    )

    with pytest.raises(api.ChampionRegistryError, match="compare_and_swap"):
        registry.record_selection(
            selection_id="selection-cas-invalid-first",
            action="activate",
            manifest=manifest_a,
            validation_plan=plan_a,
            lifecycle=lifecycle_a,
            actor=LifecycleActor.HUMAN_REVIEWER,
            human_approval_reference="approval-cas-a",
            recorded_at=NOW + timedelta(minutes=2),
            expected_current_manifest_sha256="f" * 64,
        )
    assert registry.load_history() == ()

    first = registry.record_selection(
        selection_id="selection-cas-a",
        action="activate",
        manifest=manifest_a,
        validation_plan=plan_a,
        lifecycle=lifecycle_a,
        actor=LifecycleActor.HUMAN_REVIEWER,
        human_approval_reference="approval-cas-a",
        recorded_at=NOW + timedelta(minutes=2),
        expected_current_manifest_sha256=None,
    )

    plan_b = _validation_plan(suffix="b")
    manifest_b = _manifest(plan_b, suffix="b")
    lifecycle_b = _current_lifecycle(
        manifest_b,
        approval_reference="approval-cas-b",
        recorded_at=NOW + timedelta(minutes=3),
    )
    for stale_expected in (None, "f" * 64):
        with pytest.raises(api.ChampionRegistryError, match="compare_and_swap"):
            registry.record_selection(
                selection_id=f"selection-cas-stale-{stale_expected}",
                action="activate",
                manifest=manifest_b,
                validation_plan=plan_b,
                lifecycle=lifecycle_b,
                actor=LifecycleActor.HUMAN_REVIEWER,
                human_approval_reference="approval-cas-b",
                recorded_at=NOW + timedelta(minutes=4),
                expected_current_manifest_sha256=stale_expected,
            )
    assert registry.load_history() == (first,)

    second = registry.record_selection(
        selection_id="selection-cas-b",
        action="activate",
        manifest=manifest_b,
        validation_plan=plan_b,
        lifecycle=lifecycle_b,
        actor=LifecycleActor.HUMAN_REVIEWER,
        human_approval_reference="approval-cas-b",
        recorded_at=NOW + timedelta(minutes=4),
        expected_current_manifest_sha256=first.selected_manifest_sha256,
    )

    assert second.sequence == 1
    assert second.previous_receipt_sha256 == first.receipt_sha256
    assert second.expected_current_manifest_sha256 == first.selected_manifest_sha256
    assert registry.load_current() == second
    assert registry.load_history() == (first, second)


def test_selection_id_is_idempotent_only_for_identical_content(
    tmp_path: Path,
) -> None:
    api = _api()
    root = tmp_path / "champion-registry"
    registry = api.ChampionSelectionRegistry(root)
    plan = _validation_plan()
    manifest = _manifest(plan)
    lifecycle = _current_lifecycle(
        manifest,
        approval_reference="approval-idempotent",
        recorded_at=NOW + timedelta(minutes=1),
    )
    request = {
        "selection_id": "selection-idempotent",
        "action": "activate",
        "manifest": manifest,
        "validation_plan": plan,
        "lifecycle": lifecycle,
        "actor": LifecycleActor.HUMAN_REVIEWER,
        "human_approval_reference": "approval-idempotent",
        "recorded_at": NOW + timedelta(minutes=2),
        "expected_current_manifest_sha256": None,
    }

    first = registry.record_selection(**request)
    receipt_path = (
        root / "receipts" / f"{first.sequence:020d}-{first.receipt_sha256}.json"
    )
    receipt_bytes = receipt_path.read_bytes()
    current_bytes = (root / "current.json").read_bytes()

    replay = registry.record_selection(**request)

    assert replay == first
    assert registry.load_history() == (first,)
    assert receipt_path.read_bytes() == receipt_bytes
    assert (root / "current.json").read_bytes() == current_bytes

    with pytest.raises(api.ChampionRegistryError, match="selection_id_conflict"):
        registry.record_selection(
            **{
                **request,
                "recorded_at": NOW + timedelta(minutes=3),
            }
        )
    assert registry.load_history() == (first,)


def test_manual_rollback_only_targets_a_manifest_previously_activated_in_chain(
    tmp_path: Path,
) -> None:
    api = _api()
    root = tmp_path / "champion-registry"
    drift_sentinel = tmp_path / "drift-latch.json"
    drift_sentinel.write_bytes(b'{"latched":true}\n')
    registry = api.ChampionSelectionRegistry(root)

    plan_a = _validation_plan(suffix="a")
    manifest_a = _manifest(plan_a, suffix="a")
    lifecycle_a = _current_lifecycle(
        manifest_a,
        approval_reference="approval-activate-a",
        recorded_at=NOW + timedelta(minutes=1),
    )
    first = registry.record_selection(
        selection_id="selection-activate-a",
        action="activate",
        manifest=manifest_a,
        validation_plan=plan_a,
        lifecycle=lifecycle_a,
        actor=LifecycleActor.HUMAN_REVIEWER,
        human_approval_reference="approval-activate-a",
        recorded_at=NOW + timedelta(minutes=2),
        expected_current_manifest_sha256=None,
    )

    plan_b = _validation_plan(suffix="b")
    manifest_b = _manifest(plan_b, suffix="b")
    lifecycle_b = _current_lifecycle(
        manifest_b,
        approval_reference="approval-activate-b",
        recorded_at=NOW + timedelta(minutes=3),
    )
    second = registry.record_selection(
        selection_id="selection-activate-b",
        action="activate",
        manifest=manifest_b,
        validation_plan=plan_b,
        lifecycle=lifecycle_b,
        actor=LifecycleActor.HUMAN_REVIEWER,
        human_approval_reference="approval-activate-b",
        recorded_at=NOW + timedelta(minutes=4),
        expected_current_manifest_sha256=first.selected_manifest_sha256,
    )
    immutable_history = {
        path.name: path.read_bytes() for path in (root / "receipts").iterdir()
    }

    plan_c = _validation_plan(suffix="c")
    manifest_c = _manifest(plan_c, suffix="c")
    lifecycle_c = _current_lifecycle(
        manifest_c,
        approval_reference="approval-rollback-unknown",
        recorded_at=NOW + timedelta(minutes=5),
    )
    with pytest.raises(api.ChampionRegistryError, match="rollback_target"):
        registry.record_selection(
            selection_id="selection-rollback-unknown",
            action="rollback",
            manifest=manifest_c,
            validation_plan=plan_c,
            lifecycle=lifecycle_c,
            actor=LifecycleActor.HUMAN_REVIEWER,
            human_approval_reference="approval-rollback-unknown",
            recorded_at=NOW + timedelta(minutes=6),
            expected_current_manifest_sha256=second.selected_manifest_sha256,
        )
    assert registry.load_history() == (first, second)

    rollback_lifecycle = _current_lifecycle(
        manifest_a,
        approval_reference="approval-rollback-a",
        recorded_at=NOW + timedelta(minutes=7),
    )
    rollback = registry.record_selection(
        selection_id="selection-rollback-a",
        action="rollback",
        manifest=manifest_a,
        validation_plan=plan_a,
        lifecycle=rollback_lifecycle,
        actor=LifecycleActor.HUMAN_REVIEWER,
        human_approval_reference="approval-rollback-a",
        recorded_at=NOW + timedelta(minutes=8),
        expected_current_manifest_sha256=second.selected_manifest_sha256,
    )

    assert rollback.action == "rollback"
    assert rollback.selected_manifest_sha256 == first.selected_manifest_sha256
    assert registry.load_current() == rollback
    assert registry.load_history() == (first, second, rollback)
    for name, content in immutable_history.items():
        assert (root / "receipts" / name).read_bytes() == content
    assert drift_sentinel.read_bytes() == b'{"latched":true}\n'
    assert "drift_action_store" not in Path(api.__file__).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "forbidden_action",
    ["deactivate", "auto_restore", "promote", "expand_risk", "live"],
)
def test_registry_exposes_only_activate_and_rollback_actions(
    tmp_path: Path,
    forbidden_action: str,
) -> None:
    api = _api()
    plan = _validation_plan()
    manifest = _manifest(plan)
    lifecycle = _current_lifecycle(
        manifest,
        approval_reference="approval-action-surface",
        recorded_at=NOW + timedelta(minutes=1),
    )
    registry = api.ChampionSelectionRegistry(
        tmp_path / f"champion-registry-{forbidden_action}"
    )

    with pytest.raises(api.ChampionRegistryError, match="action_invalid"):
        registry.record_selection(
            selection_id=f"selection-{forbidden_action}",
            action=forbidden_action,
            manifest=manifest,
            validation_plan=plan,
            lifecycle=lifecycle,
            actor=LifecycleActor.HUMAN_REVIEWER,
            human_approval_reference="approval-action-surface",
            recorded_at=NOW + timedelta(minutes=2),
            expected_current_manifest_sha256=None,
        )

    for absent_method in (
        "deactivate",
        "auto_restore",
        "promote_automatically",
        "expand_risk",
        "authorize_live",
        "connect_broker",
    ):
        assert not hasattr(registry, absent_method)


@pytest.mark.parametrize(
    "tamper",
    [
        "receipt_fields_invalid",
        "receipt_hash_mismatch",
        "receipt_filename_mismatch",
        "previous_receipt_broken",
        "expected_current_broken",
        "duplicate_sequence",
        "duplicate_selection_id",
        "recorded_at_nonmonotonic",
        "rollback_target_unknown",
        "unsafe_authority_flag",
        "current_pointer_missing",
        "current_pointer_fields_invalid",
        "current_pointer_not_tail",
    ],
)
def test_restart_validates_complete_receipt_chain_and_current_pointer(
    tmp_path: Path,
    tamper: str,
) -> None:
    api = _api()
    root = tmp_path / f"champion-registry-{tamper}"
    registry, first, second = _record_two_activations(api, root)

    restarted = api.ChampionSelectionRegistry(root)
    assert restarted.load_history() == (first, second)
    assert restarted.load_current() == second

    receipt_paths = sorted((root / "receipts").iterdir())
    first_payload = json.loads(receipt_paths[0].read_text(encoding="utf-8"))
    tail_path = receipt_paths[1]
    tail_payload = json.loads(tail_path.read_text(encoding="utf-8"))
    pointer_path = root / "current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))

    if tamper == "receipt_hash_mismatch":
        tail_payload["selected_model_id"] = "tampered-model"
        _write_json(tail_path, tail_payload)
    elif tamper == "receipt_filename_mismatch":
        tail_path.rename(tail_path.with_name(f"{second.sequence:020d}-{'f' * 64}.json"))
    elif tamper.startswith("current_pointer"):
        if tamper == "current_pointer_missing":
            pointer_path.unlink()
        elif tamper == "current_pointer_fields_invalid":
            pointer["unexpected"] = "forged"
            _write_json(pointer_path, pointer)
        else:
            _write_json(
                pointer_path,
                {
                    "receipt_sha256": first.receipt_sha256,
                    "schema_version": registry.POINTER_SCHEMA_VERSION,
                    "selected_manifest_sha256": first.selected_manifest_sha256,
                    "selection_id": first.selection_id,
                    "sequence": first.sequence,
                },
            )
    else:
        if tamper == "receipt_fields_invalid":
            tail_payload["unexpected"] = "forged"
        elif tamper == "previous_receipt_broken":
            tail_payload["previous_receipt_sha256"] = "f" * 64
        elif tamper == "expected_current_broken":
            tail_payload["expected_current_manifest_sha256"] = "f" * 64
        elif tamper == "duplicate_sequence":
            tail_payload["sequence"] = first_payload["sequence"]
        elif tamper == "duplicate_selection_id":
            tail_payload["selection_id"] = first_payload["selection_id"]
        elif tamper == "recorded_at_nonmonotonic":
            tail_payload["recorded_at"] = (
                datetime.fromisoformat(first_payload["recorded_at"])
                - timedelta(seconds=1)
            ).isoformat()
        elif tamper == "rollback_target_unknown":
            tail_payload["action"] = "rollback"
        elif tamper == "unsafe_authority_flag":
            tail_payload["automatic_promotion_enabled"] = True

        tail_payload["receipt_sha256"] = _receipt_sha256(tail_payload)
        new_tail_path = tail_path.with_name(
            f"{tail_payload['sequence']:020d}-{tail_payload['receipt_sha256']}.json"
        )
        tail_path.rename(new_tail_path)
        _write_json(new_tail_path, tail_payload)
        pointer.update(
            {
                "receipt_sha256": tail_payload["receipt_sha256"],
                "selected_manifest_sha256": tail_payload["selected_manifest_sha256"],
                "selection_id": tail_payload["selection_id"],
                "sequence": tail_payload["sequence"],
            }
        )
        _write_json(pointer_path, pointer)

    corrupted = api.ChampionSelectionRegistry(root)
    with pytest.raises(api.ChampionRegistryError):
        corrupted.load_history()
    with pytest.raises(api.ChampionRegistryError):
        corrupted.load_current()


@pytest.mark.parametrize(
    "symlink_role",
    ["root", "receipts_directory", "receipt", "current", "lock"],
)
def test_registry_rejects_symlinked_storage_paths(
    tmp_path: Path,
    symlink_role: str,
) -> None:
    api = _api()
    root = tmp_path / f"champion-registry-{symlink_role}"

    if symlink_role == "root":
        actual_root = tmp_path / "actual-root"
        actual_root.mkdir()
        root.symlink_to(actual_root, target_is_directory=True)
        with pytest.raises(api.ChampionRegistryError, match="symlink"):
            api.ChampionSelectionRegistry(root)
        return

    registry, _first, _second = _record_two_activations(api, root)
    if symlink_role == "receipts_directory":
        target = tmp_path / "receipts-target"
        (root / "receipts").rename(target)
        (root / "receipts").symlink_to(target, target_is_directory=True)
    elif symlink_role == "receipt":
        receipt_path = sorted((root / "receipts").iterdir())[0]
        target = tmp_path / "receipt-target.json"
        receipt_path.rename(target)
        receipt_path.symlink_to(target)
    elif symlink_role == "current":
        current_path = root / "current.json"
        target = tmp_path / "current-target.json"
        current_path.rename(target)
        current_path.symlink_to(target)
    else:
        lock_path = root / ".registry.lock"
        lock_path.unlink(missing_ok=True)
        target = tmp_path / "lock-target"
        target.write_text("lock", encoding="utf-8")
        lock_path.symlink_to(target)

    with pytest.raises(api.ChampionRegistryError, match="symlink"):
        api.ChampionSelectionRegistry(root).load_history()


def test_concurrent_first_selection_has_one_cas_winner_and_valid_chain(
    tmp_path: Path,
) -> None:
    api = _api()
    root = tmp_path / "champion-registry-concurrent"
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(4)
    results = context.Queue()
    processes = [
        context.Process(
            target=_concurrent_first_selection_worker,
            args=(str(root), suffix, barrier, results),
        )
        for suffix in ("a", "b", "c", "d")
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    outcomes = [results.get(timeout=5) for _process in processes]
    successes = [value for status, value in outcomes if status == "ok"]
    errors = [value for status, value in outcomes if status == "error"]
    unexpected = [value for status, value in outcomes if status == "unexpected"]

    assert unexpected == []
    assert len(successes) == 1
    assert len(errors) == 3
    assert all("compare_and_swap" in message for message in errors)
    restarted = api.ChampionSelectionRegistry(root)
    history = restarted.load_history()
    assert len(history) == 1
    assert history[0].receipt_sha256 == successes[0]
    assert restarted.load_current() == history[0]


def test_receipt_and_current_publish_are_fsynced_write_once_and_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _api()
    root = tmp_path / "champion-registry-durable"
    observed_fsync_modes: list[int] = []
    observed_replaces: list[tuple[Path, Path]] = []
    original_fsync = os.fsync
    original_replace = os.replace

    def recording_fsync(fd: int) -> None:
        observed_fsync_modes.append(os.fstat(fd).st_mode)
        original_fsync(fd)

    def recording_replace(source, target) -> None:
        observed_replaces.append((Path(source), Path(target)))
        original_replace(source, target)

    monkeypatch.setattr(api.os, "fsync", recording_fsync)
    monkeypatch.setattr(api.os, "replace", recording_replace)
    plan = _validation_plan()
    manifest = _manifest(plan)
    lifecycle = _current_lifecycle(
        manifest,
        approval_reference="approval-durable",
        recorded_at=NOW + timedelta(minutes=1),
    )

    receipt = api.ChampionSelectionRegistry(root).record_selection(
        selection_id="selection-durable",
        action="activate",
        manifest=manifest,
        validation_plan=plan,
        lifecycle=lifecycle,
        actor=LifecycleActor.HUMAN_REVIEWER,
        human_approval_reference="approval-durable",
        recorded_at=NOW + timedelta(minutes=2),
        expected_current_manifest_sha256=None,
    )

    receipt_path = (
        root / "receipts" / f"{receipt.sequence:020d}-{receipt.receipt_sha256}.json"
    )
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o400
    assert any(stat.S_ISREG(mode) for mode in observed_fsync_modes)
    assert sum(stat.S_ISDIR(mode) for mode in observed_fsync_modes) >= 2
    assert len(observed_replaces) == 1
    temporary, target = observed_replaces[0]
    assert temporary.parent == root
    assert temporary.name.startswith(".current-")
    assert temporary.name.endswith(".tmp")
    assert target == root / "current.json"


def test_new_selection_time_cannot_precede_chain_tail(tmp_path: Path) -> None:
    api = _api()
    root = tmp_path / "champion-registry-time-chain"
    registry = api.ChampionSelectionRegistry(root)
    plan_a = _validation_plan(suffix="a")
    manifest_a = _manifest(plan_a, suffix="a")
    lifecycle_a = _current_lifecycle(
        manifest_a,
        approval_reference="approval-time-tail-a",
        recorded_at=NOW + timedelta(minutes=1),
    )
    first = registry.record_selection(
        selection_id="selection-time-tail-a",
        action="activate",
        manifest=manifest_a,
        validation_plan=plan_a,
        lifecycle=lifecycle_a,
        actor=LifecycleActor.HUMAN_REVIEWER,
        human_approval_reference="approval-time-tail-a",
        recorded_at=NOW + timedelta(minutes=10),
        expected_current_manifest_sha256=None,
    )

    plan_b = _validation_plan(suffix="b")
    manifest_b = _manifest(plan_b, suffix="b")
    lifecycle_b = _current_lifecycle(
        manifest_b,
        approval_reference="approval-time-tail-b",
        recorded_at=NOW + timedelta(minutes=1, seconds=30),
    )
    with pytest.raises(api.ChampionRegistryError, match="nonmonotonic"):
        registry.record_selection(
            selection_id="selection-time-tail-b",
            action="activate",
            manifest=manifest_b,
            validation_plan=plan_b,
            lifecycle=lifecycle_b,
            actor=LifecycleActor.HUMAN_REVIEWER,
            human_approval_reference="approval-time-tail-b",
            recorded_at=NOW + timedelta(minutes=2),
            expected_current_manifest_sha256=first.selected_manifest_sha256,
        )
    assert registry.load_history() == (first,)


def test_non_text_action_fails_closed_without_persisting(tmp_path: Path) -> None:
    api = _api()
    root = tmp_path / "champion-registry-invalid-action-type"
    plan = _validation_plan()
    manifest = _manifest(plan)
    lifecycle = _current_lifecycle(
        manifest,
        approval_reference="approval-invalid-action-type",
        recorded_at=NOW + timedelta(minutes=1),
    )

    with pytest.raises(api.ChampionRegistryError, match="action_invalid"):
        api.ChampionSelectionRegistry(root).record_selection(
            selection_id="selection-invalid-action-type",
            action=[],
            manifest=manifest,
            validation_plan=plan,
            lifecycle=lifecycle,
            actor=LifecycleActor.HUMAN_REVIEWER,
            human_approval_reference="approval-invalid-action-type",
            recorded_at=NOW + timedelta(minutes=2),
            expected_current_manifest_sha256=None,
        )
    assert list((root / "receipts").iterdir()) == []
    assert not (root / "current.json").exists()
