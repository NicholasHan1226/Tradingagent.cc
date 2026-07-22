#!/usr/bin/env python3
"""Immutable current-observation input bundle for one TradingAgent decision.

This module is deliberately storage- and transport-free.  It accepts only
already validated TradingDatas V1 envelopes and explicit evidence decisions.
It freezes provider-native rows, envelope source proof, observation timing and
bounded pagination identity into one reproducible research snapshot.  Without
historical availability/revision evidence it never claims historical PIT
eligibility.  It cannot query SQLite, legacy endpoints, files or caches.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .evidence_gate import EvidenceAction, EvidenceDecision
from .sharedsignals_v1 import QueryEnvelope
from .tradingdatas_pagination import PagedQueryRun, PaginationContractError


_DATASET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_ROLES = frozenset({"required_execution", "optional_context"})
_OBSERVATION_MODES = frozenset({"current_observation"})
_ROW_EVENT_FORMATS = frozenset({"yyyymmdd", "iso8601"})
_ROW_EVENT_SEMANTICS = frozenset({"session", "scheduled", "effective"})
_QUERY_AS_OF_MODES = frozenset({"decision_as_of", "omit"})


class ResearchDataContractError(ValueError):
    """Raised when inputs cannot form one deterministic evidence snapshot."""


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
    identity_fields: tuple[str, ...]
    observation_mode: str = "current_observation"
    query_as_of_mode: str = "decision_as_of"
    row_event_time_field: str | None = None
    row_event_time_format: str | None = None
    row_event_timezone: str | None = None
    row_event_time_semantic: str | None = None
    minimum_row_count: int = 1
    max_pages: int = 20
    max_rows: int = 100_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "dataset_id", _dataset_id(self.dataset_id))
        role = _nonempty_string(self.role, field_name="role")
        if role not in _ROLES:
            raise ResearchDataContractError(
                "role must be required_execution or optional_context"
            )
        object.__setattr__(self, "role", role)
        if not isinstance(self.identity_fields, tuple) or not self.identity_fields:
            raise ResearchDataContractError("identity_fields must be a non-empty tuple")
        normalized_identity_fields: list[str] = []
        for value in self.identity_fields:
            normalized = _nonempty_string(value, field_name="identity_fields item")
            if normalized in normalized_identity_fields:
                raise ResearchDataContractError(
                    "identity_fields must not contain duplicates"
                )
            normalized_identity_fields.append(normalized)
        object.__setattr__(self, "identity_fields", tuple(normalized_identity_fields))
        observation_mode = _nonempty_string(
            self.observation_mode,
            field_name="observation_mode",
        )
        if observation_mode not in _OBSERVATION_MODES:
            raise ResearchDataContractError(
                "observation_mode must be current_observation"
            )
        object.__setattr__(self, "observation_mode", observation_mode)
        query_as_of_mode = _nonempty_string(
            self.query_as_of_mode,
            field_name="query_as_of_mode",
        )
        if query_as_of_mode not in _QUERY_AS_OF_MODES:
            raise ResearchDataContractError(
                "query_as_of_mode must be decision_as_of or omit"
            )
        object.__setattr__(self, "query_as_of_mode", query_as_of_mode)
        if self.row_event_time_field is None:
            if any(
                value is not None
                for value in (
                    self.row_event_time_format,
                    self.row_event_timezone,
                    self.row_event_time_semantic,
                )
            ):
                raise ResearchDataContractError(
                    "row event format/timezone/semantic require row_event_time_field"
                )
        else:
            object.__setattr__(
                self,
                "row_event_time_field",
                _nonempty_string(
                    self.row_event_time_field,
                    field_name="row_event_time_field",
                ),
            )
            row_format = _nonempty_string(
                self.row_event_time_format,
                field_name="row_event_time_format",
            )
            if row_format not in _ROW_EVENT_FORMATS:
                raise ResearchDataContractError(
                    "row_event_time_format must be yyyymmdd or iso8601"
                )
            object.__setattr__(self, "row_event_time_format", row_format)
            row_semantic = _nonempty_string(
                self.row_event_time_semantic,
                field_name="row_event_time_semantic",
            )
            if row_semantic not in _ROW_EVENT_SEMANTICS:
                raise ResearchDataContractError(
                    "row_event_time_semantic must be session, scheduled or effective"
                )
            object.__setattr__(self, "row_event_time_semantic", row_semantic)
            if row_format == "yyyymmdd":
                timezone_name = _nonempty_string(
                    self.row_event_timezone,
                    field_name="row_event_timezone",
                )
                try:
                    ZoneInfo(timezone_name)
                except ZoneInfoNotFoundError as exc:
                    raise ResearchDataContractError(
                        "row_event_timezone must be a valid IANA timezone"
                    ) from exc
                object.__setattr__(self, "row_event_timezone", timezone_name)
            elif self.row_event_timezone is not None:
                raise ResearchDataContractError(
                    "row_event_timezone is only valid for yyyymmdd"
                )
        if (
            isinstance(self.minimum_row_count, bool)
            or not isinstance(self.minimum_row_count, int)
            or self.minimum_row_count < 0
        ):
            raise ResearchDataContractError(
                "minimum_row_count must be a non-negative integer"
            )
        for field_name in ("max_pages", "max_rows"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ResearchDataContractError(
                    f"{field_name} must be a positive integer"
                )
        if self.minimum_row_count > self.max_rows:
            raise ResearchDataContractError(
                "minimum_row_count must not exceed max_rows"
            )

    def to_contract_payload(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "role": self.role,
            "identity_fields": list(self.identity_fields),
            "observation_mode": self.observation_mode,
            "query_as_of_mode": self.query_as_of_mode,
            "row_event_time_field": self.row_event_time_field,
            "row_event_time_format": self.row_event_time_format,
            "row_event_timezone": self.row_event_timezone,
            "row_event_time_semantic": self.row_event_time_semantic,
            "minimum_row_count": self.minimum_row_count,
            "max_pages": self.max_pages,
            "max_rows": self.max_rows,
        }


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

    @property
    def contract_sha256(self) -> str:
        payload = {
            "profile_id": self.profile_id,
            "catalog_version": self.catalog_version,
            "requirements": [
                requirement.to_contract_payload()
                for requirement in self.requirements
            ],
        }
        return hashlib.sha256(
            _canonical_json(payload, field_name="research profile").encode("utf-8")
        ).hexdigest()


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
    lineage_sha256: str | None
    source_proof_sha256: str | None
    data_through: str | None
    observed_at: str | None
    next_cursor: str | None
    row_count: int
    observation_mode: str
    historical_pit_eligible: bool
    query_as_of_mode: str
    minimum_row_count: int
    max_pages: int
    max_rows: int
    identity_fields: tuple[str, ...]
    row_event_time_field: str | None
    row_event_time_format: str | None
    row_event_timezone: str | None
    row_event_time_semantic: str | None
    identity_sha256: str
    row_observation_sha256: str
    max_row_observed_at: str | None
    max_row_event_value: str | None
    page_count: int
    pagination_trace_sha256: str
    pagination_semantic_sha256: str
    page_request_set_sha256: str
    page_response_set_sha256: str
    cursor_chain_sha256: str
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
    profile_contract_sha256: str
    catalog_version: str
    decision_as_of: str
    datasets: tuple[ResearchDatasetSnapshot, ...]
    execution_eligible: bool
    historical_pit_eligible: bool
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
            "profile_contract_sha256": self.profile_contract_sha256,
            "catalog_version": self.catalog_version,
            "decision_as_of": self.decision_as_of,
            "snapshot_sha256": self.snapshot_sha256,
            "execution_eligible": self.execution_eligible,
            "historical_pit_eligible": self.historical_pit_eligible,
            "blocking_reasons": list(self.blocking_reasons),
            "datasets": [
                {
                    "dataset_id": dataset.dataset_id,
                    "role": dataset.role,
                    "state": dataset.evidence_state,
                    "evidence_action": dataset.evidence_action,
                    "effective_weight": dataset.weight,
                    "source_proof_complete": dataset.source_proof_complete,
                    "lineage_sha256": dataset.lineage_sha256,
                    "source_proof_sha256": dataset.source_proof_sha256,
                    "receipt_id": dataset.receipt_id,
                    "data_through": dataset.data_through,
                    "observed_at": dataset.observed_at,
                    "row_count": dataset.row_count,
                    "observation_mode": dataset.observation_mode,
                    "historical_pit_eligible": dataset.historical_pit_eligible,
                    "query_as_of_mode": dataset.query_as_of_mode,
                    "minimum_row_count": dataset.minimum_row_count,
                    "max_pages": dataset.max_pages,
                    "max_rows": dataset.max_rows,
                    "identity_fields": list(dataset.identity_fields),
                    "row_event_time_field": dataset.row_event_time_field,
                    "row_event_time_format": dataset.row_event_time_format,
                    "row_event_timezone": dataset.row_event_timezone,
                    "row_event_time_semantic": dataset.row_event_time_semantic,
                    "identity_sha256": dataset.identity_sha256,
                    "row_observation_sha256": dataset.row_observation_sha256,
                    "max_row_observed_at": dataset.max_row_observed_at,
                    "max_row_event_value": dataset.max_row_event_value,
                    "page_count": dataset.page_count,
                    "pagination_trace_sha256": dataset.pagination_trace_sha256,
                    "pagination_semantic_sha256": dataset.pagination_semantic_sha256,
                    "page_request_set_sha256": dataset.page_request_set_sha256,
                    "page_response_set_sha256": dataset.page_response_set_sha256,
                    "cursor_chain_sha256": dataset.cursor_chain_sha256,
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


def _source_proof_complete(envelope: QueryEnvelope) -> bool:
    lineage = envelope.metadata.lineage
    return bool(
        isinstance(lineage, Mapping)
        and lineage
        and type(lineage.get("complete")) is bool
        and lineage.get("complete") is True
        and type(lineage.get("provider_neutral")) is bool
        and lineage.get("provider_neutral") is True
        and isinstance(envelope.metadata.receipt_id, str)
        and envelope.metadata.receipt_id
        and isinstance(envelope.metadata.data_through, str)
        and envelope.metadata.data_through
        and isinstance(envelope.metadata.observed_at, str)
        and envelope.metadata.observed_at
    )


def _row_event_value(
    *,
    requirement: DatasetRequirement,
    row: Mapping[str, Any],
    dataset_id: str,
    index: int,
    observed: datetime,
    decision_instant: datetime,
) -> str | None:
    field_name = requirement.row_event_time_field
    if field_name is None:
        return None
    if field_name not in row:
        raise ResearchDataContractError(
            f"row_event_field_missing:{dataset_id}:{index}"
        )
    raw = row[field_name]
    if requirement.row_event_time_format == "iso8601":
        event_instant = _aware_instant(
            raw,
            field_name=f"row.{field_name}:{dataset_id}:{index}",
        )
        if requirement.row_event_time_semantic == "session" and event_instant > observed:
            raise ResearchDataContractError(
                f"row_event_after_observation:{dataset_id}:{index}"
            )
        if requirement.row_event_time_semantic == "session" and event_instant > decision_instant:
            raise ResearchDataContractError(
                f"row_event_after_decision:{dataset_id}:{index}"
            )
        return event_instant.isoformat()

    text = _nonempty_string(raw, field_name=f"row.{field_name}:{dataset_id}:{index}")
    if not re.fullmatch(r"[0-9]{8}", text):
        raise ResearchDataContractError(
            f"row_event_date_invalid:{dataset_id}:{index}"
        )
    try:
        event_date = datetime.strptime(text, "%Y%m%d").date()
    except ValueError as exc:
        raise ResearchDataContractError(
            f"row_event_date_invalid:{dataset_id}:{index}"
        ) from exc
    assert requirement.row_event_timezone is not None
    event_zone = ZoneInfo(requirement.row_event_timezone)
    if (
        requirement.row_event_time_semantic == "session"
        and event_date > observed.astimezone(event_zone).date()
    ):
        raise ResearchDataContractError(
            f"row_event_after_observation:{dataset_id}:{index}"
        )
    if (
        requirement.row_event_time_semantic == "session"
        and event_date > decision_instant.astimezone(event_zone).date()
    ):
        raise ResearchDataContractError(
            f"row_event_after_decision:{dataset_id}:{index}"
        )
    return event_date.isoformat()


def _dataset_snapshot(
    *,
    requirement: DatasetRequirement,
    page_run: PagedQueryRun,
    decision: EvidenceDecision,
    decision_instant: datetime,
) -> ResearchDatasetSnapshot:
    if not isinstance(page_run, PagedQueryRun):
        raise ResearchDataContractError(
            "page_runs must contain PagedQueryRun values"
        )
    envelope = page_run.envelope
    try:
        page_run.verify_integrity(identity_fields=requirement.identity_fields)
    except PaginationContractError as exc:
        raise ResearchDataContractError(
            f"pagination_trace_mismatch:{requirement.dataset_id}"
        ) from exc
    if not isinstance(envelope, QueryEnvelope):
        raise ResearchDataContractError("envelopes must contain QueryEnvelope values")
    _validate_decision(decision)
    if envelope.dataset_id != decision.dataset_id:
        raise ResearchDataContractError(
            f"decision_dataset_mismatch:{requirement.dataset_id}"
        )
    if envelope.metadata.receipt_id != decision.receipt_id:
        raise ResearchDataContractError(f"receipt_mismatch:{requirement.dataset_id}")

    source_proof_complete = _source_proof_complete(envelope)
    if not source_proof_complete and decision.action is not EvidenceAction.REJECT:
        raise ResearchDataContractError(
            f"incomplete_source_proof_must_reject:{requirement.dataset_id}"
        )
    lineage_sha256 = (
        hashlib.sha256(
            _canonical_json(
                envelope.metadata.lineage,
                field_name="metadata.lineage",
            ).encode("utf-8")
        ).hexdigest()
        if source_proof_complete
        else None
    )
    source_proof_sha256 = (
        hashlib.sha256(
            _canonical_json(
                {
                    "dataset_id": envelope.dataset_id,
                    "catalog_version": envelope.catalog_version,
                    "receipt_id": envelope.metadata.receipt_id,
                    "lineage_sha256": lineage_sha256,
                    "data_through": _normalized_instant(
                        envelope.metadata.data_through,
                        field_name="metadata.data_through",
                    ),
                    "observed_at": _normalized_instant(
                        envelope.metadata.observed_at,
                        field_name="metadata.observed_at",
                    ),
                },
                field_name="source proof",
            ).encode("utf-8")
        ).hexdigest()
        if source_proof_complete
        else None
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
    if data_through is not None and observed is not None and data_through > observed:
        raise ResearchDataContractError(
            f"data_through_after_observation:{requirement.dataset_id}"
        )

    reported_rows = list(envelope.data)
    rows = reported_rows if source_proof_complete else []
    if envelope.next_cursor is not None:
        raise ResearchDataContractError(
            f"pagination_incomplete:{requirement.dataset_id}"
        )
    if source_proof_complete and len(rows) < requirement.minimum_row_count:
        raise ResearchDataContractError(
            f"row_count_below_minimum:{requirement.dataset_id}"
        )
    row_observations: list[dict[str, Any]] = []
    row_identities: list[dict[str, Any]] = []
    seen_identities: set[str] = set()
    max_row_event_value: str | None = None
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ResearchDataContractError(
                f"row_must_be_mapping:{requirement.dataset_id}:{index}"
            )
        if observed is None:  # pragma: no cover - source proof invariant
            raise ResearchDataContractError(
                f"metadata_observed_at_missing:{requirement.dataset_id}"
            )
        identity: dict[str, Any] = {}
        for field_name in requirement.identity_fields:
            if field_name not in row or row[field_name] is None:
                raise ResearchDataContractError(
                    f"row_identity_missing:{requirement.dataset_id}:{index}"
                )
            identity[field_name] = row[field_name]
        identity_json = _canonical_json(identity, field_name="row identity")
        if identity_json in seen_identities:
            raise ResearchDataContractError(
                f"duplicate_row_identity:{requirement.dataset_id}:{index}"
            )
        seen_identities.add(identity_json)
        row_identities.append(identity)
        event_value = _row_event_value(
            requirement=requirement,
            row=row,
            dataset_id=requirement.dataset_id,
            index=index,
            observed=observed,
            decision_instant=decision_instant,
        )
        if event_value is not None and (
            max_row_event_value is None or event_value > max_row_event_value
        ):
            max_row_event_value = event_value
        row_observations.append(
            {
                "identity": identity,
                "event_value": event_value,
                "observation_mode": requirement.observation_mode,
                "observed_at": observed.isoformat(),
                "envelope_receipt_id": envelope.metadata.receipt_id,
                "row_sha256": hashlib.sha256(
                    _canonical_json(row, field_name="envelope.data row").encode("utf-8")
                ).hexdigest(),
            }
        )
    rows_json = _canonical_json(rows, field_name="envelope.data")
    identity_sha256 = hashlib.sha256(
        _canonical_json(row_identities, field_name="row identities").encode("utf-8")
    ).hexdigest()
    row_observation_sha256 = hashlib.sha256(
        _canonical_json(
            row_observations,
            field_name="row observations",
        ).encode("utf-8")
    ).hexdigest()
    if (
        page_run.page_count > requirement.max_pages
        or page_run.row_count > requirement.max_rows
        or page_run.row_count != len(reported_rows)
        or (source_proof_complete and page_run.identity_sha256 != identity_sha256)
    ):
        raise ResearchDataContractError(
            f"pagination_trace_mismatch:{requirement.dataset_id}"
        )
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
        "pagination": page_run.to_receipt_payload(),
        "row_observation": {
            "row_count": len(rows),
            "minimum_row_count": requirement.minimum_row_count,
            "identity_fields": list(requirement.identity_fields),
            "identity_sha256": identity_sha256,
            "observation_mode": requirement.observation_mode,
            "historical_pit_eligible": False,
            "row_event_time_field": requirement.row_event_time_field,
            "row_event_time_format": requirement.row_event_time_format,
            "row_event_timezone": requirement.row_event_timezone,
            "row_event_time_semantic": requirement.row_event_time_semantic,
            "row_observation_sha256": row_observation_sha256,
            "max_row_observed_at": observed.isoformat() if rows and observed else None,
            "max_row_event_value": max_row_event_value,
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
        lineage_sha256=lineage_sha256,
        source_proof_sha256=source_proof_sha256,
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
        observation_mode=requirement.observation_mode,
        historical_pit_eligible=False,
        query_as_of_mode=requirement.query_as_of_mode,
        minimum_row_count=requirement.minimum_row_count,
        max_pages=requirement.max_pages,
        max_rows=requirement.max_rows,
        identity_fields=requirement.identity_fields,
        row_event_time_field=requirement.row_event_time_field,
        row_event_time_format=requirement.row_event_time_format,
        row_event_timezone=requirement.row_event_timezone,
        row_event_time_semantic=requirement.row_event_time_semantic,
        identity_sha256=identity_sha256,
        row_observation_sha256=row_observation_sha256,
        max_row_observed_at=observed.isoformat() if rows and observed else None,
        max_row_event_value=max_row_event_value,
        page_count=page_run.page_count,
        pagination_trace_sha256=page_run.pagination_trace_sha256,
        pagination_semantic_sha256=page_run.semantic_trace_sha256,
        page_request_set_sha256=hashlib.sha256(
            _canonical_json(
                list(page_run.page_request_sha256s),
                field_name="page request hashes",
            ).encode("utf-8")
        ).hexdigest(),
        page_response_set_sha256=hashlib.sha256(
            _canonical_json(
                list(page_run.page_response_sha256s),
                field_name="page response hashes",
            ).encode("utf-8")
        ).hexdigest(),
        cursor_chain_sha256=page_run.cursor_chain_sha256,
        response_sha256=response_sha256,
        _rows_json=rows_json,
    )


def build_research_data_snapshot(
    *,
    profile: ResearchDataProfile,
    page_runs: tuple[PagedQueryRun, ...],
    decisions: tuple[EvidenceDecision, ...],
    decision_as_of: datetime,
) -> ResearchDataSnapshot:
    """Bind an exact catalog/dataset/receipt set to one PIT decision instant."""

    if not isinstance(profile, ResearchDataProfile):
        raise ResearchDataContractError("profile must be ResearchDataProfile")
    decision_instant = _aware_instant(decision_as_of, field_name="decision_as_of")
    page_run_by_id = _unique_by_dataset(page_runs, kind="page_run")
    decision_by_id = _unique_by_dataset(decisions, kind="decision")
    expected = set(profile.dataset_ids)
    if set(page_run_by_id) != expected:
        raise ResearchDataContractError("page_run_dataset_set_mismatch")
    if set(decision_by_id) != expected:
        raise ResearchDataContractError("decision_dataset_set_mismatch")

    snapshots: list[ResearchDatasetSnapshot] = []
    blocking_reasons: list[str] = []
    for requirement in profile.requirements:
        page_run = page_run_by_id[requirement.dataset_id]
        if not isinstance(page_run, PagedQueryRun):
            raise ResearchDataContractError(
                "page_runs must contain PagedQueryRun values"
            )
        envelope = page_run.envelope
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
            page_run=page_run,
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
        "profile_contract_sha256": profile.contract_sha256,
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
        profile_contract_sha256=profile.contract_sha256,
        catalog_version=profile.catalog_version,
        decision_as_of=decision_as_of_text,
        datasets=tuple(snapshots),
        execution_eligible=not blocking_reasons,
        historical_pit_eligible=False,
        blocking_reasons=tuple(blocking_reasons),
        snapshot_sha256=hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest(),
    )
