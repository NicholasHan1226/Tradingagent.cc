"""Durable, simulation-only manual Champion selection registry."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Tuple

from .lifecycle import (
    LifecycleActor,
    LifecycleRecord,
    ModelLifecycleState,
    ValidationPlan,
)
from .release_manifest import ModelReleaseManifest


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_FILENAME_RE = re.compile(
    r"^(?P<sequence>[0-9]{20})-(?P<sha256>[0-9a-f]{64})\.json$"
)
_RECEIPT_FIELDS = frozenset(
    {
        "account_type",
        "action",
        "automatic_promotion_enabled",
        "automatic_risk_expansion_enabled",
        "capital_layer",
        "expected_current_manifest_sha256",
        "human_approval_reference",
        "lifecycle_record_sha256",
        "live_transition_authorized",
        "previous_receipt_sha256",
        "real_trading_enabled",
        "receipt_sha256",
        "recorded_at",
        "schema_version",
        "selected_artifact_sha256",
        "selected_manifest_id",
        "selected_manifest_sha256",
        "selected_model_id",
        "selected_model_version",
        "selection_id",
        "sequence",
        "simulation_only",
        "validation_evidence_sha256",
        "validation_plan_sha256",
    }
)
_POINTER_FIELDS = frozenset(
    {
        "receipt_sha256",
        "schema_version",
        "selected_manifest_sha256",
        "selection_id",
        "sequence",
    }
)


class ChampionRegistryError(RuntimeError):
    """Raised when a Champion selection cannot be trusted or persisted."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_nonempty_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChampionRegistryError(f"{field_name}_invalid")
    return value


def _require_sha256(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ChampionRegistryError(f"{field_name}_invalid")
    return value


def _require_optional_sha256(value: object, *, field_name: str) -> Optional[str]:
    if value is None:
        return None
    return _require_sha256(value, field_name=field_name)


def _nofollow_flag() -> int:
    return int(getattr(os, "O_NOFOLLOW", 0))


def _assert_no_symlink_components(path: Path) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ChampionRegistryError("registry_path_unreadable") from exc
        if stat.S_ISLNK(mode):
            raise ChampionRegistryError("registry_path_symlink_forbidden")
        if current != absolute and not stat.S_ISDIR(mode):
            raise ChampionRegistryError("registry_parent_not_directory")


def _assert_directory(path: Path, *, role: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ChampionRegistryError(f"{role}_unavailable") from exc
    if stat.S_ISLNK(mode):
        raise ChampionRegistryError(f"{role}_symlink_forbidden")
    if not stat.S_ISDIR(mode):
        raise ChampionRegistryError(f"{role}_not_directory")


def _assert_regular_fd_matches_path(path: Path, fd: int, *, role: str) -> None:
    try:
        fd_stat = os.fstat(fd)
        path_stat = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ChampionRegistryError(f"{role}_identity_invalid") from exc
    if stat.S_ISLNK(path_stat.st_mode):
        raise ChampionRegistryError(f"{role}_symlink_forbidden")
    if (
        not stat.S_ISREG(fd_stat.st_mode)
        or not stat.S_ISREG(path_stat.st_mode)
        or (fd_stat.st_dev, fd_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino)
    ):
        raise ChampionRegistryError(f"{role}_identity_invalid")


def _read_bytes_no_follow(path: Path, *, role: str) -> bytes:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ChampionRegistryError(f"{role}_unreadable") from exc
    if stat.S_ISLNK(mode):
        raise ChampionRegistryError(f"{role}_symlink_forbidden")
    if not stat.S_ISREG(mode):
        raise ChampionRegistryError(f"{role}_not_regular")
    try:
        fd = os.open(os.fspath(path), os.O_RDONLY | _nofollow_flag())
    except OSError as exc:
        raise ChampionRegistryError(f"{role}_unreadable") from exc
    try:
        _assert_regular_fd_matches_path(path, fd, role=role)
        chunks = []
        while True:
            try:
                block = os.read(fd, 1024 * 1024)
            except OSError as exc:
                raise ChampionRegistryError(f"{role}_unreadable") from exc
            if not block:
                break
            chunks.append(block)
        _assert_regular_fd_matches_path(path, fd, role=role)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _fsync_directory(path: Path, *, role: str) -> None:
    flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)) | _nofollow_flag()
    try:
        fd = os.open(os.fspath(path), flags)
    except OSError as exc:
        raise ChampionRegistryError(f"{role}_fsync_failed") from exc
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise ChampionRegistryError(f"{role}_fsync_failed")
        os.fsync(fd)
    except OSError as exc:
        raise ChampionRegistryError(f"{role}_fsync_failed") from exc
    finally:
        os.close(fd)


def _write_all(fd: int, encoded: bytes, *, role: str) -> None:
    offset = 0
    while offset < len(encoded):
        try:
            written = os.write(fd, encoded[offset:])
        except OSError as exc:
            raise ChampionRegistryError(f"{role}_write_failed") from exc
        if written <= 0:
            raise ChampionRegistryError(f"{role}_short_write")
        offset += written


def _lifecycle_record_sha256(record: LifecycleRecord) -> str:
    payload = {
        "account_type": record.account_type,
        "approval_reference": record.approval_reference,
        "automatic_promotion_enabled": record.automatic_promotion_enabled,
        "automatic_risk_expansion_enabled": record.automatic_risk_expansion_enabled,
        "capital_layer": record.capital_layer,
        "catalog_version": record.catalog_version,
        "live_transition_authorized": record.live_transition_authorized,
        "manifest_sha256": record.manifest_sha256,
        "model_id": record.model_id,
        "model_version": record.model_version,
        "real_trading_enabled": record.real_trading_enabled,
        "recorded_at": record.recorded_at.isoformat(),
        "research_snapshot_sha256": record.research_snapshot_sha256,
        "state": record.state.value,
        "transition_reason": record.transition_reason,
        "validation_evidence_sha256": record.validation_evidence_sha256,
        "validation_plan_sha256": record.validation_plan_sha256,
    }
    return _canonical_sha256(payload)


@dataclass(frozen=True)
class ChampionSelectionReceipt:
    """One immutable, content-addressed manual selection decision."""

    schema_version: str
    selection_id: str
    sequence: int
    action: str
    selected_manifest_id: str
    selected_manifest_sha256: str
    selected_model_id: str
    selected_model_version: str
    selected_artifact_sha256: str
    validation_plan_sha256: str
    validation_evidence_sha256: str
    lifecycle_record_sha256: str
    human_approval_reference: str
    recorded_at: datetime
    expected_current_manifest_sha256: Optional[str]
    previous_receipt_sha256: Optional[str]
    receipt_sha256: str
    capital_layer: str = "simulated"
    account_type: str = "simulated"
    simulation_only: bool = True
    real_trading_enabled: bool = False
    live_transition_authorized: bool = False
    automatic_promotion_enabled: bool = False
    automatic_risk_expansion_enabled: bool = False

    def canonical_payload(self) -> dict:
        return {
            "account_type": self.account_type,
            "action": self.action,
            "automatic_promotion_enabled": self.automatic_promotion_enabled,
            "automatic_risk_expansion_enabled": self.automatic_risk_expansion_enabled,
            "capital_layer": self.capital_layer,
            "expected_current_manifest_sha256": (self.expected_current_manifest_sha256),
            "human_approval_reference": self.human_approval_reference,
            "lifecycle_record_sha256": self.lifecycle_record_sha256,
            "live_transition_authorized": self.live_transition_authorized,
            "previous_receipt_sha256": self.previous_receipt_sha256,
            "real_trading_enabled": self.real_trading_enabled,
            "recorded_at": self.recorded_at.isoformat(),
            "schema_version": self.schema_version,
            "selected_artifact_sha256": self.selected_artifact_sha256,
            "selected_manifest_id": self.selected_manifest_id,
            "selected_manifest_sha256": self.selected_manifest_sha256,
            "selected_model_id": self.selected_model_id,
            "selected_model_version": self.selected_model_version,
            "selection_id": self.selection_id,
            "sequence": self.sequence,
            "simulation_only": self.simulation_only,
            "validation_evidence_sha256": self.validation_evidence_sha256,
            "validation_plan_sha256": self.validation_plan_sha256,
        }


class ChampionSelectionRegistry:
    """Persist the single current Champion as an append-only receipt chain."""

    SCHEMA_VERSION = "tradingagent.champion_selection_receipt.v1"
    POINTER_SCHEMA_VERSION = "tradingagent.champion_current_pointer.v1"

    def __init__(self, root: Path) -> None:
        if not isinstance(root, (str, os.PathLike)) or not os.fspath(root):
            raise ChampionRegistryError("registry_root_must_be_explicit")
        self.root = Path(os.path.abspath(os.fspath(root)))
        _assert_no_symlink_components(self.root)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ChampionRegistryError("registry_root_unavailable") from exc
        _assert_no_symlink_components(self.root)
        _assert_directory(self.root, role="registry_root")
        self.receipts_dir = self.root / "receipts"
        self.current_path = self.root / "current.json"
        self.lock_path = self.root / ".registry.lock"
        try:
            self.receipts_dir.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise ChampionRegistryError("receipts_directory_unavailable") from exc
        _assert_no_symlink_components(self.receipts_dir)
        _assert_directory(self.receipts_dir, role="receipts_directory")

    def _assert_storage_directories(self) -> None:
        _assert_no_symlink_components(self.root)
        _assert_directory(self.root, role="registry_root")
        _assert_no_symlink_components(self.receipts_dir)
        _assert_directory(self.receipts_dir, role="receipts_directory")

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        self._assert_storage_directories()
        try:
            if stat.S_ISLNK(self.lock_path.lstat().st_mode):
                raise ChampionRegistryError("registry_lock_symlink_forbidden")
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ChampionRegistryError("registry_lock_unreadable") from exc
        flags = os.O_RDWR | os.O_CREAT | _nofollow_flag()
        try:
            fd = os.open(os.fspath(self.lock_path), flags, 0o600)
        except OSError as exc:
            raise ChampionRegistryError("registry_lock_open_failed") from exc
        try:
            _assert_regular_fd_matches_path(self.lock_path, fd, role="registry_lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            except OSError as exc:
                raise ChampionRegistryError("registry_lock_failed") from exc
            _assert_regular_fd_matches_path(self.lock_path, fd, role="registry_lock")
            self._assert_storage_directories()
            yield
            self._assert_storage_directories()
            _assert_regular_fd_matches_path(self.lock_path, fd, role="registry_lock")
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _write_receipt_once(self, path: Path, encoded: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _nofollow_flag()
        created = False
        try:
            fd = os.open(os.fspath(path), flags, 0o600)
            created = True
        except FileExistsError:
            if _read_bytes_no_follow(path, role="receipt") != encoded:
                raise ChampionRegistryError("receipt_content_conflict")
            return
        except OSError as exc:
            raise ChampionRegistryError("receipt_open_failed") from exc
        try:
            _assert_regular_fd_matches_path(path, fd, role="receipt")
            _write_all(fd, encoded, role="receipt")
            os.fchmod(fd, 0o400)
            os.fsync(fd)
            _assert_regular_fd_matches_path(path, fd, role="receipt")
        except Exception:
            os.close(fd)
            if created:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise
        else:
            os.close(fd)
        _fsync_directory(self.receipts_dir, role="receipts_directory")

    def _replace_current_pointer(self, payload: Mapping[str, Any]) -> None:
        try:
            mode = self.current_path.lstat().st_mode
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ChampionRegistryError("current_pointer_unreadable") from exc
        else:
            if stat.S_ISLNK(mode):
                raise ChampionRegistryError("current_pointer_symlink_forbidden")
            if not stat.S_ISREG(mode):
                raise ChampionRegistryError("current_pointer_not_regular")

        encoded = (_canonical_json(payload) + "\n").encode("utf-8")
        temporary = self.root / f".current-{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _nofollow_flag()
        try:
            fd = os.open(os.fspath(temporary), flags, 0o600)
        except OSError as exc:
            raise ChampionRegistryError("current_pointer_temp_open_failed") from exc
        published = False
        try:
            _assert_regular_fd_matches_path(temporary, fd, role="current_pointer_temp")
            _write_all(fd, encoded, role="current_pointer")
            os.fsync(fd)
            _assert_regular_fd_matches_path(temporary, fd, role="current_pointer_temp")
            os.close(fd)
            fd = -1
            os.replace(os.fspath(temporary), os.fspath(self.current_path))
            published = True
            _fsync_directory(self.root, role="registry_root")
        except OSError as exc:
            raise ChampionRegistryError("current_pointer_publish_failed") from exc
        finally:
            if fd >= 0:
                os.close(fd)
            if not published:
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def record_selection(
        self,
        *,
        selection_id: str,
        action: str,
        manifest: ModelReleaseManifest,
        validation_plan: ValidationPlan,
        lifecycle: LifecycleRecord,
        actor: LifecycleActor,
        human_approval_reference: str,
        recorded_at: datetime,
        expected_current_manifest_sha256: Optional[str],
    ) -> ChampionSelectionReceipt:
        if not isinstance(action, str) or action not in {"activate", "rollback"}:
            raise ChampionRegistryError("champion_selection_action_invalid")
        _require_nonempty_text(selection_id, field_name="selection_id")
        _require_nonempty_text(
            human_approval_reference,
            field_name="human_approval_reference",
        )
        _require_optional_sha256(
            expected_current_manifest_sha256,
            field_name="expected_current_manifest_sha256",
        )
        if actor is not LifecycleActor.HUMAN_REVIEWER:
            raise ChampionRegistryError("champion_selection_requires_human_reviewer")
        if not isinstance(manifest, ModelReleaseManifest):
            raise ChampionRegistryError("champion_selection_binding_mismatch")
        if not isinstance(validation_plan, ValidationPlan):
            raise ChampionRegistryError("champion_selection_binding_mismatch")
        if not isinstance(lifecycle, LifecycleRecord):
            raise ChampionRegistryError("champion_selection_binding_mismatch")
        if lifecycle.state is not ModelLifecycleState.CURRENT:
            raise ChampionRegistryError("selected_lifecycle_must_be_current")
        expected_binding = (
            manifest.sha256(),
            manifest.model_id,
            manifest.model_version,
            manifest.research_snapshot_sha256,
            manifest.catalog_version,
            manifest.validation_plan_sha256,
            manifest.validation_evidence_sha256,
            human_approval_reference,
        )
        actual_binding = (
            lifecycle.manifest_sha256,
            lifecycle.model_id,
            lifecycle.model_version,
            lifecycle.research_snapshot_sha256,
            lifecycle.catalog_version,
            lifecycle.validation_plan_sha256,
            lifecycle.validation_evidence_sha256,
            lifecycle.approval_reference,
        )
        if actual_binding != expected_binding:
            raise ChampionRegistryError("champion_selection_binding_mismatch")
        if validation_plan.sha256() != manifest.validation_plan_sha256:
            raise ChampionRegistryError("champion_selection_binding_mismatch")
        if (
            not isinstance(recorded_at, datetime)
            or recorded_at.tzinfo is None
            or recorded_at.utcoffset() is None
        ):
            raise ChampionRegistryError("selection_time_must_be_timezone_aware")
        if not (
            validation_plan.frozen_at
            <= manifest.created_at
            <= lifecycle.recorded_at
            <= recorded_at
        ):
            raise ChampionRegistryError("champion_selection_time_order_invalid")
        with self._locked(exclusive=True):
            return self._record_selection_unlocked(
                selection_id=selection_id,
                action=action,
                manifest=manifest,
                validation_plan=validation_plan,
                lifecycle=lifecycle,
                human_approval_reference=human_approval_reference,
                recorded_at=recorded_at,
                expected_current_manifest_sha256=(expected_current_manifest_sha256),
            )

    def _record_selection_unlocked(
        self,
        *,
        selection_id: str,
        action: str,
        manifest: ModelReleaseManifest,
        validation_plan: ValidationPlan,
        lifecycle: LifecycleRecord,
        human_approval_reference: str,
        recorded_at: datetime,
        expected_current_manifest_sha256: Optional[str],
    ) -> ChampionSelectionReceipt:
        history = self._load_history_unlocked()
        matching_selection = next(
            (receipt for receipt in history if receipt.selection_id == selection_id),
            None,
        )
        if matching_selection is not None:
            requested_identity = (
                action,
                manifest.manifest_id,
                manifest.sha256(),
                manifest.model_id,
                manifest.model_version,
                manifest.artifact_sha256,
                validation_plan.sha256(),
                manifest.validation_evidence_sha256,
                _lifecycle_record_sha256(lifecycle),
                human_approval_reference,
                recorded_at,
                expected_current_manifest_sha256,
            )
            recorded_identity = (
                matching_selection.action,
                matching_selection.selected_manifest_id,
                matching_selection.selected_manifest_sha256,
                matching_selection.selected_model_id,
                matching_selection.selected_model_version,
                matching_selection.selected_artifact_sha256,
                matching_selection.validation_plan_sha256,
                matching_selection.validation_evidence_sha256,
                matching_selection.lifecycle_record_sha256,
                matching_selection.human_approval_reference,
                matching_selection.recorded_at,
                matching_selection.expected_current_manifest_sha256,
            )
            if requested_identity == recorded_identity:
                return matching_selection
            raise ChampionRegistryError("selection_id_conflict")
        previous = history[-1] if history else None
        if previous is not None and recorded_at < previous.recorded_at:
            raise ChampionRegistryError("selection_time_nonmonotonic")
        current_manifest_sha256 = (
            previous.selected_manifest_sha256 if previous is not None else None
        )
        if expected_current_manifest_sha256 != current_manifest_sha256:
            raise ChampionRegistryError("current_manifest_compare_and_swap_failed")
        if action == "rollback" and manifest.sha256() not in {
            receipt.selected_manifest_sha256
            for receipt in history
            if receipt.action == "activate"
        }:
            raise ChampionRegistryError("rollback_target_was_not_previously_activated")
        unsigned = {
            "account_type": "simulated",
            "action": action,
            "automatic_promotion_enabled": False,
            "automatic_risk_expansion_enabled": False,
            "capital_layer": "simulated",
            "expected_current_manifest_sha256": expected_current_manifest_sha256,
            "human_approval_reference": human_approval_reference,
            "lifecycle_record_sha256": _lifecycle_record_sha256(lifecycle),
            "live_transition_authorized": False,
            "previous_receipt_sha256": (
                previous.receipt_sha256 if previous is not None else None
            ),
            "real_trading_enabled": False,
            "recorded_at": recorded_at.isoformat(),
            "schema_version": self.SCHEMA_VERSION,
            "selected_artifact_sha256": manifest.artifact_sha256,
            "selected_manifest_id": manifest.manifest_id,
            "selected_manifest_sha256": manifest.sha256(),
            "selected_model_id": manifest.model_id,
            "selected_model_version": manifest.model_version,
            "selection_id": selection_id,
            "sequence": len(history),
            "simulation_only": True,
            "validation_evidence_sha256": manifest.validation_evidence_sha256,
            "validation_plan_sha256": validation_plan.sha256(),
        }
        digest = _canonical_sha256(unsigned)
        payload = {**unsigned, "receipt_sha256": digest}
        receipt = self._receipt_from_payload(payload)
        receipt_path = self.receipts_dir / f"{receipt.sequence:020d}-{digest}.json"
        self._write_receipt_once(
            receipt_path,
            (_canonical_json(payload) + "\n").encode("utf-8"),
        )
        pointer = {
            "receipt_sha256": receipt.receipt_sha256,
            "schema_version": self.POINTER_SCHEMA_VERSION,
            "selected_manifest_sha256": receipt.selected_manifest_sha256,
            "selection_id": receipt.selection_id,
            "sequence": receipt.sequence,
        }
        self._replace_current_pointer(pointer)
        return receipt

    def load_history(self) -> Tuple[ChampionSelectionReceipt, ...]:
        with self._locked(exclusive=False):
            return self._load_history_unlocked()

    def _load_history_unlocked(self) -> Tuple[ChampionSelectionReceipt, ...]:
        receipts = []
        try:
            paths = sorted(self.receipts_dir.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise ChampionRegistryError("receipts_directory_unreadable") from exc
        for path in paths:
            match = _RECEIPT_FILENAME_RE.fullmatch(path.name)
            if match is None:
                raise ChampionRegistryError("receipt_filename_invalid")
            try:
                encoded = _read_bytes_no_follow(path, role="receipt")
                decoded = encoded.decode("utf-8")
                payload = json.loads(decoded)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ChampionRegistryError("receipt_unreadable") from exc
            if not isinstance(payload, dict) or encoded != (
                _canonical_json(payload) + "\n"
            ).encode("utf-8"):
                raise ChampionRegistryError("receipt_not_canonical")
            receipt = self._receipt_from_payload(payload)
            if receipt.sequence != int(
                match.group("sequence")
            ) or receipt.receipt_sha256 != match.group("sha256"):
                raise ChampionRegistryError("receipt_filename_mismatch")
            receipts.append(receipt)

        previous: Optional[ChampionSelectionReceipt] = None
        selection_ids = set()
        activated_manifests = set()
        for expected_sequence, receipt in enumerate(receipts):
            if receipt.sequence != expected_sequence:
                raise ChampionRegistryError("receipt_sequence_invalid")
            if receipt.selection_id in selection_ids:
                raise ChampionRegistryError("duplicate_selection_id")
            selection_ids.add(receipt.selection_id)
            if previous is None:
                if (
                    receipt.previous_receipt_sha256 is not None
                    or receipt.expected_current_manifest_sha256 is not None
                ):
                    raise ChampionRegistryError("initial_receipt_chain_invalid")
            else:
                if receipt.previous_receipt_sha256 != previous.receipt_sha256:
                    raise ChampionRegistryError("previous_receipt_chain_broken")
                if (
                    receipt.expected_current_manifest_sha256
                    != previous.selected_manifest_sha256
                ):
                    raise ChampionRegistryError("expected_current_chain_broken")
                if receipt.recorded_at < previous.recorded_at:
                    raise ChampionRegistryError("receipt_time_nonmonotonic")
            if receipt.action == "rollback":
                if receipt.selected_manifest_sha256 not in activated_manifests:
                    raise ChampionRegistryError("rollback_target_unknown_in_chain")
            else:
                activated_manifests.add(receipt.selected_manifest_sha256)
            previous = receipt

        history = tuple(receipts)
        self._validate_current_pointer(history)
        return history

    def load_current(self) -> ChampionSelectionReceipt:
        history = self.load_history()
        if not history:
            raise ChampionRegistryError("current_pointer_missing")
        return history[-1]

    def _validate_current_pointer(
        self,
        history: Tuple[ChampionSelectionReceipt, ...],
    ) -> None:
        try:
            pointer_mode = self.current_path.lstat().st_mode
        except FileNotFoundError:
            pointer_mode = None
        except OSError as exc:
            raise ChampionRegistryError("current_pointer_unreadable") from exc
        if pointer_mode is not None and stat.S_ISLNK(pointer_mode):
            raise ChampionRegistryError("current_pointer_symlink_forbidden")
        if not history:
            if pointer_mode is not None:
                raise ChampionRegistryError("current_pointer_without_history")
            return
        if pointer_mode is None:
            raise ChampionRegistryError("current_pointer_missing")
        try:
            encoded = _read_bytes_no_follow(
                self.current_path,
                role="current_pointer",
            )
            decoded = encoded.decode("utf-8")
            pointer = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ChampionRegistryError("current_pointer_unreadable") from exc
        if not isinstance(pointer, dict) or set(pointer) != _POINTER_FIELDS:
            raise ChampionRegistryError("current_pointer_fields_invalid")
        if encoded != (_canonical_json(pointer) + "\n").encode("utf-8"):
            raise ChampionRegistryError("current_pointer_not_canonical")
        tail = history[-1]
        expected = {
            "receipt_sha256": tail.receipt_sha256,
            "schema_version": self.POINTER_SCHEMA_VERSION,
            "selected_manifest_sha256": tail.selected_manifest_sha256,
            "selection_id": tail.selection_id,
            "sequence": tail.sequence,
        }
        if pointer != expected:
            raise ChampionRegistryError("current_pointer_not_chain_tail")

    def _receipt_from_payload(
        self,
        payload: Mapping[str, Any],
    ) -> ChampionSelectionReceipt:
        if not isinstance(payload, dict) or set(payload) != _RECEIPT_FIELDS:
            raise ChampionRegistryError("receipt_fields_invalid")
        if payload["schema_version"] != self.SCHEMA_VERSION:
            raise ChampionRegistryError("receipt_schema_version_invalid")
        if payload["action"] not in {"activate", "rollback"}:
            raise ChampionRegistryError("receipt_action_invalid")
        sequence = payload["sequence"]
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ChampionRegistryError("receipt_sequence_invalid")
        for field_name in (
            "selection_id",
            "selected_manifest_id",
            "selected_model_id",
            "selected_model_version",
            "human_approval_reference",
        ):
            _require_nonempty_text(payload[field_name], field_name=field_name)
        for field_name in (
            "selected_manifest_sha256",
            "selected_artifact_sha256",
            "validation_plan_sha256",
            "validation_evidence_sha256",
            "lifecycle_record_sha256",
            "receipt_sha256",
        ):
            _require_sha256(payload[field_name], field_name=field_name)
        _require_optional_sha256(
            payload["expected_current_manifest_sha256"],
            field_name="expected_current_manifest_sha256",
        )
        _require_optional_sha256(
            payload["previous_receipt_sha256"],
            field_name="previous_receipt_sha256",
        )
        if (
            payload["capital_layer"] != "simulated"
            or payload["account_type"] != "simulated"
            or payload["simulation_only"] is not True
            or payload["real_trading_enabled"] is not False
            or payload["live_transition_authorized"] is not False
            or payload["automatic_promotion_enabled"] is not False
            or payload["automatic_risk_expansion_enabled"] is not False
        ):
            raise ChampionRegistryError("receipt_simulation_only_contract_violated")
        digest = payload["receipt_sha256"]
        unsigned = dict(payload)
        unsigned.pop("receipt_sha256")
        if digest != _canonical_sha256(unsigned):
            raise ChampionRegistryError("receipt_hash_mismatch")
        try:
            recorded_at = datetime.fromisoformat(payload["recorded_at"])
        except (TypeError, ValueError) as exc:
            raise ChampionRegistryError("receipt_recorded_at_invalid") from exc
        if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
            raise ChampionRegistryError("receipt_recorded_at_invalid")
        return ChampionSelectionReceipt(
            schema_version=payload["schema_version"],
            selection_id=payload["selection_id"],
            sequence=sequence,
            action=payload["action"],
            selected_manifest_id=payload["selected_manifest_id"],
            selected_manifest_sha256=payload["selected_manifest_sha256"],
            selected_model_id=payload["selected_model_id"],
            selected_model_version=payload["selected_model_version"],
            selected_artifact_sha256=payload["selected_artifact_sha256"],
            validation_plan_sha256=payload["validation_plan_sha256"],
            validation_evidence_sha256=payload["validation_evidence_sha256"],
            lifecycle_record_sha256=payload["lifecycle_record_sha256"],
            human_approval_reference=payload["human_approval_reference"],
            recorded_at=recorded_at,
            expected_current_manifest_sha256=payload[
                "expected_current_manifest_sha256"
            ],
            previous_receipt_sha256=payload["previous_receipt_sha256"],
            receipt_sha256=payload["receipt_sha256"],
            capital_layer=payload["capital_layer"],
            account_type=payload["account_type"],
            simulation_only=payload["simulation_only"],
            real_trading_enabled=payload["real_trading_enabled"],
            live_transition_authorized=payload["live_transition_authorized"],
            automatic_promotion_enabled=payload["automatic_promotion_enabled"],
            automatic_risk_expansion_enabled=payload[
                "automatic_risk_expansion_enabled"
            ],
        )


__all__ = [
    "ChampionRegistryError",
    "ChampionSelectionReceipt",
    "ChampionSelectionRegistry",
]
