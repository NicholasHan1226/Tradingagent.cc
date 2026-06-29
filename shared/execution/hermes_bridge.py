#!/usr/bin/env python3
"""Signal-card execution bridge for Mac Mini trade handling.

The server never connects to the Mac Mini and never performs direct trade
execution. It writes pending signal cards to disk. A separate Mac Mini cron job
pulls those cards, runs the local simulated/desktop executor, and writes fill
and position results back to the shared signals directory.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

TRADINGS_ROOT = Path("/opt/investment/Tradings")
SIGNALS_DIR = TRADINGS_ROOT / "signals"
PENDING_DIR = SIGNALS_DIR / "pending"
FILLED_DIR = SIGNALS_DIR / "filled"
CANCELLED_DIR = SIGNALS_DIR / "cancelled"
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
    for directory in (PENDING_DIR, FILLED_DIR, CANCELLED_DIR, POSITIONS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


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
        ):
            if field_name in order:
                payload[field_name] = order[field_name]

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


def send_order(order: dict[str, Any] | HermesOrder) -> dict[str, Any]:
    """Write a pending signal card for the Mac Mini cron to consume.

    Args:
        order: dict or HermesOrder with ts_code, direction/side, quantity,
               price/limit_price, stop_loss, strategy_name, and optional order_id.

    Returns:
        dict with order_id, status, signal_path, message, and hard safety flags.
    """
    ensure_signal_dirs()

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

    pending_path = _json_path(PENDING_DIR, card["order_id"])
    if pending_path.exists():
        return {
            "order_id": card["order_id"],
            "status": "duplicate",
            "signal_path": str(pending_path),
            "message": "Pending signal card already exists",
            "real_auto_order_forbidden": real_auto_order_forbidden,
            "direct_execution": False,
        }

    _write_json_atomic(pending_path, card)
    return {
        "order_id": card["order_id"],
        "status": "pending",
        "signal_path": str(pending_path),
        "signal_card": card,
        "message": "Signal card queued for Mac Mini cron; no direct execution performed",
        "real_auto_order_forbidden": real_auto_order_forbidden,
        "direct_execution": False,
    }


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

    if not fill_path.exists():
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
        fill_info = _read_json(fill_path)
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
        "filled_quantity": int(fill_info.get("filled_quantity", 0) or 0),
        "slippage": fill_info.get("slippage"),
        "fill_time": fill_info.get("fill_time"),
        "status": fill_info.get("status", "filled"),
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
    """Cancel a pending signal by moving it to signals/cancelled/."""
    ensure_signal_dirs()
    try:
        pending_path = _json_path(PENDING_DIR, order_id)
        cancelled_path = _json_path(CANCELLED_DIR, order_id)
        filled_path = _json_path(FILLED_DIR, order_id)
    except ValueError as exc:
        return {"order_id": order_id, "status": "rejected", "message": str(exc)}

    if filled_path.exists():
        return {
            "order_id": order_id,
            "status": "cannot_cancel_filled",
            "message": "Fill card already exists; pending signal cannot be cancelled",
            "direct_execution": False,
            "real_auto_order_forbidden": real_auto_order_forbidden,
        }

    if not pending_path.exists():
        status = "already_cancelled" if cancelled_path.exists() else "not_found"
        return {
            "order_id": order_id,
            "status": status,
            "message": "Pending signal card not found",
            "direct_execution": False,
            "real_auto_order_forbidden": real_auto_order_forbidden,
        }

    try:
        card = _read_json(pending_path)
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

    card["status"] = "cancelled"
    card["cancelled_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _write_json_atomic(cancelled_path, card)
    pending_path.unlink()

    return {
        "order_id": order_id,
        "status": "cancelled",
        "signal_path": str(cancelled_path),
        "message": "Pending signal card moved to cancelled directory",
        "direct_execution": False,
        "real_auto_order_forbidden": real_auto_order_forbidden,
    }
