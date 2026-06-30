#!/usr/bin/env python3
"""Capital flow ledger: track every cent of fund movement.

Records all capital events: buy, sell, deposit, withdrawal, reverse_repo,
repo_maturity, interest, fee.
CSV storage, append-only. Each entry is timestamped and linked to its
source order/audit event.

Capital events:
  - buy:          cash out (negative cash delta), increases position cost
  - sell:         cash in (positive cash delta), decreases position cost
  - deposit:      cash in from external funding
  - withdrawal:   cash out to external account
  - reverse_repo: cash out (lend money via GC001 etc.), returns next day with interest
  - repo_maturity: cash in from reverse repo principal + interest settlement
  - interest:     cash in from reverse repo or cash account interest
  - fee:          cash out (commission + stamp duty + transfer fee)

All amounts in CNY, rounded to 0.01 (cent precision).
"""

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
CAPITAL_CSV = LEDGER_DIR / "capital_ledger.csv"
CAPITAL_LOCK = CAPITAL_CSV.with_suffix(".csv.lock")
DEFAULT_CAPITAL_LAYER = "shadow"
CAPITAL_LAYERS = {"real", "simulated", "shadow"}

CSV_HEADERS = [
    "entry_id",
    "timestamp",
    "event_type",
    "capital_layer",   # shadow | simulated | real
    "ts_code",
    "quantity",
    "price",
    "amount",
    "fee",
    "order_id",
    "audit_id",
    "note",
]

FEE_CONFIG = {
    "commission_rate": 0.00025,
    "commission_min": 5.0,
    "stamp_duty_rate": 0.0005,
    "transfer_fee_rate": 0.00002,
}


@dataclass
class CapitalEntry:
    """Single capital flow record."""

    event_type: str
    capital_layer: str = DEFAULT_CAPITAL_LAYER
    ts_code: str = "CASH"
    quantity: int = 0
    price: float = 0.0
    amount: float = 0.0
    fee: float = 0.0
    order_id: str = ""
    audit_id: str = ""
    note: str = ""
    entry_id: str = field(default_factory=lambda: f"CAP-{uuid.uuid4().hex[:12]}")
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@contextmanager
def _ledger_lock() -> Iterator[None]:
    """Serialize CSV readers/writers across processes."""
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    with open(CAPITAL_LOCK, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _normalize_capital_layer(value: str | None) -> str:
    layer = str(value or DEFAULT_CAPITAL_LAYER).strip().lower()
    if layer == "sim":
        layer = "simulated"
    if layer in CAPITAL_LAYERS:
        return layer
    raise ValueError(f"capital_layer must be one of real/simulated/shadow, got {value}")


def _normalize_capital_filter(value: str | None) -> str | None:
    if value is None:
        return "real"
    layer = str(value).strip().lower()
    if layer == "all":
        return None
    return _normalize_capital_layer(layer)


def _capital_csv_for_layer(capital_layer: str) -> Path:
    layer = _normalize_capital_layer(capital_layer)
    return CAPITAL_CSV.with_name(f"{CAPITAL_CSV.stem}_{layer}{CAPITAL_CSV.suffix}")


def _read_entries_from_path_unlocked(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path, "r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return [dict(row) for row in reader]


def _write_entries_to_path_unlocked(path: Path, entries: list[dict[str, Any]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADERS)
        writer.writeheader()
        for entry in entries:
            normalized = {key: entry.get(key, "") for key in CSV_HEADERS}
            normalized["capital_layer"] = _normalize_capital_layer(
                entry.get("capital_layer", DEFAULT_CAPITAL_LAYER)
            )
            writer.writerow(normalized)


def _ensure_layer_csv_unlocked(capital_layer: str) -> Path:
    """Create the per-layer CSV if missing and return its path."""
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    path = _capital_csv_for_layer(capital_layer)
    if not path.exists():
        _write_entries_to_path_unlocked(path, [])
    return path


def _migrate_legacy_csv_unlocked() -> None:
    """Copy legacy single-file rows into physical layer files once."""
    if not CAPITAL_CSV.exists():
        return
    legacy_entries = _read_entries_from_path_unlocked(CAPITAL_CSV)
    if not legacy_entries:
        return

    by_layer: dict[str, list[dict[str, Any]]] = {layer: [] for layer in CAPITAL_LAYERS}
    for entry in legacy_entries:
        layer = _normalize_capital_layer(entry.get("capital_layer", DEFAULT_CAPITAL_LAYER))
        normalized = {key: entry.get(key, "") for key in CSV_HEADERS}
        normalized["capital_layer"] = layer
        by_layer[layer].append(normalized)

    for layer, entries in by_layer.items():
        path = _ensure_layer_csv_unlocked(layer)
        existing = _read_entries_from_path_unlocked(path)
        existing_ids = {str(row.get("entry_id", "")) for row in existing if row.get("entry_id")}
        additions = [row for row in entries if str(row.get("entry_id", "")) not in existing_ids]
        if additions:
            _write_entries_to_path_unlocked(path, existing + additions)


def _ensure_csv_unlocked() -> None:
    """Create physical per-layer CSVs and migrate legacy single-file rows."""
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    for layer in CAPITAL_LAYERS:
        _ensure_layer_csv_unlocked(layer)
    _migrate_legacy_csv_unlocked()


def _append(entry: CapitalEntry) -> str:
    """Append a capital entry to CSV. Returns entry_id."""
    row = {
        "entry_id": entry.entry_id,
        "timestamp": entry.timestamp,
        "event_type": entry.event_type,
        "capital_layer": _normalize_capital_layer(entry.capital_layer),
        "ts_code": entry.ts_code,
        "quantity": entry.quantity,
        "price": round(entry.price, 4),
        "amount": round(entry.amount, 2),
        "fee": round(entry.fee, 2),
        "order_id": entry.order_id,
        "audit_id": entry.audit_id,
        "note": entry.note,
    }
    with _ledger_lock():
        _ensure_csv_unlocked()
        layer_path = _capital_csv_for_layer(row["capital_layer"])
        with open(layer_path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_HEADERS)
            writer.writerow(row)
    return entry.entry_id


def _calc_buy_fee(amount: float) -> float:
    commission = max(amount * FEE_CONFIG["commission_rate"], FEE_CONFIG["commission_min"])
    transfer = amount * FEE_CONFIG["transfer_fee_rate"]
    return round(commission + transfer, 2)


def _calc_sell_fee(amount: float) -> float:
    commission = max(amount * FEE_CONFIG["commission_rate"], FEE_CONFIG["commission_min"])
    stamp = amount * FEE_CONFIG["stamp_duty_rate"]
    transfer = amount * FEE_CONFIG["transfer_fee_rate"]
    return round(commission + stamp + transfer, 2)


def record_deposit(
    amount: float,
    date: str,
    capital_layer: str = DEFAULT_CAPITAL_LAYER,
) -> dict[str, Any]:
    if amount <= 0:
        raise ValueError(f"amount must be positive, got {amount}")

    layer = _normalize_capital_layer(capital_layer)
    cash_delta = round(amount, 2)
    entry = CapitalEntry(
        event_type="deposit",
        capital_layer=layer,
        ts_code="CASH",
        amount=cash_delta,
        note="cash deposit",
        timestamp=date,
    )
    eid = _append(entry)
    return {
        "entry_id": eid,
        "amount": cash_delta,
        "cash_delta": cash_delta,
        "capital_layer": layer,
        "timestamp": entry.timestamp,
    }


def record_withdrawal(
    amount: float,
    date: str,
    capital_layer: str = DEFAULT_CAPITAL_LAYER,
) -> dict[str, Any]:
    if amount <= 0:
        raise ValueError(f"amount must be positive, got {amount}")

    layer = _normalize_capital_layer(capital_layer)
    cash_delta = round(-amount, 2)
    entry = CapitalEntry(
        event_type="withdrawal",
        capital_layer=layer,
        ts_code="CASH",
        amount=cash_delta,
        note="cash withdrawal",
        timestamp=date,
    )
    eid = _append(entry)
    return {
        "entry_id": eid,
        "amount": round(amount, 2),
        "cash_delta": cash_delta,
        "capital_layer": layer,
        "timestamp": entry.timestamp,
    }


def record_repo_maturity(
    principal: float,
    rate: float,
    date: str,
    capital_layer: str = DEFAULT_CAPITAL_LAYER,
) -> dict[str, Any]:
    if principal <= 0:
        raise ValueError(f"principal must be positive, got {principal}")
    if rate < 0:
        raise ValueError(f"rate must be non-negative, got {rate}")

    layer = _normalize_capital_layer(capital_layer)
    interest = round(principal * rate / 365.0, 2)
    cash_delta = round(principal + interest, 2)
    entry = CapitalEntry(
        event_type="repo_maturity",
        capital_layer=layer,
        ts_code="CASH",
        price=rate,
        amount=cash_delta,
        note=f"reverse_repo maturity principal={principal} interest={interest}",
        timestamp=date,
    )
    eid = _append(entry)
    return {
        "entry_id": eid,
        "principal": round(principal, 2),
        "interest": interest,
        "cash_delta": cash_delta,
        "capital_layer": layer,
        "timestamp": entry.timestamp,
    }


def record_buy(
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

    layer = _normalize_capital_layer(capital_layer)
    trade_amount = round(quantity * price, 2)
    fee = _calc_buy_fee(trade_amount)
    cash_delta = round(-(trade_amount + fee), 2)

    entry = CapitalEntry(
        event_type="buy",
        capital_layer=layer,
        ts_code=ts_code,
        quantity=quantity,
        price=price,
        amount=cash_delta,
        fee=fee,
        order_id=order_id,
        audit_id=audit_id,
        note=note,
    )
    eid = _append(entry)
    return {
        "entry_id": eid,
        "trade_amount": trade_amount,
        "fee": fee,
        "cash_delta": cash_delta,
        "capital_layer": layer,
        "timestamp": entry.timestamp,
    }


def record_sell(
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

    layer = _normalize_capital_layer(capital_layer)
    trade_amount = round(quantity * price, 2)
    fee = _calc_sell_fee(trade_amount)
    cash_delta = round(trade_amount - fee, 2)

    entry = CapitalEntry(
        event_type="sell",
        capital_layer=layer,
        ts_code=ts_code,
        quantity=quantity,
        price=price,
        amount=cash_delta,
        fee=fee,
        order_id=order_id,
        audit_id=audit_id,
        note=note,
    )
    eid = _append(entry)
    return {
        "entry_id": eid,
        "trade_amount": trade_amount,
        "fee": fee,
        "cash_delta": cash_delta,
        "capital_layer": layer,
        "timestamp": entry.timestamp,
    }


def record_reverse_repo(
    amount: float,
    rate: float,
    order_id: str = "",
    audit_id: str = "",
    note: str = "",
    capital_layer: str = DEFAULT_CAPITAL_LAYER,
) -> dict[str, Any]:
    if amount <= 0:
        raise ValueError(f"amount must be positive, got {amount}")

    layer = _normalize_capital_layer(capital_layer)
    expected_interest = round(amount * rate / 365.0, 2)
    cash_delta = round(-amount, 2)

    entry = CapitalEntry(
        event_type="reverse_repo",
        capital_layer=layer,
        ts_code="CASH",
        quantity=0,
        price=rate,
        amount=cash_delta,
        fee=0.0,
        order_id=order_id,
        audit_id=audit_id,
        note=note or f"reverse_repo rate={rate}",
    )
    eid = _append(entry)
    return {
        "entry_id": eid,
        "principal": amount,
        "expected_interest": expected_interest,
        "cash_delta": cash_delta,
        "capital_layer": layer,
        "timestamp": entry.timestamp,
    }


def record_interest(
    amount: float,
    source: str = "reverse_repo",
    audit_id: str = "",
    note: str = "",
    capital_layer: str = DEFAULT_CAPITAL_LAYER,
) -> dict[str, Any]:
    if amount <= 0:
        raise ValueError(f"amount must be positive, got {amount}")

    layer = _normalize_capital_layer(capital_layer)
    entry = CapitalEntry(
        event_type="interest",
        capital_layer=layer,
        ts_code="CASH",
        quantity=0,
        price=0.0,
        amount=round(amount, 2),
        fee=0.0,
        order_id="",
        audit_id=audit_id,
        note=note or f"interest source={source}",
    )
    eid = _append(entry)
    return {
        "entry_id": eid,
        "amount": round(amount, 2),
        "cash_delta": round(amount, 2),
        "capital_layer": layer,
        "timestamp": entry.timestamp,
    }


def _read_all_entries(capital_layer: str | None = None) -> list[dict[str, Any]]:
    with _ledger_lock():
        _ensure_csv_unlocked()
        layer = _normalize_capital_filter(capital_layer)
        if layer is not None:
            return _read_entries_from_path_unlocked(_capital_csv_for_layer(layer))
        entries: list[dict[str, Any]] = []
        for physical_layer in sorted(CAPITAL_LAYERS):
            entries.extend(_read_entries_from_path_unlocked(_capital_csv_for_layer(physical_layer)))
        return entries


def get_capital_balance(
    as_of: str | None = None,
    capital_layer: str | None = None,
) -> dict[str, Any]:
    layer = _normalize_capital_filter(capital_layer)
    entries = _read_all_entries(capital_layer=capital_layer)
    if as_of:
        entries = [e for e in entries if e["timestamp"] <= as_of]

    total_inflow = sum(float(e["amount"]) for e in entries if float(e["amount"]) > 0)
    total_outflow = sum(abs(float(e["amount"])) for e in entries if float(e["amount"]) < 0)
    balance = round(total_inflow - total_outflow, 2)

    return {
        "balance": balance,
        "total_inflow": round(total_inflow, 2),
        "total_outflow": round(total_outflow, 2),
        "entry_count": len(entries),
        "capital_layer": layer,
        "as_of": as_of,
    }


def get_cash_position(as_of: str | None = None, capital_layer: str | None = None) -> float:
    layer = "real" if capital_layer is None else capital_layer
    return get_capital_balance(as_of=as_of, capital_layer=layer)["balance"]


if __name__ == "__main__":
    r1 = record_buy("600519.SH", 100, 10.00, order_id="TEST-001", note="smoke test buy")
    print("buy:", r1)
    r2 = record_sell("600519.SH", 100, 11.00, order_id="TEST-002", note="smoke test sell")
    print("sell:", r2)
    r3 = record_reverse_repo(10000.0, 0.025, note="GC001 smoke")
    print("repo:", r3)
    r4 = record_interest(r3["expected_interest"], source="reverse_repo")
    print("interest:", r4)
    bal = get_capital_balance()
    print("balance:", bal)
    print("cash:", get_cash_position())
