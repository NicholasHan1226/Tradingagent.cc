"""Fail-closed A-share current-observation runtime.

The runtime consumes only the frozen TradingDatas catalog/query contract.  It
requires a complete bounded, same-observation integration probe before it
publishes one immutable research snapshot.  The snapshot remains current
observation evidence and never gains trading, capital, or historical-PIT
authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from shared.data.evidence_gate import DataEvidenceGate
from shared.data.research_snapshot import (
    ResearchDataContractError,
    ResearchDataSnapshot,
    build_research_data_snapshot,
)
from shared.data.research_snapshot_store import (
    FileResearchSnapshotStore,
    ResearchSnapshotStoreConflict,
    ResearchSnapshotStoreCorruption,
)
from shared.data.sharedsignals_v1 import HTTPTransport, SharedSignalsV1Client
from shared.data.tradingdatas_pagination import (
    PaginationContractError,
    collect_query_pages,
)
from shared.data.tradingdatas_transport import build_runtime_transport
from shared.runtime_test.sharedsignals_v1_integration_probe import (
    IntegrationProbeConfigurationError,
    SharedSignalsIntegrationProbeConfig,
    load_probe_manifest,
    run_sharedsignals_integration_probe,
    write_probe_receipt,
)
from shared.universe.policy import classify_instrument, is_mainboard_tradable


OBSERVATION_SCHEMA_ID = "tradingagent.ashare.current-observation.v1"
OBSERVATION_RECEIPT_SCHEMA_ID = "tradingagent.ashare.observation-receipt.v1"
_ALLOWED_CONTEXT_ROLES = frozenset(
    {
        "industry_classification",
        "industry_daily_context",
        "industry_context",
        "index_context",
        "market_breadth",
        "sector_context",
    }
)
_PLAINTEXT_TOKEN_ENV_NAMES = (
    "TRADINGDATAS_API_TOKEN",
    "TRADINGDATAS_BEARER_TOKEN",
    "TRADINGDATAS_TOKEN",
)
_MIN_LISTING_AGE_DAYS = 30
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class AshareObservationConfigurationError(ValueError):
    """Raised when a local observation authority input is not explicit."""


class AshareObservationBlocked(RuntimeError):
    """Controlled fail-closed stop before a usable observation is published."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _absolute_path(value: object, *, field_name: str) -> Path:
    if not isinstance(value, (str, os.PathLike)) or not os.fspath(value):
        raise AshareObservationConfigurationError(f"{field_name}_must_be_absolute")
    candidate = Path(os.fspath(value))
    if not candidate.is_absolute() or ".." in candidate.parts or not candidate.name:
        raise AshareObservationConfigurationError(f"{field_name}_must_be_absolute")
    return candidate


def _repository_external_path(value: object, *, field_name: str) -> Path:
    candidate = _absolute_path(value, field_name=field_name)
    try:
        candidate.resolve(strict=False).relative_to(_REPOSITORY_ROOT)
    except ValueError:
        return candidate
    raise AshareObservationConfigurationError(
        f"{field_name}_must_be_repository_external"
    )


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def assert_no_plaintext_token_environment(environment: Mapping[str, str]) -> None:
    """Reject known plaintext token variables even when a token file is set."""

    for name in _PLAINTEXT_TOKEN_ENV_NAMES:
        if name in environment:
            raise AshareObservationConfigurationError(
                "plaintext_token_environment_forbidden"
            )


@dataclass(frozen=True)
class AshareObservationConfig:
    """Explicit local paths and hard simulation boundary for one run."""

    manifest_path: Path | str
    token_file: Path | str = field(repr=False)
    snapshot_root: Path | str
    marketgraph_mode: str = "mg_off"
    real_trading_enabled: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "manifest_path",
            _repository_external_path(
                self.manifest_path,
                field_name="manifest_path",
            ),
        )
        object.__setattr__(
            self,
            "token_file",
            _repository_external_path(
                self.token_file,
                field_name="token_file",
            ),
        )
        object.__setattr__(
            self,
            "snapshot_root",
            _absolute_path(self.snapshot_root, field_name="snapshot_root"),
        )
        if self.marketgraph_mode != "mg_off":
            raise AshareObservationConfigurationError("marketgraph_mode_must_be_mg_off")
        if type(self.real_trading_enabled) is not bool or self.real_trading_enabled:
            raise AshareObservationConfigurationError("real_trading_must_be_disabled")


@dataclass(frozen=True)
class AshareObservationResult:
    """Secret-free readback for one immutable observation binding."""

    status: str
    mode: str
    marketgraph_mode: str
    real_trading_enabled: bool
    historical_pit_eligible: bool
    execution_authority: bool
    profile_id: str
    catalog_version: str
    decision_as_of: str
    snapshot_sha256: str
    probe_receipt_sha256: str
    observation_receipt_sha256: str
    probe_same_as_of_match: bool
    tradable_symbols: tuple[str, ...]
    tradable_universe_count: int
    tradable_universe_sha256: str
    excluded_individual_count: int
    excluded_reason_counts: Mapping[str, int]
    context_probe_roles: tuple[str, ...]
    idempotent_replay: bool
    schema_id: str = OBSERVATION_SCHEMA_ID

    def __post_init__(self) -> None:
        if (
            self.status != "pass"
            or self.mode != "observation_only"
            or self.marketgraph_mode != "mg_off"
            or self.real_trading_enabled is not False
            or self.historical_pit_eligible is not False
            or self.execution_authority is not False
        ):
            raise AshareObservationConfigurationError(
                "observation_result_authority_invalid"
            )
        object.__setattr__(
            self,
            "excluded_reason_counts",
            MappingProxyType(dict(sorted(self.excluded_reason_counts.items()))),
        )

    def to_dict(self, *, include_tradable_symbols: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_id": self.schema_id,
            "status": self.status,
            "mode": self.mode,
            "marketgraph_mode": self.marketgraph_mode,
            "real_trading_enabled": False,
            "historical_pit_eligible": False,
            "execution_authority": False,
            "profile_id": self.profile_id,
            "catalog_version": self.catalog_version,
            "decision_as_of": self.decision_as_of,
            "snapshot_sha256": self.snapshot_sha256,
            "probe_receipt_sha256": self.probe_receipt_sha256,
            "observation_receipt_sha256": self.observation_receipt_sha256,
            "probe_same_as_of_match": self.probe_same_as_of_match,
            "tradable_universe_count": self.tradable_universe_count,
            "tradable_universe_sha256": self.tradable_universe_sha256,
            "excluded_individual_count": self.excluded_individual_count,
            "excluded_reason_counts": dict(self.excluded_reason_counts),
            "context_probe_roles": list(self.context_probe_roles),
            "idempotent_replay": self.idempotent_replay,
        }
        if include_tradable_symbols:
            payload["tradable_symbols"] = list(self.tradable_symbols)
        return payload


TransportFactory = Callable[..., HTTPTransport]


def _load_config(path: Path) -> SharedSignalsIntegrationProbeConfig:
    try:
        return load_probe_manifest(path)
    except IntegrationProbeConfigurationError as exc:
        raise AshareObservationConfigurationError("manifest_contract_invalid") from exc


def _context_probe_roles(
    config: SharedSignalsIntegrationProbeConfig,
) -> tuple[str, ...]:
    roles: list[str] = []
    calendar_specs = [
        item for item in config.datasets if item.probe_role == "trade_calendar"
    ]
    if len(calendar_specs) != 1:
        raise AshareObservationBlocked("trade_calendar_probe_role_required")
    calendar = calendar_specs[0]
    if calendar.requirement_role != "required_execution":
        raise AshareObservationBlocked("trade_calendar_scope_contract_invalid")

    master_specs = [
        item for item in config.datasets if item.probe_role == "security_master"
    ]
    if len(master_specs) != 1:
        raise AshareObservationBlocked("security_master_probe_role_required")
    master = master_specs[0]
    if (
        master.requirement_role != "required_execution"
        or "ts_code" not in master.identity_fields
        or not {"ts_code", "name", "list_status", "list_date"}.issubset(master.fields)
        or master.filters.get("list_status") != {"eq": "L"}
    ):
        raise AshareObservationBlocked("security_master_scope_contract_invalid")

    daily_specs = [item for item in config.datasets if item.probe_role == "daily_bars"]
    if len(daily_specs) != 1:
        raise AshareObservationBlocked("daily_bars_probe_role_required")
    daily = daily_specs[0]
    if (
        daily.requirement_role != "required_execution"
        or "ts_code" not in daily.identity_fields
        or not {"ts_code", "trade_date", "close", "vol"}.issubset(daily.fields)
    ):
        raise AshareObservationBlocked("daily_bars_scope_contract_invalid")
    decision_date = (
        datetime.fromisoformat(config.as_of.replace("Z", "+00:00"))
        .astimezone(_SHANGHAI)
        .strftime("%Y%m%d")
    )
    if daily.filters.get("trade_date") != {"eq": decision_date}:
        raise AshareObservationBlocked("daily_bars_trade_date_filter_required")

    for item in config.datasets:
        is_context_role = item.probe_role in _ALLOWED_CONTEXT_ROLES
        if is_context_role and item.requirement_role != "optional_context":
            raise AshareObservationBlocked(
                "context_probe_role_must_be_optional_context"
            )
        if item.requirement_role == "optional_context":
            if not is_context_role:
                raise AshareObservationBlocked("optional_context_role_not_aggregate")
            roles.append(item.probe_role)
    return tuple(roles)


def _probe_binding_path(
    root: Path,
    config: SharedSignalsIntegrationProbeConfig,
) -> Path:
    identity = _canonical_sha256(
        {
            "profile_id": config.profile_id,
            "catalog_version": config.catalog_version,
            "as_of": config.as_of,
            "manifest_sha256": config.manifest_sha256,
        }
    )
    return root / f"integration-{identity}.json"


def _observation_receipt_path(
    root: Path,
    config: SharedSignalsIntegrationProbeConfig,
) -> Path:
    return root / _probe_binding_path(root, config).name.replace(
        "integration-", "observation-", 1
    )


def _read_private_json(path: Path, *, invalid_reason: str) -> dict[str, Any]:
    if not path.exists():
        raise AshareObservationBlocked(invalid_reason)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AshareObservationBlocked(invalid_reason) from exc
    try:
        metadata = os.fstat(descriptor)
        named_metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not stat.S_ISREG(named_metadata.st_mode)
            or metadata.st_nlink != 1
            or named_metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
            or (metadata.st_dev, metadata.st_ino)
            != (named_metadata.st_dev, named_metadata.st_ino)
            or metadata.st_size > 4_194_304
        ):
            raise AshareObservationBlocked(invalid_reason)
        chunks: list[bytes] = []
        remaining = 4_194_305
        while remaining > 0:
            chunk = os.read(descriptor, min(1_048_576, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as exc:
        raise AshareObservationBlocked(invalid_reason) from exc
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AshareObservationBlocked(invalid_reason) from exc
    if not isinstance(payload, dict):
        raise AshareObservationBlocked(invalid_reason)
    return payload


def _read_probe_receipt(
    path: Path,
    config: SharedSignalsIntegrationProbeConfig,
) -> dict[str, Any]:
    payload = _read_private_json(
        path,
        invalid_reason="replay_probe_receipt_invalid",
    )
    unsigned = dict(payload)
    claimed = unsigned.pop("receipt_sha256", None)
    if claimed != _canonical_sha256(unsigned):
        raise AshareObservationBlocked("replay_probe_receipt_invalid")
    if (
        payload.get("status") != "pass"
        or payload.get("blocking") is not False
        or payload.get("same_as_of_match") is not True
        or payload.get("manifest_sha256") != config.manifest_sha256
        or payload.get("profile_id") != config.profile_id
        or payload.get("catalog_version") != config.catalog_version
        or payload.get("as_of") != config.as_of
    ):
        raise AshareObservationBlocked("replay_probe_receipt_invalid")
    return payload


def _validate_snapshot(
    snapshot: ResearchDataSnapshot,
    config: SharedSignalsIntegrationProbeConfig,
) -> None:
    if (
        snapshot.profile_id != config.profile_id
        or snapshot.catalog_version != config.catalog_version
        or snapshot.profile_contract_sha256 != config.to_profile().contract_sha256
        or snapshot.historical_pit_eligible is not False
        or not snapshot.execution_eligible
        or snapshot.blocking_reasons
        or any(
            item.observation_mode != "current_observation"
            or item.historical_pit_eligible is not False
            for item in snapshot.datasets
        )
    ):
        raise AshareObservationBlocked("research_snapshot_not_eligible")


def _validate_probe_snapshot_binding(
    snapshot: ResearchDataSnapshot,
    probe_receipt: Mapping[str, Any],
    config: SharedSignalsIntegrationProbeConfig,
) -> None:
    probe_datasets = {
        item.get("dataset_id"): item
        for item in probe_receipt.get("datasets", [])
        if isinstance(item, Mapping) and isinstance(item.get("dataset_id"), str)
    }
    snapshot_datasets = {item.dataset_id: item for item in snapshot.datasets}
    expected_ids = {item.dataset_id for item in config.datasets}
    if set(probe_datasets) != expected_ids or set(snapshot_datasets) != expected_ids:
        raise AshareObservationBlocked("replay_probe_snapshot_binding_invalid")
    for dataset_id in sorted(expected_ids):
        probe = probe_datasets[dataset_id]
        frozen = snapshot_datasets[dataset_id]
        if (
            probe.get("identity_sha256") != frozen.identity_sha256
            or probe.get("pagination_semantic_sha256")
            != frozen.pagination_semantic_sha256
            or probe.get("row_count") != frozen.row_count
            or probe.get("page_count") != frozen.page_count
            or probe.get("receipt_id") != frozen.receipt_id
            or probe.get("lineage_sha256") != frozen.lineage_sha256
            or probe.get("source_proof_sha256") != frozen.source_proof_sha256
        ):
            raise AshareObservationBlocked("replay_probe_snapshot_binding_invalid")


def _universe_projection(
    snapshot: ResearchDataSnapshot,
    config: SharedSignalsIntegrationProbeConfig,
) -> tuple[tuple[str, ...], dict[str, int]]:
    daily_spec = next(
        item for item in config.datasets if item.probe_role == "daily_bars"
    )
    daily_snapshot = next(
        item for item in snapshot.datasets if item.dataset_id == daily_spec.dataset_id
    )
    master_spec = next(
        item for item in config.datasets if item.probe_role == "security_master"
    )
    master_snapshot = next(
        item for item in snapshot.datasets if item.dataset_id == master_spec.dataset_id
    )
    master_rows: dict[str, Mapping[str, Any]] = {}
    for row in master_snapshot.decoded_rows():
        symbol = row.get("ts_code")
        if not isinstance(symbol, str) or not symbol.strip() or symbol in master_rows:
            raise AshareObservationBlocked("security_master_rows_invalid")
        master_rows[symbol] = row

    decision_date = (
        datetime.fromisoformat(config.as_of.replace("Z", "+00:00"))
        .astimezone(_SHANGHAI)
        .date()
    )
    tradable: set[str] = set()
    excluded: dict[str, int] = {}

    def exclude(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    for row in daily_snapshot.decoded_rows():
        symbol = row.get("ts_code")
        if row.get("trade_date") != decision_date.strftime("%Y%m%d"):
            raise AshareObservationBlocked("daily_bar_trade_date_mismatch")
        eligibility = classify_instrument(symbol, instrument_type="common_stock")
        if not is_mainboard_tradable(symbol, instrument_type="common_stock"):
            exclude(eligibility.reason_code)
            continue
        master = master_rows.get(eligibility.normalized_symbol)
        if master is None or master.get("list_status") != "L":
            exclude("security_master_missing_or_inactive")
            continue
        name = master.get("name")
        if not isinstance(name, str) or not name.strip():
            exclude("security_master_missing_or_inactive")
            continue
        upper_name = name.upper()
        if "ST" in upper_name or "退" in name:
            exclude("risk_warning_security_excluded")
            continue
        list_date = master.get("list_date")
        if not isinstance(list_date, str):
            exclude("security_master_missing_or_inactive")
            continue
        try:
            listed = datetime.strptime(list_date.replace("-", "")[:8], "%Y%m%d").date()
        except ValueError:
            exclude("security_master_missing_or_inactive")
            continue
        if decision_date - listed < timedelta(days=_MIN_LISTING_AGE_DAYS):
            exclude("new_listing_excluded")
            continue
        close = row.get("close")
        volume = row.get("vol")
        if (
            isinstance(close, bool)
            or not isinstance(close, (int, float))
            or not math.isfinite(float(close))
            or float(close) <= 0.0
            or isinstance(volume, bool)
            or not isinstance(volume, (int, float))
            or not math.isfinite(float(volume))
            or float(volume) <= 0.0
        ):
            exclude("suspended_or_nonpositive_bar_excluded")
            continue
        tradable.add(eligibility.normalized_symbol)
    if not tradable:
        raise AshareObservationBlocked("mainboard_tradable_universe_empty")
    return tuple(sorted(tradable)), dict(sorted(excluded.items()))


def _observation_receipt(
    *,
    snapshot: ResearchDataSnapshot,
    probe_receipt: Mapping[str, Any],
    manifest: SharedSignalsIntegrationProbeConfig,
    tradable: tuple[str, ...],
    excluded: Mapping[str, int],
    context_roles: tuple[str, ...],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_id": OBSERVATION_RECEIPT_SCHEMA_ID,
        "profile_id": snapshot.profile_id,
        "catalog_version": snapshot.catalog_version,
        "decision_as_of": snapshot.decision_as_of,
        "manifest_sha256": manifest.manifest_sha256,
        "snapshot_sha256": snapshot.snapshot_sha256,
        "probe_receipt_sha256": probe_receipt["receipt_sha256"],
        "tradable_universe_count": len(tradable),
        "tradable_universe_sha256": _canonical_sha256(list(tradable)),
        "excluded_reason_counts": dict(sorted(excluded.items())),
        "context_probe_roles": list(context_roles),
        "mode": "observation_only",
        "marketgraph_mode": "mg_off",
        "real_trading_enabled": False,
        "historical_pit_eligible": False,
        "execution_authority": False,
    }
    payload["receipt_sha256"] = _canonical_sha256(payload)
    return payload


def _write_immutable_observation_receipt(
    path: Path,
    receipt: Mapping[str, Any],
) -> None:
    if path.exists() or path.is_symlink():
        existing = _read_private_json(
            path,
            invalid_reason="observation_receipt_invalid",
        )
        if existing != dict(receipt):
            raise AshareObservationBlocked("observation_receipt_conflict")
        return
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
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o600)
    except OSError as exc:
        if created:
            path.unlink(missing_ok=True)
        raise AshareObservationBlocked("observation_receipt_write_failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_observation_receipt(
    path: Path,
    expected: Mapping[str, Any],
) -> None:
    payload = _read_private_json(
        path,
        invalid_reason="observation_receipt_invalid",
    )
    unsigned = dict(payload)
    claimed = unsigned.pop("receipt_sha256", None)
    if claimed != _canonical_sha256(unsigned) or payload != dict(expected):
        raise AshareObservationBlocked("observation_receipt_invalid")


def _result(
    *,
    snapshot: ResearchDataSnapshot,
    probe_receipt: Mapping[str, Any],
    context_roles: tuple[str, ...],
    idempotent_replay: bool,
    config: AshareObservationConfig,
    manifest: SharedSignalsIntegrationProbeConfig,
) -> AshareObservationResult:
    tradable, excluded = _universe_projection(snapshot, manifest)
    observation_receipt = _observation_receipt(
        snapshot=snapshot,
        probe_receipt=probe_receipt,
        manifest=manifest,
        tradable=tradable,
        excluded=excluded,
        context_roles=context_roles,
    )
    return AshareObservationResult(
        status="pass",
        mode="observation_only",
        marketgraph_mode=config.marketgraph_mode,
        real_trading_enabled=False,
        historical_pit_eligible=False,
        execution_authority=False,
        profile_id=snapshot.profile_id,
        catalog_version=snapshot.catalog_version,
        decision_as_of=snapshot.decision_as_of,
        snapshot_sha256=snapshot.snapshot_sha256,
        probe_receipt_sha256=str(probe_receipt["receipt_sha256"]),
        observation_receipt_sha256=str(observation_receipt["receipt_sha256"]),
        probe_same_as_of_match=True,
        tradable_symbols=tradable,
        tradable_universe_count=len(tradable),
        tradable_universe_sha256=_canonical_sha256(list(tradable)),
        excluded_individual_count=sum(excluded.values()),
        excluded_reason_counts=excluded,
        context_probe_roles=context_roles,
        idempotent_replay=idempotent_replay,
    )


def run_ashare_observation(
    config: AshareObservationConfig,
    *,
    transport_factory: TransportFactory = build_runtime_transport,
) -> AshareObservationResult:
    """Validate and persist exactly one A-share current observation."""

    if not isinstance(config, AshareObservationConfig):
        raise TypeError("config must be AshareObservationConfig")
    manifest = _load_config(config.manifest_path)
    context_roles = _context_probe_roles(manifest)
    store = FileResearchSnapshotStore(config.snapshot_root)
    try:
        recovered = store.load_bound_decision(
            profile_id=manifest.profile_id,
            decision_as_of=manifest.as_of,
            catalog_version=manifest.catalog_version,
        )
    except ResearchSnapshotStoreCorruption as exc:
        raise AshareObservationBlocked("research_snapshot_store_invalid") from exc
    probe_path = _probe_binding_path(config.snapshot_root, manifest)
    observation_path = _observation_receipt_path(config.snapshot_root, manifest)
    if recovered is not None:
        probe_receipt = _read_probe_receipt(probe_path, manifest)
        _validate_snapshot(recovered, manifest)
        _validate_probe_snapshot_binding(recovered, probe_receipt, manifest)
        result = _result(
            snapshot=recovered,
            probe_receipt=probe_receipt,
            context_roles=context_roles,
            idempotent_replay=True,
            config=config,
            manifest=manifest,
        )
        tradable, excluded = _universe_projection(recovered, manifest)
        observation_receipt = _observation_receipt(
            snapshot=recovered,
            probe_receipt=probe_receipt,
            manifest=manifest,
            tradable=tradable,
            excluded=excluded,
            context_roles=context_roles,
        )
        _validate_observation_receipt(observation_path, observation_receipt)
        return result

    try:
        transport = transport_factory(
            manifest.transport_id,
            token_file=config.token_file,
            base_url=manifest.base_url,
        )
    except Exception as exc:
        if isinstance(exc, AshareObservationBlocked):
            raise
        raise AshareObservationConfigurationError(
            "tradingdatas_transport_invalid"
        ) from exc
    if transport is None or not callable(transport):
        raise AshareObservationConfigurationError("tradingdatas_transport_invalid")

    probe_receipt = run_sharedsignals_integration_probe(
        manifest,
        transport=transport,
    )
    if (
        probe_receipt.get("status") != "pass"
        or probe_receipt.get("blocking") is not False
        or probe_receipt.get("same_as_of_match") is not True
    ):
        raise AshareObservationBlocked("integration_probe_blocked")
    probe_datasets = {
        item["dataset_id"]: item
        for item in probe_receipt.get("datasets", [])
        if isinstance(item, Mapping) and isinstance(item.get("dataset_id"), str)
    }
    if set(probe_datasets) != {item.dataset_id for item in manifest.datasets}:
        raise AshareObservationBlocked("integration_probe_dataset_set_mismatch")

    client = SharedSignalsV1Client(manifest.to_client_config(), transport=transport)
    gate = DataEvidenceGate(
        {item.dataset_id: item.policy() for item in manifest.datasets}
    )
    page_runs = []
    decisions = []
    try:
        for spec in manifest.datasets:
            page_run = collect_query_pages(
                client=client,
                request=spec.query(as_of=manifest.as_of),
                identity_fields=spec.identity_fields,
                max_pages=spec.max_pages,
                max_rows=spec.max_rows,
            )
            probe_dataset = probe_datasets[spec.dataset_id]
            if (
                page_run.semantic_sha256
                != probe_dataset.get("semantic_response_sha256")
                or page_run.semantic_trace_sha256
                != probe_dataset.get("pagination_semantic_sha256")
                or page_run.identity_sha256 != probe_dataset.get("identity_sha256")
                or page_run.row_count != probe_dataset.get("row_count")
                or page_run.page_count != probe_dataset.get("page_count")
            ):
                raise AshareObservationBlocked("snapshot_read_drifted_after_probe")
            page_runs.append(page_run)
            decisions.append(gate.evaluate(page_run.envelope))
        snapshot = build_research_data_snapshot(
            profile=manifest.to_profile(),
            page_runs=tuple(page_runs),
            decisions=tuple(decisions),
            decision_as_of=datetime.fromisoformat(
                manifest.as_of.replace("Z", "+00:00")
            ),
        )
    except AshareObservationBlocked:
        raise
    except (PaginationContractError, ResearchDataContractError) as exc:
        raise AshareObservationBlocked("snapshot_collection_blocked") from exc
    _validate_snapshot(snapshot, manifest)
    _universe_projection(snapshot, manifest)
    _validate_probe_snapshot_binding(snapshot, probe_receipt, manifest)

    try:
        write_probe_receipt(probe_path, probe_receipt)
        store.compare_and_swap(
            snapshot=snapshot,
            expected_snapshot_sha256=None,
        )
        readback = store.load_bound_decision(
            profile_id=manifest.profile_id,
            decision_as_of=manifest.as_of,
            catalog_version=manifest.catalog_version,
        )
    except (
        IntegrationProbeConfigurationError,
        ResearchSnapshotStoreConflict,
        ResearchSnapshotStoreCorruption,
    ) as exc:
        raise AshareObservationBlocked("research_snapshot_store_commit_failed") from exc
    if readback != snapshot:
        raise AshareObservationBlocked("research_snapshot_store_readback_mismatch")
    persisted_probe_receipt = _read_probe_receipt(probe_path, manifest)
    _validate_probe_snapshot_binding(readback, persisted_probe_receipt, manifest)
    result = _result(
        snapshot=readback,
        probe_receipt=persisted_probe_receipt,
        context_roles=context_roles,
        idempotent_replay=False,
        config=config,
        manifest=manifest,
    )
    tradable, excluded = _universe_projection(readback, manifest)
    observation_receipt = _observation_receipt(
        snapshot=readback,
        probe_receipt=persisted_probe_receipt,
        manifest=manifest,
        tradable=tradable,
        excluded=excluded,
        context_roles=context_roles,
    )
    _write_immutable_observation_receipt(observation_path, observation_receipt)
    _validate_observation_receipt(observation_path, observation_receipt)
    return result


__all__ = [
    "AshareObservationBlocked",
    "AshareObservationConfig",
    "AshareObservationConfigurationError",
    "AshareObservationResult",
    "OBSERVATION_SCHEMA_ID",
    "assert_no_plaintext_token_environment",
    "run_ashare_observation",
]
