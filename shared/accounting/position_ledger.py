#!/usr/bin/env python3
"""Position flow ledger: track open/add/reduce/close for every holding.

Records position lifecycle events as an append-only CSV. The current
position state is derived by replaying the ledger.

Position events:
  - open:   first buy of a ts_code (quantity > 0)
  - add:    subsequent buy of an existing position (quantity > 0)
  - reduce: partial sell (0 < quantity < current holding)
  - close:  full sell (quantity == current holding)

Each event records the trade price and updates the running cost basis
using weighted average cost.
"""

from __future__ import annotations

import csv
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

LEDGER_DIR = Path(__file__).resolve().parent.parent / "logs"
POSITION_CSV = LEDGER_DIR / "position_ledger.csv"

CSV_HEADERS = [
    "entry_id",
    "timestamp",
    "event_type",       # open | add | reduce | close
    "ts_code",          # stock code
    "quantity",         # shares traded in this event (positive)
    "price",            # execution price
    "amount",           # quantity * price
    "running_quantity", # position size after this event
    "running_cost",     # total cost basis after this event
    "running_avg_price", # weighted average cost after this event
    "realized_pnl",     # realized P&L for reduce/close (0 for open/add)
    "order_id",
    "audit_id",
    "note",
]


@dataclass
class PositionEntry:
    """Single position flow record."""

    event_type: str
    ts_code: str
    quantity: int
    price: float
    order_id: str = ""
    audit_id: str = ""
    note: str = ""
    entry_id: str = field(default_factory=lambda: f"POS-{uuid.uuid4().hex[:12]}")
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


def _ensure_csv() -> None:
    """Create CSV with headers if it does not exist."""
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    if not POSITION_CSV.exists():
        with open(POSITION_CSV, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_HEADERS)
            writer.writeheader()


def _append(row: dict[str, Any]) -> str:
    """Append a row to position CSV. Returns entry_id."""
    _ensure_csv()
    with open(POSITION_CSV, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADERS)
        writer.writerow(row)
    return row["entry_id"]


def _read_all_entries() -> list[dict[str, Any]]:
    """Read all position ledger entries."""
    if not POSITION_CSV.exists():
        return []
    with open(POSITION_CSV, "r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return list(reader)


def _get_current_state(ts_code: str) -> dict[str, Any]:
    """Get current running state for a ts_code by replaying the ledger.

    Returns:
        dict with: quantity, cost_basis, avg_price (0/0/0 if no position).
    """
    entries = _read_all_entries()
    code_entries = [e for e in entries if e["ts_code"] == ts_code]
    if not code_entries:
        return {"quantity": 0, "cost_basis": 0.0, "avg_price": 0.0}

    last = code_entries[-1]
    return {
        "quantity": int(last["running_quantity"]),
        "cost_basis": float(last["running_cost"]),
        "avg_price": float(last["running_avg_price"]),
    }


def open_position(
    ts_code: str,
    quantity: int,
    price: float,
    order_id: str = "",
    audit_id: str = "",
    note: str = "",
) -> dict[str, Any]:
    """Open a new position. Fails if position already exists.

    Args:
        ts_code: stock code.
        quantity: shares (positive int).
        price: execution price per share.
        order_id: link to execution order.
        audit_id: link to audit trail event.
        note: free-text annotation.

    Returns:
        dict with: entry_id, ts_code, quantity, avg_price, cost_basis, event_type.
    """
    if quantity <= 0:
        raise ValueError(f"quantity must be positive, got {quantity}")
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")

    current = _get_current_state(ts_code)
    if current["quantity"] > 0:
        raise ValueError(
            f"Position already exists for {ts_code} "
            f"({current['quantity']} shares). Use add_position instead."
        )

    amount = round(quantity * price, 2)
    entry = PositionEntry(
        event_type="open",
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
        "event_type": "open",
        "ts_code": ts_code,
        "quantity": quantity,
        "price": round(price, 4),
        "amount": amount,
        "running_quantity": quantity,
        "running_cost": amount,
        "running_avg_price": round(price, 4),
        "realized_pnl": 0.0,
        "order_id": order_id,
        "audit_id": audit_id,
        "note": note,
    }
    eid = _append(row)
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
    """Add to an existing position. Fails if no position exists.

    Updates weighted average cost basis:
        new_avg = (old_cost + new_amount) / new_total_quantity

    Args:
        ts_code: stock code.
        quantity: shares to add (positive int).
        price: execution price per share.
        order_id: link to execution order.
        audit_id: link to audit trail event.
        note: free-text annotation.

    Returns:
        dict with: entry_id, ts_code, quantity_added, new_total_quantity,
        new_avg_price, new_cost_basis, event_type.
    """
    if quantity <= 0:
        raise ValueError(f"quantity must be positive, got {quantity}")
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")

    current = _get_current_state(ts_code)
    if current["quantity"] == 0:
        raise ValueError(
            f"No existing position for {ts_code}. Use open_position instead."
        )

    add_amount = round(quantity * price, 2)
    new_qty = current["quantity"] + quantity
    new_cost = round(current["cost_basis"] + add_amount, 2)
    new_avg = round(new_cost / new_qty, 4) if new_qty > 0 else 0.0

    entry = PositionEntry(
        event_type="add",
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
        "event_type": "add",
        "ts_code": ts_code,
        "quantity": quantity,
        "price": round(price, 4),
        "amount": add_amount,
        "running_quantity": new_qty,
        "running_cost": new_cost,
        "running_avg_price": new_avg,
        "realized_pnl": 0.0,
        "order_id": order_id,
        "audit_id": audit_id,
        "note": note,
    }
    eid = _append(row)
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
) -> dict[str, Any]:
    """Reduce (partially sell) a position. Fails if quantity exceeds holding.

    Realized P&L = (sell_price - avg_cost) * quantity_sold.

    Args:
        ts_code: stock code.
        quantity: shares to sell (positive int, must be < current holding).
        price: execution price per share.
        order_id: link to execution order.
        audit_id: link to audit trail event.
        note: free-text annotation.

    Returns:
        dict with: entry_id, ts_code, quantity_reduced, remaining_quantity,
        realized_pnl, avg_price, event_type.
    """
    if quantity <= 0:
        raise ValueError(f"quantity must be positive, got {quantity}")
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")

    current = _get_current_state(ts_code)
    if current["quantity"] == 0:
        raise ValueError(f"No position to reduce for {ts_code}.")
    if quantity >= current["quantity"]:
        raise ValueError(
            f"Reduce quantity {quantity} >= holding {current['quantity']}. "
            f"Use close_position instead."
        )

    avg_cost = current["avg_price"]
    sell_amount = round(quantity * price, 2)
    realized_pnl = round((price - avg_cost) * quantity, 2)
    new_qty = current["quantity"] - quantity
    # Cost basis reduces proportionally
    new_cost = round(avg_cost * new_qty, 2)

    entry = PositionEntry(
        event_type="reduce",
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
        "event_type": "reduce",
        "ts_code": ts_code,
        "quantity": quantity,
        "price": round(price, 4),
        "amount": sell_amount,
        "running_quantity": new_qty,
        "running_cost": new_cost,
        "running_avg_price": avg_cost,
        "realized_pnl": realized_pnl,
        "order_id": order_id,
        "audit_id": audit_id,
        "note": note,
    }
    eid = _append(row)
    return {
        "entry_id": eid,
        "ts_code": ts_code,
        "quantity_reduced": quantity,
        "remaining_quantity": new_qty,
        "realized_pnl": realized_pnl,
        "avg_price": avg_cost,
        "event_type": "reduce",
    }


def close_position(
    ts_code: str,
    price: float,
    order_id: str = "",
    audit_id: str = "",
    note: str = "",
) -> dict[str, Any]:
    """Close an entire position. Fails if no position exists.

    Realized P&L = (sell_price - avg_cost) * total_quantity.

    Args:
        ts_code: stock code.
        price: execution price per share.
        order_id: link to execution order.
        audit_id: link to audit trail event.
        note: free-text annotation.

    Returns:
        dict with: entry_id, ts_code, quantity_closed, realized_pnl,
        avg_price, event_type.
    """
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")

    current = _get_current_state(ts_code)
    if current["quantity"] == 0:
        raise ValueError(f"No position to close for {ts_code}.")

    quantity = current["quantity"]
    avg_cost = current["avg_price"]
    sell_amount = round(quantity * price, 2)
    realized_pnl = round((price - avg_cost) * quantity, 2)

    entry = PositionEntry(
        event_type="close",
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
        "event_type": "close",
        "ts_code": ts_code,
        "quantity": quantity,
        "price": round(price, 4),
        "amount": sell_amount,
        "running_quantity": 0,
        "running_cost": 0.0,
        "running_avg_price": 0.0,
        "realized_pnl": realized_pnl,
        "order_id": order_id,
        "audit_id": audit_id,
        "note": note,
    }
    eid = _append(row)
    return {
        "entry_id": eid,
        "ts_code": ts_code,
        "quantity_closed": quantity,
        "realized_pnl": realized_pnl,
        "avg_price": avg_cost,
        "event_type": "close",
    }


def get_positions() -> list[dict[str, Any]]:
    """Get all current open positions by replaying the ledger.

    Returns:
        List of dicts, each with:
            ts_code, quantity, cost_basis, avg_price.
        Only positions with quantity > 0 are returned.
    """
    entries = _read_all_entries()
    if not entries:
        return []

    # Group by ts_code, take the last entry for each
    latest: dict[str, dict[str, Any]] = {}
    for e in entries:
        latest[e["ts_code"]] = e

    positions = []
    for ts_code, e in latest.items():
        qty = int(e["running_quantity"])
        if qty > 0:
            positions.append({
                "ts_code": ts_code,
                "quantity": qty,
                "cost_basis": float(e["running_cost"]),
                "avg_price": float(e["running_avg_price"]),
            })

    return positions


# ---- self-test --------------------------------------------------------------

if __name__ == "__main__":
    # Smoke test: open -> add -> reduce -> close
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
