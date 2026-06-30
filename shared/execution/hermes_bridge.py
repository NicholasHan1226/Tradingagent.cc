#!/usr/bin/env python3
"""Execution bridge for Mac Mini trade handling.

Real-money orders remain signal cards requiring Nicholas manual confirmation.
Simulated orders are sent through the Mac Mini webhook receiver; the Mini-side
sim-signal-receiver and sim-signal-executor own pending, execution, and receipt
writing.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .signal_state_machine import SignalStateConflict, SignalStateMachine
from .webhook_sender import send_sim_signal_to_mini

TRADINGS_ROOT = Path("/opt/investment/Tradings")
SIGNALS_DIR = TRADINGS_ROOT / "signals"
PENDING_DIR = SIGNALS_DIR / "pending"
CLAIMED_DIR = SIGNALS_DIR / "claimed"
RUNNING_DIR = SIGNALS_DIR / "running"
FILLED_DIR = SIGNALS_DIR / "filled"
EXPIRED_DIR = SIGNALS_DIR / "expired"
CANCELLED_DIR = SIGNALS_DIR / "cancelled"
FAILED_DIR = SIGNALS_DIR / "failed"
PARTIAL_DIR = SIGNALS_DIR / "partial"
POSITIONS_DIR = SIGNALS_DIR / "positions"
POSITIONS_FILE = SIGNALS_DIR / "positions.json"
SIGNAL_CARD_SCHEMA_PATH = Path(__file__).resolve().with_name("signal_card_schema.json")

# Hard safety boundary: this bridge only creates signal cards. It must never
# submit, cancel, or confirm real orders directly.
real_auto_order_forbidden = True

_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass
class HermesOrder:
    """Signal card representation consumed by the Mac Mini cron."""

    order_id: str = field(default_factory=lambda: f"SIGNAL-{uuid.uuid4().hex[:12]}")
    ts_code: str = ""
    direction: str = ""  # "buy" | "sell"
    quantity: int = 0
    price: float | None = None
    stop_loss: float | None = None
    strategy_name: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().astimezone().isoformat(timespec="seconds"))
    status: str = "pending"


def ensure_signal_dirs() -> None:
    """Create the file-based bridge directories if they are missing."""
    _state_machine().ensure_dirs()
    POSITIONS_DIR.mkdir(parents=True, exist_ok=True)


def _state_machine() -> SignalStateMachine:
    return SignalStateMachine(SIGNALS_DIR)


def _normalize_order_id(order_id: str) -> str:
    if not order_id or not _ORDER_ID_RE.fullmatch(order_id):
        raise ValueError(f"Invalid order_id: {order_id!r}")
    return order_id


def _json_path(directory: Path, order_id: str) -> Path:
    return directory / f"{_normalize_order_id(order_id)}.json"


def _read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)


def _coerce_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _coerce_signal_card(order: dict[str, Any] | HermesOrder) -> dict[str, Any]:
    if isinstance(order, HermesOrder):
        payload = asdict(order)
    else:
        direction = str(order.get("direction", order.get("side", ""))).lower().strip()
        if direction == "reduce":
            direction = "sell"
        payload = {
            "order_id": str(order.get("order_id") or f"SIGNAL-{uuid.uuid4().hex[:12]}"),
            "ts_code": str(order.get("ts_code", "")).strip(),
            "direction": direction,
            "quantity": int(order.get("quantity", 0)),
            "price": _coerce_float(order.get("price", order.get("limit_price", order.get("execution_price")))),
            "stop_loss": _coerce_float(order.get("stop_loss")),
            "strategy_name": str(order.get("strategy_name", "")).strip(),
            "timestamp": str(order.get("timestamp") or datetime.now().astimezone().isoformat(timespec="seconds")),
            "status": "pending",
        }
        for field_name in (
            "capital_layer",
            "account_type",
            "manual_confirm_required",
            "direct_execution",
            "trigger",
            "evidence_refs",
            "valid_until",
            "risk_check",
            "source_condition_id",
            "idempotency_key",
            "t_plus_1",
            "graduation_receipt",
        ):
            if field_name in order:
                payload[field_name] = order[field_name]

    trigger = payload.get("trigger")
    if not payload.get("source_condition_id") and isinstance(trigger, dict):
        condition_id = trigger.get("condition_id")
        if condition_id:
            payload["source_condition_id"] = str(condition_id)
    if not payload.get("idempotency_key") and payload.get("order_id"):
        payload["idempotency_key"] = str(payload["order_id"])

    payload["order_id"] = _normalize_order_id(str(payload.get("order_id", "")))
    payload["direction"] = str(payload.get("direction", "")).lower().strip()
    payload["status"] = "pending"
    return payload


def _load_signal_card_schema() -> dict[str, Any]:
    with open(SIGNAL_CARD_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _type_matches(value: Any, expected_type: str) -> bool:
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "null":
        return value is None
    return True


def _validate_format(value: Any, expected_format: str | None, path: str) -> str | None:
    if expected_format == "date-time" and isinstance(value, str):
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return f"{path} must be date-time"
    if expected_format == "date" and isinstance(value, str):
        try:
            date.fromisoformat(value)
        except ValueError:
            return f"{path} must be date"
    return None


def _validate_schema_node(value: Any, schema: dict[str, Any], path: str) -> str | None:
    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(_type_matches(value, expected_type) for expected_type in expected):
            return f"{path} has invalid type"
    elif isinstance(expected, str) and not _type_matches(value, expected):
        return f"{path} has invalid type"

    if "enum" in schema and value not in schema["enum"]:
        return f"{path} must be one of {schema['enum']}"
    if "const" in schema and value != schema["const"]:
        return f"{path} must be {schema['const']!r}"
    if isinstance(value, str) and value and "pattern" in schema and not re.fullmatch(schema["pattern"], value):
        return f"{path} does not match required pattern"
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        return f"{path} is too short"
    if isinstance(value, int) and not isinstance(value, bool) and "minimum" in schema and value < schema["minimum"]:
        return f"{path} must be >= {schema['minimum']}"

    format_error = _validate_format(value, schema.get("format"), path)
    if format_error:
        return format_error

    if isinstance(value, dict):
        required = schema.get("required", [])
        for field_name in required:
            if field_name not in value:
                return f"{path} missing required field {field_name}"
        allowed = set(schema.get("properties", {}).keys())
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value.keys()) - allowed)
            if extra:
                return f"{path} has unexpected field {extra[0]}"
        for field_name, field_schema in schema.get("properties", {}).items():
            if field_name in value:
                error = _validate_schema_node(value[field_name], field_schema, f"{path}.{field_name}")
                if error:
                    return error

    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            error = _validate_schema_node(item, schema["items"], f"{path}[{index}]")
            if error:
                return error

    return None


def _validate_signal_card_schema(payload: dict[str, Any]) -> str | None:
    schema = _load_signal_card_schema()
    error = _validate_schema_node(payload, schema, "signal_card")
    if error:
        return error
    if payload.get("capital_layer") == "real":
        if payload.get("manual_confirm_required") is not True:
            return "signal_card.manual_confirm_required must be true for real capital_layer"
        if payload.get("direct_execution") is not False:
            return "signal_card.direct_execution must be false for real capital_layer"
        receipt = payload.get("graduation_receipt")
        if not isinstance(receipt, dict):
            return "signal_card.graduation_receipt is required for real capital_layer"
        if receipt.get("issued_by") != "execution_router":
            return "signal_card.graduation_receipt must be issued_by execution_router"
        if receipt.get("ready") is not True:
            return "signal_card.graduation_receipt.ready must be true"
        if receipt.get("current_stage") != "shadow" or receipt.get("next_stage") != "real":
            return "signal_card.graduation_receipt must prove shadow_to_real graduation"
    return None


def _validate_signal_card(card: dict[str, Any]) -> str | None:
    if not card.get("ts_code"):
        return "Missing ts_code"
    if card.get("direction") not in ("buy", "sell"):
        return f"Invalid direction: {card.get('direction')}"
    if int(card.get("quantity", 0) or 0) <= 0:
        return f"Invalid quantity: {card.get('quantity')}"
    schema_error = _validate_signal_card_schema(card)
    if schema_error:
        return f"Schema validation failed: {schema_error}"
    return None


def webhook_send_signal(order: dict[str, Any] | HermesOrder) -> dict[str, Any]:
    """Send a simulated signal through the Mac Mini webhook channel."""
    result = send_sim_signal_to_mini(order)
    result["capital_layer"] = "simulated"
    result["account_type"] = "simulated"
    result["direct_execution"] = False
    result["real_auto_order_forbidden"] = real_auto_order_forbidden
    return result


def send_order(order: dict[str, Any] | HermesOrder, *, router_authorized: bool = False) -> dict[str, Any]:
    """Queue an execution signal.

    ``capital_layer=simulated`` goes to the Mini webhook channel. Real and
    shadow signal cards keep the legacy file-backed state-machine behavior.

    Args:
        order: dict or HermesOrder with ts_code, direction/side, quantity,
               price/limit_price, stop_loss, strategy_name, and optional order_id.

    Returns:
        dict with order_id, status, signal_path, message, and hard safety flags.
    """
    try:
        card = _coerce_signal_card(order)
    except (TypeError, ValueError) as exc:
        return {
            "order_id": "",
            "status": "rejected",
            "message": str(exc),
            "real_auto_order_forbidden": real_auto_order_forbidden,
            "direct_execution": False,
        }

    validation_error = _validate_signal_card(card)
    if validation_error:
        return {
            "order_id": card.get("order_id", ""),
            "status": "rejected",
            "message": validation_error,
            "real_auto_order_forbidden": real_auto_order_forbidden,
            "direct_execution": False,
        }

    if card.get("capital_layer") == "real" and not router_authorized:
        return {
            "order_id": card.get("order_id", ""),
            "status": "rejected",
            "message": "real capital_layer writes must be routed through execution_router with graduation_receipt",
            "real_auto_order_forbidden": real_auto_order_forbidden,
            "direct_execution": False,
        }

    if card.get("capital_layer") == "simulated":
        result = webhook_send_signal(card)
        result.setdefault("order_id", card["order_id"])
        result.setdefault("message", "Simulated signal sent to Mac Mini webhook")
        return result

    ensure_signal_dirs()

    try:
        queued = _state_machine().write_pending(card)
    except SignalStateConflict as exc:
        existing_path, existing_card = _state_machine().find_by_order_id(card["order_id"])
        status = str(existing_card.get("status", "duplicate")) if existing_card else "duplicate"
        return {
            "order_id": card["order_id"],
            "status": "duplicate",
            "signal_path": str(existing_path) if existing_path else "",
            "existing_status": status,
            "message": str(exc),
            "real_auto_order_forbidden": real_auto_order_forbidden,
            "direct_execution": False,
        }
    except (OSError, ValueError) as exc:
        return {
            "order_id": card["order_id"],
            "status": "rejected",
            "message": f"Unable to queue signal card: {exc}",
            "real_auto_order_forbidden": real_auto_order_forbidden,
            "direct_execution": False,
        }

    return {
        "order_id": card["order_id"],
        "status": "pending",
        "signal_path": queued["signal_path"],
        "signal_card": queued["signal_card"],
        "message": "Signal card queued for Mac Mini cron; no direct execution performed",
        "real_auto_order_forbidden": real_auto_order_forbidden,
        "direct_execution": False,
    }


def claim_signal(order_id: str, worker_id: str | None = None) -> dict[str, Any]:
    """Atomically claim a pending signal for one Mac Mini worker."""
    ensure_signal_dirs()
    try:
        result = _state_machine().claim(order_id, worker_id=worker_id)
    except ValueError as exc:
        return {"order_id": order_id, "status": "rejected", "message": str(exc)}
    except SignalStateConflict as exc:
        return {"order_id": order_id, "status": "conflict", "message": str(exc)}
    except OSError as exc:
        return {"order_id": order_id, "status": "error", "message": str(exc)}
    result["direct_execution"] = False
    result["real_auto_order_forbidden"] = real_auto_order_forbidden
    return result


def run_signal(order_id: str, worker_id: str | None = None) -> dict[str, Any]:
    """Move a claimed signal into running."""
    ensure_signal_dirs()
    try:
        result = _state_machine().mark_running(order_id, worker_id=worker_id)
    except ValueError as exc:
        return {"order_id": order_id, "status": "rejected", "message": str(exc)}
    except FileNotFoundError as exc:
        return {"order_id": order_id, "status": "not_found", "message": str(exc)}
    except SignalStateConflict as exc:
        return {"order_id": order_id, "status": "conflict", "message": str(exc)}
    except OSError as exc:
        return {"order_id": order_id, "status": "error", "message": str(exc)}
    result["direct_execution"] = False
    result["real_auto_order_forbidden"] = real_auto_order_forbidden
    return result


def fill_signal(order_id: str, fill_info: dict[str, Any] | None = None, partial: bool = False) -> dict[str, Any]:
    """Mark a claimed/running signal filled. Filled wins over cancel_requested."""
    ensure_signal_dirs()
    try:
        result = _state_machine().fill(order_id, fill_info=fill_info, partial=partial)
    except ValueError as exc:
        return {"order_id": order_id, "status": "rejected", "message": str(exc)}
    except FileNotFoundError as exc:
        return {"order_id": order_id, "status": "not_found", "message": str(exc)}
    except SignalStateConflict as exc:
        return {"order_id": order_id, "status": "conflict", "message": str(exc)}
    except OSError as exc:
        return {"order_id": order_id, "status": "error", "message": str(exc)}
    result["direct_execution"] = False
    result["real_auto_order_forbidden"] = real_auto_order_forbidden
    return result


def cancel_signal(order_id: str, reason: str = "") -> dict[str, Any]:
    """Cancel pending or request cancellation for claimed/running signals."""
    ensure_signal_dirs()
    try:
        result = _state_machine().cancel(order_id, reason=reason)
    except ValueError as exc:
        return {"order_id": order_id, "status": "rejected", "message": str(exc)}
    except SignalStateConflict as exc:
        return {"order_id": order_id, "status": "conflict", "message": str(exc)}
    except OSError as exc:
        return {"order_id": order_id, "status": "error", "message": str(exc)}
    result["direct_execution"] = False
    result["real_auto_order_forbidden"] = real_auto_order_forbidden
    return result


def sweep_expired_signals(now: datetime | None = None) -> dict[str, Any]:
    """Move expired pending signals to signals/expired/."""
    ensure_signal_dirs()
    try:
        return _state_machine().sweep_expired(now=now)
    except OSError as exc:
        return {"status": "error", "message": str(exc), "expired_count": 0, "expired": []}


def check_fill(order_id: str) -> dict[str, Any]:
    """Read a fill result written by the Mac Mini cron."""
    ensure_signal_dirs()
    try:
        fill_path = _json_path(FILLED_DIR, order_id)
    except ValueError as exc:
        return {
            "order_id": order_id,
            "filled_price": None,
            "filled_quantity": 0,
            "slippage": None,
            "fill_time": None,
            "status": "rejected",
            "message": str(exc),
        }

    partial_path = _json_path(PARTIAL_DIR, order_id)
    if not fill_path.exists() and not partial_path.exists():
        return {
            "order_id": order_id,
            "filled_price": None,
            "filled_quantity": 0,
            "slippage": None,
            "fill_time": None,
            "status": "pending",
            "message": "Fill card not found yet",
        }

    try:
        fill_info = _read_json(fill_path if fill_path.exists() else partial_path)
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "order_id": order_id,
            "filled_price": None,
            "filled_quantity": 0,
            "slippage": None,
            "fill_time": None,
            "status": "error",
            "message": f"Unable to read fill card: {exc}",
        }

    return {
        "order_id": order_id,
        "filled_price": fill_info.get("filled_price"),
        "filled_quantity": int(fill_info.get("filled_quantity", fill_info.get("filled_qty", 0)) or 0),
        "slippage": fill_info.get("slippage"),
        "fill_time": fill_info.get("fill_time", fill_info.get("executed_at")),
        "status": fill_info.get("status", "filled"),
    }


def sync_positions_from_mini() -> dict[str, Any]:
    """Read the latest readonly positions snapshot written by the Mac Mini.

    This function is intentionally read-only. It does not submit, cancel, or
    confirm orders and does not connect to the Mini.
    """
    ensure_signal_dirs()
    if not POSITIONS_DIR.exists():
        return {
            "success": False,
            "status": "missing",
            "positions": [],
            "message": f"Positions directory not found: {POSITIONS_DIR}",
            "direct_execution": False,
            "real_auto_order_forbidden": real_auto_order_forbidden,
        }

    snapshot_paths = sorted(
        (path for path in POSITIONS_DIR.glob("*.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not snapshot_paths:
        return {
            "success": False,
            "status": "missing",
            "positions": [],
            "message": f"No Mini positions snapshot found in {POSITIONS_DIR}",
            "direct_execution": False,
            "real_auto_order_forbidden": real_auto_order_forbidden,
        }

    snapshot_path = snapshot_paths[0]
    try:
        data = _read_json(snapshot_path)
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "success": False,
            "status": "error",
            "positions": [],
            "message": f"Unable to read Mini positions snapshot: {exc}",
            "source_path": str(snapshot_path),
            "direct_execution": False,
            "real_auto_order_forbidden": real_auto_order_forbidden,
        }

    if isinstance(data, dict):
        positions = data.get("positions")
        if positions is None:
            positions = [data] if data.get("ts_code") else []
        return {
            "success": True,
            "status": "ok",
            "positions": positions,
            "snapshot": data,
            "source_path": str(snapshot_path),
            "message": "Positions synced from Mac Mini readonly snapshot",
            "direct_execution": False,
            "real_auto_order_forbidden": real_auto_order_forbidden,
        }

    if isinstance(data, list):
        return {
            "success": True,
            "status": "ok",
            "positions": data,
            "snapshot": {"positions": data},
            "source_path": str(snapshot_path),
            "message": "Positions synced from Mac Mini readonly snapshot",
            "direct_execution": False,
            "real_auto_order_forbidden": real_auto_order_forbidden,
        }

    return {
        "success": False,
        "status": "error",
        "positions": [],
        "message": "Mini positions snapshot must contain a JSON object or list",
        "source_path": str(snapshot_path),
        "direct_execution": False,
        "real_auto_order_forbidden": real_auto_order_forbidden,
    }


def sync_positions() -> dict[str, Any]:
    """Read latest positions snapshot written by the Mac Mini cron."""
    ensure_signal_dirs()
    if not POSITIONS_FILE.exists():
        return {
            "success": False,
            "status": "missing",
            "positions": [],
            "message": f"Positions file not found: {POSITIONS_FILE}",
        }

    try:
        data = _read_json(POSITIONS_FILE)
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "success": False,
            "status": "error",
            "positions": [],
            "message": f"Unable to read positions file: {exc}",
        }

    if isinstance(data, list):
        return {
            "success": True,
            "status": "ok",
            "positions": data,
            "message": "Positions synced from signal bridge file",
            "source_path": str(POSITIONS_FILE),
        }

    if isinstance(data, dict):
        result = dict(data)
        result.setdefault("success", True)
        result.setdefault("status", "ok")
        result.setdefault("positions", data.get("positions", []))
        result.setdefault("message", "Positions synced from signal bridge file")
        result.setdefault("source_path", str(POSITIONS_FILE))
        return result

    return {
        "success": False,
        "status": "error",
        "positions": [],
        "message": "Positions file must contain a JSON object or list",
    }


def cancel_order(order_id: str, manual_confirm: bool = False) -> dict[str, Any]:
    """Cancel a pending signal or request cancellation for claimed/running."""
    ensure_signal_dirs()
    try:
        pending_path = _json_path(PENDING_DIR, order_id)
        claimed_path = _json_path(CLAIMED_DIR, order_id)
        running_path = _json_path(RUNNING_DIR, order_id)
        cancelled_path = _json_path(CANCELLED_DIR, order_id)
        filled_path = _json_path(FILLED_DIR, order_id)
        partial_path = _json_path(PARTIAL_DIR, order_id)
    except ValueError as exc:
        return {"order_id": order_id, "status": "rejected", "message": str(exc)}

    if filled_path.exists() or partial_path.exists():
        return {
            "order_id": order_id,
            "status": "cannot_cancel_filled",
            "message": "Fill card already exists; pending signal cannot be cancelled",
            "direct_execution": False,
            "real_auto_order_forbidden": real_auto_order_forbidden,
        }

    active_path = None
    for candidate_path in (pending_path, claimed_path, running_path):
        if candidate_path.exists():
            active_path = candidate_path
            break
    if active_path is None:
        status = "already_cancelled" if cancelled_path.exists() else "not_found"
        return {
            "order_id": order_id,
            "status": status,
            "message": "Pending signal card not found",
            "direct_execution": False,
            "real_auto_order_forbidden": real_auto_order_forbidden,
        }

    try:
        card = _read_json(active_path)
    except (OSError, json.JSONDecodeError):
        card = {"order_id": order_id}

    capital_layer = str(card.get("capital_layer", "real")).lower().strip()
    if capital_layer not in ("simulated", "shadow") and manual_confirm is not True:
        return {
            "order_id": order_id,
            "status": "rejected",
            "message": "Manual confirmation required to cancel real pending signal",
            "direct_execution": False,
            "real_auto_order_forbidden": real_auto_order_forbidden,
        }

    result = cancel_signal(order_id, reason="manual_cancel")
    if result.get("status") == "cancel_requested":
        return {
            "order_id": order_id,
            "status": "cancel_requested",
            "signal_path": result.get("signal_path", ""),
            "message": result.get("message", "Cancellation requested for claimed/running signal"),
            "direct_execution": False,
            "real_auto_order_forbidden": real_auto_order_forbidden,
        }
    if result.get("status") != "cancelled":
        return result

    return {
        "order_id": order_id,
        "status": "cancelled",
        "signal_path": result.get("signal_path", str(cancelled_path)),
        "message": "Pending signal card moved to cancelled directory",
        "direct_execution": False,
        "real_auto_order_forbidden": real_auto_order_forbidden,
    }
