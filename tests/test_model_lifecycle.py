from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone

import pytest

from shared.models.lifecycle import (
    LifecycleActor,
    LifecycleContractError,
    LifecycleRecord,
    ModelLifecycleState,
    TradingSessionCalendarAuthority,
    TradingSessionCalendarAuthorityVerification,
    ValidationPlan,
    build_validation_plan,
    transition_model,
)
from shared.models.release_manifest import (
    ModelReleaseManifest,
    ReleaseManifestContractError,
)


NOW = datetime(2026, 7, 16, 2, 30, tzinfo=timezone.utc)
RESEARCH_SNAPSHOT_SHA256 = "b" * 64
CATALOG_VERSION = "sharedsignals-v1-catalog-20260716"
VALIDATION_EVIDENCE_SHA256 = "c" * 64


def _calendar_sessions() -> tuple[date, ...]:
    current = date(2024, 12, 2)
    end = date(2025, 3, 31)
    closed_dates = {date(2025, 1, 1)}
    sessions: list[date] = []
    while current <= end:
        if current.weekday() < 5 and current not in closed_dates:
            sessions.append(current)
        current += timedelta(days=1)
    return tuple(sessions)


def _calendar_authority() -> TradingSessionCalendarAuthority:
    return TradingSessionCalendarAuthority(
        market="ashare",
        calendar_id="sse-szse-joint-trading-sessions",
        calendar_version="fixture-2024q4-2025q1-v1",
        source_dataset_id="fixture.ashare.trade_calendar",
        source_receipt_id="receipt-fixture-calendar-001",
        source_receipt_sha256="e" * 64,
        available_at=datetime(2025, 4, 1, tzinfo=timezone.utc),
        sessions=_calendar_sessions(),
    )


class _FixtureCalendarAuthorityVerifier:
    verifier_id = "fixture-calendar-authority-verifier"
    verifier_version = "1.0.0"

    def verify(
        self,
        calendar: TradingSessionCalendarAuthority,
        *,
        frozen_at: datetime,
    ) -> TradingSessionCalendarAuthorityVerification:
        return TradingSessionCalendarAuthorityVerification(
            accepted=True,
            verifier_id=self.verifier_id,
            verifier_version=self.verifier_version,
            proof_sha256="f" * 64,
            verified_at=frozen_at - timedelta(minutes=1),
            frozen_at=frozen_at,
            calendar_sha256=calendar.calendar_sha256,
            source_receipt_id=calendar.source_receipt_id,
            source_receipt_sha256=calendar.source_receipt_sha256,
        )


def test_trading_session_calendar_authority_binds_content_and_receipt() -> None:
    calendar = _calendar_authority()

    assert calendar.market == "ashare"
    assert calendar.session_count == len(_calendar_sessions())
    assert len(calendar.calendar_sha256) == 64
    assert calendar.source_receipt_sha256 == "e" * 64


def test_validation_plan_exposes_market_calendar_authority_binding() -> None:
    fields = ValidationPlan.__dataclass_fields__

    assert "market" in fields
    assert "trading_session_calendar" in fields
    assert "frozen_at" in fields
    assert "trading_session_calendar_verification" in fields


def _validation_plan() -> ValidationPlan:
    return build_validation_plan(
        train_start=date(2024, 12, 2),
        train_end=date(2024, 12, 31),
        validation_start=date(2025, 1, 9),
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
        oos_authority_receipt_sha256="d" * 64,
        experiment_family_id="ashare-industry-event-v1",
        experiment_id="challenger-001",
        frozen_test_set_id="ashare-oos-2026h1-v1",
        market="ashare",
        trading_session_calendar=_calendar_authority(),
        frozen_at=datetime(2025, 4, 2, tzinfo=timezone.utc),
        calendar_authority_verifier=_FixtureCalendarAuthorityVerifier(),
    )


def _manifest() -> ModelReleaseManifest:
    return ModelReleaseManifest(
        manifest_id="manifest-challenger-001",
        model_id="ashare-challenger",
        model_version="0.1.0",
        artifact_sha256="a" * 64,
        training_data_version="ss-catalog-fixture-v1",
        feature_contract_version="ta-features-v1",
        validation_plan_sha256=_validation_plan().sha256(),
        research_snapshot_sha256=RESEARCH_SNAPSHOT_SHA256,
        catalog_version=CATALOG_VERSION,
        validation_evidence_sha256=VALIDATION_EVIDENCE_SHA256,
        source_commit="local-uncommitted-candidate",
        created_at=NOW,
        created_by="offline-research-pipeline",
        intended_mode="shadow",
    )


def test_validation_plan_captures_scientific_split_and_reuse_controls() -> None:
    plan = _validation_plan()

    assert plan.time_split == {
        "train": (date(2024, 12, 2), date(2024, 12, 31)),
        "validation": (date(2025, 1, 9), date(2025, 2, 28)),
        "test": (date(2025, 3, 10), date(2025, 3, 31)),
    }
    assert plan.purge_days == 5
    assert plan.embargo_days == 5
    assert plan.label_horizon_days == 5
    assert plan.max_feature_lookback_days == 5
    assert plan.registered_trial_count == 1
    assert plan.multiple_testing_trial_budget == 20
    assert plan.pbo_required is True
    assert plan.deflated_sharpe_required is True
    assert plan.oos_authority_receipt_sha256 == "d" * 64
    assert plan.decision_cluster_key == "decision_cluster_id"
    assert plan.oos_reuse_count == 0
    assert plan.experiment_family_id == "ashare-industry-event-v1"
    assert plan.market == "ashare"
    assert plan.trading_session_calendar == _calendar_authority()
    assert (
        plan.canonical_payload()["trading_session_calendar"]["calendar_sha256"]
        == _calendar_authority().calendar_sha256
    )
    assert len(plan.sha256()) == 64


def test_validation_plan_rejects_leakage_and_reused_oos_tuning() -> None:
    values = _validation_plan().__dict__.copy()
    values["validation_start"] = date(2025, 1, 8)
    with pytest.raises(LifecycleContractError, match="purge_gap"):
        ValidationPlan(**values)

    values = _validation_plan().__dict__.copy()
    values["oos_used_for_tuning"] = True
    with pytest.raises(LifecycleContractError, match="oos_tuning_forbidden"):
        ValidationPlan(**values)

    values = _validation_plan().__dict__.copy()
    values["oos_reuse_count"] = 2
    with pytest.raises(LifecycleContractError, match="oos_reuse_exceeded"):
        ValidationPlan(**values)

    values = _validation_plan().__dict__.copy()
    values["purge_days"] = 4
    with pytest.raises(LifecycleContractError, match="purge_horizon_insufficient"):
        ValidationPlan(**values)

    values = _validation_plan().__dict__.copy()
    values["embargo_days"] = 4
    with pytest.raises(LifecycleContractError, match="embargo_horizon_insufficient"):
        ValidationPlan(**values)

    values = _validation_plan().__dict__.copy()
    values["registered_trial_count"] = 21
    with pytest.raises(
        LifecycleContractError, match="multiple_testing_budget_exceeded"
    ):
        ValidationPlan(**values)

    values = _validation_plan().__dict__.copy()
    values["pbo_required"] = False
    with pytest.raises(LifecycleContractError, match="pbo_control_required"):
        ValidationPlan(**values)


def test_ashare_validation_plan_requires_calendar_authority() -> None:
    values = _validation_plan().__dict__.copy()
    values["trading_session_calendar"] = None

    with pytest.raises(
        LifecycleContractError,
        match="ashare_trading_session_calendar_required",
    ):
        ValidationPlan(**values)


def test_ashare_validation_plan_requires_independent_calendar_verifier() -> None:
    values = _validation_plan().__dict__.copy()
    values.pop("trading_session_calendar_verification")

    with pytest.raises(
        LifecycleContractError,
        match="calendar_authority_verifier_required",
    ):
        build_validation_plan(
            **values,
            calendar_authority_verifier=None,
        )


def test_ashare_validation_plan_rejects_calendar_available_after_freeze() -> None:
    values = _validation_plan().__dict__.copy()
    values.pop("trading_session_calendar_verification")
    values["frozen_at"] = datetime(2025, 3, 31, tzinfo=timezone.utc)

    with pytest.raises(
        LifecycleContractError,
        match="calendar_available_after_validation_plan_freeze",
    ):
        build_validation_plan(
            **values,
            calendar_authority_verifier=_FixtureCalendarAuthorityVerifier(),
        )


def test_ashare_validation_plan_rejects_detached_calendar_proof_mismatch() -> None:
    values = _validation_plan().__dict__.copy()
    proof = values["trading_session_calendar_verification"]
    values["trading_session_calendar_verification"] = type(proof)(
        **{
            **proof.__dict__,
            "calendar_sha256": "0" * 64,
        }
    )

    with pytest.raises(
        LifecycleContractError,
        match="calendar_authority_binding_mismatch",
    ):
        ValidationPlan(**values)


def test_calendar_receipt_or_sessions_change_validation_plan_identity() -> None:
    plan = _validation_plan()
    calendar = _calendar_authority()
    calendar_type = type(calendar)
    changed_receipt = calendar_type(
        **{
            **calendar.__dict__,
            "source_receipt_id": "receipt-fixture-calendar-002",
        }
    )
    changed_sessions = calendar_type(
        **{
            **calendar.__dict__,
            "sessions": tuple(
                session for session in calendar.sessions if session != date(2024, 12, 3)
            ),
        }
    )

    receipt_values = {
        **plan.__dict__,
        "trading_session_calendar": changed_receipt,
    }
    receipt_values.pop("trading_session_calendar_verification")
    receipt_plan = build_validation_plan(
        **receipt_values,
        calendar_authority_verifier=_FixtureCalendarAuthorityVerifier(),
    )
    session_values = {
        **plan.__dict__,
        "trading_session_calendar": changed_sessions,
    }
    session_values.pop("trading_session_calendar_verification")
    session_plan = build_validation_plan(
        **session_values,
        calendar_authority_verifier=_FixtureCalendarAuthorityVerifier(),
    )

    assert receipt_plan.sha256() != plan.sha256()
    assert session_plan.sha256() != plan.sha256()


def test_non_ashare_validation_plan_retains_calendar_day_gap_contract() -> None:
    values = _validation_plan().__dict__.copy()
    values.update(
        {
            "market": "generic",
            "trading_session_calendar": None,
            "trading_session_calendar_verification": None,
            "validation_start": date(2025, 1, 7),
        }
    )

    plan = ValidationPlan(**values)

    assert plan.canonical_payload()["split_gap_unit"] == "calendar_days"


def test_release_manifest_is_immutable_and_simulation_only() -> None:
    manifest = _manifest()

    assert len(manifest.sha256()) == 64
    assert manifest.research_snapshot_sha256 == RESEARCH_SNAPSHOT_SHA256
    assert manifest.catalog_version == CATALOG_VERSION
    assert manifest.validation_plan_sha256 == _validation_plan().sha256()
    assert manifest.validation_evidence_sha256 == VALIDATION_EVIDENCE_SHA256
    assert manifest.real_trading_enabled is False
    assert manifest.live_transition_authorized is False
    assert manifest.automatic_promotion_enabled is False
    assert manifest.automatic_risk_expansion_enabled is False

    with pytest.raises(FrozenInstanceError):
        manifest.model_version = "9.9.9"  # type: ignore[misc]

    with pytest.raises(ReleaseManifestContractError, match="reserved_safety_marker"):
        ModelReleaseManifest(
            **{
                **manifest.__dict__,
                "metadata": (("live_transition_authorized", "true"),),
            }
        )


def test_release_manifest_hash_binds_snapshot_catalog_plan_and_evidence() -> None:
    manifest = _manifest()
    payload = manifest.canonical_payload()

    assert payload["research_snapshot_sha256"] == RESEARCH_SNAPSHOT_SHA256
    assert payload["catalog_version"] == CATALOG_VERSION
    assert payload["validation_plan_sha256"] == _validation_plan().sha256()
    assert payload["validation_evidence_sha256"] == VALIDATION_EVIDENCE_SHA256

    changed_snapshot = ModelReleaseManifest(
        **{
            **manifest.__dict__,
            "research_snapshot_sha256": "d" * 64,
        }
    )
    assert changed_snapshot.sha256() != manifest.sha256()


@pytest.mark.parametrize(
    "field_name",
    [
        "research_snapshot_sha256",
        "catalog_version",
        "validation_plan_sha256",
        "validation_evidence_sha256",
    ],
)
def test_manifest_metadata_cannot_shadow_canonical_evidence_bindings(
    field_name: str,
) -> None:
    manifest = _manifest()

    with pytest.raises(ReleaseManifestContractError, match="reserved_safety_marker"):
        ModelReleaseManifest(
            **{
                **manifest.__dict__,
                "metadata": ((field_name, "forged"),),
            }
        )


@pytest.mark.parametrize(
    "field_name",
    [
        "artifact_sha256",
        "research_snapshot_sha256",
        "validation_plan_sha256",
        "validation_evidence_sha256",
    ],
)
def test_release_manifest_rejects_invalid_evidence_hashes(field_name: str) -> None:
    manifest = _manifest()

    with pytest.raises(ReleaseManifestContractError, match="invalid"):
        ModelReleaseManifest(
            **{
                **manifest.__dict__,
                field_name: "not-a-sha256",
            }
        )


@pytest.mark.parametrize(
    "field_name",
    [
        "real_trading_enabled",
        "live_transition_authorized",
        "automatic_promotion_enabled",
        "automatic_risk_expansion_enabled",
    ],
)
def test_release_manifest_rejects_live_or_automatic_authority(
    field_name: str,
) -> None:
    manifest = _manifest()

    with pytest.raises(ReleaseManifestContractError, match="simulation_only"):
        ModelReleaseManifest(
            **{
                **manifest.__dict__,
                field_name: True,
            }
        )


def test_manual_lifecycle_follows_draft_to_current_without_enabling_live() -> None:
    manifest = _manifest()
    record = LifecycleRecord.draft(manifest=manifest, recorded_at=NOW)

    for target in (
        ModelLifecycleState.BACKTEST,
        ModelLifecycleState.SHADOW,
        ModelLifecycleState.REVIEW,
    ):
        record = transition_model(
            record,
            target=target,
            actor=LifecycleActor.HUMAN_REVIEWER,
            recorded_at=NOW,
            reason="manual_gate_passed",
        )

    record = transition_model(
        record,
        target=ModelLifecycleState.CURRENT,
        actor=LifecycleActor.HUMAN_REVIEWER,
        recorded_at=NOW,
        reason="nicholas_explicit_model_selection",
        approval_reference="manual-approval-001",
    )

    assert record.state is ModelLifecycleState.CURRENT
    assert record.research_snapshot_sha256 == RESEARCH_SNAPSHOT_SHA256
    assert record.catalog_version == CATALOG_VERSION
    assert record.validation_plan_sha256 == _validation_plan().sha256()
    assert record.validation_evidence_sha256 == VALIDATION_EVIDENCE_SHA256
    assert record.real_trading_enabled is False
    assert record.live_transition_authorized is False
    assert record.automatic_promotion_enabled is False
    assert record.automatic_risk_expansion_enabled is False


@pytest.mark.parametrize(
    "target",
    [
        ModelLifecycleState.BACKTEST,
        ModelLifecycleState.SHADOW,
        ModelLifecycleState.REVIEW,
        ModelLifecycleState.CURRENT,
        ModelLifecycleState.RETIRED,
    ],
)
def test_automation_cannot_promote_or_retire_challenger(
    target: ModelLifecycleState,
) -> None:
    record = LifecycleRecord.draft(manifest=_manifest(), recorded_at=NOW)

    with pytest.raises(LifecycleContractError, match="automatic_action_forbidden"):
        transition_model(
            record,
            target=target,
            actor=LifecycleActor.AUTOMATION,
            recorded_at=NOW,
            reason="automated_attempt",
            approval_reference="fake-approval",
        )


def test_automation_can_only_quarantine() -> None:
    record = LifecycleRecord.draft(manifest=_manifest(), recorded_at=NOW)

    quarantined = transition_model(
        record,
        target=ModelLifecycleState.QUARANTINE,
        actor=LifecycleActor.AUTOMATION,
        recorded_at=NOW,
        reason="drift_threshold_breached",
    )

    assert quarantined.state is ModelLifecycleState.QUARANTINE
    assert quarantined.real_trading_enabled is False


def test_current_record_requires_manual_approval_even_when_directly_loaded() -> None:
    draft = LifecycleRecord.draft(manifest=_manifest(), recorded_at=NOW)
    with pytest.raises(LifecycleContractError, match="manual_approval_reference"):
        LifecycleRecord(
            **{
                **draft.__dict__,
                "state": ModelLifecycleState.CURRENT,
            }
        )


@pytest.mark.parametrize(
    "field_name",
    [
        "real_trading_enabled",
        "live_transition_authorized",
        "automatic_promotion_enabled",
        "automatic_risk_expansion_enabled",
    ],
)
def test_lifecycle_record_rejects_live_or_automatic_authority(
    field_name: str,
) -> None:
    draft = LifecycleRecord.draft(manifest=_manifest(), recorded_at=NOW)

    with pytest.raises(LifecycleContractError, match="simulation_only"):
        LifecycleRecord(
            **{
                **draft.__dict__,
                field_name: True,
            }
        )


@pytest.mark.parametrize(
    "field_name",
    [
        "research_snapshot_sha256",
        "validation_plan_sha256",
        "validation_evidence_sha256",
    ],
)
def test_lifecycle_record_rejects_invalid_evidence_hashes(field_name: str) -> None:
    draft = LifecycleRecord.draft(manifest=_manifest(), recorded_at=NOW)

    with pytest.raises(LifecycleContractError, match="invalid"):
        LifecycleRecord(
            **{
                **draft.__dict__,
                field_name: "not-a-sha256",
            }
        )
