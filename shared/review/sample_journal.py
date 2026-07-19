#!/usr/bin/env python3
"""Append-only, sim-only journal for prediction and learning samples.

The journal is deliberately outside the execution path.  It records every
candidate/style prediction before strategy thresholds are considered, keeps
sample layers distinct, and projects immutable label updates into the existing
``sample_kpi`` read model.  It never calls a broker or creates style capital.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import errno
import fcntl
from hashlib import sha256
import hmac
import json
import math
import os
from pathlib import Path
import stat
from time import perf_counter
from typing import Any, Iterator, Mapping, Optional, Sequence, Union

from shared.models.lifecycle import ValidationPlan
from shared.review.forward_labels import (
    PIT_TIMESTAMP_FIELDS,
    build_prediction_snapshot,
    materialize_forward_labels,
    _stable_label_update_id,
    validate_evidence_envelope,
)
from shared.review.sample_kpi import (
    SAMPLE_LAYERS,
    build_sample_kpi,
    classify_sample_layers,
)
from shared.execution.execution_lineage import (
    ASHARE_AUTHORITY_GENERATION,
    ASHARE_CAPITAL_AUTHORITY_ID,
    ASHARE_EXECUTION_LINEAGE_ID,
)


JOURNAL_SCHEMA_VERSION = 2

_LIVE_BOOLEAN_FIELDS = {
    "real_trading_enabled",
    "live_execution_enabled",
    "real_money_enabled",
    "live_broker_enabled",
    "direct_execution_enabled",
    "real_order_enabled",
    "production_execution_enabled",
    "is_live",
}
_LIVE_MODE_FIELDS = {
    "account_type",
    "capital_layer",
    "execution_mode",
    "trading_mode",
}
_TRUE_VALUES = {"1", "true", "yes", "on", "enabled", "live", "real", "production"}
_LIVE_MODE_VALUES = {"live", "real", "production", "real_money"}
_MUTUALLY_EXCLUSIVE_LAYERS = {
    "observation_counterfactual",
    "exploration_fill",
    "exploitation_fill",
    "risk_reject",
    "chain_validation",
}


class JournalError(RuntimeError):
    """Base class for sample-journal failures."""


class JournalConflictError(JournalError):
    """An idempotency identity was reused with different immutable content."""


class JournalSafetyError(JournalError):
    """A live, unsafe-path, malformed, or mixed-layer input was rejected."""


@dataclass(frozen=True)
class FrozenJournalView:
    """One integrity-checked, point-in-time journal input view.

    ``_events`` is intentionally private.  Consumers receive defensive copies,
    while the journal keeps the canonical tuple available for indexed batch
    work without re-reading or re-parsing the JSONL authority.
    """

    data_as_of: str
    journal_head_event_count: int
    journal_head_sha256: str
    max_evidence_available_at: Optional[str]
    excluded_after_as_of_count: int
    journal_source_event_count: int
    journal_source_byte_count: int
    journal_source_sha256: str
    journal_source_inode: Optional[int]
    _events: tuple[dict[str, Any], ...] = field(repr=False)
    _source_hasher: Any = field(repr=False, compare=False)
    _predictions_by_id: Mapping[str, dict[str, Any]] = field(repr=False, compare=False)
    _events_by_id: Mapping[str, dict[str, Any]] = field(repr=False, compare=False)
    _label_updates_by_snapshot: Mapping[str, tuple[dict[str, Any], ...]] = field(
        repr=False, compare=False
    )
    _cost_events_by_snapshot: Mapping[str, tuple[dict[str, Any], ...]] = field(
        repr=False, compare=False
    )

    def copy_events(self) -> list[dict[str, Any]]:
        return deepcopy(list(self._events))

    def prediction(self, snapshot_id: str) -> Optional[dict[str, Any]]:
        value = self._predictions_by_id.get(str(snapshot_id))
        return deepcopy(value) if value is not None else None

    def metadata(self) -> dict[str, Any]:
        return {
            "data_as_of": self.data_as_of,
            "journal_head_event_count": self.journal_head_event_count,
            "journal_head_sha256": self.journal_head_sha256,
            "max_evidence_available_at": self.max_evidence_available_at,
            "excluded_after_as_of_count": self.excluded_after_as_of_count,
            "journal_source_event_count": self.journal_source_event_count,
            "journal_source_byte_count": self.journal_source_byte_count,
            "journal_source_sha256": self.journal_source_sha256,
        }


@dataclass
class _JournalAppendCursor:
    expected_byte_count: int
    expected_file_sha256: str
    expected_inode: Optional[int]
    hasher: Any = field(repr=False)
    task_owned_event_count: int = 0
    task_owned_byte_count: int = 0


@dataclass
class _LockedJournalPaths:
    lock_identity: tuple[int, int]
    journal_identity: Optional[tuple[int, int]]


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        )
    except (TypeError, ValueError) as exc:
        raise JournalSafetyError("sample payload is not canonical JSON: %s" % exc)


def _payload_sha256(value: Mapping[str, Any]) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _events_payload(events: Sequence[Mapping[str, Any]]) -> bytes:
    return "".join(_canonical_json(event) + "\n" for event in events).encode("utf-8")


def _events_sha256(events: Sequence[Mapping[str, Any]]) -> str:
    return sha256(_events_payload(events)).hexdigest()


def _parse_aware_timestamp(
    value: Any, *, field_name: str, line_number: int
) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise JournalSafetyError(
            "journal line %d has no evidence availability/receipt timestamp"
            % line_number
        )
    try:
        parsed = datetime.fromisoformat(raw.replace(" ", "T", 1).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise JournalSafetyError(
            "journal line %d has invalid %s" % (line_number, field_name)
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise JournalSafetyError(
            "journal line %d has timezone-naive %s" % (line_number, field_name)
        )
    return parsed


def _nested_timestamp(record: Mapping[str, Any], *path: str) -> Any:
    current: Any = record
    for part in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _event_evidence_available_at(
    event: Mapping[str, Any], line_number: int
) -> datetime:
    """Return the conservative receipt/availability boundary for one event.

    The latest available receipt/availability timestamp is conservative.  A
    present but invalid timestamp fails closed rather than falling back to an
    earlier value.  Event-type timestamps are used only for append-only facts
    whose timestamp is itself the journal receipt boundary (label updates and
    recorded outcomes).
    """

    event_type = str(event.get("journal_event_type") or "").strip()
    lineage = event.get("point_in_time_lineage")
    if lineage is not None and not isinstance(lineage, Mapping):
        raise JournalSafetyError(
            "journal line %d has invalid point_in_time_lineage" % line_number
        )
    lineage_timestamps = lineage.get("timestamps") if lineage is not None else None
    if lineage_timestamps is not None and not isinstance(lineage_timestamps, Mapping):
        raise JournalSafetyError(
            "journal line %d has invalid point_in_time_lineage.timestamps" % line_number
        )
    candidates: list[tuple[str, Any]] = [
        ("receipt_at", event.get("receipt_at")),
        ("received_at", event.get("received_at")),
        ("retrieved_as_of", event.get("retrieved_as_of")),
        (
            "point_in_time_lineage.timestamps.receipt_at",
            _nested_timestamp(
                event, "point_in_time_lineage", "timestamps", "receipt_at"
            ),
        ),
        (
            "point_in_time_lineage.timestamps.received_at",
            _nested_timestamp(
                event, "point_in_time_lineage", "timestamps", "received_at"
            ),
        ),
        (
            "point_in_time_lineage.timestamps.retrieved_as_of",
            _nested_timestamp(
                event, "point_in_time_lineage", "timestamps", "retrieved_as_of"
            ),
        ),
        (
            "point_in_time_lineage.retrieved_as_of",
            _nested_timestamp(event, "point_in_time_lineage", "retrieved_as_of"),
        ),
        (
            "point_in_time_lineage.receipt_at",
            _nested_timestamp(event, "point_in_time_lineage", "receipt_at"),
        ),
        (
            "point_in_time_lineage.received_at",
            _nested_timestamp(event, "point_in_time_lineage", "received_at"),
        ),
        ("collected_at", event.get("collected_at")),
        ("ingested_at", event.get("ingested_at")),
        ("evidence_available_at", event.get("evidence_available_at")),
        ("available_at", event.get("available_at")),
        (
            "point_in_time_lineage.timestamps.collected_at",
            _nested_timestamp(
                event, "point_in_time_lineage", "timestamps", "collected_at"
            ),
        ),
        (
            "point_in_time_lineage.timestamps.ingested_at",
            _nested_timestamp(
                event, "point_in_time_lineage", "timestamps", "ingested_at"
            ),
        ),
        (
            "point_in_time_lineage.timestamps.evidence_available_at",
            _nested_timestamp(
                event,
                "point_in_time_lineage",
                "timestamps",
                "evidence_available_at",
            ),
        ),
        (
            "point_in_time_lineage.timestamps.available_at",
            _nested_timestamp(
                event, "point_in_time_lineage", "timestamps", "available_at"
            ),
        ),
        (
            "point_in_time_lineage.collected_at",
            _nested_timestamp(event, "point_in_time_lineage", "collected_at"),
        ),
        (
            "point_in_time_lineage.ingested_at",
            _nested_timestamp(event, "point_in_time_lineage", "ingested_at"),
        ),
        (
            "point_in_time_lineage.evidence_available_at",
            _nested_timestamp(event, "point_in_time_lineage", "evidence_available_at"),
        ),
        (
            "point_in_time_lineage.available_at",
            _nested_timestamp(event, "point_in_time_lineage", "available_at"),
        ),
        ("point_in_time_as_of", event.get("point_in_time_as_of")),
    ]
    if event_type == "forward_label_update":
        candidates.append(("labels_as_of", event.get("labels_as_of")))
    elif event_type == "sample_event":
        candidates.extend(
            [
                ("recorded_at", event.get("recorded_at")),
                ("created_at", event.get("created_at")),
                ("event_at", event.get("event_at")),
                ("closed_at", event.get("closed_at")),
                ("timestamp", event.get("timestamp")),
            ]
        )
    parsed_candidates: list[datetime] = []
    for field_name, value in candidates:
        if value not in (None, ""):
            parsed_candidates.append(
                _parse_aware_timestamp(
                    value, field_name=field_name, line_number=line_number
                )
            )
    if parsed_candidates:
        return max(parsed_candidates)
    raise JournalSafetyError(
        "journal line %d has no evidence availability/receipt timestamp" % line_number
    )


def _prediction_content_sha256(value: Mapping[str, Any]) -> str:
    content = deepcopy(dict(value))
    for field_name in (
        "journal_schema_version",
        "journal_payload_sha256",
        "journal_event_type",
        "journal_event_id",
        "sample_cluster_id",
        "cluster_role",
        "maturity_weight",
        "prediction_content_sha256",
    ):
        content.pop(field_name, None)
    return _payload_sha256(content)


def prediction_content_sha256(value: Mapping[str, Any]) -> str:
    """Return the canonical digest of one authoritative prediction event."""

    return _prediction_content_sha256(value)


def prediction_source_payload_sha256(value: Mapping[str, Any]) -> str:
    """Hash the immutable source facts stored with one prediction event."""

    return _payload_sha256(value)


_EXECUTION_RECEIPT_PAYLOAD_FIELDS = (
    "status",
    "fill_identity",
    "filled_quantity",
    "filled_price",
    "fee_cny",
    "slippage_cny",
    "event_time",
    "source_event_time",
    "available_at",
    "ingested_at",
    "retrieved_as_of",
    "evidence_envelope",
    "evidence_envelope_validation",
    "point_in_time_lineage",
)
_EXECUTION_LOCAL_TRADE_PAYLOAD_FIELDS = (
    "record_type",
    "market",
    "symbol",
    "trade_date",
    "fill_identity",
    "entry_fill_identity",
    "execution_eligible",
    "capital_authority_id",
    "authority_generation",
    "execution_lineage_id",
    "prediction_snapshot_id",
    "prediction_source_snapshot_sha256",
    "prediction_content_sha256",
    "receipt_sha256",
)
_ROUND_TRIP_CONTENT_PAYLOAD_FIELDS = (
    "event_id",
    "record_type",
    "round_trip_complete",
    "completed",
    "execution_eligible",
    "costs_cover",
    "cost_model_version",
    "market",
    "symbol",
    "trade_date",
    "closed_at",
    "as_of",
    "point_in_time_as_of",
    "source_snapshot_sha256",
    "sample_intent",
    "entry_fill_identity",
    "exit_fill_identities",
    "entry_receipt_sha256",
    "entry_local_trade_sha256",
    "exit_receipt_sha256s",
    "exit_local_trade_sha256s",
    "entry_quantity",
    "entry_price",
    "notional_cny",
    "gross_pnl_cny",
    "fee_cny",
    "slippage_cny",
    "net_pnl_cny",
    "event_time",
    "source_event_time",
    "available_at",
    "ingested_at",
    "retrieved_as_of",
    "point_in_time_lineage",
    "evidence_envelope",
    "evidence_envelope_validation",
    "capital_authority_id",
    "authority_generation",
    "execution_lineage_id",
    "prediction_snapshot_id",
    "prediction_source_snapshot_sha256",
    "prediction_content_sha256",
    "real_trading_enabled",
)


def canonical_execution_receipt_payload(
    record: Mapping[str, Any],
) -> dict[str, Any]:
    """Project immutable receipt facts from one persisted fill/stop event."""

    return {
        field_name: deepcopy(record.get(field_name))
        for field_name in _EXECUTION_RECEIPT_PAYLOAD_FIELDS
    }


def canonical_execution_local_trade_payload(
    record: Mapping[str, Any],
) -> dict[str, Any]:
    """Project the strict local-trade identity bound to a receipt digest."""

    return {
        field_name: deepcopy(record.get(field_name))
        for field_name in _EXECUTION_LOCAL_TRADE_PAYLOAD_FIELDS
    }


def canonical_execution_payload_sha256(payload: Mapping[str, Any]) -> str:
    """Hash an explicit execution payload; never hash an adapter wrapper."""

    return _payload_sha256(payload)


def seal_strict_execution_event(
    record: Mapping[str, Any],
    *,
    supplied_receipt_sha256: Any = None,
    supplied_local_trade_sha256: Any = None,
) -> dict[str, Any]:
    """Attach content-bound receipt and local-trade fingerprints.

    A provider supplied digest is a claim, not authority.  When present it must
    match the canonical payload exactly; missing claims are not synthesized
    from the containing orchestration record.
    """

    result = deepcopy(dict(record))
    receipt_payload = canonical_execution_receipt_payload(result)
    receipt_sha256 = canonical_execution_payload_sha256(receipt_payload)
    supplied_receipt = str(supplied_receipt_sha256 or "").strip().lower()
    if supplied_receipt and not hmac.compare_digest(supplied_receipt, receipt_sha256):
        raise ValueError("receipt_sha256_content_mismatch")
    result["execution_receipt_payload"] = receipt_payload
    result["receipt_sha256"] = receipt_sha256
    if supplied_receipt:
        result["receipt_source_claim_sha256"] = supplied_receipt

    local_payload = canonical_execution_local_trade_payload(result)
    local_trade_sha256 = canonical_execution_payload_sha256(local_payload)
    supplied_local = str(supplied_local_trade_sha256 or "").strip().lower()
    if supplied_local and not hmac.compare_digest(supplied_local, local_trade_sha256):
        raise ValueError("local_trade_sha256_content_mismatch")
    result["execution_local_trade_payload"] = local_payload
    result["local_trade_sha256"] = local_trade_sha256
    if supplied_local:
        result["local_trade_source_claim_sha256"] = supplied_local
    return result


def strict_round_trip_source_sha256(record: Mapping[str, Any]) -> str:
    """Bind a round trip to its authoritative prediction and fill digests."""

    return _payload_sha256(
        {
            "prediction_snapshot_id": record.get("prediction_snapshot_id"),
            "prediction_content_sha256": record.get("prediction_content_sha256"),
            "entry_fill_identity": record.get("entry_fill_identity"),
            "entry_receipt_sha256": record.get("entry_receipt_sha256"),
            "entry_local_trade_sha256": record.get("entry_local_trade_sha256"),
            "exit_fill_identities": deepcopy(record.get("exit_fill_identities")),
            "exit_receipt_sha256s": deepcopy(record.get("exit_receipt_sha256s")),
            "exit_local_trade_sha256s": deepcopy(
                record.get("exit_local_trade_sha256s")
            ),
            "closed_at": record.get("closed_at"),
        }
    )


def strict_round_trip_content_sha256(record: Mapping[str, Any]) -> str:
    """Hash the explicit strict round-trip contract, excluding wrappers."""

    payload = {
        field_name: deepcopy(record.get(field_name))
        for field_name in _ROUND_TRIP_CONTENT_PAYLOAD_FIELDS
    }
    return _payload_sha256(payload)


def _prediction_cluster_id(value: Mapping[str, Any]) -> str:
    raw_timestamp = str(value.get("prediction_at") or "").strip()
    try:
        parsed = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if (
        parsed is not None
        and parsed.tzinfo is not None
        and parsed.utcoffset() is not None
    ):
        bucket = parsed.replace(
            minute=(parsed.minute // 5) * 5,
            second=0,
            microsecond=0,
        ).isoformat()
    else:
        bucket = "invalid_prediction_time:%s" % raw_timestamp
    marketgraph = value.get("marketgraph")
    ablation_group = (
        str(marketgraph.get("ablation_group") or "unknown")
        if isinstance(marketgraph, Mapping)
        else "unknown"
    )
    identity = {
        "capital_authority_id": value.get("capital_authority_id"),
        "authority_generation": value.get("authority_generation"),
        "execution_lineage_id": value.get("execution_lineage_id"),
        "market": str(value.get("market") or "").strip().lower(),
        "symbol": str(value.get("symbol") or "").strip().upper(),
        "style": value.get("style") or value.get("style_id"),
        "strategy_version": value.get("strategy_version") or value.get("style_version"),
        "ablation_group": ablation_group,
        "five_minute_bucket": bucket,
    }
    return "sample-cluster:" + _payload_sha256(identity)[:32]


def _decision_cluster_id(value: Mapping[str, Any]) -> str:
    """Collapse style, MG arm and horizon cells into one decision opportunity."""

    raw_base = str(value.get("base_snapshot_sha256") or "").strip().lower()
    if _is_64hex(raw_base):
        identity: Mapping[str, Any] = {
            "capital_authority_id": value.get("capital_authority_id"),
            "authority_generation": value.get("authority_generation"),
            "execution_lineage_id": value.get("execution_lineage_id"),
            "base_snapshot_sha256": raw_base,
        }
    else:
        raw_timestamp = str(value.get("prediction_at") or "").strip()
        try:
            parsed = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            bucket = parsed.replace(
                minute=(parsed.minute // 5) * 5,
                second=0,
                microsecond=0,
            ).isoformat()
        else:
            bucket = "invalid_prediction_time:%s" % raw_timestamp
        identity = {
            "capital_authority_id": value.get("capital_authority_id"),
            "authority_generation": value.get("authority_generation"),
            "execution_lineage_id": value.get("execution_lineage_id"),
            "market": str(value.get("market") or "").strip().lower(),
            "symbol": str(value.get("symbol") or "").strip().upper(),
            "five_minute_bucket": bucket,
        }
    return "decision-cluster:" + _payload_sha256(identity)[:32]


def _current_authority_scope(value: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    if value is None:
        # Time-boxed compatibility for legacy callers that have not yet been
        # moved to the explicit authority-scope contract.  Current runtime
        # composition must provide the observed authority envelope instead of
        # treating this bootstrap generation as a permanent truth.
        scope: dict[str, Any] = {
            "capital_authority_id": ASHARE_CAPITAL_AUTHORITY_ID,
            "authority_generation": ASHARE_AUTHORITY_GENERATION,
            "execution_lineage_id": ASHARE_EXECUTION_LINEAGE_ID,
        }
    else:
        scope = dict(value)
    authority_id = str(scope.get("capital_authority_id") or "").strip()
    generation = scope.get("authority_generation")
    lineage_id = str(scope.get("execution_lineage_id") or "").strip()
    if authority_id != ASHARE_CAPITAL_AUTHORITY_ID:
        raise JournalSafetyError("current A-share capital authority required")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation <= 0
    ):
        raise JournalSafetyError("current A-share authority generation required")
    if not lineage_id:
        raise JournalSafetyError("current A-share execution lineage required")
    return {
        "capital_authority_id": authority_id,
        "authority_generation": generation,
        "execution_lineage_id": lineage_id,
    }


def _record_in_authority(
    record: Mapping[str, Any], authority_scope: Mapping[str, Any]
) -> bool:
    return (
        str(record.get("capital_authority_id") or "")
        == authority_scope["capital_authority_id"]
        and record.get("authority_generation")
        == authority_scope["authority_generation"]
        and str(record.get("execution_lineage_id") or "")
        == authority_scope["execution_lineage_id"]
    )


def _is_64hex(value: Any) -> bool:
    raw = str(value or "").strip().lower()
    return len(raw) == 64 and all(character in "0123456789abcdef" for character in raw)


def _digest_matches(value: Any, expected: str) -> bool:
    raw = str(value or "").strip().lower()
    return _is_64hex(raw) and hmac.compare_digest(raw, expected)


def build_strict_execution_evidence_index(
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Index immutable prediction/fill/stop facts from one frozen view."""

    predictions: dict[str, list[Mapping[str, Any]]] = {}
    fills: dict[str, list[Mapping[str, Any]]] = {}
    stops: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for event in events:
        if not isinstance(event, Mapping):
            continue
        if event.get("journal_event_type") == "prediction_snapshot":
            snapshot_id = str(event.get("snapshot_id") or "").strip()
            if snapshot_id:
                predictions.setdefault(snapshot_id, []).append(event)
            continue
        record_type = str(event.get("record_type") or "").strip().lower()
        fill_identity = str(event.get("fill_identity") or "").strip()
        if record_type == "fill" and fill_identity:
            fills.setdefault(fill_identity, []).append(event)
        elif record_type in {"stop", "exit_stop"} and fill_identity:
            entry_identity = str(event.get("entry_fill_identity") or "").strip()
            stops.setdefault((fill_identity, entry_identity), []).append(event)
    return {"predictions": predictions, "fills": fills, "stops": stops}


def _validate_embedded_execution_envelope(
    record: Mapping[str, Any],
    *,
    boundary: Optional[datetime],
) -> dict[str, Any]:
    embedded = record.get("evidence_envelope")
    if not isinstance(embedded, Mapping):
        return {"valid": False, "reason": "evidence_envelope_missing"}
    if embedded.get("structure_errors"):
        return {"valid": False, "reason": "evidence_envelope_structure_invalid"}
    event_values = embedded.get("event_time_fields")
    if not isinstance(event_values, Mapping) or not event_values:
        return {
            "valid": False,
            "reason": "evidence_envelope_event_time_fields_missing",
        }
    receipt_value_count = 0
    for group_name in (
        "availability_time_fields",
        "ingestion_time_fields",
        "retrieval_time_fields",
    ):
        values = embedded.get(group_name)
        if not isinstance(values, Mapping):
            return {
                "valid": False,
                "reason": "evidence_envelope_%s_invalid" % group_name,
            }
        receipt_value_count += len(values)
    if receipt_value_count == 0:
        return {"valid": False, "reason": "evidence_envelope_receipts_missing"}
    fresh_validation = validate_evidence_envelope(
        embedded,
        boundary=boundary,
        require_receipts=True,
    )
    if (
        fresh_validation.get("complete") is not True
        or fresh_validation.get("status") != "valid"
    ):
        return {
            "valid": False,
            "reason": "evidence_envelope_%s"
            % str(fresh_validation.get("status") or "invalid"),
            "evidence_envelope_validation": fresh_validation,
        }

    lineage = record.get("point_in_time_lineage")
    stored_validation = record.get("evidence_envelope_validation")
    if (
        not isinstance(lineage, Mapping)
        or lineage.get("complete") is not True
        or lineage.get("status") != "valid"
        or not isinstance(lineage.get("timestamps"), Mapping)
        or not isinstance(stored_validation, Mapping)
        or stored_validation.get("complete") is not True
        or stored_validation.get("status") != "valid"
        or not isinstance(stored_validation.get("canonical_timestamps"), Mapping)
    ):
        return {"valid": False, "reason": "stored_evidence_lineage_incomplete"}

    fresh_timestamps = fresh_validation.get("canonical_timestamps")
    if not isinstance(fresh_timestamps, Mapping):
        return {"valid": False, "reason": "canonical_timestamps_missing"}
    stored_timestamps = lineage["timestamps"]
    stored_canonical = stored_validation["canonical_timestamps"]
    for field_name in PIT_TIMESTAMP_FIELDS:
        values = (
            fresh_timestamps.get(field_name),
            stored_timestamps.get(field_name),
            stored_canonical.get(field_name),
            record.get(field_name),
        )
        parsed_values: list[datetime] = []
        for value in values:
            try:
                parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
            except ValueError:
                parsed = None
            if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
                return {
                    "valid": False,
                    "reason": "canonical_timestamp_%s_invalid" % field_name,
                }
            parsed_values.append(parsed.astimezone(timezone.utc))
        if len(set(parsed_values)) != 1:
            return {
                "valid": False,
                "reason": "canonical_timestamp_%s_conflict" % field_name,
            }
    return {
        "valid": True,
        "reason": "strict_embedded_evidence_envelope",
        "evidence_envelope_validation": fresh_validation,
    }


def _validate_strict_execution_event(
    event: Mapping[str, Any],
    *,
    expected_record_types: set[str],
    prediction_snapshot_id: str,
    prediction_source_sha256: str,
    prediction_content_sha256_value: str,
    authority_scope: Mapping[str, Any],
    boundary: Optional[datetime],
) -> dict[str, Any]:
    if str(event.get("record_type") or "").strip().lower() not in expected_record_types:
        return {"valid": False, "reason": "execution_event_type_mismatch"}
    if event.get("execution_eligible") is not True:
        return {"valid": False, "reason": "execution_event_not_eligible"}
    if not _record_in_authority(event, authority_scope):
        return {"valid": False, "reason": "execution_event_authority_mismatch"}
    if str(event.get("prediction_snapshot_id") or "").strip() != prediction_snapshot_id:
        return {"valid": False, "reason": "execution_event_prediction_mismatch"}
    if not _digest_matches(
        event.get("prediction_source_snapshot_sha256"), prediction_source_sha256
    ):
        return {"valid": False, "reason": "execution_event_prediction_source_mismatch"}
    if not _digest_matches(
        event.get("prediction_content_sha256"), prediction_content_sha256_value
    ):
        return {"valid": False, "reason": "execution_event_prediction_content_mismatch"}

    envelope_validation = _validate_embedded_execution_envelope(
        event,
        boundary=boundary,
    )
    if envelope_validation.get("valid") is not True:
        return envelope_validation

    stored_receipt_payload = event.get("execution_receipt_payload")
    expected_receipt_payload = canonical_execution_receipt_payload(event)
    if (
        not isinstance(stored_receipt_payload, Mapping)
        or dict(stored_receipt_payload) != expected_receipt_payload
    ):
        return {"valid": False, "reason": "execution_receipt_payload_mismatch"}
    receipt_sha256 = canonical_execution_payload_sha256(expected_receipt_payload)
    if not _digest_matches(event.get("receipt_sha256"), receipt_sha256):
        return {"valid": False, "reason": "execution_receipt_sha256_mismatch"}
    receipt_claim = str(event.get("receipt_source_claim_sha256") or "").strip()
    if receipt_claim and not _digest_matches(receipt_claim, receipt_sha256):
        return {"valid": False, "reason": "execution_receipt_claim_mismatch"}

    stored_local_payload = event.get("execution_local_trade_payload")
    expected_local_payload = canonical_execution_local_trade_payload(event)
    if (
        not isinstance(stored_local_payload, Mapping)
        or dict(stored_local_payload) != expected_local_payload
    ):
        return {"valid": False, "reason": "execution_local_trade_payload_mismatch"}
    local_trade_sha256 = canonical_execution_payload_sha256(expected_local_payload)
    if not _digest_matches(event.get("local_trade_sha256"), local_trade_sha256):
        return {"valid": False, "reason": "execution_local_trade_sha256_mismatch"}
    local_claim = str(event.get("local_trade_source_claim_sha256") or "").strip()
    if local_claim and not _digest_matches(local_claim, local_trade_sha256):
        return {"valid": False, "reason": "execution_local_trade_claim_mismatch"}
    return {
        "valid": True,
        "reason": "strict_execution_event",
        "receipt_sha256": receipt_sha256,
        "local_trade_sha256": local_trade_sha256,
        "evidence_envelope_validation": envelope_validation.get(
            "evidence_envelope_validation"
        ),
    }


def validate_strict_completed_round_trip_evidence(
    record: Mapping[str, Any],
    *,
    boundary: Optional[datetime] = None,
    prediction_snapshot_id: Optional[str] = None,
    authority_scope: Optional[Mapping[str, Any]] = None,
    evidence_index: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Validate content-bound prediction, entry, exit, and receipt evidence."""

    if boundary is not None and (
        boundary.tzinfo is None or boundary.utcoffset() is None
    ):
        return {"valid": False, "reason": "boundary_timezone_naive"}
    kind = (
        str(
            record.get("record_type")
            or record.get("event_type")
            or record.get("type")
            or ""
        )
        .strip()
        .lower()
    )
    if kind != "completed_round_trip" and record.get("round_trip_complete") is not True:
        return {"valid": False, "reason": "not_completed_round_trip"}

    current_authority = _current_authority_scope(authority_scope)
    if not _record_in_authority(record, current_authority):
        return {"valid": False, "reason": "current_authority_lineage_mismatch"}

    actual_prediction_snapshot_id = str(
        record.get("prediction_snapshot_id") or ""
    ).strip()
    if (
        record.get("round_trip_complete") is not True
        or record.get("execution_eligible") is not True
        or str(record.get("costs_cover") or "") != "round_trip"
        or not actual_prediction_snapshot_id
        or evidence_index is None
    ):
        return {"valid": False, "reason": "incomplete_round_trip_lineage"}
    if (
        prediction_snapshot_id is not None
        and actual_prediction_snapshot_id != str(prediction_snapshot_id).strip()
    ):
        return {"valid": False, "reason": "prediction_snapshot_mismatch"}

    entry_fill_identity = str(record.get("entry_fill_identity") or "").strip()
    exit_fill_identities = record.get("exit_fill_identities")
    if (
        not entry_fill_identity
        or actual_prediction_snapshot_id not in entry_fill_identity
        or not isinstance(exit_fill_identities, Sequence)
        or isinstance(exit_fill_identities, (str, bytes, bytearray))
        or not exit_fill_identities
        or any(
            not str(identity or "").strip()
            or actual_prediction_snapshot_id not in str(identity)
            for identity in exit_fill_identities
        )
    ):
        return {"valid": False, "reason": "execution_fill_identity_invalid"}

    predictions = evidence_index.get("predictions")
    fills = evidence_index.get("fills")
    stops = evidence_index.get("stops")
    if not all(isinstance(value, Mapping) for value in (predictions, fills, stops)):
        return {"valid": False, "reason": "execution_evidence_index_invalid"}
    prediction_matches = predictions.get(actual_prediction_snapshot_id)
    if (
        not isinstance(prediction_matches, Sequence)
        or isinstance(prediction_matches, (str, bytes, bytearray))
        or len(prediction_matches) != 1
        or not isinstance(prediction_matches[0], Mapping)
    ):
        return {
            "valid": False,
            "reason": "authoritative_prediction_missing_or_ambiguous",
        }
    prediction_event = prediction_matches[0]
    if not _record_in_authority(prediction_event, current_authority):
        return {"valid": False, "reason": "authoritative_prediction_authority_mismatch"}
    authoritative_prediction_content_sha256 = _prediction_content_sha256(
        prediction_event
    )
    if not _digest_matches(
        prediction_event.get("prediction_content_sha256"),
        authoritative_prediction_content_sha256,
    ):
        return {"valid": False, "reason": "authoritative_prediction_content_mismatch"}
    prediction_source_sha256 = (
        str(prediction_event.get("source_snapshot_sha256") or "").strip().lower()
    )
    prediction_source_payload = prediction_event.get("source_snapshot_payload")
    if (
        not isinstance(prediction_source_payload, Mapping)
        or not prediction_source_payload
    ):
        return {
            "valid": False,
            "reason": "authoritative_prediction_source_payload_missing",
        }
    expected_prediction_source_sha256 = prediction_source_payload_sha256(
        prediction_source_payload
    )
    if not _digest_matches(
        prediction_source_sha256,
        expected_prediction_source_sha256,
    ):
        return {
            "valid": False,
            "reason": "authoritative_prediction_source_payload_mismatch",
        }
    if not _digest_matches(
        record.get("prediction_source_snapshot_sha256"), prediction_source_sha256
    ):
        return {"valid": False, "reason": "prediction_source_snapshot_mismatch"}
    if not _digest_matches(
        record.get("prediction_content_sha256"),
        authoritative_prediction_content_sha256,
    ):
        return {"valid": False, "reason": "prediction_content_snapshot_mismatch"}

    entry_matches = fills.get(entry_fill_identity)
    if (
        not isinstance(entry_matches, Sequence)
        or isinstance(entry_matches, (str, bytes, bytearray))
        or len(entry_matches) != 1
        or not isinstance(entry_matches[0], Mapping)
    ):
        return {"valid": False, "reason": "entry_fill_missing_or_ambiguous"}
    entry_event = entry_matches[0]
    entry_validation = _validate_strict_execution_event(
        entry_event,
        expected_record_types={"fill"},
        prediction_snapshot_id=actual_prediction_snapshot_id,
        prediction_source_sha256=prediction_source_sha256,
        prediction_content_sha256_value=authoritative_prediction_content_sha256,
        authority_scope=current_authority,
        boundary=boundary,
    )
    if entry_validation.get("valid") is not True:
        return {**entry_validation, "reason": "entry_%s" % entry_validation["reason"]}

    exit_events: list[Mapping[str, Any]] = []
    exit_validations: list[Mapping[str, Any]] = []
    for raw_identity in exit_fill_identities:
        exit_identity = str(raw_identity).strip()
        matches = stops.get((exit_identity, entry_fill_identity))
        if (
            not isinstance(matches, Sequence)
            or isinstance(matches, (str, bytes, bytearray))
            or len(matches) != 1
            or not isinstance(matches[0], Mapping)
        ):
            return {"valid": False, "reason": "exit_fill_missing_or_ambiguous"}
        exit_event = matches[0]
        exit_validation = _validate_strict_execution_event(
            exit_event,
            expected_record_types={"stop", "exit_stop"},
            prediction_snapshot_id=actual_prediction_snapshot_id,
            prediction_source_sha256=prediction_source_sha256,
            prediction_content_sha256_value=authoritative_prediction_content_sha256,
            authority_scope=current_authority,
            boundary=boundary,
        )
        if exit_validation.get("valid") is not True:
            return {**exit_validation, "reason": "exit_%s" % exit_validation["reason"]}
        exit_events.append(exit_event)
        exit_validations.append(exit_validation)

    exit_receipt_sha256s = record.get("exit_receipt_sha256s")
    exit_local_trade_sha256s = record.get("exit_local_trade_sha256s")
    expected_exit_receipts = [value["receipt_sha256"] for value in exit_validations]
    expected_exit_locals = [value["local_trade_sha256"] for value in exit_validations]
    if (
        not _digest_matches(
            record.get("entry_receipt_sha256"), entry_validation["receipt_sha256"]
        )
        or not _digest_matches(
            record.get("entry_local_trade_sha256"),
            entry_validation["local_trade_sha256"],
        )
        or not isinstance(exit_receipt_sha256s, Sequence)
        or isinstance(exit_receipt_sha256s, (str, bytes, bytearray))
        or not isinstance(exit_local_trade_sha256s, Sequence)
        or isinstance(exit_local_trade_sha256s, (str, bytes, bytearray))
        or len(exit_receipt_sha256s) != len(expected_exit_receipts)
        or len(exit_local_trade_sha256s) != len(expected_exit_locals)
        or any(
            not _digest_matches(actual, expected)
            for actual, expected in zip(exit_receipt_sha256s, expected_exit_receipts)
        )
        or any(
            not _digest_matches(actual, expected)
            for actual, expected in zip(exit_local_trade_sha256s, expected_exit_locals)
        )
    ):
        return {"valid": False, "reason": "execution_fingerprint_content_mismatch"}

    expected_source_sha256 = strict_round_trip_source_sha256(record)
    if not _digest_matches(
        record.get("source_snapshot_sha256"), expected_source_sha256
    ):
        return {"valid": False, "reason": "round_trip_source_sha256_mismatch"}
    expected_content_sha256 = strict_round_trip_content_sha256(record)
    if not _digest_matches(record.get("content_sha256"), expected_content_sha256):
        return {"valid": False, "reason": "round_trip_content_sha256_mismatch"}

    net_field = (
        "net_pnl_cny" if record.get("net_pnl_cny") is not None else "post_cost_pnl_cny"
    )
    for field_name in (
        "entry_quantity",
        "entry_price",
        "notional_cny",
        "gross_pnl_cny",
        net_field,
        "fee_cny",
        "slippage_cny",
    ):
        value = record.get(field_name)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return {"valid": False, "reason": "invalid_numeric_%s" % field_name}
        if not math.isfinite(float(value)):
            return {"valid": False, "reason": "invalid_numeric_%s" % field_name}
        if (
            field_name in {"entry_quantity", "entry_price", "notional_cny"}
            and float(value) <= 0.0
        ):
            return {"valid": False, "reason": "invalid_numeric_%s" % field_name}
        if field_name in {"fee_cny", "slippage_cny"} and float(value) < 0.0:
            return {"valid": False, "reason": "invalid_numeric_%s" % field_name}

    closed_at = record.get("closed_at")
    try:
        parsed_closed_at = datetime.fromisoformat(
            str(closed_at or "").replace("Z", "+00:00")
        )
    except ValueError:
        parsed_closed_at = None
    if (
        parsed_closed_at is None
        or parsed_closed_at.tzinfo is None
        or parsed_closed_at.utcoffset() is None
    ):
        return {"valid": False, "reason": "closed_at_invalid"}
    round_trip_envelope_validation = _validate_embedded_execution_envelope(
        record,
        boundary=boundary,
    )
    if round_trip_envelope_validation.get("valid") is not True:
        return round_trip_envelope_validation

    latest_exit = max(
        exit_events,
        key=lambda event: datetime.fromisoformat(
            str(event.get("point_in_time_as_of") or "").replace("Z", "+00:00")
        ).astimezone(timezone.utc),
    )
    latest_exit_at = datetime.fromisoformat(
        str(latest_exit.get("point_in_time_as_of") or "").replace("Z", "+00:00")
    )
    if parsed_closed_at.astimezone(timezone.utc) != latest_exit_at.astimezone(
        timezone.utc
    ):
        return {"valid": False, "reason": "round_trip_closed_at_exit_mismatch"}
    if record.get("evidence_envelope") != latest_exit.get("evidence_envelope"):
        return {"valid": False, "reason": "round_trip_exit_envelope_mismatch"}

    try:
        entry_quantity = float(entry_event.get("filled_quantity"))
        entry_price = float(entry_event.get("filled_price"))
        expected_fee = float(entry_event.get("fee_cny")) + sum(
            float(event.get("fee_cny")) for event in exit_events
        )
        expected_slippage = float(entry_event.get("slippage_cny")) + sum(
            float(event.get("slippage_cny")) for event in exit_events
        )
        expected_gross = sum(
            float(event.get("filled_quantity"))
            * (float(event.get("filled_price")) - entry_price)
            for event in exit_events
        )
        exited_quantity = sum(
            float(event.get("filled_quantity")) for event in exit_events
        )
    except (TypeError, ValueError):
        return {"valid": False, "reason": "execution_event_numeric_invalid"}
    expected_values = {
        "entry_quantity": entry_quantity,
        "entry_price": entry_price,
        "notional_cny": entry_quantity * entry_price,
        "gross_pnl_cny": expected_gross,
        "fee_cny": expected_fee,
        "slippage_cny": expected_slippage,
        net_field: expected_gross - expected_fee - expected_slippage,
    }
    if not math.isclose(exited_quantity, entry_quantity, rel_tol=0.0, abs_tol=1e-9):
        return {"valid": False, "reason": "round_trip_quantity_not_closed"}
    for field_name, expected_value in expected_values.items():
        if not math.isclose(
            float(record[field_name]), expected_value, rel_tol=0.0, abs_tol=1e-4
        ):
            return {
                "valid": False,
                "reason": "round_trip_%s_content_mismatch" % field_name,
            }
    return {
        "valid": True,
        "reason": "strict_completed_round_trip_evidence",
        "evidence_envelope_validation": round_trip_envelope_validation.get(
            "evidence_envelope_validation"
        ),
    }


def _strict_evolution_evidence(
    record: Mapping[str, Any],
    *,
    evidence_index: Mapping[str, Any],
    authority_scope: Mapping[str, Any],
) -> bool:
    kind = (
        str(
            record.get("record_type")
            or record.get("event_type")
            or record.get("type")
            or ""
        )
        .strip()
        .lower()
    )
    if kind != "completed_round_trip" and record.get("round_trip_complete") is not True:
        return True
    return (
        validate_strict_completed_round_trip_evidence(
            record,
            authority_scope=authority_scope,
            evidence_index=evidence_index,
        ).get("valid")
        is True
    )


def _has_positive_maturity_weight(record: Mapping[str, Any]) -> bool:
    try:
        value = float(record.get("maturity_weight", 1.0))
    except (TypeError, ValueError):
        return False
    return math.isfinite(value) and value > 0.0


def _is_truthy_live_flag(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    return str(value or "").strip().lower() in _TRUE_VALUES


def _find_live_marker(value: Any, path: str = "payload") -> Optional[str]:
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key).strip().lower()
            child_path = "%s.%s" % (path, raw_key)
            if key in _LIVE_BOOLEAN_FIELDS and _is_truthy_live_flag(nested):
                return child_path
            if (
                key in _LIVE_MODE_FIELDS
                and str(nested or "").strip().lower() in _LIVE_MODE_VALUES
            ):
                return child_path
            found = _find_live_marker(nested, child_path)
            if found:
                return found
    elif isinstance(value, (list, tuple, set)):
        for index, nested in enumerate(value):
            found = _find_live_marker(nested, "%s[%d]" % (path, index))
            if found:
                return found
    return None


def _reject_live_markers(value: Any) -> None:
    marker = _find_live_marker(value)
    if marker:
        raise JournalSafetyError("live trading marker rejected at %s" % marker)


def _force_sim_only(record: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(record))
    result["capital_layer"] = "simulated"
    result["account_type"] = "simulated"
    for field_name in _LIVE_BOOLEAN_FIELDS:
        result[field_name] = False
    return result


def _absolute_without_resolving(path: Union[str, os.PathLike[str]]) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _assert_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if not os.path.lexists(str(current)):
            continue
        try:
            mode = os.lstat(str(current)).st_mode
        except OSError as exc:
            raise JournalSafetyError(
                "cannot inspect journal path %s: %s" % (current, exc)
            )
        if stat.S_ISLNK(mode):
            raise JournalSafetyError(
                "journal path or parent is a symlink: %s" % current
            )


def _nofollow_flag() -> int:
    return int(getattr(os, "O_NOFOLLOW", 0))


def _unique_regular_path_identity(
    path: Path, *, role: str
) -> Optional[tuple[int, int]]:
    try:
        path_stat = os.lstat(str(path))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise JournalSafetyError("cannot inspect %s path: %s" % (role, exc))
    if not stat.S_ISREG(path_stat.st_mode):
        raise JournalSafetyError("%s must be a regular file" % role)
    if path_stat.st_nlink != 1:
        raise JournalSafetyError("%s hardlink count must equal one" % role)
    return int(path_stat.st_dev), int(path_stat.st_ino)


def _assert_fd_path_identity(
    path: Path,
    fd: int,
    *,
    role: str,
    expected_identity: Optional[tuple[int, int]] = None,
) -> tuple[int, int]:
    try:
        fd_stat = os.fstat(fd)
        path_stat = os.lstat(str(path))
    except OSError as exc:
        raise JournalSafetyError("cannot validate %s identity: %s" % (role, exc))
    if not stat.S_ISREG(fd_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise JournalSafetyError("%s must be a regular file" % role)
    if fd_stat.st_nlink != 1 or path_stat.st_nlink != 1:
        raise JournalSafetyError("%s hardlink count must equal one" % role)
    fd_identity = int(fd_stat.st_dev), int(fd_stat.st_ino)
    path_identity = int(path_stat.st_dev), int(path_stat.st_ino)
    if fd_identity != path_identity:
        raise JournalSafetyError("%s path changed while locked" % role)
    if expected_identity is not None and fd_identity != expected_identity:
        raise JournalSafetyError("%s identity changed while locked" % role)
    return fd_identity


def _verified_event(raw: Any, line_number: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise JournalSafetyError("journal line %d is not an object" % line_number)
    event = dict(raw)
    expected = str(event.get("journal_payload_sha256") or "")
    if not expected:
        raise JournalSafetyError(
            "journal line %d has no payload fingerprint" % line_number
        )
    unsigned = dict(event)
    unsigned.pop("journal_payload_sha256", None)
    if _payload_sha256(unsigned) != expected:
        raise JournalSafetyError(
            "journal line %d payload fingerprint mismatch" % line_number
        )
    if event.get("real_trading_enabled") is not False:
        raise JournalSafetyError(
            "journal line %d is not explicitly sim-only" % line_number
        )
    _reject_live_markers(event)
    return event


class SampleJournal:
    """Process-locked append-only JSONL journal and its read projection."""

    def __init__(self, path: Union[str, os.PathLike[str]]) -> None:
        self.path = _absolute_without_resolving(path)
        self.lock_path = self.path.with_name(".%s.lock" % self.path.name)
        self._metrics: dict[str, float] = {
            "journal_parse_count": 0.0,
            "journal_events_parsed": 0.0,
            "journal_bytes_parsed": 0.0,
            "journal_raw_validation_count": 0.0,
            "lock_acquire_count": 0.0,
            "lock_wait_seconds": 0.0,
            "lock_hold_seconds": 0.0,
            "append_batch_count": 0.0,
            "append_event_count": 0.0,
            "append_byte_count": 0.0,
            "fsync_count": 0.0,
        }

    def _check_paths(self) -> None:
        _assert_no_symlink_components(self.path.parent)
        _assert_no_symlink_components(self.path)
        _assert_no_symlink_components(self.lock_path)

    def _prepare_for_write(self) -> None:
        self._check_paths()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise JournalSafetyError("cannot create journal parent: %s" % exc)
        self._check_paths()

    @contextmanager
    def _locked(
        self, *, exclusive: bool, create_parent: bool
    ) -> Iterator[_LockedJournalPaths]:
        if create_parent:
            self._prepare_for_write()
        else:
            self._check_paths()
        flags = os.O_RDWR | os.O_CREAT | _nofollow_flag()
        try:
            fd = os.open(str(self.lock_path), flags, 0o600)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK}:
                raise JournalSafetyError("journal lock is a symlink")
            raise JournalSafetyError("cannot open journal lock: %s" % exc)
        try:
            lock_identity = _assert_fd_path_identity(
                self.lock_path, fd, role="journal lock"
            )
            wait_started = perf_counter()
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            acquired_at = perf_counter()
            self._metrics["lock_acquire_count"] += 1
            self._metrics["lock_wait_seconds"] += acquired_at - wait_started
            self._check_paths()
            _assert_fd_path_identity(
                self.lock_path,
                fd,
                role="journal lock",
                expected_identity=lock_identity,
            )
            locked_paths = _LockedJournalPaths(
                lock_identity=lock_identity,
                journal_identity=_unique_regular_path_identity(
                    self.path, role="sample journal"
                ),
            )
            try:
                yield locked_paths
            finally:
                self._check_paths()
                _assert_fd_path_identity(
                    self.lock_path,
                    fd,
                    role="journal lock",
                    expected_identity=lock_identity,
                )
                if locked_paths.journal_identity is not None:
                    current_journal_identity = _unique_regular_path_identity(
                        self.path, role="sample journal"
                    )
                    if current_journal_identity != locked_paths.journal_identity:
                        raise JournalSafetyError(
                            "sample journal identity changed while locked"
                        )
        finally:
            if "acquired_at" in locals():
                self._metrics["lock_hold_seconds"] += perf_counter() - acquired_at
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _load_with_state_unlocked(
        self,
        locked_paths: _LockedJournalPaths,
    ) -> tuple[list[dict[str, Any]], int, str, Optional[int], Any]:
        if not os.path.exists(str(self.path)):
            empty_hasher = sha256()
            self._metrics["journal_parse_count"] += 1
            return [], 0, empty_hasher.hexdigest(), None, empty_hasher
        flags = os.O_RDONLY | _nofollow_flag()
        try:
            fd = os.open(str(self.path), flags)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK}:
                raise JournalSafetyError("journal file is a symlink")
            raise JournalSafetyError("cannot open sample journal: %s" % exc)
        events: list[dict[str, Any]] = []
        digest = sha256()
        byte_count = 0
        inode: Optional[int] = None
        try:
            identity = _assert_fd_path_identity(
                self.path,
                fd,
                role="sample journal",
                expected_identity=locked_paths.journal_identity,
            )
            if locked_paths.journal_identity is None:
                locked_paths.journal_identity = identity
            inode = identity[1]
            with os.fdopen(fd, "rb", closefd=False) as handle:
                for line_number, raw_line in enumerate(handle, start=1):
                    digest.update(raw_line)
                    byte_count += len(raw_line)
                    if not raw_line.strip():
                        continue
                    try:
                        decoded = raw_line.decode("utf-8")
                        raw = json.loads(decoded)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise JournalSafetyError(
                            "journal line %d is malformed JSON: %s" % (line_number, exc)
                        )
                    events.append(_verified_event(raw, line_number))
            _assert_fd_path_identity(
                self.path,
                fd,
                role="sample journal",
                expected_identity=locked_paths.journal_identity,
            )
        finally:
            os.close(fd)
        self._metrics["journal_parse_count"] += 1
        self._metrics["journal_events_parsed"] += len(events)
        self._metrics["journal_bytes_parsed"] += byte_count
        return events, byte_count, digest.hexdigest(), inode, digest

    def _load_unlocked(self, locked_paths: _LockedJournalPaths) -> list[dict[str, Any]]:
        events, _, _, _, _ = self._load_with_state_unlocked(locked_paths)
        return events

    def _raw_state_unlocked(
        self, locked_paths: _LockedJournalPaths
    ) -> tuple[int, str, Optional[int]]:
        if not os.path.exists(str(self.path)):
            self._metrics["journal_raw_validation_count"] += 1
            return 0, sha256().hexdigest(), None
        flags = os.O_RDONLY | _nofollow_flag()
        try:
            fd = os.open(str(self.path), flags)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK}:
                raise JournalSafetyError("journal file is a symlink")
            raise JournalSafetyError("cannot validate sample journal: %s" % exc)
        digest = sha256()
        byte_count = 0
        try:
            identity = _assert_fd_path_identity(
                self.path,
                fd,
                role="sample journal",
                expected_identity=locked_paths.journal_identity,
            )
            if locked_paths.journal_identity is None:
                locked_paths.journal_identity = identity
            inode = identity[1]
            while True:
                block = os.read(fd, 1024 * 1024)
                if not block:
                    break
                digest.update(block)
                byte_count += len(block)
            _assert_fd_path_identity(
                self.path,
                fd,
                role="sample journal",
                expected_identity=locked_paths.journal_identity,
            )
        finally:
            os.close(fd)
        self._metrics["journal_raw_validation_count"] += 1
        return byte_count, digest.hexdigest(), inode

    def _append_many_unlocked(
        self,
        events: Sequence[Mapping[str, Any]],
        locked_paths: _LockedJournalPaths,
    ) -> bytes:
        if not events:
            return b""
        payload = _events_payload(events)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | _nofollow_flag()
        try:
            fd = os.open(str(self.path), flags, 0o600)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK}:
                raise JournalSafetyError("journal file is a symlink")
            raise JournalSafetyError("cannot append sample journal: %s" % exc)
        try:
            identity = _assert_fd_path_identity(
                self.path,
                fd,
                role="sample journal",
                expected_identity=locked_paths.journal_identity,
            )
            if locked_paths.journal_identity is None:
                locked_paths.journal_identity = identity
            written = 0
            while written < len(payload):
                count = os.write(fd, payload[written:])
                if count <= 0:
                    raise JournalSafetyError(
                        "short write while appending sample journal"
                    )
                written += count
            os.fsync(fd)
            _assert_fd_path_identity(
                self.path,
                fd,
                role="sample journal",
                expected_identity=locked_paths.journal_identity,
            )
        finally:
            os.close(fd)
        self._metrics["append_batch_count"] += 1
        self._metrics["append_event_count"] += len(events)
        self._metrics["append_byte_count"] += len(payload)
        self._metrics["fsync_count"] += 1
        return payload

    def _append_unlocked(
        self, event: Mapping[str, Any], locked_paths: _LockedJournalPaths
    ) -> None:
        self._append_many_unlocked([event], locked_paths)

    def metrics_snapshot(self) -> dict[str, Any]:
        metrics: dict[str, Any] = {}
        for key, value in self._metrics.items():
            metrics[key] = int(value) if key.endswith("_count") else round(value, 6)
        return metrics

    @staticmethod
    def canonical_head(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Return the canonical count/hash identity for an immutable event view."""

        return {
            "event_count": len(events),
            "sha256": _events_sha256(events),
        }

    @contextmanager
    def guard_projection_head(
        self,
        view: FrozenJournalView,
        task_owned_delta_events: Sequence[Mapping[str, Any]],
    ) -> Iterator[dict[str, Any]]:
        """CAS-check the physical H1 head and block writers through publication.

        The expected physical journal is the exact raw source frozen for H0 plus
        the canonical bytes appended by this task.  Holding a shared journal
        lock after validation prevents an exclusive appender from creating a
        time-of-check/time-of-publish gap before the current projection pointer
        is swapped.
        """

        if isinstance(task_owned_delta_events, (str, bytes, bytearray)):
            raise TypeError("task_owned_delta_events must be a sequence")
        delta = [deepcopy(dict(event)) for event in task_owned_delta_events]
        delta_payload = _events_payload(delta)
        expected_hasher = view._source_hasher.copy()
        expected_hasher.update(delta_payload)
        expected_byte_count = view.journal_source_byte_count + len(delta_payload)
        expected_sha256 = expected_hasher.hexdigest()
        expected_event_count = view.journal_source_event_count + len(delta)

        with self._locked(exclusive=False, create_parent=True) as locked_paths:
            actual_byte_count, actual_sha256, actual_inode = self._raw_state_unlocked(
                locked_paths
            )
            if (
                actual_byte_count != expected_byte_count
                or actual_sha256 != expected_sha256
                or (
                    view.journal_source_inode is not None
                    and actual_inode != view.journal_source_inode
                )
            ):
                raise JournalConflictError(
                    "unknown journal append after task-owned H1; restart with a new cutoff"
                )
            yield {
                "physical_source_event_count": expected_event_count,
                "physical_source_byte_count": actual_byte_count,
                "physical_source_sha256": actual_sha256,
                "physical_source_inode": actual_inode,
                "task_owned_delta_event_count": len(delta),
                "H1": self.canonical_head(list(view._events) + delta),
            }

    @staticmethod
    def _seal_event(event: Mapping[str, Any]) -> dict[str, Any]:
        sealed = deepcopy(dict(event))
        sealed["journal_schema_version"] = JOURNAL_SCHEMA_VERSION
        sealed.pop("journal_payload_sha256", None)
        sealed["journal_payload_sha256"] = _payload_sha256(sealed)
        return sealed

    @staticmethod
    def _result(status: str, record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "status": status,
            "record": deepcopy(dict(record)),
            "real_trading_enabled": False,
            "live_execution_enabled": False,
        }

    @classmethod
    def _prediction_event(cls, candidate: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(candidate, Mapping):
            raise TypeError("candidate must be a mapping")
        _reject_live_markers(candidate)
        event = build_prediction_snapshot(candidate)
        event = _force_sim_only(event)
        event["record_type"] = "prediction"
        event["journal_event_type"] = "prediction_snapshot"
        event["journal_event_id"] = "prediction_snapshot:%s" % event["snapshot_id"]
        event["sample_cluster_id"] = _prediction_cluster_id(event)
        event["decision_cluster_id"] = str(
            event.get("decision_cluster_id") or _decision_cluster_id(event)
        )
        event["cluster_role"] = "origin"
        event["maturity_weight"] = 1.0
        event["prediction_content_sha256"] = _prediction_content_sha256(event)
        return cls._seal_event(event)

    def append_prediction(self, candidate: Mapping[str, Any]) -> dict[str, Any]:
        """Persist one candidate/style snapshot regardless of strategy thresholds."""

        return self.append_predictions([candidate])[0]

    def append_predictions(
        self, candidates: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Atomically validate and append a prediction batch with one fsync."""

        if isinstance(candidates, (str, bytes, bytearray)):
            raise TypeError("candidates must be a sequence of mappings")
        events = [self._prediction_event(candidate) for candidate in candidates]
        if not events:
            return []

        with self._locked(exclusive=True, create_parent=True) as locked_paths:
            current_events = self._load_unlocked(locked_paths)
            existing = {
                str(row.get("snapshot_id") or ""): row
                for row in current_events
                if row.get("journal_event_type") == "prediction_snapshot"
                and str(row.get("snapshot_id") or "")
            }
            occupied_clusters = {
                str(row.get("sample_cluster_id") or "")
                for row in current_events
                if row.get("journal_event_type") == "prediction_snapshot"
                and str(row.get("sample_cluster_id") or "")
            }
            pending: dict[str, dict[str, Any]] = {}
            results: list[dict[str, Any]] = []
            for event in events:
                snapshot_id = str(event["snapshot_id"])
                prior = existing.get(snapshot_id) or pending.get(snapshot_id)
                if prior is not None:
                    if prior.get("prediction_content_sha256") != event.get(
                        "prediction_content_sha256"
                    ):
                        raise JournalConflictError(
                            "snapshot_id %s already exists with different content"
                            % snapshot_id
                        )
                    results.append(self._result("idempotent", prior))
                    continue
                cluster_id = str(event.get("sample_cluster_id") or "")
                if cluster_id in occupied_clusters:
                    event = deepcopy(event)
                    event["cluster_role"] = "duplicate"
                    event["maturity_weight"] = 0.0
                    event = self._seal_event(event)
                else:
                    occupied_clusters.add(cluster_id)
                pending[snapshot_id] = event
                results.append(self._result("appended", event))
            # No bytes are written until every identity has passed conflict
            # validation, so a late conflict cannot partially append the batch.
            self._append_many_unlocked(list(pending.values()), locked_paths)
        return results

    @staticmethod
    def _validated_layers(record: Mapping[str, Any]) -> tuple[str, ...]:
        explicit: set[str] = set()
        if isinstance(record.get("sample_layers"), (list, tuple, set)):
            explicit.update(
                str(value or "").strip().lower() for value in record["sample_layers"]
            )
        if record.get("sample_layer") is not None:
            explicit.add(str(record.get("sample_layer") or "").strip().lower())
        unknown = {value for value in explicit if value and value not in SAMPLE_LAYERS}
        if unknown:
            raise JournalSafetyError("unknown sample layer: %s" % sorted(unknown)[0])

        layers = classify_sample_layers(record)
        if not layers:
            raise JournalSafetyError("sample record has no recognized sample layer")
        exclusive = _MUTUALLY_EXCLUSIVE_LAYERS.intersection(layers)
        if len(exclusive) > 1:
            raise JournalSafetyError(
                "mutually exclusive sample layers cannot be mixed: %s"
                % ",".join(sorted(exclusive))
            )
        return layers

    @classmethod
    def _sample_event(cls, sample: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(sample, Mapping):
            raise TypeError("sample must be a mapping")
        _reject_live_markers(sample)
        kind = (
            str(
                sample.get("record_type")
                or sample.get("event_type")
                or sample.get("type")
                or ""
            )
            .strip()
            .lower()
        )
        if kind in {
            "prediction",
            "observation",
            "counterfactual",
            "candidate_prediction",
        }:
            raise JournalSafetyError("prediction samples must use append_prediction")

        event = _force_sim_only(sample)
        layers = cls._validated_layers(event)
        event["sample_layers"] = list(layers)
        if len(layers) == 1:
            event["sample_layer"] = layers[0]
        event["journal_event_type"] = "sample_event"
        supplied_id = str(
            event.get("journal_event_id")
            or event.get("event_id")
            or event.get("sample_id")
            or ""
        ).strip()
        if supplied_id:
            event["journal_event_id"] = "sample:%s" % supplied_id
        else:
            event["journal_event_id"] = "sample:%s" % _payload_sha256(event)[:32]
        return cls._seal_event(event)

    def append_samples(
        self,
        samples: Sequence[Mapping[str, Any]],
        *,
        expected_event_count: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Atomically append a sample batch against an optional journal head.

        ``expected_event_count`` lets a caller safely derive paired events from
        a prior replay.  Any concurrent append invalidates that replay instead
        of allowing a stale exit to close the same entry twice.
        """

        if isinstance(samples, (str, bytes, bytearray)):
            raise TypeError("samples must be a sequence of mappings")
        if expected_event_count is not None and (
            not isinstance(expected_event_count, int)
            or isinstance(expected_event_count, bool)
            or expected_event_count < 0
        ):
            raise ValueError("expected_event_count must be a non-negative integer")
        prepared = [self._sample_event(sample) for sample in samples]
        if not prepared:
            return []

        with self._locked(exclusive=True, create_parent=True) as locked_paths:
            events = self._load_unlocked(locked_paths)
            if expected_event_count is not None and len(events) != expected_event_count:
                raise JournalConflictError(
                    "journal changed during outcome pairing: expected %d events, found %d"
                    % (expected_event_count, len(events))
                )
            existing = {
                str(row.get("journal_event_id") or ""): row
                for row in events
                if str(row.get("journal_event_id") or "")
            }
            pending: dict[str, dict[str, Any]] = {}
            results: list[dict[str, Any]] = []
            for event in prepared:
                event_id = str(event["journal_event_id"])
                prior = existing.get(event_id) or pending.get(event_id)
                if prior is not None:
                    if prior.get("journal_payload_sha256") != event.get(
                        "journal_payload_sha256"
                    ):
                        raise JournalConflictError(
                            "journal_event_id %s already exists with different content"
                            % event_id
                        )
                    results.append(self._result("idempotent", prior))
                    continue
                pending[event_id] = event
                results.append(self._result("appended", event))
            self._append_many_unlocked(list(pending.values()), locked_paths)
        return results

    def append_sample(self, sample: Mapping[str, Any]) -> dict[str, Any]:
        """Append a non-prediction sample while preserving layer separation."""

        return self.append_samples([sample])[0]

    @classmethod
    def _build_label_update(
        cls,
        snapshot: Mapping[str, Any],
        price_points: Sequence[Mapping[str, Any]],
        *,
        as_of: Any,
        horizon_targets: Optional[Mapping[str, Any]] = None,
        costs: Optional[Mapping[str, Any]] = None,
        validation_plan: Optional[ValidationPlan] = None,
    ) -> dict[str, Any]:
        materialized = materialize_forward_labels(
            snapshot,
            price_points,
            as_of=as_of,
            horizon_targets=horizon_targets,
            costs=costs,
            validation_plan=validation_plan,
        )
        cost_model_version = None
        cost_evidence_id = None
        for label in (materialized.get("labels") or {}).values():
            if isinstance(label, dict):
                if label.get("cost_model_version") and cost_model_version is None:
                    cost_model_version = str(label["cost_model_version"])
                if label.get("cost_evidence_event_id") and cost_evidence_id is None:
                    cost_evidence_id = str(label["cost_evidence_event_id"])

        snapshot_id = str(snapshot.get("snapshot_id") or "")
        update = _force_sim_only(
            {
                "record_type": "label_update",
                "journal_event_type": "forward_label_update",
                "snapshot_id": snapshot_id,
                "market": snapshot.get("market"),
                "symbol": snapshot.get("symbol"),
                "style": snapshot.get("style") or snapshot.get("style_id"),
                "strategy_version": snapshot.get("strategy_version")
                or snapshot.get("style_version"),
                "labels_as_of": materialized["labels_as_of"],
                # A label update is a task-owned fact whose receipt boundary is
                # exactly its run cutoff.  This explicit field lets the next
                # frozen view avoid inferring receipt time from market time.
                "evidence_available_at": materialized["labels_as_of"],
                "labels": materialized["labels"],
                "label_aliases": materialized["label_aliases"],
                "forward_label_eligibility": snapshot.get("forward_label_eligibility"),
                "forward_label_rejection_reason": snapshot.get(
                    "forward_label_rejection_reason"
                ),
                "forward_label_pending_reason": snapshot.get(
                    "forward_label_pending_reason"
                ),
                "cost_model_version": cost_model_version,
                "capital_authority_id": snapshot.get("capital_authority_id"),
                "authority_generation": snapshot.get("authority_generation"),
                "execution_lineage_id": snapshot.get("execution_lineage_id"),
                "point_in_time_as_of": snapshot.get("point_in_time_as_of")
                or snapshot.get("as_of")
                or snapshot.get("prediction_at"),
                "source_snapshot_sha256": snapshot.get("source_snapshot_sha256"),
                "base_snapshot_sha256": snapshot.get("base_snapshot_sha256"),
                "pair_id": snapshot.get("pair_id"),
                "sample_cluster_id": snapshot.get("sample_cluster_id"),
                "decision_cluster_id": snapshot.get("decision_cluster_id"),
                "cluster_role": snapshot.get("cluster_role"),
                "maturity_weight": snapshot.get("maturity_weight"),
                "primary_label_horizon": snapshot.get("primary_label_horizon"),
                "primary_horizon_policy_version": snapshot.get(
                    "primary_horizon_policy_version"
                ),
                "sample_science_contract_version": snapshot.get(
                    "sample_science_contract_version"
                ),
            }
        )
        if isinstance(materialized.get("forward_label_authority_binding"), Mapping):
            update["forward_label_authority_binding"] = deepcopy(
                materialized["forward_label_authority_binding"]
            )
        update["journal_event_id"] = _stable_label_update_id(
            snapshot_id,
            materialized["labels_as_of"],
            cost_model_version,
            cost_evidence_id,
        )
        return cls._seal_event(update)

    @staticmethod
    def _append_cursor(view: FrozenJournalView) -> _JournalAppendCursor:
        return _JournalAppendCursor(
            expected_byte_count=view.journal_source_byte_count,
            expected_file_sha256=view.journal_source_sha256,
            expected_inode=view.journal_source_inode,
            hasher=view._source_hasher.copy(),
        )

    def _validate_append_cursor_unlocked(
        self,
        cursor: _JournalAppendCursor,
        locked_paths: _LockedJournalPaths,
    ) -> None:
        byte_count, file_sha256, inode = self._raw_state_unlocked(locked_paths)
        if (
            byte_count != cursor.expected_byte_count
            or file_sha256 != cursor.expected_file_sha256
            or inode != cursor.expected_inode
        ):
            raise JournalConflictError(
                "unknown journal append after frozen head; restart with a new cutoff"
            )

    def materialize_label_batch(
        self,
        view: FrozenJournalView,
        requests: Sequence[Mapping[str, Any]],
        *,
        batch_size: int = 200,
        validation_plan: Optional[ValidationPlan] = None,
    ) -> dict[str, Any]:
        """Append task-owned label deltas in bounded idempotent batches.

        The frozen physical prefix is checked under every batch lock.  Only the
        bytes written by this method advance the cursor; an unrelated append
        blocks the task instead of silently joining its projection input.
        """

        if not isinstance(batch_size, int) or isinstance(batch_size, bool):
            raise ValueError("batch_size must be an integer")
        if not 100 <= batch_size <= 250:
            raise ValueError("batch_size must be between 100 and 250")
        if isinstance(requests, (str, bytes, bytearray)):
            raise TypeError("requests must be a sequence of mappings")

        existing = dict(view._events_by_id)
        pending_by_id: dict[str, dict[str, Any]] = {}
        results: list[dict[str, Any]] = []
        for raw_request in requests:
            if not isinstance(raw_request, Mapping):
                raise TypeError("label request must be a mapping")
            snapshot_id = str(raw_request.get("snapshot_id") or "").strip()
            snapshot = view._predictions_by_id.get(snapshot_id)
            if snapshot is None:
                raise JournalSafetyError("unknown snapshot_id: %s" % snapshot_id)
            price_points = raw_request.get("price_points")
            if not isinstance(price_points, Sequence) or isinstance(
                price_points, (str, bytes, bytearray)
            ):
                raise TypeError("price_points must be a sequence")
            _reject_live_markers(price_points)
            costs = raw_request.get("costs")
            if costs is not None:
                if not isinstance(costs, Mapping):
                    raise TypeError("costs must be a mapping")
                _reject_live_markers(costs)
            update = self._build_label_update(
                snapshot,
                price_points,
                as_of=raw_request.get("as_of"),
                horizon_targets=(
                    raw_request.get("horizon_targets")
                    if isinstance(raw_request.get("horizon_targets"), Mapping)
                    else None
                ),
                costs=costs,
                validation_plan=validation_plan,
            )
            event_id = str(update["journal_event_id"])
            prior = existing.get(event_id) or pending_by_id.get(event_id)
            if prior is not None:
                if prior.get("journal_payload_sha256") != update.get(
                    "journal_payload_sha256"
                ):
                    raise JournalConflictError(
                        "journal_event_id %s already exists with different content"
                        % event_id
                    )
                results.append(self._result("idempotent", prior))
                continue
            pending_by_id[event_id] = update
            results.append(self._result("appended", update))

        pending = list(pending_by_id.values())
        cursor = self._append_cursor(view)
        for start in range(0, len(pending), batch_size):
            batch = pending[start : start + batch_size]
            with self._locked(exclusive=True, create_parent=True) as locked_paths:
                self._validate_append_cursor_unlocked(cursor, locked_paths)
                payload = self._append_many_unlocked(batch, locked_paths)
                cursor.hasher.update(payload)
                cursor.expected_byte_count += len(payload)
                cursor.expected_file_sha256 = cursor.hasher.hexdigest()
                cursor.task_owned_event_count += len(batch)
                cursor.task_owned_byte_count += len(payload)
                if cursor.expected_inode is None and os.path.exists(str(self.path)):
                    cursor.expected_inode = int(os.stat(self.path).st_ino)

        return {
            "results": results,
            "appended_events": deepcopy(pending),
            "task_owned_delta_event_count": cursor.task_owned_event_count,
            "task_owned_delta_byte_count": cursor.task_owned_byte_count,
            "append_batch_count": (
                (len(pending) + batch_size - 1) // batch_size if pending else 0
            ),
            "fsync_count": (
                (len(pending) + batch_size - 1) // batch_size if pending else 0
            ),
            "final_source_byte_count": cursor.expected_byte_count,
            "final_source_sha256": cursor.expected_file_sha256,
        }

    def materialize_labels(
        self,
        snapshot_id: str,
        price_points: Sequence[Mapping[str, Any]],
        *,
        as_of: Any,
        horizon_targets: Optional[Mapping[str, Any]] = None,
        costs: Optional[Mapping[str, Any]] = None,
        validation_plan: Optional[ValidationPlan] = None,
    ) -> dict[str, Any]:
        """Append an idempotent forward-label update for an existing snapshot.

        The idempotency fingerprint includes the cost model version so that
        old 0-cost labels never silently collide with versioned labels.
        """

        _reject_live_markers(price_points)
        if costs is not None:
            _reject_live_markers(costs)
        with self._locked(exclusive=True, create_parent=True) as locked_paths:
            events = self._load_unlocked(locked_paths)
            matches = [
                row
                for row in events
                if row.get("journal_event_type") == "prediction_snapshot"
                and row.get("snapshot_id") == snapshot_id
            ]
            if not matches:
                raise JournalSafetyError("unknown snapshot_id: %s" % snapshot_id)
            snapshot = matches[0]
            update = self._build_label_update(
                snapshot,
                price_points,
                as_of=as_of,
                horizon_targets=horizon_targets,
                costs=costs,
                validation_plan=validation_plan,
            )

            existing = [
                row
                for row in events
                if row.get("journal_event_id") == update["journal_event_id"]
            ]
            if existing:
                if (
                    existing[0].get("journal_payload_sha256")
                    != update["journal_payload_sha256"]
                ):
                    raise JournalConflictError(
                        "journal_event_id %s already exists with different content"
                        % update["journal_event_id"]
                    )
                return self._result("idempotent", existing[0])
            self._append_unlocked(update, locked_paths)
        return self._result("appended", update)

    def read_events(self) -> list[dict[str, Any]]:
        """Read and integrity-check immutable journal events."""

        self._check_paths()
        if not os.path.exists(str(self.path)):
            return []
        with self._locked(exclusive=False, create_parent=False) as locked_paths:
            return deepcopy(self._load_unlocked(locked_paths))

    def read_frozen(self, *, as_of: Any) -> FrozenJournalView:
        """Freeze one canonical evidence-availability view of the journal.

        Every physical event is integrity checked exactly once.  Missing,
        invalid, or timezone-naive availability/receipt timestamps block the
        run.  Events received after the cutoff remain in the append-only source
        but are excluded from this immutable input view.
        """

        cutoff = _parse_aware_timestamp(as_of, field_name="as_of", line_number=0)
        self._check_paths()
        if os.path.exists(str(self.path)):
            with self._locked(exclusive=False, create_parent=False) as locked_paths:
                events, byte_count, file_sha, inode, source_hasher = (
                    self._load_with_state_unlocked(locked_paths)
                )
        else:
            events, byte_count, file_sha, inode, source_hasher = (
                [],
                0,
                sha256().hexdigest(),
                None,
                sha256(),
            )
            self._metrics["journal_parse_count"] += 1

        included: list[dict[str, Any]] = []
        excluded_after = 0
        max_evidence: Optional[datetime] = None
        for sequence, event in enumerate(events, start=1):
            evidence_at = _event_evidence_available_at(event, sequence)
            if evidence_at > cutoff:
                excluded_after += 1
                continue
            included.append(event)
            if max_evidence is None or evidence_at > max_evidence:
                max_evidence = evidence_at

        predictions: dict[str, dict[str, Any]] = {}
        events_by_id: dict[str, dict[str, Any]] = {}
        label_updates: dict[str, list[dict[str, Any]]] = {}
        cost_events: dict[str, list[dict[str, Any]]] = {}
        for event in included:
            event_id = str(event.get("journal_event_id") or "").strip()
            if event_id:
                prior = events_by_id.get(event_id)
                if prior is not None and prior.get(
                    "journal_payload_sha256"
                ) != event.get("journal_payload_sha256"):
                    raise JournalConflictError(
                        "journal_event_id %s has conflicting immutable payloads"
                        % event_id
                    )
                events_by_id[event_id] = event
            event_type = event.get("journal_event_type")
            snapshot_id = str(event.get("snapshot_id") or "").strip()
            if event_type == "prediction_snapshot":
                if not snapshot_id:
                    raise JournalSafetyError("prediction snapshot has no snapshot_id")
                if snapshot_id in predictions:
                    raise JournalConflictError(
                        "duplicate prediction snapshot_id: %s" % snapshot_id
                    )
                predictions[snapshot_id] = event
            elif event_type == "forward_label_update" and snapshot_id:
                label_updates.setdefault(snapshot_id, []).append(event)
            prediction_snapshot_id = str(
                event.get("prediction_snapshot_id") or ""
            ).strip()
            if prediction_snapshot_id and (
                event.get("record_type") == "completed_round_trip"
                or event.get("round_trip_complete") is True
            ):
                cost_events.setdefault(prediction_snapshot_id, []).append(event)

        return FrozenJournalView(
            data_as_of=cutoff.isoformat(timespec="seconds"),
            journal_head_event_count=len(included),
            journal_head_sha256=_events_sha256(included),
            max_evidence_available_at=(
                max_evidence.isoformat(timespec="seconds")
                if max_evidence is not None
                else None
            ),
            excluded_after_as_of_count=excluded_after,
            journal_source_event_count=len(events),
            journal_source_byte_count=byte_count,
            journal_source_sha256=file_sha,
            journal_source_inode=inode,
            _events=tuple(deepcopy(included)),
            _source_hasher=source_hasher.copy(),
            _predictions_by_id=deepcopy(predictions),
            _events_by_id=deepcopy(events_by_id),
            _label_updates_by_snapshot={
                key: tuple(deepcopy(value)) for key, value in label_updates.items()
            },
            _cost_events_by_snapshot={
                key: tuple(deepcopy(value)) for key, value in cost_events.items()
            },
        )

    @staticmethod
    def _label_update_sort_key(
        event: Mapping[str, Any], sequence: int
    ) -> tuple[float, str, int]:
        raw = str(event.get("labels_as_of") or "")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            chronological = parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
            chronological = float("-inf")
        return chronological, raw, sequence

    @classmethod
    def project_sample_records(
        cls, events: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Project records from one supplied event view without journal I/O."""

        latest_updates: dict[str, tuple[tuple[float, str, int], dict[str, Any]]] = {}
        for sequence, event in enumerate(events):
            if event.get("journal_event_type") != "forward_label_update":
                continue
            snapshot_id = str(event.get("snapshot_id") or "")
            sort_key = cls._label_update_sort_key(event, sequence)
            current = latest_updates.get(snapshot_id)
            if current is None or sort_key >= current[0]:
                latest_updates[snapshot_id] = (sort_key, dict(event))

        projected: list[dict[str, Any]] = []
        for event in events:
            event_type = event.get("journal_event_type")
            if event_type == "forward_label_update":
                continue
            row = deepcopy(event)
            if event_type == "prediction_snapshot":
                latest = latest_updates.get(str(row.get("snapshot_id") or ""))
                if latest is not None:
                    update = latest[1]
                    row["labels_as_of"] = update.get("labels_as_of")
                    row["labels"] = deepcopy(update.get("labels") or {})
                    row["label_aliases"] = deepcopy(update.get("label_aliases") or {})
            projected.append(_force_sim_only(row))
        return projected

    def latest_sample_records(self) -> list[dict[str, Any]]:
        """Merge the latest label update into each prediction for read-side KPIs."""

        return self.project_sample_records(self.read_events())

    @classmethod
    def build_kpi_from_events(
        cls,
        events: Sequence[Mapping[str, Any]],
        *,
        portfolio_snapshot: Optional[Mapping[str, Any]] = None,
        authority_scope: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Build KPIs from one already-frozen event view without journal I/O."""

        if portfolio_snapshot is not None:
            _reject_live_markers(portfolio_snapshot)
        current_authority = _current_authority_scope(authority_scope)
        if portfolio_snapshot is not None and not _record_in_authority(
            portfolio_snapshot, current_authority
        ):
            raise JournalSafetyError(
                "portfolio authority does not match current A-share sample authority"
            )
        records = cls.project_sample_records(events)
        evidence_index = build_strict_execution_evidence_index(events)
        current_records = [
            record
            for record in records
            if _record_in_authority(record, current_authority)
        ]
        excluded_legacy = len(records) - len(current_records)
        valid_current_records = [
            record
            for record in current_records
            if _strict_evolution_evidence(
                record,
                evidence_index=evidence_index,
                authority_scope=current_authority,
            )
        ]
        invalid_evolution_evidence_count = len(current_records) - len(
            valid_current_records
        )
        maturity_records = [
            record
            for record in valid_current_records
            if _has_positive_maturity_weight(record)
        ]
        duplicate_count = len(valid_current_records) - len(maturity_records)
        result = build_sample_kpi(
            maturity_records,
            portfolio_snapshot=deepcopy(portfolio_snapshot),
        )
        result["authority_scope"] = deepcopy(current_authority)
        result["raw_current_authority_record_count"] = len(current_records)
        result["excluded_legacy_count"] = excluded_legacy
        result["invalid_evolution_evidence_count"] = invalid_evolution_evidence_count
        result["maturity_duplicate_count"] = duplicate_count
        result["maturity_effective_record_count"] = len(maturity_records)
        result["automatic_promotion_enabled"] = False
        result["automatic_risk_expansion_enabled"] = False
        result["promotion_state"] = "manual_review_only"
        result["real_trading_enabled"] = False
        result["live_execution_enabled"] = False
        return result

    def build_kpi(
        self,
        *,
        portfolio_snapshot: Optional[Mapping[str, Any]] = None,
        authority_scope: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Build KPIs only from the current fresh-start authority generation."""

        return self.build_kpi_from_events(
            self.read_events(),
            portfolio_snapshot=deepcopy(portfolio_snapshot),
            authority_scope=authority_scope,
        )


__all__ = [
    "build_strict_execution_evidence_index",
    "canonical_execution_local_trade_payload",
    "canonical_execution_payload_sha256",
    "canonical_execution_receipt_payload",
    "JOURNAL_SCHEMA_VERSION",
    "JournalConflictError",
    "JournalError",
    "JournalSafetyError",
    "FrozenJournalView",
    "prediction_content_sha256",
    "prediction_source_payload_sha256",
    "SampleJournal",
    "seal_strict_execution_event",
    "strict_round_trip_content_sha256",
    "strict_round_trip_source_sha256",
    "validate_strict_completed_round_trip_evidence",
]
