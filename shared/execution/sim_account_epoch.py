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
import math
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


def require_authoritative_epoch_metadata(state: dict[str, Any]) -> dict[str, Any]:
    """Validate and return the three mandatory persisted epoch fields.

    Writers must not reconstruct these values from code defaults or wall-clock
    time.  An incomplete/corrupt state therefore blocks the write.
    """

    if not isinstance(state, dict):
        raise ValueError("invalid_epoch_state:not_an_object")
    if "current_epoch_id" not in state:
        raise ValueError("invalid_epoch_state:missing_current_epoch_id")
    raw_epoch = state["current_epoch_id"]
    if isinstance(raw_epoch, bool):
        raise ValueError("invalid_epoch_state:invalid_current_epoch_id")
    try:
        capital_epoch = int(raw_epoch)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_epoch_state:invalid_current_epoch_id") from exc
    if (
        capital_epoch != CURRENT_EPOCH_ID
        or capital_epoch not in EPOCHS
        or str(raw_epoch).strip() != str(capital_epoch)
    ):
        raise ValueError("invalid_epoch_state:invalid_current_epoch_id")

    if "capital_cny" not in state:
        raise ValueError("invalid_epoch_state:missing_capital_cny")
    raw_capital = state["capital_cny"]
    if isinstance(raw_capital, bool):
        raise ValueError("invalid_epoch_state:invalid_capital_cny")
    try:
        capital_cny = float(raw_capital)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_epoch_state:invalid_capital_cny") from exc
    expected_capital = float(EPOCHS[capital_epoch]["capital_cny"])
    if not math.isfinite(capital_cny) or capital_cny <= 0 or capital_cny != expected_capital:
        raise ValueError("invalid_epoch_state:invalid_capital_cny")

    if "cutover_timestamp" not in state:
        raise ValueError("invalid_epoch_state:missing_cutover_timestamp")
    cutover_timestamp = state["cutover_timestamp"]
    if not isinstance(cutover_timestamp, str) or not cutover_timestamp.strip():
        raise ValueError("invalid_epoch_state:invalid_cutover_timestamp")
    try:
        parsed_cutover = datetime.fromisoformat(cutover_timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid_epoch_state:invalid_cutover_timestamp") from exc
    if parsed_cutover.tzinfo is None:
        raise ValueError("invalid_epoch_state:invalid_cutover_timestamp")

    return {
        "capital_epoch": capital_epoch,
        "capital_cny": capital_cny,
        "cutover_timestamp": cutover_timestamp,
    }


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
    review_dir: Path | str | None = None,
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
    archive = Path(archive_root) if archive_root is not None else (
        ROOT / "shared" / "logs" / "epoch_archive"
        if ledger_path is None
        else lp.parent / "epoch_archive"
    )
    ps_path = Path(positions_snapshot_path) if positions_snapshot_path is not None else None
    tr_path = Path(tiers_root) if tiers_root is not None else None
    active_review_dir = Path(review_dir) if review_dir is not None else (
        ROOT / "shared" / "review" / "ashare"
        if ledger_path is None
        else lp.parent / "review" / "ashare"
    )

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

    from Ashare.epoch_review import build_epoch_reset_plan

    proposed_cutover = _now_iso()
    derived_review_reset = build_epoch_reset_plan(
        active_review_dir,
        archive_dir / "derived_reviews",
        {
            "current_epoch_id": CURRENT_EPOCH_ID,
            "capital_cny": EPOCHS[CURRENT_EPOCH_ID]["capital_cny"],
            "cutover_timestamp": proposed_cutover,
            "allowed_root": str(ROOT) if ledger_path is None else str(
                Path(os.path.commonpath((str(lp.resolve().parent), str(active_review_dir.resolve().parent))))
            ),
        },
    )
    if derived_review_reset.get("status") != "ready":
        return {
            "status": "error",
            "reason": "cutover_requires_review_repair",
            "derived_review_reset": derived_review_reset,
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
        "action": "reset_current_epoch_derived_reviews",
        "move_count": derived_review_reset["move_count"],
        "archive_dir": derived_review_reset["archive_dir"],
        "bootstrap_epoch": CURRENT_EPOCH_ID,
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
        "derived_review_reset": derived_review_reset,
    }


# ---------------------------------------------------------------------------
# Apply cutover
# ---------------------------------------------------------------------------


def _rollback_action(
    errors: list[dict[str, str]],
    action: str,
    path: Path,
    operation: Any,
    audit: list[dict[str, str]] | None = None,
) -> bool:
    try:
        operation()
        if audit is not None:
            audit.append({"action": action, "path": str(path), "status": "restored"})
        return True
    except Exception as exc:  # noqa: BLE001
        error = {
            "action": action,
            "path": str(path),
            "error": f"{exc.__class__.__name__}: {exc}",
        }
        errors.append(error)
        if audit is not None:
            audit.append({**error, "status": "failed"})
        return False


def _rollback_derived_review_reset(plan: dict[str, Any]) -> dict[str, Any]:
    """Restore active derived files, attempting every action independently."""

    errors: list[dict[str, str]] = []
    audit: list[dict[str, str]] = []
    latest = plan.get("latest_bootstraps") if isinstance(plan.get("latest_bootstraps"), dict) else {}
    review_dir = Path(str(plan["review_dir"]))
    archive_dir = Path(str(plan["archive_dir"]))
    for name in latest:
        path = review_dir / name
        _rollback_action(
            errors,
            "remove_review_bootstrap",
            path,
            lambda path=path: path.unlink(missing_ok=True),
            audit,
        )
    moves = plan.get("moves") if isinstance(plan.get("moves"), list) else []
    for item in reversed(moves):
        source = Path(str(item["source"]))
        destination = Path(str(item["destination"]))
        if destination.exists():
            def restore(source: Path = source, destination: Path = destination) -> None:
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(str(destination), str(source))

            _rollback_action(errors, "restore_review_file", source, restore, audit)
    def remove_archive_if_empty() -> None:
        if archive_dir.exists() and not any(archive_dir.iterdir()):
            archive_dir.rmdir()

    _rollback_action(errors, "remove_review_archive", archive_dir, remove_archive_if_empty, audit)
    return {
        "status": "blocked" if errors else "restored",
        "errors": errors,
        "audit": audit,
    }


def apply_cutover(
    *,
    ledger_path: Path | str | None = None,
    positions_snapshot_path: Path | str | None = None,
    tiers_root: Path | str | None = None,
    archive_root: Path | str | None = None,
    review_dir: Path | str | None = None,
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
    archive = Path(archive_root) if archive_root is not None else (
        ROOT / "shared" / "logs" / "epoch_archive"
        if ledger_path is None
        else lp.parent / "epoch_archive"
    )
    ps_path = Path(positions_snapshot_path) if positions_snapshot_path is not None else None
    tr_path = Path(tiers_root) if tiers_root is not None else None
    active_review_dir = Path(review_dir) if review_dir is not None else (
        ROOT / "shared" / "review" / "ashare"
        if ledger_path is None
        else lp.parent / "review" / "ashare"
    )

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
        try:
            state_authority = require_authoritative_epoch_metadata(current_state)
            ledger_authority = require_authoritative_epoch_metadata(current_metadata)
        except ValueError as exc:
            return {"status": "error", "reason": str(exc)}
        if state_authority != ledger_authority:
            return {
                "status": "error",
                "reason": "Epoch state and authoritative ledger metadata are inconsistent.",
            }
        from Ashare.epoch_review import validate_current_review_set

        review_validation = validate_current_review_set(active_review_dir, current_state)
        if review_validation.get("status") != "current":
            return {
                "status": "cutover_requires_review_repair",
                "reason": "Current epoch derived reviews are stale, missing, or not bound to authoritative epoch metadata.",
                "derived_review_reset": review_validation,
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

    from Ashare.epoch_review import build_epoch_reset_plan

    cutover_timestamp = _now_iso()
    derived_review_plan = build_epoch_reset_plan(
        active_review_dir,
        archive_dir / "derived_reviews",
        {
            "current_epoch_id": CURRENT_EPOCH_ID,
            "capital_cny": EPOCHS[CURRENT_EPOCH_ID]["capital_cny"],
            "cutover_timestamp": cutover_timestamp,
            "allowed_root": str(ROOT) if ledger_path is None else str(
                Path(os.path.commonpath((str(lp.resolve().parent), str(active_review_dir.resolve().parent))))
            ),
        },
    )
    if derived_review_plan.get("status") != "ready":
        return {
            "status": "cutover_requires_review_repair",
            "reason": "Derived-review reset plan is not safe to apply.",
            "derived_review_reset": derived_review_plan,
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
    manifest_path = archive_dir / _ARCHIVE_MANIFEST
    derived_review_reset: dict[str, Any] | None = None
    review_reset_applied = False
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
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        # --- Step 5: Bootstrap authoritative path as fresh epoch 2 ---
        epoch_meta = {
            "current_epoch_id": 2,
            "epoch_label": EPOCHS[2]["label"],
            "capital_cny": EPOCHS[2]["capital_cny"],
            "cutover_timestamp": cutover_timestamp,
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

        # --- Step 6: Invalidate and rebuild derived reviews before state advance ---
        from Ashare.epoch_review import apply_epoch_reset_plan

        derived_review_reset = apply_epoch_reset_plan(derived_review_plan)
        if derived_review_reset.get("status") != "applied":
            raise RuntimeError(
                "derived_review_reset_failed: "
                + str(derived_review_reset.get("reason") or "unknown")
            )
        review_reset_applied = True

        # --- Step 7: Persist epoch state only after derived state is current ---
        new_state = {
            "current_epoch_id": 2,
            "previous_epoch_id": current_state.get("current_epoch_id", 1),
            "cutover_timestamp": cutover_timestamp,
            "previous_state": {
                "current_epoch_id": current_state.get("current_epoch_id"),
                "activated_at": current_state.get("activated_at"),
            },
            "capital_cny": EPOCHS[2]["capital_cny"],
        }
        _write_epoch_state(new_state)

    except Exception as exc:
        rollback_errors: list[dict[str, str]] = []
        rollback_audit: list[dict[str, str]] = []
        review_rollback: dict[str, Any] | None = None
        inner_review_status = str((derived_review_reset or {}).get("status") or "")
        rollback_errors.extend((derived_review_reset or {}).get("rollback_errors") or [])
        rollback_audit.extend((derived_review_reset or {}).get("rollback_audit") or [])
        if review_reset_applied:
            try:
                review_rollback = _rollback_derived_review_reset(derived_review_plan)
                rollback_errors.extend(review_rollback.get("errors") or [])
                rollback_audit.extend(review_rollback.get("audit") or [])
            except Exception as rollback_exc:  # noqa: BLE001
                rollback_errors.append(
                    {
                        "action": "rollback_derived_reviews",
                        "path": str(derived_review_plan.get("review_dir") or ""),
                        "error": f"{rollback_exc.__class__.__name__}: {rollback_exc}",
                    }
                )
                rollback_audit.append({**rollback_errors[-1], "status": "failed"})

        if moved_tiers and tier_dest is not None and tier_dest.exists() and tr_path is not None:
            def restore_tiers() -> None:
                tr_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(str(tier_dest), str(tr_path))

            _rollback_action(rollback_errors, "restore_tiers", tr_path, restore_tiers, rollback_audit)

        positions_restored = False
        if archived_positions is not None and archived_positions.exists() and ps_path is not None:
            def restore_positions() -> None:
                shutil.copy2(str(archived_positions), str(ps_path))

            positions_restored = _rollback_action(
                rollback_errors,
                "restore_positions_snapshot",
                ps_path,
                restore_positions,
                rollback_audit,
            )

        current_entries: list[Path] = []
        if lp.exists():
            try:
                current_entries = list(lp.iterdir())
            except Exception as list_exc:  # noqa: BLE001
                rollback_errors.append(
                    {
                        "action": "inspect_current_ledger",
                        "path": str(lp),
                        "error": f"{list_exc.__class__.__name__}: {list_exc}",
                    }
                )
                rollback_audit.append({**rollback_errors[-1], "status": "failed"})
            for current_entry in current_entries:
                if current_entry.name == ".local_sim.lock":
                    continue

                def remove_current(current_entry: Path = current_entry) -> None:
                    if current_entry.is_dir():
                        shutil.rmtree(str(current_entry))
                    else:
                        current_entry.unlink(missing_ok=True)

                _rollback_action(
                    rollback_errors,
                    "remove_current_ledger_bootstrap",
                    current_entry,
                    remove_current,
                    rollback_audit,
                )

        for source, destination in reversed(moved_ledger_entries):
            if destination.exists():
                def restore_ledger(source: Path = source, destination: Path = destination) -> None:
                    source.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(str(destination), str(source))

                _rollback_action(
                    rollback_errors,
                    "restore_ledger_entry",
                    source,
                    restore_ledger,
                    rollback_audit,
                )

        if positions_restored and archived_positions is not None and archived_positions.exists():
            _rollback_action(
                rollback_errors,
                "remove_archived_positions_copy",
                archived_positions,
                archived_positions.unlink,
                rollback_audit,
            )

        _rollback_action(
            rollback_errors,
            "remove_archive_manifest",
            manifest_path,
            lambda: manifest_path.unlink(missing_ok=True),
            rollback_audit,
        )
        def remove_epoch_archive_if_empty() -> None:
            if archive_dir.exists() and not any(archive_dir.iterdir()):
                archive_dir.rmdir()

        _rollback_action(
            rollback_errors,
            "remove_empty_epoch_archive",
            archive_dir,
            remove_epoch_archive_if_empty,
            rollback_audit,
        )

        if inner_review_status == "blocked" or rollback_errors:
            status = "blocked"
        elif "derived_review_reset_failed" in str(exc):
            status = "cutover_requires_review_repair"
        else:
            status = "error"
        return {
            "status": status,
            "reason": f"Cutover failed and rollback was attempted: {exc}",
            "derived_review_reset": derived_review_reset,
            "review_rollback": review_rollback,
            "rollback_errors": rollback_errors,
            "rollback_audit": rollback_audit,
        }
    finally:
        _release_ledger_lock(lock_fd)

    return {
        "status": "migrated",
        "from_epoch": 1,
        "to_epoch": 2,
        "capital_cny": EPOCHS[2]["capital_cny"],
        "archive_dir": str(archive_dir),
        "cutover_timestamp": epoch_meta["cutover_timestamp"],
        "derived_review_reset": derived_review_reset,
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
    "require_authoritative_epoch_metadata",
]
