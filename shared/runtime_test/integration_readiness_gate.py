"""Read-only compatibility check for a SharedSignals integration receipt.

The probe receipt is deliberately non-authoritative and its content digest is
not an origin signature.  This module verifies local file integrity and exact
configuration compatibility only.  It does not prove that the probe ran, does
not authenticate SharedSignals, and must never authorize capital or Journal
writes.  The runtime Evidence Gate remains the authority for every dataset
used by a future run.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from shared.data.evidence_gate import DatasetEvidencePolicy
from shared.data.research_snapshot import ResearchDataProfile
from shared.data.sharedsignals_v1 import QueryRequest


INTEGRATION_READINESS_SCHEMA_ID = "tradingagent.sharedsignals.integration-readiness.v1"
MAX_INTEGRATION_RECEIPT_BYTES = 1_048_576
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_PROCESS_ATTESTATION_KEY = secrets.token_bytes(32)
INTEGRATION_READINESS_AUTHORITY = "non_authority"
INTEGRATION_READINESS_SCOPE = "local_content_integrity_and_config_compatibility"


class IntegrationReadinessError(ValueError):
    """Raised before runtime I/O when readiness evidence is unsafe."""


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
        raise IntegrationReadinessError("integration_receipt_not_canonical") from exc


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _nonempty_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise IntegrationReadinessError(f"{field}_invalid")
    return value


def _sha(value: object, *, field: str) -> str:
    text = _nonempty_string(value, field=field).lower()
    if not _SHA256_RE.fullmatch(text):
        raise IntegrationReadinessError(f"{field}_invalid")
    return text


def _aware(value: object, *, field: str) -> datetime:
    text = _nonempty_string(value, field=field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IntegrationReadinessError(f"{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IntegrationReadinessError(f"{field}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _exact_keys(
    value: object,
    *,
    expected: frozenset[str],
    field: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise IntegrationReadinessError(f"{field}_schema_invalid")
    return value


def _native_bool(value: object, *, expected: bool, field: str) -> None:
    if type(value) is not bool or value is not expected:
        raise IntegrationReadinessError(f"{field}_invalid")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise IntegrationReadinessError("integration_receipt_duplicate_key")
        result[key] = value
    return result


def _assert_safe_regular_file(path: Path) -> os.stat_result:
    if not isinstance(path, Path) or not path.is_absolute():
        raise IntegrationReadinessError("integration_receipt_path_must_be_absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError as exc:
            raise IntegrationReadinessError(
                "integration_receipt_file_unavailable"
            ) from exc
        except OSError as exc:
            raise IntegrationReadinessError(
                "integration_receipt_file_unavailable"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise IntegrationReadinessError("integration_receipt_symlink_forbidden")
    metadata = os.lstat(path)
    if not stat.S_ISREG(metadata.st_mode):
        raise IntegrationReadinessError("integration_receipt_must_be_regular_file")
    if metadata.st_nlink != 1:
        raise IntegrationReadinessError("integration_receipt_hardlink_forbidden")
    if metadata.st_mode & 0o077:
        raise IntegrationReadinessError("integration_receipt_permissions_too_open")
    if metadata.st_size <= 0 or metadata.st_size > MAX_INTEGRATION_RECEIPT_BYTES:
        raise IntegrationReadinessError("integration_receipt_size_invalid")
    return metadata


def _read_safe_regular_file(path: Path) -> bytes:
    """Read one stable private file descriptor without following final links."""

    before = _assert_safe_regular_file(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise IntegrationReadinessError("integration_receipt_read_failed") from exc
    try:
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        if identity != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ):
            raise IntegrationReadinessError("integration_receipt_changed_during_read")
        chunks: list[bytes] = []
        remaining = MAX_INTEGRATION_RECEIPT_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) != identity:
            raise IntegrationReadinessError("integration_receipt_changed_during_read")
        if (
            len(encoded) != opened.st_size
            or len(encoded) > MAX_INTEGRATION_RECEIPT_BYTES
        ):
            raise IntegrationReadinessError("integration_receipt_size_invalid")
        return encoded
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class DatasetReadinessExpectation:
    """Exact dataset/query/policy identity expected by the paper runtime."""

    probe_role: str
    dataset_id: str
    schema_major: int
    requirement_role: str
    query_sha256: str
    degraded_action: str
    stale_action: str
    degraded_weight: float
    stale_weight: float

    def __post_init__(self) -> None:
        _nonempty_string(self.probe_role, field="probe_role")
        _nonempty_string(self.dataset_id, field="dataset_id")
        _nonempty_string(self.requirement_role, field="requirement_role")
        _sha(self.query_sha256, field="query_sha256")
        if (
            isinstance(self.schema_major, bool)
            or not isinstance(self.schema_major, int)
            or self.schema_major <= 0
        ):
            raise IntegrationReadinessError("schema_major_invalid")
        for field_name in ("degraded_action", "stale_action"):
            if getattr(self, field_name) not in {"reject", "deweight"}:
                raise IntegrationReadinessError(f"{field_name}_invalid")
        for field_name in ("degraded_weight", "stale_weight"):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise IntegrationReadinessError(f"{field_name}_invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "probe_role": self.probe_role,
            "dataset_id": self.dataset_id,
            "schema_major": self.schema_major,
            "requirement_role": self.requirement_role,
            "query_sha256": self.query_sha256,
            "degraded_action": self.degraded_action,
            "stale_action": self.stale_action,
            "degraded_weight": float(self.degraded_weight),
            "stale_weight": float(self.stale_weight),
        }


@dataclass(frozen=True)
class IntegrationReadinessExpectation:
    """Explicit expectation derived from the approved probe manifest."""

    profile_id: str
    as_of: str
    base_url: str
    access_policy_id: str
    catalog_version: str
    transport_id: str
    manifest_sha256: str
    authority_sha256: str
    datasets: tuple[DatasetReadinessExpectation, ...]

    def __post_init__(self) -> None:
        _nonempty_string(self.profile_id, field="profile_id")
        _aware(self.as_of, field="as_of")
        _nonempty_string(self.base_url, field="base_url")
        _nonempty_string(self.access_policy_id, field="access_policy_id")
        _nonempty_string(self.catalog_version, field="catalog_version")
        _nonempty_string(self.transport_id, field="transport_id")
        _sha(self.manifest_sha256, field="manifest_sha256")
        _sha(self.authority_sha256, field="authority_sha256")
        if not isinstance(self.datasets, tuple) or not self.datasets:
            raise IntegrationReadinessError("dataset_expectations_missing")
        if not all(type(item) is DatasetReadinessExpectation for item in self.datasets):
            raise IntegrationReadinessError("dataset_expectation_invalid")
        roles = [item.probe_role for item in self.datasets]
        dataset_ids = [item.dataset_id for item in self.datasets]
        if len(roles) != len(set(roles)) or len(dataset_ids) != len(set(dataset_ids)):
            raise IntegrationReadinessError("dataset_expectation_duplicate")

    @property
    def expectation_sha256(self) -> str:
        return _sha256(self.to_payload())

    def to_payload(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "as_of": self.as_of,
            "base_url": self.base_url,
            "access_policy_id": self.access_policy_id,
            "catalog_version": self.catalog_version,
            "transport_id": self.transport_id,
            "manifest_sha256": self.manifest_sha256,
            "authority_sha256": self.authority_sha256,
            "datasets": [item.to_payload() for item in self.datasets],
        }


class VerifiedIntegrationReadiness:
    """Opaque process-local result of a non-authoritative compatibility check."""

    __slots__ = (
        "_attestation",
        "_expectation",
        "_receipt_sha256",
        "_semantic_snapshot_sha256",
    )

    def __init__(
        self,
        *,
        expectation: IntegrationReadinessExpectation,
        receipt_sha256: str,
        semantic_snapshot_sha256: str,
        attestation: str,
    ) -> None:
        self._expectation = expectation
        self._receipt_sha256 = receipt_sha256
        self._semantic_snapshot_sha256 = semantic_snapshot_sha256
        self._attestation = attestation

    @property
    def receipt_sha256(self) -> str:
        return self._receipt_sha256

    @property
    def semantic_snapshot_sha256(self) -> str:
        return self._semantic_snapshot_sha256

    @property
    def expectation_sha256(self) -> str:
        return self._expectation.expectation_sha256

    def binding_payload(self) -> Mapping[str, str]:
        _verify_capability(self)
        return MappingProxyType(
            {
                "authority": INTEGRATION_READINESS_AUTHORITY,
                "verification_scope": INTEGRATION_READINESS_SCOPE,
                "expectation_sha256": self.expectation_sha256,
                "receipt_sha256": self.receipt_sha256,
                "semantic_snapshot_sha256": self.semantic_snapshot_sha256,
            }
        )


def _attestation_payload(
    *,
    expectation_sha256: str,
    receipt_sha256: str,
    semantic_snapshot_sha256: str,
) -> bytes:
    return _canonical_json(
        {
            "expectation_sha256": expectation_sha256,
            "receipt_sha256": receipt_sha256,
            "semantic_snapshot_sha256": semantic_snapshot_sha256,
        }
    ).encode("utf-8")


def _attest(
    *,
    expectation_sha256: str,
    receipt_sha256: str,
    semantic_snapshot_sha256: str,
) -> str:
    return hmac.new(
        _PROCESS_ATTESTATION_KEY,
        _attestation_payload(
            expectation_sha256=expectation_sha256,
            receipt_sha256=receipt_sha256,
            semantic_snapshot_sha256=semantic_snapshot_sha256,
        ),
        hashlib.sha256,
    ).hexdigest()


def _verify_capability(value: object) -> VerifiedIntegrationReadiness:
    if type(value) is not VerifiedIntegrationReadiness:
        raise IntegrationReadinessError("verified_integration_readiness_required")
    expected = _attest(
        expectation_sha256=value.expectation_sha256,
        receipt_sha256=value.receipt_sha256,
        semantic_snapshot_sha256=value.semantic_snapshot_sha256,
    )
    if not hmac.compare_digest(value._attestation, expected):
        raise IntegrationReadinessError("integration_readiness_attestation_invalid")
    return value


_RECEIPT_KEYS = frozenset(
    {
        "schema_id",
        "probe_version",
        "authority",
        "production_verified",
        "real_trading_enabled",
        "profile_id",
        "as_of",
        "catalog_version",
        "transport_id",
        "manifest_sha256",
        "authority_sha256",
        "status",
        "blocking",
        "reason_codes",
        "error_type",
        "catalog",
        "datasets",
        "same_as_of_match",
        "snapshot_runs",
        "semantic_snapshot_sha256",
        "receipt_sha256",
    }
)
_CATALOG_KEYS = frozenset({"request_id", "catalog_sha256", "dataset_count"})
_DATASET_KEYS = frozenset(
    {
        "probe_role",
        "dataset_id",
        "schema_major",
        "requirement_role",
        "query_sha256",
        "request_ids",
        "state",
        "degraded",
        "freshness_state",
        "quality_state",
        "lineage_state",
        "freshness_sha256",
        "quality_sha256",
        "lineage_sha256",
        "receipt_id",
        "data_through",
        "observed_at",
        "source_proof_complete",
        "evidence_action",
        "effective_state",
        "eligible",
        "effective_weight",
        "evidence_reasons_sha256",
        "row_count",
        "requested_fields_sha256",
        "missing_requested_fields",
        "unexpected_field_count",
        "unexpected_fields_sha256",
        "pagination_complete",
        "same_as_of_match",
        "semantic_response_sha256",
        "reason_codes",
    }
)
_SNAPSHOT_KEYS = frozenset(
    {"snapshot_sha256", "execution_eligible", "blocking_reasons"}
)


def _validate_dataset_receipt(
    value: object,
    *,
    expectation: DatasetReadinessExpectation,
) -> None:
    row = _exact_keys(value, expected=_DATASET_KEYS, field="dataset_receipt")
    for field_name, expected in (
        ("probe_role", expectation.probe_role),
        ("dataset_id", expectation.dataset_id),
        ("schema_major", expectation.schema_major),
        ("requirement_role", expectation.requirement_role),
        ("query_sha256", expectation.query_sha256),
        ("state", "ready"),
        ("degraded", False),
        ("freshness_state", "fresh"),
        ("quality_state", "valid"),
        ("lineage_state", "complete"),
        ("source_proof_complete", True),
        ("evidence_action", "accept"),
        ("effective_state", "ready"),
        ("eligible", True),
        ("effective_weight", 1.0),
        ("pagination_complete", True),
        ("same_as_of_match", True),
    ):
        actual = row.get(field_name)
        if isinstance(expected, bool):
            _native_bool(actual, expected=expected, field=field_name)
        elif actual != expected:
            raise IntegrationReadinessError(f"dataset_{field_name}_mismatch")
    for field_name in (
        "freshness_sha256",
        "quality_sha256",
        "lineage_sha256",
        "evidence_reasons_sha256",
        "requested_fields_sha256",
        "unexpected_fields_sha256",
        "semantic_response_sha256",
    ):
        _sha(row.get(field_name), field=field_name)
    for field_name in ("receipt_id", "data_through", "observed_at"):
        _nonempty_string(row.get(field_name), field=field_name)
    request_ids = row.get("request_ids")
    if (
        not isinstance(request_ids, list)
        or len(request_ids) != 2
        or any(not isinstance(item, str) or not item for item in request_ids)
    ):
        raise IntegrationReadinessError("dataset_request_ids_invalid")
    for field_name in ("missing_requested_fields", "reason_codes"):
        if row.get(field_name) != []:
            raise IntegrationReadinessError(f"dataset_{field_name}_not_empty")
    if row.get("unexpected_field_count") != 0:
        raise IntegrationReadinessError("dataset_unexpected_fields_present")
    row_count = row.get("row_count")
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count <= 0:
        raise IntegrationReadinessError("dataset_row_count_invalid")


def load_and_verify_integration_receipt(
    path: Path,
    *,
    expectation: IntegrationReadinessExpectation,
) -> VerifiedIntegrationReadiness:
    """Check one local PASS receipt without authenticating its origin."""

    if type(expectation) is not IntegrationReadinessExpectation:
        raise IntegrationReadinessError("integration_expectation_invalid")
    encoded = _read_safe_regular_file(path)
    try:
        payload = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrationReadinessError("integration_receipt_json_invalid") from exc
    receipt = _exact_keys(payload, expected=_RECEIPT_KEYS, field="receipt")
    claimed_sha = _sha(receipt.get("receipt_sha256"), field="receipt_sha256")
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256")
    if not hmac.compare_digest(claimed_sha, _sha256(unsigned)):
        raise IntegrationReadinessError("integration_receipt_sha256_mismatch")

    for field_name, expected in (
        ("schema_id", INTEGRATION_READINESS_SCHEMA_ID),
        ("probe_version", 1),
        ("authority", INTEGRATION_READINESS_AUTHORITY),
        ("production_verified", False),
        ("real_trading_enabled", False),
        ("profile_id", expectation.profile_id),
        ("as_of", expectation.as_of),
        ("catalog_version", expectation.catalog_version),
        ("transport_id", expectation.transport_id),
        ("manifest_sha256", expectation.manifest_sha256),
        ("authority_sha256", expectation.authority_sha256),
        ("status", "pass"),
        ("blocking", False),
        ("same_as_of_match", True),
    ):
        actual = receipt.get(field_name)
        if isinstance(expected, bool):
            _native_bool(actual, expected=expected, field=field_name)
        elif actual != expected:
            raise IntegrationReadinessError(f"integration_{field_name}_mismatch")
    if receipt.get("reason_codes") != [] or receipt.get("error_type") is not None:
        raise IntegrationReadinessError("integration_receipt_not_clean_pass")

    catalog = _exact_keys(
        receipt.get("catalog"), expected=_CATALOG_KEYS, field="catalog_receipt"
    )
    _nonempty_string(catalog.get("request_id"), field="catalog_request_id")
    _sha(catalog.get("catalog_sha256"), field="catalog_sha256")
    if catalog.get("dataset_count") != len(expectation.datasets):
        raise IntegrationReadinessError("catalog_dataset_count_mismatch")

    datasets = receipt.get("datasets")
    if not isinstance(datasets, list) or len(datasets) != len(expectation.datasets):
        raise IntegrationReadinessError("integration_dataset_set_incomplete")
    for row, expected in zip(datasets, expectation.datasets):
        _validate_dataset_receipt(row, expectation=expected)

    snapshots = receipt.get("snapshot_runs")
    if not isinstance(snapshots, list) or len(snapshots) != 2:
        raise IntegrationReadinessError("integration_snapshot_runs_invalid")
    for snapshot in snapshots:
        value = _exact_keys(
            snapshot, expected=_SNAPSHOT_KEYS, field="integration_snapshot"
        )
        _sha(value.get("snapshot_sha256"), field="snapshot_sha256")
        _native_bool(
            value.get("execution_eligible"),
            expected=True,
            field="snapshot_execution_eligible",
        )
        if value.get("blocking_reasons") != []:
            raise IntegrationReadinessError("integration_snapshot_blocked")
    semantic_sha = _sha(
        receipt.get("semantic_snapshot_sha256"),
        field="semantic_snapshot_sha256",
    )
    return VerifiedIntegrationReadiness(
        expectation=expectation,
        receipt_sha256=claimed_sha,
        semantic_snapshot_sha256=semantic_sha,
        attestation=_attest(
            expectation_sha256=expectation.expectation_sha256,
            receipt_sha256=claimed_sha,
            semantic_snapshot_sha256=semantic_sha,
        ),
    )


def assert_readiness_matches_runtime(
    readiness: VerifiedIntegrationReadiness,
    *,
    trade_date: str,
    decision_as_of: datetime,
    base_url: str,
    access_policy_id: str,
    catalog_version: str,
    dataset_profile: ResearchDataProfile,
    dataset_requests: Mapping[str, QueryRequest],
    evidence_policies: Mapping[str, DatasetEvidencePolicy],
) -> Mapping[str, str]:
    """Bind a verified capability to the exact runtime configuration."""

    verified = _verify_capability(readiness)
    expectation = verified._expectation
    runtime_as_of = decision_as_of.astimezone(timezone.utc)
    if _aware(expectation.as_of, field="expectation_as_of") != runtime_as_of:
        raise IntegrationReadinessError("integration_runtime_as_of_mismatch")
    try:
        expected_trade_date = runtime_as_of.astimezone(_SHANGHAI).strftime("%Y-%m-%d")
        normalized_trade_date = datetime.strptime(trade_date, "%Y-%m-%d").strftime(
            "%Y-%m-%d"
        )
    except (TypeError, ValueError) as exc:
        raise IntegrationReadinessError("integration_trade_date_invalid") from exc
    if normalized_trade_date != expected_trade_date:
        raise IntegrationReadinessError("integration_receipt_cross_day_reuse")
    if (
        base_url != expectation.base_url
        or access_policy_id != expectation.access_policy_id
        or catalog_version != expectation.catalog_version
        or dataset_profile.profile_id != expectation.profile_id
        or dataset_profile.catalog_version != expectation.catalog_version
    ):
        raise IntegrationReadinessError("integration_runtime_identity_mismatch")
    requests = dict(dataset_requests)
    policies = dict(evidence_policies)
    requirements = {item.dataset_id: item for item in dataset_profile.requirements}
    if (
        set(requests) != {item.dataset_id for item in expectation.datasets}
        or set(policies) != set(requests)
        or set(requirements) != set(requests)
    ):
        raise IntegrationReadinessError("integration_runtime_dataset_set_mismatch")
    for item in expectation.datasets:
        request = requests[item.dataset_id]
        policy = policies[item.dataset_id]
        requirement = requirements[item.dataset_id]
        if (
            type(request) is not QueryRequest
            or request.sha256 != item.query_sha256
            or request.dataset_id != item.dataset_id
            or type(policy) is not DatasetEvidencePolicy
            or policy.dataset_id != item.dataset_id
            or policy.degraded_action.value != item.degraded_action
            or policy.stale_action.value != item.stale_action
            or float(policy.degraded_weight) != float(item.degraded_weight)
            or float(policy.stale_weight) != float(item.stale_weight)
            or requirement.role != item.requirement_role
        ):
            raise IntegrationReadinessError(
                f"integration_runtime_dataset_identity_mismatch:{item.dataset_id}"
            )
    return verified.binding_payload()


__all__ = [
    "DatasetReadinessExpectation",
    "INTEGRATION_READINESS_AUTHORITY",
    "INTEGRATION_READINESS_SCHEMA_ID",
    "INTEGRATION_READINESS_SCOPE",
    "IntegrationReadinessError",
    "IntegrationReadinessExpectation",
    "VerifiedIntegrationReadiness",
    "assert_readiness_matches_runtime",
    "load_and_verify_integration_receipt",
]
