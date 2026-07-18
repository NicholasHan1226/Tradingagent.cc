#!/usr/bin/env python3
"""Materialize sim-only A-share prediction labels from SharedSignals bars.

This operation reads immutable prediction snapshots from ``SampleJournal`` and
appends only forward-label updates to that same journal.  It has no broker,
order, account, position, or capital initialization path.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
from time import perf_counter
from typing import Any, Mapping, Optional, Sequence

from shared.data.reader import TradingagentDataReader
from shared.models.lifecycle import (
    TradingSessionCalendarAuthority,
    TradingSessionCalendarAuthorityVerification,
    ValidationPlan,
)
from shared.review.forward_labels import (
    CANONICAL_HORIZONS,
    evidence_envelope_from_record,
    validate_evidence_envelope,
)
from shared.review.sample_journal import (
    FrozenJournalView,
    JournalSafetyError,
    SampleJournal,
    build_strict_execution_evidence_index,
    validate_strict_completed_round_trip_evidence,
)


CN_TZ = timezone(timedelta(hours=8))

_LIVE_BOOLEAN_FIELDS = {
    "real_trading_enabled",
    "live_trading_enabled",
    "live_execution_enabled",
    "real_money_enabled",
    "live_broker_enabled",
    "direct_execution_enabled",
    "real_order_enabled",
    "production_execution_enabled",
    "is_live",
}
_LIVE_MODE_FIELDS = {"account_type", "capital_layer", "execution_mode", "trading_mode"}
_LIVE_MODE_VALUES = {"live", "real", "production", "real_money"}
_TRUE_VALUES = {
    "1",
    "true",
    "yes",
    "y",
    "on",
    "enabled",
    "enable",
    "live",
    "real",
    "production",
}

DEFAULT_BACKLOG_WINDOW_DAYS = 31
MAX_BACKLOG_WINDOW_DAYS = 31
_TERMINAL_LABEL_STATUSES = {
    "ready",
    "rejected_data_quality",
    "rejected_missing_cost_evidence",
}
_KNOWN_LABEL_STATUSES = _TERMINAL_LABEL_STATUSES | {
    "pending_not_due",
    "missing_exit_evidence",
    "rejected_missing_cost_evidence",
}


class ForwardLabelOpsError(RuntimeError):
    """Base error for the A-share forward-label operation."""


class ForwardLabelOpsSafetyError(ForwardLabelOpsError):
    """Raised before any read/write when live execution is indicated."""


def _require_ashare_validation_plan(
    value: Optional[ValidationPlan],
) -> ValidationPlan:
    if not isinstance(value, ValidationPlan):
        raise ForwardLabelOpsSafetyError("verified_frozen_validation_plan_required")
    calendar = value.trading_session_calendar
    proof = value.trading_session_calendar_verification
    if (
        value.market.strip().lower()
        not in {"ashare", "a_share", "a-share", "a股", "cn", "china"}
        or calendar is None
        or proof is None
        or proof.accepted is not True
        or proof.calendar_sha256 != calendar.calendar_sha256
        or proof.source_receipt_id != calendar.source_receipt_id
        or proof.source_receipt_sha256 != calendar.source_receipt_sha256
        or proof.frozen_at != value.frozen_at
    ):
        raise ForwardLabelOpsSafetyError(
            "verified_frozen_ashare_validation_plan_required"
        )
    return value


def _artifact_datetime(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(
                str(value or "").strip().replace("Z", "+00:00")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("%s must be an ISO timestamp" % field) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("%s must include a timezone" % field)
    return parsed


def _artifact_date_pair(value: Any, *, field: str) -> tuple[date, date]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("validation_plan.%s must be a two-date sequence" % field)
    if len(value) != 2:
        raise ValueError("validation_plan.%s must contain exactly two dates" % field)
    try:
        return date.fromisoformat(str(value[0])), date.fromisoformat(str(value[1]))
    except (TypeError, ValueError) as exc:
        raise ValueError("validation_plan.%s contains an invalid date" % field) from exc


def load_validation_plan_artifact(path: Path | str | None) -> ValidationPlan:
    """Load one immutable, detached-proof plan artifact without minting proof."""

    if path is None:
        raise ForwardLabelOpsSafetyError(
            "verified_frozen_validation_plan_artifact_required"
        )
    artifact_path = Path(path)
    if artifact_path.is_symlink():
        raise ForwardLabelOpsSafetyError("validation_plan_artifact_symlink_rejected")
    try:
        raw = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ForwardLabelOpsSafetyError("validation_plan_artifact_unreadable") from exc
    if not isinstance(raw, Mapping):
        raise ForwardLabelOpsSafetyError("validation_plan_artifact_must_be_mapping")
    if raw.get("artifact_type") != "ashare_validation_plan_v1":
        raise ForwardLabelOpsSafetyError("validation_plan_artifact_type_invalid")
    payload = raw.get("validation_plan")
    if not isinstance(payload, Mapping):
        raise ForwardLabelOpsSafetyError("validation_plan_payload_missing")
    calendar_payload = payload.get("trading_session_calendar")
    proof_payload = payload.get("trading_session_calendar_verification")
    if not isinstance(calendar_payload, Mapping) or not isinstance(
        proof_payload, Mapping
    ):
        raise ForwardLabelOpsSafetyError(
            "validation_plan_calendar_authority_proof_missing"
        )
    try:
        calendar = TradingSessionCalendarAuthority(
            market=str(calendar_payload["market"]),
            calendar_id=str(calendar_payload["calendar_id"]),
            calendar_version=str(calendar_payload["calendar_version"]),
            source_dataset_id=str(calendar_payload["source_dataset_id"]),
            source_receipt_id=str(calendar_payload["source_receipt_id"]),
            source_receipt_sha256=str(calendar_payload["source_receipt_sha256"]),
            available_at=_artifact_datetime(
                calendar_payload["available_at"], field="calendar.available_at"
            ),
            sessions=tuple(
                date.fromisoformat(str(value)) for value in calendar_payload["sessions"]
            ),
        )
        if calendar_payload.get("calendar_sha256") != calendar.calendar_sha256:
            raise ValueError("calendar_sha256_mismatch")
        if calendar_payload.get("session_count") != calendar.session_count:
            raise ValueError("calendar_session_count_mismatch")
        proof = TradingSessionCalendarAuthorityVerification(
            accepted=proof_payload["accepted"],
            verifier_id=str(proof_payload["verifier_id"]),
            verifier_version=str(proof_payload["verifier_version"]),
            proof_sha256=str(proof_payload["proof_sha256"]),
            verified_at=_artifact_datetime(
                proof_payload["verified_at"], field="calendar_proof.verified_at"
            ),
            frozen_at=_artifact_datetime(
                proof_payload["frozen_at"], field="calendar_proof.frozen_at"
            ),
            calendar_sha256=str(proof_payload["calendar_sha256"]),
            source_receipt_id=str(proof_payload["source_receipt_id"]),
            source_receipt_sha256=str(proof_payload["source_receipt_sha256"]),
        )
        train_start, train_end = _artifact_date_pair(payload["train"], field="train")
        validation_start, validation_end = _artifact_date_pair(
            payload["validation"], field="validation"
        )
        test_start, test_end = _artifact_date_pair(payload["test"], field="test")
        plan = ValidationPlan(
            train_start=train_start,
            train_end=train_end,
            validation_start=validation_start,
            validation_end=validation_end,
            test_start=test_start,
            test_end=test_end,
            purge_days=payload["purge_days"],
            embargo_days=payload["embargo_days"],
            label_horizon_days=payload["label_horizon_days"],
            max_feature_lookback_days=payload["max_feature_lookback_days"],
            event_cluster_embargo_days=payload["event_cluster_embargo_days"],
            decision_cluster_key=str(payload["decision_cluster_key"]),
            decision_cluster_deduplicated=payload["decision_cluster_deduplicated"],
            registered_trial_count=payload["registered_trial_count"],
            multiple_testing_trial_budget=payload["multiple_testing_trial_budget"],
            pbo_required=payload["pbo_required"],
            deflated_sharpe_required=payload["deflated_sharpe_required"],
            oos_reuse_count=payload["oos_reuse_count"],
            max_oos_reuse_count=payload["max_oos_reuse_count"],
            oos_used_for_tuning=payload["oos_used_for_tuning"],
            oos_authority_receipt_sha256=str(payload["oos_authority_receipt_sha256"]),
            experiment_family_id=str(payload["experiment_family_id"]),
            experiment_id=str(payload["experiment_id"]),
            frozen_test_set_id=str(payload["frozen_test_set_id"]),
            frozen_at=_artifact_datetime(payload["frozen_at"], field="frozen_at"),
            market=str(payload["market"]),
            trading_session_calendar=calendar,
            trading_session_calendar_verification=proof,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ForwardLabelOpsSafetyError(
            "validation_plan_artifact_contract_invalid:%s" % exc
        ) from exc
    expected_sha = str(raw.get("validation_plan_sha256") or "").strip().lower()
    if expected_sha != plan.sha256():
        raise ForwardLabelOpsSafetyError("validation_plan_artifact_sha256_mismatch")
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if sha256(canonical).hexdigest() != plan.sha256():
        raise ForwardLabelOpsSafetyError(
            "validation_plan_artifact_noncanonical_payload"
        )
    return plan


def load_validation_plan_artifact_with_provenance(
    path: Path | str | None,
) -> tuple[ValidationPlan, dict[str, Any]]:
    """Load the plan plus immutable top-level authority metadata.

    Metadata is preserved for downstream no-default verification; this loader
    does not itself elevate an artifact to trusted or production authority.
    """

    plan = load_validation_plan_artifact(path)
    artifact_path = Path(path)  # type: ignore[arg-type]
    try:
        raw = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ForwardLabelOpsSafetyError("validation_plan_artifact_unreadable") from exc
    if not isinstance(raw, Mapping):
        raise ForwardLabelOpsSafetyError("validation_plan_artifact_must_be_mapping")
    authority_tier = str(raw.get("authority_tier") or "").strip()
    production_eligible = raw.get("production_eligible")
    receipt_sha = str(raw.get("verification_receipt_sha256") or "").strip().lower()
    if (
        not authority_tier
        or not isinstance(production_eligible, bool)
        or len(receipt_sha) != 64
        or any(character not in "0123456789abcdef" for character in receipt_sha)
        or str(raw.get("validation_plan_sha256") or "").strip().lower() != plan.sha256()
    ):
        raise ForwardLabelOpsSafetyError("validation_plan_artifact_provenance_invalid")
    canonical_artifact = json.dumps(
        raw,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return plan, {
        "validation_plan_sha256": plan.sha256(),
        "artifact_sha256": sha256(canonical_artifact).hexdigest(),
        "authority_tier": authority_tier,
        "production_eligible": production_eligible,
        "verification_receipt_sha256": receipt_sha,
    }


def _is_truthy(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    return str(value or "").strip().lower() in _TRUE_VALUES


def _find_live_marker(value: Any, path: str = "flags") -> Optional[str]:
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key or "").strip().lower()
            child = "%s.%s" % (path, raw_key)
            live_named_flag = (
                key in _LIVE_BOOLEAN_FIELDS
                or key.startswith("live_")
                or key.endswith("_live")
                or key.startswith("real_trading_")
                or key.startswith("real_money_")
            )
            if live_named_flag and _is_truthy(nested):
                return child
            if (
                key in _LIVE_MODE_FIELDS
                and str(nested or "").strip().lower() in _LIVE_MODE_VALUES
            ):
                return child
            found = _find_live_marker(nested, child)
            if found:
                return found
    elif isinstance(value, (list, tuple, set)):
        for index, nested in enumerate(value):
            found = _find_live_marker(nested, "%s[%d]" % (path, index))
            if found:
                return found
    return None


def _assert_sim_only(
    environ: Mapping[str, Any], safety_flags: Optional[Mapping[str, Any]] = None
) -> None:
    normalized_env = {str(key).strip().lower(): value for key, value in environ.items()}
    marker = _find_live_marker(normalized_env, "environment")
    if marker:
        raise ForwardLabelOpsSafetyError(
            "live trading environment rejected at %s" % marker
        )
    if safety_flags is not None:
        marker = _find_live_marker(safety_flags, "safety_flags")
        if marker:
            raise ForwardLabelOpsSafetyError(
                "live trading flag rejected at %s" % marker
            )


def _parse_datetime(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("%s is required" % field)
        try:
            parsed = datetime.fromisoformat(
                raw.replace(" ", "T", 1).replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            raise ValueError("%s must be an ISO timestamp" % field)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("%s must include a timezone" % field)
    return parsed.astimezone(CN_TZ)


def _compact_date(value: Any) -> str:
    raw = str(value or "").strip()
    digits = "".join(character for character in raw[:10] if character.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def _validated_trade_date(value: Any) -> str:
    compact = _compact_date(value)
    if len(compact) != 8:
        raise ValueError("trade_date must be YYYYMMDD or YYYY-MM-DD")
    try:
        datetime.strptime(compact, "%Y%m%d")
    except ValueError:
        raise ValueError("trade_date is not a valid calendar date")
    return compact


def _positive_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0 or parsed != parsed:
        return None
    return parsed


def _bar_price(row: Mapping[str, Any]) -> Optional[float]:
    for key in ("close", "price", "last_price", "latest_price"):
        value = _positive_float(row.get(key))
        if value is not None:
            return value
    return None


def _bar_source(row: Mapping[str, Any]) -> str:
    for key in ("source", "provider", "data_source", "source_name", "vendor"):
        source = str(row.get(key) or "").strip()
        if source:
            return source
    return ""


def _explicit_bar_timestamp(
    row: Mapping[str, Any], *, with_lineage: bool = False
) -> Optional[str] | tuple[Optional[str], dict[str, Any]]:
    full_envelope = evidence_envelope_from_record(row)
    event_fields = dict(full_envelope.get("event_time_fields") or {})
    event_envelope = {
        "event_time_fields": event_fields,
        "availability_time_fields": {},
        "ingestion_time_fields": {},
        "retrieval_time_fields": {},
        "structure_errors": list(full_envelope.get("structure_errors") or []),
    }
    validation = validate_evidence_envelope(event_envelope, require_receipts=False)
    preferred_field = next(
        (
            key
            for key in (
                "bar_time",
                "trade_time",
                "timestamp",
                "datetime",
                "observed_at",
                "event_time",
                "source_event_time",
            )
            if row.get(key) not in (None, "")
        ),
        None,
    )
    result = (
        (validation.get("canonical_timestamps") or {}).get("event_time")
        if validation.get("status") == "valid"
        else None
    )
    if preferred_field is not None:
        preferred_validation = (validation.get("fields") or {}).get(preferred_field, {})
        lineage = {
            "source_field": preferred_field,
            "raw_value": str(row.get(preferred_field)),
            "normalized_value": result,
            "timezone_semantics": "ashare_exchange_event_time",
            "normalization_rule": preferred_validation.get(
                "normalization_rule", "none"
            ),
            "valid": validation.get("status") == "valid",
            "reason": (
                None
                if validation.get("status") == "valid"
                else validation.get("status")
            ),
            "source_event_time_fields": event_fields,
            "evidence_envelope_validation": validation,
        }
        return (result, lineage) if with_lineage else result
    lineage = {
        "source_field": None,
        "raw_value": None,
        "normalized_value": None,
        "normalization_rule": "none",
        "valid": False,
        "reason": "missing_timestamp",
    }
    return (None, lineage) if with_lineage else None


def _daily_close_timestamp(row: Mapping[str, Any]) -> Optional[str]:
    event_fields = evidence_envelope_from_record(row).get("event_time_fields") or {}
    if event_fields:
        return _explicit_bar_timestamp(row)
    trade_date = _compact_date(
        row.get("trade_date") or row.get("date") or row.get("bar_date")
    )
    if len(trade_date) != 8:
        return None
    try:
        parsed = datetime.strptime(trade_date, "%Y%m%d").replace(
            hour=15, minute=0, second=0, tzinfo=CN_TZ
        )
    except ValueError:
        return None
    # A daily bar's trade_date has formal close semantics.  The price remains
    # the source-provided close; this timestamp does not invent a market price.
    return parsed.isoformat(timespec="seconds")


def _row_quality_rejection(row: Mapping[str, Any]) -> Optional[str]:
    if row.get("reliable") is False:
        return "bar_marked_unreliable"
    if _is_truthy(row.get("stale")):
        return "stale_bar"
    quality = (
        str(row.get("data_quality") or row.get("quality_status") or "").strip().lower()
    )
    if quality in {"bad", "invalid", "rejected", "unreliable", "stale"}:
        return "bar_quality_%s" % quality
    return None


def price_points_from_bars(
    *,
    intraday_rows: Sequence[Mapping[str, Any]],
    daily_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Purely convert sourced bars to verified forward-label price points."""

    points: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []

    def add_rows(rows: Sequence[Mapping[str, Any]], kind: str) -> None:
        for index, raw_row in enumerate(rows):
            if not isinstance(raw_row, Mapping):
                rejections.append(
                    {"bar_kind": kind, "row_index": index, "reason": "invalid_row"}
                )
                continue
            row = dict(raw_row)
            reason = _row_quality_rejection(row)
            price = _bar_price(row)
            source = _bar_source(row)
            evidence_envelope = evidence_envelope_from_record(row)
            timestamp_lineage: dict[str, Any]
            if kind == "intraday_5m":
                timestamp, timestamp_lineage = _explicit_bar_timestamp(
                    row, with_lineage=True
                )
            else:
                timestamp = _daily_close_timestamp(row)
                if not evidence_envelope.get("event_time_fields") and timestamp:
                    evidence_envelope["event_time_fields"] = {
                        "derived.trade_date_close": timestamp
                    }
                timestamp_lineage = {
                    "source_field": "trade_date",
                    "raw_value": row.get("trade_date")
                    or row.get("date")
                    or row.get("bar_date"),
                    "normalized_value": timestamp,
                    "timezone_semantics": "ashare_daily_close",
                    "normalization_rule": "ashare_trade_date_to_15_00_asia_shanghai",
                    "valid": timestamp is not None,
                    "reason": None if timestamp is not None else "missing_timestamp",
                }
            envelope_validation = validate_evidence_envelope(
                evidence_envelope, require_receipts=False
            )
            if envelope_validation.get("status") == "valid":
                timestamp = (envelope_validation.get("canonical_timestamps") or {}).get(
                    "event_time"
                )
            elif reason is None:
                reason = "evidence_envelope_%s" % str(
                    envelope_validation.get("status") or "invalid"
                )
            if reason is None and price is None:
                reason = "invalid_price"
            if reason is None and not source:
                reason = "missing_source"
            if reason is None and not timestamp:
                reason = "missing_timestamp"
            if reason is not None:
                rejections.append(
                    {
                        "bar_kind": kind,
                        "row_index": index,
                        "reason": reason,
                        "trade_date": _compact_date(
                            row.get("trade_date")
                            or row.get("date")
                            or row.get("bar_date")
                        ),
                        "evidence_envelope": evidence_envelope,
                        "evidence_envelope_validation": envelope_validation,
                    }
                )
                continue
            canonical_timestamps = envelope_validation.get("canonical_timestamps") or {}
            point = {
                "price": price,
                "timestamp": timestamp,
                "event_time": timestamp,
                "available_at": canonical_timestamps.get("available_at"),
                "ingested_at": canonical_timestamps.get("ingested_at"),
                "retrieved_as_of": canonical_timestamps.get("retrieved_as_of"),
                "source": source,
                "reliable": True,
                "bar_kind": kind,
                "timestamp_lineage": timestamp_lineage,
                "evidence_envelope": evidence_envelope,
                "evidence_envelope_validation": envelope_validation,
                "eligible_horizons": (
                    ["m30", "m60", "close"]
                    if kind == "intraday_5m"
                    else ["close", "1d", "3d", "5d"]
                ),
            }
            nested_lineage = row.get("point_in_time_lineage")
            if not isinstance(nested_lineage, Mapping):
                nested_lineage = row.get("pit_lineage")
            if isinstance(nested_lineage, Mapping):
                point["point_in_time_lineage"] = deepcopy(dict(nested_lineage))
            symbol = str(row.get("ts_code") or row.get("symbol") or "").strip().upper()
            if symbol:
                point["symbol"] = symbol
            trade_date = _compact_date(
                row.get("trade_date")
                or row.get("date")
                or row.get("bar_date")
                or timestamp
            )
            if trade_date:
                point["trade_date"] = trade_date
            if kind == "daily_close" and not _explicit_bar_timestamp(row):
                point["timestamp_semantics"] = "ashare_daily_close"
            points.append(point)

    add_rows(intraday_rows, "intraday_5m")
    add_rows(daily_rows, "daily_close")
    points.sort(
        key=lambda item: (
            str(item["timestamp"]),
            str(item["source"]),
            float(item["price"]),
        )
    )

    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, float]] = set()
    for point in points:
        identity = (
            str(point["timestamp"]),
            str(point["source"]),
            float(point["price"]),
        )
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(point)
    return {
        "price_points": unique,
        "rejections": rejections,
        "rows_seen": len(intraday_rows) + len(daily_rows),
        "accepted_points": len(unique),
    }


def _call_intraday(reader: Any, symbol: str, trade_date: str) -> list[dict[str, Any]]:
    method = getattr(reader, "get_bars_intraday", None)
    if not callable(method):
        return []
    try:
        rows = method("Ashare", symbol, "5m", trade_date, trade_date)
    except TypeError:
        rows = method(
            market="Ashare",
            symbol=symbol,
            interval="5m",
            start=trade_date,
            end=trade_date,
        )
    return [dict(row) for row in rows or [] if isinstance(row, Mapping)]


def _call_daily(reader: Any, symbol: str, start: str, end: str) -> list[dict[str, Any]]:
    method = getattr(reader, "get_bars_daily", None)
    if not callable(method):
        return []
    try:
        rows = method("Ashare", symbol, start, end)
    except TypeError:
        rows = method(market="Ashare", symbol=symbol, start=start, end=end)
    return [dict(row) for row in rows or [] if isinstance(row, Mapping)]


def _prediction_trade_date(snapshot: Mapping[str, Any]) -> str:
    explicit = _compact_date(snapshot.get("trade_date"))
    if explicit:
        return explicit
    try:
        return _parse_datetime(
            snapshot.get("prediction_at"), field="prediction_at"
        ).strftime("%Y%m%d")
    except ValueError:
        return ""


def _is_ashare(snapshot: Mapping[str, Any]) -> bool:
    market = str(snapshot.get("market") or "").strip().lower()
    return market in {"ashare", "a_share", "a-share", "a股", "cn", "china"}


def _compatible_as_of(snapshot: Mapping[str, Any], as_of: datetime) -> str:
    raw = str(snapshot.get("prediction_at") or "").strip()
    try:
        prediction = datetime.fromisoformat(
            raw.replace(" ", "T", 1).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return as_of.isoformat(timespec="seconds")
    if prediction.tzinfo is None or prediction.utcoffset() is None:
        raise ValueError("prediction_at must include a timezone")
    return as_of.astimezone(prediction.tzinfo).isoformat(timespec="seconds")


def _compatible_price_points(
    snapshot: Mapping[str, Any], price_points: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Convert aware bar instants to the immutable prediction timezone."""

    raw_prediction = str(snapshot.get("prediction_at") or "").strip()
    try:
        prediction = datetime.fromisoformat(
            raw_prediction.replace(" ", "T", 1).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return [dict(point) for point in price_points]
    compatible: list[dict[str, Any]] = []
    for raw_point in price_points:
        point = dict(raw_point)
        raw_timestamp = str(point.get("timestamp") or "").strip()
        try:
            timestamp = datetime.fromisoformat(
                raw_timestamp.replace(" ", "T", 1).replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            compatible.append(point)
            continue
        if (
            prediction.tzinfo is None
            or prediction.utcoffset() is None
            or timestamp.tzinfo is None
            or timestamp.utcoffset() is None
        ):
            continue
        timestamp = timestamp.astimezone(prediction.tzinfo)
        point["timestamp"] = timestamp.isoformat(timespec="seconds")
        compatible.append(point)
    return compatible


def _collect_price_points(
    snapshot: Mapping[str, Any],
    *,
    reader: Any,
    as_of: datetime,
    request_metrics: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    symbol = (
        str(snapshot.get("symbol") or snapshot.get("ts_code") or "").strip().upper()
    )
    prediction_date = _prediction_trade_date(snapshot)
    as_of_date = as_of.strftime("%Y%m%d")
    read_errors: list[dict[str, str]] = []
    intraday: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    if not symbol or not prediction_date:
        read_errors.append(
            {"dataset": "all", "reason": "missing_symbol_or_prediction_date"}
        )
    else:
        started = perf_counter()
        if request_metrics is not None:
            request_metrics["physical_request_count"] += 1
        try:
            intraday = _call_intraday(reader, symbol, prediction_date)
        except Exception as exc:  # noqa: BLE001 - external reader boundary
            read_errors.append({"dataset": "intraday_5m", "reason": str(exc)})
            if request_metrics is not None:
                request_metrics["error_count"] += 1
                if (
                    isinstance(exc, (TimeoutError, ConnectionError))
                    or "timeout" in str(exc).lower()
                ):
                    request_metrics["timeout_count"] += 1
        finally:
            if request_metrics is not None:
                elapsed = perf_counter() - started
                request_metrics["latency_seconds"] += elapsed
                request_metrics["latencies_seconds"].append(elapsed)
        started = perf_counter()
        if request_metrics is not None:
            request_metrics["physical_request_count"] += 1
        try:
            daily = _call_daily(reader, symbol, prediction_date, as_of_date)
        except Exception as exc:  # noqa: BLE001 - external reader boundary
            read_errors.append({"dataset": "daily", "reason": str(exc)})
            if request_metrics is not None:
                request_metrics["error_count"] += 1
                if (
                    isinstance(exc, (TimeoutError, ConnectionError))
                    or "timeout" in str(exc).lower()
                ):
                    request_metrics["timeout_count"] += 1
        finally:
            if request_metrics is not None:
                elapsed = perf_counter() - started
                request_metrics["latency_seconds"] += elapsed
                request_metrics["latencies_seconds"].append(elapsed)
    converted = price_points_from_bars(intraday_rows=intraday, daily_rows=daily)
    for point in converted["price_points"]:
        envelope = evidence_envelope_from_record(point)
        envelope_validation = validate_evidence_envelope(
            envelope, boundary=as_of, require_receipts=True
        )
        point["evidence_envelope"] = envelope
        point["evidence_envelope_validation"] = envelope_validation
        nested = point.get("point_in_time_lineage")
        if (
            not isinstance(nested, Mapping)
            and envelope_validation.get("status") == "valid"
            and envelope_validation.get("complete") is True
        ):
            canonical_timestamps = envelope_validation["canonical_timestamps"]
            # Synthetic lineage is permitted only after every original event
            # and receipt alias has passed the shared EvidenceEnvelope gate.
            # No missing provider receipt is replaced with task ``as_of``.
            point["point_in_time_lineage"] = {
                "timestamps": {
                    field: canonical_timestamps.get(field)
                    for field in (
                        "event_time",
                        "available_at",
                        "ingested_at",
                        "retrieved_as_of",
                    )
                },
                "evidence_envelope_validation": deepcopy(envelope_validation),
            }
    converted["daily_trade_dates"] = sorted(
        {
            value
            for row in daily
            if isinstance(row, Mapping)
            for value in [
                _compact_date(
                    row.get("trade_date") or row.get("date") or row.get("bar_date")
                )
            ]
            if value
        }
    )
    converted["read_errors"] = read_errors
    return converted


def _timestamp_compatible_with_snapshot(
    snapshot: Mapping[str, Any], value: datetime
) -> str:
    raw = str(snapshot.get("prediction_at") or "").strip()
    try:
        prediction = datetime.fromisoformat(
            raw.replace(" ", "T", 1).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return value.isoformat(timespec="seconds")
    if prediction.tzinfo is None:
        return (
            value.astimezone(CN_TZ).replace(tzinfo=None).isoformat(timespec="seconds")
        )
    return value.astimezone(prediction.tzinfo).isoformat(timespec="seconds")


def _ashare_horizon_targets(
    snapshot: Mapping[str, Any],
    *,
    as_of: datetime,
    daily_trade_dates: Sequence[str],
) -> dict[str, str]:
    """Build only intraday assertions; the verified plan owns session targets."""

    prediction = _parse_datetime(snapshot.get("prediction_at"), field="prediction_at")
    targets = {
        "m30": prediction + timedelta(minutes=30),
        "m60": prediction + timedelta(minutes=60),
    }
    return {
        name: _timestamp_compatible_with_snapshot(snapshot, target)
        for name, target in targets.items()
    }


def _validated_backlog_window_days(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("window_days must be an integer")
    if not 1 <= value <= MAX_BACKLOG_WINDOW_DAYS:
        raise ValueError(
            "window_days must be between 1 and %d" % MAX_BACKLOG_WINDOW_DAYS
        )
    return value


def enumerate_ashare_forward_label_backlog(
    events: Sequence[Mapping[str, Any]],
    *,
    anchor_trade_date: str,
    as_of: Any,
    window_days: int = DEFAULT_BACKLOG_WINDOW_DAYS,
) -> dict[str, Any]:
    """Return bounded prediction dates whose six-label set is not terminal.

    The enumerator uses the latest label update at or before ``as_of``.  It
    validates prediction/update relationships before returning any date, so a
    malformed journal cannot be silently treated as an empty backlog.
    """

    if isinstance(events, (str, bytes, bytearray)) or not isinstance(events, Sequence):
        raise ValueError("journal events must be a sequence")
    selected_window_days = _validated_backlog_window_days(window_days)
    anchor = _validated_trade_date(anchor_trade_date)
    current_as_of = _parse_datetime(as_of, field="as_of")
    anchor_date = datetime.strptime(anchor, "%Y%m%d").date()
    if anchor_date > current_as_of.date():
        raise ValueError("anchor_trade_date cannot be after as_of")
    window_start = anchor_date - timedelta(days=selected_window_days - 1)
    window_start_key = window_start.strftime("%Y%m%d")

    predictions_by_id: dict[str, dict[str, Any]] = {}
    ashare_predictions: list[dict[str, Any]] = []
    for sequence, raw_event in enumerate(events):
        if not isinstance(raw_event, Mapping):
            raise ValueError("journal_event_not_mapping:%d" % (sequence + 1))
        if raw_event.get("journal_event_type") != "prediction_snapshot":
            continue
        event = dict(raw_event)
        snapshot_id = str(event.get("snapshot_id") or "").strip()
        if not snapshot_id:
            raise ValueError("missing_snapshot_id:%d" % (sequence + 1))
        if snapshot_id in predictions_by_id:
            raise ValueError("duplicate_prediction_snapshot:%s" % snapshot_id)
        predictions_by_id[snapshot_id] = event
        if not _is_ashare(event):
            continue
        try:
            prediction_at = _parse_datetime(
                event.get("prediction_at"), field="prediction_at"
            )
        except ValueError as exc:
            raise ValueError("invalid_prediction_timestamp:%s" % snapshot_id) from exc
        explicit_date = str(event.get("trade_date") or "").strip()
        if explicit_date:
            try:
                prediction_date = _validated_trade_date(explicit_date)
            except ValueError as exc:
                raise ValueError(
                    "invalid_prediction_trade_date:%s" % snapshot_id
                ) from exc
        else:
            prediction_date = prediction_at.strftime("%Y%m%d")
        if prediction_date != prediction_at.strftime("%Y%m%d"):
            raise ValueError("prediction_trade_date_mismatch:%s" % snapshot_id)
        event["_backlog_prediction_date"] = prediction_date
        event["_backlog_prediction_at"] = prediction_at
        ashare_predictions.append(event)

    latest_updates: dict[str, tuple[datetime, int, dict[str, Any]]] = {}
    ignored_future_updates = 0
    for sequence, raw_event in enumerate(events):
        if raw_event.get("journal_event_type") != "forward_label_update":
            continue
        event = dict(raw_event)
        snapshot_id = str(event.get("snapshot_id") or "").strip()
        if not snapshot_id or snapshot_id not in predictions_by_id:
            raise ValueError(
                "orphan_forward_label_update:%s" % (snapshot_id or sequence + 1)
            )
        try:
            labels_as_of = _parse_datetime(
                event.get("labels_as_of"), field="labels_as_of"
            )
        except ValueError as exc:
            raise ValueError("invalid_labels_as_of:%s" % snapshot_id) from exc
        labels = event.get("labels")
        if not isinstance(labels, Mapping):
            raise ValueError("invalid_forward_labels:%s" % snapshot_id)
        unknown_horizons = set(labels).difference(CANONICAL_HORIZONS)
        if unknown_horizons:
            raise ValueError("unknown_forward_label_horizon:%s" % snapshot_id)
        for horizon, raw_label in labels.items():
            if not isinstance(raw_label, Mapping):
                raise ValueError("invalid_forward_label:%s:%s" % (snapshot_id, horizon))
            status = str(raw_label.get("status") or "").strip()
            if status not in _KNOWN_LABEL_STATUSES:
                raise ValueError(
                    "unknown_forward_label_status:%s:%s" % (snapshot_id, horizon)
                )
        prediction = predictions_by_id[snapshot_id]
        try:
            prediction_at = _parse_datetime(
                prediction.get("prediction_at"), field="prediction_at"
            )
        except ValueError as exc:
            raise ValueError("invalid_prediction_timestamp:%s" % snapshot_id) from exc
        if labels_as_of < prediction_at:
            raise ValueError("forward_labels_before_prediction:%s" % snapshot_id)
        if labels_as_of > current_as_of:
            ignored_future_updates += 1
            continue
        current = latest_updates.get(snapshot_id)
        candidate = (labels_as_of, sequence, event)
        if current is None or candidate[:2] >= current[:2]:
            latest_updates[snapshot_id] = candidate

    pending_dates: set[str] = set()
    pending_snapshots: list[dict[str, Any]] = []
    terminal_snapshot_count = 0
    outside_window_prediction_count = 0
    future_prediction_count = 0
    for prediction in ashare_predictions:
        snapshot_id = str(prediction["snapshot_id"])
        prediction_date = str(prediction["_backlog_prediction_date"])
        latest = latest_updates.get(snapshot_id)
        labels = latest[2].get("labels") if latest is not None else {}
        unresolved = [
            horizon
            for horizon in CANONICAL_HORIZONS
            if not isinstance(labels.get(horizon), Mapping)
            or str(labels[horizon].get("status") or "").strip()
            not in _TERMINAL_LABEL_STATUSES
        ]
        if not unresolved:
            terminal_snapshot_count += 1
            continue
        if prediction_date > anchor:
            future_prediction_count += 1
            continue
        if prediction_date < window_start_key:
            outside_window_prediction_count += 1
            continue
        pending_dates.add(prediction_date)
        pending_snapshots.append(
            {
                "snapshot_id": snapshot_id,
                "trade_date": prediction_date,
                "symbol": str(
                    prediction.get("symbol") or prediction.get("ts_code") or ""
                )
                .strip()
                .upper(),
                "unresolved_horizons": unresolved,
            }
        )

    pending_snapshots.sort(
        key=lambda row: (str(row["trade_date"]), str(row["snapshot_id"]))
    )
    return {
        "anchor_trade_date": anchor,
        "as_of": current_as_of.isoformat(timespec="seconds"),
        "window_days": selected_window_days,
        "window_start_trade_date": window_start_key,
        "window_end_trade_date": anchor,
        "pending_trade_dates": sorted(pending_dates),
        "pending_snapshots": pending_snapshots,
        "pending_snapshot_count": len(pending_snapshots),
        "terminal_snapshot_count": terminal_snapshot_count,
        "outside_window_prediction_count": outside_window_prediction_count,
        "future_prediction_count": future_prediction_count,
        "ignored_future_label_update_count": ignored_future_updates,
        "ashare_prediction_count": len(ashare_predictions),
        "backlog_truncated": outside_window_prediction_count > 0,
        "real_trading_enabled": False,
        "live_execution_enabled": False,
    }


def _build_actual_execution_costs(
    events: Sequence[Mapping[str, Any]],
    snapshot: Mapping[str, Any],
    as_of: datetime,
    *,
    evidence_index: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return complete, point-in-time *actual round-trip* costs for a snapshot.

    A one-sided fill is deliberately insufficient: doubling its fee/slippage
    would fabricate exit costs.  Evidence must be a verified completed round
    trip linked to the exact prediction and current capital/execution lineage.
    """

    symbol = str(snapshot.get("symbol") or "").strip().upper()
    snapshot_id = str(snapshot.get("snapshot_id") or "").strip()
    authority_id = str(snapshot.get("capital_authority_id") or "").strip()
    authority_generation = snapshot.get("authority_generation")
    execution_lineage_id = str(snapshot.get("execution_lineage_id") or "").strip()
    if (
        not symbol
        or not snapshot_id
        or not authority_id
        or not isinstance(authority_generation, int)
        or isinstance(authority_generation, bool)
        or not execution_lineage_id
    ):
        return None

    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        if str(event.get("record_type") or "") != "completed_round_trip":
            continue
        strict_validation = validate_strict_completed_round_trip_evidence(
            event,
            boundary=as_of,
            prediction_snapshot_id=snapshot_id,
            authority_scope={
                "capital_authority_id": authority_id,
                "authority_generation": authority_generation,
                "execution_lineage_id": execution_lineage_id,
            },
            evidence_index=evidence_index,
        )
        if strict_validation.get("valid") is not True:
            continue
        if str(event.get("symbol") or "").strip().upper() != symbol:
            continue
        if str(event.get("prediction_snapshot_id") or "") != snapshot_id:
            continue
        if str(event.get("capital_authority_id") or "") != authority_id:
            continue
        if event.get("authority_generation") != authority_generation:
            continue
        if str(event.get("execution_lineage_id") or "") != execution_lineage_id:
            continue

        raw_event_at = str(
            event.get("closed_at")
            or event.get("event_at")
            or event.get("created_at")
            or ""
        ).strip()
        if raw_event_at:
            try:
                event_at = _parse_datetime(raw_event_at, field="cost_evidence_event_at")
            except ValueError:
                continue
        else:
            compact = _compact_date(event.get("trade_date"))
            if not compact:
                continue
            try:
                event_at = datetime.strptime(compact, "%Y%m%d").replace(
                    hour=23, minute=59, second=59, tzinfo=CN_TZ
                )
            except ValueError:
                continue
        try:
            if event_at > as_of:
                continue
        except TypeError:
            continue

        notional = _positive_float(event.get("notional_cny"))
        if notional is None:
            entry_qty = _positive_float(event.get("entry_quantity"))
            entry_price = _positive_float(event.get("entry_price"))
            if entry_qty is None or entry_price is None:
                continue
            notional = entry_qty * entry_price
        try:
            fee_cny = float(event.get("fee_cny"))
            slip_cny = float(event.get("slippage_cny"))
        except (TypeError, ValueError):
            continue
        if not all(
            math.isfinite(value) and value >= 0 for value in (fee_cny, slip_cny)
        ):
            continue
        event_id = str(event.get("journal_event_id") or event.get("event_id") or "")
        if not event_id:
            continue
        candidates.append(
            (
                event_at,
                {
                    "event_id": event_id,
                    "notional_cny": notional,
                    "fee_cny": fee_cny,
                    "slippage_cny": slip_cny,
                },
            )
        )

    if not candidates:
        return None
    _, best_event = max(candidates, key=lambda item: (item[0], item[1]["event_id"]))
    notional = float(best_event["notional_cny"])
    round_trip_fee_bps = round(best_event["fee_cny"] / notional * 10_000, 4)
    round_trip_slippage_bps = round(best_event["slippage_cny"] / notional * 10_000, 4)

    return {
        "round_trip_fee_bps": round_trip_fee_bps,
        "round_trip_slippage_bps": round_trip_slippage_bps,
        "cost_model_version": "actual_execution_costs_v1",
        "cost_basis_notional_cny": round(notional, 4),
        "cost_evidence_event_id": best_event["event_id"],
        "cost_evidence_source": "verified_execution_journal",
        "cost_evidence_type": "completed_round_trip",
        "costs_cover": "round_trip",
    }


def _index_actual_execution_cost_events(
    events: Sequence[Mapping[str, Any]], *, as_of: datetime
) -> dict[str, list[dict[str, Any]]]:
    """Index point-in-time completed round trips once by prediction snapshot."""

    indexed: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        if not isinstance(event, Mapping):
            continue
        if str(event.get("record_type") or "") != "completed_round_trip":
            continue
        snapshot_id = str(event.get("prediction_snapshot_id") or "").strip()
        if not snapshot_id:
            continue
        raw_event_at = str(
            event.get("closed_at")
            or event.get("event_at")
            or event.get("created_at")
            or ""
        ).strip()
        try:
            event_at = _parse_datetime(raw_event_at, field="cost_evidence_event_at")
        except ValueError:
            continue
        if event_at > as_of:
            continue
        indexed.setdefault(snapshot_id, []).append(dict(event))
    return indexed


def run_ashare_forward_label_ops(
    *,
    journal_path: Path | str,
    trade_date: str,
    as_of: Any,
    reader: Any | None = None,
    environ: Optional[Mapping[str, Any]] = None,
    safety_flags: Optional[Mapping[str, Any]] = None,
    journal: SampleJournal | None = None,
    frozen_view: FrozenJournalView | None = None,
    snapshot_ids: Optional[Sequence[str]] = None,
    evidence_cache: Optional[dict[tuple[str, str, str], dict[str, Any]]] = None,
    batch_size: int = 200,
    validation_plan: Optional[ValidationPlan] = None,
) -> dict[str, Any]:
    """Materialize an exact frozen set of prediction snapshots."""

    active_environ = os.environ if environ is None else environ
    _assert_sim_only(active_environ, safety_flags)
    validation_plan = _require_ashare_validation_plan(validation_plan)
    selected_trade_date = _validated_trade_date(trade_date)
    current_as_of = _parse_datetime(as_of, field="as_of")
    active_journal = journal or SampleJournal(journal_path)

    active_view = frozen_view
    if active_view is None and hasattr(active_journal, "read_frozen"):
        active_view = active_journal.read_frozen(as_of=current_as_of)
    events = (
        active_view.copy_events()
        if active_view is not None
        else active_journal.read_events()
    )
    live_journal_marker = _find_live_marker(events, "journal")
    if live_journal_marker:
        raise ForwardLabelOpsSafetyError(
            "live trading journal marker rejected at %s" % live_journal_marker
        )
    if active_view is None:
        raise ValueError("frozen_journal_view_required_for_label_append")
    predictions = [
        event
        for event in events
        if event.get("journal_event_type") == "prediction_snapshot"
    ]
    if snapshot_ids is None:
        selected = []
        for event in predictions:
            if not _is_ashare(event):
                continue
            explicit_trade_date = str(event.get("trade_date") or "").strip()
            if explicit_trade_date:
                try:
                    event_trade_date = _validated_trade_date(explicit_trade_date)
                except ValueError as exc:
                    raise ValueError(
                        "invalid_prediction_trade_date:%s"
                        % str(event.get("snapshot_id") or "")
                    ) from exc
            else:
                event_trade_date = _prediction_trade_date(event)
            if event_trade_date == selected_trade_date:
                selected.append(event)
    else:
        requested_ids = [str(value or "").strip() for value in snapshot_ids]
        if any(not value for value in requested_ids):
            raise ValueError("snapshot_ids cannot contain an empty identity")
        if len(set(requested_ids)) != len(requested_ids):
            raise ValueError("snapshot_ids must be unique")
        by_id = {str(event.get("snapshot_id") or ""): event for event in predictions}
        missing = [
            snapshot_id for snapshot_id in requested_ids if snapshot_id not in by_id
        ]
        if missing:
            raise ValueError("pending_snapshot_missing:%s" % missing[0])
        selected = [by_id[snapshot_id] for snapshot_id in requested_ids]
        for event in selected:
            if not _is_ashare(event):
                raise ValueError(
                    "pending_snapshot_not_ashare:%s" % event.get("snapshot_id")
                )
    filtered_count = len(predictions) - len(selected)
    active_reader = reader
    if selected and active_reader is None:
        active_reader = TradingagentDataReader()

    counts: Counter[str] = Counter(
        {
            "prediction_count": len(selected),
            "filtered_predictions": filtered_count,
            "new_label_updates": 0,
            "idempotent_label_updates": 0,
            "ready_labels": 0,
            "pending_not_due": 0,
            "data_quality_rejected": 0,
            "missing_evidence": 0,
            "cost_evidence_rejected": 0,
            "actual_execution_cost_used": 0,
            "bar_quality_rejections": 0,
            "market_read_errors": 0,
            "future_predictions": 0,
        }
    )
    results: list[dict[str, Any]] = []
    materialization_requests: list[dict[str, Any]] = []
    materialization_context: list[dict[str, Any]] = []
    cache = evidence_cache if evidence_cache is not None else {}
    request_metrics: dict[str, Any] = {
        "logical_request_count": 0,
        "physical_request_count": 0,
        "cache_hit_count": 0,
        "timeout_count": 0,
        "retry_count": 0,
        "error_count": 0,
        "latency_seconds": 0.0,
        "latencies_seconds": [],
    }
    actual_cost_index = _index_actual_execution_cost_events(events, as_of=current_as_of)
    strict_execution_evidence_index = build_strict_execution_evidence_index(events)

    for snapshot in selected:
        snapshot_id = str(snapshot.get("snapshot_id") or "")
        symbol = (
            str(snapshot.get("symbol") or snapshot.get("ts_code") or "").strip().upper()
        )
        try:
            prediction_at = _parse_datetime(
                snapshot.get("prediction_at"), field="prediction_at"
            )
        except ValueError:
            counts["data_quality_rejected"] += 6
            results.append(
                {
                    "snapshot_id": snapshot_id,
                    "symbol": symbol,
                    "status": "rejected_data_quality",
                    "reason": "invalid_prediction_timestamp",
                    "label_status_counts": {"rejected_data_quality": 6},
                }
            )
            continue
        if prediction_at > current_as_of:
            counts["future_predictions"] += 1
            counts["pending_not_due"] += 6
            results.append(
                {
                    "snapshot_id": snapshot_id,
                    "symbol": symbol,
                    "status": "prediction_after_as_of",
                    "label_status_counts": {"pending_not_due": 6},
                }
            )
            continue

        cache_key = (
            symbol,
            _prediction_trade_date(snapshot),
            current_as_of.isoformat(timespec="seconds"),
        )
        request_metrics["logical_request_count"] += 2
        if cache_key in cache:
            evidence = deepcopy(cache[cache_key])
            request_metrics["cache_hit_count"] += 2
        else:
            evidence = _collect_price_points(
                snapshot,
                reader=active_reader,
                as_of=current_as_of,
                request_metrics=request_metrics,
            )
            cache[cache_key] = deepcopy(evidence)
        counts["bar_quality_rejections"] += len(evidence["rejections"])
        counts["market_read_errors"] += len(evidence["read_errors"])

        # Try actual execution costs first (verified fills/round trips).
        # Fall back to the conservative model embedded at prediction time.
        # Observation/counterfactual snapshots have no actual fills, so they
        # naturally degrade to the conservative model.
        actual_costs = _build_actual_execution_costs(
            actual_cost_index.get(snapshot_id, []),
            snapshot,
            current_as_of,
            evidence_index=strict_execution_evidence_index,
        )
        if actual_costs is not None:
            explicit_costs = actual_costs
            counts["actual_execution_cost_used"] += 1
        else:
            explicit_costs = snapshot.get("costs")

        materialization_requests.append(
            {
                "snapshot_id": snapshot_id,
                "price_points": _compatible_price_points(
                    snapshot, evidence["price_points"]
                ),
                "as_of": _compatible_as_of(snapshot, current_as_of),
                "horizon_targets": _ashare_horizon_targets(
                    snapshot,
                    as_of=current_as_of,
                    daily_trade_dates=evidence.get("daily_trade_dates") or [],
                ),
                "costs": explicit_costs,
            }
        )
        materialization_context.append(
            {
                "snapshot_id": snapshot_id,
                "symbol": symbol,
                "pending_reference_evidence": (
                    snapshot.get("forward_label_eligibility")
                    == "pending_reference_evidence"
                ),
                "pending_reference_reason": snapshot.get(
                    "forward_label_pending_reason"
                ),
                "price_point_count": evidence["accepted_points"],
                "bar_quality_rejections": deepcopy(evidence["rejections"]),
                "market_read_errors": deepcopy(evidence["read_errors"]),
            }
        )

    batch_report = active_journal.materialize_label_batch(
        active_view,
        materialization_requests,
        batch_size=batch_size,
        validation_plan=validation_plan,
    )
    if len(materialization_context) != len(batch_report["results"]):
        raise ForwardLabelOpsError("label_batch_result_count_mismatch")
    for context, materialized in zip(materialization_context, batch_report["results"]):
        if materialized["status"] == "appended":
            counts["new_label_updates"] += 1
        else:
            counts["idempotent_label_updates"] += 1
        labels = materialized["record"].get("labels") or {}
        statuses = Counter(
            str(label.get("status") or "unknown")
            for label in labels.values()
            if isinstance(label, Mapping)
        )
        counts["ready_labels"] += statuses["ready"]
        counts["pending_not_due"] += statuses["pending_not_due"]
        counts["data_quality_rejected"] += statuses["rejected_data_quality"]
        counts["missing_evidence"] += statuses["missing_exit_evidence"]
        counts["cost_evidence_rejected"] += statuses["rejected_missing_cost_evidence"]
        missing_evidence_reason = next(
            (
                str(label.get("reason") or "missing_exit_evidence")
                for label in labels.values()
                if isinstance(label, Mapping)
                and str(label.get("status") or "") == "missing_exit_evidence"
            ),
            None,
        )
        retryable_degraded = bool(
            context["market_read_errors"]
            or context["pending_reference_evidence"]
            or statuses["missing_exit_evidence"]
        )
        results.append(
            {
                "snapshot_id": context["snapshot_id"],
                "symbol": context["symbol"],
                "status": materialized["status"],
                "retryable": retryable_degraded,
                "degraded": retryable_degraded,
                "degraded_reason": (
                    context["pending_reference_reason"]
                    or missing_evidence_reason
                    or (
                        "market_data_read_errors"
                        if context["market_read_errors"]
                        else None
                    )
                ),
                "price_point_count": context["price_point_count"],
                "bar_quality_rejections": context["bar_quality_rejections"],
                "market_read_errors": context["market_read_errors"],
                "label_status_counts": dict(sorted(statuses.items())),
            }
        )

    latencies = sorted(
        float(value) for value in request_metrics.pop("latencies_seconds")
    )
    physical_count = int(request_metrics["physical_request_count"])
    request_metrics["latency_seconds"] = round(
        float(request_metrics["latency_seconds"]), 6
    )
    request_metrics["mean_latency_seconds"] = (
        round(sum(latencies) / physical_count, 6) if physical_count else 0.0
    )
    request_metrics["max_latency_seconds"] = (
        round(max(latencies), 6) if latencies else 0.0
    )
    request_metrics["p95_latency_seconds"] = (
        round(
            latencies[min(len(latencies) - 1, math.ceil(len(latencies) * 0.95) - 1)], 6
        )
        if latencies
        else 0.0
    )

    return {
        "status": "pass",
        "operation": "ashare_forward_label_ops",
        "market": "Ashare",
        "trade_date": selected_trade_date,
        "as_of": current_as_of.isoformat(timespec="seconds"),
        "journal_path": str(Path(journal_path).absolute()),
        "counts": dict(counts),
        "results": results,
        "http_metrics": request_metrics,
        "journal_append": {
            key: value
            for key, value in batch_report.items()
            if key not in {"results", "appended_events"}
        },
        "task_owned_delta_events": batch_report["appended_events"],
        "frozen_head": active_view.metadata(),
        "market_data_access": "read_only",
        "journal_write_scope": "forward_label_updates_only",
        "orders_created": 0,
        "accounts_created": 0,
        "real_trading_enabled": False,
        "live_execution_enabled": False,
    }


def run_ashare_forward_label_backlog(
    *,
    journal_path: Path | str,
    anchor_trade_date: str,
    as_of: Any,
    window_days: int = DEFAULT_BACKLOG_WINDOW_DAYS,
    reader: Any | None = None,
    environ: Optional[Mapping[str, Any]] = None,
    safety_flags: Optional[Mapping[str, Any]] = None,
    journal: SampleJournal | None = None,
    frozen_view: FrozenJournalView | None = None,
    authority_scope: Optional[Mapping[str, Any]] = None,
    batch_size: int = 200,
    validation_plan: Optional[ValidationPlan] = None,
) -> dict[str, Any]:
    """Discover and materialize only the exact pending frozen snapshot IDs."""

    active_environ = os.environ if environ is None else environ
    _assert_sim_only(active_environ, safety_flags)
    validation_plan = _require_ashare_validation_plan(validation_plan)
    current_as_of = _parse_datetime(as_of, field="as_of")
    active_journal = journal or SampleJournal(journal_path)
    active_view = frozen_view
    if active_view is None and hasattr(active_journal, "read_frozen"):
        active_view = active_journal.read_frozen(as_of=current_as_of)
    events = (
        active_view.copy_events()
        if active_view is not None
        else active_journal.read_events()
    )
    live_journal_marker = _find_live_marker(events, "journal")
    if live_journal_marker:
        raise ForwardLabelOpsSafetyError(
            "live trading journal marker rejected at %s" % live_journal_marker
        )
    scoped_events = [
        event
        for event in events
        if authority_scope is None
        or (
            event.get("capital_authority_id")
            == authority_scope.get("capital_authority_id")
            and event.get("authority_generation")
            == authority_scope.get("authority_generation")
            and event.get("execution_lineage_id")
            == authority_scope.get("execution_lineage_id")
        )
    ]
    backlog = enumerate_ashare_forward_label_backlog(
        scoped_events,
        anchor_trade_date=anchor_trade_date,
        as_of=current_as_of,
        window_days=window_days,
    )
    pending_dates = list(backlog["pending_trade_dates"])
    active_reader = reader
    if pending_dates and active_reader is None:
        active_reader = TradingagentDataReader()
    if active_view is None:
        raise ValueError("frozen_journal_view_required_for_label_append")

    pending_snapshot_ids = [
        str(row["snapshot_id"]) for row in backlog["pending_snapshots"]
    ]
    label_report = run_ashare_forward_label_ops(
        journal_path=journal_path,
        trade_date=backlog["anchor_trade_date"],
        as_of=current_as_of,
        reader=active_reader,
        environ=active_environ,
        safety_flags=safety_flags,
        journal=active_journal,
        frozen_view=active_view,
        snapshot_ids=pending_snapshot_ids,
        evidence_cache={},
        batch_size=batch_size,
        validation_plan=validation_plan,
    )
    counts: Counter[str] = Counter(label_report["counts"])
    counts["backlog_date_count"] = len(pending_dates)
    counts["terminal_snapshot_count"] = int(backlog["terminal_snapshot_count"])
    counts["outside_window_prediction_count"] = int(
        backlog["outside_window_prediction_count"]
    )

    return {
        "status": "pass",
        # Keep the established operation identity for acceptance consumers;
        # mode and backlog fields expose the expanded behavior explicitly.
        "operation": "ashare_forward_label_ops",
        "mode": "bounded_backlog",
        "market": "Ashare",
        "trade_date": backlog["anchor_trade_date"],
        "as_of": current_as_of.isoformat(timespec="seconds"),
        "journal_path": str(Path(journal_path).absolute()),
        "backlog": backlog,
        "processed_trade_dates": pending_dates,
        "counts": dict(counts),
        "date_reports": [label_report] if pending_snapshot_ids else [],
        "results": deepcopy(label_report["results"]),
        "http_metrics": deepcopy(label_report["http_metrics"]),
        "journal_append": deepcopy(label_report["journal_append"]),
        "task_owned_delta_events": deepcopy(label_report["task_owned_delta_events"]),
        "frozen_head": active_view.metadata(),
        "market_data_access": "read_only",
        "journal_write_scope": "forward_label_updates_only",
        "orders_created": 0,
        "accounts_created": 0,
        "real_trading_enabled": False,
        "live_execution_enabled": False,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Append sim-only A-share forward labels from sourced SharedSignals bars."
    )
    parser.add_argument("--journal-path", type=Path, required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--validation-plan-path", type=Path)
    parser.add_argument(
        "--backlog-window-days",
        type=int,
        default=DEFAULT_BACKLOG_WINDOW_DAYS,
        help="Bounded calendar-day lookback for unresolved prediction dates (1-31).",
    )
    parser.add_argument(
        "--label-batch-size",
        type=int,
        default=200,
        help="Append batch size for task-owned label deltas (100-250).",
    )
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    try:
        validation_plan = load_validation_plan_artifact(args.validation_plan_path)
        report = run_ashare_forward_label_backlog(
            journal_path=args.journal_path,
            anchor_trade_date=args.trade_date,
            as_of=args.as_of,
            window_days=args.backlog_window_days,
            batch_size=args.label_batch_size,
            validation_plan=validation_plan,
        )
        exit_code = 0
    except (ForwardLabelOpsSafetyError, JournalSafetyError, ValueError) as exc:
        report = {
            "status": "blocked",
            "operation": "ashare_forward_label_ops",
            "reason": str(exc),
            "orders_created": 0,
            "accounts_created": 0,
            "real_trading_enabled": False,
            "live_execution_enabled": False,
        }
        exit_code = 2
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            sort_keys=True,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_BACKLOG_WINDOW_DAYS",
    "ForwardLabelOpsError",
    "ForwardLabelOpsSafetyError",
    "MAX_BACKLOG_WINDOW_DAYS",
    "enumerate_ashare_forward_label_backlog",
    "load_validation_plan_artifact",
    "load_validation_plan_artifact_with_provenance",
    "main",
    "price_points_from_bars",
    "run_ashare_forward_label_backlog",
    "run_ashare_forward_label_ops",
]
