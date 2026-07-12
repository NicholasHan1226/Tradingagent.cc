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
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from shared.data.reader import TradingagentDataReader
from shared.review.forward_labels import CANONICAL_HORIZONS
from shared.review.sample_journal import JournalSafetyError, SampleJournal


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
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
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


def _explicit_bar_timestamp(row: Mapping[str, Any]) -> Optional[str]:
    for key in ("bar_time", "timestamp", "datetime", "observed_at"):
        raw = row.get(key)
        if raw in (None, ""):
            continue
        try:
            return _parse_datetime(raw, field=key).isoformat(timespec="seconds")
        except ValueError:
            return None
    return None


def _daily_close_timestamp(row: Mapping[str, Any]) -> Optional[str]:
    explicit = _explicit_bar_timestamp(row)
    if explicit:
        return explicit
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
            timestamp = (
                _explicit_bar_timestamp(row)
                if kind == "intraday_5m"
                else _daily_close_timestamp(row)
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
                    }
                )
                continue
            point = {
                "price": price,
                "timestamp": timestamp,
                "event_time": timestamp,
                "available_at": row.get("available_at") or row.get("published_at"),
                "ingested_at": row.get("ingested_at") or row.get("received_at"),
                "retrieved_as_of": row.get("retrieved_as_of"),
                "source": source,
                "reliable": True,
                "bar_kind": kind,
                "eligible_horizons": (
                    ["m30", "m60", "close"]
                    if kind == "intraday_5m"
                    else ["close", "1d", "3d", "5d"]
                ),
            }
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
    if prediction.tzinfo is None:
        return (
            as_of.astimezone(CN_TZ).replace(tzinfo=None).isoformat(timespec="seconds")
        )
    return as_of.astimezone(prediction.tzinfo).isoformat(timespec="seconds")


def _compatible_price_points(
    snapshot: Mapping[str, Any], price_points: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Match bar timestamp awareness to the immutable prediction timestamp."""

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
        if prediction.tzinfo is None:
            if timestamp.tzinfo is not None:
                timestamp = timestamp.astimezone(CN_TZ).replace(tzinfo=None)
        else:
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=CN_TZ)
            timestamp = timestamp.astimezone(prediction.tzinfo)
        point["timestamp"] = timestamp.isoformat(timespec="seconds")
        compatible.append(point)
    return compatible


def _collect_price_points(
    snapshot: Mapping[str, Any], *, reader: Any, as_of: datetime
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
        try:
            intraday = _call_intraday(reader, symbol, prediction_date)
        except Exception as exc:  # noqa: BLE001 - external reader boundary
            read_errors.append({"dataset": "intraday_5m", "reason": str(exc)})
        try:
            daily = _call_daily(reader, symbol, prediction_date, as_of_date)
        except Exception as exc:  # noqa: BLE001 - external reader boundary
            read_errors.append({"dataset": "daily", "reason": str(exc)})
    converted = price_points_from_bars(intraday_rows=intraday, daily_rows=daily)
    for point in converted["price_points"]:
        if point.get("retrieved_as_of") in (None, ""):
            point["retrieved_as_of"] = as_of.isoformat(timespec="seconds")
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
    """Build 1d/3d/5d from observed A-share trading dates, not calendar days."""

    prediction = _parse_datetime(snapshot.get("prediction_at"), field="prediction_at")
    prediction_date = prediction.strftime("%Y%m%d")
    same_day_close = prediction.replace(hour=15, minute=0, second=0, microsecond=0)
    future_dates = sorted(
        {
            date
            for date in daily_trade_dates
            if len(date) == 8 and date > prediction_date
        }
    )

    def close_for(date_key: str) -> datetime:
        return datetime.strptime(date_key, "%Y%m%d").replace(
            hour=15,
            minute=0,
            second=0,
            microsecond=0,
            tzinfo=CN_TZ,
        )

    def pending_target() -> datetime:
        # We do not guess future exchange holidays. Until the Nth observed
        # trading date exists, keep the horizon pending just beyond as_of.
        return max(as_of + timedelta(seconds=1), prediction + timedelta(seconds=1))

    if same_day_close < prediction:
        close_target = close_for(future_dates[0]) if future_dates else pending_target()
    else:
        close_target = same_day_close
    targets = {
        "m30": prediction + timedelta(minutes=30),
        "m60": prediction + timedelta(minutes=60),
        "close": close_target,
        "1d": close_for(future_dates[0])
        if len(future_dates) >= 1
        else pending_target(),
        "3d": close_for(future_dates[2])
        if len(future_dates) >= 3
        else pending_target(),
        "5d": close_for(future_dates[4])
        if len(future_dates) >= 5
        else pending_target(),
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
        if event.get("round_trip_complete") is not True:
            continue
        if event.get("execution_eligible") is not True:
            continue
        if str(event.get("costs_cover") or "") != "round_trip":
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


def run_ashare_forward_label_ops(
    *,
    journal_path: Path | str,
    trade_date: str,
    as_of: Any,
    reader: Any | None = None,
    environ: Optional[Mapping[str, Any]] = None,
    safety_flags: Optional[Mapping[str, Any]] = None,
    journal: SampleJournal | None = None,
) -> dict[str, Any]:
    """Read prediction snapshots and append idempotent label updates only."""

    active_environ = os.environ if environ is None else environ
    _assert_sim_only(active_environ, safety_flags)
    selected_trade_date = _validated_trade_date(trade_date)
    current_as_of = _parse_datetime(as_of, field="as_of")
    active_journal = journal or SampleJournal(journal_path)

    # Reading the journal also verifies every event fingerprint and recursively
    # rejects live markers before any market read or label append.
    events = active_journal.read_events()
    live_journal_marker = _find_live_marker(events, "journal")
    if live_journal_marker:
        raise ForwardLabelOpsSafetyError(
            "live trading journal marker rejected at %s" % live_journal_marker
        )
    predictions = [
        event
        for event in events
        if event.get("journal_event_type") == "prediction_snapshot"
    ]
    selected = [
        event
        for event in predictions
        if _is_ashare(event) and _prediction_trade_date(event) == selected_trade_date
    ]
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

        evidence = _collect_price_points(
            snapshot, reader=active_reader, as_of=current_as_of
        )
        counts["bar_quality_rejections"] += len(evidence["rejections"])
        counts["market_read_errors"] += len(evidence["read_errors"])

        # Try actual execution costs first (verified fills/round trips).
        # Fall back to the conservative model embedded at prediction time.
        # Observation/counterfactual snapshots have no actual fills, so they
        # naturally degrade to the conservative model.
        actual_costs = _build_actual_execution_costs(events, snapshot, current_as_of)
        if actual_costs is not None:
            explicit_costs = actual_costs
            counts["actual_execution_cost_used"] += 1
        else:
            explicit_costs = snapshot.get("costs")

        materialized = active_journal.materialize_labels(
            snapshot_id,
            _compatible_price_points(snapshot, evidence["price_points"]),
            as_of=_compatible_as_of(snapshot, current_as_of),
            horizon_targets=_ashare_horizon_targets(
                snapshot,
                as_of=current_as_of,
                daily_trade_dates=evidence.get("daily_trade_dates") or [],
            ),
            costs=explicit_costs,
        )
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
        results.append(
            {
                "snapshot_id": snapshot_id,
                "symbol": symbol,
                "status": materialized["status"],
                "price_point_count": evidence["accepted_points"],
                "bar_quality_rejections": deepcopy(evidence["rejections"]),
                "market_read_errors": deepcopy(evidence["read_errors"]),
                "label_status_counts": dict(sorted(statuses.items())),
            }
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
) -> dict[str, Any]:
    """Discover and materialize every pending A-share date in a bounded window."""

    active_environ = os.environ if environ is None else environ
    _assert_sim_only(active_environ, safety_flags)
    current_as_of = _parse_datetime(as_of, field="as_of")
    active_journal = journal or SampleJournal(journal_path)
    events = active_journal.read_events()
    live_journal_marker = _find_live_marker(events, "journal")
    if live_journal_marker:
        raise ForwardLabelOpsSafetyError(
            "live trading journal marker rejected at %s" % live_journal_marker
        )
    backlog = enumerate_ashare_forward_label_backlog(
        events,
        anchor_trade_date=anchor_trade_date,
        as_of=current_as_of,
        window_days=window_days,
    )
    pending_dates = list(backlog["pending_trade_dates"])
    active_reader = reader
    if pending_dates and active_reader is None:
        active_reader = TradingagentDataReader()

    aggregate_keys = (
        "prediction_count",
        "new_label_updates",
        "idempotent_label_updates",
        "ready_labels",
        "pending_not_due",
        "data_quality_rejected",
        "missing_evidence",
        "cost_evidence_rejected",
        "actual_execution_cost_used",
        "bar_quality_rejections",
        "market_read_errors",
        "future_predictions",
    )
    counts: Counter[str] = Counter({key: 0 for key in aggregate_keys})
    date_reports: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for pending_date in pending_dates:
        report = run_ashare_forward_label_ops(
            journal_path=journal_path,
            trade_date=pending_date,
            as_of=current_as_of,
            reader=active_reader,
            environ=active_environ,
            safety_flags=safety_flags,
            journal=active_journal,
        )
        for key in aggregate_keys:
            counts[key] += int(report["counts"].get(key) or 0)
        date_reports.append(report)
        for result in report["results"]:
            results.append({"trade_date": pending_date, **deepcopy(result)})
    counts["filtered_predictions"] = max(
        0, int(backlog["ashare_prediction_count"]) - counts["prediction_count"]
    )
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
        "date_reports": date_reports,
        "results": results,
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
    parser.add_argument(
        "--backlog-window-days",
        type=int,
        default=DEFAULT_BACKLOG_WINDOW_DAYS,
        help="Bounded calendar-day lookback for unresolved prediction dates (1-31).",
    )
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run_ashare_forward_label_backlog(
            journal_path=args.journal_path,
            anchor_trade_date=args.trade_date,
            as_of=args.as_of,
            window_days=args.backlog_window_days,
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
    "main",
    "price_points_from_bars",
    "run_ashare_forward_label_backlog",
    "run_ashare_forward_label_ops",
]
