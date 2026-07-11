#!/usr/bin/env python3
"""Capital-epoch isolation for A-share derived review state.

Current review files are disposable projections of the immutable trade ledger.
At a capital cutover they must move into the old epoch archive as a unit, then
the active review directory is bootstrapped with empty current-epoch snapshots.
"""

from __future__ import annotations

import hashlib
import json
import os
from os.path import commonpath
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CURRENT_DERIVED_FILES = (
    "portfolio_evolution_latest.json",
    "portfolio_evolution_log.jsonl",
    "evolution_decision_latest.json",
    "evolution_decision_log.jsonl",
    "forward_validation_latest.json",
    "forward_validation.jsonl",
    "sample_learning_latest.json",
    "sample_learning_log.jsonl",
    "sample_target_monitor_latest.json",
    "sample_target_monitor_log.jsonl",
    "tier_experiments_latest.json",
    "formal_close_latest.json",
    "formal_close_history.jsonl",
)

_LATEST_FILES = frozenset(name for name in CURRENT_DERIVED_FILES if name.endswith("_latest.json"))


def _parse_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_review_epoch(
    payload: dict,
    *,
    current_epoch_id: int,
    current_cutover_timestamp: str,
) -> tuple[bool, str]:
    """Fail closed unless a review belongs to the current capital epoch."""

    if not isinstance(payload, dict):
        return False, "invalid_review_payload"
    if "capital_epoch" not in payload:
        return False, "missing_capital_epoch"
    try:
        payload_epoch = int(payload["capital_epoch"])
    except (TypeError, ValueError):
        return False, "invalid_capital_epoch"
    if payload_epoch != int(current_epoch_id):
        return False, "capital_epoch_mismatch"

    cutover = _parse_timestamp(current_cutover_timestamp)
    if cutover is None:
        return False, "invalid_epoch_cutover_timestamp"
    generated = _parse_timestamp(
        payload.get("generated_at") or payload.get("epoch_cutover_timestamp")
    )
    if generated is None:
        return False, "missing_or_invalid_generated_at"
    if generated < cutover:
        return False, "generated_before_epoch_cutover"
    return True, "current_epoch"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_epoch_tagged(path: Path) -> bool:
    try:
        if path.suffix == ".jsonl":
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                payload = json.loads(line)
                if isinstance(payload, dict) and "capital_epoch" in payload:
                    return True
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(payload, dict) and "capital_epoch" in payload
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False


def _bootstrap_payload(epoch_state: dict[str, Any]) -> dict[str, Any]:
    epoch_id = int(epoch_state["current_epoch_id"])
    capital = float(epoch_state["capital_cny"])
    cutover = str(epoch_state["cutover_timestamp"])
    return {
        "generated_at": cutover,
        "epoch_cutover_timestamp": cutover,
        "capital_epoch": epoch_id,
        "capital_cny": capital,
        "strategy_sample_count": 0,
        "today_strategy_sample_count": 0,
        "evolution_evidence": {
            "eligible_sample_count": 0,
            "realized_round_trip_count": 0,
            "forward_label_count": 0,
            "blockers": ["current_epoch_has_no_verified_samples"],
        },
        "pnl": {
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "total_pnl": 0.0,
            "cash": capital,
            "market_value": 0.0,
            "equity": capital,
        },
        "real_trading_enabled": False,
    }


def _latest_bootstraps(base: dict[str, Any]) -> dict[str, dict[str, Any]]:
    portfolio = {
        **base,
        "market": "ashare",
        "state": "waiting",
        "rankings": [],
        "tier_experiments": {"account_count": 0, "accounts": [], "capital_plans": {}},
        "read_only": True,
    }
    decision = {
        **base,
        "report_type": "ashare_evolution_decision",
        "market": "ashare",
        "state": "evidence_pending",
        "recommended_action": "observe_and_label_candidates",
        "reasons": ["current_epoch_has_no_verified_samples"],
        "read_only_decision": True,
    }
    forward = {
        **base,
        "report_type": "ashare_forward_validation",
        "market": "ashare",
        "trade_count": 0,
        "strategy_label_count": 0,
        "pending_count": 0,
        "labels": [],
        "read_only": True,
    }
    learning = {
        **base,
        "report_type": "ashare_sample_learning",
        "market": "ashare",
        "sample_count": 0,
        "samples": [],
        "read_only": True,
    }
    monitor = {
        **base,
        "report_type": "ashare_sample_target_monitor",
        "market": "ashare",
        "overall_status": "pass",
        "state": "observation_gap",
        "recommended_action": "observe_and_label_candidates",
        "reasons": ["current_epoch_has_no_verified_samples"],
        "blockers": ["current_epoch_has_no_verified_samples"],
        "daily_target": {
            "target": 0,
            "today_strategy_sample_count": 0,
            "strategy_sample_count": 0,
            "target_met": False,
        },
        "read_only": True,
        "writes_orders": False,
    }
    tiers = {
        **base,
        "report_type": "ashare_tier_experiments",
        "market": "ashare",
        "account_count": 0,
        "accounts": [],
        "read_only": True,
    }
    formal_close = {
        **base,
        "report_type": "ashare_formal_close_refresh",
        "schema_version": 2,
        "market": "ashare",
        "status": "pass",
        "reason": "no_open_positions",
        "read_only": True,
    }
    return {
        "portfolio_evolution_latest.json": portfolio,
        "evolution_decision_latest.json": decision,
        "forward_validation_latest.json": forward,
        "sample_learning_latest.json": learning,
        "sample_target_monitor_latest.json": monitor,
        "tier_experiments_latest.json": tiers,
        "formal_close_latest.json": formal_close,
    }


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _contains_symlink(path: Path, allowed_root: Path) -> bool:
    current = path.absolute()
    stop = allowed_root.absolute()
    while _is_within(current, stop):
        if current.is_symlink():
            return True
        if current == stop:
            break
        current = current.parent
    return False


def _safe_roots(
    review_dir: Path,
    archive_dir: Path,
    epoch_state: dict[str, Any],
) -> tuple[Path, Path, Path] | tuple[None, None, None]:
    review_raw = Path(review_dir).absolute()
    archive_raw = Path(archive_dir).absolute()
    explicit_root = epoch_state.get("allowed_root")
    if explicit_root:
        allowed_root = Path(str(explicit_root)).resolve(strict=True)
    else:
        review_resolved = review_raw.resolve(strict=False)
        archive_resolved = archive_raw.resolve(strict=False)
        allowed_root = Path(commonpath((str(review_resolved), str(archive_resolved)))).resolve()
    if allowed_root == Path(allowed_root.anchor):
        return None, None, None
    review_resolved = review_raw.resolve(strict=False)
    archive_resolved = archive_raw.resolve(strict=False)
    if not _is_within(review_resolved, allowed_root) or not _is_within(archive_resolved, allowed_root):
        return None, None, None
    if _contains_symlink(review_raw, allowed_root) or _contains_symlink(archive_raw, allowed_root):
        return None, None, None
    return review_resolved, archive_resolved, allowed_root


def _roots_already_bootstrapped(
    review_dir: Path,
    archive_dir: Path,
    latest_bootstraps: dict[str, dict[str, Any]],
) -> bool:
    if not archive_dir.is_dir():
        return False
    for name, expected in latest_bootstraps.items():
        if (review_dir / name).is_symlink():
            return False
        try:
            current = json.loads((review_dir / name).read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return False
        if current != expected:
            return False
    for name in CURRENT_DERIVED_FILES:
        if name not in latest_bootstraps and (review_dir / name).exists():
            return False
    return True


def build_epoch_reset_plan(
    review_dir: Path,
    archive_dir: Path,
    epoch_state: dict,
) -> dict[str, Any]:
    """Build a deterministic, read-only reset plan for active derived files."""

    try:
        bootstrap = _bootstrap_payload(epoch_state)
        safe = _safe_roots(Path(review_dir), Path(archive_dir), epoch_state)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        return {"status": "error", "reason": "invalid_epoch_state", "detail": str(exc)}
    review_path, archive_path, allowed_root = safe
    if review_path is None or archive_path is None or allowed_root is None:
        return {"status": "error", "reason": "unsafe_path"}
    latest_bootstraps = _latest_bootstraps(bootstrap)
    if _roots_already_bootstrapped(review_path, archive_path, latest_bootstraps):
        return {
            "status": "already_applied",
            "review_dir": str(review_path),
            "archive_dir": str(archive_path),
            "allowed_root": str(allowed_root),
            "move_count": 0,
            "moves": [],
            "missing_files": [],
            "bootstrap": bootstrap,
            "latest_bootstraps": latest_bootstraps,
        }

    moves: list[dict[str, Any]] = []
    missing: list[str] = []
    collisions: list[str] = []
    for name in CURRENT_DERIVED_FILES:
        source = review_path / name
        destination = archive_path / name
        if not source.exists():
            missing.append(name)
            continue
        if source.is_symlink() or _contains_symlink(source, allowed_root):
            return {
                "status": "error",
                "reason": "unsafe_path",
                "path": str(source),
            }
        if destination.is_symlink() or _contains_symlink(destination, allowed_root):
            return {
                "status": "error",
                "reason": "unsafe_path",
                "path": str(destination),
            }
        if not source.is_file():
            return {
                "status": "error",
                "reason": "source_not_regular_file",
                "path": str(source),
            }
        if destination.exists():
            collisions.append(str(destination))
            continue
        moves.append(
            {
                "name": name,
                "source": str(source),
                "destination": str(destination),
                "sha256": _sha256(source),
                "size": source.stat().st_size,
                "epoch_tagged": _is_epoch_tagged(source),
            }
        )
    if collisions:
        return {
            "status": "error",
            "reason": "destination_collision",
            "collisions": collisions,
            "review_dir": str(review_path),
            "archive_dir": str(archive_path),
        }
    return {
        "status": "ready",
        "review_dir": str(review_path),
        "archive_dir": str(archive_path),
        "allowed_root": str(allowed_root),
        "move_count": len(moves),
        "moves": moves,
        "missing_files": missing,
        "bootstrap": bootstrap,
        "latest_bootstraps": latest_bootstraps,
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(str(tmp), str(path))
    finally:
        tmp.unlink(missing_ok=True)


def _plan_already_applied(plan: dict[str, Any], allowed_root: Path) -> bool:
    latest = plan.get("latest_bootstraps") if isinstance(plan.get("latest_bootstraps"), dict) else {}
    review_dir = Path(str(plan.get("review_dir") or ""))
    moves = plan.get("moves") if isinstance(plan.get("moves"), list) else []
    for item in moves:
        destination = Path(str(item.get("destination") or ""))
        if destination.is_symlink() or _contains_symlink(destination, allowed_root):
            return False
        if not destination.is_file():
            return False
        if destination.stat().st_size != int(item.get("size") or -1):
            return False
        if _sha256(destination) != str(item.get("sha256") or ""):
            return False
    for name, payload in latest.items():
        path = review_dir / name
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return False
        if current != payload:
            return False
    return bool(latest)


def _record_rollback_error(
    errors: list[dict[str, str]],
    action: str,
    path: Path,
    operation: Any,
) -> None:
    try:
        operation()
    except Exception as exc:  # noqa: BLE001
        errors.append(
            {
                "action": action,
                "path": str(path),
                "error": f"{exc.__class__.__name__}: {exc}",
            }
        )


def apply_epoch_reset_plan(plan: dict) -> dict[str, Any]:
    """Apply a previously built reset plan, rolling back on any failure."""

    if not isinstance(plan, dict) or plan.get("status") not in {"ready", "already_applied"}:
        return {"status": "error", "reason": "plan_not_ready"}
    review_dir = Path(str(plan.get("review_dir") or ""))
    archive_dir = Path(str(plan.get("archive_dir") or ""))
    try:
        allowed_root = Path(str(plan.get("allowed_root") or "")).resolve(strict=True)
    except OSError as exc:
        return {"status": "error", "reason": "unsafe_path", "detail": str(exc)}
    if (
        _contains_symlink(review_dir, allowed_root)
        or _contains_symlink(archive_dir, allowed_root)
        or not _is_within(review_dir.resolve(strict=False), allowed_root)
        or not _is_within(archive_dir.resolve(strict=False), allowed_root)
    ):
        return {"status": "error", "reason": "unsafe_path"}
    if plan.get("status") == "already_applied" or _plan_already_applied(plan, allowed_root):
        return {
            "status": "already_applied",
            "move_count": int(plan.get("move_count") or 0),
            "archive_dir": str(plan.get("archive_dir") or ""),
        }
    moves = plan.get("moves") if isinstance(plan.get("moves"), list) else []
    latest_bootstraps = (
        plan.get("latest_bootstraps")
        if isinstance(plan.get("latest_bootstraps"), dict)
        else {}
    )
    if set(latest_bootstraps) != _LATEST_FILES:
        return {"status": "error", "reason": "invalid_latest_bootstrap_set"}

    moved: list[tuple[Path, Path]] = []
    written: list[Path] = []
    try:
        for item in moves:
            source = Path(str(item["source"]))
            destination = Path(str(item["destination"]))
            if (
                source.is_symlink()
                or destination.is_symlink()
                or _contains_symlink(source, allowed_root)
                or _contains_symlink(destination, allowed_root)
                or not _is_within(source.resolve(strict=False), allowed_root)
                or not _is_within(destination.resolve(strict=False), allowed_root)
            ):
                raise RuntimeError(f"unsafe_path: {source} -> {destination}")
            if destination.exists():
                raise FileExistsError(f"destination_collision: {destination}")
            if not source.is_file():
                raise FileNotFoundError(f"planned_source_missing: {source}")
            if source.stat().st_size != int(item["size"]) or _sha256(source) != item["sha256"]:
                raise RuntimeError(f"planned_source_changed: {source}")

        review_dir.mkdir(parents=True, exist_ok=True)
        archive_dir.mkdir(parents=True, exist_ok=False)
        for item in moves:
            source = Path(str(item["source"]))
            destination = Path(str(item["destination"]))
            os.replace(str(source), str(destination))
            moved.append((source, destination))
        for name in sorted(_LATEST_FILES):
            destination = review_dir / name
            _atomic_write_json(destination, latest_bootstraps[name])
            written.append(destination)
    except Exception as exc:  # noqa: BLE001
        rollback_errors: list[dict[str, str]] = []
        for path in written:
            _record_rollback_error(
                rollback_errors,
                "remove_bootstrap",
                path,
                lambda path=path: path.unlink(missing_ok=True),
            )
        for source, destination in reversed(moved):
            if destination.exists():
                def restore(source: Path = source, destination: Path = destination) -> None:
                    source.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(str(destination), str(source))

                _record_rollback_error(
                    rollback_errors,
                    "restore_review_file",
                    source,
                    restore,
                )
        def remove_archive_if_empty() -> None:
            if archive_dir.exists() and not any(archive_dir.iterdir()):
                archive_dir.rmdir()

        _record_rollback_error(
            rollback_errors,
            "remove_empty_archive_dir",
            archive_dir,
            remove_archive_if_empty,
        )
        return {
            "status": "blocked" if rollback_errors else "error",
            "reason": f"epoch_review_reset_failed: {exc}",
            "rollback_attempted": True,
            "rollback_errors": rollback_errors,
        }
    return {
        "status": "applied",
        "move_count": len(moved),
        "archive_dir": str(archive_dir),
        "bootstrapped_latest_files": sorted(_LATEST_FILES),
    }


__all__ = [
    "CURRENT_DERIVED_FILES",
    "apply_epoch_reset_plan",
    "build_epoch_reset_plan",
    "validate_review_epoch",
]
