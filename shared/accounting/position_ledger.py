#!/usr/bin/env python3
"""Position flow ledger: track open/add/reduce/close for every holding."""

from __future__ import annotations

import csv
import fcntl
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

LEDGER_DIR = Path(__file__).resolve().parent.parent / "logs"
POSITION_CSV = LEDGER_DIR / "position_ledger.csv"
POSITION_LOCK = POSITION_CSV.with_suffix(".csv.lock")

CSV_HEADERS = [
    "entry_id",
    "timestamp",
    "event_type",
    "ts_code",
    "quantity",
    "price",
    "amount",
    "running_quantity",
    "running_cost",
    "running_avg_price",
    "realized_pnl",
    "order_id",
    "audit_id",
    "note",
]


@dataclass
class PositionEntry:
    event_type: str
    ts_code: str
    quantity: int
    price: float
    order_id: str = ""
    audit_id: str = ""
    note: str = ""
    entry_id: str = field(default_factory=lambda: f"POS-{uuid.uuid4().hex[:12]}")
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@contextmanager
def _ledger_lock() -> Iterator[None]:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    with open(POSITION_LOCK, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _ensure_csv_unlocked() -> None:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    if not POSITION_CSV.exists():
        with open(POSITION_CSV, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_HEADERS)
            writer.writeheader()


def _append_unlocked(row: dict[str, Any]) -> str:
    with open(POSITION_CSV, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADERS)
        writer.writerow(row)
    return row["entry_id"]


def _read_all_entries_unlocked() -> list[dict[str, Any]]:
    if not POSITION_CSV.exists():
        return []
    with open(POSITION_CSV, "r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return list(reader)


def _total_fee(commission: float, stamp_duty: float, transfer_fee: float) -> float:
    total = 0.0
    for name, value in (
        ("commission", commission),
        ("stamp_duty", stamp_duty),
        ("transfer_fee", transfer_fee),
    ):
        try:
            fee = float(value or 0.0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be numeric, got {value}") from exc
        if fee < 0:
            raise ValueError(f"{name} must be non-negative, got {value}")
        total += fee
    return round(total, 2)


def _get_current_state_from_entries(entries: list[dict[str, Any]], ts_code: str) -> dict[str, Any]:
    code_entries = [e for e in entries if e["ts_code"] == ts_code]
    if not code_entries:
        return {"quantity": 0, "cost_basis": 0.0, "avg_price": 0.0}

    last = code_entries[-1]
    return {
        "quantity": int(last["running_quantity"]),
        "cost_basis": float(last["running_cost"]),
        "avg_price": float(last["running_avg_price"]),
    }


def _write_position_event(
    *,
    event_type: str,
    ts_code: str,
    quantity: int,
    price: float,
    order_id: str,
    audit_id: str,
    note: str,
    running_quantity: int,
    running_cost: float,
    running_avg_price: float,
    realized_pnl: float,
) -> str:
    entry = PositionEntry(
        event_type=event_type,
        ts_code=ts_code,
        quantity=quantity,
        price=price,
        order_id=order_id,
        audit_id=audit_id,
        note=note,
    )
    row = {
        "entry_id": entry.entry_id,
        "timestamp": entry.timestamp,
        "event_type": event_type,
        "ts_code": ts_code,
        "quantity": quantity,
        "price": round(price, 4),
        "amount": round(quantity * price, 2),
        "running_quantity": running_quantity,
        "running_cost": round(running_cost, 2),
        "running_avg_price": round(running_avg_price, 4),
        "realized_pnl": round(realized_pnl, 2),
        "order_id": order_id,
        "audit_id": audit_id,
        "note": note,
    }
    return _append_unlocked(row)


def open_position(
    ts_code: str,
    quantity: int,
    price: float,
    order_id: str = "",
    audit_id: str = "",
    note: str = "",
) -> dict[str, Any]:
    if quantity <= 0:
        raise ValueError(f"quantity must be positive, got {quantity}")
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")

    with _ledger_lock():
        _ensure_csv_unlocked()
        entries = _read_all_entries_unlocked()
        current = _get_current_state_from_entries(entries, ts_code)
        if current["quantity"] > 0:
            raise ValueError(
                f"Position already exists for {ts_code} "
                f"({current['quantity']} shares). Use add_position instead."
            )

        amount = round(quantity * price, 2)
        eid = _write_position_event(
            event_type="open",
            ts_code=ts_code,
            quantity=quantity,
            price=price,
            order_id=order_id,
            audit_id=audit_id,
            note=note,
            running_quantity=quantity,
            running_cost=amount,
            running_avg_price=price,
            realized_pnl=0.0,
        )
        return {
            "entry_id": eid,
            "ts_code": ts_code,
            "quantity": quantity,
            "avg_price": round(price, 4),
            "cost_basis": amount,
            "event_type": "open",
        }


def add_position(
    ts_code: str,
    quantity: int,
    price: float,
    order_id: str = "",
    audit_id: str = "",
    note: str = "",
) -> dict[str, Any]:
    if quantity <= 0:
        raise ValueError(f"quantity must be positive, got {quantity}")
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")

    with _ledger_lock():
        _ensure_csv_unlocked()
        entries = _read_all_entries_unlocked()
        current = _get_current_state_from_entries(entries, ts_code)
        if current["quantity"] == 0:
            raise ValueError(f"No existing position for {ts_code}. Use open_position instead.")

        add_amount = round(quantity * price, 2)
        new_qty = current["quantity"] + quantity
        new_cost = round(current["cost_basis"] + add_amount, 2)
        new_avg = round(new_cost / new_qty, 4) if new_qty > 0 else 0.0
        eid = _write_position_event(
            event_type="add",
            ts_code=ts_code,
            quantity=quantity,
            price=price,
            order_id=order_id,
            audit_id=audit_id,
            note=note,
            running_quantity=new_qty,
            running_cost=new_cost,
            running_avg_price=new_avg,
            realized_pnl=0.0,
        )
        return {
            "entry_id": eid,
            "ts_code": ts_code,
            "quantity_added": quantity,
            "new_total_quantity": new_qty,
            "new_avg_price": new_avg,
            "new_cost_basis": new_cost,
            "event_type": "add",
        }


def reduce_position(
    ts_code: str,
    quantity: int,
    price: float,
    order_id: str = "",
    audit_id: str = "",
    note: str = "",
    commission: float = 0.0,
    stamp_duty: float = 0.0,
    transfer_fee: float = 0.0,
) -> dict[str, Any]:
    if quantity <= 0:
        raise ValueError(f"quantity must be positive, got {quantity}")
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")

    with _ledger_lock():
        _ensure_csv_unlocked()
        entries = _read_all_entries_unlocked()
        current = _get_current_state_from_entries(entries, ts_code)
        if current["quantity"] == 0:
            raise ValueError(f"No position to reduce for {ts_code}.")
        if quantity >= current["quantity"]:
            raise ValueError(
                f"Reduce quantity {quantity} >= holding {current['quantity']}. "
                f"Use close_position instead."
            )

        avg_cost = current["avg_price"]
        fee_total = _total_fee(commission, stamp_duty, transfer_fee)
        realized_pnl = round((quantity * price) - (avg_cost * quantity) - fee_total, 2)
        new_qty = current["quantity"] - quantity
        new_cost = round(avg_cost * new_qty, 2)
        eid = _write_position_event(
            event_type="reduce",
            ts_code=ts_code,
            quantity=quantity,
            price=price,
            order_id=order_id,
            audit_id=audit_id,
            note=note,
            running_quantity=new_qty,
            running_cost=new_cost,
            running_avg_price=avg_cost if new_qty > 0 else 0.0,
            realized_pnl=realized_pnl,
        )
        return {
            "entry_id": eid,
            "ts_code": ts_code,
            "quantity_reduced": quantity,
            "remaining_quantity": new_qty,
            "realized_pnl": realized_pnl,
            "fee": fee_total,
            "avg_price": avg_cost,
            "event_type": "reduce",
        }


def close_position(
    ts_code: str,
    price: float,
    order_id: str = "",
    audit_id: str = "",
    note: str = "",
    commission: float = 0.0,
    stamp_duty: float = 0.0,
    transfer_fee: float = 0.0,
) -> dict[str, Any]:
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")

    with _ledger_lock():
        _ensure_csv_unlocked()
        entries = _read_all_entries_unlocked()
        current = _get_current_state_from_entries(entries, ts_code)
        if current["quantity"] == 0:
            raise ValueError(f"No position to close for {ts_code}.")

        quantity = current["quantity"]
        avg_cost = current["avg_price"]
        fee_total = _total_fee(commission, stamp_duty, transfer_fee)
        realized_pnl = round((quantity * price) - (avg_cost * quantity) - fee_total, 2)
        eid = _write_position_event(
            event_type="close",
            ts_code=ts_code,
            quantity=quantity,
            price=price,
            order_id=order_id,
            audit_id=audit_id,
            note=note,
            running_quantity=0,
            running_cost=0.0,
            running_avg_price=0.0,
            realized_pnl=realized_pnl,
        )
        return {
            "entry_id": eid,
            "ts_code": ts_code,
            "quantity_closed": quantity,
            "realized_pnl": realized_pnl,
            "fee": fee_total,
            "avg_price": avg_cost,
            "event_type": "close",
        }


def get_positions() -> list[dict[str, Any]]:
    with _ledger_lock():
        _ensure_csv_unlocked()
        entries = _read_all_entries_unlocked()

    if not entries:
        return []

    latest: dict[str, dict[str, Any]] = {}
    for entry in entries:
        latest[entry["ts_code"]] = entry

    positions = []
    for ts_code, entry in latest.items():
        qty = int(entry["running_quantity"])
        if qty > 0:
            positions.append({
                "ts_code": ts_code,
                "quantity": qty,
                "cost_basis": float(entry["running_cost"]),
                "avg_price": float(entry["running_avg_price"]),
            })
    return positions


if __name__ == "__main__":
    r1 = open_position("600519.SH", 100, 1000.00, order_id="T1")
    print("open:", r1)
    r2 = add_position("600519.SH", 100, 1100.00, order_id="T2")
    print("add:", r2)
    r3 = reduce_position("600519.SH", 50, 1150.00, order_id="T3")
    print("reduce:", r3)
    print("positions:", get_positions())
    r4 = close_position("600519.SH", 1200.00, order_id="T4")
    print("close:", r4)
    print("positions after close:", get_positions())
