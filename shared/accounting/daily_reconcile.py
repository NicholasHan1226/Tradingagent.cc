#!/usr/bin/env python3
"""Identity-bound reconciliation protocol for one market adapter.

Each market owns its broker/paper payload mapping and quantity rules.  The
shared layer only compares already-normalized snapshots after proving that both
snapshots belong to the same market, account, authority and broker contract.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Any

from shared.governance.market_lanes import load_market_lanes


LEDGER_DIR = Path(__file__).resolve().parent.parent / "logs"
RECONCILE_LOG = LEDGER_DIR / "reconcile_results.jsonl"


@dataclass(frozen=True)
class ReconcileSnapshotIdentity:
    market: str
    account_id: str
    authority_id: str
    broker_contract: str
    receipt_id: str
    observed_at: str
    generation: int

    def __post_init__(self) -> None:
        for field_name, value in asdict(self).items():
            if field_name == "generation":
                continue
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"{field_name} must be a non-empty canonical string")
        if isinstance(self.generation, bool) or not isinstance(self.generation, int):
            raise ValueError("generation must be a positive integer")
        if self.generation <= 0:
            raise ValueError("generation must be a positive integer")
        _parse_observed_at(self.observed_at)

    def binding(self) -> tuple[str, str, str, str, str]:
        return (
            self.market,
            self.account_id,
            self.authority_id,
            self.broker_contract,
            str(self.generation),
        )


def _log_reconcile(result: dict[str, Any]) -> None:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"timestamp": datetime.now().isoformat(), "result": result}
    with open(RECONCILE_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _decimal(value: Any, *, field_name: str) -> Decimal:
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError(f"{field_name} must be a finite decimal")
    try:
        parsed = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal")
    return parsed


def _json_number(value: Decimal) -> int | float:
    integral = value.to_integral_value()
    return int(integral) if value == integral else float(value)


def _parse_observed_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("observed_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def _normalize_positions(
    positions: list[dict[str, Any]],
    *,
    quantity_step: Decimal,
    source_name: str,
    market: str,
) -> dict[tuple[str, str, str], dict[str, Any]]:
    normalized: dict[tuple[str, str, str], dict[str, Any]] = {}
    for index, position in enumerate(positions):
        if not isinstance(position, dict):
            raise ValueError(f"{source_name}[{index}] must be a mapping")
        instrument_id = str(
            position.get("instrument_id")
            or position.get("ts_code")
            or position.get("symbol")
            or ""
        ).strip()
        if not instrument_id:
            raise ValueError(f"{source_name}[{index}] missing instrument_id")
        raw_side = str(position.get("position_side") or "").strip().lower()
        raw_bucket = str(position.get("position_bucket") or "").strip().lower()
        if market == "cn_futures":
            if raw_side not in {"long", "short"}:
                raise ValueError(
                    f"{source_name}[{index}].position_side must be long or short"
                )
            if raw_bucket not in {"today", "yesterday", "total"}:
                raise ValueError(
                    f"{source_name}[{index}].position_bucket must be explicit"
                )
        else:
            raw_side = raw_side or "long"
            raw_bucket = raw_bucket or "settled"
            if raw_side != "long" or raw_bucket != "settled":
                raise ValueError(
                    f"{source_name}[{index}] uses an unsupported position dimension"
                )
        key = (instrument_id, raw_side, raw_bucket)
        if key in normalized:
            raise ValueError(
                f"{source_name} contains duplicate {instrument_id}/{raw_side}/{raw_bucket}"
            )
        quantity = _decimal(
            position.get("quantity"),
            field_name=f"{source_name}[{index}].quantity",
        )
        if quantity < 0 or quantity % quantity_step != 0:
            raise ValueError(
                f"{source_name}[{index}].quantity violates step {quantity_step}"
            )
        raw_value = position.get(
            "market_value", position.get("cost_basis", position.get("value", 0))
        )
        market_value = _decimal(
            raw_value,
            field_name=f"{source_name}[{index}].market_value",
        )
        if market_value < 0:
            raise ValueError(f"{source_name}[{index}].market_value must be non-negative")
        normalized[key] = {
            "instrument_id": instrument_id,
            "position_side": raw_side,
            "position_bucket": raw_bucket,
            "quantity": quantity,
            "value": market_value.quantize(Decimal("0.01")),
        }
    return normalized


def reconcile(
    system_positions: list[dict[str, Any]],
    broker_positions: list[dict[str, Any]],
    *,
    system_identity: ReconcileSnapshotIdentity,
    broker_identity: ReconcileSnapshotIdentity,
    quantity_step: str,
    allow_short: bool = False,
    value_tolerance: str = "0.01",
    max_snapshot_skew_seconds: int = 60,
    log: bool = True,
) -> dict[str, Any]:
    """Compare one market's normalized snapshots without cross-market fallback."""

    if not isinstance(system_identity, ReconcileSnapshotIdentity) or not isinstance(
        broker_identity, ReconcileSnapshotIdentity
    ):
        raise TypeError("both snapshot identities must be ReconcileSnapshotIdentity")
    if system_identity.binding() != broker_identity.binding():
        raise ValueError("reconcile snapshot identity mismatch")
    if not isinstance(allow_short, bool):
        raise TypeError("allow_short must be boolean")
    try:
        lane = load_market_lanes().get_for_runtime_market(system_identity.market)
    except ValueError as exc:
        raise ValueError("reconcile market is not governed") from exc
    if (
        system_identity.authority_id != lane.authority_id
        or system_identity.broker_contract
        != lane.broker_boundary.simulation_contract
    ):
        raise ValueError("reconcile identity does not match market governance")
    if allow_short != (system_identity.market == "cn_futures"):
        raise ValueError("allow_short must match the market reconciliation contract")
    if (
        isinstance(max_snapshot_skew_seconds, bool)
        or not isinstance(max_snapshot_skew_seconds, int)
        or max_snapshot_skew_seconds < 0
    ):
        raise ValueError("max_snapshot_skew_seconds must be a non-negative integer")
    snapshot_skew_seconds = abs(
        (
            _parse_observed_at(system_identity.observed_at)
            - _parse_observed_at(broker_identity.observed_at)
        ).total_seconds()
    )
    if snapshot_skew_seconds > max_snapshot_skew_seconds:
        raise ValueError("reconcile snapshots exceed the allowed observation-time skew")
    step = _decimal(quantity_step, field_name="quantity_step")
    if step <= 0:
        raise ValueError("quantity_step must be positive")
    tolerance = _decimal(value_tolerance, field_name="value_tolerance")
    if tolerance < 0:
        raise ValueError("value_tolerance must be non-negative")

    system = _normalize_positions(
        system_positions,
        quantity_step=step,
        source_name="system_positions",
        market=system_identity.market,
    )
    broker = _normalize_positions(
        broker_positions,
        quantity_step=step,
        source_name="broker_positions",
        market=system_identity.market,
    )

    matched: list[str] = []
    mismatches: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []
    actions: list[dict[str, str]] = []
    for position_key in sorted(set(system) | set(broker)):
        instrument_id, position_side, position_bucket = position_key
        system_position = system.get(position_key)
        broker_position = broker.get(position_key)
        system_qty = system_position["quantity"] if system_position else Decimal(0)
        broker_qty = broker_position["quantity"] if broker_position else Decimal(0)
        system_value = system_position["value"] if system_position else Decimal(0)
        broker_value = broker_position["value"] if broker_position else Decimal(0)
        quantity_diff = system_qty - broker_qty
        value_diff = system_value - broker_value
        comparisons.append(
            {
                "instrument_id": instrument_id,
                "position_side": position_side,
                "position_bucket": position_bucket,
                "system_value": _json_number(system_value),
                "broker_value": _json_number(broker_value),
                "value_diff": _json_number(value_diff),
            }
        )
        if (
            quantity_diff == 0
            and abs(value_diff) <= tolerance
            and system_position
            and broker_position
        ):
            matched.append(
                f"{instrument_id}|{position_side}|{position_bucket}"
            )
            continue
        if system_position is None:
            mismatch_type = "missing_in_system"
        elif broker_position is None:
            mismatch_type = "missing_in_broker"
        elif quantity_diff != 0:
            mismatch_type = "quantity_diff"
        else:
            mismatch_type = "market_value_diff"
        mismatches.append(
            {
                "instrument_id": instrument_id,
                "position_side": position_side,
                "position_bucket": position_bucket,
                "system_qty": _json_number(system_qty),
                "broker_qty": _json_number(broker_qty),
                "qty_diff": _json_number(quantity_diff),
                "system_value": _json_number(system_value),
                "broker_value": _json_number(broker_value),
                "value_diff": _json_number(value_diff),
                "type": mismatch_type,
            }
        )
        actions.append(
            {
                "instrument_id": instrument_id,
                "position_side": position_side,
                "position_bucket": position_bucket,
                "action": "investigate",
                "detail": (
                    f"Quantity mismatch: system={_json_number(system_qty)}, "
                    f"broker={_json_number(broker_qty)}, "
                    f"diff={_json_number(quantity_diff)}. "
                    "Investigate before selecting either side as authority."
                ),
            }
        )

    result = {
        "identity": {
            "market": system_identity.market,
            "account_id": system_identity.account_id,
            "authority_id": system_identity.authority_id,
            "broker_contract": system_identity.broker_contract,
            "system_receipt_id": system_identity.receipt_id,
            "broker_receipt_id": broker_identity.receipt_id,
            "system_observed_at": system_identity.observed_at,
            "broker_observed_at": broker_identity.observed_at,
            "generation": system_identity.generation,
            "snapshot_skew_seconds": snapshot_skew_seconds,
            "max_snapshot_skew_seconds": max_snapshot_skew_seconds,
            "quantity_step": str(step),
            "allow_short": allow_short,
            "value_tolerance": str(tolerance),
        },
        "matched": matched,
        "mismatches": mismatches,
        "market_value_comparisons": comparisons,
        "actions": actions,
        "summary": {
            "total_system": len(system),
            "total_broker": len(broker),
            "matched_count": len(matched),
            "mismatch_count": len(mismatches),
            "passed": not mismatches,
        },
    }
    if log:
        _log_reconcile(result)
    return result


__all__ = ["ReconcileSnapshotIdentity", "reconcile"]
