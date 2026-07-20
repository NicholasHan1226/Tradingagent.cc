"""Canonical A-share simulated-execution lineage contract.

The fresh execution lineage is deliberately unrelated to the retired numeric
account epochs.  This module is pure validation: importing it never creates or
mutates runtime state.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Mapping

from shared.capital.market_policy import MarketPolicy


ASHARE_CAPITAL_AUTHORITY_ID = "ashare-capital-v1"
# Compatibility export for callers that still need the configured bootstrap
# generation.  Runtime lineage builders accept the current snapshot generation
# explicitly and never compare it to this import-time value.
ASHARE_AUTHORITY_GENERATION = MarketPolicy.load("ashare").authority_generation
ASHARE_EXECUTION_LINEAGE_ID = "ashare-sim-fresh-20260712-v1"
EXECUTION_LINEAGE_SCHEMA_VERSION = "2026-07-12.ashare-execution-lineage.v1"

LEGACY_AUTHORITY_FIELDS = frozenset(
    {
        "capital_epoch",
        "epoch_id",
        "current_epoch_id",
        "previous_epoch_id",
        "epoch_label",
        "previous_epoch_label",
        "cutover_timestamp",
        "epoch_cutover_timestamp",
        "master_capital_required",
        "master_capital_reference_id",
        "master_capital_reservation_id",
        "master_capital_event_id",
        "master_reserved_gross_cny",
        "master_retained_gross_cny",
        "master_release_allocations",
    }
)


class ExecutionLineageError(ValueError):
    """Raised when a record is not part of the fresh execution lineage."""


def _aware_timestamp(value: Any, *, field: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ExecutionLineageError(f"missing_{field}")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExecutionLineageError(f"invalid_{field}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExecutionLineageError(f"timezone_required_{field}")
    return parsed.isoformat(timespec="seconds")


def _lineage_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _positive_authority_generation(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ExecutionLineageError("authority_generation_mismatch")
    return value


def build_execution_lineage(
    *,
    lineage_started_at: Any,
    point_in_time_as_of: Any,
    authority_generation: Any = None,
) -> dict[str, Any]:
    started = _aware_timestamp(lineage_started_at, field="lineage_started_at")
    as_of = _aware_timestamp(point_in_time_as_of, field="point_in_time_as_of")
    if datetime.fromisoformat(as_of) < datetime.fromisoformat(started):
        raise ExecutionLineageError("point_in_time_before_lineage_start")
    generation = _positive_authority_generation(
        MarketPolicy.load("ashare").authority_generation
        if authority_generation is None
        else authority_generation
    )
    payload = {
        "schema_version": EXECUTION_LINEAGE_SCHEMA_VERSION,
        "capital_authority_id": ASHARE_CAPITAL_AUTHORITY_ID,
        "authority_generation": generation,
        "execution_lineage_id": ASHARE_EXECUTION_LINEAGE_ID,
        "lineage_started_at": started,
        "point_in_time_as_of": as_of,
    }
    payload["execution_lineage_sha256"] = _lineage_sha256(payload)
    return payload


def require_execution_lineage(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ExecutionLineageError("execution_lineage_mapping_required")
    forbidden = sorted(LEGACY_AUTHORITY_FIELDS.intersection(payload))
    if forbidden:
        raise ExecutionLineageError(
            "legacy_numeric_epoch_forbidden"
            if any("epoch" in field for field in forbidden)
            else "legacy_master_capital_forbidden"
        )
    generation = _positive_authority_generation(payload.get("authority_generation"))
    normalized = build_execution_lineage(
        lineage_started_at=payload.get("lineage_started_at"),
        point_in_time_as_of=payload.get("point_in_time_as_of"),
        authority_generation=generation,
    )
    for field in (
        "schema_version",
        "capital_authority_id",
        "authority_generation",
        "execution_lineage_id",
        "execution_lineage_sha256",
    ):
        if payload.get(field) != normalized[field]:
            raise ExecutionLineageError(f"{field}_mismatch")
    return normalized


__all__ = [
    "ASHARE_AUTHORITY_GENERATION",
    "ASHARE_CAPITAL_AUTHORITY_ID",
    "ASHARE_EXECUTION_LINEAGE_ID",
    "EXECUTION_LINEAGE_SCHEMA_VERSION",
    "ExecutionLineageError",
    "LEGACY_AUTHORITY_FIELDS",
    "build_execution_lineage",
    "require_execution_lineage",
]
