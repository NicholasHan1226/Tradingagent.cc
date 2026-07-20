#!/usr/bin/env python3
"""Reusable, fail-closed TradingDatas V1 integration readiness probe.

The probe is a TradingAgent consumer only.  It composes the existing strict V1
client, dataset Evidence Gate and immutable research snapshot contract.  It
never reads a TradingDatas database, calls a data provider, or falls back to a
legacy route.  A passing receipt is integration evidence, not trading or
production authority.  Public Python symbols and the immutable receipt schema
ID remain compatibility identifiers after the product rename.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import re
import sys
from typing import Any
import urllib.parse

from shared.data.evidence_gate import (
    DataEvidenceGate,
    DatasetEvidencePolicy,
    EvidenceAction,
    EvidenceDecision,
)
from shared.data.research_snapshot import (
    DatasetRequirement,
    ResearchDataContractError,
    ResearchDataProfile,
    build_research_data_snapshot,
)
from shared.data.sharedsignals_v1 import (
    HTTPTransport,
    QueryEnvelope,
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
    SharedSignalsV1Error,
)
from shared.runtime_test.sharedsignals_v1_gate import build_runtime_transport


RECEIPT_SCHEMA_ID = "tradingagent.sharedsignals.integration-readiness.v1"
PROBE_VERSION = 1
MAX_MANIFEST_BYTES = 1_048_576
_PROBE_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SECRET_VALUE_RE = re.compile(r"(?i)(?:^|[^a-z0-9])sk-[a-z0-9_-]{16,}")
_SECRET_KEY_FRAGMENTS = (
    "api_key",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)
_HARD_FAILED_STATES = frozenset(
    {"failed", "error", "invalid", "unavailable", "unobserved", "paused", "empty"}
)


class IntegrationProbeConfigurationError(ValueError):
    """Raised when an explicit probe manifest is absent or malformed."""


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise IntegrationProbeConfigurationError(
            "probe values must be canonical JSON"
        ) from exc


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _nonempty_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IntegrationProbeConfigurationError(
            f"{field_name} must be a non-empty string"
        )
    if value != value.strip():
        raise IntegrationProbeConfigurationError(
            f"{field_name} must not contain outer whitespace"
        )
    return value


def _aware_datetime(value: object, *, field_name: str) -> datetime:
    text = _nonempty_string(value, field_name=field_name)
    normalized = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise IntegrationProbeConfigurationError(
            f"{field_name} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IntegrationProbeConfigurationError(
            f"{field_name} must include a timezone offset"
        )
    return parsed


def _assert_exact_keys(
    payload: Mapping[str, Any],
    *,
    required: frozenset[str],
    optional: frozenset[str],
    field_name: str,
) -> None:
    keys = set(payload)
    missing = required.difference(keys)
    unknown = keys.difference(required | optional)
    if missing:
        raise IntegrationProbeConfigurationError(
            f"{field_name} missing required keys: {', '.join(sorted(missing))}"
        )
    if unknown:
        raise IntegrationProbeConfigurationError(
            f"{field_name} contains unknown keys: {', '.join(sorted(unknown))}"
        )


def _assert_secret_free(value: object, *, path: str = "manifest") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise IntegrationProbeConfigurationError(f"{path} keys must be strings")
            normalized = key.strip().lower()
            if any(fragment in normalized for fragment in _SECRET_KEY_FRAGMENTS):
                raise IntegrationProbeConfigurationError(
                    "probe manifest must contain identities, not secrets"
                )
            _assert_secret_free(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_secret_free(item, path=f"{path}[{index}]")
    elif isinstance(value, str) and _SECRET_VALUE_RE.search(value):
        raise IntegrationProbeConfigurationError(
            "probe manifest must not contain credential-shaped values"
        )


def _action(value: object, *, field_name: str) -> EvidenceAction:
    text = _nonempty_string(value, field_name=field_name)
    try:
        action = EvidenceAction(text)
    except ValueError as exc:
        raise IntegrationProbeConfigurationError(
            f"{field_name} must be reject or deweight"
        ) from exc
    if action is EvidenceAction.ACCEPT:
        raise IntegrationProbeConfigurationError(
            f"{field_name} cannot accept impaired evidence"
        )
    return action


@dataclass(frozen=True)
class DatasetProbeSpec:
    """One functional role and exact provider-neutral query contract."""

    probe_role: str
    dataset_id: str
    schema_major: int
    requirement_role: str
    fields: tuple[str, ...]
    filters: Mapping[str, Any]
    limit: int
    minimum_row_count: int
    row_event_time_field: str
    row_available_time_field: str
    row_revision_id_field: str
    row_receipt_id_field: str
    order: tuple[str, ...] | None = None
    degraded_action: EvidenceAction = EvidenceAction.REJECT
    stale_action: EvidenceAction = EvidenceAction.REJECT
    degraded_weight: float = 0.25
    stale_weight: float = 0.10
    _filters_json: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        role = _nonempty_string(self.probe_role, field_name="probe_role")
        if not _PROBE_ROLE_RE.fullmatch(role):
            raise IntegrationProbeConfigurationError(
                "probe_role must use canonical snake_case"
            )
        object.__setattr__(self, "probe_role", role)

        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= 10_000
        ):
            raise IntegrationProbeConfigurationError(
                "limit must be between 1 and 10000"
            )

        try:
            requirement = DatasetRequirement(
                dataset_id=self.dataset_id,
                role=self.requirement_role,
                row_event_time_field=self.row_event_time_field,
                row_available_time_field=self.row_available_time_field,
                row_revision_id_field=self.row_revision_id_field,
                row_receipt_id_field=self.row_receipt_id_field,
                minimum_row_count=self.minimum_row_count,
            )
            request = QueryRequest(
                dataset_id=self.dataset_id,
                schema_major=self.schema_major,
                fields=self.fields,
                filters=self.filters,
                order=self.order,
                limit=self.limit,
            )
            policy = DatasetEvidencePolicy(
                dataset_id=self.dataset_id,
                degraded_action=self.degraded_action,
                stale_action=self.stale_action,
                degraded_weight=self.degraded_weight,
                stale_weight=self.stale_weight,
            )
        except (TypeError, ValueError, SharedSignalsV1Error) as exc:
            raise IntegrationProbeConfigurationError(str(exc)) from exc

        pit_fields = {
            requirement.row_event_time_field,
            requirement.row_available_time_field,
            requirement.row_revision_id_field,
            requirement.row_receipt_id_field,
        }
        missing_pit_fields = pit_fields.difference(request.fields)
        if missing_pit_fields:
            raise IntegrationProbeConfigurationError(
                "fields must explicitly include every row PIT field"
            )

        object.__setattr__(self, "dataset_id", requirement.dataset_id)
        object.__setattr__(self, "requirement_role", requirement.role)
        object.__setattr__(self, "fields", request.fields)
        object.__setattr__(self, "filters", request.filters)
        object.__setattr__(self, "order", request.order)
        object.__setattr__(self, "_filters_json", _canonical_json(request.filters))
        object.__setattr__(self, "degraded_action", policy.degraded_action)
        object.__setattr__(self, "stale_action", policy.stale_action)

    def requirement(self) -> DatasetRequirement:
        return DatasetRequirement(
            dataset_id=self.dataset_id,
            role=self.requirement_role,
            row_event_time_field=self.row_event_time_field,
            row_available_time_field=self.row_available_time_field,
            row_revision_id_field=self.row_revision_id_field,
            row_receipt_id_field=self.row_receipt_id_field,
            minimum_row_count=self.minimum_row_count,
        )

    def policy(self) -> DatasetEvidencePolicy:
        return DatasetEvidencePolicy(
            dataset_id=self.dataset_id,
            degraded_action=self.degraded_action,
            stale_action=self.stale_action,
            degraded_weight=self.degraded_weight,
            stale_weight=self.stale_weight,
        )

    def query(self, *, as_of: str) -> QueryRequest:
        return QueryRequest(
            dataset_id=self.dataset_id,
            schema_major=self.schema_major,
            fields=self.fields,
            filters=json.loads(self._filters_json),
            as_of=as_of,
            order=self.order,
            limit=self.limit,
        )

    def to_manifest_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "probe_role": self.probe_role,
            "dataset_id": self.dataset_id,
            "schema_major": self.schema_major,
            "requirement_role": self.requirement_role,
            "fields": list(self.fields),
            "filters": json.loads(self._filters_json),
            "limit": self.limit,
            "minimum_row_count": self.minimum_row_count,
            "row_event_time_field": self.row_event_time_field,
            "row_available_time_field": self.row_available_time_field,
            "row_revision_id_field": self.row_revision_id_field,
            "row_receipt_id_field": self.row_receipt_id_field,
            "degraded_action": self.degraded_action.value,
            "stale_action": self.stale_action.value,
            "degraded_weight": self.degraded_weight,
            "stale_weight": self.stale_weight,
        }
        if self.order is not None:
            payload["order"] = list(self.order)
        return payload


@dataclass(frozen=True)
class SharedSignalsIntegrationProbeConfig:
    """Complete, explicit authority and profile inputs for one probe."""

    manifest_version: int
    profile_id: str
    base_url: str
    catalog_version: str
    access_policy_id: str
    transport_id: str
    timeout_seconds: float
    as_of: str
    expected_probe_roles: tuple[str, ...]
    datasets: tuple[DatasetProbeSpec, ...]

    def __post_init__(self) -> None:
        if self.manifest_version != PROBE_VERSION:
            raise IntegrationProbeConfigurationError(
                f"manifest_version must equal {PROBE_VERSION}"
            )
        profile_id = _nonempty_string(self.profile_id, field_name="profile_id")
        transport_id = _nonempty_string(
            self.transport_id,
            field_name="transport_id",
        )
        _aware_datetime(self.as_of, field_name="as_of")
        parsed_url = urllib.parse.urlsplit(self.base_url)
        if parsed_url.username is not None or parsed_url.password is not None:
            raise IntegrationProbeConfigurationError(
                "base_url must not contain user information"
            )
        if not isinstance(self.expected_probe_roles, tuple) or not (
            self.expected_probe_roles
        ):
            raise IntegrationProbeConfigurationError(
                "expected_probe_roles must be a non-empty tuple"
            )
        for role in self.expected_probe_roles:
            if not isinstance(role, str) or not _PROBE_ROLE_RE.fullmatch(role):
                raise IntegrationProbeConfigurationError(
                    "expected_probe_roles must use canonical snake_case"
                )
        if len(set(self.expected_probe_roles)) != len(self.expected_probe_roles):
            raise IntegrationProbeConfigurationError(
                "expected_probe_roles must not contain duplicates"
            )
        if not isinstance(self.datasets, tuple) or not self.datasets:
            raise IntegrationProbeConfigurationError(
                "datasets must be a non-empty tuple"
            )
        if not all(isinstance(item, DatasetProbeSpec) for item in self.datasets):
            raise IntegrationProbeConfigurationError(
                "datasets must contain DatasetProbeSpec values"
            )
        actual_roles = tuple(item.probe_role for item in self.datasets)
        if actual_roles != self.expected_probe_roles:
            raise IntegrationProbeConfigurationError(
                "dataset probe roles must exactly match expected_probe_roles order"
            )
        dataset_ids = tuple(item.dataset_id for item in self.datasets)
        if len(set(dataset_ids)) != len(dataset_ids):
            raise IntegrationProbeConfigurationError(
                "dataset IDs must not contain duplicates"
            )
        try:
            self.to_client_config()
            self.to_profile()
        except (TypeError, ValueError, SharedSignalsV1Error) as exc:
            raise IntegrationProbeConfigurationError(str(exc)) from exc

        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "transport_id", transport_id)

    def to_client_config(self) -> SharedSignalsV1Config:
        return SharedSignalsV1Config(
            base_url=self.base_url,
            expected_catalog_version=self.catalog_version,
            dataset_ids=frozenset(item.dataset_id for item in self.datasets),
            access_policy_id=self.access_policy_id,
            timeout_seconds=self.timeout_seconds,
            max_limit=10_000,
            cache_ttl_seconds=0,
        )

    def to_profile(self) -> ResearchDataProfile:
        return ResearchDataProfile(
            profile_id=self.profile_id,
            catalog_version=self.catalog_version,
            requirements=tuple(item.requirement() for item in self.datasets),
        )

    def to_manifest_payload(self) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "profile_id": self.profile_id,
            "base_url": self.to_client_config().base_url,
            "catalog_version": self.catalog_version,
            "access_policy_id": self.access_policy_id,
            "transport_id": self.transport_id,
            "timeout_seconds": self.timeout_seconds,
            "as_of": self.as_of,
            "expected_probe_roles": list(self.expected_probe_roles),
            "datasets": [item.to_manifest_payload() for item in self.datasets],
        }

    @property
    def manifest_sha256(self) -> str:
        return _sha256(self.to_manifest_payload())

    @property
    def authority_sha256(self) -> str:
        config = self.to_client_config()
        return _sha256(
            {
                "base_url": config.base_url,
                "access_policy_id": config.access_policy_id,
                "transport_id": self.transport_id,
            }
        )


_ROOT_REQUIRED_KEYS = frozenset(
    {
        "manifest_version",
        "profile_id",
        "base_url",
        "catalog_version",
        "access_policy_id",
        "transport_id",
        "timeout_seconds",
        "as_of",
        "expected_probe_roles",
        "datasets",
    }
)
_DATASET_REQUIRED_KEYS = frozenset(
    {
        "probe_role",
        "dataset_id",
        "schema_major",
        "requirement_role",
        "fields",
        "filters",
        "limit",
        "minimum_row_count",
        "row_event_time_field",
        "row_available_time_field",
        "row_revision_id_field",
        "row_receipt_id_field",
    }
)
_DATASET_OPTIONAL_KEYS = frozenset(
    {
        "order",
        "degraded_action",
        "stale_action",
        "degraded_weight",
        "stale_weight",
    }
)


def _dataset_spec(payload: object, *, index: int) -> DatasetProbeSpec:
    if not isinstance(payload, Mapping):
        raise IntegrationProbeConfigurationError(f"datasets[{index}] must be an object")
    _assert_exact_keys(
        payload,
        required=_DATASET_REQUIRED_KEYS,
        optional=_DATASET_OPTIONAL_KEYS,
        field_name=f"datasets[{index}]",
    )
    raw_fields = payload["fields"]
    raw_order = payload.get("order")
    if not isinstance(raw_fields, list):
        raise IntegrationProbeConfigurationError(
            f"datasets[{index}].fields must be a list"
        )
    if raw_order is not None and not isinstance(raw_order, list):
        raise IntegrationProbeConfigurationError(
            f"datasets[{index}].order must be a list when provided"
        )
    return DatasetProbeSpec(
        probe_role=payload["probe_role"],
        dataset_id=payload["dataset_id"],
        schema_major=payload["schema_major"],
        requirement_role=payload["requirement_role"],
        fields=tuple(raw_fields),
        filters=payload["filters"],
        order=None if raw_order is None else tuple(raw_order),
        limit=payload["limit"],
        minimum_row_count=payload["minimum_row_count"],
        row_event_time_field=payload["row_event_time_field"],
        row_available_time_field=payload["row_available_time_field"],
        row_revision_id_field=payload["row_revision_id_field"],
        row_receipt_id_field=payload["row_receipt_id_field"],
        degraded_action=_action(
            payload.get("degraded_action", "reject"),
            field_name=f"datasets[{index}].degraded_action",
        ),
        stale_action=_action(
            payload.get("stale_action", "reject"),
            field_name=f"datasets[{index}].stale_action",
        ),
        degraded_weight=payload.get("degraded_weight", 0.25),
        stale_weight=payload.get("stale_weight", 0.10),
    )


def load_probe_manifest(path: Path) -> SharedSignalsIntegrationProbeConfig:
    """Load one explicit, secret-free integration profile from an absolute path."""

    if not isinstance(path, Path) or not path.is_absolute():
        raise IntegrationProbeConfigurationError("manifest path must be absolute")
    if path.is_symlink():
        raise IntegrationProbeConfigurationError("manifest path must not be a symlink")
    try:
        stat = path.stat()
    except OSError as exc:
        raise IntegrationProbeConfigurationError(
            "manifest file is unavailable"
        ) from exc
    if not path.is_file():
        raise IntegrationProbeConfigurationError("manifest path must be a file")
    if stat.st_size <= 0 or stat.st_size > MAX_MANIFEST_BYTES:
        raise IntegrationProbeConfigurationError(
            "manifest file must be between 1 byte and 1 MiB"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrationProbeConfigurationError("manifest must be UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise IntegrationProbeConfigurationError("manifest root must be an object")
    _assert_secret_free(payload)
    _assert_exact_keys(
        payload,
        required=_ROOT_REQUIRED_KEYS,
        optional=frozenset(),
        field_name="manifest",
    )
    raw_roles = payload["expected_probe_roles"]
    raw_datasets = payload["datasets"]
    if not isinstance(raw_roles, list):
        raise IntegrationProbeConfigurationError("expected_probe_roles must be a list")
    if not isinstance(raw_datasets, list):
        raise IntegrationProbeConfigurationError("datasets must be a list")
    return SharedSignalsIntegrationProbeConfig(
        manifest_version=payload["manifest_version"],
        profile_id=payload["profile_id"],
        base_url=payload["base_url"],
        catalog_version=payload["catalog_version"],
        access_policy_id=payload["access_policy_id"],
        transport_id=payload["transport_id"],
        timeout_seconds=payload["timeout_seconds"],
        as_of=payload["as_of"],
        expected_probe_roles=tuple(raw_roles),
        datasets=tuple(
            _dataset_spec(item, index=index) for index, item in enumerate(raw_datasets)
        ),
    )


def _semantic_envelope_payload(envelope: QueryEnvelope) -> dict[str, Any]:
    return {
        "api_version": envelope.api_version,
        "catalog_version": envelope.catalog_version,
        "dataset_id": envelope.dataset_id,
        "data": list(envelope.data),
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
    }


def _source_proof_complete(envelope: QueryEnvelope) -> bool:
    metadata = envelope.metadata
    return bool(
        isinstance(metadata.lineage, Mapping)
        and metadata.lineage
        and isinstance(metadata.receipt_id, str)
        and metadata.receipt_id
        and isinstance(metadata.data_through, str)
        and metadata.data_through
        and isinstance(metadata.observed_at, str)
        and metadata.observed_at
    )


def _controlled_evidence_reason_code(
    envelope: QueryEnvelope,
    decision: EvidenceDecision,
) -> str | None:
    """Derive a local code without trusting provider-supplied reason text."""

    if not _source_proof_complete(envelope):
        top_state = envelope.metadata.state.strip().lower()
        return (
            "dataset_failed"
            if top_state in _HARD_FAILED_STATES
            else "dataset_evidence_incomplete"
        )
    if decision.effective_state == "degraded":
        return "dataset_degraded"
    if decision.effective_state == "stale":
        return "dataset_stale"
    if decision.effective_state == "failed":
        return "dataset_failed"
    if decision.effective_state == "unknown":
        return "dataset_state_unknown"
    return None


def _append_reason(reasons: list[str], value: str) -> None:
    if value and value not in reasons:
        reasons.append(value)


def _error_type(error: Exception) -> str:
    name = type(error).__name__
    return name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", name) else "Exception"


def _controlled_contract_code(error: ResearchDataContractError) -> str:
    text = str(error)
    prefix = text.split(":", 1)[0]
    if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", prefix):
        return prefix
    return "research_data_contract_error"


def _base_receipt(config: SharedSignalsIntegrationProbeConfig) -> dict[str, Any]:
    return {
        "schema_id": RECEIPT_SCHEMA_ID,
        "probe_version": PROBE_VERSION,
        "authority": "non_authority",
        "production_verified": False,
        "real_trading_enabled": False,
        "profile_id": config.profile_id,
        "as_of": config.as_of,
        "catalog_version": config.catalog_version,
        "transport_id": config.transport_id,
        "manifest_sha256": config.manifest_sha256,
        "authority_sha256": config.authority_sha256,
        "status": "fail",
        "blocking": True,
        "reason_codes": [],
        "error_type": None,
        "catalog": None,
        "datasets": [],
        "same_as_of_match": False,
        "snapshot_runs": [],
        "semantic_snapshot_sha256": None,
    }


def _finalize_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256", None)
    receipt["receipt_sha256"] = _sha256(unsigned)
    return receipt


def _dataset_receipt(
    *,
    spec: DatasetProbeSpec,
    request: QueryRequest,
    first: QueryEnvelope,
    second: QueryEnvelope,
    first_decision: EvidenceDecision,
    second_decision: EvidenceDecision,
) -> dict[str, Any]:
    first_semantic = _sha256(_semantic_envelope_payload(first))
    second_semantic = _sha256(_semantic_envelope_payload(second))
    reasons: list[str] = []
    for envelope, decision in (
        (first, first_decision),
        (second, second_decision),
    ):
        reason = _controlled_evidence_reason_code(envelope, decision)
        if reason is not None:
            _append_reason(reasons, reason)
    missing_fields = sorted(
        {
            field_name
            for envelope in (first, second)
            for row in envelope.data
            for field_name in spec.fields
            if field_name not in row
        }
    )
    if missing_fields:
        _append_reason(reasons, "requested_field_missing")
    requested_fields = frozenset(spec.fields)
    unexpected_fields = sorted(
        {
            field_name
            for envelope in (first, second)
            for row in envelope.data
            for field_name in row
            if field_name not in requested_fields
        }
    )
    if unexpected_fields:
        _append_reason(reasons, "undeclared_field_present")
    pagination_complete = first.next_cursor is None and second.next_cursor is None
    if not pagination_complete:
        _append_reason(reasons, "pagination_contract_unfrozen")
    same_as_of_match = first_semantic == second_semantic
    if not same_as_of_match:
        _append_reason(reasons, "same_as_of_semantic_mismatch")
    if not first_decision.eligible or not second_decision.eligible:
        _append_reason(reasons, "dataset_evidence_rejected")

    lineage = first.metadata.lineage
    return {
        "probe_role": spec.probe_role,
        "dataset_id": spec.dataset_id,
        "schema_major": spec.schema_major,
        "requirement_role": spec.requirement_role,
        "query_sha256": request.sha256,
        "request_ids": [first.request_id, second.request_id],
        "state": first.metadata.state,
        "degraded": first.metadata.degraded,
        "freshness_state": first.metadata.freshness.get("state"),
        "quality_state": first.metadata.quality.get("state"),
        "lineage_state": (
            lineage.get("state") if isinstance(lineage, Mapping) else None
        ),
        "freshness_sha256": _sha256(first.metadata.freshness),
        "quality_sha256": _sha256(first.metadata.quality),
        "lineage_sha256": None if lineage is None else _sha256(lineage),
        "receipt_id": first.metadata.receipt_id,
        "data_through": first.metadata.data_through,
        "observed_at": first.metadata.observed_at,
        "source_proof_complete": _source_proof_complete(first),
        "evidence_action": first_decision.action.value,
        "effective_state": first_decision.effective_state,
        "eligible": first_decision.eligible,
        "effective_weight": first_decision.weight,
        "evidence_reasons_sha256": _sha256(
            [list(first_decision.reasons), list(second_decision.reasons)]
        ),
        "row_count": len(first.data),
        "requested_fields_sha256": _sha256(list(spec.fields)),
        "missing_requested_fields": missing_fields,
        "unexpected_field_count": len(unexpected_fields),
        "unexpected_fields_sha256": _sha256(unexpected_fields),
        "pagination_complete": pagination_complete,
        "same_as_of_match": same_as_of_match,
        "semantic_response_sha256": first_semantic,
        "reason_codes": reasons,
    }


def run_sharedsignals_integration_probe(
    config: SharedSignalsIntegrationProbeConfig,
    *,
    transport: HTTPTransport | None,
) -> dict[str, Any]:
    """Run catalog plus two identical as-of reads for every profile dataset."""

    if not isinstance(config, SharedSignalsIntegrationProbeConfig):
        raise TypeError("config must be SharedSignalsIntegrationProbeConfig")
    receipt = _base_receipt(config)
    reasons: list[str] = receipt["reason_codes"]
    if transport is None:
        reasons.append("transport_not_configured")
        return _finalize_receipt(receipt)

    client = SharedSignalsV1Client(config.to_client_config(), transport=transport)
    try:
        catalog = client.get_catalog()
    except Exception as exc:
        reasons.append("catalog_contract_or_transport_failure")
        receipt["error_type"] = _error_type(exc)
        return _finalize_receipt(receipt)

    catalog_payload = {
        "api_version": catalog.api_version,
        "catalog_version": catalog.catalog_version,
        "data": list(catalog.data),
    }
    receipt["catalog"] = {
        "request_id": catalog.request_id,
        "catalog_sha256": _sha256(catalog_payload),
        "dataset_count": len(catalog.data),
    }

    gate = DataEvidenceGate(
        {spec.dataset_id: spec.policy() for spec in config.datasets}
    )
    first_envelopes: list[QueryEnvelope] = []
    second_envelopes: list[QueryEnvelope] = []
    first_decisions: list[EvidenceDecision] = []
    second_decisions: list[EvidenceDecision] = []

    for spec in config.datasets:
        request = spec.query(as_of=config.as_of)
        try:
            first = client.query(request)
            second = client.query(request)
        except Exception as exc:
            _append_reason(reasons, "query_contract_or_transport_failure")
            if receipt["error_type"] is None:
                receipt["error_type"] = _error_type(exc)
            receipt["datasets"].append(
                {
                    "probe_role": spec.probe_role,
                    "dataset_id": spec.dataset_id,
                    "schema_major": spec.schema_major,
                    "query_sha256": request.sha256,
                    "status": "fail",
                    "reason_codes": ["query_contract_or_transport_failure"],
                }
            )
            continue

        first_decision = gate.evaluate(first)
        second_decision = gate.evaluate(second)
        dataset_result = _dataset_receipt(
            spec=spec,
            request=request,
            first=first,
            second=second,
            first_decision=first_decision,
            second_decision=second_decision,
        )
        receipt["datasets"].append(dataset_result)
        for reason in dataset_result["reason_codes"]:
            if reason in {
                "requested_field_missing",
                "undeclared_field_present",
                "pagination_contract_unfrozen",
                "same_as_of_semantic_mismatch",
                "dataset_evidence_rejected",
            }:
                _append_reason(reasons, reason)
        first_envelopes.append(first)
        second_envelopes.append(second)
        first_decisions.append(first_decision)
        second_decisions.append(second_decision)

    complete_dataset_set = len(first_envelopes) == len(config.datasets)
    if complete_dataset_set:
        try:
            snapshots = (
                build_research_data_snapshot(
                    profile=config.to_profile(),
                    envelopes=tuple(first_envelopes),
                    decisions=tuple(first_decisions),
                    decision_as_of=_aware_datetime(config.as_of, field_name="as_of"),
                ),
                build_research_data_snapshot(
                    profile=config.to_profile(),
                    envelopes=tuple(second_envelopes),
                    decisions=tuple(second_decisions),
                    decision_as_of=_aware_datetime(config.as_of, field_name="as_of"),
                ),
            )
        except ResearchDataContractError as exc:
            _append_reason(reasons, "research_snapshot_contract_failure")
            receipt["snapshot_error_code"] = _controlled_contract_code(exc)
        else:
            receipt["snapshot_runs"] = [
                {
                    "snapshot_sha256": snapshot.snapshot_sha256,
                    "execution_eligible": snapshot.execution_eligible,
                    "blocking_reasons": list(snapshot.blocking_reasons),
                }
                for snapshot in snapshots
            ]
            for snapshot in snapshots:
                for reason in snapshot.blocking_reasons:
                    _append_reason(reasons, reason)
            semantic_snapshots = [
                _sha256(
                    {
                        "profile_id": config.profile_id,
                        "catalog_version": config.catalog_version,
                        "as_of": config.as_of,
                        "datasets": [
                            _sha256(_semantic_envelope_payload(envelope))
                            for envelope in envelopes
                        ],
                    }
                )
                for envelopes in (first_envelopes, second_envelopes)
            ]
            receipt["semantic_snapshot_sha256"] = semantic_snapshots[0]
            if semantic_snapshots[0] != semantic_snapshots[1]:
                _append_reason(reasons, "same_as_of_semantic_mismatch")

    dataset_matches = [
        item.get("same_as_of_match", False) for item in receipt["datasets"]
    ]
    receipt["same_as_of_match"] = bool(dataset_matches) and all(dataset_matches)
    blocking = bool(reasons)
    receipt["blocking"] = blocking
    receipt["status"] = "fail" if blocking else "pass"
    return _finalize_receipt(receipt)


def write_probe_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    """Atomically persist one verified receipt with private file permissions."""

    if not isinstance(path, Path) or not path.is_absolute():
        raise IntegrationProbeConfigurationError("output path must be absolute")
    if path.exists() and path.is_symlink():
        raise IntegrationProbeConfigurationError("output path must not be a symlink")
    if not isinstance(receipt, Mapping):
        raise IntegrationProbeConfigurationError("receipt must be a mapping")
    payload = dict(receipt)
    claimed = payload.pop("receipt_sha256", None)
    if not isinstance(claimed, str) or claimed != _sha256(payload):
        raise IntegrationProbeConfigurationError("receipt hash is invalid")
    encoded = (
        json.dumps(
            dict(receipt),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise IntegrationProbeConfigurationError("temporary output path is occupied")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise IntegrationProbeConfigurationError(
            "probe receipt could not be persisted"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _configuration_failure_receipt(error: Exception) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "schema_id": RECEIPT_SCHEMA_ID,
        "probe_version": PROBE_VERSION,
        "authority": "non_authority",
        "production_verified": False,
        "real_trading_enabled": False,
        "status": "fail",
        "blocking": True,
        "reason_codes": ["invalid_probe_manifest"],
        "error_type": _error_type(error),
        "datasets": [],
    }
    return _finalize_receipt(receipt)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed TradingDatas V1 integration readiness probe"
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        config = load_probe_manifest(args.manifest)
        transport = build_runtime_transport(config.transport_id)
    except (IntegrationProbeConfigurationError, ValueError) as exc:
        receipt = _configuration_failure_receipt(exc)
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 64

    receipt = run_sharedsignals_integration_probe(config, transport=transport)
    if args.output is not None:
        try:
            write_probe_receipt(args.output, receipt)
        except IntegrationProbeConfigurationError as exc:
            failure = _configuration_failure_receipt(exc)
            print(json.dumps(failure, ensure_ascii=False, sort_keys=True))
            return 74
    if args.json:
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"{receipt['status']} blocking={receipt['blocking']} "
            f"receipt_sha256={receipt['receipt_sha256']}"
        )
    return 2 if receipt["blocking"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))


__all__ = [
    "DatasetProbeSpec",
    "IntegrationProbeConfigurationError",
    "SharedSignalsIntegrationProbeConfig",
    "load_probe_manifest",
    "main",
    "run_sharedsignals_integration_probe",
    "write_probe_receipt",
]
