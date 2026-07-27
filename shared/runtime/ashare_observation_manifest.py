#!/usr/bin/env python3
"""Build one catalog-bound A-share current-observation manifest.

The builder discovers TradingDatas through ``GET /v1/catalog`` and only turns
three explicitly reviewed A-share core datasets into an observation manifest.
All other active catalog rows are frozen as inventory, never auto-promoted.
Core query metadata must independently pass the dataset Evidence Gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from shared.data.evidence_gate import (
    DataEvidenceGate,
    DatasetEvidencePolicy,
    EvidenceAction,
)
from shared.data.sharedsignals_v1 import (
    CATALOG_PATH,
    HTTPTransport,
    QueryEnvelope,
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
    SharedSignalsV1Error,
    parse_catalog_envelope,
)
from shared.data.tradingdatas_pagination import (
    collect_query_pages,
)
from shared.runtime_test.sharedsignals_v1_integration_probe import (
    IntegrationProbeConfigurationError,
    SharedSignalsIntegrationProbeConfig,
    load_probe_manifest,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_CORE_DATASETS = {
    "trade_calendar": "cn.market.trade_calendar",
    "security_master": "cn.equity.security_master",
    "daily_bars": "cn.equity.daily",
}
_CORE_FIELDS = {
    "trade_calendar": ("exchange", "cal_date", "is_open", "pretrade_date"),
    "security_master": ("ts_code", "name", "list_status", "list_date"),
    "daily_bars": ("ts_code", "trade_date", "close", "vol", "amount"),
}
_IDENTITY_FIELDS = {
    "trade_calendar": ("exchange", "cal_date"),
    "security_master": ("ts_code",),
    "daily_bars": ("ts_code", "trade_date"),
}
_MAX_PAGES = {
    "trade_calendar": 4,
    "security_master": 20,
    "daily_bars": 20,
}
_MAX_ROWS = {
    "trade_calendar": 2_000,
    "security_master": 10_000,
    "daily_bars": 10_000,
}


class AshareObservationManifestConfigurationError(ValueError):
    """The local manifest build boundary is invalid."""


class AshareObservationManifestBlocked(RuntimeError):
    """A current catalog/query fact safely blocked manifest publication."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


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
        raise AshareObservationManifestConfigurationError(
            "manifest_value_not_canonical_json"
        ) from exc


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _nonempty(value: object, *, reason: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AshareObservationManifestBlocked(reason)
    return value


def _absolute_external_directory(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise AshareObservationManifestConfigurationError(
            "manifest_root_must_be_absolute"
        )
    try:
        path.relative_to(_REPO_ROOT)
    except ValueError:
        pass
    else:
        raise AshareObservationManifestConfigurationError(
            "manifest_root_must_be_repository_external"
        )
    return path


@dataclass(frozen=True)
class AshareObservationManifestBuildConfig:
    base_url: str
    access_policy_id: str
    transport_id: str
    timeout_seconds: float
    manifest_root: Path
    decision_as_of: datetime
    real_trading_enabled: bool = False

    def __post_init__(self) -> None:
        if self.real_trading_enabled is not False:
            raise AshareObservationManifestConfigurationError(
                "real_trading_must_remain_disabled"
            )
        if (
            not isinstance(self.access_policy_id, str)
            or not self.access_policy_id
            or self.access_policy_id != self.access_policy_id.strip()
        ):
            raise AshareObservationManifestConfigurationError(
                "access_policy_id_invalid"
            )
        if self.transport_id != "http-json-v1":
            raise AshareObservationManifestConfigurationError(
                "transport_id_must_equal_http_json_v1"
            )
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
        ):
            raise AshareObservationManifestConfigurationError(
                "timeout_seconds_invalid"
            )
        if (
            not isinstance(self.decision_as_of, datetime)
            or self.decision_as_of.tzinfo is None
            or self.decision_as_of.utcoffset() is None
        ):
            raise AshareObservationManifestConfigurationError(
                "decision_as_of_must_be_timezone_aware"
            )
        object.__setattr__(
            self,
            "manifest_root",
            _absolute_external_directory(Path(self.manifest_root)),
        )
        try:
            probe = SharedSignalsV1Config(
                base_url=self.base_url,
                expected_catalog_version="catalog-discovery-placeholder",
                dataset_ids=frozenset(_CORE_DATASETS.values()),
                access_policy_id=self.access_policy_id,
                timeout_seconds=self.timeout_seconds,
                max_limit=500,
                cache_ttl_seconds=0,
            )
        except (TypeError, ValueError, SharedSignalsV1Error) as exc:
            raise AshareObservationManifestConfigurationError(
                "tradingdatas_client_config_invalid"
            ) from exc
        object.__setattr__(self, "base_url", probe.base_url)


@dataclass(frozen=True)
class AshareObservationManifestBuildResult:
    observation_session: str
    catalog_version: str
    catalog_counts: Mapping[str, int]
    active_contract_sha256: str
    manifest_sha256: str
    current_manifest_path: Path
    archive_manifest_path: Path
    catalog_snapshot_path: Path
    build_receipt_path: Path
    reused: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "pass",
            "blocking": False,
            "observation_session": self.observation_session,
            "catalog_version": self.catalog_version,
            "catalog_counts": dict(self.catalog_counts),
            "active_contract_sha256": self.active_contract_sha256,
            "manifest_sha256": self.manifest_sha256,
            "current_manifest_path": str(self.current_manifest_path),
            "archive_manifest_path": str(self.archive_manifest_path),
            "catalog_snapshot_path": str(self.catalog_snapshot_path),
            "build_receipt_path": str(self.build_receipt_path),
            "reused": self.reused,
            "historical_pit_eligible": False,
            "execution_authority": False,
            "simulation_started": False,
            "real_trading_enabled": False,
        }


def _activation_state(row: Mapping[str, Any], *, dataset_id: str) -> str:
    availability = row.get("availability")
    if not isinstance(availability, Mapping):
        raise AshareObservationManifestBlocked(
            f"catalog_activation_invalid:{dataset_id}"
        )
    raw_states = availability.get("activation_states")
    if not isinstance(raw_states, list) or len(raw_states) != 1:
        raise AshareObservationManifestBlocked(
            f"catalog_activation_invalid:{dataset_id}"
        )
    state = _nonempty(
        raw_states[0],
        reason=f"catalog_activation_invalid:{dataset_id}",
    ).lower()
    if state not in {"active", "paused"}:
        raise AshareObservationManifestBlocked(
            f"catalog_activation_invalid:{dataset_id}"
        )
    return state


def _catalog_inventory(
    rows: tuple[dict[str, Any], ...],
) -> tuple[
    dict[str, dict[str, Any]],
    tuple[dict[str, Any], ...],
    dict[str, int],
    str,
]:
    by_id: dict[str, dict[str, Any]] = {}
    active_rows: list[dict[str, Any]] = []
    paused = 0
    for row in rows:
        dataset_id = _nonempty(
            row.get("dataset_id"),
            reason="catalog_dataset_id_invalid",
        )
        if dataset_id in by_id:
            raise AshareObservationManifestBlocked("catalog_dataset_id_duplicate")
        copied = json.loads(_canonical_json(row))
        by_id[dataset_id] = copied
        state = _activation_state(copied, dataset_id=dataset_id)
        if state == "active":
            active_rows.append(copied)
        else:
            paused += 1
    active_rows.sort(key=lambda item: item["dataset_id"])
    counts = {
        "total": len(rows),
        "active": len(active_rows),
        "paused": paused,
    }
    if counts["total"] != counts["active"] + counts["paused"]:
        raise AshareObservationManifestBlocked("catalog_counts_invalid")
    contract_rows = []
    for row in active_rows:
        contract_rows.append(
            {
                key: row.get(key)
                for key in (
                    "dataset_id",
                    "schema_major",
                    "default_fields",
                    "default_order",
                    "fields",
                    "filter_operators",
                    "limits",
                    "availability",
                    "queryability",
                )
            }
        )
    return by_id, tuple(active_rows), counts, _sha256(contract_rows)


def _catalog_row_contract(
    row: Mapping[str, Any],
    *,
    role: str,
) -> tuple[int, tuple[str, ...], tuple[str, ...], int]:
    dataset_id = _CORE_DATASETS[role]
    if _activation_state(row, dataset_id=dataset_id) != "active":
        raise AshareObservationManifestBlocked(
            f"core_dataset_not_active:{dataset_id}"
        )
    schema_major = row.get("schema_major")
    if type(schema_major) is not int or schema_major <= 0:
        raise AshareObservationManifestBlocked(
            f"core_dataset_schema_invalid:{dataset_id}"
        )
    raw_fields = row.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise AshareObservationManifestBlocked(
            f"core_dataset_fields_invalid:{dataset_id}"
        )
    selectable: set[str] = set()
    for field in raw_fields:
        if not isinstance(field, Mapping):
            raise AshareObservationManifestBlocked(
                f"core_dataset_fields_invalid:{dataset_id}"
            )
        name = field.get("name")
        if (
            isinstance(name, str)
            and name
            and name == name.strip()
            and field.get("selectable") is True
        ):
            selectable.add(name)
    required_fields = _CORE_FIELDS[role]
    if set(required_fields).difference(selectable):
        raise AshareObservationManifestBlocked(
            f"core_dataset_fields_missing:{dataset_id}"
        )
    filter_operators = row.get("filter_operators")
    needed_filter = {
        "trade_calendar": ("exchange", "eq"),
        "security_master": ("list_status", "eq"),
        "daily_bars": ("trade_date", "eq"),
    }[role]
    if (
        not isinstance(filter_operators, Mapping)
        or not isinstance(filter_operators.get(needed_filter[0]), list)
        or needed_filter[1] not in filter_operators[needed_filter[0]]
    ):
        raise AshareObservationManifestBlocked(
            f"core_dataset_filter_contract_invalid:{dataset_id}"
        )
    raw_order = row.get("default_order")
    if not isinstance(raw_order, list) or not raw_order:
        raise AshareObservationManifestBlocked(
            f"core_dataset_order_invalid:{dataset_id}"
        )
    order: list[str] = []
    for value in raw_order:
        order.append(
            _nonempty(
                value,
                reason=f"core_dataset_order_invalid:{dataset_id}",
            )
        )
    limits = row.get("limits")
    page_size = limits.get("max_page_size") if isinstance(limits, Mapping) else None
    if type(page_size) is not int or page_size <= 0:
        raise AshareObservationManifestBlocked(
            f"core_dataset_page_limit_invalid:{dataset_id}"
        )
    return schema_major, required_fields, tuple(order), min(page_size, 500)


def _fetch_catalog(
    *,
    config: AshareObservationManifestBuildConfig,
    transport: HTTPTransport,
) -> tuple[str, tuple[dict[str, Any], ...]]:
    response = transport(
        method="GET",
        url=f"{config.base_url}{CATALOG_PATH}",
        headers={"Accept": "application/json"},
        json_body=None,
        timeout_seconds=float(config.timeout_seconds),
    )
    if response.status_code != 200:
        raise AshareObservationManifestBlocked(
            f"catalog_http_status:{response.status_code}"
        )
    try:
        catalog = parse_catalog_envelope(response.json_body)
    except SharedSignalsV1Error as exc:
        raise AshareObservationManifestBlocked("catalog_contract_invalid") from exc
    return catalog.catalog_version, catalog.data


def _accept_core(envelope: QueryEnvelope) -> None:
    gate = DataEvidenceGate(
        {
            envelope.dataset_id: DatasetEvidencePolicy(
                dataset_id=envelope.dataset_id,
            )
        }
    )
    decision = gate.evaluate(envelope)
    if (
        decision.action is not EvidenceAction.ACCEPT
        or decision.eligible is not True
        or decision.weight != 1.0
    ):
        raise AshareObservationManifestBlocked(
            f"core_dataset_evidence_rejected:{envelope.dataset_id}"
        )


def _latest_completed_session(
    *,
    rows: tuple[dict[str, Any], ...],
    decision_as_of: datetime,
) -> str:
    local_decision = decision_as_of.astimezone(_SHANGHAI)
    if (local_decision.hour, local_decision.minute, local_decision.second) < (
        15,
        0,
        0,
    ):
        raise AshareObservationManifestBlocked("post_close_manifest_required")
    open_sessions: list[str] = []
    for row in rows:
        cal_date = row.get("cal_date")
        is_open = row.get("is_open")
        if (
            not isinstance(cal_date, str)
            or len(cal_date.replace("-", "")) < 8
            or type(is_open) not in {int, str}
            or str(is_open) not in {"0", "1"}
        ):
            raise AshareObservationManifestBlocked("trade_calendar_rows_invalid")
        normalized = cal_date.replace("-", "")[:8]
        try:
            session_date = datetime.strptime(normalized, "%Y%m%d").date()
        except ValueError as exc:
            raise AshareObservationManifestBlocked(
                "trade_calendar_rows_invalid"
            ) from exc
        if str(is_open) == "1" and session_date <= local_decision.date():
            open_sessions.append(normalized)
    if not open_sessions:
        raise AshareObservationManifestBlocked("trade_calendar_open_session_missing")
    return max(open_sessions)


def _manifest_payload(
    *,
    config: AshareObservationManifestBuildConfig,
    catalog_version: str,
    active_contract_sha256: str,
    contracts: Mapping[str, tuple[int, tuple[str, ...], tuple[str, ...], int]],
    session: str,
) -> dict[str, Any]:
    datasets: list[dict[str, Any]] = []
    for role in ("trade_calendar", "security_master", "daily_bars"):
        schema_major, fields, order, limit = contracts[role]
        dataset_id = _CORE_DATASETS[role]
        filters = {
            "trade_calendar": {"exchange": {"eq": "SSE"}},
            "security_master": {"list_status": {"eq": "L"}},
            "daily_bars": {"trade_date": {"eq": session}},
        }[role]
        payload: dict[str, Any] = {
            "probe_role": role,
            "dataset_id": dataset_id,
            "schema_major": schema_major,
            "requirement_role": "required_execution",
            "fields": list(fields),
            "filters": filters,
            "order": list(order),
            "limit": limit,
            "minimum_row_count": 1,
            "identity_fields": list(_IDENTITY_FIELDS[role]),
            "observation_mode": "current_observation",
            "query_as_of_mode": (
                "omit" if role == "security_master" else "decision_as_of"
            ),
            "max_pages": _MAX_PAGES[role],
            "max_rows": _MAX_ROWS[role],
            "degraded_action": "reject",
            "stale_action": "reject",
            "degraded_weight": 0.25,
            "stale_weight": 0.10,
        }
        if role in {"trade_calendar", "daily_bars"}:
            payload.update(
                {
                    "row_event_time_field": (
                        "cal_date" if role == "trade_calendar" else "trade_date"
                    ),
                    "row_event_time_format": "yyyymmdd",
                    "row_event_timezone": "Asia/Shanghai",
                    "row_event_time_semantic": (
                        "scheduled" if role == "trade_calendar" else "session"
                    ),
                }
            )
        datasets.append(payload)
    return {
        "manifest_version": 2,
        "profile_id": (
            "ashare-phase1-current-observation-"
            f"{session}-{active_contract_sha256[:12]}-v1"
        ),
        "base_url": config.base_url,
        "catalog_version": catalog_version,
        "access_policy_id": config.access_policy_id,
        "transport_id": config.transport_id,
        "timeout_seconds": config.timeout_seconds,
        "as_of": config.decision_as_of.isoformat(),
        "expected_probe_roles": [
            "trade_calendar",
            "security_master",
            "daily_bars",
        ],
        "datasets": datasets,
    }


def _ensure_private_directory(path: Path) -> None:
    if path.exists():
        metadata = os.lstat(path)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise AshareObservationManifestConfigurationError(
                "manifest_directory_invalid"
            )
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise AshareObservationManifestConfigurationError(
                "manifest_directory_mode_invalid"
            )
        return
    try:
        path.mkdir(mode=0o700)
    except OSError as exc:
        raise AshareObservationManifestConfigurationError(
            "manifest_directory_create_failed"
        ) from exc


def _fsync_directory(path: Path) -> None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_private_artifact(
    path: Path,
    *,
    invalid_reason: str,
) -> os.stat_result:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise AshareObservationManifestBlocked(invalid_reason) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise AshareObservationManifestBlocked(invalid_reason)
    return metadata


def _read_private_artifact(path: Path, *, invalid_reason: str) -> bytes:
    before = _validate_private_artifact(path, invalid_reason=invalid_reason)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AshareObservationManifestBlocked(invalid_reason) from exc
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise AshareObservationManifestBlocked(invalid_reason)
        chunks: list[bytes] = []
        remaining = opened.st_size + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, encoded: bytes) -> None:
    remaining = memoryview(encoded)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("artifact write made no progress")
        remaining = remaining[written:]


def _write_new(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (_canonical_json(payload) + "\n").encode("utf-8")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        if _read_private_artifact(
            path,
            invalid_reason="immutable_artifact_conflict",
        ) != encoded:
            raise AshareObservationManifestBlocked("immutable_artifact_conflict")
        return
    try:
        _write_all(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600, follow_symlinks=False)
    _validate_private_artifact(
        path,
        invalid_reason="immutable_artifact_publish_invalid",
    )
    _fsync_directory(path.parent)


def _replace_current(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        _validate_private_artifact(
            path,
            invalid_reason="current_manifest_publish_target_invalid",
        )
    encoded = (_canonical_json(payload) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(temporary, flags, 0o600)
        try:
            _write_all(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        os.chmod(path, 0o600, follow_symlinks=False)
        _validate_private_artifact(
            path,
            invalid_reason="current_manifest_publish_invalid",
        )
        _fsync_directory(path.parent)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise AshareObservationManifestConfigurationError(
            "current_manifest_publish_failed"
        ) from exc


def _existing_current(path: Path) -> SharedSignalsIntegrationProbeConfig | None:
    if not path.exists() and not path.is_symlink():
        return None
    _validate_private_artifact(
        path,
        invalid_reason="current_manifest_invalid",
    )
    try:
        return load_probe_manifest(path)
    except IntegrationProbeConfigurationError as exc:
        raise AshareObservationManifestBlocked("current_manifest_invalid") from exc


def _manifest_session(manifest: SharedSignalsIntegrationProbeConfig) -> str:
    daily = [item for item in manifest.datasets if item.probe_role == "daily_bars"]
    if len(daily) != 1:
        raise AshareObservationManifestBlocked("current_manifest_invalid")
    value = daily[0].filters.get("trade_date")
    if not isinstance(value, Mapping) or set(value) != {"eq"}:
        raise AshareObservationManifestBlocked("current_manifest_invalid")
    session = value.get("eq")
    if not isinstance(session, str) or len(session) != 8 or not session.isdigit():
        raise AshareObservationManifestBlocked("current_manifest_invalid")
    return session


def build_ashare_observation_manifest(
    config: AshareObservationManifestBuildConfig,
    *,
    transport: HTTPTransport,
) -> AshareObservationManifestBuildResult:
    """Discover, preflight and atomically publish the next core manifest."""

    if not isinstance(config, AshareObservationManifestBuildConfig):
        raise TypeError("config must be AshareObservationManifestBuildConfig")
    if transport is None or not callable(transport):
        raise AshareObservationManifestConfigurationError(
            "tradingdatas_transport_invalid"
        )

    catalog_version, catalog_rows = _fetch_catalog(
        config=config,
        transport=transport,
    )
    by_id, active_rows, counts, active_contract_sha256 = _catalog_inventory(
        catalog_rows
    )
    contracts: dict[
        str,
        tuple[int, tuple[str, ...], tuple[str, ...], int],
    ] = {}
    for role, dataset_id in _CORE_DATASETS.items():
        row = by_id.get(dataset_id)
        if row is None:
            raise AshareObservationManifestBlocked(
                f"core_dataset_missing:{dataset_id}"
            )
        if _activation_state(row, dataset_id=dataset_id) != "active":
            raise AshareObservationManifestBlocked(
                f"core_dataset_not_active:{dataset_id}"
            )
        contracts[role] = _catalog_row_contract(row, role=role)

    client = SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url=config.base_url,
            expected_catalog_version=catalog_version,
            dataset_ids=frozenset(_CORE_DATASETS.values()),
            access_policy_id=config.access_policy_id,
            timeout_seconds=config.timeout_seconds,
            max_limit=500,
            cache_ttl_seconds=0,
        ),
        transport=transport,
    )
    calendar_contract = contracts["trade_calendar"]
    calendar_run = collect_query_pages(
        client=client,
        request=QueryRequest(
            dataset_id=_CORE_DATASETS["trade_calendar"],
            schema_major=calendar_contract[0],
            fields=calendar_contract[1],
            filters={"exchange": {"eq": "SSE"}},
            as_of=config.decision_as_of.isoformat(),
            order=calendar_contract[2],
            limit=calendar_contract[3],
        ),
        identity_fields=_IDENTITY_FIELDS["trade_calendar"],
        max_pages=_MAX_PAGES["trade_calendar"],
        max_rows=_MAX_ROWS["trade_calendar"],
    )
    _accept_core(calendar_run.envelope)
    session = _latest_completed_session(
        rows=calendar_run.envelope.data,
        decision_as_of=config.decision_as_of,
    )

    core_envelopes: dict[str, QueryEnvelope] = {
        "trade_calendar": calendar_run.envelope
    }
    for role in ("security_master", "daily_bars"):
        schema_major, fields, order, _ = contracts[role]
        request = QueryRequest(
            dataset_id=_CORE_DATASETS[role],
            schema_major=schema_major,
            fields=fields,
            filters=(
                {"list_status": {"eq": "L"}}
                if role == "security_master"
                else {"trade_date": {"eq": session}}
            ),
            as_of=(
                None
                if role == "security_master"
                else config.decision_as_of.isoformat()
            ),
            order=order,
            limit=1,
        )
        envelope = client.query_uncached(request)
        _accept_core(envelope)
        if len(envelope.data) != 1:
            raise AshareObservationManifestBlocked(
                f"core_dataset_probe_empty:{envelope.dataset_id}"
            )
        if (
            role == "daily_bars"
            and envelope.data[0].get("trade_date") != session
        ):
            raise AshareObservationManifestBlocked(
                "daily_bars_session_probe_mismatch"
            )
        core_envelopes[role] = envelope

    payload = _manifest_payload(
        config=config,
        catalog_version=catalog_version,
        active_contract_sha256=active_contract_sha256,
        contracts=contracts,
        session=session,
    )
    manifest_sha256 = _sha256(payload)

    _ensure_private_directory(config.manifest_root)
    archive_root = config.manifest_root / "archive"
    catalog_root = config.manifest_root / "catalog"
    receipt_root = config.manifest_root / "receipts"
    for path in (archive_root, catalog_root, receipt_root):
        _ensure_private_directory(path)

    catalog_snapshot_payload = {
        "schema_id": "tradingagent.tradingdatas.active-catalog-snapshot.v1",
        "catalog_version": catalog_version,
        "counts": counts,
        "active_contract_sha256": active_contract_sha256,
        "active_catalog_rows": list(active_rows),
        "authority": "tradingdatas_get_v1_catalog",
        "research_auto_promotion": False,
        "execution_authority": False,
        "real_trading_enabled": False,
    }
    catalog_snapshot_sha256 = _sha256(catalog_snapshot_payload)
    catalog_snapshot_path = (
        catalog_root / f"{catalog_snapshot_sha256}.json"
    )
    _write_new(catalog_snapshot_path, catalog_snapshot_payload)

    current_path = config.manifest_root / "current.json"
    existing = _existing_current(current_path)
    reused = False
    if existing is not None and _manifest_session(existing) == session:
        existing_payload = existing.to_manifest_payload()
        comparison_existing = dict(existing_payload)
        comparison_candidate = dict(payload)
        comparison_existing.pop("as_of", None)
        comparison_candidate.pop("as_of", None)
        if (
            existing.catalog_version != catalog_version
            or _canonical_json(comparison_existing)
            != _canonical_json(comparison_candidate)
        ):
            raise AshareObservationManifestBlocked(
                "same_session_catalog_contract_changed"
            )
        payload = existing_payload
        manifest_sha256 = existing.manifest_sha256
        reused = True

    archive_path = archive_root / f"{session}-{manifest_sha256}.json"
    _write_new(archive_path, payload)

    core_evidence = {
        role: {
            "dataset_id": envelope.dataset_id,
            "state": envelope.metadata.state,
            "degraded": envelope.metadata.degraded,
            "receipt_id": envelope.metadata.receipt_id,
            "data_through": envelope.metadata.data_through,
            "observed_at": envelope.metadata.observed_at,
            "lineage_sha256": _sha256(envelope.metadata.lineage),
        }
        for role, envelope in core_envelopes.items()
    }
    receipt_payload = {
        "schema_id": "tradingagent.ashare.observation-manifest-build.v1",
        "status": "pass",
        "blocking": False,
        "observation_session": session,
        "decision_as_of": config.decision_as_of.isoformat(),
        "catalog_version": catalog_version,
        "catalog_counts": counts,
        "active_contract_sha256": active_contract_sha256,
        "catalog_snapshot_sha256": catalog_snapshot_sha256,
        "manifest_sha256": manifest_sha256,
        "core_evidence": core_evidence,
        "non_core_active_research_eligible": False,
        "reused": reused,
        "historical_pit_eligible": False,
        "execution_authority": False,
        "simulation_started": False,
        "real_trading_enabled": False,
    }
    receipt_sha256 = _sha256(receipt_payload)
    build_receipt_path = receipt_root / f"{session}-{receipt_sha256}.json"
    _write_new(build_receipt_path, receipt_payload)

    if not reused:
        _replace_current(current_path, payload)
        try:
            loaded = load_probe_manifest(current_path)
        except IntegrationProbeConfigurationError as exc:
            raise AshareObservationManifestBlocked(
                "published_manifest_invalid"
            ) from exc
        if loaded.manifest_sha256 != manifest_sha256:
            raise AshareObservationManifestBlocked(
                "published_manifest_hash_mismatch"
            )

    return AshareObservationManifestBuildResult(
        observation_session=session,
        catalog_version=catalog_version,
        catalog_counts=counts,
        active_contract_sha256=active_contract_sha256,
        manifest_sha256=manifest_sha256,
        current_manifest_path=current_path,
        archive_manifest_path=archive_path,
        catalog_snapshot_path=catalog_snapshot_path,
        build_receipt_path=build_receipt_path,
        reused=reused,
    )


__all__ = [
    "AshareObservationManifestBlocked",
    "AshareObservationManifestBuildConfig",
    "AshareObservationManifestBuildResult",
    "AshareObservationManifestConfigurationError",
    "build_ashare_observation_manifest",
]
