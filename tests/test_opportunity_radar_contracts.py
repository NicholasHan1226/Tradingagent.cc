from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from shared.opportunity.contracts import (
    OpportunityContractError,
    OpportunityEvidenceRef,
    OpportunityScope,
    OpportunityState,
    transition_opportunity,
)
from shared.opportunity.radar import (
    FrozenOpportunityRadar,
    OpportunityCoverageVerification,
    OpportunityScanRow,
)


UTC = timezone.utc
DECISION_TIME = datetime(2026, 7, 16, 1, 30, tzinfo=UTC)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _evidence(
    suffix: str,
    *,
    available_at: datetime | None = None,
) -> OpportunityEvidenceRef:
    return OpportunityEvidenceRef(
        evidence_id=f"evidence-{suffix}",
        dataset_id="ashare.daily.market.v1",
        receipt_id=f"receipt-{suffix}",
        lineage_id=f"lineage-{suffix}",
        evidence_group_id=f"price-volume-{suffix}",
        data_through=DECISION_TIME - timedelta(minutes=20),
        available_at=available_at or DECISION_TIME - timedelta(minutes=10),
        expires_at=DECISION_TIME + timedelta(hours=3),
        payload_sha256=_sha(f"payload-{suffix}"),
    )


def _detected_row(
    entity_id: str = "600000.SH",
    *,
    scope: OpportunityScope = OpportunityScope.STOCK,
    score: float = 0.62,
    evidence: tuple[OpportunityEvidenceRef, ...] | None = None,
) -> OpportunityScanRow:
    return OpportunityScanRow(
        scope=scope,
        entity_id=entity_id,
        thesis_id=f"thesis-{entity_id}",
        state=OpportunityState.FORMING,
        uncalibrated_hazard_score=score,
        priced_in_score=0.25,
        trigger_window_start=DECISION_TIME,
        trigger_window_end=DECISION_TIME + timedelta(days=5),
        horizon="5d",
        evidence_refs=evidence or (_evidence(entity_id),),
        invalidation_conditions=("evidence_expires",),
        reason_codes=("leading_evidence_detected",),
    )


def _no_opportunity_row(entity_id: str = "600001.SH") -> OpportunityScanRow:
    return OpportunityScanRow(
        scope=OpportunityScope.STOCK,
        entity_id=entity_id,
        thesis_id=f"thesis-{entity_id}",
        state=None,
        uncalibrated_hazard_score=None,
        priced_in_score=None,
        trigger_window_start=None,
        trigger_window_end=None,
        horizon=None,
        evidence_refs=(),
        invalidation_conditions=(),
        reason_codes=("no_qualified_leading_evidence",),
    )


class _CoverageVerifier:
    verifier_id = "fixture-opportunity-coverage-verifier-v1"
    production_eligible = False

    def __init__(self, *, accepted: bool = True, expected_count: int = 2) -> None:
        self.accepted = accepted
        self.expected_count = expected_count

    def verify(self, **request: object) -> OpportunityCoverageVerification:
        return OpportunityCoverageVerification(
            accepted=self.accepted,
            verifier_id=self.verifier_id,
            production_eligible=False,
            proof_sha256=_sha(repr(sorted(request.items()))),
            verified_at=request["decision_time"],
            decision_time=request["decision_time"],
            detector_id=request["detector_id"],
            detector_version=request["detector_version"],
            universe_snapshot_sha256=request["universe_snapshot_sha256"],
            scan_rows_sha256=request["scan_rows_sha256"],
            scanned_entity_ids_sha256=request["scanned_entity_ids_sha256"],
            expected_entity_count=self.expected_count,
            observed_entity_count=len(request["scanned_entity_ids"]),
        )


def _scan(
    rows: tuple[OpportunityScanRow, ...],
    *,
    verifier: _CoverageVerifier | None = None,
):
    return FrozenOpportunityRadar(
        detector_id="phase1-leading-evidence-radar",
        detector_version="1",
    ).scan(
        rows,
        decision_time=DECISION_TIME,
        universe_snapshot_sha256="a" * 64,
        coverage_verifier=verifier or _CoverageVerifier(expected_count=len(rows)),
    )


def test_radar_records_complete_denominator_but_only_emits_shadow_opportunities() -> (
    None
):
    rows = (
        _detected_row(),
        _no_opportunity_row(),
    )

    batch = _scan(rows)

    assert batch.scanned_entity_ids == ("600000.SH", "600001.SH")
    assert batch.coverage.expected_entity_count == 2
    assert batch.coverage.observed_entity_count == 2
    assert len(batch.opportunities) == 1
    opportunity = batch.opportunities[0]
    assert opportunity.scope is OpportunityScope.STOCK
    assert opportunity.entity_id == "600000.SH"
    assert opportunity.state is OpportunityState.FORMING
    assert opportunity.score_semantics == "uncalibrated_hazard_score"
    assert opportunity.shadow_only is True
    assert opportunity.trade_candidate_emission_allowed is False
    assert opportunity.position_effect_allowed is False
    assert opportunity.order_effect_allowed is False
    assert opportunity.promotion_eligible is False
    assert batch.production_eligible is False
    assert not hasattr(opportunity, "target_weight")
    assert not hasattr(opportunity, "quantity")


def test_radar_accepts_sector_aggregate_as_context_but_rejects_chinext_stock() -> None:
    sector = _detected_row(
        "SW-AI-HARDWARE",
        scope=OpportunityScope.SECTOR,
    )
    batch = _scan((sector,))
    assert batch.opportunities[0].scope is OpportunityScope.SECTOR
    assert batch.opportunities[0].context_only is True

    with pytest.raises(
        OpportunityContractError,
        match="stock_scope_requires_mainboard_common_stock",
    ):
        _detected_row("300001.SZ")


def test_radar_requires_independent_complete_coverage_proof() -> None:
    radar = FrozenOpportunityRadar(
        detector_id="phase1-leading-evidence-radar",
        detector_version="1",
    )
    rows = (_detected_row(), _no_opportunity_row())

    with pytest.raises(
        OpportunityContractError,
        match="coverage_verifier_required",
    ):
        radar.scan(
            rows,
            decision_time=DECISION_TIME,
            universe_snapshot_sha256="a" * 64,
            coverage_verifier=None,
        )

    with pytest.raises(
        OpportunityContractError,
        match="coverage_denominator_mismatch",
    ):
        _scan(rows, verifier=_CoverageVerifier(expected_count=3))


def test_radar_fails_closed_on_future_evidence_or_proof_rebinding() -> None:
    future = _evidence(
        "future",
        available_at=DECISION_TIME + timedelta(seconds=1),
    )
    with pytest.raises(OpportunityContractError, match="evidence_from_future"):
        _scan((_detected_row(evidence=(future,)),))

    class _RebindingVerifier(_CoverageVerifier):
        def verify(self, **request: object) -> OpportunityCoverageVerification:
            proof = super().verify(**request)
            return replace(proof, scan_rows_sha256="f" * 64)

    with pytest.raises(
        OpportunityContractError,
        match="coverage_proof_binding_mismatch",
    ):
        _scan(
            (_detected_row(),),
            verifier=_RebindingVerifier(expected_count=1),
        )


def test_radar_identity_is_deterministic_and_input_order_independent() -> None:
    rows = (_detected_row(), _no_opportunity_row())
    first = _scan(rows)
    second = _scan(tuple(reversed(rows)))

    assert second.batch_sha256 == first.batch_sha256
    assert second.scan_rows_sha256 == first.scan_rows_sha256


def test_opportunity_state_machine_requires_new_pit_evidence_and_has_terminal_state() -> (
    None
):
    initial = _scan((_detected_row(),)).opportunities[0]
    new_evidence = _evidence(
        "trigger",
        available_at=DECISION_TIME + timedelta(minutes=10),
    )
    triggered = transition_opportunity(
        initial,
        target_state=OpportunityState.READY,
        decision_time=DECISION_TIME + timedelta(minutes=30),
        new_evidence_refs=(new_evidence,),
        reason_codes=("confirmation_arrived",),
    )
    invalidated = transition_opportunity(
        triggered,
        target_state=OpportunityState.INVALIDATED,
        decision_time=DECISION_TIME + timedelta(hours=1),
        new_evidence_refs=(
            _evidence(
                "invalidated",
                available_at=DECISION_TIME + timedelta(minutes=45),
            ),
        ),
        reason_codes=("thesis_invalidated",),
    )

    assert triggered.previous_snapshot_sha256 == initial.snapshot_sha256
    assert invalidated.state is OpportunityState.INVALIDATED

    with pytest.raises(
        OpportunityContractError, match="transition_requires_new_evidence"
    ):
        transition_opportunity(
            initial,
            target_state=OpportunityState.READY,
            decision_time=DECISION_TIME + timedelta(minutes=30),
            new_evidence_refs=(),
            reason_codes=("unsupported",),
        )
    with pytest.raises(OpportunityContractError, match="opportunity_state_terminal"):
        transition_opportunity(
            invalidated,
            target_state=OpportunityState.FORMING,
            decision_time=DECISION_TIME + timedelta(hours=2),
            new_evidence_refs=(_evidence("revive"),),
            reason_codes=("forbidden_revive",),
        )


def test_opportunity_batch_direct_construction_cannot_detach_denominator_or_proof() -> (
    None
):
    batch = _scan((_detected_row(), _no_opportunity_row()))

    with pytest.raises(
        OpportunityContractError,
        match="opportunity_batch_denominator_binding_invalid",
    ):
        replace(batch, scanned_entity_ids=("600000.SH", "600002.SH"))
    with pytest.raises(
        OpportunityContractError,
        match="opportunity_batch_coverage_binding_invalid",
    ):
        replace(
            batch,
            coverage=replace(batch.coverage, observed_entity_count=1),
        )
    with pytest.raises(
        OpportunityContractError,
        match="opportunity_batch_opportunity_binding_invalid",
    ):
        replace(
            batch,
            opportunities=(replace(batch.opportunities[0], entity_id="600002.SH"),),
        )
