#!/usr/bin/env python3
"""Append-only CN futures order events and read-only directory reconciliation.

The current local simulator is IOC-like: both ``filled`` and ``partial`` are
terminal directory projections.  The event contract nevertheless carries an
explicit ``terminal`` flag so a future asynchronous adapter can represent an
active or reducing partial order without redefining historical events.  No
broker, email, or live route is implemented here.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


SCHEMA_VERSION = "cn-futures-order-event.v1"
PROJECTION_SCHEMA_VERSION = "cn-futures-order-projection.v1"
JOURNAL_FILENAME = "cn_futures_order_events.jsonl"
PROJECTION_FILENAME = "cn_futures_order_projection.json"
LOCK_FILENAME = ".cn_futures_order_events.lock"
LOCAL_SIM_EXECUTION_MODEL = "local_ioc_sim.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DIRECTORY_STATES = (
    "pending",
    "claimed",
    "running",
    "filled",
    "partial",
    "cancelled",
    "expired",
    "failed",
)
_EVENT_STATUS = {
    "submitted": "pending",
    "claimed": "claimed",
    "running": "running",
    "fill": "filled",
    "partial_fill": "partial",
    "cancelled": "cancelled",
    "expired": "expired",
    "failed": "failed",
    "projection_bootstrap": None,
}
_TERMINAL_STATUSES = {"filled", "cancelled", "expired", "failed"}
_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class OrderEventError(RuntimeError):
    """Raised when the immutable order-event contract is violated."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _event_checksum(event: Mapping[str, Any]) -> str:
    return _canonical_sha256(
        {key: value for key, value in event.items() if key != "event_checksum"}
    )


def _aware_timestamp(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise OrderEventError("order_event_time_required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OrderEventError("order_event_time_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OrderEventError("order_event_time_timezone_required")
    return parsed.isoformat(timespec="seconds")


def _nonnegative_number(value: Any, *, field: str) -> float:
    if isinstance(value, bool):
        raise OrderEventError(f"{field}_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise OrderEventError(f"{field}_invalid") from exc
    if not math.isfinite(result) or result < 0:
        raise OrderEventError(f"{field}_invalid")
    return result


def _paths(signals_dir: Path | str) -> tuple[Path, Path, Path]:
    root = Path(signals_dir) / "order_events"
    return (
        root / JOURNAL_FILENAME,
        root / PROJECTION_FILENAME,
        root / LOCK_FILENAME,
    )


def order_event_journal_path(signals_dir: Path | str) -> Path:
    return _paths(signals_dir)[0]


def order_event_projection_path(signals_dir: Path | str) -> Path:
    return _paths(signals_dir)[1]


@contextmanager
def _locked(signals_dir: Path | str) -> Iterator[None]:
    journal, _, lock = _paths(signals_dir)
    journal.parent.mkdir(parents=True, exist_ok=True)
    if journal.parent.is_symlink():
        raise OrderEventError("order_event_root_symlink_forbidden")
    with lock.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_events_unlocked(signals_dir: Path | str) -> list[dict[str, Any]]:
    journal = order_event_journal_path(signals_dir)
    if not journal.exists():
        return []
    if journal.is_symlink():
        raise OrderEventError("order_event_journal_symlink_forbidden")
    events: list[dict[str, Any]] = []
    previous_checksum = ""
    for line_number, line in enumerate(
        journal.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OrderEventError("order_event_json_invalid") from exc
        if not isinstance(event, dict):
            raise OrderEventError("order_event_row_invalid")
        if event.get("schema_version") != SCHEMA_VERSION:
            raise OrderEventError("order_event_schema_invalid")
        if event.get("event_sequence") != len(events) + 1:
            raise OrderEventError("order_event_sequence_invalid")
        if str(event.get("previous_event_checksum") or "") != previous_checksum:
            raise OrderEventError("order_event_chain_mismatch")
        checksum = str(event.get("event_checksum") or "")
        if not _SHA256_RE.fullmatch(checksum):
            raise OrderEventError("order_event_checksum_invalid")
        if checksum != _event_checksum(event):
            raise OrderEventError("order_event_checksum_mismatch")
        if event.get("real_trading_enabled") is not False:
            raise OrderEventError("order_event_sim_only_required")
        previous_checksum = checksum
        events.append(event)
    return events


def _lifecycle_state(order: Mapping[str, Any]) -> str:
    if order.get("terminal") is True:
        return "TERMINAL"
    intent = str(order.get("order_intent") or "").strip().lower()
    if intent in {"reduce_only", "close", "flatten_no_overnight"}:
        return "REDUCING"
    return "ACTIVE"


def _project_events(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    orders: dict[str, dict[str, Any]] = {}
    for event in events:
        order_id = str(event.get("order_id") or "")
        row = dict(orders.get(order_id) or {})
        row.update(
            {
                "order_id": order_id,
                "symbol": str(event.get("symbol") or row.get("symbol") or ""),
                "side": str(event.get("side") or row.get("side") or ""),
                "order_intent": str(
                    event.get("order_intent") or row.get("order_intent") or "open"
                ),
                "execution_model": str(
                    event.get("execution_model") or row.get("execution_model") or ""
                ),
                "status": str(event.get("status") or ""),
                "terminal": event.get("terminal") is True,
                "latest_event_id": str(event.get("event_id") or ""),
                "latest_event_sequence": int(event.get("event_sequence") or 0),
                "latest_event_checksum": str(event.get("event_checksum") or ""),
                "latest_event_time": str(event.get("event_time") or ""),
                "capital_layer": "simulated",
                "account_type": "simulated",
                "real_trading_enabled": False,
                "promotion_evidence_eligible": False,
            }
        )
        if event.get("event_type") in {"fill", "partial_fill"}:
            row["filled_quantity"] = int(row.get("filled_quantity") or 0) + int(
                event.get("filled_quantity_delta") or 0
            )
            row["fill_price"] = float(event.get("fill_price") or 0.0)
            row["fee"] = round(
                float(row.get("fee") or 0.0) + float(event.get("fee") or 0.0),
                6,
            )
        else:
            row.setdefault("filled_quantity", 0)
            row.setdefault("fill_price", 0.0)
            row.setdefault("fee", 0.0)
        row["lifecycle_state"] = _lifecycle_state(row)
        orders[order_id] = row
    payload: dict[str, Any] = {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "market": "cn_futures",
        "authority": "append_only_order_events",
        "event_count": len(events),
        "as_of_event_checksum": (
            str(events[-1].get("event_checksum") or "") if events else ""
        ),
        "generated_at": (str(events[-1].get("event_time") or "") if events else ""),
        "orders": orders,
        "real_trading_enabled": False,
    }
    payload["projection_sha256"] = _canonical_sha256(payload)
    return payload


def _write_projection_unlocked(
    signals_dir: Path | str,
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    projection = _project_events(events)
    path = order_event_projection_path(signals_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(projection, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    return projection


def _validate_transition(
    previous: Mapping[str, Any] | None,
    event: Mapping[str, Any],
) -> None:
    event_type = str(event.get("event_type") or "")
    status = str(event.get("status") or "")
    if event_type not in _EVENT_STATUS:
        raise OrderEventError("order_event_type_invalid")
    expected_status = _EVENT_STATUS[event_type]
    if expected_status is not None and status != expected_status:
        raise OrderEventError("order_event_status_mismatch")
    terminal = event.get("terminal")
    if not isinstance(terminal, bool):
        raise OrderEventError("order_event_terminal_required")
    if status in _TERMINAL_STATUSES and not terminal:
        raise OrderEventError("order_event_terminal_status_required")
    if status in {"pending", "claimed", "running"} and terminal:
        raise OrderEventError("order_event_active_status_cannot_be_terminal")
    if (
        status == "partial"
        and str(event.get("execution_model") or "") == LOCAL_SIM_EXECUTION_MODEL
        and not terminal
    ):
        raise OrderEventError("local_sim_partial_must_be_terminal")

    previous_status = str((previous or {}).get("status") or "")
    previous_terminal = (previous or {}).get("terminal") is True
    if previous_terminal:
        raise OrderEventError("order_event_after_terminal_forbidden")
    allowed: dict[str, set[str]] = {
        "": {"pending"},
        "pending": {"claimed", "cancelled", "expired", "failed"},
        "claimed": {"running", "cancelled", "failed"},
        "running": {"filled", "partial", "cancelled", "expired", "failed"},
        "partial": {"partial", "filled", "cancelled", "expired", "failed"},
    }
    if status not in allowed.get(previous_status, set()):
        raise OrderEventError("order_event_transition_invalid")


def append_order_events(
    signals_dir: Path | str,
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Append one validated lifecycle batch and rebuild its projection."""

    if not events:
        raise OrderEventError("order_event_batch_empty")
    with _locked(signals_dir):
        existing = _read_events_unlocked(signals_dir)
        projected = _project_events(existing)
        orders = dict(projected.get("orders") or {})
        previous_checksum = (
            str(existing[-1].get("event_checksum") or "") if existing else ""
        )
        order_sequences: dict[str, int] = {}
        for existing_event in existing:
            existing_order_id = str(existing_event.get("order_id") or "")
            order_sequences[existing_order_id] = max(
                order_sequences.get(existing_order_id, 0),
                int(existing_event.get("order_sequence") or 0),
            )
        appended: list[dict[str, Any]] = []
        for raw_event in events:
            event = dict(raw_event)
            order_id = str(event.get("order_id") or "").strip()
            if not _ORDER_ID_RE.fullmatch(order_id):
                raise OrderEventError("order_event_order_id_invalid")
            if str(event.get("capital_layer") or "") != "simulated":
                raise OrderEventError("order_event_capital_layer_invalid")
            if str(event.get("account_type") or "") != "simulated":
                raise OrderEventError("order_event_account_type_invalid")
            if event.get("real_trading_enabled") is not False:
                raise OrderEventError("order_event_sim_only_required")
            event["event_time"] = _aware_timestamp(event.get("event_time"))
            if event.get("event_type") in {"fill", "partial_fill"}:
                quantity = _nonnegative_number(
                    event.get("filled_quantity_delta"),
                    field="filled_quantity_delta",
                )
                if not quantity.is_integer() or quantity <= 0:
                    raise OrderEventError("filled_quantity_delta_invalid")
                event["filled_quantity_delta"] = int(quantity)
                fill_price = _nonnegative_number(
                    event.get("fill_price"), field="fill_price"
                )
                if fill_price <= 0:
                    raise OrderEventError("fill_price_invalid")
                event["fill_price"] = fill_price
                event["fee"] = _nonnegative_number(event.get("fee", 0.0), field="fee")
            previous = orders.get(order_id)
            _validate_transition(previous, event)
            order_sequence = order_sequences.get(order_id, 0) + 1
            order_sequences[order_id] = order_sequence
            event.update(
                {
                    "schema_version": SCHEMA_VERSION,
                    "event_sequence": len(existing) + len(appended) + 1,
                    "order_sequence": order_sequence,
                    "previous_event_checksum": previous_checksum,
                    "promotion_evidence_eligible": False,
                }
            )
            event.setdefault(
                "event_id",
                "CNFOE-"
                + _canonical_sha256(
                    {
                        "order_id": order_id,
                        "order_sequence": order_sequence,
                        "event_type": event.get("event_type"),
                        "event_time": event.get("event_time"),
                        "previous_event_checksum": previous_checksum,
                    }
                )[:24],
            )
            event["event_checksum"] = _event_checksum(event)
            previous_checksum = str(event["event_checksum"])
            appended.append(event)
            temporary_projection = _project_events([*existing, *appended])
            orders = dict(temporary_projection.get("orders") or {})

        journal = order_event_journal_path(signals_dir)
        journal.parent.mkdir(parents=True, exist_ok=True)
        with journal.open("a", encoding="utf-8") as handle:
            for event in appended:
                handle.write(
                    json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
                )
            handle.flush()
            os.fsync(handle.fileno())
        all_events = [*existing, *appended]
        projection = _write_projection_unlocked(signals_dir, all_events)
    return {
        "status": "appended",
        "appended_event_count": len(appended),
        "event_count": len(all_events),
        "projection_sha256": projection["projection_sha256"],
        "real_trading_enabled": False,
    }


def load_order_event_projection(signals_dir: Path | str) -> dict[str, Any]:
    with _locked(signals_dir):
        events = _read_events_unlocked(signals_dir)
        rebuilt = _project_events(events)
        path = order_event_projection_path(signals_dir)
        if events and not path.exists():
            raise OrderEventError("order_event_projection_missing")
        if path.exists():
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise OrderEventError("order_event_projection_invalid") from exc
            if not isinstance(stored, dict):
                raise OrderEventError("order_event_projection_invalid")
            checksum = str(stored.get("projection_sha256") or "")
            if checksum != _canonical_sha256(
                {
                    key: value
                    for key, value in stored.items()
                    if key != "projection_sha256"
                }
            ):
                raise OrderEventError("order_event_projection_checksum_mismatch")
            if stored != rebuilt:
                raise OrderEventError("order_event_projection_stale")
        return rebuilt


def _startup_projection_from_event_authority(
    signals_dir: Path | str,
) -> tuple[dict[str, Any], bool]:
    """Rebuild a missing/stale derived projection from a valid journal."""

    with _locked(signals_dir):
        events = _read_events_unlocked(signals_dir)
        rebuilt = _project_events(events)
        path = order_event_projection_path(signals_dir)
        projection_rebuilt = False
        stored_valid = False
        if path.exists():
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                stored = None
            if isinstance(stored, dict):
                checksum = str(stored.get("projection_sha256") or "")
                stored_valid = bool(
                    checksum
                    == _canonical_sha256(
                        {
                            key: value
                            for key, value in stored.items()
                            if key != "projection_sha256"
                        }
                    )
                    and stored == rebuilt
                )
        elif not events:
            stored_valid = True
        if not stored_valid:
            _write_projection_unlocked(signals_dir, events)
            projection_rebuilt = True
        return rebuilt, projection_rebuilt


def _cn_directory_cards(
    signals_dir: Path | str,
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    root = Path(signals_dir)
    cards: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for state in _DIRECTORY_STATES:
        state_dir = root / state
        if not state_dir.exists():
            continue
        for path in sorted(state_dir.glob("*.json")):
            try:
                card = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                duplicates.add(path.stem)
                continue
            if not isinstance(card, dict):
                duplicates.add(path.stem)
                continue
            order_id = str(card.get("order_id") or path.stem)
            market = str(card.get("market") or "").strip().lower()
            if market not in {"cn_futures", "cnfutures"} and not order_id.startswith(
                "SIM-CNF-"
            ):
                continue
            if order_id in cards:
                duplicates.add(order_id)
            card = dict(card)
            card["_directory_state"] = state
            cards[order_id] = card
    return cards, duplicates


def startup_reconcile_order_projection(signals_dir: Path | str) -> dict[str, Any]:
    """Validate the event authority against the legacy status directories.

    A mismatch returns ``HALTED`` for new order execution.  Callers should
    continue recording observation/counterfactual samples and surface the
    mismatch rather than silently treating the directory tree as authority.
    """

    try:
        projection, projection_rebuilt = _startup_projection_from_event_authority(
            signals_dir
        )
    except OrderEventError as exc:
        return {
            "ready": False,
            "state": "HALTED",
            "reason": "order_event_journal_invalid",
            "error": str(exc),
            "mismatch_orders": [],
            "projection_rebuilt": False,
            "real_trading_enabled": False,
        }
    cards, duplicates = _cn_directory_cards(signals_dir)
    projected_orders = dict(projection.get("orders") or {})
    mismatch = set(duplicates)
    mismatch.update(set(cards) ^ set(projected_orders))
    for order_id in set(cards) & set(projected_orders):
        card = cards[order_id]
        expected = projected_orders[order_id]
        status = str(card.get("status") or card.get("_directory_state") or "")
        directory_state = str(card.get("_directory_state") or "")
        quantity = int(card.get("filled_qty") or card.get("filled_quantity") or 0)
        price = float(card.get("filled_price") or card.get("avg_price") or 0.0)
        expected_quantity = int(expected.get("filled_quantity") or 0)
        expected_price = float(expected.get("fill_price") or 0.0)
        sim_only = (
            str(card.get("capital_layer") or "") == "simulated"
            and str(card.get("account_type") or "") == "simulated"
            and card.get("real_trading_enabled") is False
        )
        directory_terminal = directory_state in {
            "filled",
            "partial",
            "cancelled",
            "expired",
            "failed",
        }
        if (
            status != str(expected.get("status") or "")
            or directory_state != status
            or quantity != expected_quantity
            or not math.isclose(price, expected_price, abs_tol=1e-9)
            or directory_terminal != (expected.get("terminal") is True)
            or not sim_only
        ):
            mismatch.add(order_id)
    if mismatch:
        return {
            "ready": False,
            "state": "HALTED",
            "reason": "order_directory_projection_mismatch",
            "mismatch_orders": sorted(mismatch),
            "event_count": int(projection.get("event_count") or 0),
            "projection_sha256": str(projection.get("projection_sha256") or ""),
            "projection_rebuilt": projection_rebuilt,
            "real_trading_enabled": False,
        }
    lifecycle_states = {
        str(row.get("lifecycle_state") or "") for row in projected_orders.values()
    }
    return {
        "ready": True,
        "state": "REDUCING" if "REDUCING" in lifecycle_states else "ACTIVE",
        "reason": "order_projection_reconciled",
        "mismatch_orders": [],
        "event_count": int(projection.get("event_count") or 0),
        "projection_sha256": str(projection.get("projection_sha256") or ""),
        "projection_rebuilt": projection_rebuilt,
        "real_trading_enabled": False,
    }


def record_local_sim_order_lifecycle(
    signals_dir: Path | str,
    *,
    card: Mapping[str, Any],
    receipt: Mapping[str, Any],
    final_card: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist the completed local IOC lifecycle as one append-only batch."""

    for container in (card, receipt, final_card):
        if (
            str(container.get("capital_layer") or "") != "simulated"
            or str(container.get("account_type") or "") != "simulated"
            or container.get("real_trading_enabled") is not False
        ):
            raise OrderEventError("local_sim_lifecycle_sim_only_required")
    status = str(receipt.get("status") or "").strip().lower()
    if status not in {"filled", "partial"}:
        raise OrderEventError("local_sim_fill_status_invalid")
    order_id = str(card.get("order_id") or "")
    common = {
        "order_id": order_id,
        "order_intent": str(card.get("order_intent") or "open"),
        "symbol": str(card.get("symbol") or card.get("ts_code") or ""),
        "side": str(card.get("side") or card.get("direction") or ""),
        "execution_model": LOCAL_SIM_EXECUTION_MODEL,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real_trading_enabled": False,
        "order_snapshot_sha256": _canonical_sha256(dict(card)),
        "directory_projection_sha256": _canonical_sha256(dict(final_card)),
    }
    submitted_at = _aware_timestamp(card.get("timestamp") or _now_iso())
    claimed_at = _aware_timestamp(final_card.get("claimed_at") or submitted_at)
    running_at = _aware_timestamp(final_card.get("running_at") or claimed_at)
    filled_at = _aware_timestamp(
        final_card.get("filled_at")
        or final_card.get("fill_time")
        or receipt.get("filled_at")
        or _now_iso()
    )
    result = append_order_events(
        signals_dir,
        [
            {
                **common,
                "event_type": "submitted",
                "status": "pending",
                "terminal": False,
                "event_time": submitted_at,
            },
            {
                **common,
                "event_type": "claimed",
                "status": "claimed",
                "terminal": False,
                "event_time": claimed_at,
            },
            {
                **common,
                "event_type": "running",
                "status": "running",
                "terminal": False,
                "event_time": running_at,
            },
            {
                **common,
                "event_type": "partial_fill" if status == "partial" else "fill",
                "status": status,
                "terminal": True,
                "filled_quantity_delta": int(receipt.get("filled_qty") or 0),
                "fill_price": float(receipt.get("avg_price") or 0.0),
                "fee": float(receipt.get("fee") or 0.0),
                "receipt_sha256": _canonical_sha256(dict(receipt)),
                "event_time": filled_at,
            },
        ],
    )
    reconcile = startup_reconcile_order_projection(signals_dir)
    if not reconcile.get("ready"):
        raise OrderEventError(
            str(reconcile.get("reason") or "order_projection_unreconciled")
        )
    return {**result, "startup_reconcile": reconcile}


__all__ = [
    "OrderEventError",
    "append_order_events",
    "load_order_event_projection",
    "order_event_journal_path",
    "order_event_projection_path",
    "record_local_sim_order_lifecycle",
    "startup_reconcile_order_projection",
]
