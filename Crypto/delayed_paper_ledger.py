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
LOCAL_AUDIT_DURABILITY = "local_audit_fsync_only"
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_LEDGER_BYTES = 16 * 1024 * 1024
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
            market_slot = canonical.get("market_slot")
            parsed_market_slot = _market_slot(market_slot)
            for existing_path in sorted(self.observations_dir.glob("*.json")):
                existing = _read_json(existing_path)
                existing_id = self._verify_observation(existing)
                existing_market_slot = existing.get("market_slot")
                parsed_existing_slot = _market_slot(existing_market_slot)
                completion_path = self._completion_path(existing_id)
                if completion_path.exists():
                    self._verify_completion(
                        _read_json(completion_path),
                        observation=existing,
                    )
                elif existing_id != observation_id:
                    raise CryptoDelayedPaperLedgerError(
                        "delayed_paper_prior_observation_pending"
                    )
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
            return _write_immutable_json(
                self._observation_path(observation_id),
                canonical,
            )

    def pending_observation(self) -> dict[str, Any] | None:
        with self._locked():
            pending: list[dict[str, Any]] = []
            for path in sorted(self.observations_dir.glob("*.json")):
                observation = _read_json(path)
                observation_id = self._verify_observation(observation)
                if path.name != f"{observation_id}.json":
                    raise CryptoDelayedPaperLedgerError(
                        "delayed_paper_observation_filename_mismatch"
                    )
                completion_path = self._completion_path(observation_id)
                if completion_path.exists():
                    self._verify_completion(
                        _read_json(completion_path),
                        observation=observation,
                    )
                else:
                    pending.append(observation)
            if len(pending) > 1:
                raise CryptoDelayedPaperLedgerError(
                    "delayed_paper_multiple_pending_observations"
                )
            return pending[0] if pending else None

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
            stored_observation = _read_json(self._observation_path(observation_id))
            self._verify_observation(stored_observation)
            if _canonical_json(stored_observation) != _canonical_json(
                canonical_observation
            ):
                raise CryptoDelayedPaperLedgerError(
                    "delayed_paper_completion_observation_conflict"
                )
            stored = _write_immutable_json(
                self._completion_path(observation_id),
                completion,
            )
            self._verify_completion(stored, observation=stored_observation)
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
            rows, current_rows, segment_count = self._read_ledger_state()
            matches = [row for row in rows if row.get("event_id") == event_id]
            if matches:
                if len(matches) != 1:
                    raise CryptoDelayedPaperLedgerError(
                        "delayed_paper_decision_event_duplicated"
                    )
                existing = dict(matches[0])
                for key in forbidden:
                    existing.pop(key, None)
                if _canonical_json(existing) != _canonical_json(canonical):
                    raise CryptoDelayedPaperLedgerError(
                        "delayed_paper_decision_event_content_conflict"
                    )
                return matches[0]

            row = {
                **canonical,
                "sequence": len(rows) + 1,
                "previous_checksum": (rows[-1]["checksum"] if rows else "0" * 64),
            }
            row["checksum"] = _sha256(row)
            candidate_current = [*current_rows, row]
            candidate_bytes = b"".join(
                (_canonical_json(item) + "\n").encode("utf-8")
                for item in candidate_current
            )
            if len(candidate_bytes) > MAX_LEDGER_BYTES:
                if not current_rows:
                    raise CryptoDelayedPaperLedgerError(
                        "delayed_paper_decision_event_too_large"
                    )
                self._rotate_current_ledger(segment_count)
                candidate_current = [row]
            _write_decision_ledger_atomic(
                self.ledger_path,
                candidate_current,
            )
            return row


__all__ = [
    "COMPLETION_CONTRACT",
    "DECISION_LEDGER_CONTRACT",
    "LOCAL_AUDIT_DURABILITY",
    "OBSERVATION_CONTRACT",
    "CryptoDelayedPaperLedgerError",
    "CryptoDelayedPaperObservationStore",
]
