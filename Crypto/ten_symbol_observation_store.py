"""Append-only ten-symbol observation ledger for the Crypto accumulator.

This store is deliberately independent of the BTC/ETH delayed-paper capital
path: it records observation / data_reject / data_gap evidence for the
ten-symbol 5-minute cohort and nothing else.  It keeps a per-row checksum
chain with an O(1) ``head.json`` checkpoint, an exclusive process lock,
atomic 16 MiB segment rotation, crash recovery from already-fsynced events,
idempotent same-slot replay and fail-closed same-slot conflict detection.
Every event carries fixed zero-authority fields.
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
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Mapping


TEN_SYMBOL_EVENT_CONTRACT = "tradingagent.crypto.ten_symbol_observation_event.v1"
TEN_SYMBOL_HEAD_CONTRACT = "tradingagent.crypto.ten_symbol_observation_head.v1"
TEN_SYMBOL_PENDING_CONTRACT = (
    "tradingagent.crypto.ten_symbol_observation_pending.v1"
)
TEN_SYMBOL_DATA_GAP_CONTRACT = "tradingagent.crypto.ten_symbol_observation_data_gap.v1"
EVENT_TYPES = frozenset({"observation", "data_reject", "data_gap"})
TERMINAL_SLOT_TYPES = frozenset({"observation", "data_gap"})
MAX_EVENTS_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
EVENTS_SEGMENT_PREFIX = "events.segment-"
EVENTS_SEGMENT_SUFFIX = ".jsonl"
FIVE_MINUTES = timedelta(minutes=5)


class CryptoTenSymbolObservationStoreError(RuntimeError):
    """Raised when observation evidence is unsafe, conflicting, or corrupt."""


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_decimal_not_finite"
            )
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_timestamp_timezone_required"
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
                raise CryptoTenSymbolObservationStoreError(
                    "ten_symbol_observation_mapping_key_invalid"
                )
            if key in normalized:
                raise CryptoTenSymbolObservationStoreError(
                    "ten_symbol_observation_mapping_key_duplicated"
                )
            normalized[key] = _canonical_value(item)
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, float):
        raise CryptoTenSymbolObservationStoreError(
            "ten_symbol_observation_float_not_allowed"
        )
    if value is None or isinstance(value, (str, bool, int)):
        return value
    raise CryptoTenSymbolObservationStoreError(
        f"ten_symbol_observation_value_type_invalid:{type(value).__name__}"
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
        raise CryptoTenSymbolObservationStoreError(
            "ten_symbol_observation_json_not_canonical"
        ) from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _non_authority_fields() -> dict[str, Any]:
    return {
        "authority": "none",
        "execution_eligible": False,
        "capital_write_eligible": False,
        "model_authority": False,
    }


def _market_slot(value: Any, *, aligned: bool = True) -> datetime:
    if not isinstance(value, str) or not value:
        raise CryptoTenSymbolObservationStoreError(
            "ten_symbol_observation_slot_invalid"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CryptoTenSymbolObservationStoreError(
            "ten_symbol_observation_slot_invalid"
        ) from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.utcoffset() != timezone.utc.utcoffset(parsed)
    ):
        raise CryptoTenSymbolObservationStoreError(
            "ten_symbol_observation_slot_invalid"
        )
    normalized = parsed.astimezone(timezone.utc)
    if aligned and (
        normalized.second != 0
        or normalized.microsecond != 0
        or normalized.minute % 5 != 0
    ):
        raise CryptoTenSymbolObservationStoreError(
            "ten_symbol_observation_slot_invalid"
        )
    return normalized


def _slot_token(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
        .replace(":", "-")
    )


def _regular_single_link(path: Path, *, reason: str) -> None:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise CryptoTenSymbolObservationStoreError(reason)


def _ensure_directory(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_directory_invalid"
            )
        return
    path.mkdir(parents=True, exist_ok=False, mode=0o700)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, encoded: bytes) -> None:
    offset = 0
    while offset < len(encoded):
        written = os.write(descriptor, encoded[offset:])
        if written <= 0:
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_events_short_write"
            )
        offset += written


def _write_events_atomic(path: Path, rows: list[Mapping[str, Any]]) -> None:
    encoded = b"".join((_canonical_json(row) + "\n").encode("utf-8") for row in rows)
    if not encoded or len(encoded) > MAX_EVENTS_BYTES:
        raise CryptoTenSymbolObservationStoreError(
            "ten_symbol_observation_events_size_invalid"
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
    _regular_single_link(path, reason="ten_symbol_observation_artifact_file_invalid")
    size = path.stat().st_size
    if size <= 0 or size > MAX_ARTIFACT_BYTES:
        raise CryptoTenSymbolObservationStoreError(
            "ten_symbol_observation_artifact_size_invalid"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CryptoTenSymbolObservationStoreError(
            "ten_symbol_observation_artifact_json_invalid"
        ) from exc
    if not isinstance(value, dict):
        raise CryptoTenSymbolObservationStoreError(
            "ten_symbol_observation_artifact_object_required"
        )
    if _canonical_json(value).encode("utf-8") + b"\n" != path.read_bytes():
        raise CryptoTenSymbolObservationStoreError(
            "ten_symbol_observation_artifact_not_canonical"
        )
    return value


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> dict[str, Any]:
    canonical = _canonical_value(value)
    if not isinstance(canonical, dict):
        raise CryptoTenSymbolObservationStoreError(
            "ten_symbol_observation_artifact_object_required"
        )
    encoded = (_canonical_json(canonical) + "\n").encode("utf-8")
    if not encoded or len(encoded) > MAX_ARTIFACT_BYTES:
        raise CryptoTenSymbolObservationStoreError(
            "ten_symbol_observation_artifact_size_invalid"
        )
    if path.exists() or path.is_symlink():
        _regular_single_link(
            path,
            reason="ten_symbol_observation_artifact_file_invalid",
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
        raise CryptoTenSymbolObservationStoreError(
            "ten_symbol_observation_artifact_persist_failed"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return canonical


def _write_immutable_json(path: Path, value: Mapping[str, Any]) -> dict[str, Any]:
    canonical = _canonical_value(value)
    if not isinstance(canonical, dict):
        raise CryptoTenSymbolObservationStoreError(
            "ten_symbol_observation_artifact_object_required"
        )
    encoded = (_canonical_json(canonical) + "\n").encode("utf-8")
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise CryptoTenSymbolObservationStoreError(
            "ten_symbol_observation_artifact_size_invalid"
        )
    if path.exists():
        existing = _read_json(path)
        if _canonical_json(existing) != _canonical_json(canonical):
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_artifact_content_conflict"
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


class CryptoTenSymbolObservationStore:
    """Checksum-chained append-only ten-symbol observation event store."""

    def __init__(self, output_root: Path | str) -> None:
        root = Path(output_root)
        if root.exists() and root.is_symlink():
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_root_symlink_not_allowed"
            )
        _ensure_directory(root)
        self.root = root
        self.events_path = self.root / "events.jsonl"
        self.head_path = self.root / "head.json"
        self.pending_path = self.root / "pending.json"
        self.lock_path = self.root / ".lock"
        self.cycle_lock_path = self.root / ".cycle.lock"
        self.slot_index_dir = self.root / "slot_index"
        _ensure_directory(self.slot_index_dir)
        self._verified_events_cache: list[dict[str, Any]] | None = None

    @contextmanager
    def _file_lock(self, path: Path, *, nonblocking: bool = False) -> Iterator[None]:
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise CryptoTenSymbolObservationStoreError(
                    "ten_symbol_observation_lock_file_invalid"
                )
            fcntl.flock(
                descriptor,
                fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0),
            )
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
    def cycle(self, *, nonblocking: bool = False) -> Iterator[None]:
        """Serialize one complete pending/recover/append invocation."""

        with self._file_lock(self.cycle_lock_path, nonblocking=nonblocking):
            yield

    # ------------------------------------------------------------------
    # Event chain reading
    # ------------------------------------------------------------------

    def _segment_paths(self) -> list[Path]:
        indexed: list[tuple[int, Path]] = []
        for path in self.root.glob(
            f"{EVENTS_SEGMENT_PREFIX}*{EVENTS_SEGMENT_SUFFIX}"
        ):
            token = path.name[
                len(EVENTS_SEGMENT_PREFIX) : -len(EVENTS_SEGMENT_SUFFIX)
            ]
            if len(token) != 6 or not token.isdigit() or int(token) <= 0:
                raise CryptoTenSymbolObservationStoreError(
                    "ten_symbol_observation_events_segment_name_invalid"
                )
            indexed.append((int(token), path))
        indexed.sort()
        if [index for index, _ in indexed] != list(range(1, len(indexed) + 1)):
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_events_segment_sequence_invalid"
            )
        return [path for _, path in indexed]

    @staticmethod
    def _verify_event_row(row: Mapping[str, Any]) -> None:
        if row.get("contract") != TEN_SYMBOL_EVENT_CONTRACT:
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_event_contract_invalid"
            )
        if row.get("event_type") not in EVENT_TYPES:
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_event_type_invalid"
            )
        for key, expected in _non_authority_fields().items():
            if row.get(key) != expected:
                raise CryptoTenSymbolObservationStoreError(
                    "ten_symbol_observation_event_authority_invalid"
                )

    def _read_events_file(
        self,
        path: Path,
        *,
        start_sequence: int,
        previous_checksum: str,
    ) -> list[dict[str, Any]]:
        _regular_single_link(
            path,
            reason="ten_symbol_observation_events_file_invalid",
        )
        raw = path.read_bytes()
        if not raw or len(raw) > MAX_EVENTS_BYTES:
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_events_too_large"
            )
        if not raw.endswith(b"\n"):
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_events_partial_tail"
            )
        rows: list[dict[str, Any]] = []
        expected_previous = previous_checksum
        for offset, line in enumerate(raw.splitlines()):
            sequence = start_sequence + offset
            try:
                row = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CryptoTenSymbolObservationStoreError(
                    "ten_symbol_observation_events_json_invalid"
                ) from exc
            if not isinstance(row, dict):
                raise CryptoTenSymbolObservationStoreError(
                    "ten_symbol_observation_events_row_invalid"
                )
            material = dict(row)
            checksum = material.pop("checksum", None)
            if (
                material.get("sequence") != sequence
                or material.get("previous_checksum") != expected_previous
                or checksum != _sha256(material)
            ):
                raise CryptoTenSymbolObservationStoreError(
                    "ten_symbol_observation_events_chain_invalid"
                )
            self._verify_event_row(material)
            expected_previous = str(checksum)
            rows.append(row)
        return rows

    def _read_all_events(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
        all_rows: list[dict[str, Any]] = []
        previous_checksum = "0" * 64
        segment_paths = self._segment_paths()
        for path in segment_paths:
            segment_rows = self._read_events_file(
                path,
                start_sequence=len(all_rows) + 1,
                previous_checksum=previous_checksum,
            )
            all_rows.extend(segment_rows)
            previous_checksum = str(all_rows[-1]["checksum"])
        current_rows: list[dict[str, Any]] = []
        if self.events_path.exists():
            current_rows = self._read_events_file(
                self.events_path,
                start_sequence=len(all_rows) + 1,
                previous_checksum=previous_checksum,
            )
            all_rows.extend(current_rows)
        return all_rows, current_rows, len(segment_paths)

    def events(self) -> list[dict[str, Any]]:
        """Return the fully verified event chain in sequence order."""

        with self._locked():
            rows, _, _ = self._read_all_events()
            return rows

    def data_gap_events(self) -> list[dict[str, Any]]:
        return [
            row for row in self.events() if row.get("event_type") == "data_gap"
        ]

    def data_reject_events(self) -> list[dict[str, Any]]:
        return [
            row for row in self.events() if row.get("event_type") == "data_reject"
        ]

    # ------------------------------------------------------------------
    # Head checkpoint
    # ------------------------------------------------------------------

    @staticmethod
    def _head_payload(
        *,
        sequence: int,
        last_checksum: str,
        segment_count: int,
        current_start_sequence: int,
        current_start_previous_checksum: str,
        current_row_count: int,
        current_file_sha256: str | None,
        observation_count: int,
        data_reject_count: int,
        data_gap_count: int,
        latest_terminal_slot: str | None,
        latest_event_id: str | None,
        latest_event_checksum: str | None,
    ) -> dict[str, Any]:
        head: dict[str, Any] = {
            "contract": TEN_SYMBOL_HEAD_CONTRACT,
            "sequence": sequence,
            "last_checksum": last_checksum,
            "segment_count": segment_count,
            "current_start_sequence": current_start_sequence,
            "current_start_previous_checksum": current_start_previous_checksum,
            "current_row_count": current_row_count,
            "current_file_sha256": current_file_sha256,
            "event_count": sequence,
            "observation_count": observation_count,
            "data_reject_count": data_reject_count,
            "data_gap_count": data_gap_count,
            "latest_terminal_slot": latest_terminal_slot,
            "latest_event_id": latest_event_id,
            "latest_event_checksum": latest_event_checksum,
            **_non_authority_fields(),
        }
        head["head_sha256"] = _sha256(head)
        return head

    def _rebuild_head(self) -> dict[str, Any]:
        rows, current_rows, segment_count = self._read_all_events()
        last_checksum = str(rows[-1]["checksum"]) if rows else "0" * 64
        terminal_slots = [
            _market_slot(row.get("window_end"))
            for row in rows
            if row.get("event_type") in TERMINAL_SLOT_TYPES
        ]
        head = self._head_payload(
            sequence=len(rows),
            last_checksum=last_checksum,
            segment_count=segment_count,
            current_start_sequence=(
                int(current_rows[0]["sequence"]) if current_rows else len(rows) + 1
            ),
            current_start_previous_checksum=(
                str(current_rows[0]["previous_checksum"])
                if current_rows
                else last_checksum
            ),
            current_row_count=len(current_rows),
            current_file_sha256=(
                hashlib.sha256(self.events_path.read_bytes()).hexdigest()
                if self.events_path.exists()
                else None
            ),
            observation_count=sum(
                row.get("event_type") == "observation" for row in rows
            ),
            data_reject_count=sum(
                row.get("event_type") == "data_reject" for row in rows
            ),
            data_gap_count=sum(row.get("event_type") == "data_gap" for row in rows),
            latest_terminal_slot=(
                terminal_slots[-1].isoformat().replace("+00:00", "Z")
                if terminal_slots
                else None
            ),
            latest_event_id=(str(rows[-1]["event_id"]) if rows else None),
            latest_event_checksum=last_checksum if rows else None,
        )
        _write_json_atomic(self.head_path, head)
        return head

    def _verify_head_structure(self, head: Mapping[str, Any]) -> dict[str, Any]:
        material = dict(head)
        claimed = material.pop("head_sha256", None)
        if claimed != _sha256(material) or head.get("contract") != (
            TEN_SYMBOL_HEAD_CONTRACT
        ):
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_head_invalid"
            )
        integers = (
            head.get("sequence"),
            head.get("segment_count"),
            head.get("current_start_sequence"),
            head.get("current_row_count"),
            head.get("event_count"),
            head.get("observation_count"),
            head.get("data_reject_count"),
            head.get("data_gap_count"),
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in integers
        ):
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_head_invalid"
            )
        if (
            head.get("event_count") != head.get("sequence")
            or head.get("current_start_sequence")
            != head.get("sequence") - head.get("current_row_count") + 1
        ):
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_head_invalid"
            )
        for key in ("last_checksum", "current_start_previous_checksum"):
            value = head.get(key)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise CryptoTenSymbolObservationStoreError(
                    "ten_symbol_observation_head_invalid"
                )
        for key, expected in _non_authority_fields().items():
            if head.get(key) != expected:
                raise CryptoTenSymbolObservationStoreError(
                    "ten_symbol_observation_head_invalid"
                )
        if head.get("sequence") == 0:
            if (
                head.get("last_checksum") != "0" * 64
                or head.get("latest_terminal_slot") is not None
                or head.get("latest_event_id") is not None
            ):
                raise CryptoTenSymbolObservationStoreError(
                    "ten_symbol_observation_head_invalid"
                )
        return dict(head)

    def _head_state(self) -> dict[str, Any]:
        """Return the verified head, recovering from a crash-stale checkpoint.

        A missing head is rebuilt from the fsynced event chain.  A head that
        lags a *valid* chain whose covered prefix still matches is a crash
        between the event publish and the head write and is rebuilt.  Any
        other divergence between the head and the chain is tamper evidence
        and fails closed.
        """

        if not self.head_path.exists():
            return self._rebuild_head()
        head = self._verify_head_structure(_read_json(self.head_path))
        current_sha = (
            hashlib.sha256(self.events_path.read_bytes()).hexdigest()
            if self.events_path.exists()
            else None
        )
        if current_sha == head.get("current_file_sha256"):
            return head
        rows, _, _ = self._read_all_events()
        covered_checksum = (
            str(rows[int(head["sequence"]) - 1]["checksum"])
            if int(head["sequence"]) > 0 and len(rows) >= int(head["sequence"])
            else None
        )
        if len(rows) > int(head["sequence"]) and covered_checksum == head.get(
            "last_checksum"
        ):
            return self._rebuild_head()
        raise CryptoTenSymbolObservationStoreError(
            "ten_symbol_observation_head_invalid"
        )

    def head(self) -> tuple[int, str]:
        with self._locked():
            state = self._head_state()
            return int(state["sequence"]), str(state["last_checksum"])

    def checkpoint(self) -> dict[str, Any]:
        with self._locked():
            state = self._head_state()
            return {
                "sequence": int(state["sequence"]),
                "last_checksum": state["last_checksum"],
                "event_count": int(state["event_count"]),
                "observation_count": int(state["observation_count"]),
                "data_reject_count": int(state["data_reject_count"]),
                "data_gap_count": int(state["data_gap_count"]),
                "latest_terminal_slot": state["latest_terminal_slot"],
                "latest_event_id": state["latest_event_id"],
            }

    # ------------------------------------------------------------------
    # Slot index
    # ------------------------------------------------------------------

    def _slot_index_path(self, event_type: str, window_end: str) -> Path:
        slot = _market_slot(window_end)
        return self.slot_index_dir / f"{event_type}-{_slot_token(slot)}.json"

    def _verified_indexed_event(self, path: Path) -> dict[str, Any]:
        row = _read_json(path)
        material = dict(row)
        checksum = material.pop("checksum", None)
        if checksum != _sha256(material):
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_slot_index_invalid"
            )
        self._verify_event_row(material)
        if self._verified_events_cache is None:
            self._verified_events_cache, _, _ = self._read_all_events()
        sequence = material.get("sequence")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence <= 0
            or sequence > len(self._verified_events_cache)
        ):
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_slot_index_invalid"
            )
        ledger_row = self._verified_events_cache[sequence - 1]
        if _canonical_json(ledger_row) != _canonical_json(row):
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_slot_index_invalid"
            )
        return row

    def event_for_slot(
        self,
        event_type: str,
        window_end: str,
    ) -> dict[str, Any] | None:
        if event_type not in EVENT_TYPES:
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_event_type_invalid"
            )
        with self._locked():
            path = self._slot_index_path(event_type, window_end)
            if not path.exists():
                return None
            return self._verified_indexed_event(path)

    # ------------------------------------------------------------------
    # Append
    # ------------------------------------------------------------------

    def _validate_event(self, event: Mapping[str, Any]) -> datetime:
        if event.get("contract") != TEN_SYMBOL_EVENT_CONTRACT:
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_event_contract_invalid"
            )
        event_type = event.get("event_type")
        if event_type not in EVENT_TYPES:
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_event_type_invalid"
            )
        event_id = event.get("event_id")
        if (
            not isinstance(event_id, str)
            or not event_id
            or len(event_id) > 160
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789-_"
                for character in event_id
            )
        ):
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_event_id_invalid"
            )
        for key, expected in _non_authority_fields().items():
            if event.get(key) != expected:
                raise CryptoTenSymbolObservationStoreError(
                    "ten_symbol_observation_event_authority_invalid"
                )
        window_end = _market_slot(event.get("window_end"))
        cutoff = _market_slot(event.get("observation_cutoff"), aligned=False)
        if cutoff <= window_end:
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_slot_invalid"
            )
        if event_type == "data_reject":
            reason = event.get("reason_code")
            if not isinstance(reason, str) or not reason:
                raise CryptoTenSymbolObservationStoreError(
                    "ten_symbol_observation_event_reason_invalid"
                )
        if event_type == "data_gap":
            if event.get("gap_contract") != TEN_SYMBOL_DATA_GAP_CONTRACT:
                raise CryptoTenSymbolObservationStoreError(
                    "ten_symbol_observation_gap_slot_invalid"
                )
            try:
                prior = _market_slot(event.get("prior_market_slot"))
                skipped_from = _market_slot(event.get("skipped_from"))
                skipped_to = _market_slot(event.get("skipped_to"))
                recovery = _market_slot(event.get("recovery_market_slot"))
            except CryptoTenSymbolObservationStoreError as exc:
                raise CryptoTenSymbolObservationStoreError(
                    "ten_symbol_observation_gap_slot_invalid"
                ) from exc
            if (
                skipped_from != prior + FIVE_MINUTES
                or skipped_to != recovery - FIVE_MINUTES
                or skipped_from > skipped_to
                or recovery != window_end
                or not isinstance(event.get("recovery_observation"), Mapping)
            ):
                raise CryptoTenSymbolObservationStoreError(
                    "ten_symbol_observation_gap_slot_invalid"
                )
        return window_end

    def _rotate_current_events(self, segment_count: int) -> None:
        if not self.events_path.exists():
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_events_rotation_source_missing"
            )
        target = self.root / (
            f"{EVENTS_SEGMENT_PREFIX}{segment_count + 1:06d}{EVENTS_SEGMENT_SUFFIX}"
        )
        if target.exists() or target.is_symlink():
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_events_segment_conflict"
            )
        os.replace(self.events_path, target)
        _fsync_directory(self.root)

    def append_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        canonical = _canonical_value(event)
        if not isinstance(canonical, dict):
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_event_object_required"
            )
        forbidden = {"sequence", "previous_checksum", "checksum"}
        if forbidden.intersection(canonical):
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_event_chain_fields_forbidden"
            )
        window_end = self._validate_event(canonical)
        event_type = str(canonical["event_type"])
        with self._locked():
            state = self._head_state()
            index_path = self._slot_index_path(
                event_type,
                str(canonical["window_end"]),
            )
            if index_path.exists():
                indexed = self._verified_indexed_event(index_path)
                existing = dict(indexed)
                for key in forbidden:
                    existing.pop(key, None)
                if _canonical_json(existing) != _canonical_json(canonical):
                    raise CryptoTenSymbolObservationStoreError(
                        "ten_symbol_observation_slot_payload_conflict"
                    )
                return indexed
            if event_type in TERMINAL_SLOT_TYPES:
                for other_type in sorted(TERMINAL_SLOT_TYPES - {event_type}):
                    if self._slot_index_path(
                        other_type,
                        str(canonical["window_end"]),
                    ).exists():
                        raise CryptoTenSymbolObservationStoreError(
                            "ten_symbol_observation_slot_payload_conflict"
                        )
                latest_terminal = state.get("latest_terminal_slot")
                if latest_terminal is not None and window_end <= _market_slot(
                    latest_terminal
                ):
                    raise CryptoTenSymbolObservationStoreError(
                        "ten_symbol_observation_slot_not_monotonic"
                    )

            current_rows: list[dict[str, Any]] = []
            if self.events_path.exists():
                current_rows = self._read_events_file(
                    self.events_path,
                    start_sequence=int(state["current_start_sequence"]),
                    previous_checksum=str(state["current_start_previous_checksum"]),
                )
                if len(current_rows) != int(state["current_row_count"]):
                    raise CryptoTenSymbolObservationStoreError(
                        "ten_symbol_observation_events_chain_invalid"
                    )
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
            if len(candidate_bytes) > MAX_EVENTS_BYTES:
                if not current_rows:
                    raise CryptoTenSymbolObservationStoreError(
                        "ten_symbol_observation_event_too_large"
                    )
                segment_count = int(state["segment_count"])
                self._rotate_current_events(segment_count)
                candidate_current = [row]
                segment_count += 1
                current_start_sequence = int(row["sequence"])
                current_start_previous = str(row["previous_checksum"])
            else:
                segment_count = int(state["segment_count"])
                current_start_sequence = int(state["current_start_sequence"])
                current_start_previous = str(state["current_start_previous_checksum"])
            _write_events_atomic(self.events_path, candidate_current)
            _write_immutable_json(index_path, row)
            latest_terminal = state.get("latest_terminal_slot")
            if event_type in TERMINAL_SLOT_TYPES:
                latest_terminal = str(canonical["window_end"])
            next_head = self._head_payload(
                sequence=int(row["sequence"]),
                last_checksum=str(row["checksum"]),
                segment_count=segment_count,
                current_start_sequence=current_start_sequence,
                current_start_previous_checksum=current_start_previous,
                current_row_count=len(candidate_current),
                current_file_sha256=hashlib.sha256(
                    self.events_path.read_bytes()
                ).hexdigest(),
                observation_count=int(state["observation_count"])
                + (1 if event_type == "observation" else 0),
                data_reject_count=int(state["data_reject_count"])
                + (1 if event_type == "data_reject" else 0),
                data_gap_count=int(state["data_gap_count"])
                + (1 if event_type == "data_gap" else 0),
                latest_terminal_slot=latest_terminal,
                latest_event_id=str(row["event_id"]),
                latest_event_checksum=str(row["checksum"]),
            )
            _write_json_atomic(self.head_path, next_head)
            self._verified_events_cache = None
            return row

    # ------------------------------------------------------------------
    # Pending marker (runtime crash bookkeeping, not evidence)
    # ------------------------------------------------------------------

    @staticmethod
    def _pending_payload(record: Mapping[str, Any]) -> dict[str, Any]:
        canonical = _canonical_value(record)
        if not isinstance(canonical, dict):
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_pending_invalid"
            )
        window_end = _market_slot(canonical.get("window_end"))
        cutoff = _market_slot(canonical.get("observation_cutoff"), aligned=False)
        profile_sha = canonical.get("profile_sha256")
        catalog_version = canonical.get("catalog_version")
        if (
            cutoff <= window_end
            or not isinstance(profile_sha, str)
            or len(profile_sha) != 64
            or any(
                character not in "0123456789abcdef" for character in profile_sha
            )
            or not isinstance(catalog_version, str)
            or not catalog_version
        ):
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_pending_invalid"
            )
        payload: dict[str, Any] = {
            "contract": TEN_SYMBOL_PENDING_CONTRACT,
            "window_end": canonical["window_end"],
            "observation_cutoff": canonical["observation_cutoff"],
            "profile_sha256": profile_sha,
            "catalog_version": catalog_version,
            **_non_authority_fields(),
        }
        payload["pending_sha256"] = _sha256(payload)
        return payload

    def _read_pending(self) -> dict[str, Any] | None:
        if not self.pending_path.exists():
            return None
        raw = _read_json(self.pending_path)
        material = dict(raw)
        claimed = material.pop("pending_sha256", None)
        expected = self._pending_payload(
            {key: raw.get(key) for key in (
                "window_end",
                "observation_cutoff",
                "profile_sha256",
                "catalog_version",
            )}
        )
        if (
            claimed != expected["pending_sha256"]
            or raw.get("contract") != TEN_SYMBOL_PENDING_CONTRACT
        ):
            raise CryptoTenSymbolObservationStoreError(
                "ten_symbol_observation_pending_invalid"
            )
        for key, expected_value in _non_authority_fields().items():
            if raw.get(key) != expected_value:
                raise CryptoTenSymbolObservationStoreError(
                    "ten_symbol_observation_pending_invalid"
                )
        return expected

    def set_pending(self, record: Mapping[str, Any]) -> dict[str, Any]:
        payload = self._pending_payload(record)
        with self._locked():
            existing = self._read_pending()
            if existing is not None:
                if _canonical_json(existing) == _canonical_json(payload):
                    return existing
                raise CryptoTenSymbolObservationStoreError(
                    "ten_symbol_observation_pending_conflict"
                )
            return _write_json_atomic(self.pending_path, payload)

    def pending_record(self) -> dict[str, Any] | None:
        with self._locked():
            return self._read_pending()

    def clear_pending(self, window_end: str) -> None:
        slot = _market_slot(window_end)
        with self._locked():
            existing = self._read_pending()
            if existing is None:
                return
            if _market_slot(existing["window_end"]) != slot:
                raise CryptoTenSymbolObservationStoreError(
                    "ten_symbol_observation_pending_conflict"
                )
            self.pending_path.unlink()
            _fsync_directory(self.root)


__all__ = [
    "EVENT_TYPES",
    "MAX_EVENTS_BYTES",
    "TEN_SYMBOL_DATA_GAP_CONTRACT",
    "TEN_SYMBOL_EVENT_CONTRACT",
    "TEN_SYMBOL_HEAD_CONTRACT",
    "TEN_SYMBOL_PENDING_CONTRACT",
    "TERMINAL_SLOT_TYPES",
    "CryptoTenSymbolObservationStore",
    "CryptoTenSymbolObservationStoreError",
]
