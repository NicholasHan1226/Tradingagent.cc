#!/usr/bin/env python3
"""Immutable point-in-time input bundle for one TradingAgent decision.

This module is deliberately storage- and transport-free.  It accepts only
already validated SharedSignals V1 envelopes and explicit evidence decisions.
It then freezes their identity, timing and eligibility into one reproducible
research snapshot.  It cannot query SQLite, legacy endpoints, files or caches.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .evidence_gate import EvidenceAction, EvidenceDecision
from .sharedsignals_v1 import QueryEnvelope


_DATASET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_ROLES = frozenset({"required_execution", "optional_context"})


class ResearchDataContractError(ValueError):
    """Raised when inputs cannot form one deterministic PIT snapshot."""


def _nonempty_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchDataContractError(f"{field_name} must be a non-empty string")
    if value != value.strip():
        raise ResearchDataContractError(
            f"{field_name} must not contain outer whitespace"
        )
    return value


def _dataset_id(value: object, *, field_name: str = "dataset_id") -> str:
    normalized = _nonempty_string(value, field_name=field_name)
    if not _DATASET_ID_RE.fullmatch(normalized):
        raise ResearchDataContractError(f"{field_name} has an invalid canonical format")
    return normalized


def _canonical_json(value: object, *, field_name: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ResearchDataContractError(
            f"{field_name} must contain canonical JSON values"
        ) from exc


def _aware_instant(value: object, *, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = _nonempty_string(value, field_name=field_name)
        normalized = f"{text[:-1]}+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ResearchDataContractError(
                f"{field_name} must be an ISO-8601 timestamp"
            ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResearchDataContractError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _normalized_instant(value: object, *, field_name: str) -> str:
    return _aware_instant(value, field_name=field_name).isoformat()


def _unique_by_dataset(values: Iterable[object], *, kind: str) -> dict[str, object]:
    result: dict[str, object] = {}
    for value in values:
        dataset = getattr(value, "dataset_id", None)
        dataset_id = _dataset_id(dataset, field_name=f"{kind}.dataset_id")
        if dataset_id in result:
            raise ResearchDataContractError(f"duplicate_{kind}_dataset_id:{dataset_id}")
        result[dataset_id] = value
    return result


@dataclass(frozen=True)
class DatasetRequirement:
    """Role of one explicitly configured canonical dataset."""

    dataset_id: str
    role: str
    row_event_time_field: str = "event_time"
    row_available_time_field: str = "available_time"
    row_revision_id_field: str = "revision_id"
    row_receipt_id_field: str = "receipt_id"
    minimum_row_count: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "dataset_id", _dataset_id(self.dataset_id))
        role = _nonempty_string(self.role, field_name="role")
        if role not in _ROLES:
            raise ResearchDataContractError(
                "role must be required_execution or optional_context"
            )
        object.__setattr__(self, "role", role)
        for field_name in (
            "row_event_time_field",
            "row_available_time_field",
            "row_revision_id_field",
            "row_receipt_id_field",
        ):
            object.__setattr__(
                self,
                field_name,
                _nonempty_string(getattr(self, field_name), field_name=field_name),
            )
        if (
            isinstance(self.minimum_row_count, bool)
            or not isinstance(self.minimum_row_count, int)
            or self.minimum_row_count < 0
        ):
            raise ResearchDataContractError(
                "minimum_row_count must be a non-negative integer"
            )


@dataclass(frozen=True)
class ResearchDataProfile:
    """Version-pinned dataset set required by a research/decision profile."""

    profile_id: str
    catalog_version: str
    requirements: tuple[DatasetRequirement, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "profile_id",
            _nonempty_string(self.profile_id, field_name="profile_id"),
        )
        object.__setattr__(
            self,
            "catalog_version",
            _nonempty_string(self.catalog_version, field_name="catalog_version"),
        )
        if not isinstance(self.requirements, tuple) or not self.requirements:
            raise ResearchDataContractError("requirements must be a non-empty tuple")
        seen: set[str] = set()
        required_count = 0
        for requirement in self.requirements:
            if not isinstance(requirement, DatasetRequirement):
                raise ResearchDataContractError(
                    "requirements must contain DatasetRequirement values"
                )
            if requirement.dataset_id in seen:
                raise ResearchDataContractError(
                    f"duplicate_profile_dataset_id:{requirement.dataset_id}"
                )
            seen.add(requirement.dataset_id)
            if requirement.role == "required_execution":
                required_count += 1
        if required_count == 0:
            raise ResearchDataContractError(
                "profile must contain at least one required_execution dataset"
            )

    @property
    def dataset_ids(self) -> tuple[str, ...]:
        return tuple(item.dataset_id for item in self.requirements)


@dataclass(frozen=True)
class ResearchDatasetSnapshot:
    """Immutable evidence identity for one dataset inside a decision snapshot."""

    dataset_id: str
    role: str
    api_version: str
    catalog_version: str
    request_id: str
    receipt_id: str | None
    evidence_state: str
    evidence_action: str
    eligible: bool
    weight: float
    reasons: tuple[str, ...]
    source_proof_complete: bool
    data_through: str | None
    observed_at: str | None
    next_cursor: str | None
    row_count: int
    row_pit_sha256: str
    max_row_available_time: str | None
    response_sha256: str
    _rows_json: str = field(repr=False)

    def decoded_rows(self) -> list[dict[str, Any]]:
        """Return a fresh copy so callers cannot mutate the frozen snapshot."""

        decoded = json.loads(self._rows_json)
        if not isinstance(decoded, list):  # pragma: no cover - constructor invariant
            raise ResearchDataContractError("stored rows are not a list")
        return decoded


@dataclass(frozen=True)
class ResearchDataSnapshot:
    """Complete, reproducible inputs for one decision timestamp."""

    profile_id: str
    catalog_version: str
    decision_as_of: str
    datasets: tuple[ResearchDatasetSnapshot, ...]
    execution_eligible: bool
    blocking_reasons: tuple[str, ...]
    snapshot_sha256: str

    def to_evidence_payload(self) -> dict[str, Any]:
        """Return the provider-neutral day-loop evidence projection.

        The projection intentionally excludes rows.  Downstream orchestration
        receives only immutable identity, eligibility and receipt evidence;
        research/decision components consume rows from this snapshot object.
        """

        return {
            "profile_id": self.profile_id,
            "catalog_version": self.catalog_version,
            "decision_as_of": self.decision_as_of,
            "snapshot_sha256": self.snapshot_sha256,
            "execution_eligible": self.execution_eligible,
            "blocking_reasons": list(self.blocking_reasons),
            "datasets": [
                {
                    "dataset_id": dataset.dataset_id,
                    "role": dataset.role,
                    "state": dataset.evidence_state,
                    "evidence_action": dataset.evidence_action,
                    "effective_weight": dataset.weight,
                    "source_proof_complete": dataset.source_proof_complete,
                    "receipt_id": dataset.receipt_id,
                    "row_count": dataset.row_count,
                    "row_pit_sha256": dataset.row_pit_sha256,
                    "max_row_available_time": dataset.max_row_available_time,
                    "reasons": list(dataset.reasons),
                }
                for dataset in self.datasets
            ],
        }


def _validate_decision(decision: EvidenceDecision) -> None:
    if not isinstance(decision, EvidenceDecision):
        raise ResearchDataContractError(
            "decisions must contain EvidenceDecision values"
        )
    if not isinstance(decision.action, EvidenceAction):
        raise ResearchDataContractError("decision.action must be EvidenceAction")
    if type(decision.eligible) is not bool:
        raise ResearchDataContractError("decision.eligible must be a native bool")
    if isinstance(decision.weight, bool) or not isinstance(
        decision.weight, (int, float)
    ):
        raise ResearchDataContractError("decision.weight must be numeric")
    weight = float(decision.weight)
    if decision.action is EvidenceAction.ACCEPT:
        valid = decision.eligible and weight == 1.0
    elif decision.action is EvidenceAction.DEWEIGHT:
        valid = decision.eligible and 0.0 < weight < 1.0
    else:
        valid = not decision.eligible and weight == 0.0
    if not valid:
        raise ResearchDataContractError(
            f"inconsistent_evidence_decision:{decision.dataset_id}"
        )
    for reason in decision.reasons:
        _nonempty_string(reason, field_name="decision.reasons item")


def _dataset_snapshot(
    *,
    requirement: DatasetRequirement,
    envelope: QueryEnvelope,
    decision: EvidenceDecision,
    decision_instant: datetime,
) -> ResearchDatasetSnapshot:
    if not isinstance(envelope, QueryEnvelope):
        raise ResearchDataContractError("envelopes must contain QueryEnvelope values")
    _validate_decision(decision)
    if envelope.dataset_id != decision.dataset_id:
        raise ResearchDataContractError(
            f"decision_dataset_mismatch:{requirement.dataset_id}"
        )
    if envelope.metadata.receipt_id != decision.receipt_id:
        raise ResearchDataContractError(f"receipt_mismatch:{requirement.dataset_id}")

    source_proof_complete = bool(
        isinstance(envelope.metadata.lineage, Mapping)
        and envelope.metadata.lineage
        and isinstance(envelope.metadata.receipt_id, str)
        and envelope.metadata.receipt_id
        and isinstance(envelope.metadata.data_through, str)
        and envelope.metadata.data_through
        and isinstance(envelope.metadata.observed_at, str)
        and envelope.metadata.observed_at
    )
    if not source_proof_complete and decision.action is not EvidenceAction.REJECT:
        raise ResearchDataContractError(
            f"incomplete_source_proof_must_reject:{requirement.dataset_id}"
        )

    observed = (
        _aware_instant(
            envelope.metadata.observed_at,
            field_name="metadata.observed_at",
        )
        if envelope.metadata.observed_at is not None
        else None
    )
    data_through = (
        _aware_instant(
            envelope.metadata.data_through,
            field_name="metadata.data_through",
        )
        if envelope.metadata.data_through is not None
        else None
    )
    if observed is not None and observed > decision_instant:
        raise ResearchDataContractError(
            f"observed_after_decision:{requirement.dataset_id}"
        )
    if data_through is not None and data_through > decision_instant:
        raise ResearchDataContractError(
            f"data_through_after_decision:{requirement.dataset_id}"
        )

    reported_rows = list(envelope.data)
    rows = reported_rows if source_proof_complete else []
    if source_proof_complete and len(rows) < requirement.minimum_row_count:
        raise ResearchDataContractError(
            f"row_count_below_minimum:{requirement.dataset_id}"
        )
    row_pit_identities: list[dict[str, str]] = []
    max_row_available: datetime | None = None
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ResearchDataContractError(
                f"row_must_be_mapping:{requirement.dataset_id}:{index}"
            )
        event_time = _aware_instant(
            row.get(requirement.row_event_time_field),
            field_name=(
                f"row.{requirement.row_event_time_field}:"
                f"{requirement.dataset_id}:{index}"
            ),
        )
        available_time = _aware_instant(
            row.get(requirement.row_available_time_field),
            field_name=(
                f"row.{requirement.row_available_time_field}:"
                f"{requirement.dataset_id}:{index}"
            ),
        )
        if event_time > available_time:
            raise ResearchDataContractError(
                f"row_event_after_available:{requirement.dataset_id}:{index}"
            )
        if available_time > decision_instant:
            raise ResearchDataContractError(
                f"row_available_after_decision:{requirement.dataset_id}:{index}"
            )
        if observed is None:  # pragma: no cover - source proof invariant
            raise ResearchDataContractError(
                f"metadata_observed_at_missing:{requirement.dataset_id}"
            )
        if available_time > observed:
            raise ResearchDataContractError(
                f"row_available_after_envelope_observed:"
                f"{requirement.dataset_id}:{index}"
            )
        revision_id = _nonempty_string(
            row.get(requirement.row_revision_id_field),
            field_name=(
                f"row.{requirement.row_revision_id_field}:"
                f"{requirement.dataset_id}:{index}"
            ),
        )
        row_receipt_id = _nonempty_string(
            row.get(requirement.row_receipt_id_field),
            field_name=(
                f"row.{requirement.row_receipt_id_field}:"
                f"{requirement.dataset_id}:{index}"
            ),
        )
        max_row_available = (
            available_time
            if max_row_available is None or available_time > max_row_available
            else max_row_available
        )
        row_pit_identities.append(
            {
                "event_time": event_time.isoformat(),
                "available_time": available_time.isoformat(),
                "revision_id": revision_id,
                "receipt_id": row_receipt_id,
                "row_sha256": hashlib.sha256(
                    _canonical_json(row, field_name="envelope.data row").encode("utf-8")
                ).hexdigest(),
            }
        )
    rows_json = _canonical_json(rows, field_name="envelope.data")
    row_pit_sha256 = hashlib.sha256(
        _canonical_json(
            row_pit_identities,
            field_name="row PIT identities",
        ).encode("utf-8")
    ).hexdigest()
    response_payload = {
        "api_version": envelope.api_version,
        "catalog_version": envelope.catalog_version,
        "request_id": envelope.request_id,
        "dataset_id": envelope.dataset_id,
        "data": json.loads(
            _canonical_json(reported_rows, field_name="reported envelope.data")
        ),
        "next_cursor": envelope.next_cursor,
        "metadata": {
            "state": envelope.metadata.state,
            "degraded": envelope.metadata.degraded,
            "freshness": envelope.metadata.freshness,
            "quality": envelope.metadata.quality,
            "lineage": envelope.metadata.lineage,
            "receipt_id": envelope.metadata.receipt_id,
            "data_through": envelope.metadata.data_through,
            "observed_at": envelope.metadata.observed_at,
            "reasons": list(envelope.metadata.reasons),
        },
        "evidence": {
            "effective_state": decision.effective_state,
            "action": decision.action.value,
            "eligible": decision.eligible,
            "weight": float(decision.weight),
            "reasons": list(decision.reasons),
            "source_proof_complete": source_proof_complete,
        },
        "role": requirement.role,
        "row_pit": {
            "row_count": len(rows),
            "minimum_row_count": requirement.minimum_row_count,
            "row_event_time_field": requirement.row_event_time_field,
            "row_available_time_field": requirement.row_available_time_field,
            "row_revision_id_field": requirement.row_revision_id_field,
            "row_receipt_id_field": requirement.row_receipt_id_field,
            "row_pit_sha256": row_pit_sha256,
            "max_row_available_time": (
                max_row_available.isoformat() if max_row_available is not None else None
            ),
        },
    }
    response_json = _canonical_json(response_payload, field_name="response snapshot")
    response_sha256 = hashlib.sha256(response_json.encode("utf-8")).hexdigest()
    return ResearchDatasetSnapshot(
        dataset_id=requirement.dataset_id,
        role=requirement.role,
        api_version=envelope.api_version,
        catalog_version=envelope.catalog_version,
        request_id=envelope.request_id,
        receipt_id=envelope.metadata.receipt_id,
        evidence_state=decision.effective_state,
        evidence_action=decision.action.value,
        eligible=decision.eligible,
        weight=float(decision.weight),
        reasons=tuple(decision.reasons),
        source_proof_complete=source_proof_complete,
        data_through=(
            _normalized_instant(
                envelope.metadata.data_through,
                field_name="metadata.data_through",
            )
            if envelope.metadata.data_through is not None
            else None
        ),
        observed_at=(
            _normalized_instant(
                envelope.metadata.observed_at,
                field_name="metadata.observed_at",
            )
            if envelope.metadata.observed_at is not None
            else None
        ),
        next_cursor=envelope.next_cursor,
        row_count=len(rows),
        row_pit_sha256=row_pit_sha256,
        max_row_available_time=(
            max_row_available.isoformat() if max_row_available is not None else None
        ),
        response_sha256=response_sha256,
        _rows_json=rows_json,
    )


def build_research_data_snapshot(
    *,
    profile: ResearchDataProfile,
    envelopes: tuple[QueryEnvelope, ...],
    decisions: tuple[EvidenceDecision, ...],
    decision_as_of: datetime,
) -> ResearchDataSnapshot:
    """Bind an exact catalog/dataset/receipt set to one PIT decision instant."""

    if not isinstance(profile, ResearchDataProfile):
        raise ResearchDataContractError("profile must be ResearchDataProfile")
    decision_instant = _aware_instant(decision_as_of, field_name="decision_as_of")
    envelope_by_id = _unique_by_dataset(envelopes, kind="envelope")
    decision_by_id = _unique_by_dataset(decisions, kind="decision")
    expected = set(profile.dataset_ids)
    if set(envelope_by_id) != expected:
        raise ResearchDataContractError("envelope_dataset_set_mismatch")
    if set(decision_by_id) != expected:
        raise ResearchDataContractError("decision_dataset_set_mismatch")

    snapshots: list[ResearchDatasetSnapshot] = []
    blocking_reasons: list[str] = []
    for requirement in profile.requirements:
        envelope = envelope_by_id[requirement.dataset_id]
        decision = decision_by_id[requirement.dataset_id]
        if not isinstance(envelope, QueryEnvelope):
            raise ResearchDataContractError(
                "envelopes must contain QueryEnvelope values"
            )
        if envelope.catalog_version != profile.catalog_version:
            raise ResearchDataContractError(
                f"catalog_version_mismatch:{requirement.dataset_id}"
            )
        snapshot = _dataset_snapshot(
            requirement=requirement,
            envelope=envelope,
            decision=decision,
            decision_instant=decision_instant,
        )
        snapshots.append(snapshot)
        if requirement.role == "required_execution" and (
            decision.action is not EvidenceAction.ACCEPT
        ):
            impairment = (
                "deweighted"
                if decision.action is EvidenceAction.DEWEIGHT
                else "rejected"
            )
            blocking_reasons.append(
                f"required_dataset_{impairment}:{requirement.dataset_id}"
            )
        if not snapshot.source_proof_complete:
            blocking_reasons.append(
                f"dataset_source_proof_incomplete:{requirement.dataset_id}"
            )

    decision_as_of_text = decision_instant.isoformat()
    snapshot_payload = {
        "profile_id": profile.profile_id,
        "catalog_version": profile.catalog_version,
        "decision_as_of": decision_as_of_text,
        "datasets": [
            {
                "dataset_id": item.dataset_id,
                "role": item.role,
                "response_sha256": item.response_sha256,
            }
            for item in snapshots
        ],
        "blocking_reasons": blocking_reasons,
    }
    snapshot_json = _canonical_json(snapshot_payload, field_name="research snapshot")
    return ResearchDataSnapshot(
        profile_id=profile.profile_id,
        catalog_version=profile.catalog_version,
        decision_as_of=decision_as_of_text,
        datasets=tuple(snapshots),
        execution_eligible=not blocking_reasons,
        blocking_reasons=tuple(blocking_reasons),
        snapshot_sha256=hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest(),
    )
