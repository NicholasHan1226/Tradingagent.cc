"""Fail-closed A-share current-observation runtime.

The runtime consumes only the frozen TradingDatas catalog/query contract.  It
requires a complete bounded, same-observation integration probe before it
publishes one immutable research snapshot.  The snapshot remains current
observation evidence and never gains trading, capital, or historical-PIT
authority.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping
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
from shared.runtime.ashare_observation_ledger import (
    OBSERVED_REASON_CODE,
    AshareObservationLedgerConflict,
    AshareObservationLedgerContractError,
    AshareObservationLedgerCorruption,
    AshareObservationMembershipArtifact,
    AshareObservationMembershipRecord,
    FileAshareObservationMembershipLedger,
    build_ashare_observation_membership_artifact,
)
from shared.runtime_test.sharedsignals_v1_integration_probe import (
    IntegrationProbeConfigurationError,
    SharedSignalsIntegrationProbeConfig,
    load_probe_manifest,
    run_sharedsignals_integration_probe,
)
from shared.universe.policy import classify_instrument, is_mainboard_tradable


OBSERVATION_SCHEMA_ID = "tradingagent.ashare.current-observation.v1"
OBSERVATION_RECEIPT_SCHEMA_ID = "tradingagent.ashare.observation-receipt.v1"
OBSERVATION_TRANSACTION_INTENT_SCHEMA_ID = (
    "tradingagent.ashare.observation-transaction-intent.v1"
)
OBSERVATION_TRANSACTION_COMPLETE_SCHEMA_ID = (
    "tradingagent.ashare.observation-transaction-complete.v1"
)
OBSERVATION_UNIVERSE_SEMANTICS = "mainboard_observation_universe_not_order_eligible"
OBSERVATION_TRANSACTION_ARTIFACTS = (
    "integration_probe_receipt",
    "research_snapshot",
    "observation_receipt",
    "observation_membership",
)
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
    observation_transaction_complete_sha256: str
    probe_same_as_of_match: bool
    observation_session: str
    observation_universe_semantics: str
    observation_universe_count: int
    observation_universe_sha256: str
    observation_ledger_sha256: str
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
            or self.observation_universe_semantics != OBSERVATION_UNIVERSE_SEMANTICS
            or self.observation_universe_count != self.tradable_universe_count
            or self.observation_universe_sha256 != self.tradable_universe_sha256
            or not isinstance(
                self.observation_transaction_complete_sha256,
                str,
            )
            or len(self.observation_transaction_complete_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.observation_transaction_complete_sha256
            )
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
            "observation_transaction_complete_sha256": (
                self.observation_transaction_complete_sha256
            ),
            "probe_same_as_of_match": self.probe_same_as_of_match,
            "observation_session": self.observation_session,
            "observation_universe_semantics": self.observation_universe_semantics,
            "observation_universe_count": self.observation_universe_count,
            "observation_universe_sha256": self.observation_universe_sha256,
            "observation_ledger_sha256": self.observation_ledger_sha256,
            # Compatibility aliases for the already-frozen v1 receipt/runtime.
            "tradable_universe_count": self.tradable_universe_count,
            "tradable_universe_sha256": self.tradable_universe_sha256,
            "excluded_individual_count": self.excluded_individual_count,
            "excluded_reason_counts": dict(self.excluded_reason_counts),
            "context_probe_roles": list(self.context_probe_roles),
            "idempotent_replay": self.idempotent_replay,
        }
        if include_tradable_symbols:
            payload["observation_symbols"] = list(self.tradable_symbols)
            payload["tradable_symbols"] = list(self.tradable_symbols)
        return payload


@dataclass(frozen=True)
class _ObservationUniverseProjection:
    observation_session: str
    observed_symbols: tuple[str, ...]
    excluded_reason_counts: Mapping[str, int]
    membership_records: tuple[AshareObservationMembershipRecord, ...]


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
        or not {"ts_code", "trade_date", "close", "vol", "amount"}.issubset(
            daily.fields
        )
    ):
        raise AshareObservationBlocked("daily_bars_scope_contract_invalid")
    decision_instant = datetime.fromisoformat(config.as_of.replace("Z", "+00:00"))
    if decision_instant.tzinfo is None or decision_instant.utcoffset() is None:
        raise AshareObservationBlocked("decision_as_of_must_be_timezone_aware")
    local_decision = decision_instant.astimezone(_SHANGHAI)
    if (local_decision.hour, local_decision.minute, local_decision.second) < (
        15,
        0,
        0,
    ):
        raise AshareObservationBlocked("post_close_observation_required")
    decision_date = local_decision.strftime("%Y%m%d")
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


def _observation_transaction_identity(
    config: SharedSignalsIntegrationProbeConfig,
) -> str:
    return _canonical_sha256(
        {
            "profile_id": config.profile_id,
            "catalog_version": config.catalog_version,
            "as_of": config.as_of,
            "manifest_sha256": config.manifest_sha256,
        }
    )


def _probe_binding_path(
    root: Path,
    config: SharedSignalsIntegrationProbeConfig,
) -> Path:
    return root / f"integration-{_observation_transaction_identity(config)}.json"


def _observation_receipt_path(
    root: Path,
    config: SharedSignalsIntegrationProbeConfig,
) -> Path:
    return root / _probe_binding_path(root, config).name.replace(
        "integration-", "observation-", 1
    )


def _observation_transaction_intent_path(
    root: Path,
    config: SharedSignalsIntegrationProbeConfig,
) -> Path:
    return root / (
        f"observation-intent-{_observation_transaction_identity(config)}.json"
    )


def _observation_transaction_complete_path(
    root: Path,
    config: SharedSignalsIntegrationProbeConfig,
) -> Path:
    return root / (
        f"observation-complete-{_observation_transaction_identity(config)}.json"
    )


def _observation_transaction_lock_path(
    root: Path,
    config: SharedSignalsIntegrationProbeConfig,
) -> Path:
    try:
        decision = datetime.fromisoformat(config.as_of.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AshareObservationBlocked("decision_as_of_must_be_timezone_aware") from exc
    if decision.tzinfo is None or decision.utcoffset() is None:
        raise AshareObservationBlocked("decision_as_of_must_be_timezone_aware")
    session = decision.astimezone(_SHANGHAI).strftime("%Y%m%d")
    # The membership authority is unique per A-share session, not per manifest.
    # A session lock therefore serializes different profile/manifest identities
    # that could otherwise race for the same immutable membership binding.
    return root / f"observation-session-lock-{session}.lock"


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


def _assert_no_symlink_path(path: Path, *, invalid_reason: str) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise AshareObservationBlocked(invalid_reason) from exc
        if stat.S_ISLNK(mode):
            raise AshareObservationBlocked(invalid_reason)
        if current != absolute and not stat.S_ISDIR(mode):
            raise AshareObservationBlocked(invalid_reason)


def _fsync_directory(path: Path, *, invalid_reason: str) -> None:
    _assert_no_symlink_path(path, invalid_reason=invalid_reason)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AshareObservationBlocked(invalid_reason) from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise AshareObservationBlocked(invalid_reason)
        os.fsync(descriptor)
    except OSError as exc:
        raise AshareObservationBlocked(invalid_reason) from exc
    finally:
        os.close(descriptor)


def _trusted_private_file(
    descriptor: int,
    path: Path,
    *,
    invalid_reason: str,
) -> os.stat_result:
    try:
        metadata = os.fstat(descriptor)
        named_metadata = path.lstat()
    except OSError as exc:
        raise AshareObservationBlocked(invalid_reason) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or not stat.S_ISREG(named_metadata.st_mode)
        or metadata.st_nlink != 1
        or named_metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
        or (metadata.st_dev, metadata.st_ino)
        != (named_metadata.st_dev, named_metadata.st_ino)
    ):
        raise AshareObservationBlocked(invalid_reason)
    return metadata


def _prepare_private_root(root: Path) -> None:
    invalid_reason = "observation_transaction_root_invalid"
    _assert_no_symlink_path(root, invalid_reason=invalid_reason)
    existed = root.exists()
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise AshareObservationBlocked(invalid_reason) from exc
    _assert_no_symlink_path(root, invalid_reason=invalid_reason)
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise AshareObservationBlocked(invalid_reason) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or (existed and stat.S_IMODE(metadata.st_mode) != 0o700)
    ):
        raise AshareObservationBlocked(invalid_reason)
    if not existed:
        try:
            os.chmod(root, 0o700)
        except OSError as exc:
            raise AshareObservationBlocked(invalid_reason) from exc
        _fsync_directory(root.parent, invalid_reason=invalid_reason)
    _fsync_directory(root, invalid_reason=invalid_reason)


@contextmanager
def _observation_transaction_lock(
    root: Path,
    manifest: SharedSignalsIntegrationProbeConfig,
) -> Iterator[None]:
    _prepare_private_root(root)
    path = _observation_transaction_lock_path(root, manifest)
    invalid_reason = "observation_transaction_lock_invalid"
    _assert_no_symlink_path(path, invalid_reason=invalid_reason)
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise AshareObservationBlocked(invalid_reason) from exc
    try:
        _trusted_private_file(descriptor, path, invalid_reason=invalid_reason)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        _trusted_private_file(descriptor, path, invalid_reason=invalid_reason)
        yield
        _trusted_private_file(descriptor, path, invalid_reason=invalid_reason)
    except OSError as exc:
        raise AshareObservationBlocked(invalid_reason) from exc
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _write_immutable_private_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    invalid_reason: str,
    conflict_reason: str,
    write_reason: str,
) -> None:
    expected = dict(payload)
    encoded = _private_json_bytes(expected)
    if path.exists() or path.is_symlink():
        _repair_immutable_publish_window(
            path,
            encoded,
            invalid_reason=invalid_reason,
            write_reason=write_reason,
        )
        existing = _read_private_json(path, invalid_reason=invalid_reason)
        if existing != expected:
            raise AshareObservationBlocked(conflict_reason)
        return
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        _trusted_private_file(
            descriptor,
            temporary,
            invalid_reason=write_reason,
        )
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise AshareObservationBlocked(write_reason)
            offset += written
        os.fsync(descriptor)
        _trusted_private_file(
            descriptor,
            temporary,
            invalid_reason=write_reason,
        )
        os.close(descriptor)
        descriptor = None
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            _repair_immutable_publish_window(
                path,
                encoded,
                invalid_reason=invalid_reason,
                write_reason=write_reason,
            )
            existing = _read_private_json(path, invalid_reason=invalid_reason)
            if existing != expected:
                raise AshareObservationBlocked(conflict_reason)
        temporary.unlink(missing_ok=True)
        _fsync_directory(path.parent, invalid_reason=write_reason)
        published = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            _trusted_private_file(
                published,
                path,
                invalid_reason=invalid_reason,
            )
            os.fsync(published)
        finally:
            os.close(published)
    except AshareObservationBlocked:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise AshareObservationBlocked(write_reason) from exc


def _private_json_bytes(payload: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                dict(payload),
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AshareObservationBlocked("observation_transaction_payload_invalid") from exc


def _repair_immutable_publish_window(
    path: Path,
    expected_bytes: bytes,
    *,
    invalid_reason: str,
    write_reason: str,
) -> None:
    """Finish the sole safe link/unlink crash window for one exact payload."""

    if path.is_symlink():
        raise AshareObservationBlocked(invalid_reason)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AshareObservationBlocked(invalid_reason) from exc
    if metadata.st_nlink == 1:
        return
    if (
        metadata.st_nlink != 2
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
    ):
        raise AshareObservationBlocked(invalid_reason)
    candidates: list[Path] = []
    for candidate in path.parent.glob(f".{path.name}.*.tmp"):
        try:
            candidate_metadata = candidate.lstat()
        except OSError as exc:
            raise AshareObservationBlocked(invalid_reason) from exc
        if (
            stat.S_ISREG(candidate_metadata.st_mode)
            and candidate_metadata.st_nlink == 2
            and candidate_metadata.st_uid == os.geteuid()
            and stat.S_IMODE(candidate_metadata.st_mode) == 0o600
            and (candidate_metadata.st_dev, candidate_metadata.st_ino)
            == (metadata.st_dev, metadata.st_ino)
        ):
            candidates.append(candidate)
    if len(candidates) != 1:
        raise AshareObservationBlocked(invalid_reason)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        descriptor_metadata = os.fstat(descriptor)
        if (
            descriptor_metadata.st_nlink != 2
            or (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
            != (metadata.st_dev, metadata.st_ino)
        ):
            raise AshareObservationBlocked(invalid_reason)
        chunks: list[bytes] = []
        remaining = len(expected_bytes) + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1_048_576, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if b"".join(chunks) != expected_bytes:
            raise AshareObservationBlocked(invalid_reason)
        os.close(descriptor)
        descriptor = None
        candidates[0].unlink()
        _fsync_directory(path.parent, invalid_reason=write_reason)
        published = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            _trusted_private_file(
                published,
                path,
                invalid_reason=invalid_reason,
            )
            os.fsync(published)
        finally:
            os.close(published)
    except AshareObservationBlocked:
        raise
    except OSError as exc:
        raise AshareObservationBlocked(write_reason) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


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
) -> _ObservationUniverseProjection:
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

    decision_instant = datetime.fromisoformat(config.as_of.replace("Z", "+00:00"))
    if decision_instant.tzinfo is None or decision_instant.utcoffset() is None:
        raise AshareObservationBlocked("decision_as_of_must_be_timezone_aware")
    local_decision = decision_instant.astimezone(_SHANGHAI)
    decision_date = local_decision.date()
    try:
        daily_data_through = datetime.fromisoformat(
            str(daily_snapshot.data_through).replace("Z", "+00:00")
        )
    except (TypeError, ValueError) as exc:
        raise AshareObservationBlocked("daily_data_through_invalid") from exc
    if daily_data_through.tzinfo is None or daily_data_through.utcoffset() is None:
        raise AshareObservationBlocked("daily_data_through_invalid")
    local_daily_data_through = daily_data_through.astimezone(_SHANGHAI)
    if local_daily_data_through.date() != decision_date or (
        local_daily_data_through.hour,
        local_daily_data_through.minute,
        local_daily_data_through.second,
    ) < (15, 0, 0):
        raise AshareObservationBlocked("post_close_daily_data_through_required")

    try:
        daily_observed_at = datetime.fromisoformat(
            str(daily_snapshot.max_row_observed_at).replace("Z", "+00:00")
        )
    except (TypeError, ValueError) as exc:
        raise AshareObservationBlocked("daily_observation_time_invalid") from exc
    if daily_observed_at.tzinfo is None or daily_observed_at.utcoffset() is None:
        raise AshareObservationBlocked("daily_observation_time_invalid")
    local_daily_observed_at = daily_observed_at.astimezone(_SHANGHAI)
    if (
        local_daily_observed_at.date() != decision_date
        or (
            local_daily_observed_at.hour,
            local_daily_observed_at.minute,
            local_daily_observed_at.second,
        )
        < (15, 0, 0)
        or daily_data_through > daily_observed_at
        or daily_observed_at > decision_instant
    ):
        raise AshareObservationBlocked("post_close_daily_observation_required")

    daily_rows: dict[str, Mapping[str, Any]] = {}
    for row in daily_snapshot.decoded_rows():
        symbol = row.get("ts_code")
        if row.get("trade_date") != decision_date.strftime("%Y%m%d"):
            raise AshareObservationBlocked("daily_bar_trade_date_mismatch")
        if not isinstance(symbol, str) or not symbol or symbol in daily_rows:
            raise AshareObservationBlocked("daily_symbol_identity_invalid")
        daily_rows[symbol] = row

    observed: set[str] = set()
    excluded: dict[str, int] = {}
    records: list[AshareObservationMembershipRecord] = []

    def exclude(symbol: str, reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1
        try:
            records.append(
                AshareObservationMembershipRecord(
                    symbol=symbol,
                    disposition="excluded",
                    reason_code=reason,
                )
            )
        except AshareObservationLedgerContractError as exc:
            raise AshareObservationBlocked(
                "observation_symbol_identity_invalid"
            ) from exc

    for symbol in sorted(set(master_rows).union(daily_rows)):
        row = daily_rows.get(symbol)
        eligibility = classify_instrument(symbol, instrument_type="common_stock")
        if not is_mainboard_tradable(symbol, instrument_type="common_stock"):
            exclude(symbol, eligibility.reason_code)
            continue
        if eligibility.normalized_symbol != symbol:
            raise AshareObservationBlocked("daily_symbol_identity_invalid")
        master = master_rows.get(eligibility.normalized_symbol)
        if master is None or master.get("list_status") != "L":
            exclude(symbol, "security_master_missing_or_inactive")
            continue
        name = master.get("name")
        if not isinstance(name, str) or not name.strip():
            exclude(symbol, "security_master_missing_or_inactive")
            continue
        upper_name = name.upper()
        if "ST" in upper_name or "退" in name:
            exclude(symbol, "risk_warning_security_excluded")
            continue
        list_date = master.get("list_date")
        if not isinstance(list_date, str):
            exclude(symbol, "security_master_missing_or_inactive")
            continue
        try:
            listed = datetime.strptime(list_date.replace("-", "")[:8], "%Y%m%d").date()
        except ValueError:
            exclude(symbol, "security_master_missing_or_inactive")
            continue
        if decision_date - listed < timedelta(days=_MIN_LISTING_AGE_DAYS):
            exclude(symbol, "new_listing_excluded")
            continue
        if row is None:
            exclude(symbol, "daily_bar_missing_or_unavailable")
            continue
        close = row.get("close")
        volume = row.get("vol")
        amount = row.get("amount")
        if (
            isinstance(close, bool)
            or not isinstance(close, (int, float))
            or not math.isfinite(float(close))
            or float(close) <= 0.0
            or isinstance(volume, bool)
            or not isinstance(volume, (int, float))
            or not math.isfinite(float(volume))
            or float(volume) <= 0.0
            or isinstance(amount, bool)
            or not isinstance(amount, (int, float))
            or not math.isfinite(float(amount))
            or float(amount) <= 0.0
        ):
            exclude(symbol, "suspended_or_nonpositive_bar_excluded")
            continue
        observed.add(eligibility.normalized_symbol)
        try:
            records.append(
                AshareObservationMembershipRecord(
                    symbol=eligibility.normalized_symbol,
                    disposition="observed",
                    reason_code=OBSERVED_REASON_CODE,
                )
            )
        except AshareObservationLedgerContractError as exc:
            raise AshareObservationBlocked(
                "observation_symbol_identity_invalid"
            ) from exc
    if not observed:
        raise AshareObservationBlocked("mainboard_tradable_universe_empty")
    if len(records) != len(set(master_rows).union(daily_rows)):
        raise AshareObservationBlocked("daily_symbol_membership_incomplete")
    return _ObservationUniverseProjection(
        observation_session=decision_date.strftime("%Y%m%d"),
        observed_symbols=tuple(sorted(observed)),
        excluded_reason_counts=dict(sorted(excluded.items())),
        membership_records=tuple(sorted(records, key=lambda item: item.symbol)),
    )


def _observation_receipt(
    *,
    snapshot: ResearchDataSnapshot,
    probe_receipt: Mapping[str, Any],
    manifest: SharedSignalsIntegrationProbeConfig,
    projection: _ObservationUniverseProjection,
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
        # Frozen v1 compatibility fields.  New consumers call this the
        # observation universe because these symbols are not order-eligible.
        "tradable_universe_count": len(projection.observed_symbols),
        "tradable_universe_sha256": _canonical_sha256(
            list(projection.observed_symbols)
        ),
        "excluded_reason_counts": dict(projection.excluded_reason_counts),
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
    _write_immutable_private_json(
        path,
        receipt,
        invalid_reason="observation_receipt_invalid",
        conflict_reason="observation_receipt_conflict",
        write_reason="observation_receipt_write_failed",
    )


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


def _persist_observation_membership(
    *,
    root: Path,
    snapshot: ResearchDataSnapshot,
    observation_receipt: Mapping[str, Any],
    projection: _ObservationUniverseProjection,
    allow_create: bool,
    expected_artifact: AshareObservationMembershipArtifact | None = None,
) -> AshareObservationMembershipArtifact:
    ledger = FileAshareObservationMembershipLedger(root / "observation-membership")
    try:
        expected = expected_artifact or build_ashare_observation_membership_artifact(
            observation_session=projection.observation_session,
            research_snapshot=snapshot,
            observation_receipt=observation_receipt,
            records=projection.membership_records,
        )
        current = ledger.load_bound_session(
            observation_session=projection.observation_session
        )
        if current is None and not allow_create:
            raise AshareObservationBlocked("observation_membership_ledger_missing")
        persisted = ledger.compare_and_swap(
            observation_session=projection.observation_session,
            research_snapshot=snapshot,
            observation_receipt=observation_receipt,
            records=projection.membership_records,
            expected_content_sha256=(current.content_sha256 if current else None),
        )
        if persisted != expected:
            raise AshareObservationBlocked("observation_membership_ledger_invalid")
        return persisted
    except AshareObservationBlocked:
        raise
    except AshareObservationLedgerConflict as exc:
        raise AshareObservationBlocked(
            "observation_membership_ledger_conflict"
        ) from exc
    except (
        AshareObservationLedgerContractError,
        AshareObservationLedgerCorruption,
    ) as exc:
        raise AshareObservationBlocked("observation_membership_ledger_invalid") from exc


def _observation_transaction_marker(
    *,
    schema_id: str,
    manifest: SharedSignalsIntegrationProbeConfig,
    snapshot: ResearchDataSnapshot,
    probe_receipt: Mapping[str, Any],
    observation_receipt: Mapping[str, Any],
    membership: AshareObservationMembershipArtifact,
) -> dict[str, Any]:
    if schema_id not in {
        OBSERVATION_TRANSACTION_INTENT_SCHEMA_ID,
        OBSERVATION_TRANSACTION_COMPLETE_SCHEMA_ID,
    }:
        raise AshareObservationBlocked("observation_transaction_marker_invalid")
    payload: dict[str, Any] = {
        "schema_id": schema_id,
        "profile_id": snapshot.profile_id,
        "catalog_version": snapshot.catalog_version,
        "decision_as_of": snapshot.decision_as_of,
        "observation_session": membership.observation_session,
        "manifest_sha256": manifest.manifest_sha256,
        "snapshot_sha256": snapshot.snapshot_sha256,
        "probe_receipt_sha256": probe_receipt.get("receipt_sha256"),
        "observation_receipt_sha256": observation_receipt.get("receipt_sha256"),
        "observation_membership_sha256": membership.content_sha256,
        "required_artifacts": list(OBSERVATION_TRANSACTION_ARTIFACTS),
        "mode": "observation_only",
        "marketgraph_mode": "mg_off",
        "historical_pit_eligible": False,
        "real_trading_enabled": False,
        "execution_authority": False,
    }
    payload["content_sha256"] = _canonical_sha256(payload)
    return payload


def _validate_observation_transaction_marker(
    path: Path,
    expected: Mapping[str, Any],
    *,
    invalid_reason: str,
) -> None:
    _repair_immutable_publish_window(
        path,
        _private_json_bytes(expected),
        invalid_reason=invalid_reason,
        write_reason=invalid_reason,
    )
    payload = _read_private_json(path, invalid_reason=invalid_reason)
    unsigned = dict(payload)
    claimed = unsigned.pop("content_sha256", None)
    if claimed != _canonical_sha256(unsigned) or payload != dict(expected):
        raise AshareObservationBlocked(invalid_reason)


def _write_observation_transaction_marker(
    path: Path,
    marker: Mapping[str, Any],
    *,
    marker_kind: str,
) -> None:
    _write_immutable_private_json(
        path,
        marker,
        invalid_reason=f"observation_transaction_{marker_kind}_invalid",
        conflict_reason=f"observation_transaction_{marker_kind}_conflict",
        write_reason=f"observation_transaction_{marker_kind}_write_failed",
    )


def _persist_observation_transaction(
    *,
    root: Path,
    store: FileResearchSnapshotStore,
    manifest: SharedSignalsIntegrationProbeConfig,
    snapshot: ResearchDataSnapshot,
    probe_receipt: Mapping[str, Any],
    projection: _ObservationUniverseProjection,
    context_roles: tuple[str, ...],
    recovered_snapshot_required: bool,
) -> tuple[ResearchDataSnapshot, AshareObservationMembershipArtifact, bool]:
    """Commit or recover one four-artifact observation transaction.

    Intent is durable before the first data artifact.  A complete marker only
    appears after the exact probe, snapshot, observation receipt, and session
    membership authority all read back successfully.  Existing snapshots that
    predate this protocol cannot be upgraded because recovery requires the
    exact pre-existing intent marker.
    """

    probe_path = _probe_binding_path(root, manifest)
    observation_path = _observation_receipt_path(root, manifest)
    intent_path = _observation_transaction_intent_path(root, manifest)
    complete_path = _observation_transaction_complete_path(root, manifest)
    observation_receipt = _observation_receipt(
        snapshot=snapshot,
        probe_receipt=probe_receipt,
        manifest=manifest,
        projection=projection,
        context_roles=context_roles,
    )
    try:
        expected_membership = build_ashare_observation_membership_artifact(
            observation_session=projection.observation_session,
            research_snapshot=snapshot,
            observation_receipt=observation_receipt,
            records=projection.membership_records,
        )
    except AshareObservationLedgerContractError as exc:
        raise AshareObservationBlocked("observation_membership_ledger_invalid") from exc
    intent = _observation_transaction_marker(
        schema_id=OBSERVATION_TRANSACTION_INTENT_SCHEMA_ID,
        manifest=manifest,
        snapshot=snapshot,
        probe_receipt=probe_receipt,
        observation_receipt=observation_receipt,
        membership=expected_membership,
    )
    complete = _observation_transaction_marker(
        schema_id=OBSERVATION_TRANSACTION_COMPLETE_SCHEMA_ID,
        manifest=manifest,
        snapshot=snapshot,
        probe_receipt=probe_receipt,
        observation_receipt=observation_receipt,
        membership=expected_membership,
    )

    with _observation_transaction_lock(root, manifest):
        try:
            current = store.load_bound_decision(
                profile_id=manifest.profile_id,
                decision_as_of=manifest.as_of,
                catalog_version=manifest.catalog_version,
            )
        except ResearchSnapshotStoreCorruption as exc:
            raise AshareObservationBlocked("research_snapshot_store_invalid") from exc
        if current is None:
            if recovered_snapshot_required:
                raise AshareObservationBlocked("research_snapshot_store_invalid")
            intent_exists = intent_path.exists() or intent_path.is_symlink()
            probe_exists = probe_path.exists() or probe_path.is_symlink()
            observation_exists = (
                observation_path.exists() or observation_path.is_symlink()
            )
            complete_exists = complete_path.exists() or complete_path.is_symlink()
            ledger = FileAshareObservationMembershipLedger(
                root / "observation-membership"
            )
            try:
                current_membership = ledger.load_bound_session(
                    observation_session=projection.observation_session
                )
            except (
                AshareObservationLedgerContractError,
                AshareObservationLedgerCorruption,
            ) as exc:
                raise AshareObservationBlocked(
                    "observation_membership_ledger_invalid"
                ) from exc
            if not intent_exists:
                if (
                    probe_exists
                    or observation_exists
                    or complete_exists
                    or current_membership is not None
                ):
                    raise AshareObservationBlocked(
                        "observation_transaction_legacy_state_forbidden"
                    )
                _write_observation_transaction_marker(
                    intent_path,
                    intent,
                    marker_kind="intent",
                )
            else:
                _write_observation_transaction_marker(
                    intent_path,
                    intent,
                    marker_kind="intent",
                )
                if (
                    observation_exists
                    or complete_exists
                    or current_membership is not None
                ):
                    raise AshareObservationBlocked(
                        "observation_transaction_state_invalid"
                    )
            _write_immutable_private_json(
                probe_path,
                probe_receipt,
                invalid_reason="replay_probe_receipt_invalid",
                conflict_reason="integration_probe_receipt_conflict",
                write_reason="integration_probe_receipt_write_failed",
            )
            try:
                store.compare_and_swap(
                    snapshot=snapshot,
                    expected_snapshot_sha256=None,
                )
                current = store.load_bound_decision(
                    profile_id=manifest.profile_id,
                    decision_as_of=manifest.as_of,
                    catalog_version=manifest.catalog_version,
                )
            except (
                ResearchSnapshotStoreConflict,
                ResearchSnapshotStoreCorruption,
            ) as exc:
                raise AshareObservationBlocked(
                    "research_snapshot_store_commit_failed"
                ) from exc
        else:
            # A completed or interrupted peer transaction may have won after
            # the lock-free discovery read.  Never continue the fresh path.
            if current != snapshot:
                raise AshareObservationBlocked("research_snapshot_store_conflict")
            if not intent_path.exists() and not intent_path.is_symlink():
                raise AshareObservationBlocked(
                    "observation_transaction_intent_missing"
                )

        if current != snapshot:
            raise AshareObservationBlocked(
                "research_snapshot_store_readback_mismatch"
            )
        _validate_observation_transaction_marker(
            intent_path,
            intent,
            invalid_reason="observation_transaction_intent_invalid",
        )
        persisted_probe = _read_probe_receipt(probe_path, manifest)
        if persisted_probe != dict(probe_receipt):
            raise AshareObservationBlocked("replay_probe_receipt_invalid")
        _validate_snapshot(current, manifest)
        _validate_probe_snapshot_binding(current, persisted_probe, manifest)

        complete_existed = complete_path.exists() or complete_path.is_symlink()
        if complete_existed:
            _validate_observation_transaction_marker(
                complete_path,
                complete,
                invalid_reason="observation_transaction_complete_invalid",
            )
            _validate_observation_receipt(observation_path, observation_receipt)
            membership = _persist_observation_membership(
                root=root,
                snapshot=current,
                observation_receipt=observation_receipt,
                projection=projection,
                allow_create=False,
                expected_artifact=expected_membership,
            )
            return current, membership, True

        _write_immutable_observation_receipt(observation_path, observation_receipt)
        _validate_observation_receipt(observation_path, observation_receipt)
        membership = _persist_observation_membership(
            root=root,
            snapshot=current,
            observation_receipt=observation_receipt,
            projection=projection,
            allow_create=True,
            expected_artifact=expected_membership,
        )
        _write_observation_transaction_marker(
            complete_path,
            complete,
            marker_kind="complete",
        )
        _validate_observation_transaction_marker(
            complete_path,
            complete,
            invalid_reason="observation_transaction_complete_invalid",
        )
        return current, membership, False


def _result(
    *,
    snapshot: ResearchDataSnapshot,
    probe_receipt: Mapping[str, Any],
    context_roles: tuple[str, ...],
    idempotent_replay: bool,
    config: AshareObservationConfig,
    manifest: SharedSignalsIntegrationProbeConfig,
    projection: _ObservationUniverseProjection,
    observation_ledger: AshareObservationMembershipArtifact,
) -> AshareObservationResult:
    observation_receipt = _observation_receipt(
        snapshot=snapshot,
        probe_receipt=probe_receipt,
        manifest=manifest,
        projection=projection,
        context_roles=context_roles,
    )
    complete = _observation_transaction_marker(
        schema_id=OBSERVATION_TRANSACTION_COMPLETE_SCHEMA_ID,
        manifest=manifest,
        snapshot=snapshot,
        probe_receipt=probe_receipt,
        observation_receipt=observation_receipt,
        membership=observation_ledger,
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
        observation_transaction_complete_sha256=str(complete["content_sha256"]),
        probe_same_as_of_match=True,
        observation_session=projection.observation_session,
        observation_universe_semantics=OBSERVATION_UNIVERSE_SEMANTICS,
        observation_universe_count=len(projection.observed_symbols),
        observation_universe_sha256=_canonical_sha256(
            list(projection.observed_symbols)
        ),
        observation_ledger_sha256=observation_ledger.content_sha256,
        tradable_symbols=projection.observed_symbols,
        tradable_universe_count=len(projection.observed_symbols),
        tradable_universe_sha256=_canonical_sha256(list(projection.observed_symbols)),
        excluded_individual_count=sum(projection.excluded_reason_counts.values()),
        excluded_reason_counts=projection.excluded_reason_counts,
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
    if recovered is not None:
        probe_receipt = _read_probe_receipt(probe_path, manifest)
        _validate_snapshot(recovered, manifest)
        _validate_probe_snapshot_binding(recovered, probe_receipt, manifest)
        projection = _universe_projection(recovered, manifest)
        readback, membership, idempotent_replay = _persist_observation_transaction(
            root=config.snapshot_root,
            store=store,
            manifest=manifest,
            snapshot=recovered,
            probe_receipt=probe_receipt,
            projection=projection,
            context_roles=context_roles,
            recovered_snapshot_required=True,
        )
        return _result(
            snapshot=readback,
            probe_receipt=probe_receipt,
            context_roles=context_roles,
            idempotent_replay=idempotent_replay,
            config=config,
            manifest=manifest,
            projection=projection,
            observation_ledger=membership,
        )

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
    projection = _universe_projection(snapshot, manifest)
    _validate_probe_snapshot_binding(snapshot, probe_receipt, manifest)
    readback, membership, idempotent_replay = _persist_observation_transaction(
        root=config.snapshot_root,
        store=store,
        manifest=manifest,
        snapshot=snapshot,
        probe_receipt=probe_receipt,
        projection=projection,
        context_roles=context_roles,
        recovered_snapshot_required=False,
    )
    return _result(
        snapshot=readback,
        probe_receipt=probe_receipt,
        context_roles=context_roles,
        idempotent_replay=idempotent_replay,
        config=config,
        manifest=manifest,
        projection=projection,
        observation_ledger=membership,
    )


__all__ = [
    "AshareObservationBlocked",
    "AshareObservationConfig",
    "AshareObservationConfigurationError",
    "AshareObservationResult",
    "OBSERVATION_SCHEMA_ID",
    "OBSERVATION_UNIVERSE_SEMANTICS",
    "assert_no_plaintext_token_environment",
    "run_ashare_observation",
]
