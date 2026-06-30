#!/usr/bin/env python3
"""Position flow ledger: track open/add/reduce/close for every holding."""

from __future__ import annotations

import fcntl
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

LEDGER_DIR = Path(__file__).resolve().parent.parent / "logs"
POSITION_CSV = LEDGER_DIR / "position_ledger.csv"
POSITION_DB = LEDGER_DIR / "position_ledger.sqlite3"
POSITION_LOCK = POSITION_DB.with_suffix(".sqlite3.lock")
DEFAULT_CAPITAL_LAYER = "shadow"
CAPITAL_LAYERS = {"real", "simulated", "shadow"}
POSITION_TABLES = {
    "real": "position_ledger_real",
    "simulated": "position_ledger_simulated",
    "shadow": "position_ledger_shadow",
}

CSV_HEADERS = [
    "entry_id",
    "timestamp",
    "entry_date",
    "event_type",
    "capital_layer",
    "is_real_money",
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
    capital_layer: str = DEFAULT_CAPITAL_LAYER
    order_id: str = ""
    audit_id: str = ""
    note: str = ""
    entry_date: str = ""
    entry_id: str = field(default_factory=lambda: f"POS-{uuid.uuid4().hex[:12]}")
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@contextmanager
def _ledger_lock() -> Iterator[None]:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    with open(LEDGER_DIR / POSITION_LOCK.name, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _table_for_layer(capital_layer: str) -> str:
    return POSITION_TABLES[_normalize_capital_layer(capital_layer)]


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(LEDGER_DIR / POSITION_DB.name)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _create_tables_unlocked(conn: sqlite3.Connection) -> None:
    columns = ", ".join(f"{name} TEXT NOT NULL DEFAULT ''" for name in CSV_HEADERS)
    for table in POSITION_TABLES.values():
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table} (
                {columns},
                PRIMARY KEY(entry_id)
            )
            """
        )
    conn.commit()


def _normalize_legacy_row(row: dict[str, Any], row_number: int) -> dict[str, str]:
    normalized = {key: str(row.get(key, "") or "") for key in CSV_HEADERS}
    layer = _normalize_capital_layer(row.get("capital_layer", DEFAULT_CAPITAL_LAYER))
    normalized["capital_layer"] = layer
    normalized["is_real_money"] = _is_real_money(layer)
    if not normalized["entry_id"]:
        normalized["entry_id"] = f"POS-LEGACY-{row_number:08d}"
    return normalized


def _migrate_legacy_csv_unlocked(conn: sqlite3.Connection) -> None:
    if not POSITION_CSV.exists():
        return
    import csv

    with open(POSITION_CSV, "r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row_number, row in enumerate(reader, start=1):
            normalized = _normalize_legacy_row(dict(row), row_number)
            _insert_entry_unlocked(conn, normalized)
    conn.commit()


def _ensure_db_unlocked(conn: sqlite3.Connection) -> None:
    _create_tables_unlocked(conn)
    _migrate_legacy_csv_unlocked(conn)


def _insert_entry_unlocked(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    normalized = {key: str(row.get(key, "") or "") for key in CSV_HEADERS}
    layer = _normalize_capital_layer(normalized.get("capital_layer"))
    normalized["capital_layer"] = layer
    normalized["is_real_money"] = _is_real_money(layer)
    table = _table_for_layer(layer)
    placeholders = ", ".join("?" for _ in CSV_HEADERS)
    columns = ", ".join(CSV_HEADERS)
    conn.execute(
        f"INSERT OR IGNORE INTO {table} ({columns}) VALUES ({placeholders})",
        [normalized[key] for key in CSV_HEADERS],
    )


def _append_unlocked(conn: sqlite3.Connection, row: dict[str, Any]) -> str:
    _insert_entry_unlocked(conn, row)
    return str(row["entry_id"])


def _read_all_entries_unlocked(
    conn: sqlite3.Connection,
    capital_layer: str | None = None,
) -> list[dict[str, Any]]:
    layer_filter = _normalize_position_filter(capital_layer)
    layers = sorted(CAPITAL_LAYERS) if layer_filter is None else [layer_filter]
    entries: list[dict[str, Any]] = []
    for layer in layers:
        table = _table_for_layer(layer)
        rows = conn.execute(
            f"SELECT {', '.join(CSV_HEADERS)} FROM {table} ORDER BY timestamp, rowid"
        ).fetchall()
        for row in rows:
            entry = dict(row)
            entry["capital_layer"] = _normalize_capital_layer(entry.get("capital_layer"))
            entry["is_real_money"] = _is_real_money(entry["capital_layer"])
            entries.append(entry)
    return entries


def _normalize_capital_layer(value: str | None) -> str:
    layer = str(value or DEFAULT_CAPITAL_LAYER).strip().lower()
    if layer == "sim":
        layer = "simulated"
    if layer in CAPITAL_LAYERS:
        return layer
    raise ValueError(f"capital_layer must be one of real/simulated/shadow, got {value}")


def _normalize_position_filter(value: str | None) -> str | None:
    if value is None:
        return "real"
    layer = str(value).strip().lower()
    if layer == "all":
        return None
    return _normalize_capital_layer(layer)


def _is_real_money(capital_layer: str) -> str:
    layer = _normalize_capital_layer(capital_layer)
    return "Y" if layer == "real" else "N"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value or 0.0)
        return parsed if parsed == parsed else default
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return default


def _entry_date(timestamp: str) -> str:
    return str(timestamp or "")[:10]


def _normalize_entry_date(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text[:10] if text else ""


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


def _get_current_state_from_entries(
    entries: list[dict[str, Any]],
    ts_code: str,
    capital_layer: str,
) -> dict[str, Any]:
    layer = _normalize_capital_layer(capital_layer)
    code_entries = [
        e for e in entries
        if e["ts_code"] == ts_code
        and _normalize_capital_layer(e.get("capital_layer", DEFAULT_CAPITAL_LAYER)) == layer
    ]
    state = {"quantity": 0, "cost_basis": 0.0, "avg_price": 0.0, "entry_date": None}
    for entry in code_entries:
        event_type = str(entry.get("event_type", ""))
        qty = _safe_int(entry.get("running_quantity"))
        if event_type == "open":
            state["entry_date"] = _normalize_entry_date(entry.get("entry_date")) or None
        state.update({
            "quantity": qty,
            "cost_basis": _safe_float(entry.get("running_cost")),
            "avg_price": _safe_float(entry.get("running_avg_price")),
        })
        if qty <= 0:
            state["entry_date"] = None
    return state


def _is_ashare_symbol(ts_code: str) -> bool:
    code = str(ts_code or "").strip().upper()
    return code.endswith((".SH", ".SZ", ".BJ"))


def _assert_a_share_t_plus_1(
    *,
    ts_code: str,
    entry_date: Any,
    trade_date: Any,
) -> str | None:
    if not _is_ashare_symbol(ts_code):
        return None
    normalized_trade_date = _normalize_entry_date(trade_date)
    normalized_entry_date = _normalize_entry_date(entry_date)
    if not normalized_trade_date:
        raise ValueError("A-share sell ledger write requires trade_date for T+1 check")
    if not normalized_entry_date:
        raise ValueError("A-share sell ledger write requires entry_date for T+1 check")

    from Ashare.t_plus_1 import can_sell, next_trading_day

    sellable_date = next_trading_day(normalized_entry_date).isoformat()
    if not can_sell(normalized_entry_date, normalized_trade_date):
        raise ValueError(
            "A-share T+1 not satisfied: "
            f"entry_date={normalized_entry_date}, "
            f"sellable_date={sellable_date}, trade_date={normalized_trade_date}"
        )
    return sellable_date


def _write_position_event(
    *,
    conn: sqlite3.Connection,
    event_type: str,
    capital_layer: str,
    ts_code: str,
    quantity: int,
    price: float,
    order_id: str,
    audit_id: str,
    note: str,
    entry_date: str = "",
    running_quantity: int,
    running_cost: float,
    running_avg_price: float,
    realized_pnl: float,
) -> str:
    layer = _normalize_capital_layer(capital_layer)
    entry = PositionEntry(
        event_type=event_type,
        ts_code=ts_code,
        quantity=quantity,
        price=price,
        capital_layer=layer,
        order_id=order_id,
        audit_id=audit_id,
        note=note,
        entry_date=_normalize_entry_date(entry_date),
    )
    row = {
        "entry_id": entry.entry_id,
        "timestamp": entry.timestamp,
        "entry_date": entry.entry_date,
        "event_type": event_type,
        "capital_layer": layer,
        "is_real_money": _is_real_money(layer),
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
    return _append_unlocked(conn, row)


def open_position(
    ts_code: str,
    quantity: int,
    price: float,
    order_id: str = "",
    audit_id: str = "",
    note: str = "",
    capital_layer: str = DEFAULT_CAPITAL_LAYER,
    entry_date: str | None = None,
) -> dict[str, Any]:
    if quantity <= 0:
        raise ValueError(f"quantity must be positive, got {quantity}")
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")

    with _ledger_lock():
        with _connect() as conn:
            _ensure_db_unlocked(conn)
            layer = _normalize_capital_layer(capital_layer)
            entries = _read_all_entries_unlocked(conn, capital_layer=layer)
            current = _get_current_state_from_entries(entries, ts_code, layer)
            if current["quantity"] > 0:
                raise ValueError(
                    f"Position already exists for {ts_code} in {layer} layer "
                    f"({current['quantity']} shares). Use add_position instead."
                )

            amount = round(quantity * price, 2)
            normalized_entry_date = _normalize_entry_date(entry_date) or datetime.now().strftime("%Y-%m-%d")
            eid = _write_position_event(
                conn=conn,
                event_type="open",
                capital_layer=layer,
                ts_code=ts_code,
                quantity=quantity,
                price=price,
                order_id=order_id,
                audit_id=audit_id,
                note=note,
                entry_date=normalized_entry_date,
                running_quantity=quantity,
                running_cost=amount,
                running_avg_price=price,
                realized_pnl=0.0,
            )
            conn.commit()
        return {
            "entry_id": eid,
            "ts_code": ts_code,
            "quantity": quantity,
            "avg_price": round(price, 4),
            "cost_basis": amount,
            "event_type": "open",
            "capital_layer": layer,
            "is_real_money": _is_real_money(layer),
            "entry_date": normalized_entry_date,
        }


def add_position(
    ts_code: str,
    quantity: int,
    price: float,
    order_id: str = "",
    audit_id: str = "",
    note: str = "",
    capital_layer: str = DEFAULT_CAPITAL_LAYER,
) -> dict[str, Any]:
    if quantity <= 0:
        raise ValueError(f"quantity must be positive, got {quantity}")
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")

    with _ledger_lock():
        with _connect() as conn:
            _ensure_db_unlocked(conn)
            layer = _normalize_capital_layer(capital_layer)
            entries = _read_all_entries_unlocked(conn, capital_layer=layer)
            current = _get_current_state_from_entries(entries, ts_code, layer)
            if current["quantity"] == 0:
                raise ValueError(f"No existing position for {ts_code} in {layer} layer. Use open_position instead.")

            add_amount = round(quantity * price, 2)
            new_qty = current["quantity"] + quantity
            new_cost = round(current["cost_basis"] + add_amount, 2)
            new_avg = round(new_cost / new_qty, 4) if new_qty > 0 else 0.0
            eid = _write_position_event(
                conn=conn,
                event_type="add",
                capital_layer=layer,
                ts_code=ts_code,
                quantity=quantity,
                price=price,
                order_id=order_id,
                audit_id=audit_id,
                note=note,
                entry_date="",
                running_quantity=new_qty,
                running_cost=new_cost,
                running_avg_price=new_avg,
                realized_pnl=0.0,
            )
            conn.commit()
        return {
            "entry_id": eid,
            "ts_code": ts_code,
            "quantity_added": quantity,
            "new_total_quantity": new_qty,
            "new_avg_price": new_avg,
            "new_cost_basis": new_cost,
            "event_type": "add",
            "capital_layer": layer,
            "is_real_money": _is_real_money(layer),
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
    capital_layer: str = DEFAULT_CAPITAL_LAYER,
    trade_date: str | None = None,
    current_trade_date: str | None = None,
) -> dict[str, Any]:
    if quantity <= 0:
        raise ValueError(f"quantity must be positive, got {quantity}")
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")

    with _ledger_lock():
        with _connect() as conn:
            _ensure_db_unlocked(conn)
            layer = _normalize_capital_layer(capital_layer)
            entries = _read_all_entries_unlocked(conn, capital_layer=layer)
            current = _get_current_state_from_entries(entries, ts_code, layer)
            if current["quantity"] == 0:
                raise ValueError(f"No position to reduce for {ts_code} in {layer} layer.")
            if quantity >= current["quantity"]:
                raise ValueError(
                    f"Reduce quantity {quantity} >= holding {current['quantity']}. "
                    f"Use close_position instead."
                )
            sellable_date = _assert_a_share_t_plus_1(
                ts_code=ts_code,
                entry_date=current.get("entry_date"),
                trade_date=current_trade_date or trade_date,
            )

            avg_cost = current["avg_price"]
            fee_total = _total_fee(commission, stamp_duty, transfer_fee)
            realized_pnl = round((quantity * price) - (avg_cost * quantity) - fee_total, 2)
            new_qty = current["quantity"] - quantity
            new_cost = round(avg_cost * new_qty, 2)
            eid = _write_position_event(
                conn=conn,
                event_type="reduce",
                capital_layer=layer,
                ts_code=ts_code,
                quantity=quantity,
                price=price,
                order_id=order_id,
                audit_id=audit_id,
                note=note,
                entry_date="",
                running_quantity=new_qty,
                running_cost=new_cost,
                running_avg_price=avg_cost if new_qty > 0 else 0.0,
                realized_pnl=realized_pnl,
            )
            conn.commit()
        return {
            "entry_id": eid,
            "ts_code": ts_code,
            "quantity_reduced": quantity,
            "remaining_quantity": new_qty,
            "realized_pnl": realized_pnl,
            "fee": fee_total,
            "avg_price": avg_cost,
            "event_type": "reduce",
            "capital_layer": layer,
            "is_real_money": _is_real_money(layer),
            "sellable_date": sellable_date,
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
    capital_layer: str = DEFAULT_CAPITAL_LAYER,
    trade_date: str | None = None,
    current_trade_date: str | None = None,
) -> dict[str, Any]:
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")

    with _ledger_lock():
        with _connect() as conn:
            _ensure_db_unlocked(conn)
            layer = _normalize_capital_layer(capital_layer)
            entries = _read_all_entries_unlocked(conn, capital_layer=layer)
            current = _get_current_state_from_entries(entries, ts_code, layer)
            if current["quantity"] == 0:
                raise ValueError(f"No position to close for {ts_code} in {layer} layer.")
            sellable_date = _assert_a_share_t_plus_1(
                ts_code=ts_code,
                entry_date=current.get("entry_date"),
                trade_date=current_trade_date or trade_date,
            )

            quantity = current["quantity"]
            avg_cost = current["avg_price"]
            fee_total = _total_fee(commission, stamp_duty, transfer_fee)
            realized_pnl = round((quantity * price) - (avg_cost * quantity) - fee_total, 2)
            eid = _write_position_event(
                conn=conn,
                event_type="close",
                capital_layer=layer,
                ts_code=ts_code,
                quantity=quantity,
                price=price,
                order_id=order_id,
                audit_id=audit_id,
                note=note,
                entry_date="",
                running_quantity=0,
                running_cost=0.0,
                running_avg_price=0.0,
                realized_pnl=realized_pnl,
            )
            conn.commit()
        return {
            "entry_id": eid,
            "ts_code": ts_code,
            "quantity_closed": quantity,
            "realized_pnl": realized_pnl,
            "fee": fee_total,
            "avg_price": avg_cost,
            "event_type": "close",
            "capital_layer": layer,
            "is_real_money": _is_real_money(layer),
            "sellable_date": sellable_date,
        }


def get_positions(capital_layer: str | None = None) -> list[dict[str, Any]]:
    with _ledger_lock():
        with _connect() as conn:
            _ensure_db_unlocked(conn)
            entries = _read_all_entries_unlocked(conn, capital_layer=capital_layer)

    if not entries:
        return []

    layer_filter = _normalize_position_filter(capital_layer)
    states: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries:
        ts_code = str(entry.get("ts_code", ""))
        if not ts_code:
            continue
        layer = _normalize_capital_layer(entry.get("capital_layer", DEFAULT_CAPITAL_LAYER))
        if layer_filter is not None and layer != layer_filter:
            continue

        qty = _safe_int(entry.get("running_quantity"))
        avg_price = _safe_float(entry.get("running_avg_price"))
        event_price = _safe_float(entry.get("price"))
        note = str(entry.get("note", "") or "")
        event_type = str(entry.get("event_type", ""))
        state_key = (layer, ts_code)

        if event_type == "open" or state_key not in states:
            states[state_key] = {
                "entry_date": _normalize_entry_date(entry.get("entry_date")) or None,
                "high_price": max(avg_price, event_price),
                "thesis": note,
            }
        else:
            states[state_key]["high_price"] = max(
                _safe_float(states[state_key].get("high_price")),
                avg_price,
                event_price,
            )
            if note:
                states[state_key]["thesis"] = note

        states[state_key].update({
            "ts_code": ts_code,
            "capital_layer": layer,
            "is_real_money": _is_real_money(layer),
            "quantity": qty,
            "cost_basis": _safe_float(entry.get("running_cost")),
            "avg_price": avg_price,
            "cost": avg_price,
        })

        if qty <= 0:
            states.pop(state_key, None)

    return [state for state in states.values() if _safe_int(state.get("quantity")) > 0]


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
