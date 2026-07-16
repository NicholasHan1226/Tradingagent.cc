from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

import shared.review.decision_ledger as decision_ledger_module
from shared.review.decision_ledger import (
    DecisionExposureRecord,
    DecisionLedgerContractError,
    ExposureDisposition,
)
from shared.review.sample_journal import SampleJournal
from shared.review.sample_kpi import build_sample_kpi


DECISION_TIME = datetime(2026, 7, 16, 2, 30, tzinfo=timezone.utc)
RECEIPT_TIME = datetime(2026, 7, 16, 2, 31, tzinfo=timezone.utc)
SOURCE_RUN_ID = "ashare-paper-day-" + "1" * 32
INPUT_BUNDLE_SHA256 = "b" * 64
AUTHORITY_ID = "ashare-capital-v1"
AUTHORITY_GENERATION = 1
EXECUTION_LINEAGE_ID = "ashare-sim-fixture-v1"


def _record(
    decision_id: str = "decision-rejected",
    disposition: ExposureDisposition = ExposureDisposition.REJECTED,
    **overrides: object,
) -> DecisionExposureRecord:
    values = {
        "decision_id": decision_id,
        "decision_cluster_id": "cluster-1",
        "decision_time": DECISION_TIME,
        "symbol": "600000.SH",
        "model_id": "ashare-champion",
        "model_version": "1.0.0",
        "manifest_sha256": "a" * 64,
        "action": "buy",
        "disposition": disposition,
        "requested_notional_cny": 5_000.0,
        "filled_quantity": 0,
        "filled_notional_cny": 0.0,
        "actual_cost_cny": 0.0,
        "simulated_fill_id": None,
        "rejection_reason": "insufficient_net_edge_after_cost",
        "nonfill_reason": None,
    }
    values.update(overrides)
    return DecisionExposureRecord(**values)


def _ledger(
    path: Path, **overrides: object
) -> decision_ledger_module.SampleJournalDecisionLedger:
    values = {
        "journal": SampleJournal(path),
        "source_run_id": SOURCE_RUN_ID,
        "input_bundle_sha256": INPUT_BUNDLE_SHA256,
        "capital_authority_id": AUTHORITY_ID,
        "authority_generation": AUTHORITY_GENERATION,
        "execution_lineage_id": EXECUTION_LINEAGE_ID,
    }
    values.update(overrides)
    return decision_ledger_module.SampleJournalDecisionLedger(**values)


def test_persists_disposition_as_audit_only_sample_journal_chain_event(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "sample_journal.jsonl"
    ledger = _ledger(journal_path)
    record = _record()

    assert ledger.append(record, receipt_time=RECEIPT_TIME) is True

    events = SampleJournal(journal_path).read_events()
    assert len(events) == 1
    event = events[0]
    assert event["journal_event_type"] == "sample_event"
    assert event["record_type"] == "chain_validation"
    assert event["sample_layer"] == "chain_validation"
    assert event["sample_layers"] == ["chain_validation"]
    assert event["classification"] == "chain_validation"
    assert event["audit_event_type"] == "decision_exposure_disposition"
    assert event["decision_ledger_schema_version"] == 1
    assert event["decision_id"] == record.decision_id
    assert event["disposition"] == "rejected"
    assert event["disposition_type"] == "rejected"
    assert event["reason"] == "insufficient_net_edge_after_cost"
    assert event["source_run_id"] == SOURCE_RUN_ID
    assert event["input_bundle_sha256"] == INPUT_BUNDLE_SHA256
    assert event["capital_authority_id"] == AUTHORITY_ID
    assert event["authority_generation"] == AUTHORITY_GENERATION
    assert event["execution_lineage_id"] == EXECUTION_LINEAGE_ID
    assert event["receipt_at"] == RECEIPT_TIME.isoformat()
    expected_source_sha256 = hashlib.sha256(
        json.dumps(
            event["decision_exposure"],
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    assert event["canonical_source_sha256"] == expected_source_sha256
    assert event["canonical_source_sha256"] != record.manifest_sha256
    assert event["audit_only"] is True
    assert event["eligible_for_statistical_learning"] is False
    assert event["eligible_for_performance_metrics"] is False
    assert event["eligible_for_calibration"] is False
    assert event["eligible_for_promotion"] is False
    assert event["decision_exposure"] == {
        "decision_id": "decision-rejected",
        "decision_cluster_id": "cluster-1",
        "decision_time": DECISION_TIME.isoformat(),
        "symbol": "600000.SH",
        "model_id": "ashare-champion",
        "model_version": "1.0.0",
        "manifest_sha256": "a" * 64,
        "action": "buy",
        "disposition": "rejected",
        "requested_notional_cny": 5_000.0,
        "filled_quantity": 0,
        "filled_notional_cny": 0.0,
        "actual_cost_cny": 0.0,
        "simulated_fill_id": None,
        "rejection_reason": "insufficient_net_edge_after_cost",
        "nonfill_reason": None,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real_trading_enabled": False,
        "live_transition_authorized": False,
        "broker_order_id": None,
    }

    kpi = build_sample_kpi(events)
    assert kpi["sample_layer_totals"]["chain_validation"] == 1
    assert kpi["sample_layer_totals"]["completed_round_trip"] == 0
    assert kpi["styles"]["unknown"]["win_rate"] is None
    assert kpi["styles"]["unknown"]["expectancy_cny"] is None
    assert kpi["sample_size_evidence"]["raw_N"] == 0
    assert kpi["calibration_evidence"]["independent_sample_count"] == 0
    assert not (tmp_path / "decision_ledger.jsonl").exists()


def test_same_content_replay_is_idempotent_but_identity_conflict_fails_closed(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "sample_journal.jsonl"
    ledger = _ledger(journal_path)
    record = _record()

    assert ledger.append(record, receipt_time=RECEIPT_TIME) is True
    assert ledger.append(record, receipt_time=RECEIPT_TIME) is False

    conflicting = _record(model_version="2.0.0")
    with pytest.raises(
        DecisionLedgerContractError, match="conflicting_decision_identity"
    ):
        ledger.append(conflicting, receipt_time=RECEIPT_TIME)

    assert len(SampleJournal(journal_path).read_events()) == 1


def test_restart_strictly_reads_back_validated_record_and_bound_context(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "sample_journal.jsonl"
    record = _record()
    _ledger(journal_path).append(record, receipt_time=RECEIPT_TIME)

    restarted = _ledger(journal_path)
    entries = restarted.audit_records()

    assert len(entries) == 1
    entry = entries[0]
    assert entry.record == record
    assert entry.source_run_id == SOURCE_RUN_ID
    assert entry.input_bundle_sha256 == INPUT_BUNDLE_SHA256
    assert entry.capital_authority_id == AUTHORITY_ID
    assert entry.authority_generation == AUTHORITY_GENERATION
    assert entry.execution_lineage_id == EXECUTION_LINEAGE_ID
    assert entry.receipt_time == RECEIPT_TIME
    assert (
        entry.canonical_source_sha256
        == SampleJournal(journal_path).read_events()[0]["canonical_source_sha256"]
    )
    assert entry.reason == "insufficient_net_edge_after_cost"
    assert restarted.records() == (record,)
    assert restarted.by_disposition(ExposureDisposition.REJECTED) == (record,)

    drifted = _ledger(journal_path, input_bundle_sha256="c" * 64)
    with pytest.raises(DecisionLedgerContractError, match="readback_context_mismatch"):
        drifted.audit_records()


def test_run_context_drift_is_rejected_before_a_second_event_is_appended(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "sample_journal.jsonl"
    _ledger(journal_path).append(_record(), receipt_time=RECEIPT_TIME)
    drifted = _ledger(journal_path, input_bundle_sha256="c" * 64)

    with pytest.raises(DecisionLedgerContractError, match="readback_context_mismatch"):
        drifted.append(
            _record(decision_id="decision-other"),
            receipt_time=RECEIPT_TIME,
        )

    assert len(SampleJournal(journal_path).read_events()) == 1


def test_restart_recomputes_canonical_source_sha_and_rejects_forged_binding(
    tmp_path: Path,
) -> None:
    template_path = tmp_path / "template-journal.jsonl"
    _ledger(template_path).append(_record(), receipt_time=RECEIPT_TIME)
    template = SampleJournal(template_path).read_events()[0]
    sample_journal_fields = {
        "journal_schema_version",
        "journal_payload_sha256",
        "journal_event_type",
        "journal_event_id",
        "sample_layers",
    }
    forged = {
        key: value
        for key, value in template.items()
        if key not in sample_journal_fields
    }
    forged["decision_exposure"] = {
        **forged["decision_exposure"],
        "model_version": "forged-version",
    }
    forged_path = tmp_path / "forged-journal.jsonl"
    SampleJournal(forged_path).append_sample(forged)

    with pytest.raises(
        DecisionLedgerContractError,
        match="decision_exposure_source_sha256_mismatch",
    ):
        _ledger(forged_path).audit_records()


@pytest.mark.parametrize("unsafe_value", [-1.0, float("nan"), float("inf")])
def test_persistence_rejects_unsafe_source_economics(
    unsafe_value: float,
) -> None:
    with pytest.raises(
        DecisionLedgerContractError,
        match="requested_notional_cny_must_be_nonnegative_finite",
    ):
        _record(requested_notional_cny=unsafe_value)
