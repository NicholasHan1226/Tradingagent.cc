#!/usr/bin/env python3
"""Simulated account epoch cutover mechanism.

Defines immutable epoch definitions for A-share/CN-Futures simulated capital
and provides an authoritative-path cutover from epoch 1 to epoch 2.

Epoch 1 (legacy_200k): historical 200,000 CNY capital, ledger contents at
  ``shared/logs/local_sim/`` — archived immutably at cutover.

Epoch 2 (current_50k): canonical 50,000 CNY capital since cutover, ledger at
  ``shared/logs/local_sim/`` — SAME authoritative path, recreated fresh
  at cutover with empty cash/positions/trades.

There is NEVER a ``local_sim_epoch2`` path. Both epochs share the single
authoritative ``shared/logs/local_sim/`` directory. The cutover keeps that
directory and its lock inode in place, moves the old ledger contents into an
archive while holding the lock, and bootstraps epoch 2 before releasing it.

Key invariants:
- Missing epoch state means legacy epoch 1, never current epoch 2.
- Dry-run reports exact actions and writes nothing.
- Apply acquires the local_sim lock, archives old content while preserving the
  active lock inode, bootstraps the authoritative path as epoch 2, and writes
  metadata before releasing the lock.
- Old files are never rewritten; archive target collisions fail closed.
- Apply is idempotent.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Epoch definitions (immutable)
# ---------------------------------------------------------------------------

EPOCHS: dict[int, dict[str, Any]] = {
    1: {
        "id": 1,
        "label": "legacy_200k",
        "capital_cny": 200_000.0,
        "description": "Historical canonical CNY simulated-capital reference at 200,000 CNY.",
    },
    2: {
        "id": 2,
        "label": "current_50k",
        "capital_cny": 50_000.0,
        "description": "Canonical current A-share / CN-Futures simulated capital at 50,000 CNY.",
    },
}

CURRENT_EPOCH_ID: int = 2

# ---------------------------------------------------------------------------
# Epoch API
# ---------------------------------------------------------------------------


def get_current_epoch() -> dict[str, Any]:
    """Return the definition for the default current epoch (code-level)."""
    return EPOCHS[CURRENT_EPOCH_ID]


def get_epoch(epoch_id: int) -> dict[str, Any]:
    """Return the definition for *epoch_id*.

    Raises ``KeyError`` for unknown ids.
    """
    return EPOCHS[epoch_id]


def epoch_capital_cny(epoch_id: int) -> float:
    """Return the ``capital_cny`` for *epoch_id*."""
    return float(EPOCHS[epoch_id]["capital_cny"])


def epoch_ledger_root(epoch_id: int) -> Path:
    """Deterministic ledger root for a given epoch.

    Both epochs share the SAME authoritative path:
    ``shared/logs/local_sim/``.  There is no ``local_sim_epoch2``
    directory — the cutover physically replaces the content at this
    single authoritative path.
    """
    if epoch_id not in EPOCHS:
        raise KeyError(f"Unknown epoch id {epoch_id}. Known: {sorted(EPOCHS.keys())}")
    return ROOT / "shared" / "logs" / "local_sim"


def epoch_state_path() -> Path:
    """Path to the persisted epoch-state JSON file."""
    return ROOT / "shared" / "logs" / "sim_epoch_state.json"


def _epoch_metadata_path(ledger_path: Path) -> Path:
    """Path to the .epoch_metadata.json inside the authoritative ledger directory."""
    return ledger_path / ".epoch_metadata.json"


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_epoch_state() -> dict[str, Any]:
    """Read the persisted epoch state.

    If no state file exists, returns epoch 1 (legacy) — missing state
    always means legacy, never current epoch 2.  This is the critical
    safety invariant: a fresh deployment or a missing file does not
    silently assume the cutover has already happened.
    """
    path = epoch_state_path()
    if not path.exists():
        return {
            "current_epoch_id": 1,
            "activated_at": _now_iso(),
            "source": "no_state_file",
            "note": "No persisted epoch state; defaulting to legacy epoch 1.",
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "current_epoch_id": 1,
            "activated_at": _now_iso(),
            "source": "corrupt_state_file",
            "note": "Corrupt epoch state file; defaulting to legacy epoch 1.",
        }
    if not isinstance(data, dict):
        return {
            "current_epoch_id": 1,
            "activated_at": _now_iso(),
            "source": "invalid_state_format",
            "note": "Invalid epoch state format; defaulting to legacy epoch 1.",
        }
    return data


def _write_epoch_state(state: dict[str, Any]) -> None:
    """Atomically write the epoch state file."""
    path = epoch_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def read_ledger_epoch_metadata(ledger_path: Path) -> dict[str, Any] | None:
    """Read .epoch_metadata.json from the authoritative ledger directory.

    Returns None if the file does not exist or is corrupt.
    """
    meta_path = _epoch_metadata_path(ledger_path)
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _cutover_state_error(
    state: dict[str, Any],
    metadata: dict[str, Any] | None,
) -> str | None:
    source = str(state.get("source") or "")
    if source in {"corrupt_state_file", "invalid_state_format"}:
        return f"Epoch state is not trustworthy ({source}); refusing capital cutover."
    current_id = state.get("current_epoch_id")
    if current_id not in EPOCHS:
        return f"Epoch state contains unknown current_epoch_id={current_id!r}."
    if metadata is not None:
        metadata_id = metadata.get("current_epoch_id")
        if metadata_id not in EPOCHS or metadata_id != current_id:
            return (
                "Epoch state and authoritative ledger metadata are inconsistent: "
                f"state={current_id!r}, metadata={metadata_id!r}."
            )
    return None


# ---------------------------------------------------------------------------
# Lock acquisition
# ---------------------------------------------------------------------------

_LOCK_RETRY_ATTEMPTS = 3
_LOCK_RETRY_DELAY_SECONDS = 0.1


def _acquire_ledger_lock(ledger_path: Path) -> int:
    """Acquire the exclusive lock on the ledger directory.

    Returns the file descriptor of the opened lock file.  Caller is
    responsible for releasing the lock and closing the fd.

    Raises TimeoutError if the lock cannot be acquired.
    """
    lock_path = ledger_path / ".local_sim.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    retry_errnos = {errno.EACCES, errno.EAGAIN, getattr(errno, "EWOULDBLOCK", errno.EAGAIN)}
    last_error: OSError | None = None
    for attempt in range(1, _LOCK_RETRY_ATTEMPTS + 1):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as exc:
            if exc.errno not in retry_errnos:
                os.close(fd)
                raise
            last_error = exc
            if attempt < _LOCK_RETRY_ATTEMPTS:
                time.sleep(_LOCK_RETRY_DELAY_SECONDS * attempt)
    os.close(fd)
    raise TimeoutError(
        f"Could not acquire local sim lock {lock_path} after {_LOCK_RETRY_ATTEMPTS} attempts"
    ) from last_error


def _release_ledger_lock(fd: int) -> None:
    """Release the exclusive lock and close the file descriptor."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


_ARCHIVE_MANIFEST = "epoch_archive_manifest.json"


def _archive_dir_for_epoch(epoch_id: int, archive_root: Path) -> Path:
    """Deterministic archive directory for an epoch."""
    epoch = EPOCHS[epoch_id]
    return archive_root / f"epoch_{epoch_id}_{epoch['label']}"


def _archive_has_valid_manifest(archive_dir: Path, epoch_id: int) -> bool:
    """Check whether the archive directory has a valid manifest proving
    a completed migration for the given epoch."""
    manifest_path = archive_dir / _ARCHIVE_MANIFEST
    if not manifest_path.exists():
        return False
    try:
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        return existing.get("epoch_id") == epoch_id
    except (OSError, json.JSONDecodeError):
        return False


def _empty_pnl() -> dict[str, Any]:
    current_capital = float(EPOCHS[CURRENT_EPOCH_ID]["capital_cny"])
    return {
        "ashare_sim": {
            "account": "ashare_sim",
            "total_trades": 0,
            "buys": 0,
            "sells": 0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "market_value": 0.0,
            "total_pnl": 0.0,
            "cash_available": current_capital,
            "positions": {},
        }
    }


def _write_current_epoch_bootstrap(ledger_path: Path, positions_snapshot_path: Path | None) -> None:
    current_capital = float(EPOCHS[CURRENT_EPOCH_ID]["capital_cny"])
    positions = {"ashare_sim": {}}
    pnl = _empty_pnl()
    (ledger_path / "local_sim_positions.json").write_text(
        json.dumps(positions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (ledger_path / "local_sim_pnl.json").write_text(
        json.dumps(pnl, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if positions_snapshot_path is None:
        return
    positions_snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "snapshot_id": "simulated_ashare_positions",
        "market": "ashare",
        "account_type": "simulated",
        "capital_layer": "simulated",
        "source": "capital_epoch_cutover",
        "synced_at": _now_iso(),
        "positions": [],
        "positions_by_account": positions,
        "pnl": pnl,
        "account_view": "strategy_samples_only",
        "audit_positions_by_account": positions,
        "audit_pnl": pnl,
        "bootstrap_state": "no_trades_yet",
        "cash_available": current_capital,
        "trade_date": datetime.now(timezone.utc).strftime("%Y%m%d"),
        "capital_epoch": CURRENT_EPOCH_ID,
    }
    positions_snapshot_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Dry-run cutover
# ---------------------------------------------------------------------------


def dry_run_cutover(
    *,
    ledger_path: Path | str | None = None,
    positions_snapshot_path: Path | str | None = None,
    tiers_root: Path | str | None = None,
    archive_root: Path | str | None = None,
) -> dict[str, Any]:
    """Report exactly what an apply-cutover would do, without writing anything.

    Parameters
    ----------
    ledger_path:
        Path to the authoritative local_sim directory.  Defaults to
        ``epoch_ledger_root(1)``.
    positions_snapshot_path:
        Optional path to the external ``simulated_ashare_positions.json``
        snapshot to archive as evidence.
    tiers_root:
        Optional path to ``local_sim_tiers/`` directory to archive as evidence.
    archive_root:
        Root directory for epoch archives.  Defaults to
        ``shared/logs/epoch_archive``.

    Returns an operational summary with the list of planned actions.
    """
    lp = Path(ledger_path) if ledger_path is not None else epoch_ledger_root(1)
    archive = Path(archive_root) if archive_root is not None else (ROOT / "shared" / "logs" / "epoch_archive")
    ps_path = Path(positions_snapshot_path) if positions_snapshot_path is not None else None
    tr_path = Path(tiers_root) if tiers_root is not None else None

    current_state = read_epoch_state()
    current_id = current_state.get("current_epoch_id", 1)
    current_metadata = read_ledger_epoch_metadata(lp)
    state_error = _cutover_state_error(current_state, current_metadata)
    if state_error:
        return {"status": "error", "reason": state_error}

    actions: list[dict[str, Any]] = []

    if current_id == 2:
        if not current_metadata:
            return {
                "status": "error",
                "reason": "Epoch state says epoch 2 but authoritative ledger metadata is missing.",
            }
        return {
            "status": "dry_run",
            "note": "Already at epoch 2; no actions needed.",
            "current_epoch_id": 2,
            "actions": actions,
        }

    archive_dir = _archive_dir_for_epoch(1, archive)

    # Check archive target collision
    if archive_dir.exists():
        return {
            "status": "error",
            "reason": f"Archive target collision or incomplete prior cutover: {archive_dir}",
            "archive_dir": str(archive_dir),
        }

    # Discover files to archive
    files_to_archive: list[str] = []
    if lp.exists():
        for entry in sorted(lp.iterdir()):
            if entry.is_file() or entry.is_dir():
                if entry.name != ".local_sim.lock":
                    files_to_archive.append(entry.name)

    if files_to_archive:
        actions.append({
            "action": "move_ledger_contents_to_archive",
            "source": str(lp),
            "destination": str(archive_dir),
            "file_count": len(files_to_archive),
            "files": sorted(files_to_archive),
        })
    else:
        actions.append({
            "action": "archive_empty_ledger",
            "source": str(lp),
            "destination": str(archive_dir),
            "note": "Ledger directory is empty; will create archive with just a manifest.",
        })

    if ps_path and ps_path.exists():
        actions.append({
            "action": "archive_positions_snapshot",
            "source": str(ps_path),
            "destination": str(archive_dir / ps_path.name),
        })

    if tr_path and tr_path.exists():
        tier_entries = []
        for entry in sorted(tr_path.rglob("*")):
            if entry.is_file():
                tier_entries.append(str(entry.relative_to(tr_path)))
        actions.append({
            "action": "archive_tier_evidence",
            "source": str(tr_path),
            "destination": str(archive_dir / tr_path.name),
            "file_count": len(tier_entries),
        })

    actions.append({
        "action": "bootstrap_authoritative_path_in_place",
        "path": str(lp),
        "lock_preserved": str(lp / ".local_sim.lock"),
    })

    actions.append({
        "action": "write_epoch_metadata",
        "path": str(_epoch_metadata_path(lp)),
        "epoch_id": 2,
        "capital_cny": EPOCHS[2]["capital_cny"],
    })

    actions.append({
        "action": "write_epoch_state",
        "path": str(epoch_state_path()),
        "from_epoch": 1,
        "to_epoch": 2,
    })

    return {
        "status": "dry_run",
        "from_epoch": 1,
        "to_epoch": 2,
        "actions": actions,
    }


# ---------------------------------------------------------------------------
# Apply cutover
# ---------------------------------------------------------------------------


def apply_cutover(
    *,
    ledger_path: Path | str | None = None,
    positions_snapshot_path: Path | str | None = None,
    tiers_root: Path | str | None = None,
    archive_root: Path | str | None = None,
) -> dict[str, Any]:
    """Execute the authoritative-path cutover from epoch 1 to epoch 2.

    Steps:
    1. Read current epoch state — if already epoch 2, return no-op.
    2. Verify archive target is clear (fail closed on collision without manifest).
    3. Acquire the ``.local_sim.lock`` in the ledger directory.
    4. Keep ``local_sim/`` and its lock inode in place, moving the old ledger
       contents into the archive while the lock is held.
    5. Copy external positions snapshot into archive as evidence (if supplied).
    6. Copy tier experiments data into archive as evidence (if supplied).
    7. Write archive manifest proving completion.
    8. Bootstrap the same ``local_sim/`` path as fresh epoch 2 with metadata.
    9. Write the epoch state file.
    10. Release the lock.

    Old files are never rewritten. The authoritative lock remains stable for
    the full operation, so normal ledger writers cannot bypass the cutover.
    Running again is idempotent (returns ``already_migrated``).

    Returns an operational summary.
    """
    lp = Path(ledger_path) if ledger_path is not None else epoch_ledger_root(1)
    archive = Path(archive_root) if archive_root is not None else (ROOT / "shared" / "logs" / "epoch_archive")
    ps_path = Path(positions_snapshot_path) if positions_snapshot_path is not None else None
    tr_path = Path(tiers_root) if tiers_root is not None else None

    # --- Idempotency: check current state ---
    current_state = read_epoch_state()
    current_id = current_state.get("current_epoch_id", 1)
    current_metadata = read_ledger_epoch_metadata(lp)
    state_error = _cutover_state_error(current_state, current_metadata)
    if state_error:
        return {"status": "error", "reason": state_error}

    if current_id == 2:
        if not current_metadata or current_metadata.get("current_epoch_id") != 2:
            return {
                "status": "error",
                "reason": "Epoch state says epoch 2 but authoritative ledger metadata is missing or inconsistent.",
            }
        return {
            "status": "already_migrated",
            "note": "Epoch state already at epoch 2; cutover is a no-op.",
            "current_epoch_id": 2,
        }

    archive_dir = _archive_dir_for_epoch(1, archive)

    # --- Archive collision check ---
    if archive_dir.exists():
        if _archive_has_valid_manifest(archive_dir, 1) and current_metadata and current_metadata.get("current_epoch_id") == 2:
            return {"status": "already_migrated", "current_epoch_id": 2, "archive_dir": str(archive_dir)}
        return {
            "status": "error",
            "reason": f"Archive target collision or incomplete prior cutover: {archive_dir}",
            "archive_dir": str(archive_dir),
        }

    # --- Acquire lock ---
    lock_fd: int | None = None
    try:
        lock_fd = _acquire_ledger_lock(lp)
    except TimeoutError as exc:
        return {
            "status": "error",
            "reason": f"Could not acquire ledger lock: {exc}",
        }

    moved_ledger_entries: list[tuple[Path, Path]] = []
    moved_tiers = False
    tier_dest: Path | None = None
    archived_positions: Path | None = None
    try:
        # --- Step 1: Move ledger contents to archive while preserving lock ---
        archive_dir.parent.mkdir(parents=True, exist_ok=True)
        archive_dir.mkdir(parents=True, exist_ok=False)
        for source in sorted(lp.iterdir()):
            if source.name == ".local_sim.lock":
                continue
            destination = archive_dir / source.name
            os.replace(str(source), str(destination))
            moved_ledger_entries.append((source, destination))
        archived_files_count = sum(1 for item in archive_dir.rglob("*") if item.is_file())

            # --- Step 2: Archive external positions snapshot ---
        if ps_path and ps_path.exists():
            archived_positions = archive_dir / ps_path.name
            shutil.copy2(str(ps_path), str(archived_positions))

            # --- Step 3: Archive tier evidence ---
        if tr_path and tr_path.exists():
            tier_dest = archive_dir / tr_path.name
            os.replace(str(tr_path), str(tier_dest))
            moved_tiers = True

            # --- Step 4: Write archive manifest ---
        manifest = {
            "epoch_id": 1,
            "epoch_label": EPOCHS[1]["label"],
            "capital_cny": EPOCHS[1]["capital_cny"],
            "archived_at": _now_iso(),
            "ledger_root_archived": str(lp),
            "archive_file_count": archived_files_count,
            "positions_snapshot_archived": archived_positions is not None,
            "tier_evidence_archived": moved_tiers,
        }
        manifest_path = archive_dir / _ARCHIVE_MANIFEST
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        # --- Step 5: Bootstrap authoritative path as fresh epoch 2 ---
        epoch_meta = {
            "current_epoch_id": 2,
            "epoch_label": EPOCHS[2]["label"],
            "capital_cny": EPOCHS[2]["capital_cny"],
            "cutover_timestamp": _now_iso(),
            "previous_epoch_id": 1,
            "previous_epoch_label": EPOCHS[1]["label"],
            "archived_to": str(archive_dir),
        }
        meta_path = _epoch_metadata_path(lp)
        meta_path.write_text(
            json.dumps(epoch_meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        _write_current_epoch_bootstrap(lp, ps_path)

        # --- Step 6: Persist epoch state ---
        new_state = {
            "current_epoch_id": 2,
            "previous_epoch_id": current_state.get("current_epoch_id", 1),
            "cutover_timestamp": _now_iso(),
            "previous_state": {
                "current_epoch_id": current_state.get("current_epoch_id"),
                "activated_at": current_state.get("activated_at"),
            },
            "capital_cny": EPOCHS[2]["capital_cny"],
        }
        _write_epoch_state(new_state)

    except Exception as exc:
        try:
            if moved_tiers and tier_dest is not None and tier_dest.exists() and tr_path is not None:
                tr_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(str(tier_dest), str(tr_path))
            if archived_positions is not None and archived_positions.exists() and ps_path is not None:
                shutil.copy2(str(archived_positions), str(ps_path))
            if lp.exists():
                for current_entry in list(lp.iterdir()):
                    if current_entry.name == ".local_sim.lock":
                        continue
                    if current_entry.is_dir():
                        shutil.rmtree(str(current_entry))
                    else:
                        current_entry.unlink(missing_ok=True)
            for source, destination in reversed(moved_ledger_entries):
                if destination.exists():
                    os.replace(str(destination), str(source))
            if archived_positions is not None and archived_positions.exists():
                archived_positions.unlink()
            manifest_path = archive_dir / _ARCHIVE_MANIFEST
            manifest_path.unlink(missing_ok=True)
            if archive_dir.exists() and not any(archive_dir.iterdir()):
                archive_dir.rmdir()
        finally:
            return {"status": "error", "reason": f"Cutover failed and rollback was attempted: {exc}"}
    finally:
        _release_ledger_lock(lock_fd)

    return {
        "status": "migrated",
        "from_epoch": 1,
        "to_epoch": 2,
        "capital_cny": EPOCHS[2]["capital_cny"],
        "archive_dir": str(archive_dir),
        "cutover_timestamp": epoch_meta["cutover_timestamp"],
    }


__all__ = [
    "CURRENT_EPOCH_ID",
    "EPOCHS",
    "apply_cutover",
    "dry_run_cutover",
    "epoch_capital_cny",
    "epoch_ledger_root",
    "epoch_state_path",
    "get_current_epoch",
    "get_epoch",
    "read_epoch_state",
    "read_ledger_epoch_metadata",
]
