#!/usr/bin/env python3
"""Append-only audit trail: signal -> decision -> risk -> execution -> result.

Every trade lifecycle event is recorded as an immutable JSONL entry.
This file is NEVER modified or deleted — it is the single source of
truth for post-hoc investigation and compliance.

Audit event stages:
  1. signal     — screening system emitted a buy/sell signal
  2. decision   — adversarial debate + portfolio decision (accept/reject/modify)
  3. risk       — pre-trade risk check (pass/fail/de-weighted)
  4. execution  — order routed to sim/shadow/real, fill received
  5. result     — trade closed or position updated, P&L recorded

Each event links to the previous via audit_id, forming a complete chain.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

LEDGER_DIR = Path(__file__).resolve().parent.parent / "logs"
AUDIT_TRAIL = LEDGER_DIR / "trade_audit_trail.jsonl"

VALID_STAGES = {"signal", "decision", "risk", "execution", "result"}


def _ensure_file() -> None:
    """Ensure the audit trail file exists."""
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    if not AUDIT_TRAIL.exists():
        # Create empty file — no header needed for JSONL
        AUDIT_TRAIL.touch()


def _append_event(event: dict[str, Any]) -> str:
    """Append an event to the JSONL audit trail. Returns audit_id."""
    _ensure_file()
    with open(AUDIT_TRAIL, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event["audit_id"]


def record_event(
    stage: str,
    ts_code: str = "",
    signal_data: dict[str, Any] | None = None,
    decision_data: dict[str, Any] | None = None,
    risk_data: dict[str, Any] | None = None,
    execution_data: dict[str, Any] | None = None,
    result_data: dict[str, Any] | None = None,
    parent_audit_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record a single audit event in the trade lifecycle.

    The caller provides the stage and the relevant data payload. Only the
    data dict matching the stage is required; others are optional context.

    Args:
        stage: one of "signal", "decision", "risk", "execution", "result".
        ts_code: stock code this event relates to (may be empty for portfolio-level).
        signal_data: signal payload (score, dimensions, trigger conditions).
        decision_data: decision payload (action, conviction, debate summary).
        risk_data: risk check payload (checks, verdict, adjustments).
        execution_data: execution payload (order_id, channel, fill, slippage).
        result_data: result payload (realized_pnl, position update, fees).
        parent_audit_id: audit_id of the preceding event in this trade chain.
        metadata: any additional free-form context.

    Returns:
        dict with: audit_id, stage, ts_code, timestamp, parent_audit_id,
        and the relevant data payload.

    Raises:
        ValueError: if stage is not one of VALID_STAGES.
    """
    stage_lower = stage.lower().strip()
    if stage_lower not in VALID_STAGES:
        raise ValueError(
            f"Invalid stage '{stage}'. Must be one of {VALID_STAGES}"
        )

    audit_id = f"AUDIT-{uuid.uuid4().hex[:16]}"
    timestamp = datetime.now().isoformat()

    event: dict[str, Any] = {
        "audit_id": audit_id,
        "timestamp": timestamp,
        "stage": stage_lower,
        "ts_code": ts_code,
        "parent_audit_id": parent_audit_id,
    }

    # Attach the relevant data payload
    if signal_data is not None:
        event["signal_data"] = signal_data
    if decision_data is not None:
        event["decision_data"] = decision_data
    if risk_data is not None:
        event["risk_data"] = risk_data
    if execution_data is not None:
        event["execution_data"] = execution_data
    if result_data is not None:
        event["result_data"] = result_data
    if metadata is not None:
        event["metadata"] = metadata

    _append_event(event)

    return {
        "audit_id": audit_id,
        "stage": stage_lower,
        "ts_code": ts_code,
        "timestamp": timestamp,
        "parent_audit_id": parent_audit_id,
    }


def get_trade_chain(audit_id: str) -> list[dict[str, Any]]:
    """Reconstruct the full trade chain for a given audit_id.

    Walks backwards via parent_audit_id, then forwards to find children.
    Returns the chain in chronological order.

    Args:
        audit_id: any audit_id within the chain.

    Returns:
        List of audit events in chronological order.
    """
    if not AUDIT_TRAIL.exists():
        return []

    all_events = []
    with open(AUDIT_TRAIL, "r", encoding="utf-8") as fh:
        for line in fh:
            try:
                all_events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # Find the starting event
    start = None
    for e in all_events:
        if e.get("audit_id") == audit_id:
            start = e
            break

    if start is None:
        return []

    # Walk backwards to find the root (chain head)
    chain_by_id = {e["audit_id"]: e for e in all_events}
    root = start
    while root.get("parent_audit_id"):
        parent_id = root["parent_audit_id"]
        if parent_id in chain_by_id:
            root = chain_by_id[parent_id]
        else:
            break

    # Walk forwards from root, following children
    ordered = [root]
    current_id = root["audit_id"]
    changed = True
    while changed:
        changed = False
        for e in all_events:
            if e.get("parent_audit_id") == current_id and e["audit_id"] not in [x["audit_id"] for x in ordered]:
                ordered.append(e)
                current_id = e["audit_id"]
                changed = True
                break

    return ordered


def query_events(
    stage: str | None = None,
    ts_code: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Query audit events with optional filters.

    Args:
        stage: filter by stage (signal/decision/risk/execution/result).
        ts_code: filter by stock code.
        start_time: ISO timestamp lower bound (inclusive).
        end_time: ISO timestamp upper bound (inclusive).
        limit: max number of events to return.

    Returns:
        List of matching audit events, most recent first.
    """
    if not AUDIT_TRAIL.exists():
        return []

    results = []
    with open(AUDIT_TRAIL, "r", encoding="utf-8") as fh:
        for line in fh:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if stage and event.get("stage") != stage.lower():
                continue
            if ts_code and event.get("ts_code") != ts_code:
                continue
            ts = event.get("timestamp", "")
            if start_time and ts < start_time:
                continue
            if end_time and ts > end_time:
                continue

            results.append(event)

            if len(results) >= limit:
                break

    return results


# ---- self-test --------------------------------------------------------------

if __name__ == "__main__":
    # Smoke test: full signal -> decision -> risk -> execution -> result chain
    s = record_event(
        "signal",
        ts_code="600519.SH",
        signal_data={"score": 7.5, "trigger": "momentum_breakout", "dimensions": {"technical": 8, "fundamental": 7}},
    )
    print("signal:", s)

    d = record_event(
        "decision",
        ts_code="600519.SH",
        parent_audit_id=s["audit_id"],
        decision_data={"action": "buy", "conviction": 0.8, "debate": "bull wins on momentum"},
    )
    print("decision:", d)

    r = record_event(
        "risk",
        ts_code="600519.SH",
        parent_audit_id=d["audit_id"],
        risk_data={"verdict": "pass", "position_pct": 5.0, "checks": {"single_stock": "ok"}},
    )
    print("risk:", r)

    e = record_event(
        "execution",
        ts_code="600519.SH",
        parent_audit_id=r["audit_id"],
        execution_data={"order_id": "ORD-001", "channel": "sim", "fill_price": 1000.50, "slippage": 0.05},
    )
    print("execution:", e)

    res = record_event(
        "result",
        ts_code="600519.SH",
        parent_audit_id=e["audit_id"],
        result_data={"quantity": 100, "avg_price": 1000.50, "fees": 5.25, "status": "filled"},
    )
    print("result:", res)

    # Reconstruct the chain
    chain = get_trade_chain(res["audit_id"])
    print(f"\nTrade chain ({len(chain)} events):")
    for evt in chain:
        print(f"  {evt['stage']:12s} {evt['audit_id']} -> {evt.get('parent_audit_id', '(root)')}")
