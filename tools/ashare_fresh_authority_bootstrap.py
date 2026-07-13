#!/usr/bin/env python3
"""Build isolated, fixed-lineage A-share simulated authorities.

This operator CLI is deliberately staging-only.  It validates an existing
operator opening manifest, changes only its execution lineage to the canonical
A-share lineage, then creates a new capital ledger and a zero-import execution
ledger under explicitly supplied non-production roots.  Activating either root
is a separate audited filesystem cutover.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.capital.market_ledger import (  # noqa: E402
    MarketCapitalLedger,
    MarketCapitalLedgerError,
    _is_default_production_root,
)
from shared.capital.market_policy import (  # noqa: E402
    PINNED_CUTOVER_DECISION_ID,
    PINNED_SOURCE_THREAD_ID,
    REQUIRED_CUTOVER_STATE,
    MarketPolicy,
)
from shared.execution.execution_lineage import (  # noqa: E402
    ASHARE_EXECUTION_LINEAGE_ID,
)
from shared.execution.local_sim_ledger import (  # noqa: E402
    LOCAL_SIM_DIR,
    bootstrap_fresh_local_sim,
    get_local_sim_execution_lineage_manifest,
)
from tools.market_capital_ops import _opening_manifest_from_payload  # noqa: E402


BLOCKED = 2
TRUTHY = {"1", "true", "yes", "y", "on", "enabled", "enable"}


def _emit(payload: Mapping[str, Any]) -> None:
    print(json.dumps(dict(payload), ensure_ascii=False, sort_keys=True))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_object(path: Path, *, blocker: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(blocker) from exc
    if not isinstance(payload, dict):
        raise ValueError(blocker)
    return payload


def _aware_timestamp(raw: str, *, field: str) -> datetime:
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field}_invalid") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field}_timezone_required")
    return value


def _validate_zero_state(manifest: Any) -> None:
    checks = {
        "market": manifest.market == "ashare",
        "authority": manifest.authority_id == "ashare-capital-v1",
        "decision": manifest.cutover_decision_id == PINNED_CUTOVER_DECISION_ID,
        "mode": manifest.mode == "fresh_start",
        "cash": float(manifest.cash_balance_cny) == 50_000.0,
        "equity": float(manifest.opening_equity_cny) == 50_000.0,
        "reservations": float(manifest.active_reservations_cny) == 0.0,
        "losses": manifest.consecutive_losses == 0,
        "high_water": float(manifest.inherited_high_water_equity_cny) == 0.0,
        "positions": manifest.positions_by_risk_unit == {},
        "margin": manifest.position_margin_by_risk_unit == {},
        "frozen_cash": float(manifest.frozen_order_cash_cny) == 0.0,
        "realized_pnl": float(manifest.realized_pnl_cny) == 0.0,
        "unrealized_pnl": float(manifest.unrealized_pnl_cny) == 0.0,
        "real": manifest.real is False,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise ValueError(f"opening_manifest_not_zero_import:{','.join(failed)}")


def _validate_source(manifest: Any) -> None:
    source = Path(manifest.source).expanduser()
    if not source.is_file():
        raise ValueError("opening_source_missing")
    if _sha256_file(source) != manifest.source_sha256:
        raise ValueError("opening_source_sha256_mismatch")


def _validate_roots(capital_root: Path, execution_root: Path) -> None:
    if _is_default_production_root("ashare", capital_root):
        raise ValueError("default_production_capital_root_forbidden")
    if capital_root.exists():
        raise ValueError("staging_capital_root_must_not_exist")
    if execution_root.absolute() == LOCAL_SIM_DIR.absolute():
        raise ValueError("default_production_execution_root_forbidden")
    if execution_root.name != ASHARE_EXECUTION_LINEAGE_ID:
        raise ValueError("staging_execution_root_must_be_lineage_namespaced")
    if execution_root.exists():
        raise ValueError("staging_execution_root_must_not_exist")
    if capital_root.absolute() == execution_root.absolute():
        raise ValueError("capital_and_execution_roots_must_differ")


def _write_manifest(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists():
        raise ValueError("output_opening_manifest_exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoded = (
        json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _run(args: argparse.Namespace) -> dict[str, Any]:
    if str(os.environ.get("REAL_TRADING_ENABLED") or "").strip().lower() in TRUTHY:
        raise ValueError("environment_real_trading_requested")
    if not args.confirm_zero_import:
        raise ValueError("confirm_zero_import_required")

    capital_root = args.capital_root.expanduser()
    execution_root = args.execution_root.expanduser()
    output_manifest = args.output_opening_manifest.expanduser()
    _validate_roots(capital_root, execution_root)

    raw_opening = _read_object(
        args.source_opening_manifest.expanduser(),
        blocker="opening_manifest_unreadable",
    )
    source_manifest = _opening_manifest_from_payload(raw_opening)
    _validate_zero_state(source_manifest)
    _validate_source(source_manifest)
    legacy_freeze = _read_object(
        args.legacy_freeze_manifest.expanduser(),
        blocker="legacy_freeze_manifest_unreadable",
    )

    started = _aware_timestamp(args.lineage_started_at, field="lineage_started_at")
    point_in_time = _aware_timestamp(
        args.point_in_time_as_of,
        field="point_in_time_as_of",
    )
    if point_in_time < started:
        raise ValueError("point_in_time_before_lineage_start")
    if point_in_time > datetime.now(timezone.utc) + timedelta(seconds=5):
        raise ValueError("future_point_in_time_forbidden")
    if point_in_time.astimezone(timezone(timedelta(hours=8))).strftime(
        "%Y%m%d"
    ) != str(source_manifest.as_of).replace("-", ""):
        raise ValueError("opening_manifest_trade_date_mismatch")

    corrected_manifest = replace(
        source_manifest,
        execution_lineage_id=ASHARE_EXECUTION_LINEAGE_ID,
    )
    plan = {
        "status": "validated_dry_run",
        "market": "ashare",
        "capital_root": str(capital_root),
        "execution_root": str(execution_root),
        "output_opening_manifest": str(output_manifest),
        "source_opening_manifest_sha256": _sha256_file(
            args.source_opening_manifest.expanduser()
        ),
        "source_opening_evidence_sha256": corrected_manifest.source_sha256,
        "legacy_freeze_manifest_sha256": _sha256_file(
            args.legacy_freeze_manifest.expanduser()
        ),
        "execution_lineage_id": ASHARE_EXECUTION_LINEAGE_ID,
        "initial_cash_cny": 50_000.0,
        "positions": {},
        "real_trading_enabled": False,
    }
    if not args.apply:
        return plan

    _write_manifest(output_manifest, asdict(corrected_manifest))
    policy = MarketPolicy.load("ashare")
    ledger = MarketCapitalLedger(capital_root, policy=policy)
    capital_result = ledger.initialize(
        corrected_manifest,
        cutover_manifest={
            "cutover_decision_id": PINNED_CUTOVER_DECISION_ID,
            "source_thread_id": PINNED_SOURCE_THREAD_ID,
            "cutover_state": REQUIRED_CUTOVER_STATE,
            "authority_generation": policy.authority_generation,
            "confirmed_by": "nicholas",
        },
        legacy_freeze_manifest=legacy_freeze,
    )
    execution_result = bootstrap_fresh_local_sim(
        root=execution_root,
        lineage_started_at=args.lineage_started_at,
        point_in_time_as_of=args.point_in_time_as_of,
    )

    snapshot = ledger.snapshot()
    execution_manifest = get_local_sim_execution_lineage_manifest(root=execution_root)
    if snapshot.execution_lineage_id != ASHARE_EXECUTION_LINEAGE_ID:
        raise ValueError("capital_execution_lineage_mismatch")
    if execution_manifest.get("execution_lineage_id") != snapshot.execution_lineage_id:
        raise ValueError("execution_manifest_capital_lineage_mismatch")
    if (
        snapshot.cash_balance_cny != 50_000.0
        or snapshot.equity_cny != 50_000.0
        or snapshot.positions_market_value_cny != 0.0
        or snapshot.realized_pnl_cny != 0.0
        or snapshot.unrealized_pnl_cny != 0.0
        or snapshot.reconciled
    ):
        raise ValueError("staged_capital_postcondition_failed")
    chain = ledger.validate_checksum_chain()
    if chain.get("status") != "valid":
        raise ValueError("staged_capital_checksum_invalid")

    return {
        **plan,
        "status": "staged",
        "output_opening_manifest_sha256": _sha256_file(output_manifest),
        "capital": capital_result,
        "capital_checksum": chain,
        "execution": execution_result,
        "execution_manifest_status": execution_manifest.get("status"),
        "reconciled": False,
        "fresh": False,
        "real_trading_enabled": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capital-root", type=Path, required=True)
    parser.add_argument("--execution-root", type=Path, required=True)
    parser.add_argument("--source-opening-manifest", type=Path, required=True)
    parser.add_argument("--legacy-freeze-manifest", type=Path, required=True)
    parser.add_argument("--output-opening-manifest", type=Path, required=True)
    parser.add_argument("--lineage-started-at", required=True)
    parser.add_argument("--point-in-time-as-of", required=True)
    parser.add_argument("--confirm-zero-import", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        _emit(_run(args))
        return 0
    except (ValueError, MarketCapitalLedgerError, OSError) as exc:
        _emit(
            {
                "status": "blocked",
                "blockers": [str(exc)],
                "real_trading_enabled": False,
            }
        )
        return BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
