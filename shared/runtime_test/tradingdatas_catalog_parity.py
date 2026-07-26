#!/usr/bin/env python3
"""Catalog-driven, bounded TradingDatas parity acceptance for TradingAgent.

This consumer-side checker discovers the current active dataset set from the
only supported catalog endpoint and requires an external, secret-free manifest
to describe that exact set.  It then performs two complete bounded reads per
active dataset.  Declared impaired datasets must remain rejected with zero
weight; they are accounted for by the transport contract but never promoted to
research evidence.

The checker has no database, provider, legacy route, or localhost/file fallback.
Its receipt contains hashes and controlled reason codes, never rows, cursors,
headers, endpoint locations, credentials, or provider-supplied reason text.
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
from shared.data.research_snapshot import DatasetRequirement, ResearchDataContractError
from shared.data.sharedsignals_v1 import (
    HTTPStatusError,
    HTTPTransport,
    QueryEnvelope,
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
    SharedSignalsV1Error,
)
from shared.data.tradingdatas_pagination import (
    IdentitylessSinglePageRun,
    PagedQueryRun,
    PaginationContractError,
    collect_identityless_single_page,
    collect_query_pages,
)
from shared.data.tradingdatas_transport import (
    RuntimeGateConfigurationError,
    build_runtime_transport,
)


RECEIPT_SCHEMA_ID = "tradingagent.tradingdatas.catalog-parity.v1"
MANIFEST_VERSION = 1
MAX_MANIFEST_BYTES = 1_048_576
_EXPECTED_HEALTH = frozenset({"ready", "impaired"})
_SECRET_KEY_FRAGMENTS = (
    "api_key",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)
_SECRET_VALUE_RE = re.compile(r"(?i)(?:^|[^a-z0-9])sk-[a-z0-9_-]{16,}")


class CatalogParityConfigurationError(ValueError):
    """The external parity manifest is absent, unsafe, or malformed."""


class CatalogParityContractError(SharedSignalsV1Error):
    """The live catalog does not match the explicit external manifest."""


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
        raise CatalogParityConfigurationError(
            "catalog parity values must be canonical JSON"
        ) from exc


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def receipt_sha256(receipt: Mapping[str, Any]) -> str:
    """Return the content hash of a receipt, excluding its claimed hash."""

    if not isinstance(receipt, Mapping):
        raise TypeError("receipt must be a mapping")
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256", None)
    return _sha256(unsigned)


def _nonempty_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogParityConfigurationError(
            f"{field_name} must be a non-empty string"
        )
    if value != value.strip():
        raise CatalogParityConfigurationError(
            f"{field_name} must not contain outer whitespace"
        )
    return value


def _aware_timestamp(value: object, *, field_name: str) -> str:
    text = _nonempty_string(value, field_name=field_name)
    normalized = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise CatalogParityConfigurationError(
            f"{field_name} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CatalogParityConfigurationError(
            f"{field_name} must include a timezone offset"
        )
    return text


def _native_count(value: object, *, field_name: str, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        qualifier = "positive" if positive else "non-negative"
        raise CatalogParityConfigurationError(
            f"{field_name} must be a {qualifier} integer"
        )
    return value


def _exact_keys(
    payload: Mapping[str, Any],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
    field_name: str,
) -> None:
    missing = required.difference(payload)
    unknown = set(payload).difference(required | optional)
    if missing:
        raise CatalogParityConfigurationError(
            f"{field_name} missing required keys: {', '.join(sorted(missing))}"
        )
    if unknown:
        raise CatalogParityConfigurationError(
            f"{field_name} contains unknown keys: {', '.join(sorted(unknown))}"
        )


def _secret_free(value: object, *, path: str = "manifest") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CatalogParityConfigurationError(f"{path} keys must be strings")
            normalized = key.strip().lower()
            if any(fragment in normalized for fragment in _SECRET_KEY_FRAGMENTS):
                raise CatalogParityConfigurationError(
                    "catalog parity manifest must contain identities, not secrets"
                )
            _secret_free(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _secret_free(item, path=f"{path}[{index}]")
    elif isinstance(value, str) and _SECRET_VALUE_RE.search(value):
        raise CatalogParityConfigurationError(
            "catalog parity manifest must not contain credential-shaped values"
        )


def _string_tuple(value: object, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise CatalogParityConfigurationError(f"{field_name} must be a non-empty list")
    result: list[str] = []
    for item in value:
        normalized = _nonempty_string(item, field_name=f"{field_name} item")
        if normalized in result:
            raise CatalogParityConfigurationError(
                f"{field_name} must not contain duplicates"
            )
        result.append(normalized)
    return tuple(result)


def _identity_fields_tuple(value: object, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CatalogParityConfigurationError(f"{field_name} must be a list")
    if not value:
        return ()
    return _string_tuple(value, field_name=field_name)


def _mapping(
    value: object, *, field_name: str, nonempty: bool = False
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or (nonempty and not value):
        qualifier = "non-empty " if nonempty else ""
        raise CatalogParityConfigurationError(
            f"{field_name} must be a {qualifier}mapping"
        )
    try:
        return json.loads(_canonical_json(value))
    except json.JSONDecodeError as exc:  # pragma: no cover - canonical JSON is valid
        raise CatalogParityConfigurationError(f"{field_name} is invalid") from exc


@dataclass(frozen=True)
class ObservationMapping:
    mode: str
    query_as_of_mode: str
    row_event_time_field: str | None = None
    row_event_time_format: str | None = None
    row_event_timezone: str | None = None
    row_event_time_semantic: str | None = None

    def __post_init__(self) -> None:
        try:
            requirement = DatasetRequirement(
                dataset_id="fixture.validation.observation",
                role="optional_context",
                identity_fields=("identity",),
                observation_mode=self.mode,
                query_as_of_mode=self.query_as_of_mode,
                row_event_time_field=self.row_event_time_field,
                row_event_time_format=self.row_event_time_format,
                row_event_timezone=self.row_event_timezone,
                row_event_time_semantic=self.row_event_time_semantic,
                minimum_row_count=0,
                max_pages=1,
                max_rows=1,
            )
        except ResearchDataContractError as exc:
            raise CatalogParityConfigurationError(str(exc)) from exc
        object.__setattr__(self, "mode", requirement.observation_mode)
        object.__setattr__(
            self,
            "query_as_of_mode",
            requirement.query_as_of_mode,
        )
        object.__setattr__(
            self,
            "row_event_time_field",
            requirement.row_event_time_field,
        )
        object.__setattr__(
            self,
            "row_event_time_format",
            requirement.row_event_time_format,
        )
        object.__setattr__(
            self,
            "row_event_timezone",
            requirement.row_event_timezone,
        )
        object.__setattr__(
            self,
            "row_event_time_semantic",
            requirement.row_event_time_semantic,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "query_as_of_mode": self.query_as_of_mode,
            "row_event_time_field": self.row_event_time_field,
            "row_event_time_format": self.row_event_time_format,
            "row_event_timezone": self.row_event_timezone,
            "row_event_time_semantic": self.row_event_time_semantic,
        }


@dataclass(frozen=True)
class CatalogDatasetSpec:
    dataset_id: str
    expected_health: str
    schema_major: int
    catalog_default_fields: tuple[str, ...]
    catalog_limits: Mapping[str, Any]
    filters: Mapping[str, Any]
    query_limit: int
    minimum_row_count: int
    identity_fields: tuple[str, ...]
    observation: ObservationMapping
    max_pages: int
    max_rows: int
    _limits_json: str = field(init=False, repr=False)
    _filters_json: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        expected_health = _nonempty_string(
            self.expected_health,
            field_name="expected_health",
        )
        if expected_health not in _EXPECTED_HEALTH:
            raise CatalogParityConfigurationError(
                "expected_health must be ready or impaired"
            )
        try:
            request = QueryRequest(
                dataset_id=self.dataset_id,
                schema_major=self.schema_major,
                fields=self.catalog_default_fields,
                filters=self.filters,
                limit=self.query_limit,
            )
        except SharedSignalsV1Error as exc:
            raise CatalogParityConfigurationError(str(exc)) from exc
        if not isinstance(self.identity_fields, tuple):
            raise CatalogParityConfigurationError("identity_fields must be a tuple")

        if self.identity_fields:
            try:
                requirement = DatasetRequirement(
                    dataset_id=self.dataset_id,
                    role="optional_context",
                    identity_fields=self.identity_fields,
                    observation_mode=self.observation.mode,
                    query_as_of_mode=self.observation.query_as_of_mode,
                    row_event_time_field=self.observation.row_event_time_field,
                    row_event_time_format=self.observation.row_event_time_format,
                    row_event_timezone=self.observation.row_event_timezone,
                    row_event_time_semantic=self.observation.row_event_time_semantic,
                    minimum_row_count=self.minimum_row_count,
                    max_pages=self.max_pages,
                    max_rows=self.max_rows,
                )
            except ResearchDataContractError as exc:
                raise CatalogParityConfigurationError(str(exc)) from exc
            identity_fields = requirement.identity_fields
            minimum_row_count = requirement.minimum_row_count
            max_pages = requirement.max_pages
            max_rows = requirement.max_rows
        else:
            if expected_health != "impaired":
                raise CatalogParityConfigurationError(
                    "identityless datasets must be declared impaired"
                )
            if type(self.max_pages) is not int or self.max_pages != 1:
                raise CatalogParityConfigurationError(
                    "identityless datasets require max_pages equal to 1"
                )
            if type(self.minimum_row_count) is not int or self.minimum_row_count != 0:
                raise CatalogParityConfigurationError(
                    "identityless datasets require minimum_row_count equal to 0"
                )
            if type(self.max_rows) is not int or self.max_rows < 0:
                raise CatalogParityConfigurationError(
                    "identityless max_rows must be a non-negative integer"
                )
            identity_fields = ()
            minimum_row_count = 0
            max_pages = 1
            max_rows = self.max_rows

        required_fields = set(identity_fields)
        if self.observation.row_event_time_field is not None:
            required_fields.add(self.observation.row_event_time_field)
        if required_fields.difference(request.fields):
            raise CatalogParityConfigurationError(
                "catalog_default_fields must include identity and row event fields"
            )
        if expected_health == "ready" and minimum_row_count == 0:
            raise CatalogParityConfigurationError(
                "ready datasets require a positive minimum_row_count"
            )
        limits = _mapping(
            self.catalog_limits,
            field_name="catalog_limits",
            nonempty=True,
        )
        object.__setattr__(self, "dataset_id", request.dataset_id)
        object.__setattr__(self, "expected_health", expected_health)
        object.__setattr__(self, "schema_major", request.schema_major)
        object.__setattr__(self, "catalog_default_fields", request.fields)
        object.__setattr__(self, "filters", request.filters)
        object.__setattr__(self, "query_limit", request.limit)
        object.__setattr__(
            self,
            "minimum_row_count",
            minimum_row_count,
        )
        object.__setattr__(self, "identity_fields", identity_fields)
        object.__setattr__(self, "max_pages", max_pages)
        object.__setattr__(self, "max_rows", max_rows)
        object.__setattr__(self, "_limits_json", _canonical_json(limits))
        object.__setattr__(self, "_filters_json", _canonical_json(request.filters))

    def expected_limits(self) -> dict[str, Any]:
        return json.loads(self._limits_json)

    @property
    def identity_authority_available(self) -> bool:
        return bool(self.identity_fields)

    def query(self, *, as_of: str | None) -> QueryRequest:
        if self.observation.query_as_of_mode == "decision_as_of" and as_of is None:
            raise CatalogParityConfigurationError(
                "decision_as_of datasets require a manifest as_of timestamp"
            )
        return QueryRequest(
            dataset_id=self.dataset_id,
            schema_major=self.schema_major,
            fields=self.catalog_default_fields,
            filters=json.loads(self._filters_json),
            as_of=(
                as_of if self.observation.query_as_of_mode == "decision_as_of" else None
            ),
            order=None,
            limit=self.query_limit,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "expected_health": self.expected_health,
            "schema_major": self.schema_major,
            "catalog_default_fields": list(self.catalog_default_fields),
            "catalog_limits": self.expected_limits(),
            "filters": json.loads(self._filters_json),
            "query_limit": self.query_limit,
            "minimum_row_count": self.minimum_row_count,
            "identity_fields": list(self.identity_fields),
            "observation": self.observation.to_payload(),
            "max_pages": self.max_pages,
            "max_rows": self.max_rows,
        }


@dataclass(frozen=True)
class TradingDatasCatalogParityConfig:
    manifest_version: int
    base_url: str
    catalog_version: str
    access_policy_id: str
    transport_id: str
    timeout_seconds: float
    as_of: str | None
    expected_counts: Mapping[str, int]
    datasets: tuple[CatalogDatasetSpec, ...]
    _counts_json: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.manifest_version != MANIFEST_VERSION:
            raise CatalogParityConfigurationError(
                f"manifest_version must equal {MANIFEST_VERSION}"
            )
        parsed_url = urllib.parse.urlsplit(self.base_url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.netloc
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise CatalogParityConfigurationError(
                "base_url must be an absolute credential-free HTTP(S) URL"
            )
        catalog_version = _nonempty_string(
            self.catalog_version,
            field_name="catalog_version",
        )
        access_policy_id = _nonempty_string(
            self.access_policy_id,
            field_name="access_policy_id",
        )
        transport_id = _nonempty_string(
            self.transport_id,
            field_name="transport_id",
        )
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
        ):
            raise CatalogParityConfigurationError("timeout_seconds must be positive")
        decision_as_of_required = any(
            item.observation.query_as_of_mode == "decision_as_of"
            for item in self.datasets
        )
        if self.as_of is None:
            if decision_as_of_required:
                raise CatalogParityConfigurationError(
                    "as_of may be null only when every dataset omits decision_as_of"
                )
            as_of = None
        else:
            if not decision_as_of_required:
                raise CatalogParityConfigurationError(
                    "as_of must be null when every dataset omits decision_as_of"
                )
            as_of = _aware_timestamp(self.as_of, field_name="as_of")
        counts = _mapping(
            self.expected_counts,
            field_name="expected_counts",
            nonempty=True,
        )
        _exact_keys(
            counts,
            required=frozenset({"total", "active", "paused"}),
            field_name="expected_counts",
        )
        total = _native_count(counts["total"], field_name="total count", positive=True)
        active = _native_count(counts["active"], field_name="active count")
        paused = _native_count(counts["paused"], field_name="paused count")
        if total != active + paused:
            raise CatalogParityConfigurationError(
                "total count must equal active plus paused"
            )
        if not isinstance(self.datasets, tuple) or not self.datasets:
            raise CatalogParityConfigurationError("datasets must be a non-empty tuple")
        if not all(isinstance(item, CatalogDatasetSpec) for item in self.datasets):
            raise CatalogParityConfigurationError(
                "datasets must contain CatalogDatasetSpec values"
            )
        dataset_ids = tuple(item.dataset_id for item in self.datasets)
        if len(set(dataset_ids)) != len(dataset_ids):
            raise CatalogParityConfigurationError(
                "datasets must not contain duplicate dataset IDs"
            )
        if active != len(dataset_ids):
            raise CatalogParityConfigurationError(
                "active count must exactly equal manifest dataset cardinality"
            )
        try:
            self.to_client_config()
        except (TypeError, ValueError, SharedSignalsV1Error) as exc:
            raise CatalogParityConfigurationError(str(exc)) from exc
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))
        object.__setattr__(self, "catalog_version", catalog_version)
        object.__setattr__(self, "access_policy_id", access_policy_id)
        object.__setattr__(self, "transport_id", transport_id)
        object.__setattr__(self, "as_of", as_of)
        object.__setattr__(
            self,
            "_counts_json",
            _canonical_json({"total": total, "active": active, "paused": paused}),
        )

    @property
    def counts(self) -> dict[str, int]:
        return json.loads(self._counts_json)

    @property
    def manifest_sha256(self) -> str:
        return _sha256(self.to_payload())

    def to_client_config(self) -> SharedSignalsV1Config:
        return SharedSignalsV1Config(
            base_url=self.base_url,
            expected_catalog_version=self.catalog_version,
            dataset_ids=frozenset(item.dataset_id for item in self.datasets),
            access_policy_id=self.access_policy_id,
            timeout_seconds=self.timeout_seconds,
            max_limit=max(item.query_limit for item in self.datasets),
            cache_ttl_seconds=0,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "base_url": self.base_url,
            "catalog_version": self.catalog_version,
            "access_policy_id": self.access_policy_id,
            "transport_id": self.transport_id,
            "timeout_seconds": self.timeout_seconds,
            "as_of": self.as_of,
            "expected_counts": self.counts,
            "datasets": [item.to_payload() for item in self.datasets],
        }


_ROOT_KEYS = frozenset(
    {
        "manifest_version",
        "base_url",
        "catalog_version",
        "access_policy_id",
        "transport_id",
        "timeout_seconds",
        "as_of",
        "expected_counts",
        "datasets",
    }
)
_DATASET_KEYS = frozenset(
    {
        "dataset_id",
        "expected_health",
        "schema_major",
        "catalog_default_fields",
        "catalog_limits",
        "filters",
        "query_limit",
        "minimum_row_count",
        "identity_fields",
        "observation",
        "max_pages",
        "max_rows",
    }
)
_OBSERVATION_REQUIRED_KEYS = frozenset({"mode", "query_as_of_mode"})
_OBSERVATION_OPTIONAL_KEYS = frozenset(
    {
        "row_event_time_field",
        "row_event_time_format",
        "row_event_timezone",
        "row_event_time_semantic",
    }
)


def _dataset_spec(value: object, *, index: int) -> CatalogDatasetSpec:
    if not isinstance(value, Mapping):
        raise CatalogParityConfigurationError(f"datasets[{index}] must be an object")
    _exact_keys(value, required=_DATASET_KEYS, field_name=f"datasets[{index}]")
    observation = value["observation"]
    if not isinstance(observation, Mapping):
        raise CatalogParityConfigurationError(
            f"datasets[{index}].observation must be an object"
        )
    _exact_keys(
        observation,
        required=_OBSERVATION_REQUIRED_KEYS,
        optional=_OBSERVATION_OPTIONAL_KEYS,
        field_name=f"datasets[{index}].observation",
    )
    return CatalogDatasetSpec(
        dataset_id=value["dataset_id"],
        expected_health=value["expected_health"],
        schema_major=value["schema_major"],
        catalog_default_fields=_string_tuple(
            value["catalog_default_fields"],
            field_name=f"datasets[{index}].catalog_default_fields",
        ),
        catalog_limits=_mapping(
            value["catalog_limits"],
            field_name=f"datasets[{index}].catalog_limits",
            nonempty=True,
        ),
        filters=_mapping(
            value["filters"],
            field_name=f"datasets[{index}].filters",
        ),
        query_limit=value["query_limit"],
        minimum_row_count=value["minimum_row_count"],
        identity_fields=_identity_fields_tuple(
            value["identity_fields"],
            field_name=f"datasets[{index}].identity_fields",
        ),
        observation=ObservationMapping(
            mode=observation["mode"],
            query_as_of_mode=observation["query_as_of_mode"],
            row_event_time_field=observation.get("row_event_time_field"),
            row_event_time_format=observation.get("row_event_time_format"),
            row_event_timezone=observation.get("row_event_timezone"),
            row_event_time_semantic=observation.get("row_event_time_semantic"),
        ),
        max_pages=value["max_pages"],
        max_rows=value["max_rows"],
    )


def load_catalog_parity_manifest(path: Path) -> TradingDatasCatalogParityConfig:
    """Load a secret-free external manifest without path or content fallback."""

    if not isinstance(path, Path) or not path.is_absolute():
        raise CatalogParityConfigurationError("manifest path must be absolute")
    if path.is_symlink():
        raise CatalogParityConfigurationError("manifest path must not be a symlink")
    try:
        stat = path.stat()
    except OSError as exc:
        raise CatalogParityConfigurationError("manifest file is unavailable") from exc
    if not path.is_file() or stat.st_size <= 0 or stat.st_size > MAX_MANIFEST_BYTES:
        raise CatalogParityConfigurationError(
            "manifest must be a regular file between 1 byte and 1 MiB"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogParityConfigurationError("manifest must be UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise CatalogParityConfigurationError("manifest root must be an object")
    _secret_free(payload)
    _exact_keys(payload, required=_ROOT_KEYS, field_name="manifest")
    datasets = payload["datasets"]
    if not isinstance(datasets, list):
        raise CatalogParityConfigurationError("datasets must be a list")
    return TradingDatasCatalogParityConfig(
        manifest_version=payload["manifest_version"],
        base_url=payload["base_url"],
        catalog_version=payload["catalog_version"],
        access_policy_id=payload["access_policy_id"],
        transport_id=payload["transport_id"],
        timeout_seconds=payload["timeout_seconds"],
        as_of=payload["as_of"],
        expected_counts=_mapping(
            payload["expected_counts"],
            field_name="expected_counts",
            nonempty=True,
        ),
        datasets=tuple(
            _dataset_spec(item, index=index) for index, item in enumerate(datasets)
        ),
    )


def _catalog_activation_state(row: Mapping[str, Any]) -> str:
    availability = row.get("availability")
    if not isinstance(availability, Mapping):
        raise CatalogParityContractError("catalog availability is invalid")
    activation_states = availability.get("activation_states")
    if not isinstance(activation_states, list) or not activation_states:
        raise CatalogParityContractError("catalog activation_states is invalid")
    states: list[str] = []
    for value in activation_states:
        if not isinstance(value, str) or not value or value != value.strip():
            raise CatalogParityContractError("catalog activation_states is invalid")
        normalized = value.lower()
        if normalized in states:
            raise CatalogParityContractError("catalog activation_states is invalid")
        states.append(normalized)
    if states == ["active"]:
        return "active"
    if states == ["paused"]:
        return "paused"
    raise CatalogParityContractError("catalog activation state is unaccounted")


def _validate_catalog(
    *,
    config: TradingDatasCatalogParityConfig,
    rows: tuple[dict[str, Any], ...],
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    by_id = {row["dataset_id"]: row for row in rows}
    active: set[str] = set()
    paused: set[str] = set()
    for dataset_id, row in by_id.items():
        state = _catalog_activation_state(row)
        (active if state == "active" else paused).add(dataset_id)
    counts = {"total": len(rows), "active": len(active), "paused": len(paused)}
    if counts != config.counts:
        raise CatalogParityContractError("catalog counts do not match manifest")
    expected_active = {item.dataset_id for item in config.datasets}
    if active != expected_active:
        raise CatalogParityContractError(
            "catalog active dataset set does not match manifest"
        )
    for spec in config.datasets:
        row = by_id[spec.dataset_id]
        schema_major = row.get("schema_major")
        if type(schema_major) is not int or schema_major != spec.schema_major:
            raise CatalogParityContractError("catalog schema_major mismatch")
        raw_default_fields = row.get("default_fields")
        if not isinstance(raw_default_fields, list):
            raise CatalogParityContractError("catalog default_fields is invalid")
        try:
            default_fields = _string_tuple(
                raw_default_fields,
                field_name="catalog default_fields",
            )
        except CatalogParityConfigurationError as exc:
            raise CatalogParityContractError(
                "catalog default_fields is invalid"
            ) from exc
        if default_fields != spec.catalog_default_fields:
            raise CatalogParityContractError("catalog default_fields mismatch")
        raw_limits = row.get("limits")
        if not isinstance(raw_limits, Mapping) or not raw_limits:
            raise CatalogParityContractError("catalog limits is invalid")
        try:
            limits_json = _canonical_json(raw_limits)
        except CatalogParityConfigurationError as exc:
            raise CatalogParityContractError("catalog limits is invalid") from exc
        if limits_json != _canonical_json(spec.expected_limits()):
            raise CatalogParityContractError("catalog limits mismatch")
    return by_id, counts


def _source_proof_complete(envelope: QueryEnvelope) -> bool:
    metadata = envelope.metadata
    lineage = metadata.lineage

    def valid_identity(value: object) -> bool:
        return isinstance(value, str) and bool(value) and value == value.strip()

    return bool(
        isinstance(lineage, Mapping)
        and lineage
        and type(lineage.get("complete")) is bool
        and lineage.get("complete") is True
        and type(lineage.get("provider_neutral")) is bool
        and lineage.get("provider_neutral") is True
        and valid_identity(lineage.get("provider"))
        and valid_identity(lineage.get("transport_service"))
        and valid_identity(metadata.receipt_id)
        and valid_identity(metadata.data_through)
        and valid_identity(metadata.observed_at)
    )


def _source_proof_sha256(envelope: QueryEnvelope) -> str | None:
    if not _source_proof_complete(envelope):
        return None
    return _sha256(
        {
            "dataset_id": envelope.dataset_id,
            "catalog_version": envelope.catalog_version,
            "receipt_id": envelope.metadata.receipt_id,
            "data_through": envelope.metadata.data_through,
            "observed_at": envelope.metadata.observed_at,
            "lineage": envelope.metadata.lineage,
        }
    )


def _same_observation(
    first: PagedQueryRun | IdentitylessSinglePageRun,
    second: PagedQueryRun | IdentitylessSinglePageRun,
) -> bool:
    return bool(
        first.semantic_sha256 == second.semantic_sha256
        and first.semantic_trace_sha256 == second.semantic_trace_sha256
    )


def _health_result(
    *,
    spec: CatalogDatasetSpec,
    decisions: tuple[EvidenceDecision, EvidenceDecision],
) -> tuple[bool, str | None]:
    if spec.expected_health == "ready":
        healthy = all(
            decision.effective_state == "ready"
            and decision.action is EvidenceAction.ACCEPT
            and decision.eligible
            and decision.weight == 1.0
            for decision in decisions
        )
        return healthy, None if healthy else "ready_dataset_rejected"
    accounted = all(
        decision.effective_state != "ready"
        and decision.action is EvidenceAction.REJECT
        and not decision.eligible
        and decision.weight == 0.0
        for decision in decisions
    )
    return (
        accounted,
        None if accounted else "impaired_dataset_unexpectedly_accepted",
    )


def _base_receipt(config: TradingDatasCatalogParityConfig) -> dict[str, Any]:
    return {
        "schema_id": RECEIPT_SCHEMA_ID,
        "manifest_version": config.manifest_version,
        "authority": "non_authority",
        "production_verified": False,
        "real_trading_enabled": False,
        "research_snapshot_emitted": False,
        "as_of": config.as_of,
        "catalog_version": config.catalog_version,
        "transport_id": config.transport_id,
        "manifest_sha256": config.manifest_sha256,
        "status": "fail",
        "blocking": True,
        "transport_contract_pass": False,
        "ready_set_pass": False,
        "impaired_set_accounted": False,
        "catalog": None,
        "datasets": [],
        "reason_codes": [],
        "error_type": None,
    }


def _finalize(receipt: dict[str, Any]) -> dict[str, Any]:
    receipt["receipt_sha256"] = receipt_sha256(receipt)
    return receipt


def _error_type(error: Exception) -> str:
    name = type(error).__name__
    return name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", name) else "Exception"


def _append_reason(receipt: dict[str, Any], reason: str) -> None:
    if reason not in receipt["reason_codes"]:
        receipt["reason_codes"].append(reason)


def _dataset_receipt(
    *,
    spec: CatalogDatasetSpec,
    request: QueryRequest,
    first: PagedQueryRun | IdentitylessSinglePageRun,
    second: PagedQueryRun | IdentitylessSinglePageRun,
    decisions: tuple[EvidenceDecision, EvidenceDecision],
) -> tuple[dict[str, Any], bool, bool]:
    reasons: list[str] = []
    accounting_reasons: list[str] = []
    identity_authority_available = spec.identity_authority_available
    if not identity_authority_available:
        accounting_reasons.append("identity_authority_unavailable")
    proof_complete = _source_proof_complete(first.envelope) and _source_proof_complete(
        second.envelope
    )
    same_observation = _same_observation(first, second)
    if not proof_complete and spec.expected_health == "ready":
        reasons.append("source_proof_incomplete")
    elif not proof_complete:
        accounting_reasons.append("source_proof_unavailable")
    if not same_observation:
        reasons.append("same_observation_semantic_mismatch")
    health_ok, health_reason = _health_result(spec=spec, decisions=decisions)
    row_count_ok = bool(
        first.row_count >= spec.minimum_row_count
        and second.row_count >= spec.minimum_row_count
    )
    if not reasons and health_reason is not None:
        reasons.append(health_reason)
    if not reasons and not row_count_ok:
        reasons.append("minimum_row_count_not_met")
    transport_ok = same_observation and (
        proof_complete or spec.expected_health == "impaired"
    )
    classification_ok = transport_ok and health_ok and row_count_ok
    decision = decisions[0]
    pagination = first.to_receipt_payload()
    return (
        {
            "dataset_id": spec.dataset_id,
            "expected_health": spec.expected_health,
            "schema_major": spec.schema_major,
            "catalog_contract_sha256": _sha256(
                {
                    "default_fields": list(spec.catalog_default_fields),
                    "limits": spec.expected_limits(),
                }
            ),
            "query_sha256": request.sha256,
            "query_as_of": request.as_of,
            "observation_mapping_sha256": _sha256(spec.observation.to_payload()),
            "identity_fields_sha256": _sha256(list(spec.identity_fields)),
            "identity_authority_available": identity_authority_available,
            "max_pages": spec.max_pages,
            "max_rows": spec.max_rows,
            "minimum_row_count": spec.minimum_row_count,
            "page_count": first.page_count,
            "row_count": first.row_count,
            "pagination_complete": first.envelope.next_cursor is None,
            "pagination_trace_sha256": first.pagination_trace_sha256,
            "page_request_set_sha256": pagination["page_request_set_sha256"],
            "page_response_set_sha256": pagination["page_response_set_sha256"],
            "ordered_rows_sha256": pagination["ordered_rows_sha256"],
            "metadata_sha256": pagination["metadata_sha256"],
            "semantic_sha256": pagination["semantic_sha256"],
            "semantic_trace_sha256": pagination["semantic_trace_sha256"],
            "run_ordered_rows_sha256s": [
                first.ordered_rows_sha256,
                second.ordered_rows_sha256,
            ],
            "run_metadata_sha256s": [
                first.metadata_sha256,
                second.metadata_sha256,
            ],
            "run_semantic_sha256s": [
                first.semantic_sha256,
                second.semantic_sha256,
            ],
            "run_semantic_trace_sha256s": [
                first.semantic_trace_sha256,
                second.semantic_trace_sha256,
            ],
            "identity_sha256": (
                first.identity_sha256 if identity_authority_available else None
            ),
            "same_observation_match": same_observation,
            "source_proof_complete": proof_complete,
            "source_proof_sha256": _source_proof_sha256(first.envelope),
            "effective_state": decision.effective_state,
            "evidence_action": (
                decision.action.value
                if identity_authority_available
                else EvidenceAction.REJECT.value
            ),
            "evidence_eligible": (
                decision.eligible if identity_authority_available else False
            ),
            "effective_weight": (
                decision.weight if identity_authority_available else 0.0
            ),
            "parity_data_accepted": bool(
                identity_authority_available
                and spec.expected_health == "ready"
                and classification_ok
            ),
            "research_snapshot_eligible": False,
            "reason_codes": reasons,
            "accounting_reason_codes": accounting_reasons,
        },
        transport_ok,
        classification_ok,
    )


def run_tradingdatas_catalog_parity(
    config: TradingDatasCatalogParityConfig,
    *,
    transport: HTTPTransport | None,
) -> dict[str, Any]:
    """Run one catalog discovery and two bounded reads per discovered active ID."""

    if not isinstance(config, TradingDatasCatalogParityConfig):
        raise TypeError("config must be TradingDatasCatalogParityConfig")
    receipt = _base_receipt(config)
    if transport is None:
        _append_reason(receipt, "transport_not_configured")
        return _finalize(receipt)
    client = SharedSignalsV1Client(config.to_client_config(), transport=transport)
    try:
        catalog = client.get_catalog()
        _, counts = _validate_catalog(config=config, rows=catalog.data)
    except Exception as exc:
        _append_reason(receipt, "catalog_contract_failure")
        receipt["error_type"] = _error_type(exc)
        return _finalize(receipt)
    receipt["catalog"] = {
        "counts": counts,
        "catalog_sha256": _sha256(
            {
                "api_version": catalog.api_version,
                "catalog_version": catalog.catalog_version,
                "data": list(catalog.data),
            }
        ),
        "active_set_sha256": _sha256(
            sorted(item.dataset_id for item in config.datasets)
        ),
    }
    gate = DataEvidenceGate(
        {
            spec.dataset_id: DatasetEvidencePolicy(
                dataset_id=spec.dataset_id,
                degraded_action=EvidenceAction.REJECT,
                stale_action=EvidenceAction.REJECT,
            )
            for spec in config.datasets
        }
    )
    transport_ok = True
    ready_results: list[bool] = []
    impaired_results: list[bool] = []
    for spec in config.datasets:
        request = spec.query(as_of=config.as_of)
        try:
            if spec.identity_authority_available:
                first = collect_query_pages(
                    client=client,
                    request=request,
                    identity_fields=spec.identity_fields,
                    max_pages=spec.max_pages,
                    max_rows=spec.max_rows,
                )
                second = collect_query_pages(
                    client=client,
                    request=request,
                    identity_fields=spec.identity_fields,
                    max_pages=spec.max_pages,
                    max_rows=spec.max_rows,
                )
                first.verify_integrity(identity_fields=spec.identity_fields)
                second.verify_integrity(identity_fields=spec.identity_fields)
            else:
                first = collect_identityless_single_page(
                    client=client,
                    request=request,
                    max_pages=spec.max_pages,
                    max_rows=spec.max_rows,
                )
                second = collect_identityless_single_page(
                    client=client,
                    request=request,
                    max_pages=spec.max_pages,
                    max_rows=spec.max_rows,
                )
                first.verify_integrity()
                second.verify_integrity()
            decisions = (gate.evaluate(first.envelope), gate.evaluate(second.envelope))
        except PaginationContractError as exc:
            code = str(exc)
            _append_reason(receipt, code)
            receipt["error_type"] = _error_type(exc)
            receipt["datasets"].append(
                {
                    "dataset_id": spec.dataset_id,
                    "expected_health": spec.expected_health,
                    "query_sha256": request.sha256,
                    "query_as_of": request.as_of,
                    "identity_authority_available": spec.identity_authority_available,
                    "evidence_action": EvidenceAction.REJECT.value,
                    "evidence_eligible": False,
                    "effective_weight": 0.0,
                    "parity_data_accepted": False,
                    "research_snapshot_eligible": False,
                    "reason_codes": [code],
                }
            )
            transport_ok = False
            break
        except HTTPStatusError as exc:
            _append_reason(receipt, "query_contract_or_transport_failure")
            receipt["error_type"] = _error_type(exc)
            receipt["datasets"].append(
                {
                    "dataset_id": spec.dataset_id,
                    "expected_health": spec.expected_health,
                    "query_sha256": request.sha256,
                    "query_as_of": request.as_of,
                    "identity_authority_available": spec.identity_authority_available,
                    "evidence_action": EvidenceAction.REJECT.value,
                    "evidence_eligible": False,
                    "effective_weight": 0.0,
                    "parity_data_accepted": False,
                    "research_snapshot_eligible": False,
                    "reason_codes": ["query_contract_or_transport_failure"],
                }
            )
            transport_ok = False
            break
        except Exception as exc:
            _append_reason(receipt, "query_contract_or_transport_failure")
            receipt["error_type"] = _error_type(exc)
            receipt["datasets"].append(
                {
                    "dataset_id": spec.dataset_id,
                    "expected_health": spec.expected_health,
                    "query_sha256": request.sha256,
                    "query_as_of": request.as_of,
                    "identity_authority_available": spec.identity_authority_available,
                    "evidence_action": EvidenceAction.REJECT.value,
                    "evidence_eligible": False,
                    "effective_weight": 0.0,
                    "parity_data_accepted": False,
                    "research_snapshot_eligible": False,
                    "reason_codes": ["query_contract_or_transport_failure"],
                }
            )
            transport_ok = False
            break
        dataset_receipt, dataset_transport_ok, classification_ok = _dataset_receipt(
            spec=spec,
            request=request,
            first=first,
            second=second,
            decisions=decisions,
        )
        receipt["datasets"].append(dataset_receipt)
        transport_ok = transport_ok and dataset_transport_ok
        for reason in dataset_receipt["reason_codes"]:
            _append_reason(receipt, reason)
        if spec.expected_health == "ready":
            ready_results.append(classification_ok)
        else:
            impaired_results.append(classification_ok)

    expected_ready = sum(item.expected_health == "ready" for item in config.datasets)
    expected_impaired = len(config.datasets) - expected_ready
    receipt["transport_contract_pass"] = bool(
        transport_ok and len(receipt["datasets"]) == len(config.datasets)
    )
    receipt["ready_set_pass"] = bool(
        len(ready_results) == expected_ready and all(ready_results)
    )
    receipt["impaired_set_accounted"] = bool(
        len(impaired_results) == expected_impaired and all(impaired_results)
    )
    overall = bool(
        receipt["transport_contract_pass"]
        and receipt["ready_set_pass"]
        and receipt["impaired_set_accounted"]
    )
    receipt["status"] = "pass" if overall else "fail"
    receipt["blocking"] = not overall
    return _finalize(receipt)


def write_catalog_parity_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    """Atomically persist a verified receipt with private file permissions."""

    if not isinstance(path, Path) or not path.is_absolute():
        raise CatalogParityConfigurationError("output path must be absolute")
    if path.exists() and path.is_symlink():
        raise CatalogParityConfigurationError("output path must not be a symlink")
    if not isinstance(receipt, Mapping):
        raise CatalogParityConfigurationError("receipt must be a mapping")
    claimed = receipt.get("receipt_sha256")
    if not isinstance(claimed, str) or claimed != receipt_sha256(receipt):
        raise CatalogParityConfigurationError("receipt hash is invalid")
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
        raise CatalogParityConfigurationError("temporary output path is occupied")
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise CatalogParityConfigurationError(
            "catalog parity receipt could not be persisted"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Catalog-driven TradingDatas parity acceptance"
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if os.environ.get("REAL_TRADING_ENABLED", "false").strip().lower() != "false":
        print("catalog parity requires REAL_TRADING_ENABLED=false", file=sys.stderr)
        return 64
    try:
        config = load_catalog_parity_manifest(args.manifest)
        if not args.token_file.is_absolute():
            raise CatalogParityConfigurationError("token file path must be absolute")
        transport = build_runtime_transport(
            config.transport_id,
            token_file=args.token_file,
            base_url=config.base_url,
        )
        receipt = run_tradingdatas_catalog_parity(config, transport=transport)
        write_catalog_parity_receipt(args.output, receipt)
    except (CatalogParityConfigurationError, RuntimeGateConfigurationError, OSError):
        print("catalog parity configuration failed", file=sys.stderr)
        return 64
    if args.json:
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"{receipt['status']} blocking={receipt['blocking']} "
            f"transport={receipt['transport_contract_pass']} "
            f"ready={receipt['ready_set_pass']} "
            f"impaired={receipt['impaired_set_accounted']}"
        )
    return 2 if receipt["blocking"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))


__all__ = [
    "CatalogDatasetSpec",
    "CatalogParityConfigurationError",
    "CatalogParityContractError",
    "ObservationMapping",
    "TradingDatasCatalogParityConfig",
    "load_catalog_parity_manifest",
    "main",
    "receipt_sha256",
    "run_tradingdatas_catalog_parity",
    "write_catalog_parity_receipt",
]
