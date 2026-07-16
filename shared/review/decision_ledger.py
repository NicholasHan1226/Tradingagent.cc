"""Immutable decision exposure ledger contracts.

This module records selected, unfilled, rejected and shadow decisions.  It is
not a second persistence authority: durable records are adapted into the
existing append-only SampleJournal only after these contracts validate them.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple

from shared.review.sample_journal import JournalConflictError, SampleJournal


DECISION_LEDGER_SCHEMA_VERSION = 1


class DecisionLedgerContractError(ValueError):
    """Raised for unsafe or internally inconsistent decision records."""


class ExposureDisposition(str, Enum):
    PAPER_FILLED = "paper_filled"
    PAPER_NOT_FILLED = "paper_not_filled"
    REJECTED = "rejected"
    SHADOW_ONLY = "shadow_only"
    OBSERVATION_ONLY = "observation_only"


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise DecisionLedgerContractError("%s_must_be_nonempty_text" % field_name)


def _require_aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DecisionLedgerContractError("decision_time_must_be_timezone_aware")
    if value.utcoffset() is None:
        raise DecisionLedgerContractError("decision_time_must_be_timezone_aware")


def _require_nonnegative_number(value: float, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DecisionLedgerContractError("%s_must_be_nonnegative_finite" % field_name)
    if not math.isfinite(float(value)) or value < 0:
        raise DecisionLedgerContractError("%s_must_be_nonnegative_finite" % field_name)


def _require_sha256(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise DecisionLedgerContractError("%s_invalid" % field_name)


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise DecisionLedgerContractError("decision_payload_not_canonical") from exc


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_timestamp(value: datetime, field_name: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DecisionLedgerContractError("%s_must_be_timezone_aware" % field_name)
    if value.utcoffset() is None:
        raise DecisionLedgerContractError("%s_must_be_timezone_aware" % field_name)
    return value.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class DecisionExposureRecord:
    """One model decision and its exposure disposition, including no-trades."""

    decision_id: str
    decision_cluster_id: str
    decision_time: datetime
    symbol: str
    model_id: str
    model_version: str
    manifest_sha256: str
    action: str
    disposition: ExposureDisposition
    requested_notional_cny: float
    filled_quantity: int
    filled_notional_cny: float
    actual_cost_cny: float
    simulated_fill_id: Optional[str]
    rejection_reason: Optional[str]
    nonfill_reason: Optional[str]
    capital_layer: str = "simulated"
    account_type: str = "simulated"
    real_trading_enabled: bool = False
    live_transition_authorized: bool = False
    broker_order_id: Optional[str] = None

    def __post_init__(self) -> None:
        for field_name in (
            "decision_id",
            "decision_cluster_id",
            "symbol",
            "model_id",
            "model_version",
            "action",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_aware(self.decision_time)
        if (
            not isinstance(self.manifest_sha256, str)
            or len(self.manifest_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.manifest_sha256)
        ):
            raise DecisionLedgerContractError("manifest_sha256_invalid")
        if not isinstance(self.disposition, ExposureDisposition):
            raise DecisionLedgerContractError("disposition_invalid")
        _require_nonnegative_number(
            self.requested_notional_cny, "requested_notional_cny"
        )
        if isinstance(self.filled_quantity, bool) or not isinstance(
            self.filled_quantity, int
        ):
            raise DecisionLedgerContractError("filled_quantity_must_be_nonnegative_int")
        if self.filled_quantity < 0:
            raise DecisionLedgerContractError("filled_quantity_must_be_nonnegative_int")
        _require_nonnegative_number(self.filled_notional_cny, "filled_notional_cny")
        _require_nonnegative_number(self.actual_cost_cny, "actual_cost_cny")
        self._validate_simulation_only()
        self._validate_disposition()

    def _validate_simulation_only(self) -> None:
        if (
            self.real_trading_enabled is not False
            or self.live_transition_authorized is not False
            or self.broker_order_id is not None
            or self.account_type != "simulated"
            or self.capital_layer != "simulated"
        ):
            raise DecisionLedgerContractError("simulation_only_contract_violated")

    def _validate_disposition(self) -> None:
        has_fill = (
            self.filled_quantity > 0
            or self.filled_notional_cny > 0
            or self.actual_cost_cny > 0
            or self.simulated_fill_id is not None
        )
        if self.disposition is ExposureDisposition.PAPER_FILLED:
            if (
                self.filled_quantity <= 0
                or self.filled_notional_cny <= 0
                or not self.simulated_fill_id
            ):
                raise DecisionLedgerContractError("simulated_fill_evidence_required")
            if self.rejection_reason or self.nonfill_reason:
                raise DecisionLedgerContractError(
                    "filled_record_cannot_have_reject_reason"
                )
            return
        if has_fill:
            raise DecisionLedgerContractError(
                "nonfilled_record_cannot_have_fill_evidence"
            )
        if self.disposition is ExposureDisposition.PAPER_NOT_FILLED:
            if not self.nonfill_reason:
                raise DecisionLedgerContractError("nonfill_reason_required")
            if self.rejection_reason:
                raise DecisionLedgerContractError(
                    "nonfill_cannot_have_rejection_reason"
                )
        elif self.disposition is ExposureDisposition.REJECTED:
            if not self.rejection_reason:
                raise DecisionLedgerContractError("rejection_reason_required")
            if self.nonfill_reason:
                raise DecisionLedgerContractError("reject_cannot_have_nonfill_reason")
        elif self.rejection_reason or self.nonfill_reason:
            raise DecisionLedgerContractError("observation_reason_field_not_allowed")


@dataclass(frozen=True)
class DecisionExposureAuditEntry:
    """Strict readback of one decision exposure event from SampleJournal."""

    record: DecisionExposureRecord
    source_run_id: str
    input_bundle_sha256: str
    capital_authority_id: str
    authority_generation: int
    execution_lineage_id: str
    receipt_time: datetime
    canonical_source_sha256: str
    reason: str
    journal_event_id: str


def _decision_payload(record: DecisionExposureRecord) -> dict[str, Any]:
    return {
        "decision_id": record.decision_id,
        "decision_cluster_id": record.decision_cluster_id,
        "decision_time": _canonical_timestamp(record.decision_time, "decision_time"),
        "symbol": record.symbol,
        "model_id": record.model_id,
        "model_version": record.model_version,
        "manifest_sha256": record.manifest_sha256,
        "action": record.action,
        "disposition": record.disposition.value,
        "requested_notional_cny": float(record.requested_notional_cny),
        "filled_quantity": record.filled_quantity,
        "filled_notional_cny": float(record.filled_notional_cny),
        "actual_cost_cny": float(record.actual_cost_cny),
        "simulated_fill_id": record.simulated_fill_id,
        "rejection_reason": record.rejection_reason,
        "nonfill_reason": record.nonfill_reason,
        "capital_layer": record.capital_layer,
        "account_type": record.account_type,
        "real_trading_enabled": record.real_trading_enabled,
        "live_transition_authorized": record.live_transition_authorized,
        "broker_order_id": record.broker_order_id,
    }


def _disposition_reason(record: DecisionExposureRecord) -> str:
    if record.disposition is ExposureDisposition.REJECTED:
        return str(record.rejection_reason)
    if record.disposition is ExposureDisposition.PAPER_NOT_FILLED:
        return str(record.nonfill_reason)
    if record.disposition is ExposureDisposition.PAPER_FILLED:
        return "simulated_fill_recorded"
    return record.disposition.value


def _decision_event_id(source_run_id: str, decision_id: str) -> str:
    identity = {
        "decision_ledger_schema_version": DECISION_LEDGER_SCHEMA_VERSION,
        "source_run_id": source_run_id,
        "decision_id": decision_id,
    }
    return "decision-exposure-v1:%s" % _canonical_sha256(identity)


def _readback_timestamp(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise DecisionLedgerContractError("%s_invalid" % field_name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DecisionLedgerContractError("%s_invalid" % field_name) from exc
    canonical = _canonical_timestamp(parsed, field_name)
    if value != canonical:
        raise DecisionLedgerContractError("%s_not_canonical" % field_name)
    return parsed.astimezone(timezone.utc)


_DECISION_PAYLOAD_FIELDS = frozenset(
    {
        "decision_id",
        "decision_cluster_id",
        "decision_time",
        "symbol",
        "model_id",
        "model_version",
        "manifest_sha256",
        "action",
        "disposition",
        "requested_notional_cny",
        "filled_quantity",
        "filled_notional_cny",
        "actual_cost_cny",
        "simulated_fill_id",
        "rejection_reason",
        "nonfill_reason",
        "capital_layer",
        "account_type",
        "real_trading_enabled",
        "live_transition_authorized",
        "broker_order_id",
    }
)


def _record_from_payload(value: Any) -> DecisionExposureRecord:
    if not isinstance(value, Mapping) or set(value) != _DECISION_PAYLOAD_FIELDS:
        raise DecisionLedgerContractError("decision_exposure_payload_fields_invalid")
    raw = dict(value)
    try:
        disposition = ExposureDisposition(raw["disposition"])
    except (TypeError, ValueError) as exc:
        raise DecisionLedgerContractError("disposition_invalid") from exc
    record = DecisionExposureRecord(
        decision_id=raw["decision_id"],
        decision_cluster_id=raw["decision_cluster_id"],
        decision_time=_readback_timestamp(raw["decision_time"], "decision_time"),
        symbol=raw["symbol"],
        model_id=raw["model_id"],
        model_version=raw["model_version"],
        manifest_sha256=raw["manifest_sha256"],
        action=raw["action"],
        disposition=disposition,
        requested_notional_cny=raw["requested_notional_cny"],
        filled_quantity=raw["filled_quantity"],
        filled_notional_cny=raw["filled_notional_cny"],
        actual_cost_cny=raw["actual_cost_cny"],
        simulated_fill_id=raw["simulated_fill_id"],
        rejection_reason=raw["rejection_reason"],
        nonfill_reason=raw["nonfill_reason"],
        capital_layer=raw["capital_layer"],
        account_type=raw["account_type"],
        real_trading_enabled=raw["real_trading_enabled"],
        live_transition_authorized=raw["live_transition_authorized"],
        broker_order_id=raw["broker_order_id"],
    )
    if _decision_payload(record) != raw:
        raise DecisionLedgerContractError("decision_exposure_payload_not_canonical")
    return record


def _audit_entry_from_event(event: Mapping[str, Any]) -> DecisionExposureAuditEntry:
    if (
        event.get("record_type") != "chain_validation"
        or event.get("sample_layer") != "chain_validation"
        or event.get("sample_layers") != ["chain_validation"]
        or event.get("classification") != "chain_validation"
        or event.get("audit_event_type") != "decision_exposure_disposition"
        or event.get("decision_ledger_schema_version") != DECISION_LEDGER_SCHEMA_VERSION
    ):
        raise DecisionLedgerContractError("decision_exposure_audit_schema_invalid")
    if (
        event.get("audit_only") is not True
        or event.get("eligible_for_statistical_learning") is not False
        or event.get("eligible_for_performance_metrics") is not False
        or event.get("eligible_for_calibration") is not False
        or event.get("eligible_for_promotion") is not False
    ):
        raise DecisionLedgerContractError("decision_exposure_audit_scope_invalid")
    if (
        event.get("capital_layer") != "simulated"
        or event.get("account_type") != "simulated"
        or event.get("real_trading_enabled") is not False
        or event.get("live_execution_enabled") is not False
    ):
        raise DecisionLedgerContractError("simulation_only_contract_violated")

    record = _record_from_payload(event.get("decision_exposure"))
    source_run_id = event.get("source_run_id")
    input_bundle_sha256 = event.get("input_bundle_sha256")
    capital_authority_id = event.get("capital_authority_id")
    authority_generation = event.get("authority_generation")
    execution_lineage_id = event.get("execution_lineage_id")
    _require_text(source_run_id, "source_run_id")
    _require_sha256(input_bundle_sha256, "input_bundle_sha256")
    _require_text(capital_authority_id, "capital_authority_id")
    _require_text(execution_lineage_id, "execution_lineage_id")
    if (
        isinstance(authority_generation, bool)
        or not isinstance(authority_generation, int)
        or authority_generation <= 0
    ):
        raise DecisionLedgerContractError("authority_generation_invalid")

    canonical_source_sha256 = event.get("canonical_source_sha256")
    _require_sha256(canonical_source_sha256, "canonical_source_sha256")
    if canonical_source_sha256 != _canonical_sha256(_decision_payload(record)):
        raise DecisionLedgerContractError("decision_exposure_source_sha256_mismatch")
    reason = event.get("reason")
    _require_text(reason, "reason")
    if (
        event.get("decision_id") != record.decision_id
        or event.get("disposition") != record.disposition.value
        or event.get("disposition_type") != record.disposition.value
        or reason != _disposition_reason(record)
    ):
        raise DecisionLedgerContractError("decision_exposure_binding_mismatch")
    journal_event_id = event.get("journal_event_id")
    expected_event_id = "sample:%s" % _decision_event_id(
        source_run_id, record.decision_id
    )
    if journal_event_id != expected_event_id:
        raise DecisionLedgerContractError("decision_exposure_identity_mismatch")
    return DecisionExposureAuditEntry(
        record=record,
        source_run_id=source_run_id,
        input_bundle_sha256=input_bundle_sha256,
        capital_authority_id=capital_authority_id,
        authority_generation=authority_generation,
        execution_lineage_id=execution_lineage_id,
        receipt_time=_readback_timestamp(event.get("receipt_at"), "receipt_time"),
        canonical_source_sha256=canonical_source_sha256,
        reason=reason,
        journal_event_id=journal_event_id,
    )


class SampleJournalDecisionLedger:
    """Persist validated decision dispositions in the canonical SampleJournal."""

    def __init__(
        self,
        *,
        journal: SampleJournal,
        source_run_id: str,
        input_bundle_sha256: str,
        capital_authority_id: str,
        authority_generation: int,
        execution_lineage_id: str,
    ) -> None:
        if not isinstance(journal, SampleJournal):
            raise DecisionLedgerContractError("sample_journal_required")
        _require_text(source_run_id, "source_run_id")
        _require_sha256(input_bundle_sha256, "input_bundle_sha256")
        _require_text(capital_authority_id, "capital_authority_id")
        if (
            isinstance(authority_generation, bool)
            or not isinstance(authority_generation, int)
            or authority_generation <= 0
        ):
            raise DecisionLedgerContractError("authority_generation_invalid")
        _require_text(execution_lineage_id, "execution_lineage_id")
        self._journal = journal
        self.source_run_id = source_run_id
        self.input_bundle_sha256 = input_bundle_sha256
        self.capital_authority_id = capital_authority_id
        self.authority_generation = authority_generation
        self.execution_lineage_id = execution_lineage_id

    def _event_id(self, decision_id: str) -> str:
        return _decision_event_id(self.source_run_id, decision_id)

    def append(
        self,
        record: DecisionExposureRecord,
        *,
        receipt_time: datetime,
    ) -> bool:
        if not isinstance(record, DecisionExposureRecord):
            raise DecisionLedgerContractError("record_type_invalid")
        self.audit_records()
        decision_payload = _decision_payload(record)
        event = {
            "event_id": self._event_id(record.decision_id),
            "record_type": "chain_validation",
            "sample_layer": "chain_validation",
            "classification": "chain_validation",
            "audit_event_type": "decision_exposure_disposition",
            "decision_ledger_schema_version": DECISION_LEDGER_SCHEMA_VERSION,
            "decision_id": record.decision_id,
            "disposition": record.disposition.value,
            "disposition_type": record.disposition.value,
            "reason": _disposition_reason(record),
            "source_run_id": self.source_run_id,
            "input_bundle_sha256": self.input_bundle_sha256,
            "capital_authority_id": self.capital_authority_id,
            "authority_generation": self.authority_generation,
            "execution_lineage_id": self.execution_lineage_id,
            "receipt_at": _canonical_timestamp(receipt_time, "receipt_time"),
            "canonical_source_sha256": _canonical_sha256(decision_payload),
            "decision_exposure": decision_payload,
            "audit_only": True,
            "eligible_for_statistical_learning": False,
            "eligible_for_performance_metrics": False,
            "eligible_for_calibration": False,
            "eligible_for_promotion": False,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "real_trading_enabled": False,
            "live_execution_enabled": False,
        }
        try:
            result = self._journal.append_sample(event)
        except JournalConflictError as exc:
            raise DecisionLedgerContractError("conflicting_decision_identity") from exc
        return result["status"] == "appended"

    def audit_records(self) -> Tuple[DecisionExposureAuditEntry, ...]:
        entries: list[DecisionExposureAuditEntry] = []
        seen: set[str] = set()
        for event in self._journal.read_events():
            if event.get("audit_event_type") != "decision_exposure_disposition":
                continue
            entry = _audit_entry_from_event(event)
            if entry.source_run_id != self.source_run_id:
                continue
            if (
                entry.input_bundle_sha256 != self.input_bundle_sha256
                or entry.capital_authority_id != self.capital_authority_id
                or entry.authority_generation != self.authority_generation
                or entry.execution_lineage_id != self.execution_lineage_id
            ):
                raise DecisionLedgerContractError("readback_context_mismatch")
            if entry.record.decision_id in seen:
                raise DecisionLedgerContractError("duplicate_decision_identity")
            seen.add(entry.record.decision_id)
            entries.append(entry)
        return tuple(entries)

    def records(self) -> Tuple[DecisionExposureRecord, ...]:
        return tuple(entry.record for entry in self.audit_records())

    def by_disposition(
        self, disposition: ExposureDisposition
    ) -> Tuple[DecisionExposureRecord, ...]:
        if not isinstance(disposition, ExposureDisposition):
            raise DecisionLedgerContractError("disposition_invalid")
        return tuple(
            entry.record
            for entry in self.audit_records()
            if entry.record.disposition is disposition
        )


class InMemoryDecisionLedger:
    """Idempotent process-local collector with no production write path."""

    def __init__(self) -> None:
        self._records: Dict[str, DecisionExposureRecord] = {}

    def append(self, record: DecisionExposureRecord) -> bool:
        if not isinstance(record, DecisionExposureRecord):
            raise DecisionLedgerContractError("record_type_invalid")
        existing = self._records.get(record.decision_id)
        if existing is None:
            self._records[record.decision_id] = record
            return True
        if existing == record:
            return False
        raise DecisionLedgerContractError("conflicting_decision_id")

    def records(self) -> Tuple[DecisionExposureRecord, ...]:
        return tuple(self._records.values())

    def by_disposition(
        self, disposition: ExposureDisposition
    ) -> Tuple[DecisionExposureRecord, ...]:
        return tuple(
            record
            for record in self._records.values()
            if record.disposition is disposition
        )
