#!/usr/bin/env python3
"""Capital flow ledger: track every cent of fund movement.

Records all capital events: buy, sell, reverse_repo, interest, fee.
CSV storage, append-only. Each entry is timestamped and linked to its
source order/audit event.

Capital events:
  - buy:          cash out (negative cash delta), increases position cost
  - sell:         cash in (positive cash delta), decreases position cost
  - reverse_repo: cash out (lend money via GC001 etc.), returns next day with interest
  - interest:     cash in from reverse repo or cash account interest
  - fee:          cash out (commission + stamp duty + transfer fee)

All amounts in CNY, rounded to 0.01 (cent precision).
"""

from __future__ import annotations

import csv
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

LEDGER_DIR = Path(__file__).resolve().parent.parent / "logs"
CAPITAL_CSV = LEDGER_DIR / "capital_ledger.csv"

CSV_HEADERS = [
    "entry_id",
    "timestamp",
    "event_type",       # buy | sell | reverse_repo | interest | fee
    "ts_code",          # stock code or "CASH" for pure cash events
    "quantity",         # shares (0 for interest/fee/reverse_repo principal)
    "price",            # execution price (0 for interest/fee)
    "amount",           # total cash delta: negative=outflow, positive=inflow
    "fee",              # commission + stamp duty + transfer fee (>=0)
    "order_id",         # link to execution order
    "audit_id",         # link to trade_audit_trail event
    "note",
]

# A-share fee components (configurable, defaults are typical retail rates)
FEE_CONFIG = {
    "commission_rate": 0.00025,       # 0.025% per side
    "commission_min": 5.0,            # minimum 5 CNY per trade
    "stamp_duty_rate": 0.0005,        # 0.05% sell only
    "transfer_fee_rate": 0.00002,     # 0.002% both sides (SSE only, but applied uniformly)
}


@dataclass
class CapitalEntry:
    """Single capital flow record."""

    event_type: str
    ts_code: str = "CASH"
    quantity: int = 0
    price: float = 0.0
    amount: float = 0.0                 # net cash delta (signed)
    fee: float = 0.0
    order_id: str = ""
    audit_id: str = ""
    note: str = ""
    entry_id: str = field(default_factory=lambda: f"CAP-{uuid.uuid4().hex[:12]}")
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


def _ensure_csv() -> None:
    """Create CSV with headers if it does not exist."""
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    if not CAPITAL_CSV.exists():
        with open(CAPITAL_CSV, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_HEADERS)
            writer.writeheader()


def _append(entry: CapitalEntry) -> str:
    """Append a capital entry to CSV. Returns entry_id."""
    _ensure_csv()
    row = {
        "entry_id": entry.entry_id,
        "timestamp": entry.timestamp,
        "event_type": entry.event_type,
        "ts_code": entry.ts_code,
        "quantity": entry.quantity,
        "price": round(entry.price, 4),
        "amount": round(entry.amount, 2),
        "fee": round(entry.fee, 2),
        "order_id": entry.order_id,
        "audit_id": entry.audit_id,
        "note": entry.note,
    }
    with open(CAPITAL_CSV, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADERS)
        writer.writerow(row)
    return entry.entry_id


def _calc_buy_fee(amount: float) -> float:
    """Calculate fee for a buy: commission + transfer fee (no stamp duty)."""
    commission = max(amount * FEE_CONFIG["commission_rate"], FEE_CONFIG["commission_min"])
    transfer = amount * FEE_CONFIG["transfer_fee_rate"]
    return round(commission + transfer, 2)


def _calc_sell_fee(amount: float) -> float:
    """Calculate fee for a sell: commission + stamp duty + transfer fee."""
    commission = max(amount * FEE_CONFIG["commission_rate"], FEE_CONFIG["commission_min"])
    stamp = amount * FEE_CONFIG["stamp_duty_rate"]
    transfer = amount * FEE_CONFIG["transfer_fee_rate"]
    return round(commission + stamp + transfer, 2)


# ---- public API -------------------------------------------------------------

def record_buy(
    ts_code: str,
    quantity: int,
    price: float,
    order_id: str = "",
    audit_id: str = "",
    note: str = "",
) -> dict[str, Any]:
    """Record a buy: cash out = trade amount + fees.

    Args:
        ts_code: stock code, e.g. "600519.SH".
        quantity: shares bought (positive int).
        price: execution price per share.
        order_id: link to execution order.
        audit_id: link to audit trail event.
        note: free-text annotation.

    Returns:
        dict with: entry_id, trade_amount, fee, cash_delta, timestamp.
    """
    if quantity <= 0:
        raise ValueError(f"quantity must be positive, got {quantity}")
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")

    trade_amount = round(quantity * price, 2)
    fee = _calc_buy_fee(trade_amount)
    cash_delta = round(-(trade_amount + fee), 2)  # outflow

    entry = CapitalEntry(
        event_type="buy",
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
        "timestamp": entry.timestamp,
    }


def record_sell(
    ts_code: str,
    quantity: int,
    price: float,
    order_id: str = "",
    audit_id: str = "",
    note: str = "",
) -> dict[str, Any]:
    """Record a sell: cash in = trade amount - fees.

    Args:
        ts_code: stock code.
        quantity: shares sold (positive int).
        price: execution price per share.
        order_id: link to execution order.
        audit_id: link to audit trail event.
        note: free-text annotation.

    Returns:
        dict with: entry_id, trade_amount, fee, cash_delta, timestamp.
    """
    if quantity <= 0:
        raise ValueError(f"quantity must be positive, got {quantity}")
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")

    trade_amount = round(quantity * price, 2)
    fee = _calc_sell_fee(trade_amount)
    cash_delta = round(trade_amount - fee, 2)  # inflow

    entry = CapitalEntry(
        event_type="sell",
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
        "timestamp": entry.timestamp,
    }


def record_reverse_repo(
    amount: float,
    rate: float,
    order_id: str = "",
    audit_id: str = "",
    note: str = "",
) -> dict[str, Any]:
    """Record a reverse repo (e.g. GC001): lend cash, get it back + interest next day.

    Args:
        amount: principal lent (positive float).
        rate: annualized repo rate (e.g. 0.025 = 2.5%).
        order_id: link to execution order.
        audit_id: link to audit trail event.
        note: free-text annotation (e.g. "GC001").

    Returns:
        dict with: entry_id, principal, expected_interest, cash_delta, timestamp.
    """
    if amount <= 0:
        raise ValueError(f"amount must be positive, got {amount}")

    # T+1 settlement, 1-day repo: interest = principal * rate * 1/365
    expected_interest = round(amount * rate / 365.0, 2)
    cash_delta = round(-amount, 2)  # principal outflow today

    entry = CapitalEntry(
        event_type="reverse_repo",
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
        "timestamp": entry.timestamp,
    }


def record_interest(
    amount: float,
    source: str = "reverse_repo",
    audit_id: str = "",
    note: str = "",
) -> dict[str, Any]:
    """Record interest received (from reverse repo settlement or cash account).

    Args:
        amount: interest received (positive float).
        source: "reverse_repo" | "cash_account" | "dividend".
        audit_id: link to audit trail event.
        note: free-text annotation.

    Returns:
        dict with: entry_id, amount, cash_delta, timestamp.
    """
    if amount <= 0:
        raise ValueError(f"amount must be positive, got {amount}")

    entry = CapitalEntry(
        event_type="interest",
        ts_code="CASH",
        quantity=0,
        price=0.0,
        amount=round(amount, 2),  # inflow
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
        "timestamp": entry.timestamp,
    }


def _read_all_entries() -> list[dict[str, Any]]:
    """Read all capital ledger entries."""
    if not CAPITAL_CSV.exists():
        return []
    with open(CAPITAL_CSV, "r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return list(reader)


def get_capital_balance(as_of: str | None = None) -> dict[str, Any]:
    """Get total capital balance (sum of all cash deltas).

    Args:
        as_of: ISO timestamp cutoff (inclusive). None = all entries.

    Returns:
        dict with: balance, total_inflow, total_outflow, entry_count, as_of.
    """
    entries = _read_all_entries()
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
        "as_of": as_of,
    }


def get_cash_position(as_of: str | None = None) -> float:
    """Get current cash position (alias for capital balance).

    This is the liquid cash available for trading. It equals the sum of all
    cash deltas (deposits - withdrawals + sells - buys - fees + interest).

    Args:
        as_of: ISO timestamp cutoff (inclusive). None = all entries.

    Returns:
        Cash position in CNY (float, rounded to cent).
    """
    return get_capital_balance(as_of=as_of)["balance"]


# ---- self-test --------------------------------------------------------------

if __name__ == "__main__":
    # Smoke test: buy 100 shares at 10.00, sell 100 shares at 11.00
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
