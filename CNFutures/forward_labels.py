"""Append-only CNFutures forward-label materialization."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from shared.review.forward_labels import materialize_forward_labels

from .margin_model import estimate_order_cost
from .review import load_review_rows


CN_TZ = timezone(timedelta(hours=8))
UPDATE_SCHEMA_VERSION = "cn_futures.forward_label_update.v1"
CONSERVATIVE_COST_MODEL_VERSION = "cn-futures-conservative-round-trip.v1"
ACTUAL_HYBRID_COST_MODEL_VERSION = "cn-futures-actual-entry-plus-conservative-exit.v1"
CURRENT_AUTHORITY_ID = "cn-futures-capital-v1"
CURRENT_AUTHORITY_GENERATION = 1
_SHA = frozenset("0123456789abcdef")


def _sha256(value: Mapping[str, Any], *, excluded: str = "") -> str:
    payload = {
        key: nested for key, nested in value.items() if not excluded or key != excluded
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _is_sha(value: Any) -> bool:
    raw = str(value or "").lower()
    return len(raw) == 64 and all(character in _SHA for character in raw)


def _aware(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _positive(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _authority_matches(
    row: Mapping[str, Any], authority_scope: Mapping[str, Any]
) -> bool:
    source_sha = str(row.get("source_snapshot_sha256") or "").lower()
    return bool(
        row.get("lineage_status") == "complete"
        and row.get("authority") == "market_capital_ledger"
        and row.get("capital_authority_id") == CURRENT_AUTHORITY_ID
        and row.get("authority_generation") == CURRENT_AUTHORITY_GENERATION
        and row.get("execution_lineage_id")
        == authority_scope.get("execution_lineage_id")
        and _is_sha(source_sha)
        and row.get("source_snapshot_id") == f"CNF-SNAP-{source_sha[:16]}"
        and _aware(row.get("point_in_time_as_of")) is not None
        and _aware(row.get("source_event_time")) is not None
    )


def _direction(row: Mapping[str, Any]) -> str:
    decision = row.get("decision") if isinstance(row.get("decision"), Mapping) else {}
    return str(
        row.get("direction")
        or row.get("side")
        or decision.get("direction")
        or decision.get("side")
        or decision.get("action")
        or ""
    ).lower()


def _entry_price(row: Mapping[str, Any]) -> float | None:
    decision = row.get("decision") if isinstance(row.get("decision"), Mapping) else {}
    return _positive(row.get("entry_price") or decision.get("entry_price"))


def _costs(row: Mapping[str, Any], entry_price: float) -> dict[str, Any]:
    symbol = str(row.get("symbol") or "")
    side = _direction(row)
    estimate = estimate_order_cost(
        symbol=symbol,
        side=side,
        quantity=1,
        price=entry_price,
    )
    notional = max(float(estimate.notional), 0.01)
    conservative_fee_bps = (
        (float(estimate.open_fee) + float(estimate.estimated_close_fee))
        / notional
        * 10_000.0
    )
    conservative_slippage_bps = (
        max(0.0, float(estimate.rule.modeled_slippage_bps)) * 2.0
    )
    execution = (
        row.get("execution_evidence")
        if isinstance(row.get("execution_evidence"), Mapping)
        else {}
    )
    if row.get("execution_eligible") is True and execution:
        actual_fee_bps = (
            max(float(execution.get("fee_cash_cny") or 0.0), 0.0) / notional * 10_000.0
        )
        actual_slippage_bps = max(float(execution.get("slippage_bps") or 0.0), 0.0)
        return {
            "cost_model_version": ACTUAL_HYBRID_COST_MODEL_VERSION,
            "round_trip_fee_bps": actual_fee_bps
            + (float(estimate.estimated_close_fee) / notional * 10_000.0),
            "round_trip_slippage_bps": actual_slippage_bps
            + max(0.0, float(estimate.rule.modeled_slippage_bps)),
            "cost_evidence_event_id": str(
                execution.get("capital_commit_event_id")
                or execution.get("execution_fill_id")
                or ""
            ),
        }
    return {
        "cost_model_version": CONSERVATIVE_COST_MODEL_VERSION,
        "round_trip_fee_bps": conservative_fee_bps,
        "round_trip_slippage_bps": conservative_slippage_bps,
        "cost_evidence_event_id": "contract-rule:" + symbol,
    }


def _date_timestamp(value: Any) -> datetime | None:
    raw = "".join(character for character in str(value or "") if character.isdigit())
    if len(raw) < 8:
        return None
    try:
        return datetime.strptime(raw[:8], "%Y%m%d").replace(hour=15, tzinfo=CN_TZ)
    except ValueError:
        return None


def _price_evidence(
    reader: Any,
    row: Mapping[str, Any],
    *,
    as_of: datetime,
) -> tuple[list[dict[str, Any]], dict[str, datetime]]:
    prediction_at = _aware(row.get("point_in_time_as_of"))
    if prediction_at is None:
        return [], {}
    symbol = str(row.get("symbol") or "")
    trade_date = str(row.get("trade_date") or "").replace("-", "")
    end_date = as_of.astimezone(CN_TZ).strftime("%Y%m%d")
    try:
        intraday = reader.get_bars_intraday(
            "Futures", symbol, "5min", trade_date, end_date
        )
    except Exception:
        intraday = []
    try:
        daily = reader.get_bars_daily("Futures", symbol, trade_date, end_date)
    except Exception:
        daily = []

    points: list[dict[str, Any]] = []
    for raw in intraday or []:
        if not isinstance(raw, Mapping):
            continue
        point_at = _aware(
            raw.get("bar_time") or raw.get("timestamp") or raw.get("trade_time")
        )
        price = _positive(raw.get("close") or raw.get("price"))
        source = str(raw.get("source") or raw.get("provider") or "").strip()
        if point_at is None or price is None or not source:
            continue
        points.append(
            {
                "timestamp": point_at.isoformat(timespec="seconds"),
                "price": price,
                "source": source,
                "reliable": True,
            }
        )

    daily_rows: list[tuple[datetime, Mapping[str, Any]]] = []
    for raw in daily or []:
        if not isinstance(raw, Mapping):
            continue
        point_at = _date_timestamp(raw.get("trade_date") or raw.get("date"))
        price = _positive(raw.get("close") or raw.get("price"))
        source = str(raw.get("source") or raw.get("provider") or "").strip()
        if point_at is None or price is None or not source or point_at <= prediction_at:
            continue
        daily_rows.append((point_at, raw))
        points.append(
            {
                "timestamp": point_at.isoformat(timespec="seconds"),
                "price": price,
                "source": source,
                "reliable": True,
                "eligible_horizons": ["1d", "3d", "5d"],
            }
        )
    daily_rows.sort(key=lambda item: item[0])
    active_trade_close = _date_timestamp(trade_date)
    if active_trade_close is None or active_trade_close <= prediction_at:
        active_trade_close = prediction_at.replace(
            hour=15, minute=0, second=0, microsecond=0
        )
        if active_trade_close <= prediction_at:
            active_trade_close += timedelta(days=1)
    targets: dict[str, datetime] = {
        "m30": prediction_at + timedelta(minutes=30),
        "m60": prediction_at + timedelta(minutes=60),
        "close": active_trade_close,
    }
    for name, index in (("1d", 0), ("3d", 2), ("5d", 4)):
        if len(daily_rows) > index:
            targets[name] = daily_rows[index][0]
    return points, targets


def _existing_update_ids(path: Path) -> set[str]:
    result: set[str] = set()
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(row, dict)
            and row.get("record_type") == "cn_futures_forward_label_update"
        ):
            result.add(str(row.get("update_id") or ""))
    return result


def _append_updates(path: Path, updates: list[dict[str, Any]]) -> tuple[int, int]:
    if path.is_symlink():
        raise ValueError("review_path_symlink_not_allowed")
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    appended = 0
    idempotent = 0
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            existing = _existing_update_ids(path)
            with path.open("a", encoding="utf-8") as handle:
                for update in updates:
                    update_id = str(update.get("update_id") or "")
                    if update_id in existing:
                        idempotent += 1
                        continue
                    handle.write(
                        json.dumps(update, ensure_ascii=False, sort_keys=True) + "\n"
                    )
                    existing.add(update_id)
                    appended += 1
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return appended, idempotent


def materialize_cn_futures_forward_labels(
    *,
    review_path: str | Path,
    reader: Any,
    authority_scope: Mapping[str, Any],
    as_of: Any,
) -> dict[str, Any]:
    """Materialize due labels for every current, data-qualified session fact."""

    path = Path(review_path)
    current_as_of = _aware(as_of)
    if current_as_of is None:
        raise ValueError("as_of_timezone_required")
    if (
        authority_scope.get("capital_authority_id") != CURRENT_AUTHORITY_ID
        or authority_scope.get("authority_generation") != CURRENT_AUTHORITY_GENERATION
        or not str(authority_scope.get("execution_lineage_id") or "")
    ):
        raise ValueError("current_cn_futures_authority_scope_required")
    rows = load_review_rows(path, verify_checksums=True, include_summaries=False)
    updates: list[dict[str, Any]] = []
    excluded_authority = 0
    excluded_data = 0
    for row in rows:
        if not _authority_matches(row, authority_scope):
            excluded_authority += 1
            continue
        direction = _direction(row)
        entry_price = _entry_price(row)
        prediction_at = _aware(row.get("point_in_time_as_of"))
        if (
            direction not in {"buy", "sell", "long", "short"}
            or entry_price is None
            or prediction_at is None
        ):
            excluded_data += 1
            continue
        points, targets = _price_evidence(reader, row, as_of=current_as_of)
        costs = _costs(row, entry_price)
        snapshot = {
            "snapshot_id": str(row.get("_identity") or ""),
            "market": "cn_futures",
            "symbol": str(row.get("symbol") or ""),
            "style": str(row.get("style") or "unknown"),
            "style_version": str(row.get("style_version") or ""),
            "prediction_at": prediction_at.isoformat(timespec="seconds"),
            "reference_price": entry_price,
            "direction": direction,
            "forward_label_eligibility": "eligible",
            "forward_label_rejection_reason": None,
            "costs": costs,
            "real_trading_enabled": False,
        }
        materialized = materialize_forward_labels(
            snapshot,
            points,
            as_of=current_as_of,
            horizon_targets=targets,
            costs=costs,
        )
        labels: dict[str, dict[str, Any]] = {}
        source_sha = str(row.get("source_snapshot_sha256") or "").lower()
        for horizon, raw_label in materialized["labels"].items():
            label = {
                **dict(raw_label),
                "source_snapshot_sha256": source_sha,
                "point_in_time_as_of": current_as_of.isoformat(timespec="seconds"),
                "cost_model_version": costs["cost_model_version"],
                "cost_evidence_event_id": costs["cost_evidence_event_id"],
                "real_trading_enabled": False,
            }
            label["label_evidence_sha256"] = _sha256(label)
            labels[horizon] = label
        update = {
            "schema_version": UPDATE_SCHEMA_VERSION,
            "record_type": "cn_futures_forward_label_update",
            "target_identity": str(row.get("_identity") or ""),
            "target_record_type": str(row.get("record_type") or ""),
            "trade_date": str(row.get("trade_date") or ""),
            "style": str(row.get("style") or "unknown"),
            "symbol": str(row.get("symbol") or ""),
            "source_snapshot_sha256": source_sha,
            "capital_authority_id": CURRENT_AUTHORITY_ID,
            "authority_generation": CURRENT_AUTHORITY_GENERATION,
            "execution_lineage_id": str(authority_scope["execution_lineage_id"]),
            "point_in_time_as_of": current_as_of.isoformat(timespec="seconds"),
            "cost_model_version": costs["cost_model_version"],
            "labels": labels,
            "real_trading_enabled": False,
        }
        update["update_id"] = "CNFLABEL-" + _sha256(update)[:32]
        update["journal_payload_sha256"] = _sha256(update)
        updates.append(update)
    appended, idempotent = _append_updates(path, updates) if updates else (0, 0)
    return {
        "operation": "cn_futures_forward_label_materialization",
        "eligible_target_count": len(updates),
        "excluded_authority_count": excluded_authority,
        "excluded_data_quality_count": excluded_data,
        "appended_update_count": appended,
        "idempotent_update_count": idempotent,
        "cost_model_versions": sorted(
            {str(update["cost_model_version"]) for update in updates}
        ),
        "real_trading_enabled": False,
    }


__all__ = [
    "ACTUAL_HYBRID_COST_MODEL_VERSION",
    "CONSERVATIVE_COST_MODEL_VERSION",
    "UPDATE_SCHEMA_VERSION",
    "materialize_cn_futures_forward_labels",
]
