#!/usr/bin/env python3
"""Read-only verifier for retired A-share simulated-account epochs.

Numeric epochs 1 (200,000 CNY) and 2 (50,000 CNY), their ledger files, and
their cutover state are immutable legacy evidence.  They are not an execution
authority and must never be imported into the fresh execution lineage.

This module intentionally contains no filesystem write operation.  The old
public mutation/authority entry points remain as fail-closed tombstones so a
stale caller cannot silently reactivate epoch semantics.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from shared.execution.execution_lineage import (
    ASHARE_EXECUTION_LINEAGE_ID,
    ExecutionLineageError,
    require_execution_lineage,
)


ROOT = Path(__file__).resolve().parents[2]
LEGACY_LOCAL_SIM_ROOT = ROOT / "shared" / "logs" / "local_sim"
LEGACY_EPOCH_STATE_PATH = ROOT / "shared" / "logs" / "sim_epoch_state.json"
LEGACY_EPOCH_ARCHIVE_ROOT = ROOT / "shared" / "logs" / "epoch_archive"
LINEAGE_MANIFEST_FILENAME = "execution_lineage_manifest.json"

# Historical facts only.  Mapping proxies prevent in-process mutation and the
# absence of CURRENT_EPOCH_ID is deliberate: no numeric epoch is current.
EPOCHS: Mapping[int, Mapping[str, Any]] = MappingProxyType(
    {
        1: MappingProxyType(
            {
                "id": 1,
                "label": "legacy_200k",
                "capital_cny": 200_000.0,
                "status": "immutable_legacy",
            }
        ),
        2: MappingProxyType(
            {
                "id": 2,
                "label": "legacy_50k",
                "capital_cny": 50_000.0,
                "status": "immutable_legacy",
            }
        ),
    }
)
CURRENT_EPOCH_ID = None


class LegacyExecutionFreezeError(RuntimeError):
    """Raised when legacy evidence is unsafe or a retired API is invoked."""


def _retired_authority(*_args: Any, **_kwargs: Any) -> Any:
    raise LegacyExecutionFreezeError("numeric_epoch_authority_retired")


def get_current_epoch() -> dict[str, Any]:
    return _retired_authority()


def get_epoch(epoch_id: int) -> dict[str, Any]:
    return _retired_authority(epoch_id)


def epoch_capital_cny(epoch_id: int) -> float:
    return _retired_authority(epoch_id)


def require_authoritative_epoch_metadata(state: dict[str, Any]) -> dict[str, Any]:
    return _retired_authority(state)


def epoch_ledger_root(epoch_id: int) -> Path:
    return _retired_authority(epoch_id)


def epoch_state_path() -> Path:
    """Return the retired state path for read-only inspection."""

    return LEGACY_EPOCH_STATE_PATH


def _read_json_object(path: Path) -> dict[str, Any] | None:
    if path.is_symlink():
        raise LegacyExecutionFreezeError(f"legacy_evidence_not_regular_file:{path}")
    if not path.exists():
        return None
    if not path.is_file():
        raise LegacyExecutionFreezeError(f"legacy_evidence_not_regular_file:{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LegacyExecutionFreezeError(f"legacy_evidence_unreadable:{path}") from exc
    if not isinstance(payload, dict):
        raise LegacyExecutionFreezeError(f"legacy_evidence_not_object:{path}")
    return payload


def read_epoch_state() -> dict[str, Any]:
    """Read retired epoch state without manufacturing defaults or timestamps."""

    payload = _read_json_object(LEGACY_EPOCH_STATE_PATH)
    if payload is None:
        return {
            "status": "legacy_epoch_state_absent",
            "authority_status": "retired",
            "path": str(LEGACY_EPOCH_STATE_PATH),
            "real_trading_enabled": False,
        }
    return {
        "status": "legacy_epoch_state_frozen",
        "authority_status": "retired",
        "path": str(LEGACY_EPOCH_STATE_PATH),
        "legacy_state": payload,
        "real_trading_enabled": False,
    }


def read_ledger_epoch_metadata(ledger_path: Path) -> dict[str, Any] | None:
    """Read retired ``.epoch_metadata.json`` strictly as legacy evidence."""

    return _read_json_object(Path(ledger_path) / ".epoch_metadata.json")


def _assert_no_symlink(path: Path) -> None:
    current = path.absolute()
    system_root_aliases = {Path("/var"), Path("/tmp"), Path("/etc")}
    while True:
        if current.is_symlink() and current not in system_root_aliases:
            raise LegacyExecutionFreezeError(
                f"legacy_evidence_symlink_forbidden:{path}"
            )
        if current == current.parent:
            break
        current = current.parent


def _tree_fingerprint(root: Path) -> dict[str, Any]:
    """Hash a legacy tree without following links or changing atime-sensitive data."""

    root = Path(root)
    _assert_no_symlink(root)
    if not root.exists():
        return {
            "path": str(root),
            "exists": False,
            "tree_sha256": hashlib.sha256(b"missing").hexdigest(),
            "file_count": 0,
            "record_count": 0,
        }
    if not root.is_dir():
        raise LegacyExecutionFreezeError(f"legacy_root_not_directory:{root}")

    digest = hashlib.sha256()
    file_count = 0
    record_count = 0
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        directory_names.sort()
        file_names.sort()
        for name in directory_names:
            entry = directory_path / name
            mode = entry.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise LegacyExecutionFreezeError(
                    f"legacy_evidence_symlink_forbidden:{entry}"
                )
            relative = entry.relative_to(root).as_posix()
            digest.update(b"D\0" + relative.encode("utf-8") + b"\0")
        for name in file_names:
            entry = directory_path / name
            mode = entry.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise LegacyExecutionFreezeError(
                    f"legacy_evidence_not_regular_file:{entry}"
                )
            relative = entry.relative_to(root).as_posix()
            data = entry.read_bytes()
            digest.update(b"F\0" + relative.encode("utf-8") + b"\0")
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(hashlib.sha256(data).digest())
            file_count += 1
            if entry.suffix == ".jsonl":
                record_count += sum(1 for line in data.splitlines() if line.strip())
    return {
        "path": str(root),
        "exists": True,
        "tree_sha256": digest.hexdigest(),
        "file_count": file_count,
        "record_count": record_count,
    }


def _verify_fresh_zero_import(fresh_root: Path) -> dict[str, Any]:
    fresh_root = Path(fresh_root)
    _assert_no_symlink(fresh_root)
    if fresh_root.name != ASHARE_EXECUTION_LINEAGE_ID:
        raise LegacyExecutionFreezeError("fresh_execution_root_not_lineage_namespaced")
    manifest = _read_json_object(fresh_root / LINEAGE_MANIFEST_FILENAME)
    if manifest is None:
        raise LegacyExecutionFreezeError("fresh_execution_manifest_missing")
    try:
        require_execution_lineage(manifest)
    except ExecutionLineageError as exc:
        raise LegacyExecutionFreezeError(str(exc)) from exc
    if manifest.get("source") != "fresh_zero_import_bootstrap":
        raise LegacyExecutionFreezeError("fresh_execution_manifest_source_invalid")
    if (
        type(manifest.get("imported_legacy_record_count")) is not int
        or manifest.get("imported_legacy_record_count") != 0
    ):
        raise LegacyExecutionFreezeError("legacy_import_detected")
    if manifest.get("legacy_roots_read") != []:
        raise LegacyExecutionFreezeError("fresh_bootstrap_read_legacy_roots")
    initial_cash = manifest.get("initial_cash_cny")
    if (
        isinstance(initial_cash, bool)
        or not isinstance(initial_cash, (int, float))
        or float(initial_cash) != 50_000.0
    ):
        raise LegacyExecutionFreezeError("fresh_execution_initial_cash_invalid")
    if manifest.get("real_trading_enabled") is not False:
        raise LegacyExecutionFreezeError("fresh_execution_real_trading_forbidden")
    return manifest


def verify_legacy_execution_freeze(
    *,
    fresh_root: Path | str,
    legacy_roots: Iterable[Path | str] | None = None,
    expected_fingerprints: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Verify legacy immutability and fresh/legacy physical separation.

    ``expected_fingerprints`` may key entries by either the supplied path or
    its absolute form.  A mismatch blocks the verification; no baseline is
    ever written automatically.
    """

    fresh = Path(fresh_root)
    manifest = _verify_fresh_zero_import(fresh)
    roots = tuple(
        Path(root)
        for root in (
            legacy_roots
            if legacy_roots is not None
            else (LEGACY_LOCAL_SIM_ROOT, LEGACY_EPOCH_ARCHIVE_ROOT)
        )
    )
    fresh_absolute = fresh.absolute()
    snapshots: list[dict[str, Any]] = []
    expected = dict(expected_fingerprints or {})
    for legacy_root in roots:
        legacy_absolute = legacy_root.absolute()
        if (
            fresh_absolute == legacy_absolute
            or legacy_absolute in fresh_absolute.parents
            or fresh_absolute in legacy_absolute.parents
        ):
            raise LegacyExecutionFreezeError(
                "fresh_execution_root_overlaps_legacy_root"
            )
        snapshot = _tree_fingerprint(legacy_root)
        expected_hash = expected.get(
            str(legacy_root), expected.get(str(legacy_absolute))
        )
        if expected_hash is not None and expected_hash != snapshot["tree_sha256"]:
            raise LegacyExecutionFreezeError(
                f"legacy_fingerprint_mismatch:{legacy_root}"
            )
        snapshot["expected_fingerprint_verified"] = expected_hash is not None
        snapshots.append(snapshot)
    return {
        "status": "legacy_execution_frozen",
        "authority_status": "retired",
        "fresh_execution_lineage_id": manifest["execution_lineage_id"],
        "fresh_zero_import_verified": True,
        "legacy_roots": snapshots,
        "real_trading_enabled": False,
    }


def dry_run_cutover(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise LegacyExecutionFreezeError("runtime_cutover_retired_fresh_bootstrap_required")


def apply_cutover(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise LegacyExecutionFreezeError("runtime_cutover_retired_fresh_bootstrap_required")


__all__ = [
    "CURRENT_EPOCH_ID",
    "EPOCHS",
    "LEGACY_EPOCH_ARCHIVE_ROOT",
    "LEGACY_EPOCH_STATE_PATH",
    "LEGACY_LOCAL_SIM_ROOT",
    "LegacyExecutionFreezeError",
    "apply_cutover",
    "dry_run_cutover",
    "epoch_capital_cny",
    "epoch_ledger_root",
    "epoch_state_path",
    "get_current_epoch",
    "get_epoch",
    "read_epoch_state",
    "read_ledger_epoch_metadata",
    "require_authoritative_epoch_metadata",
    "verify_legacy_execution_freeze",
]
