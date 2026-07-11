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


def validate_review_authority(
    payload: dict[str, Any],
    authority: dict[str, Any],
) -> tuple[bool, str]:
    """Require the exact persisted epoch/capital/cutover authority triple."""

    if not isinstance(payload, dict):
        return False, "invalid_review_payload"
    expected_epoch = authority.get("capital_epoch", authority.get("current_epoch_id"))
    expected_capital = authority.get("capital_cny")
    expected_cutover = authority.get(
        "epoch_cutover_timestamp", authority.get("cutover_timestamp")
    )
    if isinstance(expected_epoch, bool) or not isinstance(expected_epoch, int):
        return False, "invalid_epoch_authority"
    if isinstance(expected_capital, bool) or not isinstance(expected_capital, (int, float)):
        return False, "invalid_epoch_authority"
    if not isinstance(expected_cutover, str) or not expected_cutover:
        return False, "invalid_epoch_authority"

    raw_epoch = payload.get("capital_epoch")
    if raw_epoch is None:
        return False, "missing_capital_epoch"
    if isinstance(raw_epoch, bool) or not isinstance(raw_epoch, int):
        return False, "invalid_capital_epoch"
    if raw_epoch != expected_epoch:
        return False, "capital_epoch_mismatch"

    raw_capital = payload.get("capital_cny")
    if raw_capital is None:
        return False, "missing_capital_cny"
    if isinstance(raw_capital, bool) or not isinstance(raw_capital, (int, float)):
        return False, "invalid_capital_cny"
    if float(raw_capital) != float(expected_capital):
        return False, "capital_cny_mismatch"

    raw_cutover = payload.get("epoch_cutover_timestamp")
    if raw_cutover is None:
        return False, "missing_epoch_cutover_timestamp"
    if not isinstance(raw_cutover, str):
        return False, "invalid_epoch_cutover_timestamp"
    # Exact string comparison is intentional: timezone-equivalent aliases are
    # not the persisted authority value from the cutover plan.
    if raw_cutover != expected_cutover:
        return False, "epoch_cutover_timestamp_mismatch"
    return True, "current_authority"


def _path_or_ancestor_is_symlink(path: Path) -> bool:
    current = path.absolute()
    # Darwin exposes these fixed system roots as aliases into /private.  They
    # are outside the operator-controlled review tree and are not writable
    # review/archive indirections.
    system_root_aliases = {Path("/etc"), Path("/tmp"), Path("/var")}
    while True:
        if current.is_symlink() and current not in system_root_aliases:
            return True
        if current == current.parent:
            return False
        current = current.parent


def _review_tree_has_symlink(review_dir: Path) -> bool:
    if _path_or_ancestor_is_symlink(review_dir):
        return True
    return any((review_dir / name).is_symlink() for name in CURRENT_DERIVED_FILES)


def validate_current_review_set(review_dir: Path, epoch_state: dict[str, Any]) -> dict[str, Any]:
    """Require every Task-3 projection file to carry exact current authority."""
    try:
        from shared.execution.sim_account_epoch import require_authoritative_epoch_metadata

        authority = require_authoritative_epoch_metadata(epoch_state)
    except (TypeError, ValueError) as exc:
        return {"status": "error", "reason": "invalid_epoch_state", "detail": str(exc)}
    root = Path(review_dir)
    if _review_tree_has_symlink(root):
        return {"status": "error", "reason": "unsafe_path"}
    stale_or_missing: list[dict[str, str]] = []
    for name in CURRENT_DERIVED_FILES:
        path = root / name
        if path.is_symlink():
            stale_or_missing.append({"file": name, "reason": "unsafe_path"})
            continue
        if not path.exists():
            stale_or_missing.append({"file": name, "reason": "missing_current_review"})
            continue
        try:
            payloads = (
                [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
                if path.suffix == ".jsonl"
                else [json.loads(path.read_text(encoding="utf-8"))]
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            stale_or_missing.append({"file": name, "reason": "invalid_review_payload"})
            continue
        if not payloads:
            stale_or_missing.append({"file": name, "reason": "missing_current_review"})
            continue
        for payload in payloads:
            valid, reason = validate_review_epoch(
                payload,
                current_epoch_id=int(authority["capital_epoch"]),
                current_cutover_timestamp=str(authority["cutover_timestamp"]),
            )
            if not valid:
                stale_or_missing.append({"file": name, "reason": reason})
                break
            exact, exact_reason = validate_review_authority(payload, authority)
            if not exact:
                stale_or_missing.append({"file": name, "reason": exact_reason})
                break
    if stale_or_missing:
        return {"status": "stale_or_missing", "issues": stale_or_missing}
    return {
        "status": "current",
        "checked_latest_count": len(_LATEST_FILES),
        "checked_file_count": len(CURRENT_DERIVED_FILES),
    }


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
    # Local import avoids the cutover module's deliberate lazy import of this
    # reset planner while keeping every derived writer on one validation path.
    from shared.execution.sim_account_epoch import require_authoritative_epoch_metadata

    metadata = require_authoritative_epoch_metadata(epoch_state)
    epoch_id = int(metadata["capital_epoch"])
    capital = float(metadata["capital_cny"])
    cutover = str(metadata["cutover_timestamp"])
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


def _log_bootstraps(
    latest_bootstraps: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        "portfolio_evolution_log.jsonl": latest_bootstraps["portfolio_evolution_latest.json"],
        "evolution_decision_log.jsonl": latest_bootstraps["evolution_decision_latest.json"],
        "forward_validation.jsonl": latest_bootstraps["forward_validation_latest.json"],
        "sample_learning_log.jsonl": latest_bootstraps["sample_learning_latest.json"],
        "sample_target_monitor_log.jsonl": latest_bootstraps["sample_target_monitor_latest.json"],
        "formal_close_history.jsonl": latest_bootstraps["formal_close_latest.json"],
    }


_PLAN_DIGEST_FIELDS = (
    "status",
    "review_dir",
    "archive_dir",
    "allowed_root",
    "move_count",
    "moves",
    "missing_files",
    "bootstrap",
    "latest_bootstraps",
    "log_bootstraps",
    "authority_metadata",
)


def _plan_digest(plan: dict[str, Any]) -> str:
    encoded = json.dumps(
        {field: plan.get(field) for field in _PLAN_DIGEST_FIELDS},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
    if _path_or_ancestor_is_symlink(review_raw) or _path_or_ancestor_is_symlink(archive_raw):
        return None, None, None
    explicit_root = epoch_state.get("allowed_root")
    if explicit_root:
        raw_root = Path(str(explicit_root))
        if raw_root.is_symlink() or _path_or_ancestor_is_symlink(raw_root):
            return None, None, None
        allowed_root = raw_root.resolve(strict=True)
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
    log_bootstraps: dict[str, dict[str, Any]],
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
    for name, expected in log_bootstraps.items():
        if (review_dir / name).is_symlink():
            return False
        try:
            rows = [
                json.loads(line)
                for line in (review_dir / name).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return False
        if rows != [expected]:
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
    log_bootstraps = _log_bootstraps(latest_bootstraps)
    authority_metadata = {
        "current_epoch_id": bootstrap["capital_epoch"],
        "capital_cny": bootstrap["capital_cny"],
        "cutover_timestamp": bootstrap["epoch_cutover_timestamp"],
    }
    for root in (review_path, archive_path):
        if _review_tree_has_symlink(root):
            return {"status": "error", "reason": "unsafe_path", "path": str(root)}
    if _roots_already_bootstrapped(review_path, archive_path, latest_bootstraps, log_bootstraps):
        result = {
            "status": "already_applied",
            "review_dir": str(review_path),
            "archive_dir": str(archive_path),
            "allowed_root": str(allowed_root),
            "move_count": 0,
            "moves": [],
            "missing_files": [],
            "bootstrap": bootstrap,
            "latest_bootstraps": latest_bootstraps,
            "log_bootstraps": log_bootstraps,
            "authority_metadata": authority_metadata,
        }
        result["plan_digest"] = _plan_digest(result)
        return result

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
    result = {
        "status": "ready",
        "review_dir": str(review_path),
        "archive_dir": str(archive_path),
        "allowed_root": str(allowed_root),
        "move_count": len(moves),
        "moves": moves,
        "missing_files": missing,
        "bootstrap": bootstrap,
        "latest_bootstraps": latest_bootstraps,
        "log_bootstraps": log_bootstraps,
        "authority_metadata": authority_metadata,
    }
    result["plan_digest"] = _plan_digest(result)
    return result


class _PlanStaleError(RuntimeError):
    pass


def _atomic_write(path: Path, content: str, *, exclusive: bool = False) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        if exclusive:
            try:
                os.link(str(tmp), str(path))
            except FileExistsError as exc:
                raise _PlanStaleError(f"late_created_path: {path}") from exc
        else:
            os.replace(str(tmp), str(path))
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_write_json(
    path: Path, payload: dict[str, Any], *, exclusive: bool = False
) -> None:
    _atomic_write(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        exclusive=exclusive,
    )


def _atomic_write_jsonl(
    path: Path, payload: dict[str, Any], *, exclusive: bool = False
) -> None:
    _atomic_write(
        path,
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        exclusive=exclusive,
    )


def _plan_already_applied(plan: dict[str, Any], allowed_root: Path) -> bool:
    latest = plan.get("latest_bootstraps") if isinstance(plan.get("latest_bootstraps"), dict) else {}
    logs = plan.get("log_bootstraps") if isinstance(plan.get("log_bootstraps"), dict) else {}
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
        if path.is_symlink() or _contains_symlink(path, allowed_root):
            return False
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return False
        if current != payload:
            return False
    for name, payload in logs.items():
        path = review_dir / name
        if path.is_symlink() or _contains_symlink(path, allowed_root):
            return False
        try:
            rows = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return False
        if rows != [payload]:
            return False
    return bool(latest)


def _record_rollback_error(
    errors: list[dict[str, str]],
    audit: list[dict[str, str]],
    action: str,
    path: Path,
    operation: Any,
) -> None:
    try:
        operation()
        audit.append({"action": action, "path": str(path), "status": "restored"})
    except Exception as exc:  # noqa: BLE001
        error = {
            "action": action,
            "path": str(path),
            "error": f"{exc.__class__.__name__}: {exc}",
        }
        errors.append(error)
        audit.append({**error, "status": "failed"})


def apply_epoch_reset_plan(plan: dict) -> dict[str, Any]:
    """Apply a previously built reset plan, rolling back on any failure."""

    if not isinstance(plan, dict) or plan.get("status") not in {"ready", "already_applied"}:
        return {"status": "error", "reason": "plan_not_ready"}
    if str(plan.get("plan_digest") or "") != _plan_digest(plan):
        return {"status": "error", "reason": "invalid_plan_digest"}
    try:
        authority = plan["authority_metadata"]
        bootstrap = plan["bootstrap"]
        latest = plan["latest_bootstraps"]
        from shared.execution.sim_account_epoch import require_authoritative_epoch_metadata

        validated = require_authoritative_epoch_metadata(authority)
        expected_bootstrap = _bootstrap_payload(authority)
        expected_latest = _latest_bootstraps(expected_bootstrap)
        if (
            bootstrap != expected_bootstrap
            or latest != expected_latest
            or int(validated["capital_epoch"]) != int(bootstrap.get("capital_epoch", -1))
        ):
            raise ValueError("plan authority mismatch")
    except (KeyError, TypeError, ValueError):
        return {"status": "error", "reason": "invalid_plan_authority"}
    review_dir = Path(str(plan.get("review_dir") or ""))
    archive_dir = Path(str(plan.get("archive_dir") or ""))
    try:
        raw_allowed_root = Path(str(plan.get("allowed_root") or ""))
        if raw_allowed_root.is_symlink() or _path_or_ancestor_is_symlink(raw_allowed_root):
            return {"status": "error", "reason": "unsafe_path"}
        allowed_root = raw_allowed_root.resolve(strict=True)
    except OSError as exc:
        return {"status": "error", "reason": "unsafe_path", "detail": str(exc)}
    if (
        _contains_symlink(review_dir, allowed_root)
        or _contains_symlink(archive_dir, allowed_root)
        or not _is_within(review_dir.resolve(strict=False), allowed_root)
        or not _is_within(archive_dir.resolve(strict=False), allowed_root)
    ):
        return {"status": "error", "reason": "unsafe_path"}
    if _review_tree_has_symlink(review_dir) or _review_tree_has_symlink(archive_dir):
        return {"status": "error", "reason": "unsafe_path"}
    if _plan_already_applied(plan, allowed_root):
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
    log_bootstraps = (
        plan.get("log_bootstraps") if isinstance(plan.get("log_bootstraps"), dict) else {}
    )
    if set(latest_bootstraps) != _LATEST_FILES:
        return {"status": "error", "reason": "invalid_latest_bootstrap_set"}
    if set(log_bootstraps) != set(CURRENT_DERIVED_FILES) - _LATEST_FILES:
        return {"status": "error", "reason": "invalid_log_bootstrap_set"}

    missing_files = plan.get("missing_files")
    if not isinstance(missing_files, list) or any(
        not isinstance(name, str) or name not in CURRENT_DERIVED_FILES
        for name in missing_files
    ):
        return {"status": "error", "reason": "invalid_missing_file_set"}
    late_created = [
        review_dir / name
        for name in missing_files
        if os.path.lexists(review_dir / name)
    ]
    if late_created:
        return {
            "status": "blocked",
            "reason": "plan_stale",
            "late_created_paths": [str(path) for path in late_created],
        }

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
            _atomic_write_json(destination, latest_bootstraps[name], exclusive=True)
            written.append(destination)
        for name in sorted(log_bootstraps):
            destination = review_dir / name
            _atomic_write_jsonl(destination, log_bootstraps[name], exclusive=True)
            written.append(destination)
    except Exception as exc:  # noqa: BLE001
        rollback_errors: list[dict[str, str]] = []
        rollback_audit: list[dict[str, str]] = []
        for path in written:
            _record_rollback_error(
                rollback_errors,
                rollback_audit,
                "remove_bootstrap",
                path,
                lambda path=path: path.unlink(missing_ok=True),
            )
        for source, destination in reversed(moved):
            if destination.exists():
                def restore(source: Path = source, destination: Path = destination) -> None:
                    if os.path.lexists(source):
                        raise FileExistsError(f"rollback_source_recreated: {source}")
                    source.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(str(destination), str(source))

                _record_rollback_error(
                    rollback_errors,
                    rollback_audit,
                    "restore_review_file",
                    source,
                    restore,
                )
        def remove_archive_if_empty() -> None:
            if archive_dir.exists() and not any(archive_dir.iterdir()):
                archive_dir.rmdir()

        _record_rollback_error(
            rollback_errors,
            rollback_audit,
            "remove_empty_archive_dir",
            archive_dir,
            remove_archive_if_empty,
        )
        result = {
            "status": "blocked" if rollback_errors else "error",
            "reason": f"epoch_review_reset_failed: {exc}",
            "rollback_attempted": True,
            "rollback_errors": rollback_errors,
            "rollback_audit": rollback_audit,
        }
        if isinstance(exc, _PlanStaleError):
            result["status"] = "blocked"
            result["reason"] = "plan_stale"
        return result
    return {
        "status": "applied",
        "move_count": len(moved),
        "archive_dir": str(archive_dir),
        "bootstrapped_latest_files": sorted(_LATEST_FILES),
        "bootstrapped_log_files": sorted(log_bootstraps),
    }


__all__ = [
    "CURRENT_DERIVED_FILES",
    "apply_epoch_reset_plan",
    "build_epoch_reset_plan",
    "validate_review_authority",
    "validate_review_epoch",
    "validate_current_review_set",
]
