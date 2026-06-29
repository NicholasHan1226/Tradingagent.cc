#!/usr/bin/env python3
"""Hermes bridge: server-side command sender to Mac Mini Hermes desktop control.

Hermes runs on the Mac Mini and controls the Tonghuashun (同花顺) client via
desktop automation. This module is the server-side bridge: it sends commands
to the Mac Mini over SSH and reads back fill/position status.

Reference interface: /opt/investment/Ashare/tools/a_share_tonghuashun_execution.py

Real-money boundary: this bridge sends *instructions* to Hermes on the Mac
Mini. Hermes places real orders. Nicholas must confirm real-money orders
manually — the bridge never auto-submits real orders without confirmation.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

# Mac Mini SSH connection config (set via env or config file)
MAC_MINI_HOST = "nicholas@nicholas-mac-mini.local"
HERMES_REMOTE_PATH = "/opt/hermes/hermes_cli.py"
HERMES_STATUS_PATH = "/opt/hermes/hermes_status.py"

# Local ledger for orders sent to Hermes
HERMES_LEDGER = Path(__file__).resolve().parent.parent / "logs" / "hermes_orders.jsonl"


@dataclass
class HermesOrder:
    """Order representation for Hermes bridge."""

    order_id: str = field(default_factory=lambda: f"HERMES-{uuid.uuid4().hex[:12]}")
    ts_code: str = ""               # e.g. "600519.SH"
    side: str = ""                  # "buy" | "sell"
    quantity: int = 0               # shares (must be round lot for A-share)
    order_type: str = "limit"       # "limit" | "market"
    limit_price: float | None = None
    strategy_name: str = ""
    strategy_stage: str = "real"    # "sim" | "shadow" | "real"
    requires_manual_confirm: bool = True
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    status: str = "pending"         # pending | sent | filled | partial | cancelled | rejected
    filled_price: float | None = None
    filled_quantity: int = 0
    error: str = ""


def _ssh_to_mac_mini(command: str, timeout: int = 30) -> dict[str, Any]:
    """Execute a command on the Mac Mini via SSH.

    Returns dict with: success, stdout, stderr, returncode.
    """
    ssh_cmd = [
        "ssh",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        MAC_MINI_HOST,
        command,
    ]
    try:
        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "stdout": "", "stderr": "SSH timeout", "returncode": -1}
    except Exception as exc:
        return {"success": False, "stdout": "", "stderr": str(exc), "returncode": -1}


def _log_order(order: HermesOrder) -> None:
    """Append order to local Hermes ledger."""
    HERMES_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(HERMES_LEDGER, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(order), ensure_ascii=False) + "\n")


def send_order(order: dict[str, Any] | HermesOrder) -> dict[str, Any]:
    """Send an order to Hermes on the Mac Mini.

    Args:
        order: dict or HermesOrder with ts_code, side, quantity, order_type,
               limit_price, strategy_name, strategy_stage.

    Returns:
        dict with: order_id, status, message, hermes_response.
    """
    if isinstance(order, dict):
        order = HermesOrder(
            ts_code=order.get("ts_code", ""),
            side=order.get("side", ""),
            quantity=int(order.get("quantity", 0)),
            order_type=order.get("order_type", "limit"),
            limit_price=order.get("limit_price"),
            strategy_name=order.get("strategy_name", ""),
            strategy_stage=order.get("strategy_stage", "real"),
            requires_manual_confirm=order.get("requires_manual_confirm", True),
        )

    # Validate
    if not order.ts_code:
        return {"order_id": order.order_id, "status": "rejected", "message": "Missing ts_code"}
    if order.side not in ("buy", "sell"):
        return {"order_id": order.order_id, "status": "rejected", "message": f"Invalid side: {order.side}"}
    if order.quantity <= 0 or order.quantity % 100 != 0:
        return {"order_id": order.order_id, "status": "rejected", "message": f"Invalid quantity: {order.quantity} (must be positive round lot)"}
    if order.order_type == "limit" and order.limit_price is None:
        return {"order_id": order.order_id, "status": "rejected", "message": "Limit order requires limit_price"}

    # Real-money orders require manual confirmation
    if order.strategy_stage == "real" and order.requires_manual_confirm:
        order.status = "pending_manual_confirm"
        _log_order(order)
        return {
            "order_id": order.order_id,
            "status": "pending_manual_confirm",
            "message": "Real-money order queued for manual confirmation by Nicholas. Hermes will not auto-submit.",
            "hermes_response": None,
        }

    # Build Hermes CLI command
    hermes_cmd = (
        f"python3 {HERMES_REMOTE_PATH} "
        f"--action send_order "
        f"--order-id {order.order_id} "
        f"--code {order.ts_code} "
        f"--side {order.side} "
        f"--qty {order.quantity} "
        f"--order-type {order.order_type}"
    )
    if order.limit_price is not None:
        hermes_cmd += f" --price {order.limit_price}"

    result = _ssh_to_mac_mini(hermes_cmd, timeout=30)

    if result["success"]:
        order.status = "sent"
        message = "Order sent to Hermes on Mac Mini"
    else:
        order.status = "send_failed"
        message = f"Failed to send order to Hermes: {result[stderr]}"

    _log_order(order)

    return {
        "order_id": order.order_id,
        "status": order.status,
        "message": message,
        "hermes_response": result["stdout"] if result["success"] else result["stderr"],
    }


def check_fill(order_id: str) -> dict[str, Any]:
    """Check fill status of an order from Hermes.

    Args:
        order_id: The Hermes order ID to check.

    Returns:
        dict with: order_id, status, filled_price, filled_quantity, message.
    """
    hermes_cmd = f"python3 {HERMES_STATUS_PATH} --action check_fill --order-id {order_id}"
    result = _ssh_to_mac_mini(hermes_cmd, timeout=15)

    if not result["success"]:
        return {
            "order_id": order_id,
            "status": "unknown",
            "filled_price": None,
            "filled_quantity": 0,
            "message": f"SSH to Mac Mini failed: {result[stderr]}",
        }

    # Parse Hermes response (expects JSON on stdout)
    try:
        fill_info = json.loads(result["stdout"])
        return {
            "order_id": order_id,
            "status": fill_info.get("status", "unknown"),
            "filled_price": fill_info.get("filled_price"),
            "filled_quantity": fill_info.get("filled_quantity", 0),
            "message": fill_info.get("message", ""),
        }
    except (json.JSONDecodeError, ValueError):
        return {
            "order_id": order_id,
            "status": "unknown",
            "filled_price": None,
            "filled_quantity": 0,
            "message": f"Unparseable Hermes response: {result[stdout][:200]}",
        }


def sync_positions() -> dict[str, Any]:
    """Sync positions from Hermes/Tonghuashun on Mac Mini.

    Returns:
        dict with: success, positions (list), message.
    """
    hermes_cmd = f"python3 {HERMES_STATUS_PATH} --action sync_positions"
    result = _ssh_to_mac_mini(hermes_cmd, timeout=30)

    if not result["success"]:
        return {
            "success": False,
            "positions": [],
            "message": f"SSH to Mac Mini failed: {result[stderr]}",
        }

    try:
        data = json.loads(result["stdout"])
        return {
            "success": True,
            "positions": data.get("positions", []),
            "message": data.get("message", "Positions synced"),
        }
    except (json.JSONDecodeError, ValueError):
        return {
            "success": False,
            "positions": [],
            "message": f"Unparseable positions response: {result[stdout][:200]}",
        }


def cancel_order(order_id: str) -> dict[str, Any]:
    """Send cancel request to Hermes (real orders require manual confirmation).

    Args:
        order_id: The Hermes order ID to cancel.

    Returns:
        dict with: order_id, status, message.
    """
    hermes_cmd = f"python3 {HERMES_REMOTE_PATH} --action cancel --order-id {order_id}"
    result = _ssh_to_mac_mini(hermes_cmd, timeout=15)

    status = "cancel_sent" if result["success"] else "cancel_failed"
    return {
        "order_id": order_id,
        "status": status,
        "message": result["stdout"] if result["success"] else result["stderr"],
    }
