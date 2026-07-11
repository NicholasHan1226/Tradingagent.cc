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
    "tier_experiments_latest.json",
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
    tiers = {
        **base,
        "report_type": "ashare_tier_experiments",
        "market": "ashare",
        "account_count": 0,
        "accounts": [],
        "read_only": True,
    }
    return {
        "portfolio_evolution_latest.json": portfolio,
        "evolution_decision_latest.json": decision,
        "forward_validation_latest.json": forward,
        "sample_learning_latest.json": learning,
        "tier_experiments_latest.json": tiers,
    }


def build_epoch_reset_plan(
    review_dir: Path,
    archive_dir: Path,
    epoch_state: dict,
) -> dict[str, Any]:
    """Build a deterministic, read-only reset plan for active derived files."""

    review_path = Path(review_dir)
    archive_path = Path(archive_dir)
    try:
        bootstrap = _bootstrap_payload(epoch_state)
    except (KeyError, TypeError, ValueError) as exc:
        return {"status": "error", "reason": "invalid_epoch_state", "detail": str(exc)}

    moves: list[dict[str, Any]] = []
    missing: list[str] = []
    collisions: list[str] = []
    for name in CURRENT_DERIVED_FILES:
        source = review_path / name
        destination = archive_path / name
        if not source.exists():
            missing.append(name)
            continue
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
        "move_count": len(moves),
        "moves": moves,
        "missing_files": missing,
        "bootstrap": bootstrap,
        "latest_bootstraps": _latest_bootstraps(bootstrap),
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


def apply_epoch_reset_plan(plan: dict) -> dict[str, Any]:
    """Apply a previously built reset plan, rolling back on any failure."""

    if not isinstance(plan, dict) or plan.get("status") != "ready":
        return {"status": "error", "reason": "plan_not_ready"}
    review_dir = Path(str(plan.get("review_dir") or ""))
    archive_dir = Path(str(plan.get("archive_dir") or ""))
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
        for path in written:
            path.unlink(missing_ok=True)
        for source, destination in reversed(moved):
            if destination.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(str(destination), str(source))
        if archive_dir.exists() and not any(archive_dir.iterdir()):
            archive_dir.rmdir()
        return {
            "status": "error",
            "reason": f"epoch_review_reset_failed: {exc}",
            "rollback_attempted": True,
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
