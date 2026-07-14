#!/usr/bin/env python3
"""Pure prediction-snapshot and forward-label helpers.

The observation path is intentionally independent from execution eligibility:
strategy thresholds may explain why a candidate was not traded, but only
unreliable price evidence makes a forward label ineligible.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Any, Mapping, Optional, Sequence


CANONICAL_HORIZONS = ("m30", "m60", "close", "1d", "3d", "5d")
HORIZON_ALIASES = {"next-day": "1d", "next_day": "1d"}
PRIMARY_HORIZON_POLICY_VERSION = "ashare-primary-horizon-v1"
SAMPLE_SCIENCE_CONTRACT_VERSION = "ashare-sample-science-v1"
DEFAULT_PRIMARY_LABEL_HORIZON = "1d"
PIT_TIMESTAMP_FIELDS = ("event_time", "available_at", "ingested_at", "retrieved_as_of")
EVENT_TIME_ALIASES = (
    "event_time",
    "source_event_time",
    "timestamp",
    "observed_at",
    "bar_time",
    "trade_time",
    "datetime",
)
AVAILABILITY_TIME_ALIASES = (
    "available_at",
    "evidence_available_at",
    "published_at",
)
INGESTION_TIME_ALIASES = (
    "ingested_at",
    "received_at",
    "receipt_at",
    "collected_at",
    "collected_at_dt",
)
RETRIEVAL_TIME_ALIASES = ("retrieved_as_of", "retrieved_at")
EVIDENCE_ENVELOPE_GROUPS = {
    "event_time_fields": EVENT_TIME_ALIASES,
    "availability_time_fields": AVAILABILITY_TIME_ALIASES,
    "ingestion_time_fields": INGESTION_TIME_ALIASES,
    "retrieval_time_fields": RETRIEVAL_TIME_ALIASES,
}
MAX_EVIDENCE_LAG = {
    "m30": timedelta(minutes=15),
    "m60": timedelta(minutes=15),
    "close": timedelta(minutes=30),
    "1d": timedelta(hours=18),
    "3d": timedelta(hours=18),
    "5d": timedelta(hours=18),
}

_LIVE_BOOLEAN_FIELDS = (
    "real_trading_enabled",
    "live_execution_enabled",
    "real_money_enabled",
    "live_broker_enabled",
    "direct_execution_enabled",
    "is_live",
)


def canonical_horizon(value: Any) -> str:
    """Return the canonical horizon name, accepting the next-day alias."""

    normalized = str(value or "").strip().lower()
    normalized = HORIZON_ALIASES.get(normalized, normalized)
    if normalized not in CANONICAL_HORIZONS:
        raise ValueError("unsupported forward-label horizon: %s" % value)
    return normalized


def _parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _parse_aware_datetime(value: Any) -> Optional[datetime]:
    parsed = _parse_datetime(value)
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def evidence_envelope_from_record(
    record: Mapping[str, Any],
    *,
    extra_event_fields: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Collect every present event and receipt alias before normalization.

    The same aliases are collected at the record root, the PIT-lineage root,
    and ``point_in_time_lineage.timestamps``.  Adapter-provided envelopes are
    merged with path prefixes so a later canonical convenience field can never
    overwrite the original provider value that produced it.
    """

    envelope: dict[str, Any] = {group: {} for group in EVIDENCE_ENVELOPE_GROUPS}
    structure_errors: list[str] = []

    def collect(source: Mapping[str, Any], *, prefix: str) -> None:
        for group, aliases in EVIDENCE_ENVELOPE_GROUPS.items():
            target = envelope[group]
            for alias in aliases:
                value = source.get(alias)
                if value not in (None, ""):
                    path = "%s.%s" % (prefix, alias) if prefix else alias
                    target[path] = value

    collect(record, prefix="")
    for lineage_name in ("point_in_time_lineage", "pit_lineage"):
        lineage = record.get(lineage_name)
        if lineage is None:
            continue
        if not isinstance(lineage, Mapping):
            structure_errors.append(lineage_name)
            continue
        collect(lineage, prefix=lineage_name)
        timestamps = lineage.get("timestamps")
        if timestamps is None:
            continue
        if not isinstance(timestamps, Mapping):
            structure_errors.append("%s.timestamps" % lineage_name)
            continue
        collect(timestamps, prefix="%s.timestamps" % lineage_name)

    embedded = record.get("evidence_envelope")
    if embedded is not None:
        if not isinstance(embedded, Mapping):
            structure_errors.append("evidence_envelope")
        else:
            embedded_structure_errors = embedded.get("structure_errors")
            if embedded_structure_errors is not None:
                if isinstance(embedded_structure_errors, Sequence) and not isinstance(
                    embedded_structure_errors, (str, bytes, bytearray)
                ):
                    for raw_error in embedded_structure_errors:
                        error = str(raw_error or "").strip()
                        if error:
                            structure_errors.append(error)
                else:
                    structure_errors.append("evidence_envelope.structure_errors")
            for group in EVIDENCE_ENVELOPE_GROUPS:
                values = embedded.get(group)
                if values is None:
                    continue
                if not isinstance(values, Mapping):
                    structure_errors.append("evidence_envelope.%s" % group)
                    continue
                target = envelope[group]
                for path, value in values.items():
                    if value not in (None, ""):
                        target["evidence_envelope.%s" % path] = value

    if extra_event_fields:
        target = envelope["event_time_fields"]
        for path, value in extra_event_fields.items():
            if value not in (None, ""):
                target["explicit.%s" % path] = value
    envelope["structure_errors"] = list(dict.fromkeys(structure_errors))
    return envelope


def validate_evidence_envelope(
    envelope: Mapping[str, Any],
    *,
    boundary: Optional[datetime] = None,
    require_receipts: bool = True,
) -> dict[str, Any]:
    """Validate one raw EvidenceEnvelope without discarding any alias.

    Event aliases must denote one UTC instant.  Receipt aliases may represent
    distinct stages, so every present value is parsed and the conservative
    latest receipt controls the boundary.  Missing canonical receipt stages are
    derived only from present source receipts, never from wall clock or task
    ``as_of``.
    """

    structure_errors = envelope.get("structure_errors")
    if structure_errors:
        return {
            "status": "invalid_envelope_structure",
            "complete": False,
            "structure_errors": list(structure_errors),
        }

    parsed_groups: dict[str, list[tuple[str, datetime]]] = {}
    field_validation: dict[str, dict[str, Any]] = {}
    invalid_fields: list[str] = []
    for group in EVIDENCE_ENVELOPE_GROUPS:
        raw_group = envelope.get(group)
        if raw_group is None:
            raw_group = {}
        if not isinstance(raw_group, Mapping):
            return {
                "status": "invalid_envelope_structure",
                "complete": False,
                "structure_errors": [group],
            }
        parsed_group: list[tuple[str, datetime]] = []
        for path, value in raw_group.items():
            raw = str(value or "").strip()
            parsed = _parse_datetime(raw)
            basename = str(path).rsplit(".", 1)[-1]
            allow_exchange_local = group == "event_time_fields" and basename in {
                "bar_time",
                "trade_time",
            }
            rule = "none"
            reason = None
            if parsed is None:
                reason = "invalid_timestamp"
            elif parsed.tzinfo is None or parsed.utcoffset() is None:
                if allow_exchange_local:
                    parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
                    rule = "ashare_exchange_local_attach_asia_shanghai"
                else:
                    reason = "timezone_naive_timestamp"
            if reason is not None or parsed is None:
                invalid_fields.append(str(path))
                field_validation[str(path)] = {
                    "raw_value": raw or None,
                    "normalized_value": None,
                    "valid": False,
                    "reason": reason,
                }
                continue
            if group == "event_time_fields":
                normalized = parsed.astimezone(timezone(timedelta(hours=8)))
                if rule == "none":
                    rule = "convert_aware_instant_to_asia_shanghai"
            else:
                normalized = parsed.astimezone(timezone.utc)
                rule = "convert_aware_instant_to_utc"
            parsed_group.append((str(path), parsed))
            field_validation[str(path)] = {
                "raw_value": raw,
                "normalized_value": normalized.isoformat(timespec="seconds"),
                "valid": True,
                "reason": None,
                "normalization_rule": rule,
            }
        parsed_groups[group] = parsed_group

    if invalid_fields:
        return {
            "status": "invalid_or_timezone_naive_timestamp",
            "complete": False,
            "invalid_fields": invalid_fields,
            "fields": field_validation,
        }

    event_fields = parsed_groups["event_time_fields"]
    if not event_fields:
        return {
            "status": "missing_event_time",
            "complete": False,
            "fields": field_validation,
        }
    event_instants = {value.astimezone(timezone.utc) for _, value in event_fields}
    if len(event_instants) != 1:
        return {
            "status": "event_time_conflict",
            "complete": False,
            "fields": field_validation,
            "event_time_fields": [path for path, _ in event_fields],
        }
    event_at = event_fields[0][1]

    availability = parsed_groups["availability_time_fields"]
    ingestion = parsed_groups["ingestion_time_fields"]
    retrieval = parsed_groups["retrieval_time_fields"]
    receipts = availability + ingestion + retrieval
    if require_receipts and not receipts:
        return {
            "status": "missing_receipt_timestamps",
            "complete": False,
            "fields": field_validation,
        }

    canonical: dict[str, str] = {
        "event_time": event_at.astimezone(timezone(timedelta(hours=8))).isoformat(
            timespec="seconds"
        )
    }
    latest_receipt: Optional[datetime] = None
    if receipts:
        latest_receipt = max(value for _, value in receipts)
        earliest_receipt = min(value for _, value in receipts)
        effective_availability = (
            [value for _, value in availability] if availability else [earliest_receipt]
        )
        effective_ingestion = (
            [value for _, value in ingestion]
            if ingestion
            else [max(effective_availability)]
        )
        effective_retrieval = (
            [value for _, value in retrieval] if retrieval else [latest_receipt]
        )
        available_at = max(effective_availability)
        ingested_at = max(effective_ingestion)
        retrieved_at = max(effective_retrieval)
        event_utc = event_at.astimezone(timezone.utc)
        receipt_order_is_valid = (
            event_utc <= min(value.astimezone(timezone.utc) for _, value in receipts)
            and max(value.astimezone(timezone.utc) for value in effective_availability)
            <= min(value.astimezone(timezone.utc) for value in effective_ingestion)
            and max(value.astimezone(timezone.utc) for value in effective_ingestion)
            <= min(value.astimezone(timezone.utc) for value in effective_retrieval)
        )
        if not receipt_order_is_valid:
            return {
                "status": "invalid_receipt_order",
                "complete": False,
                "fields": field_validation,
                "canonical_timestamps": {
                    "event_time": canonical["event_time"],
                    "available_at": available_at.astimezone(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    "ingested_at": ingested_at.astimezone(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    "retrieved_as_of": retrieved_at.astimezone(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                },
            }
        canonical.update(
            {
                "available_at": available_at.astimezone(timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "ingested_at": ingested_at.astimezone(timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "retrieved_as_of": retrieved_at.astimezone(timezone.utc).isoformat(
                    timespec="seconds"
                ),
            }
        )
        if boundary is not None and latest_receipt.astimezone(
            timezone.utc
        ) > boundary.astimezone(timezone.utc):
            return {
                "status": "receipt_after_boundary",
                "complete": False,
                "fields": field_validation,
                "canonical_timestamps": canonical,
                "max_evidence_receipt_at": latest_receipt.astimezone(
                    timezone.utc
                ).isoformat(timespec="seconds"),
            }

    return {
        "status": "valid",
        "complete": True,
        "fields": field_validation,
        "canonical_timestamps": canonical,
        "max_evidence_receipt_at": (
            latest_receipt.astimezone(timezone.utc).isoformat(timespec="seconds")
            if latest_receipt is not None
            else None
        ),
        "evidence_envelope": deepcopy(dict(envelope)),
    }


def canonicalize_evidence_record(
    record: Mapping[str, Any],
    *,
    boundary: Optional[datetime] = None,
    extra_event_fields: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Preserve, validate, and only then canonicalize one evidence record.

    The returned record always contains the complete raw ``evidence_envelope``
    and its validation result.  Canonical top-level and nested PIT timestamps
    are emitted only when every present event/receipt alias is timezone-aware,
    internally consistent, correctly ordered, and no later than ``boundary``.
    Missing or invalid provider receipts are never filled from the boundary or
    wall clock.
    """

    result = deepcopy(dict(record))
    envelope = evidence_envelope_from_record(
        result,
        extra_event_fields=extra_event_fields,
    )
    validation = validate_evidence_envelope(
        envelope,
        boundary=boundary,
        require_receipts=True,
    )
    result["evidence_envelope"] = envelope
    result["evidence_envelope_validation"] = deepcopy(validation)
    if validation.get("complete") is not True or validation.get("status") != "valid":
        return result

    canonical = validation.get("canonical_timestamps")
    if not isinstance(canonical, Mapping):
        return result
    for field in PIT_TIMESTAMP_FIELDS:
        value = canonical.get(field)
        if value not in (None, ""):
            result.setdefault(field, value)
    result.setdefault("source_event_time", canonical.get("event_time"))

    raw_lineage = result.get("point_in_time_lineage")
    lineage = deepcopy(dict(raw_lineage)) if isinstance(raw_lineage, Mapping) else {}
    lineage.update(
        {
            "status": "valid",
            "complete": True,
            "timestamps": {
                field: canonical.get(field) for field in PIT_TIMESTAMP_FIELDS
            },
            "evidence_envelope": deepcopy(envelope),
        }
    )
    result["point_in_time_lineage"] = lineage
    return result


def validate_point_in_time_lineage(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an explicit point-in-time availability chain.

    A source hash or generic ``as_of`` marker is not enough to prove a
    point-in-time join.  Scientific evidence requires all four timestamps and
    the ordering ``event <= available <= ingested <= retrieved``.  When a
    prediction/label boundary is supplied, retrieval must not be later than
    that boundary.  This validator is diagnostic only: an incomplete chain
    does not suppress observation or forward-label collection.
    """

    if not isinstance(record, Mapping):
        raise TypeError("point-in-time record must be a mapping")
    nested = record.get("point_in_time_lineage")
    if not isinstance(nested, Mapping):
        nested = record.get("pit_lineage")
    source = nested if isinstance(nested, Mapping) else record
    if isinstance(source.get("timestamps"), Mapping):
        source = source["timestamps"]
    raw_values = {field: source.get(field) for field in PIT_TIMESTAMP_FIELDS}
    missing = [field for field, value in raw_values.items() if value in (None, "")]
    if missing:
        return {
            "status": "missing_timestamps",
            "complete": False,
            "missing_fields": missing,
            "timestamps": {field: None for field in PIT_TIMESTAMP_FIELDS},
        }

    parsed: dict[str, datetime] = {}
    invalid: list[str] = []
    for field, value in raw_values.items():
        timestamp = _parse_datetime(value)
        if (
            timestamp is None
            or timestamp.tzinfo is None
            or timestamp.utcoffset() is None
        ):
            invalid.append(field)
        else:
            parsed[field] = timestamp
    if invalid:
        return {
            "status": "invalid_or_timezone_naive_timestamps",
            "complete": False,
            "missing_fields": [],
            "invalid_fields": invalid,
            "timestamps": {
                field: _iso(parsed[field]) if field in parsed else None
                for field in PIT_TIMESTAMP_FIELDS
            },
        }

    ordered = [parsed[field] for field in PIT_TIMESTAMP_FIELDS]
    if any(later < earlier for earlier, later in zip(ordered, ordered[1:])):
        return {
            "status": "invalid_timestamp_order",
            "complete": False,
            "missing_fields": [],
            "timestamps": {
                field: _iso(parsed[field]) for field in PIT_TIMESTAMP_FIELDS
            },
        }

    boundary_raw = (
        record.get("prediction_at")
        or record.get("labels_as_of")
        or record.get("decision_as_of")
    )
    if boundary_raw not in (None, ""):
        boundary = _parse_datetime(boundary_raw)
        if boundary is None or boundary.tzinfo is None or boundary.utcoffset() is None:
            return {
                "status": "invalid_or_timezone_naive_boundary",
                "complete": False,
                "missing_fields": [],
                "timestamps": {
                    field: _iso(parsed[field]) for field in PIT_TIMESTAMP_FIELDS
                },
            }
        if parsed["retrieved_as_of"] > boundary:
            return {
                "status": "retrieved_after_decision_boundary",
                "complete": False,
                "missing_fields": [],
                "timestamps": {
                    field: _iso(parsed[field]) for field in PIT_TIMESTAMP_FIELDS
                },
                "decision_boundary": _iso(boundary),
            }

    return {
        "status": "valid",
        "complete": True,
        "missing_fields": [],
        "timestamps": {field: _iso(parsed[field]) for field in PIT_TIMESTAMP_FIELDS},
    }


def _iso(value: datetime) -> str:
    return value.isoformat()


def _as_positive_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def _reference_timestamp_lineage_status(
    quality: Mapping[str, Any], price_at: datetime
) -> tuple[str, str]:
    lineage = quality.get("reference_timestamp_lineage")
    if lineage is None:
        return "pending", "missing_reference_timestamp_lineage"
    if not isinstance(lineage, Mapping):
        return "rejected", "reference_timestamp_lineage_conflict"
    required_fields = (
        "source_field",
        "raw_value",
        "normalized_value",
        "timezone_semantics",
        "normalization_rule",
        "valid",
    )
    if any(lineage.get(field) in (None, "") for field in required_fields):
        return "pending", "incomplete_reference_timestamp_lineage"
    if lineage.get("valid") is not True:
        return "rejected", "reference_timestamp_lineage_conflict"
    normalized = _parse_aware_datetime(lineage.get("normalized_value"))
    if normalized is None or normalized.astimezone(timezone.utc) != price_at.astimezone(
        timezone.utc
    ):
        return "rejected", "reference_timestamp_lineage_conflict"
    raw = str(lineage.get("raw_value") or "").strip()
    source_field = str(lineage.get("source_field") or "").strip()
    rule = str(lineage.get("normalization_rule") or "").strip()
    semantics = str(lineage.get("timezone_semantics") or "").strip()
    if semantics not in {"ashare_exchange_event_time", "ashare_daily_close"}:
        return "rejected", "reference_timestamp_lineage_conflict"
    raw_parsed = _parse_datetime(raw)
    if raw_parsed is None:
        return "rejected", "reference_timestamp_lineage_conflict"
    if raw_parsed.tzinfo is None or raw_parsed.utcoffset() is None:
        if (
            source_field in {"bar_time", "trade_time"}
            and rule == "ashare_exchange_local_attach_asia_shanghai"
        ):
            raw_exchange_time = raw_parsed.replace(tzinfo=timezone(timedelta(hours=8)))
            matches = raw_exchange_time.astimezone(
                timezone.utc
            ) == normalized.astimezone(timezone.utc)
            return (
                ("eligible", "verified_reference_timestamp_lineage")
                if matches
                else ("rejected", "reference_timestamp_lineage_conflict")
            )
        if (
            source_field == "trade_date"
            and rule == "ashare_trade_date_to_15_00_asia_shanghai"
        ):
            normalized_shanghai = normalized.astimezone(timezone(timedelta(hours=8)))
            matches = bool(
                raw_parsed.date() == normalized_shanghai.date()
                and normalized_shanghai.hour == 15
                and normalized_shanghai.minute == 0
                and normalized_shanghai.second == 0
            )
            return (
                ("eligible", "verified_reference_timestamp_lineage")
                if matches
                else ("rejected", "reference_timestamp_lineage_conflict")
            )
        return "rejected", "reference_timestamp_lineage_conflict"
    matches = bool(
        rule
        in {
            "convert_aware_instant_to_asia_shanghai",
            "convert_aware_instant_to_utc",
        }
        and raw_parsed.astimezone(timezone.utc) == normalized.astimezone(timezone.utc)
    )
    return (
        ("eligible", "verified_reference_timestamp_lineage")
        if matches
        else ("rejected", "reference_timestamp_lineage_conflict")
    )


def _decision_timestamp_lineage_status(
    record: Mapping[str, Any], prediction_at: datetime, data_as_of: datetime
) -> tuple[str, str]:
    lineage = record.get("decision_timestamp_lineage")
    if lineage is None:
        return "pending", "missing_decision_timestamp_lineage"
    if not isinstance(lineage, Mapping):
        return "rejected", "decision_timestamp_lineage_conflict"
    for field, expected in (
        ("prediction_at", prediction_at),
        ("data_as_of", data_as_of),
    ):
        item = lineage.get(field)
        if not isinstance(item, Mapping):
            return "pending", "incomplete_decision_timestamp_lineage"
        required_fields = (
            "source_field",
            "raw_value",
            "normalized_value",
            "timezone_semantics",
            "normalization_rule",
            "valid",
        )
        if any(item.get(required) in (None, "") for required in required_fields):
            return "pending", "incomplete_decision_timestamp_lineage"
        if item.get("valid") is not True:
            return "rejected", "decision_timestamp_lineage_conflict"
        if str(item.get("source_field") or "").strip() != field:
            return "rejected", "decision_timestamp_lineage_conflict"
        if str(item.get("timezone_semantics") or "").strip() != "ashare_decision_time":
            return "rejected", "decision_timestamp_lineage_conflict"
        if (
            str(item.get("normalization_rule") or "").strip()
            != "convert_aware_instant_to_asia_shanghai"
        ):
            return "rejected", "decision_timestamp_lineage_conflict"
        raw = _parse_aware_datetime(item.get("raw_value"))
        normalized = _parse_aware_datetime(item.get("normalized_value"))
        if raw is None or normalized is None:
            return "rejected", "decision_timestamp_lineage_conflict"
        if raw.astimezone(timezone.utc) != normalized.astimezone(timezone.utc):
            return "rejected", "decision_timestamp_lineage_conflict"
        if normalized.astimezone(timezone.utc) != expected.astimezone(timezone.utc):
            return "rejected", "decision_timestamp_lineage_conflict"
    return "eligible", "verified_decision_timestamp_lineage"


def _prediction_data_quality(record: Mapping[str, Any]) -> tuple[str, str]:
    prediction_raw = record.get("prediction_at")
    prediction_at = _parse_aware_datetime(prediction_raw)
    if prediction_at is None:
        return "rejected", (
            "missing_prediction_timestamp"
            if prediction_raw in (None, "")
            else "prediction_timestamp_timezone_mismatch"
        )

    quality = record.get("data_quality")
    if not isinstance(quality, Mapping):
        return (
            ("pending", "missing_reference_price")
            if _as_positive_float(record.get("reference_price")) is None
            else ("rejected", "missing_data_quality_evidence")
        )

    price_raw = quality.get("price_timestamp")
    price_at = _parse_aware_datetime(price_raw)
    if price_raw not in (None, "") and price_at is None:
        return "rejected", "reference_timestamp_timezone_mismatch"
    if "data_as_of" in record:
        data_as_of_raw = record.get("data_as_of")
    elif "point_in_time_as_of" in record:
        data_as_of_raw = record.get("point_in_time_as_of")
    elif "as_of" in record:
        data_as_of_raw = record.get("as_of")
    else:
        data_as_of_raw = prediction_raw
    data_as_of = _parse_aware_datetime(data_as_of_raw)
    if data_as_of is None:
        return "rejected", "data_as_of_timestamp_timezone_mismatch"
    if record.get("evidence_envelope") is not None:
        envelope_validation = validate_evidence_envelope(
            evidence_envelope_from_record(record),
            boundary=prediction_at,
            require_receipts=True,
        )
        if (
            envelope_validation.get("complete") is not True
            or envelope_validation.get("status") != "valid"
        ):
            envelope_status = str(envelope_validation.get("status") or "invalid")
            return (
                (
                    "pending"
                    if envelope_status
                    in {"missing_receipt_timestamps", "receipt_after_boundary"}
                    else "rejected"
                ),
                "reference_evidence_envelope_%s" % envelope_status,
            )
    prediction_utc = prediction_at.astimezone(timezone.utc)
    data_as_of_utc = data_as_of.astimezone(timezone.utc)
    if data_as_of_utc > prediction_utc:
        return "rejected", "data_as_of_after_prediction"
    if price_at is not None and price_at.astimezone(timezone.utc) > prediction_utc:
        return "rejected", "reference_price_after_prediction"
    if price_at is not None and price_at.astimezone(timezone.utc) > data_as_of_utc:
        return "rejected", "reference_price_after_data_as_of"

    if _as_positive_float(record.get("reference_price")) is None:
        return "pending", "missing_reference_price"
    if quality.get("reliable") is not True:
        return "rejected", "unreliable_reference_data"
    if not str(quality.get("source") or "").strip():
        return "rejected", "missing_reference_source"
    if price_at is None:
        return "rejected", "missing_reference_timestamp"
    reference_lineage_status, reference_lineage_reason = (
        _reference_timestamp_lineage_status(quality, price_at)
    )
    if reference_lineage_status != "eligible":
        return reference_lineage_status, reference_lineage_reason
    decision_lineage_status, decision_lineage_reason = (
        _decision_timestamp_lineage_status(record, prediction_at, data_as_of)
    )
    if decision_lineage_status != "eligible":
        return decision_lineage_status, decision_lineage_reason
    return "eligible", "verified_reference_data"


def _stable_snapshot_id(record: Mapping[str, Any]) -> str:
    identity = {
        "market": record.get("market"),
        "symbol": record.get("symbol"),
        "style": record.get("style") or record.get("style_id"),
        "strategy_version": record.get("strategy_version")
        or record.get("style_version"),
        "prediction_at": record.get("prediction_at"),
    }
    encoded = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    return "prediction:" + sha256(encoded).hexdigest()[:24]


def _stable_label_update_id(
    snapshot_id: str,
    labels_as_of: str,
    cost_model_version: Optional[str],
    cost_evidence_id: Optional[str] = None,
) -> str:
    """Build a stable idempotency fingerprint that includes the cost version.

    Old 0-cost labels must never silently collide with versioned labels.
    When *cost_evidence_id* is provided (e.g. an actual execution event id),
    different evidence sources produce different fingerprints, so a cost
    revision does not silently collide.
    """
    identity = {
        "snapshot_id": snapshot_id,
        "labels_as_of": labels_as_of,
        "cost_model_version": cost_model_version or "no_cost_evidence",
        "cost_evidence_id": cost_evidence_id or "",
    }
    encoded = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    return "forward_label_update:" + sha256(encoded).hexdigest()[:32]


def build_prediction_snapshot(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Copy a candidate into an immutable observation/counterfactual snapshot.

    A low score, incomplete research thesis, or execution rejection remains
    useful metadata and never suppresses the snapshot.  Missing or unreliable
    market data is recorded as label ineligibility instead of dropping it.
    """

    if not isinstance(candidate, Mapping):
        raise TypeError("candidate must be a mapping")
    snapshot = deepcopy(dict(candidate))
    eligibility, reason = _prediction_data_quality(snapshot)
    pit_validation = validate_point_in_time_lineage(snapshot)
    pit_status = str(pit_validation.get("status") or "")
    # Missing optional PIT fields remains audit-visible and label-collectable,
    # but a present-yet-invalid chain is positive evidence of an unsafe time
    # relationship and must fail closed.
    if eligibility == "eligible" and pit_status not in {
        "valid",
        "missing_timestamps",
    }:
        eligibility = "rejected"
        reason = "point_in_time_lineage_%s" % (pit_status or "invalid")

    snapshot["snapshot_id"] = str(
        snapshot.get("snapshot_id") or _stable_snapshot_id(snapshot)
    )
    snapshot["snapshot_status"] = "recorded"
    snapshot["sample_layer"] = "observation_counterfactual"
    snapshot["forward_label_eligibility"] = {
        "eligible": "eligible",
        "pending": "pending_reference_evidence",
        "rejected": "rejected_data_quality",
    }[eligibility]
    snapshot["forward_label_rejection_reason"] = (
        reason if eligibility == "rejected" else None
    )
    snapshot["forward_label_pending_reason"] = (
        reason if eligibility == "pending" else None
    )
    snapshot["reference_evidence_status"] = reason
    snapshot["primary_label_horizon"] = canonical_horizon(
        snapshot.get("primary_label_horizon") or DEFAULT_PRIMARY_LABEL_HORIZON
    )
    snapshot["primary_horizon_policy_version"] = str(
        snapshot.get("primary_horizon_policy_version") or PRIMARY_HORIZON_POLICY_VERSION
    )
    snapshot["sample_science_contract_version"] = str(
        snapshot.get("sample_science_contract_version")
        or SAMPLE_SCIENCE_CONTRACT_VERSION
    )
    snapshot["point_in_time_lineage_validation"] = pit_validation
    snapshot["capital_layer"] = "simulated"
    snapshot["account_type"] = "simulated"
    for field in _LIVE_BOOLEAN_FIELDS:
        snapshot[field] = False
    return snapshot


def _default_horizon_targets(prediction_at: datetime) -> dict[str, datetime]:
    close_at = prediction_at.replace(hour=15, minute=0, second=0, microsecond=0)
    if close_at <= prediction_at:
        close_at += timedelta(days=1)
    return {
        "m30": prediction_at + timedelta(minutes=30),
        "m60": prediction_at + timedelta(minutes=60),
        "close": close_at,
        "1d": prediction_at + timedelta(days=1),
        "3d": prediction_at + timedelta(days=3),
        "5d": prediction_at + timedelta(days=5),
    }


def _normalized_targets(
    prediction_at: datetime, horizon_targets: Optional[Mapping[str, Any]]
) -> dict[str, datetime]:
    targets = _default_horizon_targets(prediction_at)
    if horizon_targets is not None:
        for raw_name, raw_target in horizon_targets.items():
            name = canonical_horizon(raw_name)
            parsed = _parse_datetime(raw_target)
            if parsed is None:
                raise ValueError("invalid target timestamp for horizon %s" % raw_name)
            try:
                if parsed < prediction_at:
                    raise ValueError(
                        "target timestamp cannot precede prediction_at: %s" % raw_name
                    )
            except TypeError:
                raise ValueError(
                    "horizon targets and prediction_at must use compatible timezones"
                )
            targets[name] = parsed
    return targets


def _direction_multiplier(value: Any) -> int:
    normalized = str(value or "long").strip().lower()
    if normalized in {"long", "buy", "bullish", "up", "1", "+1"}:
        return 1
    if normalized in {"short", "sell", "bearish", "down", "-1"}:
        return -1
    if normalized in {"hold", "flat", "neutral", "abstain", "0"}:
        return 0
    raise ValueError("unsupported prediction direction: %s" % value)


def _cost_bps(
    snapshot: Mapping[str, Any], costs: Optional[Mapping[str, Any]]
) -> tuple[float, float, Optional[str], Optional[str]]:
    """Extract versioned cost evidence.

    Returns ``(fee_bps, slippage_bps, cost_model_version, cost_evidence_id)``.
    Raises ``ValueError`` when no versioned cost evidence is present,
    so that a caller can fail closed or label the sample
    ``rejected_missing_cost_evidence``.
    """
    merged: dict[str, Any] = {}
    embedded = snapshot.get("costs")
    if isinstance(embedded, Mapping):
        merged.update(embedded)
    if isinstance(costs, Mapping):
        merged.update(costs)

    cost_model_version = str(merged.get("cost_model_version") or "").strip() or None
    cost_evidence_id = str(merged.get("cost_evidence_event_id") or "").strip() or None
    if not cost_model_version:
        raise ValueError("missing_cost_model_version")

    def number(name: str) -> float:
        try:
            return max(float(merged.get(name) or 0.0), 0.0)
        except (TypeError, ValueError):
            raise ValueError("%s must be numeric" % name)

    if "round_trip_fee_bps" in merged:
        fee = number("round_trip_fee_bps")
    elif "entry_fee_bps" in merged or "exit_fee_bps" in merged:
        fee = number("entry_fee_bps") + number("exit_fee_bps")
    else:
        fee = number("fee_bps")

    if "round_trip_slippage_bps" in merged:
        slippage = number("round_trip_slippage_bps")
    elif "entry_slippage_bps" in merged or "exit_slippage_bps" in merged:
        slippage = number("entry_slippage_bps") + number("exit_slippage_bps")
    else:
        slippage = number("slippage_bps")
    return fee, slippage, cost_model_version, cost_evidence_id


def _event_time_fields(
    record: Mapping[str, Any], field_names: Sequence[str]
) -> dict[str, Any]:
    return {
        field: record.get(field)
        for field in field_names
        if record.get(field) not in (None, "")
    }


def _point_event_time_fields(point: Mapping[str, Any]) -> dict[str, Any]:
    return _event_time_fields(point, EVENT_TIME_ALIASES)


def _reference_event_time_fields(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    fields = _event_time_fields(snapshot, EVENT_TIME_ALIASES)
    quality = snapshot.get("data_quality")
    if isinstance(quality, Mapping) and quality.get("price_timestamp") not in (
        None,
        "",
    ):
        fields["data_quality.price_timestamp"] = quality.get("price_timestamp")
    return fields


def _point_quality(point: Mapping[str, Any]) -> tuple[bool, str]:
    if point.get("reliable") is not True:
        return False, "unreliable_exit_evidence"
    if _as_positive_float(point.get("price")) is None:
        return False, "invalid_exit_price"
    if not str(point.get("source") or "").strip():
        return False, "missing_exit_source"
    return True, "verified_exit_evidence"


def _pit_evidence_gate(
    record: Mapping[str, Any],
    *,
    boundary_field: str,
    boundary: datetime,
    event_time_fields: Mapping[str, Any],
) -> tuple[bool, str, dict[str, Any], Optional[datetime]]:
    """Validate one immutable evidence record before it can be selected.

    The validator is deliberately recomputed from source timestamps instead of
    trusting a cached ``point_in_time_lineage_validation`` result.  Nested PIT
    timestamps, when present, are authoritative and are never replaced with
    more convenient top-level values.
    """

    payload = deepcopy(dict(record))
    if not event_time_fields:
        validation = {
            "status": "missing_top_level_event_time",
            "complete": False,
            "missing_fields": ["top_level_event_time"],
            "timestamps": {field: None for field in PIT_TIMESTAMP_FIELDS},
        }
        return (
            False,
            "point_in_time_lineage_missing_top_level_event_time",
            validation,
            None,
        )

    envelope = evidence_envelope_from_record(
        payload, extra_event_fields=event_time_fields
    )
    envelope_validation = validate_evidence_envelope(
        envelope, boundary=boundary, require_receipts=True
    )
    envelope_status = str(envelope_validation.get("status") or "invalid")
    if envelope_validation.get("complete") is not True or envelope_status != "valid":
        reason_by_status = {
            "missing_event_time": "point_in_time_lineage_missing_top_level_event_time",
            "event_time_conflict": "point_in_time_lineage_event_time_conflict",
            "invalid_or_timezone_naive_timestamp": (
                "point_in_time_lineage_invalid_or_timezone_naive_source_timestamp"
            ),
            "missing_receipt_timestamps": (
                "point_in_time_lineage_missing_receipt_timestamps"
            ),
            "invalid_receipt_order": "point_in_time_lineage_invalid_timestamp_order",
            "receipt_after_boundary": "point_in_time_lineage_receipt_after_boundary",
            "invalid_envelope_structure": (
                "point_in_time_lineage_invalid_evidence_envelope"
            ),
        }
        return (
            False,
            reason_by_status.get(
                envelope_status,
                "point_in_time_lineage_evidence_envelope_%s" % envelope_status,
            ),
            envelope_validation,
            None,
        )

    nested = payload.get("point_in_time_lineage")
    if not isinstance(nested, Mapping):
        nested = payload.get("pit_lineage")
    if not isinstance(nested, Mapping):
        validation = {
            "status": "missing_nested_lineage",
            "complete": False,
            "missing_fields": ["point_in_time_lineage"],
            "timestamps": {field: None for field in PIT_TIMESTAMP_FIELDS},
        }
        return (
            False,
            "point_in_time_lineage_missing_nested_lineage",
            validation,
            None,
        )
    # ``validate_point_in_time_lineage`` accepts several boundary aliases for
    # standalone diagnostics.  The evidence gate has exactly one task-owned
    # boundary, so remove competing aliases before validating.  Otherwise a
    # stray ``prediction_at`` on an exit point could take precedence over the
    # frozen labels cutoff and admit evidence received after ``as_of``.
    for alias in ("prediction_at", "labels_as_of", "decision_as_of"):
        if alias != boundary_field:
            payload.pop(alias, None)
    payload[boundary_field] = _iso(boundary)
    validation = validate_point_in_time_lineage(payload)
    status = str(validation.get("status") or "invalid")
    complete = validation.get("complete") is True
    if not complete or status != "valid":
        return False, "point_in_time_lineage_%s" % status, validation, None

    timestamps = validation.get("timestamps")
    canonical_event_at = _parse_aware_datetime(
        timestamps.get("event_time") if isinstance(timestamps, Mapping) else None
    )
    if canonical_event_at is None:
        failed = deepcopy(validation)
        failed["status"] = "invalid_nested_event_time"
        failed["complete"] = False
        return (
            False,
            "point_in_time_lineage_invalid_nested_event_time",
            failed,
            None,
        )

    envelope_timestamps = envelope_validation.get("canonical_timestamps")
    if not isinstance(envelope_timestamps, Mapping):
        failed = deepcopy(validation)
        failed.update(
            {
                "status": "invalid_evidence_envelope",
                "complete": False,
                "evidence_envelope_validation": envelope_validation,
            }
        )
        return (
            False,
            "point_in_time_lineage_invalid_evidence_envelope",
            failed,
            None,
        )
    for field in PIT_TIMESTAMP_FIELDS:
        envelope_at = _parse_aware_datetime(envelope_timestamps.get(field))
        nested_at = _parse_aware_datetime(
            timestamps.get(field) if isinstance(timestamps, Mapping) else None
        )
        if envelope_at is None or nested_at is None:
            failed = deepcopy(validation)
            failed.update(
                {
                    "status": "invalid_evidence_envelope",
                    "complete": False,
                    "evidence_envelope_validation": envelope_validation,
                }
            )
            return (
                False,
                "point_in_time_lineage_invalid_evidence_envelope",
                failed,
                None,
            )
        if envelope_at.astimezone(timezone.utc) != nested_at.astimezone(timezone.utc):
            failed = deepcopy(validation)
            failed.update(
                {
                    "status": "%s_conflict" % field,
                    "complete": False,
                    "evidence_envelope_validation": envelope_validation,
                }
            )
            return (
                False,
                (
                    "point_in_time_lineage_event_time_conflict"
                    if field == "event_time"
                    else "point_in_time_lineage_%s_alias_conflict" % field
                ),
                failed,
                None,
            )

    normalized_top: dict[str, str | None] = {}
    canonical_utc = canonical_event_at.astimezone(timezone.utc)
    for field, raw in event_time_fields.items():
        parsed = _parse_aware_datetime(raw)
        normalized_top[str(field)] = _iso(parsed) if parsed is not None else None
        if parsed is None:
            failed = deepcopy(validation)
            failed.update(
                {
                    "status": "invalid_or_timezone_naive_top_level_event_time",
                    "complete": False,
                    "top_level_event_times": normalized_top,
                }
            )
            return (
                False,
                "point_in_time_lineage_invalid_or_timezone_naive_top_level_event_time",
                failed,
                None,
            )
        if parsed.astimezone(timezone.utc) != canonical_utc:
            failed = deepcopy(validation)
            failed.update(
                {
                    "status": "event_time_conflict",
                    "complete": False,
                    "top_level_event_times": normalized_top,
                    "canonical_event_time": _iso(canonical_event_at),
                }
            )
            return (
                False,
                "point_in_time_lineage_event_time_conflict",
                failed,
                None,
            )
    verified = deepcopy(validation)
    verified["canonical_event_time"] = _iso(canonical_event_at)
    verified["top_level_event_times"] = normalized_top
    verified["evidence_envelope_validation"] = envelope_validation
    return True, "verified_point_in_time_lineage", verified, canonical_event_at


def _point_evidence_gate(
    point: Mapping[str, Any],
    *,
    as_of: datetime,
) -> tuple[str, str, dict[str, Any], Optional[datetime]]:
    """Return ``verified``, ``pending``, or ``rejected`` for one exit point."""

    quality_ok, quality_reason = _point_quality(point)
    pit_ok, pit_reason, pit_validation, canonical_event_at = _pit_evidence_gate(
        point,
        boundary_field="labels_as_of",
        boundary=as_of,
        event_time_fields=_point_event_time_fields(point),
    )
    if not pit_ok:
        # A later provider receipt or a newly arriving valid point may recover
        # this horizon.  Keep it nonterminal instead of fabricating readiness.
        return "pending", pit_reason, pit_validation, None
    if not quality_ok:
        return "rejected", quality_reason, pit_validation, canonical_event_at
    return "verified", "verified_exit_evidence", pit_validation, canonical_event_at


def _point_supports_horizon(point: Mapping[str, Any], horizon: str) -> bool:
    raw = point.get("eligible_horizons")
    if raw is None:
        return True
    if not isinstance(raw, (list, tuple, set)):
        return False
    supported: set[str] = set()
    for value in raw:
        try:
            supported.add(canonical_horizon(value))
        except ValueError:
            continue
    return horizon in supported


def _empty_label(
    name: str, target: datetime, status: str, reason: str
) -> dict[str, Any]:
    return {
        "horizon": name,
        "target_at": _iso(target),
        "status": status,
        "reason": reason,
        "evidence_at": None,
        "evidence_source": None,
        "exit_price": None,
        "market_return": None,
        "gross_return_after_direction": None,
        "fee_bps": None,
        "slippage_bps": None,
        "total_cost_bps": None,
        "cost_model_version": None,
        "cost_evidence_event_id": None,
        "net_return_after_costs": None,
        "outcome": None,
        "point_in_time_lineage": {
            "status": "unavailable_no_exit_evidence",
            "complete": False,
        },
    }


def materialize_forward_labels(
    snapshot: Mapping[str, Any],
    price_points: Sequence[Mapping[str, Any]],
    *,
    as_of: Any,
    horizon_targets: Optional[Mapping[str, Any]] = None,
    costs: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Materialize only labels due at ``as_of`` without looking into the future.

    Requires versioned cost evidence.  Fails closed with
    ``rejected_missing_cost_evidence`` when no cost model version is present.
    """

    if not isinstance(snapshot, Mapping):
        raise TypeError("snapshot must be a mapping")
    prediction_at = _parse_datetime(snapshot.get("prediction_at"))
    current_as_of = _parse_datetime(as_of)
    if prediction_at is None:
        raise ValueError("snapshot prediction_at is required")
    if current_as_of is None:
        raise ValueError("as_of must be an ISO timestamp or datetime")
    try:
        if current_as_of < prediction_at:
            raise ValueError("as_of cannot be before prediction_at")
    except TypeError:
        raise ValueError("as_of and prediction_at must use compatible timezones")

    targets = _normalized_targets(prediction_at, horizon_targets)
    entry_price = _as_positive_float(snapshot.get("reference_price"))
    multiplier = _direction_multiplier(snapshot.get("direction"))

    # Extract cost evidence; fail closed when no versioned cost model.
    try:
        fee_bps, slippage_bps, cost_model_version, cost_evidence_id = _cost_bps(
            snapshot, costs
        )
    except ValueError:
        fee_bps = 0.0
        slippage_bps = 0.0
        cost_model_version = None
        cost_evidence_id = None

    points = deepcopy(list(price_points))
    result = deepcopy(dict(snapshot))
    labels: dict[str, dict[str, Any]] = {}
    (
        reference_pit_ok,
        reference_pit_reason,
        reference_pit_validation,
        _reference_event_at,
    ) = _pit_evidence_gate(
        snapshot,
        boundary_field="prediction_at",
        boundary=prediction_at,
        event_time_fields=_reference_event_time_fields(snapshot),
    )

    for name in CANONICAL_HORIZONS:
        target = targets[name]
        try:
            due = current_as_of >= target
        except TypeError:
            raise ValueError("horizon targets and as_of must use compatible timezones")
        if not due:
            labels[name] = _empty_label(
                name, target, "pending_not_due", "horizon_not_due_as_of"
            )
            continue

        if snapshot.get("forward_label_eligibility") == "pending_reference_evidence":
            labels[name] = _empty_label(
                name,
                target,
                "missing_exit_evidence",
                str(
                    snapshot.get("forward_label_pending_reason")
                    or "missing_reference_price"
                ),
            )
            continue

        if (
            snapshot.get("forward_label_eligibility") != "eligible"
            or entry_price is None
        ):
            labels[name] = _empty_label(
                name,
                target,
                "rejected_data_quality",
                str(
                    snapshot.get("forward_label_rejection_reason")
                    or "invalid_reference_evidence"
                ),
            )
            continue

        if not reference_pit_ok:
            retryable_reference_gap = reference_pit_reason in {
                "point_in_time_lineage_missing_nested_lineage",
                "point_in_time_lineage_missing_top_level_event_time",
                "point_in_time_lineage_missing_receipt_timestamps",
                "point_in_time_lineage_receipt_after_boundary",
            }
            label = _empty_label(
                name,
                target,
                (
                    "missing_exit_evidence"
                    if retryable_reference_gap
                    else "rejected_data_quality"
                ),
                "reference_%s" % reference_pit_reason,
            )
            label["point_in_time_lineage"] = deepcopy(reference_pit_validation)
            labels[name] = label
            continue

        # Reject labels when no versioned cost evidence exists.
        if cost_model_version is None:
            labels[name] = _empty_label(
                name,
                target,
                "rejected_missing_cost_evidence",
                "no_versioned_cost_evidence",
            )
            continue

        verified_window: list[tuple[datetime, Mapping[str, Any], dict[str, Any]]] = []
        gate_failures: list[tuple[str, str, dict[str, Any]]] = []
        latest_eligible = target + MAX_EVIDENCE_LAG[name]
        for point in points:
            if not isinstance(point, Mapping):
                continue
            if not _point_supports_horizon(point, name):
                continue
            gate_status, gate_reason, pit_validation, canonical_event_at = (
                _point_evidence_gate(
                    point,
                    as_of=current_as_of,
                )
            )
            if canonical_event_at is None:
                gate_failures.append((gate_status, gate_reason, pit_validation))
                continue
            try:
                if canonical_event_at < target:
                    # A valid earlier point belongs to an earlier horizon and
                    # is not evidence for this one.
                    continue
                in_window = bool(
                    canonical_event_at <= current_as_of
                    and canonical_event_at <= latest_eligible
                )
            except TypeError:
                in_window = False
            if not in_window:
                gate_failures.append(
                    (
                        "pending",
                        "canonical_event_outside_horizon_window",
                        pit_validation,
                    )
                )
                continue
            if gate_status != "verified":
                gate_failures.append((gate_status, gate_reason, pit_validation))
                continue
            verified_window.append((canonical_event_at, point, pit_validation))
        verified_window.sort(key=lambda item: item[0])

        if not verified_window:
            if gate_failures:
                pending_failure = next(
                    (failure for failure in gate_failures if failure[0] == "pending"),
                    None,
                )
                gate_status, reason, pit_validation = (
                    pending_failure or gate_failures[0]
                )
                status = (
                    "missing_exit_evidence"
                    if gate_status == "pending"
                    else "rejected_data_quality"
                )
                label = _empty_label(name, target, status, reason)
                label["point_in_time_lineage"] = deepcopy(pit_validation)
                labels[name] = label
            else:
                labels[name] = _empty_label(
                    name,
                    target,
                    "missing_exit_evidence",
                    "no_exit_evidence_as_of",
                )
            continue

        point_at, point, point_lineage = verified_window[0]
        exit_price = float(point["price"])
        market_return = (exit_price - entry_price) / entry_price
        gross_return = market_return * multiplier
        applied_fee_bps = fee_bps if multiplier else 0.0
        applied_slippage_bps = slippage_bps if multiplier else 0.0
        total_cost_bps = applied_fee_bps + applied_slippage_bps
        net_return = gross_return - (total_cost_bps / 10_000.0)
        labels[name] = {
            "horizon": name,
            "target_at": _iso(target),
            "status": "ready",
            "reason": "verified_exit_evidence",
            "evidence_at": _iso(point_at),
            "evidence_source": str(point.get("source")),
            "exit_price": exit_price,
            "market_return": market_return,
            "gross_return_after_direction": gross_return,
            "fee_bps": applied_fee_bps,
            "slippage_bps": applied_slippage_bps,
            "total_cost_bps": total_cost_bps,
            "cost_model_version": cost_model_version,
            "cost_evidence_event_id": cost_evidence_id,
            "net_return_after_costs": net_return,
            "outcome": "win"
            if net_return > 0
            else "loss"
            if net_return < 0
            else "flat",
            "point_in_time_lineage": point_lineage,
        }

    result["labels_as_of"] = _iso(current_as_of)
    result["labels"] = labels
    result["label_aliases"] = dict(HORIZON_ALIASES)
    result["real_trading_enabled"] = False
    result["live_execution_enabled"] = False
    return result


__all__ = [
    "CANONICAL_HORIZONS",
    "HORIZON_ALIASES",
    "PRIMARY_HORIZON_POLICY_VERSION",
    "SAMPLE_SCIENCE_CONTRACT_VERSION",
    "DEFAULT_PRIMARY_LABEL_HORIZON",
    "PIT_TIMESTAMP_FIELDS",
    "build_prediction_snapshot",
    "canonicalize_evidence_record",
    "canonical_horizon",
    "evidence_envelope_from_record",
    "materialize_forward_labels",
    "validate_evidence_envelope",
    "validate_point_in_time_lineage",
    "_stable_label_update_id",
]
