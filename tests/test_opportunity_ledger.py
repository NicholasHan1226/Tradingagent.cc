from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from shared.opportunity.contracts import (
    OpportunityEvidenceRef,
    OpportunityScope,
    OpportunityState,
    transition_opportunity,
)
from shared.opportunity.ledger import (
    EMPTY_OPPORTUNITY_LEDGER_HEAD_SHA256,
    OpportunityLedger,
    OpportunityLedgerError,
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


def _batch():
    evidence = OpportunityEvidenceRef(
        evidence_id="evidence-initial",
        dataset_id="ashare.daily.market.v1",
        receipt_id="receipt-initial",
        lineage_id="lineage-initial",
        evidence_group_id="price-volume",
        data_through=DECISION_TIME - timedelta(minutes=20),
        available_at=DECISION_TIME - timedelta(minutes=10),
        expires_at=DECISION_TIME + timedelta(hours=4),
        payload_sha256="a" * 64,
    )
    row = OpportunityScanRow(
        scope=OpportunityScope.STOCK,
        entity_id="600000.SH",
        thesis_id="thesis-600000",
        state=OpportunityState.FORMING,
        uncalibrated_hazard_score=0.62,
        priced_in_score=0.25,
        trigger_window_start=DECISION_TIME,
        trigger_window_end=DECISION_TIME + timedelta(days=5),
        horizon="5d",
        evidence_refs=(evidence,),
        invalidation_conditions=("evidence_expires",),
        reason_codes=("leading_evidence_detected",),
    )

    class _Verifier:
        verifier_id = "fixture-opportunity-coverage-verifier-v1"
        production_eligible = False

        def verify(self, **request: object) -> OpportunityCoverageVerification:
            return OpportunityCoverageVerification(
                accepted=True,
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
                expected_entity_count=1,
                observed_entity_count=1,
            )

    return FrozenOpportunityRadar(
        detector_id="phase1-leading-evidence-radar",
        detector_version="1",
    ).scan(
        (row,),
        decision_time=DECISION_TIME,
        universe_snapshot_sha256="b" * 64,
        coverage_verifier=_Verifier(),
    )


def test_opportunity_ledger_is_append_only_idempotent_and_state_aware(tmp_path) -> None:
    ledger = OpportunityLedger(tmp_path / "opportunity-ledger.jsonl")
    batch = _batch()

    assert (
        ledger.append_batch(
            batch,
            expected_head_sha256=EMPTY_OPPORTUNITY_LEDGER_HEAD_SHA256,
        )
        is True
    )
    assert ledger.append_batch(batch) is False

    current = batch.opportunities[0]
    ready = transition_opportunity(
        current,
        target_state=OpportunityState.READY,
        decision_time=DECISION_TIME + timedelta(minutes=30),
        new_evidence_refs=(
            OpportunityEvidenceRef(
                evidence_id="evidence-ready",
                dataset_id="ashare.events.v1",
                receipt_id="receipt-ready",
                lineage_id="lineage-ready",
                evidence_group_id="event",
                data_through=DECISION_TIME + timedelta(minutes=10),
                available_at=DECISION_TIME + timedelta(minutes=20),
                expires_at=DECISION_TIME + timedelta(hours=4),
                payload_sha256="c" * 64,
            ),
        ),
        reason_codes=("confirmation_arrived",),
    )
    first_head = ledger.read().head_sha256
    assert ledger.append_transition(ready, expected_head_sha256=first_head) is True
    assert ledger.append_transition(ready) is False

    readback = ledger.read()
    assert len(readback.events) == 2
    assert readback.latest_by_opportunity[current.opportunity_id] == ready
    assert readback.events[-1].previous_event_sha256 == readback.events[0].event_sha256


def test_opportunity_ledger_rejects_stale_cas_and_branching_transition(
    tmp_path,
) -> None:
    ledger = OpportunityLedger(tmp_path / "opportunity-ledger.jsonl")
    batch = _batch()
    ledger.append_batch(batch)
    initial = batch.opportunities[0]
    stale_head = EMPTY_OPPORTUNITY_LEDGER_HEAD_SHA256

    ready = transition_opportunity(
        initial,
        target_state=OpportunityState.READY,
        decision_time=DECISION_TIME + timedelta(minutes=30),
        new_evidence_refs=(
            OpportunityEvidenceRef(
                evidence_id="evidence-ready",
                dataset_id="ashare.events.v1",
                receipt_id="receipt-ready",
                lineage_id="lineage-ready",
                evidence_group_id="event",
                data_through=DECISION_TIME + timedelta(minutes=10),
                available_at=DECISION_TIME + timedelta(minutes=20),
                expires_at=DECISION_TIME + timedelta(hours=4),
                payload_sha256="c" * 64,
            ),
        ),
        reason_codes=("confirmation_arrived",),
    )
    with pytest.raises(OpportunityLedgerError, match="ledger_head_cas_mismatch"):
        ledger.append_transition(ready, expected_head_sha256=stale_head)

    ledger.append_transition(ready)
    branching = transition_opportunity(
        initial,
        target_state=OpportunityState.DECAYING,
        decision_time=DECISION_TIME + timedelta(minutes=40),
        new_evidence_refs=(
            OpportunityEvidenceRef(
                evidence_id="evidence-decay",
                dataset_id="ashare.events.v1",
                receipt_id="receipt-decay",
                lineage_id="lineage-decay",
                evidence_group_id="event",
                data_through=DECISION_TIME + timedelta(minutes=20),
                available_at=DECISION_TIME + timedelta(minutes=30),
                expires_at=DECISION_TIME + timedelta(hours=4),
                payload_sha256="d" * 64,
            ),
        ),
        reason_codes=("evidence_weakened",),
    )
    with pytest.raises(
        OpportunityLedgerError,
        match="opportunity_transition_branch_mismatch",
    ):
        ledger.append_transition(branching)


def test_opportunity_ledger_tamper_or_partial_line_fails_closed(tmp_path) -> None:
    path = tmp_path / "opportunity-ledger.jsonl"
    ledger = OpportunityLedger(path)
    ledger.append_batch(_batch())

    path.write_text(path.read_text(encoding="utf-8") + "{partial", encoding="utf-8")
    with pytest.raises(OpportunityLedgerError, match="ledger_partial_line"):
        ledger.read()


def test_opportunity_ledger_refuses_symlink_path(tmp_path) -> None:
    target = tmp_path / "target.jsonl"
    target.write_text("", encoding="utf-8")
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)

    with pytest.raises(OpportunityLedgerError, match="ledger_symlink_forbidden"):
        OpportunityLedger(link)


def test_opportunity_ledger_rejects_directly_forged_state_transition(tmp_path) -> None:
    ledger = OpportunityLedger(tmp_path / "opportunity-ledger.jsonl")
    batch = _batch()
    ledger.append_batch(batch)
    initial = batch.opportunities[0]

    illegal_jump = replace(
        initial,
        state=OpportunityState.ACTIVE,
        decision_time=DECISION_TIME + timedelta(minutes=5),
        previous_snapshot_sha256=initial.snapshot_sha256,
    )
    with pytest.raises(
        OpportunityLedgerError,
        match="opportunity_transition_invalid",
    ):
        ledger.append_transition(illegal_jump)


def test_opportunity_ledger_rejects_identity_mutation_and_missing_new_evidence(
    tmp_path,
) -> None:
    ledger = OpportunityLedger(tmp_path / "opportunity-ledger.jsonl")
    batch = _batch()
    ledger.append_batch(batch)
    initial = batch.opportunities[0]

    missing_evidence = replace(
        initial,
        state=OpportunityState.READY,
        decision_time=DECISION_TIME + timedelta(minutes=5),
        previous_snapshot_sha256=initial.snapshot_sha256,
    )
    with pytest.raises(
        OpportunityLedgerError,
        match="transition_requires_new_evidence",
    ):
        ledger.append_transition(missing_evidence)

    forged_identity = replace(
        transition_opportunity(
            initial,
            target_state=OpportunityState.READY,
            decision_time=DECISION_TIME + timedelta(minutes=30),
            new_evidence_refs=(
                OpportunityEvidenceRef(
                    evidence_id="evidence-ready-forge",
                    dataset_id="ashare.events.v1",
                    receipt_id="receipt-ready-forge",
                    lineage_id="lineage-ready-forge",
                    evidence_group_id="event-forge",
                    data_through=DECISION_TIME + timedelta(minutes=10),
                    available_at=DECISION_TIME + timedelta(minutes=20),
                    expires_at=DECISION_TIME + timedelta(hours=4),
                    payload_sha256="e" * 64,
                ),
            ),
            reason_codes=("confirmation_arrived",),
        ),
        entity_id="600001.SH",
    )
    with pytest.raises(
        OpportunityLedgerError,
        match="opportunity_transition_identity_mismatch",
    ):
        ledger.append_transition(forged_identity)
