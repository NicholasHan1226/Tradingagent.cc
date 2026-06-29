#!/usr/bin/env python3
"""Daily reconciliation: system positions vs Hermes (broker) positions.

Compares the position ledger's view of holdings against the broker's
(Hermes/Tonghuashun) actual positions, flagging any discrepancies in
quantity or value.

Reconciliation tolerance:
  - quantity: exact match required (shares are integers)
  - value: within 0.01 CNY (one cent) per position

Output is structured for both automated action and human review.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

LEDGER_DIR = Path(__file__).resolve().parent.parent / "logs"
RECONCILE_LOG = LEDGER_DIR / "reconcile_results.jsonl"

# Tolerance: any value difference > this is a mismatch (in CNY)
VALUE_TOLERANCE = 0.01


def _log_reconcile(result: dict[str, Any]) -> None:
    """Append reconciliation result to JSONL log."""
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now().isoformat(),
        "result": result,
    }
    with open(RECONCILE_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _normalize_positions(
    positions: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Normalize a position list into a dict keyed by ts_code.

    Accepts both system format (from position_ledger.get_positions):
        {"ts_code": str, "quantity": int, "cost_basis": float, "avg_price": float}
    and Hermes format:
        {"ts_code": str, "quantity": int, "market_value": float, ...}

    Returns:
        dict: ts_code -> {"quantity": int, "value": float}
    """
    normalized = {}
    for p in positions:
        ts_code = p.get("ts_code", "")
        if not ts_code:
            continue
        qty = int(p.get("quantity", 0))
        # Prefer market_value if available (Hermes), fall back to cost_basis
        value = float(
            p.get("market_value", p.get("cost_basis", p.get("value", 0.0)))
        )
        normalized[ts_code] = {"quantity": qty, "value": round(value, 2)}
    return normalized


def reconcile(
    system_positions: list[dict[str, Any]],
    hermes_positions: list[dict[str, Any]],
    log: bool = True,
) -> dict[str, Any]:
    """Reconcile system positions vs Hermes (broker) positions.

    Args:
        system_positions: list of position dicts from position_ledger.get_positions().
        hermes_positions: list of position dicts from hermes_bridge.sync_positions().
        log: if True, append result to reconcile_results.jsonl.

    Returns:
        dict with:
            "matched": list of ts_codes that match exactly.
            "mismatches": list of dicts:
                {"ts_code", "system_qty", "hermes_qty", "qty_diff",
                 "system_value", "hermes_value", "value_diff", "type"}
                type is one of: "quantity_diff", "value_diff",
                                "missing_in_system", "missing_in_hermes"
            "actions": list of recommended actions:
                {"ts_code", "action", "detail"}
                action is one of: "investigate", "adjust_system",
                                  "adjust_hermes", "force_sync"
            "summary": {
                "total_system": int,
                "total_hermes": int,
                "matched_count": int,
                "mismatch_count": int,
                "passed": bool,
            }
    """
    sys_norm = _normalize_positions(system_positions)
    herm_norm = _normalize_positions(hermes_positions)

    all_codes = set(sys_norm.keys()) | set(herm_norm.keys())

    matched = []
    mismatches = []
    actions = []

    for ts_code in sorted(all_codes):
        sys_pos = sys_norm.get(ts_code)
        herm_pos = herm_norm.get(ts_code)

        if sys_pos is None and herm_pos is not None:
            # Position exists in Hermes but not in system
            mismatches.append({
                "ts_code": ts_code,
                "system_qty": 0,
                "hermes_qty": herm_pos["quantity"],
                "qty_diff": herm_pos["quantity"],
                "system_value": 0.0,
                "hermes_value": herm_pos["value"],
                "value_diff": round(herm_pos["value"], 2),
                "type": "missing_in_system",
            })
            actions.append({
                "ts_code": ts_code,
                "action": "investigate",
                "detail": f"Position {ts_code} exists in Hermes ({herm_pos['quantity']} shares) "
                          f"but not in system. Check for unrecorded trades.",
            })
            continue

        if herm_pos is None and sys_pos is not None:
            # Position exists in system but not in Hermes
            mismatches.append({
                "ts_code": ts_code,
                "system_qty": sys_pos["quantity"],
                "hermes_qty": 0,
                "qty_diff": -sys_pos["quantity"],
                "system_value": sys_pos["value"],
                "hermes_value": 0.0,
                "value_diff": round(-sys_pos["value"], 2),
                "type": "missing_in_hermes",
            })
            actions.append({
                "ts_code": ts_code,
                "action": "investigate",
                "detail": f"Position {ts_code} exists in system ({sys_pos['quantity']} shares) "
                          f"but not in Hermes. Check for failed/unsent orders.",
            })
            continue

        # Both exist — compare quantity and value
        qty_diff = sys_pos["quantity"] - herm_pos["quantity"]
        value_diff = round(sys_pos["value"] - herm_pos["value"], 2)

        if qty_diff == 0 and abs(value_diff) <= VALUE_TOLERANCE:
            matched.append(ts_code)
        else:
            mismatch_type = "quantity_diff" if qty_diff != 0 else "value_diff"
            mismatches.append({
                "ts_code": ts_code,
                "system_qty": sys_pos["quantity"],
                "hermes_qty": herm_pos["quantity"],
                "qty_diff": qty_diff,
                "system_value": sys_pos["value"],
                "hermes_value": herm_pos["value"],
                "value_diff": value_diff,
                "type": mismatch_type,
            })

            if qty_diff != 0:
                actions.append({
                    "ts_code": ts_code,
                    "action": "force_sync",
                    "detail": f"Quantity mismatch: system={sys_pos['quantity']}, "
                              f"hermes={herm_pos['quantity']}, diff={qty_diff}. "
                              f"Force-sync from Hermes as source of truth.",
                })
            else:
                actions.append({
                    "ts_code": ts_code,
                    "action": "investigate",
                    "detail": f"Value mismatch: system={sys_pos['value']}, "
                              f"hermes={herm_pos['value']}, diff={value_diff}. "
                              f"Check for price/cost basis calculation differences.",
                })

    result = {
        "matched": matched,
        "mismatches": mismatches,
        "actions": actions,
        "summary": {
            "total_system": len(sys_norm),
            "total_hermes": len(herm_norm),
            "matched_count": len(matched),
            "mismatch_count": len(mismatches),
            "passed": len(mismatches) == 0,
        },
    }

    if log:
        _log_reconcile(result)

    return result


# ---- self-test --------------------------------------------------------------

if __name__ == "__main__":
    # Smoke test: 3 positions, 1 matches, 1 qty mismatch, 1 missing in system
    sys_positions = [
        {"ts_code": "600519.SH", "quantity": 100, "cost_basis": 100000.00},
        {"ts_code": "000001.SZ", "quantity": 200, "cost_basis": 3000.00},
        {"ts_code": "300750.SZ", "quantity": 50, "cost_basis": 15000.00},
    ]
    hermes_positions = [
        {"ts_code": "600519.SH", "quantity": 100, "market_value": 100000.00},
        {"ts_code": "000001.SZ", "quantity": 150, "market_value": 2250.00},
        {"ts_code": "688981.SH", "quantity": 30, "market_value": 6000.00},
    ]

    result = reconcile(sys_positions, hermes_positions, log=False)
    print(json.dumps(result, indent=2, ensure_ascii=False))
