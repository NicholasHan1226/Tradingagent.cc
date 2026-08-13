"""Crypto-local, audit-only storage for delayed-paper observations.

This module deliberately does not model cash, positions, orders, fills, or a
capital commit.  The existing fixture simulator remains the sole capital
writer.  This store only makes the input observation durable *before* that
simulator is called, records immutable completion markers, and appends compact
non-authoritative decision/rejection audit events.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import uuid
from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Mapping


OBSERVATION_CONTRACT = "tradingagent.crypto.delayed_paper_observation.v1"
COMPLETION_CONTRACT = "tradingagent.crypto.delayed_paper_completion.v1"
DECISION_LEDGER_CONTRACT = "tradingagent.crypto.delayed_paper_decision_ledger.v1"
OBSERVATION_STATE_CONTRACT = "tradingagent.crypto.delayed_paper_state.v1"
DECISION_LEDGER_STATE_CONTRACT = (
    "tradingagent.crypto.delayed_paper_decision_ledger_state.v1"
)
LOCAL_AUDIT_DURABILITY = "local_audit_fsync_only"
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_LEDGER_BYTES = 16 * 1024 * 1024
LEDGER_ROTATION_TARGET_BYTES = 1 * 1024 * 1024
LEDGER_SEGMENT_PREFIX = "decision_ledger.segment-"
LEDGER_SEGMENT_SUFFIX = ".jsonl"


class CryptoDelayedPaperLedgerError(RuntimeError):
    """Raised when local audit evidence is unsafe, conflicting, or corrupt."""


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CryptoDelayedPaperLedgerError("delayed_paper_decimal_not_finite")
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_timestamp_timezone_required"
            )
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonical_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or key != key.strip():
                raise CryptoDelayedPaperLedgerError("delayed_paper_mapping_key_invalid")
            if key in normalized:
                raise CryptoDelayedPaperLedgerError(
                    "delayed_paper_mapping_key_duplicated"
                )
            normalized[key] = _canonical_value(item)
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, float):
        raise CryptoDelayedPaperLedgerError("delayed_paper_float_not_allowed")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    raise CryptoDelayedPaperLedgerError(
        f"delayed_paper_value_type_invalid:{type(value).__name__}"
    )


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            _canonical_value(value),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise CryptoDelayedPaperLedgerError("delayed_paper_json_not_canonical") from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _market_slot(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise CryptoDelayedPaperLedgerError("delayed_paper_market_slot_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CryptoDelayedPaperLedgerError(
            "delayed_paper_market_slot_invalid"
        ) from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.utcoffset() != timezone.utc.utcoffset(parsed)
    ):
        raise CryptoDelayedPaperLedgerError("delayed_paper_market_slot_invalid")
    return parsed.astimezone(timezone.utc)


def _non_authority_fields() -> dict[str, Any]:
    return {
        "authority": "none",
        "execution_eligible": False,
        "execution_authority": False,
        "durable_execution_receipt": False,
        "production_eligible": False,
        "real_trading_enabled": False,
        "network_used": False,
        "testnet_used": False,
        "live_broker_used": False,
        "promotion_authorized": False,
        "outbox_id": None,
        "capital_commit_id": None,
        "durability_scope": LOCAL_AUDIT_DURABILITY,
    }


def _regular_single_link(path: Path, *, reason: str) -> None:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise CryptoDelayedPaperLedgerError(reason)


def _ensure_directory(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise CryptoDelayedPaperLedgerError("delayed_paper_directory_invalid")
        return
    path.mkdir(parents=True, exist_ok=False, mode=0o700)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, encoded: bytes) -> None:
    """Write every byte or fail without publishing a partial target file."""

    offset = 0
    while offset < len(encoded):
        written = os.write(descriptor, encoded[offset:])
        if written <= 0:
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_decision_ledger_short_write"
            )
        offset += written


def _write_decision_ledger_atomic(
    path: Path,
    rows: list[Mapping[str, Any]],
) -> None:
    encoded = b"".join((_canonical_json(row) + "\n").encode("utf-8") for row in rows)
    if not encoded or len(encoded) > MAX_LEDGER_BYTES:
        raise CryptoDelayedPaperLedgerError(
            "delayed_paper_decision_ledger_size_invalid"
        )

    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        _write_all(descriptor, encoded)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> dict[str, Any]:
    _regular_single_link(path, reason="delayed_paper_artifact_file_invalid")
    size = path.stat().st_size
    if size <= 0 or size > MAX_ARTIFACT_BYTES:
        raise CryptoDelayedPaperLedgerError("delayed_paper_artifact_size_invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CryptoDelayedPaperLedgerError(
            "delayed_paper_artifact_json_invalid"
        ) from exc
    if not isinstance(value, dict):
        raise CryptoDelayedPaperLedgerError("delayed_paper_artifact_object_required")
    if _canonical_json(value).encode("utf-8") + b"\n" != path.read_bytes():
        raise CryptoDelayedPaperLedgerError("delayed_paper_artifact_not_canonical")
    return value


def _write_immutable_json(path: Path, value: Mapping[str, Any]) -> dict[str, Any]:
    canonical = _canonical_value(value)
    if not isinstance(canonical, dict):
        raise CryptoDelayedPaperLedgerError("delayed_paper_artifact_object_required")
    encoded = (_canonical_json(canonical) + "\n").encode("utf-8")
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise CryptoDelayedPaperLedgerError("delayed_paper_artifact_size_invalid")
    if path.exists():
        existing = _read_json(path)
        if _canonical_json(existing) != _canonical_json(canonical):
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_artifact_content_conflict"
            )
        return existing

    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return canonical


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> dict[str, Any]:
    canonical = _canonical_value(value)
    if not isinstance(canonical, dict):
        raise CryptoDelayedPaperLedgerError("delayed_paper_state_object_required")
    encoded = (_canonical_json(canonical) + "\n").encode("utf-8")
    if not encoded or len(encoded) > MAX_ARTIFACT_BYTES:
        raise CryptoDelayedPaperLedgerError("delayed_paper_state_size_invalid")
    if path.exists() or path.is_symlink():
        _regular_single_link(
            path,
            reason="delayed_paper_state_file_invalid",
        )
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise CryptoDelayedPaperLedgerError(
            "delayed_paper_state_persist_failed"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return canonical


class CryptoDelayedPaperObservationStore:
    """Immutable observation/completion store plus append-only audit ledger."""

    def __init__(self, output_root: Path | str) -> None:
        root = Path(output_root)
        if root.exists() and root.is_symlink():
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_output_root_symlink_not_allowed"
            )
        _ensure_directory(root)
        self.root = root / "delayed_paper"
        _ensure_directory(self.root)
        self.observations_dir = self.root / "observations"
        self.completions_dir = self.root / "completions"
        _ensure_directory(self.observations_dir)
        _ensure_directory(self.completions_dir)
        self.ledger_path = self.root / "decision_ledger.jsonl"
        self.lock_path = self.root / ".lock"
        self.cycle_lock_path = self.root / ".cycle.lock"
        self.observation_state_path = self.root / "observation_state.json"
        self.ledger_state_path = self.root / "decision_ledger_state.json"
        self.event_index_dir = self.root / "event_index"
        self.observation_event_index_dir = self.root / "observation_event_index"
        self._verified_ledger_events_by_sequence: dict[int, dict[str, Any]] | None = None

    @contextmanager
    def _file_lock(self, path: Path) -> Iterator[None]:
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise CryptoDelayedPaperLedgerError("delayed_paper_lock_file_invalid")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._file_lock(self.lock_path):
            yield

    @contextmanager
    def _read_only_locked(self, *, nonblocking: bool = False) -> Iterator[None]:
        """Take a shared lock without creating or modifying a lock file.

        Runtime recovery may create a missing lock and rebuild the O(1) state
        index.  A health reader must never do either: a missing lock or stale
        state is evidence that needs an operator/runtime repair, not a reason
        for monitoring to mutate the capital-generation root.
        """

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.lock_path, flags)
        except OSError as exc:
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_readonly_lock_unavailable"
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise CryptoDelayedPaperLedgerError("delayed_paper_lock_file_invalid")
            flags = fcntl.LOCK_SH | (fcntl.LOCK_NB if nonblocking else 0)
            try:
                fcntl.flock(descriptor, flags)
            except BlockingIOError as exc:
                raise CryptoDelayedPaperLedgerError(
                    "delayed_paper_readonly_lock_busy"
                ) from exc
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    @contextmanager
    def cycle(self) -> Iterator[None]:
        """Serialize one complete pending/read/accept/execute/complete cycle."""

        with self._file_lock(self.cycle_lock_path):
            yield

    @staticmethod
    def _observation_id(observation: Mapping[str, Any]) -> str:
        value = observation.get("observation_id")
        if (
            not isinstance(value, str)
            or not value.startswith("crypto-delayed-observation-")
            or len(value) > 96
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789-_"
                for character in value
            )
        ):
            raise CryptoDelayedPaperLedgerError("delayed_paper_observation_id_invalid")
        return value

    def _observation_path(self, observation_id: str) -> Path:
        return self.observations_dir / f"{observation_id}.json"

    def _completion_path(self, observation_id: str) -> Path:
        return self.completions_dir / f"{observation_id}.json"

    @staticmethod
    def _observation_state_payload(
        *,
        latest_observation_id: str | None,
        latest_market_slot: str | None,
        pending_observation_id: str | None,
        observation_count: int,
        completion_count: int,
        latest_observation_content_sha256: str | None,
        latest_completion_sha256: str | None,
        observations_directory_mtime_ns: int,
        completions_directory_mtime_ns: int,
    ) -> dict[str, Any]:
        state: dict[str, Any] = {
            "contract": OBSERVATION_STATE_CONTRACT,
            "latest_observation_id": latest_observation_id,
            "latest_market_slot": latest_market_slot,
            "pending_observation_id": pending_observation_id,
            "observation_count": observation_count,
            "completion_count": completion_count,
            "latest_observation_content_sha256": (latest_observation_content_sha256),
            "latest_completion_sha256": latest_completion_sha256,
            "observations_directory_mtime_ns": (observations_directory_mtime_ns),
            "completions_directory_mtime_ns": (completions_directory_mtime_ns),
            **_non_authority_fields(),
        }
        state["state_sha256"] = _sha256(state)
        return state

    def _verify_observation_state(
        self,
        state: Mapping[str, Any],
    ) -> dict[str, Any]:
        canonical = _canonical_value(state)
        if not isinstance(canonical, dict):
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_observation_state_invalid"
            )
        material = dict(canonical)
        claimed = material.pop("state_sha256", None)
        counts = (
            canonical.get("observation_count"),
            canonical.get("completion_count"),
        )
        mtimes = (
            canonical.get("observations_directory_mtime_ns"),
            canonical.get("completions_directory_mtime_ns"),
        )
        if (
            canonical.get("contract") != OBSERVATION_STATE_CONTRACT
            or claimed != _sha256(material)
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in counts
            )
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in mtimes
            )
            or counts[1] > counts[0]
        ):
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_observation_state_invalid"
            )
        latest_id = canonical.get("latest_observation_id")
        pending_id = canonical.get("pending_observation_id")
        if latest_id is None:
            if (
                canonical.get("latest_market_slot") is not None
                or canonical.get("latest_observation_content_sha256") is not None
                or counts != (0, 0)
                or pending_id is not None
            ):
                raise CryptoDelayedPaperLedgerError(
                    "delayed_paper_observation_state_invalid"
                )
            return canonical
        latest_id = self._observation_id({"observation_id": latest_id})
        latest = _read_json(self._observation_path(latest_id))
        self._verify_observation(latest)
        if (
            latest.get("market_slot") != canonical.get("latest_market_slot")
            or latest.get("observation_content_sha256")
            != canonical.get("latest_observation_content_sha256")
            or counts[0] <= 0
        ):
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_observation_state_invalid"
            )
        _market_slot(canonical.get("latest_market_slot"))
        if pending_id is not None:
            pending_id = self._observation_id({"observation_id": pending_id})
            pending = _read_json(self._observation_path(pending_id))
            self._verify_observation(pending)
            if self._completion_path(pending_id).exists():
                raise CryptoDelayedPaperLedgerError(
                    "delayed_paper_observation_state_invalid"
                )
        latest_completion_sha = canonical.get("latest_completion_sha256")
        latest_completion_path = self._completion_path(latest_id)
        if latest_completion_path.exists():
            completion = _read_json(latest_completion_path)
            self._verify_completion(completion, observation=latest)
            if completion.get("completion_sha256") != latest_completion_sha:
                raise CryptoDelayedPaperLedgerError(
                    "delayed_paper_observation_state_invalid"
                )
        elif latest_id != pending_id or latest_completion_sha is not None:
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_observation_state_invalid"
            )
        return canonical

    def _rebuild_observation_state(self) -> dict[str, Any]:
        observations: list[tuple[datetime, dict[str, Any]]] = []
        pending: list[str] = []
        completion_count = 0
        latest_completion_sha: str | None = None
        for path in sorted(self.observations_dir.glob("*.json")):
            observation = _read_json(path)
            observation_id = self._verify_observation(observation)
            if path.name != f"{observation_id}.json":
                raise CryptoDelayedPaperLedgerError(
                    "delayed_paper_observation_filename_mismatch"
                )
            slot = _market_slot(observation.get("market_slot"))
            completion_path = self._completion_path(observation_id)
            if completion_path.exists():
                completion = _read_json(completion_path)
                self._verify_completion(
                    completion,
                    observation=observation,
                )
                completion_count += 1
            else:
                pending.append(observation_id)
            observations.append((slot, observation))
        if len(pending) > 1:
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_multiple_pending_observations"
            )
        observations.sort(key=lambda item: item[0])
        latest = observations[-1][1] if observations else None
        if latest is not None:
            latest_path = self._completion_path(str(latest["observation_id"]))
            if latest_path.exists():
                latest_completion_sha = _read_json(latest_path).get("completion_sha256")
        state = self._observation_state_payload(
            latest_observation_id=(
                str(latest["observation_id"]) if latest is not None else None
            ),
            latest_market_slot=(
                str(latest["market_slot"]) if latest is not None else None
            ),
            pending_observation_id=(pending[0] if pending else None),
            observation_count=len(observations),
            completion_count=completion_count,
            latest_observation_content_sha256=(
                str(latest["observation_content_sha256"])
                if latest is not None
                else None
            ),
            latest_completion_sha256=latest_completion_sha,
            observations_directory_mtime_ns=(self.observations_dir.stat().st_mtime_ns),
            completions_directory_mtime_ns=(self.completions_dir.stat().st_mtime_ns),
        )
        _write_json_atomic(self.observation_state_path, state)
        return state

    def _observation_state(self) -> dict[str, Any]:
        if not self.observation_state_path.exists():
            return self._rebuild_observation_state()
        raw = _read_json(self.observation_state_path)
        if (
            raw.get("observations_directory_mtime_ns")
            != self.observations_dir.stat().st_mtime_ns
            or raw.get("completions_directory_mtime_ns")
            != self.completions_dir.stat().st_mtime_ns
        ):
            return self._rebuild_observation_state()
        return self._verify_observation_state(raw)

    def _observation_state_read_only(self) -> dict[str, Any]:
        """Verify the persisted O(1) state without a repair/rebuild path."""

        if not self.observation_state_path.exists():
            raise CryptoDelayedPaperLedgerError("delayed_paper_readonly_state_missing")
        raw = _read_json(self.observation_state_path)
        if (
            raw.get("observations_directory_mtime_ns")
            != self.observations_dir.stat().st_mtime_ns
            or raw.get("completions_directory_mtime_ns")
            != self.completions_dir.stat().st_mtime_ns
        ):
            raise CryptoDelayedPaperLedgerError("delayed_paper_readonly_state_stale")
        return self._verify_observation_state(raw)

    def runtime_checkpoint(self) -> dict[str, Any]:
        """Return O(1) latest/pending state after one-time legacy rebuild."""

        with self._locked():
            state = self._observation_state()
            pending_id = state.get("pending_observation_id")
            pending = (
                _read_json(self._observation_path(str(pending_id)))
                if pending_id is not None
                else None
            )
            return {
                "pending": pending,
                "latest_market_slot": state.get("latest_market_slot"),
                "observation_count": state.get("observation_count"),
                "completion_count": state.get("completion_count"),
            }

    def runtime_checkpoint_read_only(
        self, *, nonblocking: bool = False
    ) -> dict[str, Any]:
        """Return the verified checkpoint without creating or repairing state."""

        with self._read_only_locked(nonblocking=nonblocking):
            state = self._observation_state_read_only()
            pending_id = state.get("pending_observation_id")
            pending = (
                _read_json(self._observation_path(str(pending_id)))
                if pending_id is not None
                else None
            )
            return {
                "pending": pending,
                "latest_market_slot": state.get("latest_market_slot"),
                "observation_count": state.get("observation_count"),
                "completion_count": state.get("completion_count"),
            }

    def _verify_observation(self, observation: Mapping[str, Any]) -> str:
        observation_id = self._observation_id(observation)
        if observation.get("contract") != OBSERVATION_CONTRACT:
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_observation_contract_invalid"
            )
        digest_material = dict(observation)
        claimed_digest = digest_material.pop("observation_content_sha256", None)
        if claimed_digest != _sha256(digest_material):
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_observation_digest_mismatch"
            )
        return observation_id

    def accept(self, observation: Mapping[str, Any]) -> dict[str, Any]:
        canonical = _canonical_value(observation)
        if not isinstance(canonical, dict):
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_observation_object_required"
            )
        observation_id = self._verify_observation(canonical)
        with self._locked():
            state = self._observation_state()
            market_slot = canonical.get("market_slot")
            parsed_market_slot = _market_slot(market_slot)
            latest_id = state.get("latest_observation_id")
            pending_id = state.get("pending_observation_id")
            existing = (
                _read_json(self._observation_path(str(latest_id)))
                if latest_id is not None
                else None
            )
            if existing is not None:
                existing_market_slot = existing.get("market_slot")
                parsed_existing_slot = _market_slot(existing_market_slot)
                if existing_market_slot == market_slot and existing.get(
                    "observation_content_sha256"
                ) != canonical.get("observation_content_sha256"):
                    raise CryptoDelayedPaperLedgerError(
                        "delayed_paper_slot_payload_conflict"
                    )
                if parsed_market_slot < parsed_existing_slot:
                    raise CryptoDelayedPaperLedgerError(
                        "delayed_paper_slot_not_monotonic"
                    )
                if pending_id is not None and pending_id != observation_id:
                    raise CryptoDelayedPaperLedgerError(
                        "delayed_paper_prior_observation_pending"
                    )
            existed = self._observation_path(observation_id).exists()
            stored = _write_immutable_json(
                self._observation_path(observation_id),
                canonical,
            )
            completion_path = self._completion_path(observation_id)
            completion = (
                _read_json(completion_path) if completion_path.exists() else None
            )
            if completion is not None:
                self._verify_completion(
                    completion,
                    observation=stored,
                )
            next_state = self._observation_state_payload(
                latest_observation_id=observation_id,
                latest_market_slot=str(market_slot),
                pending_observation_id=(
                    None if completion is not None else observation_id
                ),
                observation_count=(
                    int(state["observation_count"]) + (0 if existed else 1)
                ),
                completion_count=int(state["completion_count"]),
                latest_observation_content_sha256=str(
                    stored["observation_content_sha256"]
                ),
                latest_completion_sha256=(
                    str(completion["completion_sha256"])
                    if completion is not None
                    else None
                ),
                observations_directory_mtime_ns=(
                    self.observations_dir.stat().st_mtime_ns
                ),
                completions_directory_mtime_ns=(
                    self.completions_dir.stat().st_mtime_ns
                ),
            )
            _write_json_atomic(
                self.observation_state_path,
                next_state,
            )
            return stored

    def pending_observation(self) -> dict[str, Any] | None:
        with self._locked():
            state = self._observation_state()
            pending_id = state.get("pending_observation_id")
            if pending_id is None:
                return None
            observation = _read_json(self._observation_path(str(pending_id)))
            self._verify_observation(observation)
            if self._completion_path(str(pending_id)).exists():
                raise CryptoDelayedPaperLedgerError(
                    "delayed_paper_observation_state_invalid"
                )
            return observation

    def _verify_completion(
        self,
        completion: Mapping[str, Any],
        *,
        observation: Mapping[str, Any],
    ) -> None:
        observation_id = self._observation_id(observation)
        if (
            completion.get("contract") != COMPLETION_CONTRACT
            or completion.get("observation_id") != observation_id
            or completion.get("observation_content_sha256")
            != observation.get("observation_content_sha256")
            or completion.get("authority") != "none"
            or completion.get("execution_authority") is not False
            or completion.get("production_eligible") is not False
        ):
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_completion_binding_invalid"
            )
        material = dict(completion)
        claimed = material.pop("completion_sha256", None)
        if claimed != _sha256(material):
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_completion_digest_mismatch"
            )

    def mark_complete(
        self,
        observation: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        canonical_observation = _canonical_value(observation)
        canonical_result = _canonical_value(result)
        if not isinstance(canonical_observation, dict) or not isinstance(
            canonical_result, dict
        ):
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_completion_object_required"
            )
        observation_id = self._observation_id(canonical_observation)
        symbols = canonical_result.get("symbols")
        if not isinstance(symbols, Mapping):
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_completion_symbols_missing"
            )
        bundle_references: dict[str, Any] = {}
        for symbol, item in sorted(symbols.items()):
            if not isinstance(symbol, str) or not isinstance(item, Mapping):
                raise CryptoDelayedPaperLedgerError(
                    "delayed_paper_completion_symbol_invalid"
                )
            bundle = item.get("bundle")
            risk_reject = item.get("risk_reject")
            if isinstance(bundle, Mapping):
                bundle_references[symbol] = {
                    "disposition": item.get("disposition"),
                    "run_id": bundle.get("run_id"),
                    "business_bundle_sha256": bundle.get("business_bundle_sha256"),
                    "decision_id": (
                        bundle.get("decision", {}).get("decision_id")
                        if isinstance(bundle.get("decision"), Mapping)
                        else None
                    ),
                    "risk_reject_event_id": None,
                    "risk_reason_code": None,
                }
                continue
            if not isinstance(risk_reject, Mapping):
                raise CryptoDelayedPaperLedgerError(
                    "delayed_paper_completion_reference_missing"
                )
            bundle_references[symbol] = {
                "disposition": item.get("disposition"),
                "run_id": None,
                "business_bundle_sha256": None,
                "decision_id": (
                    risk_reject.get("decision", {}).get("decision_id")
                    if isinstance(risk_reject.get("decision"), Mapping)
                    else None
                ),
                "risk_reject_event_id": risk_reject.get("event_id"),
                "risk_reason_code": risk_reject.get("reason_code"),
            }
        completion: dict[str, Any] = {
            "contract": COMPLETION_CONTRACT,
            "observation_id": observation_id,
            "observation_content_sha256": canonical_observation.get(
                "observation_content_sha256"
            ),
            "status": canonical_result.get("status"),
            "bundle_references": bundle_references,
            **_non_authority_fields(),
        }
        completion["completion_sha256"] = _sha256(completion)
        with self._locked():
            state = self._observation_state()
            stored_observation = _read_json(self._observation_path(observation_id))
            self._verify_observation(stored_observation)
            if _canonical_json(stored_observation) != _canonical_json(
                canonical_observation
            ):
                raise CryptoDelayedPaperLedgerError(
                    "delayed_paper_completion_observation_conflict"
                )
            completion_path = self._completion_path(observation_id)
            existed = completion_path.exists()
            stored = _write_immutable_json(
                completion_path,
                completion,
            )
            self._verify_completion(stored, observation=stored_observation)
            latest_id = state.get("latest_observation_id")
            next_state = self._observation_state_payload(
                latest_observation_id=(
                    str(latest_id) if latest_id is not None else None
                ),
                latest_market_slot=state.get("latest_market_slot"),
                pending_observation_id=(
                    None
                    if state.get("pending_observation_id") == observation_id
                    else state.get("pending_observation_id")
                ),
                observation_count=int(state["observation_count"]),
                completion_count=(
                    int(state["completion_count"]) + (0 if existed else 1)
                ),
                latest_observation_content_sha256=state.get(
                    "latest_observation_content_sha256"
                ),
                latest_completion_sha256=(
                    str(stored["completion_sha256"])
                    if latest_id == observation_id
                    else state.get("latest_completion_sha256")
                ),
                observations_directory_mtime_ns=(
                    self.observations_dir.stat().st_mtime_ns
                ),
                completions_directory_mtime_ns=(
                    self.completions_dir.stat().st_mtime_ns
                ),
            )
            _write_json_atomic(
                self.observation_state_path,
                next_state,
            )
            return stored

    def _read_ledger(self) -> list[dict[str, Any]]:
        rows, _, _ = self._read_ledger_state()
        return rows

    def _segment_paths(self) -> list[Path]:
        indexed: list[tuple[int, Path]] = []
        for path in self.root.glob(f"{LEDGER_SEGMENT_PREFIX}*{LEDGER_SEGMENT_SUFFIX}"):
            token = path.name[len(LEDGER_SEGMENT_PREFIX) : -len(LEDGER_SEGMENT_SUFFIX)]
            if len(token) != 6 or not token.isdigit() or int(token) <= 0:
                raise CryptoDelayedPaperLedgerError(
                    "delayed_paper_decision_ledger_segment_name_invalid"
                )
            indexed.append((int(token), path))
        indexed.sort()
        if [index for index, _ in indexed] != list(range(1, len(indexed) + 1)):
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_decision_ledger_segment_sequence_invalid"
            )
        return [path for _, path in indexed]

    @staticmethod
    def _read_ledger_file(
        path: Path,
        *,
        start_sequence: int,
        previous_checksum: str,
    ) -> list[dict[str, Any]]:
        _regular_single_link(
            path,
            reason="delayed_paper_decision_ledger_file_invalid",
        )
        raw = path.read_bytes()
        if not raw or len(raw) > MAX_LEDGER_BYTES:
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_decision_ledger_too_large"
            )
        if not raw.endswith(b"\n"):
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_decision_ledger_partial_tail"
            )
        rows: list[dict[str, Any]] = []
        expected_previous = previous_checksum
        for offset, line in enumerate(raw.splitlines()):
            sequence = start_sequence + offset
            try:
                row = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CryptoDelayedPaperLedgerError(
                    "delayed_paper_decision_ledger_json_invalid"
                ) from exc
            if not isinstance(row, dict):
                raise CryptoDelayedPaperLedgerError(
                    "delayed_paper_decision_ledger_row_invalid"
                )
            material = dict(row)
            checksum = material.pop("checksum", None)
            if (
                material.get("sequence") != sequence
                or material.get("previous_checksum") != expected_previous
                or checksum != _sha256(material)
            ):
                raise CryptoDelayedPaperLedgerError(
                    "delayed_paper_decision_ledger_chain_invalid"
                )
            expected_previous = str(checksum)
            rows.append(row)
        return rows

    def _read_ledger_state(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
        all_rows: list[dict[str, Any]] = []
        previous_checksum = "0" * 64
        segment_paths = self._segment_paths()
        for path in segment_paths:
            segment_rows = self._read_ledger_file(
                path,
                start_sequence=len(all_rows) + 1,
                previous_checksum=previous_checksum,
            )
            all_rows.extend(segment_rows)
            previous_checksum = str(all_rows[-1]["checksum"])

        current_rows: list[dict[str, Any]] = []
        if self.ledger_path.exists():
            current_rows = self._read_ledger_file(
                self.ledger_path,
                start_sequence=len(all_rows) + 1,
                previous_checksum=previous_checksum,
            )
            all_rows.extend(current_rows)
        return all_rows, current_rows, len(segment_paths)

    def _rotate_current_ledger(self, segment_count: int) -> None:
        if not self.ledger_path.exists():
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_decision_ledger_rotation_source_missing"
            )
        target = self.root / (
            f"{LEDGER_SEGMENT_PREFIX}{segment_count + 1:06d}{LEDGER_SEGMENT_SUFFIX}"
        )
        if target.exists() or target.is_symlink():
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_decision_ledger_segment_conflict"
            )
        os.replace(self.ledger_path, target)
        _fsync_directory(self.root)

    @staticmethod
    def _safe_event_id(value: Any) -> str:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 160
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789-_"
                for character in value
            )
        ):
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_decision_event_id_invalid"
            )
        return value

    @staticmethod
    def _safe_symbol(value: Any) -> str:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 24
            or any(
                character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
                for character in value
            )
        ):
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_decision_event_symbol_invalid"
            )
        return value

    def _write_event_indexes(self, row: Mapping[str, Any]) -> None:
        event_id = self._safe_event_id(row.get("event_id"))
        _ensure_directory(self.event_index_dir)
        _write_immutable_json(
            self.event_index_dir / f"{event_id}.json",
            row,
        )
        if row.get("event_type") not in {"decision", "risk_reject"}:
            return
        observation_id = self._observation_id(row)
        symbol = self._safe_symbol(row.get("symbol"))
        observation_dir = self.observation_event_index_dir / observation_id
        _ensure_directory(self.observation_event_index_dir)
        _ensure_directory(observation_dir)
        _write_immutable_json(
            observation_dir / f"{symbol}.json",
            row,
        )

    def _ledger_event_at_sequence(self, sequence: int) -> dict[str, Any]:
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_decision_event_index_invalid"
            )
        if self._verified_ledger_events_by_sequence is None:
            rows = self._read_ledger()
            indexed = {row.get("sequence"): row for row in rows}
            if len(indexed) != len(rows) or any(
                isinstance(key, bool) or not isinstance(key, int) or key <= 0
                for key in indexed
            ):
                raise CryptoDelayedPaperLedgerError(
                    "delayed_paper_decision_event_index_invalid"
                )
            self._verified_ledger_events_by_sequence = indexed
        try:
            return self._verified_ledger_events_by_sequence[sequence]
        except KeyError as exc:
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_decision_event_index_invalid"
            ) from exc

    def _verify_indexed_event(
        self,
        row: Mapping[str, Any],
    ) -> dict[str, Any]:
        canonical = _canonical_value(row)
        if not isinstance(canonical, dict):
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_decision_event_index_invalid"
            )
        event_id = self._safe_event_id(canonical.get("event_id"))
        sequence = canonical.get("sequence")
        previous_checksum = canonical.get("previous_checksum")
        checksum = canonical.get("checksum")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence <= 0
            or not isinstance(previous_checksum, str)
            or len(previous_checksum) != 64
            or any(
                character not in "0123456789abcdef" for character in previous_checksum
            )
            or not isinstance(checksum, str)
            or len(checksum) != 64
            or any(character not in "0123456789abcdef" for character in checksum)
        ):
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_decision_event_index_invalid"
            )
        material = dict(canonical)
        material.pop("checksum")
        if checksum != _sha256(material):
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_decision_event_index_invalid"
            )
        authoritative = _read_json(self.event_index_dir / f"{event_id}.json")
        if _canonical_json(authoritative) != _canonical_json(canonical):
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_decision_event_index_invalid"
            )
        ledger_row = self._ledger_event_at_sequence(sequence)
        if _canonical_json(ledger_row) != _canonical_json(canonical):
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_decision_event_index_invalid"
            )
        return canonical

    def events_for_observation(
        self,
        observation_id: str,
    ) -> list[dict[str, Any]]:
        """Read the two indexed decision events without replaying history."""

        validated_id = self._observation_id({"observation_id": observation_id})
        observation_dir = self.observation_event_index_dir / validated_id
        if not observation_dir.is_dir():
            rows = [
                row
                for row in self._read_ledger()
                if row.get("observation_id") == validated_id
                and row.get("event_type") in {"decision", "risk_reject"}
            ]
            for row in rows:
                self._write_event_indexes(row)
            return rows
        rows: list[dict[str, Any]] = []
        for path in sorted(observation_dir.glob("*.json")):
            symbol = self._safe_symbol(path.stem)
            row = self._verify_indexed_event(_read_json(path))
            if (
                row.get("observation_id") != validated_id
                or row.get("symbol") != symbol
                or row.get("event_type") not in {"decision", "risk_reject"}
            ):
                raise CryptoDelayedPaperLedgerError(
                    "delayed_paper_observation_event_index_invalid"
                )
            rows.append(row)
        return rows

    def data_gap_events(self) -> list[dict[str, Any]]:
        """Return checksum/index/ledger-bound outage gap receipts in sequence."""

        rows = [
            row for row in self._read_ledger() if row.get("event_type") == "data_gap"
        ]
        return [self._verify_indexed_event(row) for row in rows]

    def data_reject_events(self) -> list[dict[str, Any]]:
        """Return checksum/index/ledger-bound rejected data requests in sequence."""

        rows = [
            row
            for row in self._read_ledger()
            if row.get("event_type") == "data_reject"
        ]
        return [self._verify_indexed_event(row) for row in rows]

    @staticmethod
    def _ledger_state_payload(
        *,
        sequence: int,
        last_checksum: str,
        segment_count: int,
        current_start_sequence: int,
        current_start_previous_checksum: str,
        current_row_count: int,
        current_file_sha256: str | None,
    ) -> dict[str, Any]:
        state: dict[str, Any] = {
            "contract": DECISION_LEDGER_STATE_CONTRACT,
            "sequence": sequence,
            "last_checksum": last_checksum,
            "segment_count": segment_count,
            "current_start_sequence": current_start_sequence,
            "current_start_previous_checksum": (current_start_previous_checksum),
            "current_row_count": current_row_count,
            "current_file_sha256": current_file_sha256,
            **_non_authority_fields(),
        }
        state["state_sha256"] = _sha256(state)
        return state

    def _rebuild_ledger_runtime_state(self) -> dict[str, Any]:
        rows, current_rows, segment_count = self._read_ledger_state()
        for row in rows:
            self._write_event_indexes(row)
        last_checksum = str(rows[-1]["checksum"]) if rows else "0" * 64
        current_start_sequence = (
            int(current_rows[0]["sequence"]) if current_rows else len(rows) + 1
        )
        current_start_previous = (
            str(current_rows[0]["previous_checksum"]) if current_rows else last_checksum
        )
        current_sha = (
            hashlib.sha256(self.ledger_path.read_bytes()).hexdigest()
            if self.ledger_path.exists()
            else None
        )
        state = self._ledger_state_payload(
            sequence=len(rows),
            last_checksum=last_checksum,
            segment_count=segment_count,
            current_start_sequence=current_start_sequence,
            current_start_previous_checksum=current_start_previous,
            current_row_count=len(current_rows),
            current_file_sha256=current_sha,
        )
        _write_json_atomic(self.ledger_state_path, state)
        return state

    def _ledger_runtime_state(self) -> dict[str, Any]:
        if not self.ledger_state_path.exists():
            return self._rebuild_ledger_runtime_state()
        state = _read_json(self.ledger_state_path)
        material = dict(state)
        claimed = material.pop("state_sha256", None)
        integer_fields = (
            state.get("sequence"),
            state.get("segment_count"),
            state.get("current_start_sequence"),
            state.get("current_row_count"),
        )
        if (
            state.get("contract") != DECISION_LEDGER_STATE_CONTRACT
            or claimed != _sha256(material)
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in integer_fields
            )
            or state.get("current_start_sequence")
            != state.get("sequence") - state.get("current_row_count") + 1
            or not isinstance(state.get("last_checksum"), str)
            or len(str(state.get("last_checksum"))) != 64
            or not isinstance(
                state.get("current_start_previous_checksum"),
                str,
            )
            or len(str(state.get("current_start_previous_checksum"))) != 64
        ):
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_decision_ledger_state_invalid"
            )
        expected_current_sha = state.get("current_file_sha256")
        if self.ledger_path.exists():
            actual_current_sha = hashlib.sha256(
                self.ledger_path.read_bytes()
            ).hexdigest()
            if actual_current_sha != expected_current_sha:
                return self._rebuild_ledger_runtime_state()
        elif expected_current_sha is not None or state.get("current_row_count") != 0:
            return self._rebuild_ledger_runtime_state()
        return state

    def _current_rows_from_runtime_state(
        self,
        state: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        if not self.ledger_path.exists():
            return []
        rows = self._read_ledger_file(
            self.ledger_path,
            start_sequence=int(state["current_start_sequence"]),
            previous_checksum=str(state["current_start_previous_checksum"]),
        )
        if len(rows) != state.get("current_row_count") or (
            rows and rows[-1].get("checksum") != state.get("last_checksum")
        ):
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_decision_ledger_state_invalid"
            )
        return rows

    def append_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        canonical = _canonical_value(event)
        if not isinstance(canonical, dict):
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_decision_event_object_required"
            )
        forbidden = {"sequence", "previous_checksum", "checksum"}
        if forbidden.intersection(canonical):
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_decision_event_chain_fields_forbidden"
            )
        event_id = canonical.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise CryptoDelayedPaperLedgerError(
                "delayed_paper_decision_event_id_required"
            )
        for key, expected in _non_authority_fields().items():
            if canonical.get(key) != expected:
                raise CryptoDelayedPaperLedgerError(
                    "delayed_paper_decision_event_authority_invalid"
                )
        with self._locked():
            state = self._ledger_runtime_state()
            event_id = self._safe_event_id(event_id)
            event_index_path = self.event_index_dir / f"{event_id}.json"
            if event_index_path.exists():
                indexed = self._verify_indexed_event(_read_json(event_index_path))
                existing = dict(indexed)
                for key in forbidden:
                    existing.pop(key, None)
                if _canonical_json(existing) != _canonical_json(canonical):
                    raise CryptoDelayedPaperLedgerError(
                        "delayed_paper_decision_event_content_conflict"
                    )
                self._write_event_indexes(indexed)
                return indexed

            current_rows = self._current_rows_from_runtime_state(state)
            row = {
                **canonical,
                "sequence": int(state["sequence"]) + 1,
                "previous_checksum": str(state["last_checksum"]),
            }
            row["checksum"] = _sha256(row)
            candidate_current = [*current_rows, row]
            candidate_bytes = b"".join(
                (_canonical_json(item) + "\n").encode("utf-8")
                for item in candidate_current
            )
            if len(candidate_bytes) > min(
                MAX_LEDGER_BYTES,
                LEDGER_ROTATION_TARGET_BYTES,
            ):
                if not current_rows:
                    raise CryptoDelayedPaperLedgerError(
                        "delayed_paper_decision_event_too_large"
                    )
                segment_count = int(state["segment_count"])
                self._rotate_current_ledger(segment_count)
                candidate_current = [row]
                segment_count += 1
                current_start_sequence = int(row["sequence"])
                current_start_previous = str(row["previous_checksum"])
            else:
                segment_count = int(state["segment_count"])
                current_start_sequence = int(state["current_start_sequence"])
                current_start_previous = str(state["current_start_previous_checksum"])
            _write_decision_ledger_atomic(
                self.ledger_path,
                candidate_current,
            )
            self._write_event_indexes(row)
            current_file_sha = hashlib.sha256(self.ledger_path.read_bytes()).hexdigest()
            next_state = self._ledger_state_payload(
                sequence=int(row["sequence"]),
                last_checksum=str(row["checksum"]),
                segment_count=segment_count,
                current_start_sequence=current_start_sequence,
                current_start_previous_checksum=current_start_previous,
                current_row_count=len(candidate_current),
                current_file_sha256=current_file_sha,
            )
            _write_json_atomic(self.ledger_state_path, next_state)
            return row


__all__ = [
    "COMPLETION_CONTRACT",
    "DECISION_LEDGER_CONTRACT",
    "DECISION_LEDGER_STATE_CONTRACT",
    "LOCAL_AUDIT_DURABILITY",
    "OBSERVATION_CONTRACT",
    "OBSERVATION_STATE_CONTRACT",
    "CryptoDelayedPaperLedgerError",
    "CryptoDelayedPaperObservationStore",
]
