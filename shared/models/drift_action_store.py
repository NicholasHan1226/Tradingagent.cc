"""Durable fail-closed latch for risk-reducing drift actions."""

from __future__ import annotations

import hashlib
import json
import os
import fcntl
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Tuple

from .drift_policy import DriftDecision, SafeAutomaticAction


_RISK_ACTION_SEVERITY = {
    SafeAutomaticAction.REDUCE_ONLY: 1,
    SafeAutomaticAction.STOP_NEW_RISK: 2,
    SafeAutomaticAction.QUARANTINE: 3,
}


class DriftActionStoreError(RuntimeError):
    """Raised when a drift-action receipt cannot be trusted or persisted."""


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DriftActionStoreError("recorded_at_must_be_timezone_aware")


def _action_severity_profile(
    actions: Tuple[SafeAutomaticAction, ...],
) -> Tuple[int, int]:
    action_set = frozenset(actions)
    known_actions = frozenset(_RISK_ACTION_SEVERITY) | {
        SafeAutomaticAction.REQUIRE_REVIEW
    }
    if not action_set.issubset(known_actions):
        raise DriftActionStoreError("drift_action_severity_undefined")
    return (
        max(
            (
                severity
                for action, severity in _RISK_ACTION_SEVERITY.items()
                if action in action_set
            ),
            default=0,
        ),
        int(SafeAutomaticAction.REQUIRE_REVIEW in action_set),
    )


def _is_stricter_latch(
    candidate: "DriftActionReceipt",
    active: "DriftActionReceipt",
) -> bool:
    candidate_profile = _action_severity_profile(candidate.actions)
    active_profile = _action_severity_profile(active.actions)
    action_dimensions_do_not_loosen = all(
        candidate_level >= active_level
        for candidate_level, active_level in zip(candidate_profile, active_profile)
    )
    risk_dimension_does_not_loosen = candidate.risk_multiplier <= active.risk_multiplier
    at_least_one_dimension_tightens = (
        candidate.risk_multiplier < active.risk_multiplier
        or candidate_profile != active_profile
    )
    return (
        risk_dimension_does_not_loosen
        and action_dimensions_do_not_loosen
        and at_least_one_dimension_tightens
    )


def _reject_symlink_components(path: Path) -> None:
    candidate = path.absolute()
    for component in (candidate, *candidate.parents):
        if component.exists() or component.is_symlink():
            if component.is_symlink():
                raise DriftActionStoreError("store_root_symlink_forbidden")


def _nofollow_flag() -> int:
    return int(getattr(os, "O_NOFOLLOW", 0))


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)) | _nofollow_flag()
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise DriftActionStoreError("directory_fsync_failed") from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        raise DriftActionStoreError("directory_fsync_failed") from exc
    finally:
        os.close(fd)


def _assert_regular_fd_matches_path(path: Path, fd: int, *, role: str) -> None:
    try:
        fd_stat = os.fstat(fd)
        path_stat = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise DriftActionStoreError(f"{role}_identity_invalid") from exc
    if (
        not stat.S_ISREG(fd_stat.st_mode)
        or not stat.S_ISREG(path_stat.st_mode)
        or (fd_stat.st_dev, fd_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino)
    ):
        raise DriftActionStoreError(f"{role}_identity_invalid")


def _read_bytes_no_follow(path: Path, *, role: str) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | _nofollow_flag())
    except OSError as exc:
        raise DriftActionStoreError(f"{role}_unreadable") from exc
    try:
        _assert_regular_fd_matches_path(path, fd, role=role)
        chunks = []
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        _assert_regular_fd_matches_path(path, fd, role=role)
        return b"".join(chunks)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class DriftActionReceipt:
    schema_version: str
    evidence_sha256: str
    actions: Tuple[SafeAutomaticAction, ...]
    risk_multiplier: float
    reasons: Tuple[str, ...]
    recorded_at: datetime
    receipt_sha256: str

    def canonical_payload(self) -> dict:
        return {
            "actions": [action.value for action in self.actions],
            "evidence_sha256": self.evidence_sha256,
            "reasons": list(self.reasons),
            "recorded_at": self.recorded_at.isoformat(),
            "risk_multiplier": self.risk_multiplier,
            "schema_version": self.schema_version,
        }


class DriftActionStore:
    """Persist negative drift actions and never loosen the active latch.

    Clearing or relaxing the active action is intentionally absent. It requires a
    separate, manual review workflow rather than another model assertion.
    """

    SCHEMA_VERSION = "tradingagent.drift_action_receipt.v1"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        _reject_symlink_components(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise DriftActionStoreError("store_root_symlink_forbidden")
        self.receipts_dir = self.root / "receipts"
        self.receipts_dir.mkdir(mode=0o700, exist_ok=True)
        if self.receipts_dir.is_symlink() or not self.receipts_dir.is_dir():
            raise DriftActionStoreError("receipts_directory_invalid")
        self.active_path = self.root / "active.json"
        self.lock_path = self.root / ".active.lock"

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        if self.lock_path.is_symlink():
            raise DriftActionStoreError("active_lock_symlink_forbidden")
        flags = os.O_RDWR | os.O_CREAT | _nofollow_flag()
        try:
            fd = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise DriftActionStoreError("active_lock_open_failed") from exc
        try:
            _assert_regular_fd_matches_path(self.lock_path, fd, role="active_lock")
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            _assert_regular_fd_matches_path(self.lock_path, fd, role="active_lock")
            yield
            _assert_regular_fd_matches_path(self.lock_path, fd, role="active_lock")
        except OSError as exc:
            raise DriftActionStoreError("active_lock_failed") from exc
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def record(
        self,
        decision: DriftDecision,
        *,
        recorded_at: datetime,
    ) -> DriftActionReceipt:
        if not isinstance(decision, DriftDecision):
            raise DriftActionStoreError("drift_decision_required")
        _require_aware(recorded_at)
        if not decision.actions or decision.risk_multiplier >= 1.0:
            raise DriftActionStoreError("negative_action_required")

        payload = {
            "actions": [action.value for action in decision.actions],
            "evidence_sha256": decision.evidence_sha256,
            "reasons": list(decision.reasons),
            "recorded_at": recorded_at.isoformat(),
            "risk_multiplier": decision.risk_multiplier,
            "schema_version": self.SCHEMA_VERSION,
        }
        receipt_sha256 = _canonical_sha256(payload)
        receipt = self._receipt_from_payload(
            {**payload, "receipt_sha256": receipt_sha256}
        )
        receipt_path = self.receipts_dir / f"{receipt_sha256}.json"
        with self._locked(exclusive=True):
            self._write_once(
                receipt_path, {**payload, "receipt_sha256": receipt_sha256}
            )
            active = self._load_active_unlocked(required=False)
            if active is None or _is_stricter_latch(receipt, active):
                self._replace_active(receipt)
        return receipt

    def load_active(self, *, required: bool = True) -> Optional[DriftActionReceipt]:
        with self._locked(exclusive=False):
            return self._load_active_unlocked(required=required)

    def _load_active_unlocked(self, *, required: bool) -> Optional[DriftActionReceipt]:
        if self.active_path.is_symlink():
            raise DriftActionStoreError("active_receipt_symlink_forbidden")
        if not self.active_path.exists():
            if required:
                raise DriftActionStoreError("active_receipt_missing")
            return None
        try:
            payload = json.loads(
                _read_bytes_no_follow(self.active_path, role="active_receipt").decode(
                    "utf-8"
                )
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DriftActionStoreError("active_receipt_unreadable") from exc
        return self._receipt_from_payload(payload)

    def _receipt_from_payload(self, payload: Any) -> DriftActionReceipt:
        if not isinstance(payload, dict):
            raise DriftActionStoreError("receipt_payload_invalid")
        expected = {
            "actions",
            "evidence_sha256",
            "reasons",
            "recorded_at",
            "receipt_sha256",
            "risk_multiplier",
            "schema_version",
        }
        if set(payload) != expected:
            raise DriftActionStoreError("receipt_fields_invalid")
        digest = payload["receipt_sha256"]
        unsigned = {key: payload[key] for key in expected if key != "receipt_sha256"}
        if not isinstance(digest, str) or digest != _canonical_sha256(unsigned):
            raise DriftActionStoreError("receipt_digest_mismatch")
        try:
            recorded_at = datetime.fromisoformat(payload["recorded_at"])
            actions = tuple(SafeAutomaticAction(value) for value in payload["actions"])
        except (TypeError, ValueError) as exc:
            raise DriftActionStoreError("receipt_payload_invalid") from exc
        _require_aware(recorded_at)
        risk_multiplier = payload["risk_multiplier"]
        if (
            isinstance(risk_multiplier, bool)
            or not isinstance(risk_multiplier, (int, float))
            or not 0 <= float(risk_multiplier) < 1
            or not actions
        ):
            raise DriftActionStoreError("receipt_payload_invalid")
        reasons = payload["reasons"]
        if (
            not isinstance(reasons, list)
            or not reasons
            or any(not isinstance(item, str) or not item for item in reasons)
        ):
            raise DriftActionStoreError("receipt_payload_invalid")
        if payload["schema_version"] != self.SCHEMA_VERSION:
            raise DriftActionStoreError("receipt_schema_version_invalid")
        return DriftActionReceipt(
            schema_version=payload["schema_version"],
            evidence_sha256=payload["evidence_sha256"],
            actions=actions,
            risk_multiplier=float(risk_multiplier),
            reasons=tuple(reasons),
            recorded_at=recorded_at,
            receipt_sha256=digest,
        )

    def _write_once(self, path: Path, payload: Mapping[str, Any]) -> None:
        encoded = (
            json.dumps(
                payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        ).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            existing = _read_bytes_no_follow(path, role="receipt")
            if existing != encoded:
                raise DriftActionStoreError("receipt_content_conflict")
            return
        except OSError as exc:
            raise DriftActionStoreError("receipt_write_failed") from exc
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise DriftActionStoreError("receipt_write_failed") from exc
        _fsync_directory(path.parent)

    def _replace_active(self, receipt: DriftActionReceipt) -> None:
        if self.active_path.is_symlink():
            raise DriftActionStoreError("active_receipt_symlink_forbidden")
        payload = {
            **receipt.canonical_payload(),
            "receipt_sha256": receipt.receipt_sha256,
        }
        encoded = (
            json.dumps(
                payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        ).encode("utf-8")
        temp_path = self.root / f".active-{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(temp_path, flags, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.active_path)
            _fsync_directory(self.root)
        except OSError as exc:
            temp_path.unlink(missing_ok=True)
            raise DriftActionStoreError("active_receipt_write_failed") from exc
