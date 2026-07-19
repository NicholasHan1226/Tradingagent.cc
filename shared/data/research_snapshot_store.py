#!/usr/bin/env python3
"""Durable, immutable storage for validated research data snapshots.

The store has no default root, transport, database, or legacy fallback.  A
snapshot is published in two steps under a per-decision file lock: first an
immutable content-addressed artifact, then an immutable decision binding.  A
binding is set once; exact replays are idempotent and every other transition is
rejected.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import stat
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .research_snapshot import (
    ResearchDataContractError,
    ResearchDataSnapshot,
    ResearchDatasetSnapshot,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DATASET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_ARTIFACT_KEYS = frozenset(
    {
        "schema_version",
        "artifact_type",
        "identity",
        "snapshot_state",
        "datasets",
        "evidence_projection",
        "content_sha256",
    }
)
_IDENTITY_KEYS = frozenset(
    {"profile_id", "catalog_version", "decision_as_of", "snapshot_sha256"}
)
_SNAPSHOT_STATE_KEYS = frozenset({"execution_eligible", "blocking_reasons"})
_DATASET_KEYS = frozenset(
    {
        "dataset_id",
        "role",
        "api_version",
        "catalog_version",
        "request_id",
        "receipt_id",
        "evidence_state",
        "evidence_action",
        "eligible",
        "weight",
        "reasons",
        "source_proof_complete",
        "data_through",
        "observed_at",
        "next_cursor",
        "row_count",
        "row_pit_sha256",
        "max_row_available_time",
        "response_sha256",
        "rows",
        "rows_sha256",
        "dataset_artifact_sha256",
    }
)
_BINDING_KEYS = frozenset(
    {
        "schema_version",
        "binding_type",
        "decision_identity_sha256",
        "profile_id",
        "catalog_version",
        "decision_as_of",
        "snapshot_sha256",
        "artifact_content_sha256",
        "receipt_ids",
        "receipt_ids_sha256",
        "content_sha256",
    }
)


class ResearchSnapshotStoreCorruption(RuntimeError):
    """Raised when persisted or supplied snapshot identity is not trustworthy."""


class ResearchSnapshotStoreConflict(RuntimeError):
    """Raised when immutable/CAS semantics reject a write."""


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
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_noncanonical_value"
        ) from exc


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_value(value: object) -> str:
    return _sha256_text(_canonical_json(value))


def _nonempty_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ResearchSnapshotStoreCorruption(
            f"research_snapshot_store_{field_name}_invalid"
        )
    return value


def _dataset_id(value: object, *, field_name: str = "dataset_id") -> str:
    result = _nonempty_string(value, field_name=field_name)
    if not _DATASET_ID_RE.fullmatch(result):
        raise ResearchSnapshotStoreCorruption(
            f"research_snapshot_store_{field_name}_invalid"
        )
    return result


def _sha256(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ResearchSnapshotStoreCorruption(
            f"research_snapshot_store_{field_name}_invalid"
        )
    return value


def _aware_instant(value: object, *, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = _nonempty_string(value, field_name=field_name)
        normalized = f"{text[:-1]}+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ResearchSnapshotStoreCorruption(
                f"research_snapshot_store_{field_name}_invalid"
            ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResearchSnapshotStoreCorruption(
            f"research_snapshot_store_{field_name}_invalid"
        )
    return parsed.astimezone(timezone.utc)


def _normalized_instant(value: object, *, field_name: str) -> str:
    return _aware_instant(value, field_name=field_name).isoformat()


def _assert_safe_path(path: Path) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ResearchSnapshotStoreCorruption(
                "research_snapshot_store_path_unreadable"
            ) from exc
        if stat.S_ISLNK(mode):
            raise ResearchSnapshotStoreCorruption(
                "research_snapshot_store_symlink_forbidden"
            )
        if current != absolute and not stat.S_ISDIR(mode):
            raise ResearchSnapshotStoreCorruption(
                "research_snapshot_store_parent_not_directory"
            )


def _no_follow_flag() -> int:
    return getattr(os, "O_NOFOLLOW", 0)


def _directory_flag() -> int:
    return getattr(os, "O_DIRECTORY", 0)


def _same_file_identity(fd: int, path: Path, *, kind: str) -> os.stat_result:
    try:
        fd_stat = os.fstat(fd)
        path_stat = path.lstat()
    except OSError as exc:
        raise ResearchSnapshotStoreCorruption(
            f"research_snapshot_store_{kind}_identity_unavailable"
        ) from exc
    if not stat.S_ISREG(fd_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise ResearchSnapshotStoreCorruption(
            f"research_snapshot_store_{kind}_not_regular"
        )
    if fd_stat.st_nlink != 1 or path_stat.st_nlink != 1:
        raise ResearchSnapshotStoreCorruption(
            f"research_snapshot_store_{kind}_hardlink_forbidden"
        )
    if (fd_stat.st_dev, fd_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
        raise ResearchSnapshotStoreCorruption(
            f"research_snapshot_store_{kind}_identity_changed"
        )
    return fd_stat


def _fsync_directory(path: Path) -> None:
    _assert_safe_path(path)
    flags = os.O_RDONLY | _directory_flag() | _no_follow_flag()
    try:
        fd = os.open(os.fspath(path), flags)
    except OSError as exc:
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_directory_sync_failed"
        ) from exc
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise ResearchSnapshotStoreCorruption(
                "research_snapshot_store_root_not_directory"
            )
        os.fsync(fd)
    except OSError as exc:
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_directory_sync_failed"
        ) from exc
    finally:
        os.close(fd)


def _decision_identity(profile_id: str, decision_as_of: str) -> str:
    return _sha256_value({"profile_id": profile_id, "decision_as_of": decision_as_of})


def _validate_receipt_ids(value: object) -> dict[str, str | None]:
    if not isinstance(value, Mapping) or not value:
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_receipt_ids_invalid"
        )
    result: dict[str, str | None] = {}
    for raw_dataset_id, raw_receipt_id in value.items():
        dataset = _dataset_id(raw_dataset_id, field_name="receipt_dataset_id")
        receipt = (
            None
            if raw_receipt_id is None
            else _nonempty_string(raw_receipt_id, field_name="receipt_id")
        )
        if dataset in result:
            raise ResearchSnapshotStoreCorruption(
                "research_snapshot_store_receipt_ids_duplicate"
            )
        result[dataset] = receipt
    return dict(sorted(result.items()))


def _validate_evidence_semantics(dataset: Mapping[str, Any]) -> None:
    state = _nonempty_string(dataset.get("evidence_state"), field_name="state")
    action = _nonempty_string(dataset.get("evidence_action"), field_name="action")
    eligible = dataset.get("eligible")
    weight = dataset.get("weight")
    if type(eligible) is not bool:
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_dataset_eligible_invalid"
        )
    if isinstance(weight, bool) or not isinstance(weight, (int, float)):
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_dataset_weight_invalid"
        )
    numeric_weight = float(weight)
    if not math.isfinite(numeric_weight):
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_dataset_weight_invalid"
        )
    if action == "accept":
        valid = state == "ready" and eligible and numeric_weight == 1.0
    elif action == "deweight":
        valid = (
            state in {"degraded", "stale"} and eligible and 0.0 < numeric_weight < 1.0
        )
    elif action == "reject":
        valid = state in {"degraded", "stale", "failed", "unknown"} and (
            not eligible and numeric_weight == 0.0
        )
    else:
        valid = False
    if not valid:
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_evidence_semantics_invalid"
        )


def _dataset_payload(dataset: ResearchDatasetSnapshot) -> dict[str, Any]:
    try:
        rows = dataset.decoded_rows()
    except (ResearchDataContractError, json.JSONDecodeError) as exc:
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_rows_invalid"
        ) from exc
    rows_json = _canonical_json(rows)
    payload: dict[str, Any] = {
        "dataset_id": dataset.dataset_id,
        "role": dataset.role,
        "api_version": dataset.api_version,
        "catalog_version": dataset.catalog_version,
        "request_id": dataset.request_id,
        "receipt_id": dataset.receipt_id,
        "evidence_state": dataset.evidence_state,
        "evidence_action": dataset.evidence_action,
        "eligible": dataset.eligible,
        "weight": dataset.weight,
        "reasons": list(dataset.reasons),
        "source_proof_complete": dataset.source_proof_complete,
        "data_through": dataset.data_through,
        "observed_at": dataset.observed_at,
        "next_cursor": dataset.next_cursor,
        "row_count": dataset.row_count,
        "row_pit_sha256": dataset.row_pit_sha256,
        "max_row_available_time": dataset.max_row_available_time,
        "response_sha256": dataset.response_sha256,
        "rows": json.loads(rows_json),
        "rows_sha256": _sha256_text(rows_json),
    }
    payload["dataset_artifact_sha256"] = _sha256_value(payload)
    return payload


def _expected_snapshot_hash(
    *,
    profile_id: str,
    catalog_version: str,
    decision_as_of: str,
    datasets: list[ResearchDatasetSnapshot],
    blocking_reasons: list[str],
) -> str:
    return _sha256_value(
        {
            "profile_id": profile_id,
            "catalog_version": catalog_version,
            "decision_as_of": decision_as_of,
            "datasets": [
                {
                    "dataset_id": item.dataset_id,
                    "role": item.role,
                    "response_sha256": item.response_sha256,
                }
                for item in datasets
            ],
            "blocking_reasons": blocking_reasons,
        }
    )


def _artifact_payload(snapshot: ResearchDataSnapshot) -> dict[str, Any]:
    if not isinstance(snapshot, ResearchDataSnapshot):
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_snapshot_type_invalid"
        )
    dataset_payloads = [_dataset_payload(item) for item in snapshot.datasets]
    unsigned: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": "research_data_snapshot.v1",
        "identity": {
            "profile_id": snapshot.profile_id,
            "catalog_version": snapshot.catalog_version,
            "decision_as_of": snapshot.decision_as_of,
            "snapshot_sha256": snapshot.snapshot_sha256,
        },
        "snapshot_state": {
            "execution_eligible": snapshot.execution_eligible,
            "blocking_reasons": list(snapshot.blocking_reasons),
        },
        "datasets": dataset_payloads,
        "evidence_projection": snapshot.to_evidence_payload(),
    }
    unsigned["content_sha256"] = _sha256_value(unsigned)
    # Decode through the same strict path used after a process restart.
    recovered, _ = _decode_artifact(unsigned)
    if recovered != snapshot:
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_snapshot_round_trip_mismatch"
        )
    return unsigned


def _decode_dataset(
    raw: object,
    *,
    catalog_version: str,
    decision_instant: datetime,
) -> ResearchDatasetSnapshot:
    if not isinstance(raw, Mapping) or set(raw) != _DATASET_KEYS:
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_dataset_fields_invalid"
        )
    dataset_id = _dataset_id(raw.get("dataset_id"))
    role = _nonempty_string(raw.get("role"), field_name="role")
    if role not in {"required_execution", "optional_context"}:
        raise ResearchSnapshotStoreCorruption("research_snapshot_store_role_invalid")
    if raw.get("catalog_version") != catalog_version:
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_dataset_catalog_version_mismatch"
        )
    api_version = _nonempty_string(raw.get("api_version"), field_name="api_version")
    request_id = _nonempty_string(raw.get("request_id"), field_name="request_id")
    source_proof_complete = raw.get("source_proof_complete")
    if type(source_proof_complete) is not bool:
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_source_proof_complete_invalid"
        )
    raw_receipt_id = raw.get("receipt_id")
    receipt_id = (
        None
        if raw_receipt_id is None
        else _nonempty_string(raw_receipt_id, field_name="receipt_id")
    )
    _validate_evidence_semantics(raw)
    reasons = raw.get("reasons")
    if not isinstance(reasons, list):
        raise ResearchSnapshotStoreCorruption("research_snapshot_store_reasons_invalid")
    normalized_reasons: list[str] = []
    for item in reasons:
        normalized = _nonempty_string(item, field_name="reason")
        if normalized in normalized_reasons:
            raise ResearchSnapshotStoreCorruption(
                "research_snapshot_store_reasons_duplicate"
            )
        normalized_reasons.append(normalized)
    raw_data_through = raw.get("data_through")
    data_through = (
        None
        if raw_data_through is None
        else _normalized_instant(raw_data_through, field_name="data_through")
    )
    raw_observed_at = raw.get("observed_at")
    observed_at = (
        None
        if raw_observed_at is None
        else _normalized_instant(raw_observed_at, field_name="observed_at")
    )
    if source_proof_complete and (
        receipt_id is None or data_through is None or observed_at is None
    ):
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_source_proof_semantics_invalid"
        )
    if not source_proof_complete and raw.get("evidence_action") != "reject":
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_source_proof_semantics_invalid"
        )
    if data_through is not None and (
        _aware_instant(data_through, field_name="data_through") > decision_instant
    ):
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_data_through_after_decision"
        )
    if observed_at is not None and (
        _aware_instant(observed_at, field_name="observed_at") > decision_instant
    ):
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_observed_after_decision"
        )
    if raw.get("next_cursor") is not None:
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_pagination_incomplete"
        )
    row_count = raw.get("row_count")
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 0:
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_row_count_invalid"
        )
    row_pit_sha256 = _sha256(raw.get("row_pit_sha256"), field_name="row_pit_sha256")
    raw_max_row_available_time = raw.get("max_row_available_time")
    max_row_available_time: str | None
    if raw_max_row_available_time is None:
        max_row_available_time = None
        if row_count != 0:
            raise ResearchSnapshotStoreCorruption(
                "research_snapshot_store_max_row_available_time_missing"
            )
    else:
        max_row_available_time = _normalized_instant(
            raw_max_row_available_time,
            field_name="max_row_available_time",
        )
        if raw_max_row_available_time != max_row_available_time or row_count == 0:
            raise ResearchSnapshotStoreCorruption(
                "research_snapshot_store_max_row_available_time_invalid"
            )
        max_available = _aware_instant(
            max_row_available_time,
            field_name="max_row_available_time",
        )
        if (
            observed_at is None
            or max_available > decision_instant
            or (max_available > _aware_instant(observed_at, field_name="observed_at"))
        ):
            raise ResearchSnapshotStoreCorruption(
                "research_snapshot_store_max_row_available_time_after_boundary"
            )
    response_sha256 = _sha256(raw.get("response_sha256"), field_name="response_sha256")
    rows = raw.get("rows")
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise ResearchSnapshotStoreCorruption("research_snapshot_store_rows_invalid")
    rows_json = _canonical_json(rows)
    if len(rows) != row_count:
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_row_count_mismatch"
        )
    if raw.get("rows_sha256") != _sha256_text(rows_json):
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_rows_hash_mismatch"
        )
    if not source_proof_complete and (
        row_count != 0
        or rows
        or max_row_available_time is not None
        or row_pit_sha256 != _sha256_value([])
    ):
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_source_proof_semantics_invalid"
        )
    dataset_unsigned = dict(raw)
    stored_dataset_sha = dataset_unsigned.pop("dataset_artifact_sha256")
    if stored_dataset_sha != _sha256_value(dataset_unsigned):
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_dataset_hash_mismatch"
        )
    return ResearchDatasetSnapshot(
        dataset_id=dataset_id,
        role=role,
        api_version=api_version,
        catalog_version=catalog_version,
        request_id=request_id,
        receipt_id=receipt_id,
        evidence_state=str(raw["evidence_state"]),
        evidence_action=str(raw["evidence_action"]),
        eligible=bool(raw["eligible"]),
        weight=float(raw["weight"]),
        reasons=tuple(normalized_reasons),
        source_proof_complete=source_proof_complete,
        data_through=data_through,
        observed_at=observed_at,
        next_cursor=None,
        row_count=row_count,
        row_pit_sha256=row_pit_sha256,
        max_row_available_time=max_row_available_time,
        response_sha256=response_sha256,
        _rows_json=rows_json,
    )


def _decode_artifact(raw: object) -> tuple[ResearchDataSnapshot, str]:
    if not isinstance(raw, Mapping) or set(raw) != _ARTIFACT_KEYS:
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_artifact_fields_invalid"
        )
    if raw.get("schema_version") != 1 or raw.get("artifact_type") != (
        "research_data_snapshot.v1"
    ):
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_artifact_schema_invalid"
        )
    unsigned = dict(raw)
    content_sha256 = unsigned.pop("content_sha256")
    if content_sha256 != _sha256_value(unsigned):
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_content_hash_mismatch"
        )
    identity = raw.get("identity")
    if not isinstance(identity, Mapping) or set(identity) != _IDENTITY_KEYS:
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_identity_fields_invalid"
        )
    profile_id = _nonempty_string(identity.get("profile_id"), field_name="profile_id")
    catalog_version = _nonempty_string(
        identity.get("catalog_version"), field_name="catalog_version"
    )
    decision_as_of = _normalized_instant(
        identity.get("decision_as_of"), field_name="decision_as_of"
    )
    if identity.get("decision_as_of") != decision_as_of:
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_decision_as_of_not_canonical"
        )
    snapshot_sha256 = _sha256(
        identity.get("snapshot_sha256"), field_name="snapshot_sha256"
    )
    state = raw.get("snapshot_state")
    if not isinstance(state, Mapping) or set(state) != _SNAPSHOT_STATE_KEYS:
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_snapshot_state_fields_invalid"
        )
    if type(state.get("execution_eligible")) is not bool:
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_execution_eligible_invalid"
        )
    raw_blocking = state.get("blocking_reasons")
    if not isinstance(raw_blocking, list):
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_blocking_reasons_invalid"
        )
    blocking_reasons = [
        _nonempty_string(item, field_name="blocking_reason") for item in raw_blocking
    ]
    raw_datasets = raw.get("datasets")
    if not isinstance(raw_datasets, list) or not raw_datasets:
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_datasets_invalid"
        )
    decision_instant = _aware_instant(decision_as_of, field_name="decision_as_of")
    datasets = [
        _decode_dataset(
            item,
            catalog_version=catalog_version,
            decision_instant=decision_instant,
        )
        for item in raw_datasets
    ]
    dataset_ids = [item.dataset_id for item in datasets]
    if len(dataset_ids) != len(set(dataset_ids)):
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_dataset_ids_duplicate"
        )
    if not any(item.role == "required_execution" for item in datasets):
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_required_dataset_missing"
        )
    expected_blocking: list[str] = []
    for dataset in datasets:
        if dataset.role == "required_execution" and dataset.evidence_action != "accept":
            impairment = (
                "deweighted" if dataset.evidence_action == "deweight" else "rejected"
            )
            expected_blocking.append(
                f"required_dataset_{impairment}:{dataset.dataset_id}"
            )
        if not dataset.source_proof_complete:
            expected_blocking.append(
                f"dataset_source_proof_incomplete:{dataset.dataset_id}"
            )
    if blocking_reasons != expected_blocking:
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_blocking_reasons_mismatch"
        )
    if state["execution_eligible"] is not (not expected_blocking):
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_execution_eligibility_mismatch"
        )
    expected_snapshot_sha = _expected_snapshot_hash(
        profile_id=profile_id,
        catalog_version=catalog_version,
        decision_as_of=decision_as_of,
        datasets=datasets,
        blocking_reasons=expected_blocking,
    )
    if snapshot_sha256 != expected_snapshot_sha:
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_snapshot_hash_mismatch"
        )
    recovered = ResearchDataSnapshot(
        profile_id=profile_id,
        catalog_version=catalog_version,
        decision_as_of=decision_as_of,
        datasets=tuple(datasets),
        execution_eligible=bool(state["execution_eligible"]),
        blocking_reasons=tuple(blocking_reasons),
        snapshot_sha256=snapshot_sha256,
    )
    if raw.get("evidence_projection") != recovered.to_evidence_payload():
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_evidence_projection_mismatch"
        )
    return recovered, str(content_sha256)


def _binding_payload(
    snapshot: ResearchDataSnapshot,
    *,
    artifact_content_sha256: str,
) -> dict[str, Any]:
    receipts = dict(
        sorted((item.dataset_id, item.receipt_id) for item in snapshot.datasets)
    )
    unsigned: dict[str, Any] = {
        "schema_version": 1,
        "binding_type": "research_snapshot_decision_binding.v1",
        "decision_identity_sha256": _decision_identity(
            snapshot.profile_id, snapshot.decision_as_of
        ),
        "profile_id": snapshot.profile_id,
        "catalog_version": snapshot.catalog_version,
        "decision_as_of": snapshot.decision_as_of,
        "snapshot_sha256": snapshot.snapshot_sha256,
        "artifact_content_sha256": artifact_content_sha256,
        "receipt_ids": receipts,
        "receipt_ids_sha256": _sha256_value(receipts),
    }
    unsigned["content_sha256"] = _sha256_value(unsigned)
    return unsigned


def _decode_binding(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != _BINDING_KEYS:
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_binding_fields_invalid"
        )
    if raw.get("schema_version") != 1 or raw.get("binding_type") != (
        "research_snapshot_decision_binding.v1"
    ):
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_binding_schema_invalid"
        )
    unsigned = dict(raw)
    stored_content_sha = unsigned.pop("content_sha256")
    if stored_content_sha != _sha256_value(unsigned):
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_binding_hash_mismatch"
        )
    profile_id = _nonempty_string(raw.get("profile_id"), field_name="profile_id")
    decision_as_of = _normalized_instant(
        raw.get("decision_as_of"), field_name="decision_as_of"
    )
    if raw.get("decision_as_of") != decision_as_of:
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_decision_as_of_not_canonical"
        )
    expected_identity = _decision_identity(profile_id, decision_as_of)
    if raw.get("decision_identity_sha256") != expected_identity:
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_decision_identity_mismatch"
        )
    _nonempty_string(raw.get("catalog_version"), field_name="catalog_version")
    _sha256(raw.get("snapshot_sha256"), field_name="snapshot_sha256")
    _sha256(raw.get("artifact_content_sha256"), field_name="artifact_content_sha256")
    receipts = _validate_receipt_ids(raw.get("receipt_ids"))
    if raw.get("receipt_ids") != receipts:
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_receipt_ids_not_canonical"
        )
    if raw.get("receipt_ids_sha256") != _sha256_value(receipts):
        raise ResearchSnapshotStoreCorruption(
            "research_snapshot_store_receipt_ids_hash_mismatch"
        )
    return dict(raw)


class FileResearchSnapshotStore:
    """Explicit filesystem store for one immutable snapshot per decision."""

    def __init__(self, root: Path | str) -> None:
        if not isinstance(root, (str, os.PathLike)) or not os.fspath(root):
            raise ValueError(
                "research snapshot store root must be explicitly configured"
            )
        raw_path = Path(os.fspath(root))
        if ".." in raw_path.parts:
            raise ValueError("research snapshot store root path traversal forbidden")
        self.root = Path(os.path.abspath(os.fspath(raw_path)))
        _assert_safe_path(self.root)

    def _prepare_root(self) -> None:
        _assert_safe_path(self.root)
        existed = self.root.exists()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ResearchSnapshotStoreCorruption(
                "research_snapshot_store_root_unavailable"
            ) from exc
        _assert_safe_path(self.root)
        try:
            mode = self.root.lstat().st_mode
        except OSError as exc:
            raise ResearchSnapshotStoreCorruption(
                "research_snapshot_store_root_unavailable"
            ) from exc
        if not stat.S_ISDIR(mode):
            raise ResearchSnapshotStoreCorruption(
                "research_snapshot_store_root_not_directory"
            )
        if not existed:
            _fsync_directory(self.root.parent)
        _fsync_directory(self.root)

    def _artifact_path(self, snapshot_sha256: str) -> Path:
        validated = _sha256(snapshot_sha256, field_name="snapshot_sha256")
        return self.root / f"snapshot-{validated}.json"

    def _binding_path(self, decision_identity_sha256: str) -> Path:
        validated = _sha256(
            decision_identity_sha256, field_name="decision_identity_sha256"
        )
        return self.root / f"decision-{validated}.json"

    def _lock_path(self, decision_identity_sha256: str) -> Path:
        validated = _sha256(
            decision_identity_sha256, field_name="decision_identity_sha256"
        )
        return self.root / f".decision-{validated}.lock"

    @contextmanager
    def _locked(
        self,
        decision_identity_sha256: str,
        *,
        exclusive: bool,
    ) -> Iterator[None]:
        self._prepare_root()
        lock_path = self._lock_path(decision_identity_sha256)
        _assert_safe_path(lock_path)
        flags = os.O_RDWR | os.O_CREAT | _no_follow_flag()
        try:
            fd = os.open(os.fspath(lock_path), flags, 0o600)
        except OSError as exc:
            raise ResearchSnapshotStoreCorruption(
                "research_snapshot_store_lock_unavailable"
            ) from exc
        try:
            _same_file_identity(fd, lock_path, kind="lock")
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            _same_file_identity(fd, lock_path, kind="lock")
            yield
            _same_file_identity(fd, lock_path, kind="lock")
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _read_json(self, path: Path, *, kind: str) -> dict[str, Any]:
        _assert_safe_path(path)
        flags = os.O_RDONLY | _no_follow_flag()
        try:
            fd = os.open(os.fspath(path), flags)
        except OSError as exc:
            raise ResearchSnapshotStoreCorruption(
                f"research_snapshot_store_{kind}_unavailable"
            ) from exc
        try:
            before = _same_file_identity(fd, path, kind=kind)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = _same_file_identity(fd, path, kind=kind)
            if (
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ResearchSnapshotStoreCorruption(
                    f"research_snapshot_store_{kind}_changed_during_read"
                )
        finally:
            os.close(fd)
        try:
            text = b"".join(chunks).decode("utf-8")
            raw = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResearchSnapshotStoreCorruption(
                f"research_snapshot_store_{kind}_malformed"
            ) from exc
        if not isinstance(raw, dict) or _canonical_json(raw) != text:
            raise ResearchSnapshotStoreCorruption(
                f"research_snapshot_store_{kind}_not_canonical"
            )
        return raw

    def _atomic_create(self, path: Path, payload: Mapping[str, Any]) -> None:
        _assert_safe_path(path)
        encoded = _canonical_json(payload).encode("utf-8")
        temporary = self.root / f".tmp-{uuid.uuid4().hex}.json"
        _assert_safe_path(temporary)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow_flag()
        fd: int | None = None
        try:
            fd = os.open(os.fspath(temporary), flags, 0o600)
            _same_file_identity(fd, temporary, kind="temporary")
            offset = 0
            while offset < len(encoded):
                written = os.write(fd, encoded[offset:])
                if written <= 0:
                    raise ResearchSnapshotStoreCorruption(
                        "research_snapshot_store_short_write"
                    )
                offset += written
            os.fsync(fd)
            _same_file_identity(fd, temporary, kind="temporary")
            os.close(fd)
            fd = None
            os.link(temporary, path, follow_symlinks=False)
            os.unlink(temporary)
            _fsync_directory(self.root)
            final_fd = os.open(os.fspath(path), os.O_RDONLY | _no_follow_flag())
            try:
                _same_file_identity(final_fd, path, kind="published")
                os.fsync(final_fd)
            finally:
                os.close(final_fd)
        except ResearchSnapshotStoreCorruption:
            if fd is not None:
                os.close(fd)
            try:
                temporary.unlink(missing_ok=True)
            finally:
                if self.root.exists():
                    _fsync_directory(self.root)
            raise
        except OSError as exc:
            if fd is not None:
                os.close(fd)
            try:
                temporary.unlink(missing_ok=True)
            finally:
                if self.root.exists():
                    _fsync_directory(self.root)
            raise ResearchSnapshotStoreCorruption(
                "research_snapshot_store_atomic_write_failed"
            ) from exc

    def _read_artifact(
        self,
        snapshot_sha256: str,
    ) -> tuple[ResearchDataSnapshot, str, dict[str, Any]]:
        path = self._artifact_path(snapshot_sha256)
        raw = self._read_json(path, kind="artifact")
        snapshot, content_sha = _decode_artifact(raw)
        if snapshot.snapshot_sha256 != snapshot_sha256:
            raise ResearchSnapshotStoreCorruption(
                "research_snapshot_store_snapshot_sha256_mismatch"
            )
        return snapshot, content_sha, raw

    def compare_and_swap(
        self,
        *,
        snapshot: ResearchDataSnapshot,
        expected_snapshot_sha256: str | None,
    ) -> None:
        artifact = _artifact_payload(snapshot)
        if expected_snapshot_sha256 is not None:
            _sha256(
                expected_snapshot_sha256,
                field_name="expected_snapshot_sha256",
            )
        identity_sha = _decision_identity(snapshot.profile_id, snapshot.decision_as_of)
        binding_path = self._binding_path(identity_sha)
        with self._locked(identity_sha, exclusive=True):
            if binding_path.exists():
                binding = _decode_binding(self._read_json(binding_path, kind="binding"))
                current_sha = str(binding["snapshot_sha256"])
                if current_sha == snapshot.snapshot_sha256:
                    if expected_snapshot_sha256 not in (None, current_sha):
                        raise ResearchSnapshotStoreConflict(
                            "research_snapshot_store_compare_and_swap_failed"
                        )
                    recovered, content_sha, raw = self._read_artifact(current_sha)
                    if recovered != snapshot or raw != artifact:
                        raise ResearchSnapshotStoreCorruption(
                            "research_snapshot_store_idempotent_replay_mismatch"
                        )
                    if binding["artifact_content_sha256"] != content_sha:
                        raise ResearchSnapshotStoreCorruption(
                            "research_snapshot_store_binding_artifact_mismatch"
                        )
                    if binding != _binding_payload(
                        snapshot,
                        artifact_content_sha256=content_sha,
                    ):
                        raise ResearchSnapshotStoreCorruption(
                            "research_snapshot_store_binding_payload_mismatch"
                        )
                    return
                if expected_snapshot_sha256 != current_sha:
                    raise ResearchSnapshotStoreConflict(
                        "research_snapshot_store_compare_and_swap_failed"
                    )
                raise ResearchSnapshotStoreConflict(
                    "research_snapshot_store_immutable_decision_conflict"
                )
            if expected_snapshot_sha256 is not None:
                raise ResearchSnapshotStoreConflict(
                    "research_snapshot_store_compare_and_swap_failed"
                )

            artifact_path = self._artifact_path(snapshot.snapshot_sha256)
            if artifact_path.exists():
                recovered, content_sha, raw = self._read_artifact(
                    snapshot.snapshot_sha256
                )
                if recovered != snapshot or raw != artifact:
                    raise ResearchSnapshotStoreCorruption(
                        "research_snapshot_store_orphan_artifact_mismatch"
                    )
            else:
                self._atomic_create(artifact_path, artifact)
                recovered, content_sha, raw = self._read_artifact(
                    snapshot.snapshot_sha256
                )
                if recovered != snapshot or raw != artifact:
                    raise ResearchSnapshotStoreCorruption(
                        "research_snapshot_store_published_artifact_mismatch"
                    )
            binding = _binding_payload(
                snapshot,
                artifact_content_sha256=content_sha,
            )
            self._atomic_create(binding_path, binding)
            if (
                _decode_binding(self._read_json(binding_path, kind="binding"))
                != binding
            ):
                raise ResearchSnapshotStoreCorruption(
                    "research_snapshot_store_published_binding_mismatch"
                )

    def load_bound_decision(
        self,
        *,
        profile_id: str,
        decision_as_of: datetime | str,
        catalog_version: str,
    ) -> ResearchDataSnapshot | None:
        """Recover a previously frozen decision without provider re-query.

        The immutable decision binding is the replay authority.  A missing
        binding means the provider stage has never committed; any present but
        inconsistent binding is corruption and must fail closed.
        """

        expected_profile = _nonempty_string(profile_id, field_name="profile_id")
        expected_as_of = _normalized_instant(
            decision_as_of, field_name="decision_as_of"
        )
        expected_catalog = _nonempty_string(
            catalog_version, field_name="catalog_version"
        )
        _assert_safe_path(self.root)
        if not self.root.exists():
            return None
        identity_sha = _decision_identity(expected_profile, expected_as_of)
        binding_path = self._binding_path(identity_sha)
        with self._locked(identity_sha, exclusive=False):
            if not binding_path.exists():
                return None
            binding = _decode_binding(self._read_json(binding_path, kind="binding"))
            if binding["profile_id"] != expected_profile:
                raise ResearchSnapshotStoreCorruption(
                    "research_snapshot_store_profile_id_mismatch"
                )
            if binding["decision_as_of"] != expected_as_of:
                raise ResearchSnapshotStoreCorruption(
                    "research_snapshot_store_decision_as_of_mismatch"
                )
            if binding["catalog_version"] != expected_catalog:
                raise ResearchSnapshotStoreCorruption(
                    "research_snapshot_store_catalog_version_mismatch"
                )
            snapshot_sha256 = str(binding["snapshot_sha256"])
            receipt_ids = dict(binding["receipt_ids"])
        return self.load(
            profile_id=expected_profile,
            decision_as_of=expected_as_of,
            expected_snapshot_sha256=snapshot_sha256,
            catalog_version=expected_catalog,
            receipt_ids=receipt_ids,
        )

    def load(
        self,
        *,
        profile_id: str,
        decision_as_of: datetime | str,
        expected_snapshot_sha256: str,
        catalog_version: str,
        receipt_ids: Mapping[str, str | None],
    ) -> ResearchDataSnapshot:
        expected_profile = _nonempty_string(profile_id, field_name="profile_id")
        expected_as_of = _normalized_instant(
            decision_as_of, field_name="decision_as_of"
        )
        expected_sha = _sha256(
            expected_snapshot_sha256, field_name="expected_snapshot_sha256"
        )
        expected_catalog = _nonempty_string(
            catalog_version, field_name="catalog_version"
        )
        expected_receipts = _validate_receipt_ids(receipt_ids)
        identity_sha = _decision_identity(expected_profile, expected_as_of)
        with self._locked(identity_sha, exclusive=False):
            expected_binding_path = self._binding_path(identity_sha)
            binding: dict[str, Any] | None = None
            if expected_binding_path.exists():
                binding = _decode_binding(
                    self._read_json(expected_binding_path, kind="binding")
                )
                if binding["snapshot_sha256"] != expected_sha:
                    raise ResearchSnapshotStoreCorruption(
                        "research_snapshot_store_snapshot_sha256_mismatch"
                    )
            recovered, artifact_content_sha, _ = self._read_artifact(expected_sha)
            if recovered.profile_id != expected_profile:
                raise ResearchSnapshotStoreCorruption(
                    "research_snapshot_store_profile_id_mismatch"
                )
            if recovered.decision_as_of != expected_as_of:
                raise ResearchSnapshotStoreCorruption(
                    "research_snapshot_store_decision_as_of_mismatch"
                )
            if recovered.catalog_version != expected_catalog:
                raise ResearchSnapshotStoreCorruption(
                    "research_snapshot_store_catalog_version_mismatch"
                )
            actual_receipts = dict(
                sorted(
                    (item.dataset_id, item.receipt_id) for item in recovered.datasets
                )
            )
            if actual_receipts != expected_receipts:
                raise ResearchSnapshotStoreCorruption(
                    "research_snapshot_store_receipt_ids_mismatch"
                )
            if recovered.snapshot_sha256 != expected_sha:
                raise ResearchSnapshotStoreCorruption(
                    "research_snapshot_store_snapshot_sha256_mismatch"
                )
            actual_identity_sha = _decision_identity(
                recovered.profile_id, recovered.decision_as_of
            )
            binding_path = self._binding_path(actual_identity_sha)
            if binding is None:
                if not binding_path.exists():
                    raise ResearchSnapshotStoreCorruption(
                        "research_snapshot_store_decision_binding_missing"
                    )
                binding = _decode_binding(self._read_json(binding_path, kind="binding"))
            if (
                binding["profile_id"] != recovered.profile_id
                or binding["catalog_version"] != recovered.catalog_version
                or binding["decision_as_of"] != recovered.decision_as_of
                or binding["snapshot_sha256"] != recovered.snapshot_sha256
                or binding["artifact_content_sha256"] != artifact_content_sha
                or binding["receipt_ids"] != actual_receipts
            ):
                raise ResearchSnapshotStoreCorruption(
                    "research_snapshot_store_binding_artifact_mismatch"
                )
            return recovered


__all__ = [
    "FileResearchSnapshotStore",
    "ResearchSnapshotStoreConflict",
    "ResearchSnapshotStoreCorruption",
]
