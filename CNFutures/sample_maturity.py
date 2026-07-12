#!/usr/bin/env python3
"""Pure CNFutures sample and maturity projection.

The existing CNFutures review JSONL remains the append-only fact input.  This
module verifies embedded per-session checksums, filters the approved fresh-start
window, de-duplicates decision/observation identities, reuses the shared layered
KPI builder, and evaluates the independent futures maturity state.  It never
creates capital, orders, promotions, or a live route.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from shared.review.forward_labels import canonical_horizon
from shared.review.market_maturity import FuturesEvidence, assess_futures_maturity
from shared.review.sample_kpi import build_sample_kpi

from .contract_rules import normalize_product
from .execution_evidence import validate_execution_evidence


REPORT_TYPE = "cn_futures_market_maturity_v1"
EVIDENCE_SOURCE = "cn_futures_review_journal+sample_kpi"
CURRENT_CAPITAL_AUTHORITY_ID = "cn-futures-capital-v1"
CURRENT_AUTHORITY_GENERATION = 1
CURRENT_POOL_CNY = 50_000.0
CURRENT_MARGIN_LIMIT_CNY = 25_000.0
SESSION_ROW_TYPE = "cn_futures_session_decision"
ROUND_TRIP_EVIDENCE_SCHEMA = "cn_futures.round_trip_evidence.v1"
FORWARD_LABEL_UPDATE_SCHEMA = "cn_futures.forward_label_update.v1"
FORWARD_LABEL_UPDATE_TYPE = "cn_futures_forward_label_update"
PROJECTION_HASH_FIELD = "projection_sha256"

_LIVE_BOOLEAN_FIELDS = {
    "real_trading_enabled",
    "live_execution_enabled",
    "real_money_enabled",
    "live_broker_enabled",
    "direct_execution_enabled",
    "production_execution_enabled",
    "is_live",
    "real_execution",
}
_MODE_FIELDS = {"account_type", "capital_layer", "execution_mode", "trading_mode"}
_LIVE_VALUES = {"1", "true", "yes", "on", "enabled", "live", "real", "production"}
_EXTREME_MARKERS = (
    "limit_gap",
    "gap_open",
    "night_gap",
    "circuit_breaker",
    "limit_up",
    "limit_down",
    "extreme",
    "stress",
)


class CNFuturesMaturityError(RuntimeError):
    """Raised when maturity evidence cannot be safely projected."""


def _compact_trade_date(value: Any) -> str:
    raw = str(value or "").strip()
    digits = "".join(character for character in raw[:10] if character.isdigit())
    if len(digits) != 8:
        raise CNFuturesMaturityError("trade_date_must_be_yyyymmdd")
    try:
        datetime.strptime(digits, "%Y%m%d")
    except ValueError as exc:
        raise CNFuturesMaturityError("invalid_trade_date") from exc
    return digits


def _finite_number(value: Any, *, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _aware_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _nonnegative_int(value: Any) -> int:
    try:
        parsed = int(float(value))
    except (OverflowError, TypeError, ValueError):
        return 0
    return max(0, parsed)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _is_sha256(value: Any) -> bool:
    raw = str(value or "").strip().lower()
    return len(raw) == 64 and all(character in "0123456789abcdef" for character in raw)


def _find_live_marker(value: Any, path: str = "root") -> str | None:
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key or "").strip().lower()
            nested_path = f"{path}.{raw_key}"
            if key in _LIVE_BOOLEAN_FIELDS:
                if nested is True or str(nested or "").strip().lower() in _LIVE_VALUES:
                    return nested_path
            if key in _MODE_FIELDS and str(nested or "").strip().lower() in {
                "live",
                "real",
                "production",
                "real_money",
            }:
                return nested_path
            marker = _find_live_marker(nested, nested_path)
            if marker:
                return marker
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            marker = _find_live_marker(nested, f"{path}[{index}]")
            if marker:
                return marker
    return None


def validate_futures_authority_state(
    authority_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the independent fresh-start 50k/25k-margin authority."""

    if not isinstance(authority_state, Mapping):
        raise CNFuturesMaturityError("capital_authority_state_required")
    live_marker = _find_live_marker(authority_state, "authority_state")
    if live_marker:
        raise CNFuturesMaturityError(f"live_capital_authority_rejected:{live_marker}")
    if str(authority_state.get("source") or "") != "market_capital_ledger":
        raise CNFuturesMaturityError("market_capital_ledger_source_required")
    if str(authority_state.get("authority_id") or "") != CURRENT_CAPITAL_AUTHORITY_ID:
        raise CNFuturesMaturityError("capital_authority_id_mismatch")
    if authority_state.get("authority_generation") != CURRENT_AUTHORITY_GENERATION:
        raise CNFuturesMaturityError("authority_generation_mismatch")
    if not math.isclose(
        _finite_number(authority_state.get("initial_equity_cny")),
        CURRENT_POOL_CNY,
        abs_tol=1e-6,
    ):
        raise CNFuturesMaturityError("initial_equity_must_be_50000")
    if not math.isclose(
        _finite_number(authority_state.get("margin_utilization_limit_cny")),
        CURRENT_MARGIN_LIMIT_CNY,
        abs_tol=1e-6,
    ):
        raise CNFuturesMaturityError("margin_limit_must_be_25000")
    execution_lineage_id = str(
        authority_state.get("execution_lineage_id") or ""
    ).strip()
    if not execution_lineage_id:
        raise CNFuturesMaturityError("execution_lineage_id_required")
    return {
        "capital_authority_id": CURRENT_CAPITAL_AUTHORITY_ID,
        "authority_generation": CURRENT_AUTHORITY_GENERATION,
        "execution_lineage_id": execution_lineage_id,
    }


def _canonical_session_checksum(row: Mapping[str, Any]) -> str:
    content = {key: value for key, value in row.items() if key != "_checksum"}
    encoded = json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_compact_sha256(value: Mapping[str, Any], hash_field: str) -> str:
    payload = {key: nested for key, nested in value.items() if key != hash_field}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_futures_maturity_projection_sha256(
    projection: Mapping[str, Any],
) -> str:
    """Return a Python/TypeScript-stable hash of a maturity projection."""

    def normalize(value: Any) -> Any:
        if value is None or isinstance(value, (bool, str, int)):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("projection_number_must_be_finite")
            return int(value) if value.is_integer() else value
        if isinstance(value, Mapping):
            return {str(key): normalize(nested) for key, nested in value.items()}
        if isinstance(value, (list, tuple)):
            return [normalize(nested) for nested in value]
        raise TypeError(f"projection_value_not_json_safe:{type(value).__name__}")

    payload = {
        key: nested
        for key, nested in projection.items()
        if key != PROJECTION_HASH_FIELD
    }
    encoded = json.dumps(
        normalize(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_futures_maturity_projection_hash(
    projection: Mapping[str, Any],
) -> bool:
    """Verify that no projected field changed after materialization."""

    provided = str(projection.get(PROJECTION_HASH_FIELD) or "").strip().lower()
    if not _is_sha256(provided):
        return False
    try:
        expected = canonical_futures_maturity_projection_sha256(projection)
    except (TypeError, ValueError):
        return False
    return provided == expected


def _record_hash_valid(row: Mapping[str, Any]) -> bool:
    journal_sha = str(row.get("journal_payload_sha256") or "").lower()
    if _is_sha256(journal_sha):
        return journal_sha == _canonical_compact_sha256(row, "journal_payload_sha256")
    checksum = str(row.get("_checksum") or "").lower()
    if not _is_sha256(checksum):
        return False
    # Existing session rows use json.dumps(sort_keys=True); the new immutable
    # sample facts use compact canonical JSON.  Both hashes cover every field.
    return checksum in {
        _canonical_session_checksum(row),
        _canonical_compact_sha256(row, "_checksum"),
    }


def _read_review_summaries(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if path.is_symlink():
        raise CNFuturesMaturityError("review_path_symlink_not_allowed")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CNFuturesMaturityError(
                f"review_json_corrupt_line_{line_number}"
            ) from exc
        if not isinstance(raw, dict):
            raise CNFuturesMaturityError(f"review_row_not_object_line_{line_number}")
        live_marker = _find_live_marker(raw, f"review[{line_number}]")
        if live_marker:
            raise CNFuturesMaturityError(f"live_review_marker_rejected:{live_marker}")
        session_rows = raw.get("session_decisions")
        if isinstance(session_rows, list):
            for index, session in enumerate(session_rows):
                if not isinstance(session, Mapping):
                    raise CNFuturesMaturityError(
                        f"session_decision_not_object:{line_number}:{index}"
                    )
                if session.get("_row_type") != SESSION_ROW_TYPE:
                    continue
                checksum = str(session.get("_checksum") or "")
                if not _is_sha256(checksum):
                    raise CNFuturesMaturityError(
                        f"session_checksum_missing:{line_number}:{index}"
                    )
                expected = _canonical_session_checksum(session)
                if checksum != expected:
                    raise CNFuturesMaturityError(
                        f"session_checksum_mismatch:{line_number}:{index}"
                    )
        observations = raw.get("observation_samples")
        if isinstance(observations, list):
            for index, observation in enumerate(observations):
                if not isinstance(observation, Mapping):
                    raise CNFuturesMaturityError(
                        f"observation_not_object:{line_number}:{index}"
                    )
        rows.append(raw)
    return rows


def validate_futures_review_safety(review_path: str | Path) -> int:
    """Fail closed on unsafe/corrupt journal content before append-only updates."""

    return len(_read_review_summaries(Path(review_path)))


def _review_authority_status(
    row: Mapping[str, Any], authority_scope: Mapping[str, Any]
) -> str:
    raw_scope = row.get("authority_scope")
    if not isinstance(raw_scope, Mapping):
        return "missing"
    normalized = {
        "capital_authority_id": str(raw_scope.get("capital_authority_id") or ""),
        "authority_generation": raw_scope.get("authority_generation"),
        "execution_lineage_id": str(raw_scope.get("execution_lineage_id") or ""),
    }
    return "current" if normalized == dict(authority_scope) else "mismatch"


def _valid_lineage(row: Mapping[str, Any], authority_scope: Mapping[str, Any]) -> bool:
    status = str(row.get("lineage_status") or "").strip().lower()
    source_sha = str(row.get("source_snapshot_sha256") or "")
    return (
        status == "complete"
        and _is_sha256(source_sha)
        and row.get("capital_authority_id") == authority_scope["capital_authority_id"]
        and row.get("authority_generation") == authority_scope["authority_generation"]
        and row.get("execution_lineage_id") == authority_scope["execution_lineage_id"]
    )


def _maturity_weight(row: Mapping[str, Any]) -> float:
    """Return scientific maturity weight without dropping unclustered facts."""

    if not str(row.get("cluster_id") or "").strip():
        return 1.0
    weight = _finite_number(row.get("weight_multiplier"), default=0.0)
    return max(0.0, weight)


def _scenario_tags(row: Mapping[str, Any]) -> Mapping[str, Any]:
    tags = row.get("scenario_tags")
    if isinstance(tags, Mapping):
        return tags
    decision = _mapping(row.get("decision"))
    return _mapping(decision.get("scenario_tags"))


def _size_decision(row: Mapping[str, Any]) -> Mapping[str, Any]:
    direct = row.get("size_decision")
    if isinstance(direct, Mapping):
        return direct
    return _mapping(_mapping(row.get("decision")).get("size_decision"))


def _row_reason(row: Mapping[str, Any]) -> str:
    return str(
        row.get("reason") or _mapping(row.get("decision")).get("reason") or ""
    ).strip()


def _row_product(row: Mapping[str, Any]) -> str:
    explicit = (
        str(row.get("product") or _scenario_tags(row).get("product") or "")
        .strip()
        .lower()
    )
    if explicit and explicit != "unknown":
        return explicit
    symbol = str(row.get("symbol") or "").strip()
    if not symbol or symbol.lower() == "unknown":
        return ""
    try:
        return normalize_product(symbol)
    except ValueError:
        return ""


def _volatility_regime(row: Mapping[str, Any]) -> str:
    tags = _scenario_tags(row)
    for key in ("volatility_regime", "volatility_bucket", "market_regime"):
        value = str(tags.get(key) or row.get(key) or "").strip().lower()
        if value and value not in {"unknown", "unavailable", "normal"}:
            return value
    return ""


def _night_session(row: Mapping[str, Any]) -> bool:
    session = str(
        row.get("session") or _scenario_tags(row).get("session") or ""
    ).lower()
    return "night" in session


def _rollover_evidence(row: Mapping[str, Any]) -> bool:
    tags = _scenario_tags(row)
    values = (
        tags.get("contract_rollover_handled"),
        tags.get("rollover_handled"),
        row.get("contract_rollover_handled"),
        row.get("rollover_handled"),
    )
    return any(value is True for value in values)


def _extreme_risk_evidence(row: Mapping[str, Any]) -> bool:
    tags = _scenario_tags(row)
    if tags.get("extreme_risk_covered") is True:
        return True
    text = "|".join(
        [
            _row_reason(row),
            str(tags.get("extreme_risk_scenario") or ""),
            str(tags.get("market_regime") or ""),
            str(tags.get("scenario") or ""),
        ]
    ).lower()
    return any(marker in text for marker in _EXTREME_MARKERS)


def _margin_evidence(row: Mapping[str, Any]) -> bool:
    size = _size_decision(row)
    return any(
        _finite_number(size.get(key), default=-1.0) > 0
        for key in ("margin_per_lot", "margin_required", "margin_budget")
    )


def _slippage_evidence(row: Mapping[str, Any]) -> bool:
    size = _size_decision(row)
    return any(
        _finite_number(size.get(key), default=-1.0) > 0
        for key in ("modeled_slippage_bps", "actual_slippage_bps", "slippage_cny")
    )


def _observation_match_key(row: Mapping[str, Any], trade_date: str) -> tuple[str, ...]:
    return (
        trade_date,
        str(row.get("session") or "").strip().lower(),
        str(row.get("style") or "unknown").strip(),
        str(row.get("symbol") or "unknown").strip().upper(),
    )


def _normalize_for_kpi(
    row: Mapping[str, Any],
    *,
    record_type: str,
    labels: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "record_type": record_type,
        "style": str(row.get("style") or "unknown"),
        "sample_intent": str(row.get("sample_intent") or "unclassified"),
        "status": str(row.get("status") or ""),
        "reject_reason": _row_reason(row) or "unspecified",
        "evidence_status": "complete",
        "labels": dict(labels or {}),
        "real_trading_enabled": False,
    }


def _validated_round_trip_evidence(
    row: Mapping[str, Any],
    *,
    authority_scope: Mapping[str, Any],
) -> tuple[dict[str, float] | None, str]:
    execution = _mapping(row.get("execution_evidence"))
    if execution.get("capital_commit_action") != "position_close_commit":
        return None, "not_position_close_commit"
    evidence = _mapping(row.get("round_trip_evidence"))
    if evidence.get("schema_version") != ROUND_TRIP_EVIDENCE_SCHEMA:
        return None, "round_trip_evidence_schema_invalid"
    supplied_sha = str(evidence.get("round_trip_evidence_sha256") or "").lower()
    if not _is_sha256(supplied_sha) or supplied_sha != _canonical_compact_sha256(
        evidence, "round_trip_evidence_sha256"
    ):
        return None, "round_trip_evidence_sha256_mismatch"
    if (
        evidence.get("capital_authority_id") != authority_scope["capital_authority_id"]
        or evidence.get("authority_generation")
        != authority_scope["authority_generation"]
        or evidence.get("execution_lineage_id")
        != authority_scope["execution_lineage_id"]
        or evidence.get("real_trading_enabled") is not False
    ):
        return None, "round_trip_evidence_authority_mismatch"
    if (
        evidence.get("round_trip_complete") is not True
        or evidence.get("costs_cover") != "round_trip"
        or not str(evidence.get("entry_fill_id") or "").strip()
        or evidence.get("exit_fill_id") != execution.get("execution_fill_id")
    ):
        return None, "round_trip_evidence_identity_incomplete"
    for key in ("entry_evidence_sha256", "exit_evidence_sha256"):
        if not _is_sha256(evidence.get(key)):
            return None, f"round_trip_evidence_{key}_invalid"
    values: dict[str, float] = {}
    for key in ("gross_pnl_cny", "fee_cny", "slippage_cny", "net_pnl_cny"):
        value = evidence.get(key)
        if isinstance(value, bool):
            return None, f"round_trip_evidence_{key}_invalid"
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None, f"round_trip_evidence_{key}_invalid"
        if not math.isfinite(parsed):
            return None, f"round_trip_evidence_{key}_invalid"
        values[key] = parsed
    if values["fee_cny"] < 0 or values["slippage_cny"] < 0:
        return None, "round_trip_evidence_cost_negative"
    expected_net = values["gross_pnl_cny"] - values["fee_cny"] - values["slippage_cny"]
    if not math.isclose(expected_net, values["net_pnl_cny"], abs_tol=1e-6):
        return None, "round_trip_evidence_net_pnl_mismatch"
    if values["fee_cny"] + 1e-9 < _finite_number(execution.get("fee_cash_cny")):
        return None, "round_trip_evidence_fee_below_exit_fill"
    return values, "complete"


def _validated_labels(
    row: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], int, int, int]:
    raw_labels = row.get("labels")
    if not isinstance(raw_labels, Mapping):
        return {}, 0, 0, 0
    source_sha = str(row.get("source_snapshot_sha256") or "").lower()
    labels: dict[str, dict[str, Any]] = {}
    ready = 0
    pending = 0
    invalid = 0
    for raw_horizon, raw_evidence in raw_labels.items():
        if not isinstance(raw_evidence, Mapping):
            invalid += 1
            continue
        try:
            horizon = canonical_horizon(str(raw_horizon))
        except ValueError:
            invalid += 1
            continue
        evidence = dict(raw_evidence)
        supplied_sha = str(evidence.get("label_evidence_sha256") or "").lower()
        status = str(evidence.get("status") or "").strip().lower()
        try:
            as_of = datetime.fromisoformat(
                str(evidence.get("point_in_time_as_of") or "").replace("Z", "+00:00")
            )
        except ValueError:
            as_of = None
        if (
            not _is_sha256(supplied_sha)
            or supplied_sha
            != _canonical_compact_sha256(evidence, "label_evidence_sha256")
            or evidence.get("source_snapshot_sha256") != source_sha
            or evidence.get("real_trading_enabled") is not False
            or not str(evidence.get("cost_model_version") or "").strip()
            or as_of is None
            or as_of.tzinfo is None
            or as_of.utcoffset() is None
        ):
            invalid += 1
            continue
        if status in {"ready", "labeled", "complete", "completed"}:
            normalized_status = "ready"
            ready += 1
        elif status in {"pending", "pending_not_due", "pending_future_bars"}:
            normalized_status = "pending_not_due"
            pending += 1
        else:
            invalid += 1
            continue
        labels[horizon] = {**evidence, "status": normalized_status}
    return labels, ready, pending, invalid


def _validated_forward_label_update(
    update: Mapping[str, Any],
    *,
    target: Mapping[str, Any],
    authority_scope: Mapping[str, Any],
    projection_at: datetime,
) -> tuple[dict[str, dict[str, Any]], datetime | None, str]:
    """Validate one immutable top-level label update against its target fact."""

    if (
        update.get("schema_version") != FORWARD_LABEL_UPDATE_SCHEMA
        or update.get("record_type") != FORWARD_LABEL_UPDATE_TYPE
        or not str(update.get("update_id") or "").strip()
        or not _record_hash_valid(update)
    ):
        return {}, None, "forward_label_update_hash_or_schema_invalid"
    if (
        update.get("capital_authority_id") != authority_scope["capital_authority_id"]
        or update.get("authority_generation") != authority_scope["authority_generation"]
        or update.get("execution_lineage_id") != authority_scope["execution_lineage_id"]
        or update.get("real_trading_enabled") is not False
    ):
        return {}, None, "forward_label_update_authority_mismatch"
    if (
        update.get("target_identity") != target.get("_identity")
        or update.get("target_record_type") != target.get("record_type")
        or update.get("trade_date") != target.get("trade_date")
        or update.get("style") != target.get("style")
        or update.get("symbol") != target.get("symbol")
        or update.get("source_snapshot_sha256") != target.get("source_snapshot_sha256")
    ):
        return {}, None, "forward_label_update_target_mismatch"
    update_at = _aware_datetime(update.get("point_in_time_as_of"))
    cost_model_version = str(update.get("cost_model_version") or "").strip()
    if (
        update_at is None
        or update_at > projection_at
        or not cost_model_version
        or not _is_sha256(update.get("source_snapshot_sha256"))
    ):
        return {}, None, "forward_label_update_pit_or_cost_invalid"
    labels, _ready, _pending, invalid = _validated_labels(update)
    if not labels or invalid:
        return {}, None, "forward_label_update_label_evidence_invalid"
    for label in labels.values():
        if (
            label.get("cost_model_version") != cost_model_version
            or _aware_datetime(label.get("point_in_time_as_of")) != update_at
        ):
            return {}, None, "forward_label_update_label_binding_invalid"
    return labels, update_at, "complete"


def _aggregate_performance(
    round_trips: Sequence[Mapping[str, float]],
) -> dict[str, Any]:
    completed = len(round_trips)
    wins = sum(1 for row in round_trips if row["net_pnl_cny"] > 0)
    losses = sum(1 for row in round_trips if row["net_pnl_cny"] < 0)
    gross = sum(row["gross_pnl_cny"] for row in round_trips)
    net = sum(row["net_pnl_cny"] for row in round_trips)
    fee = sum(row["fee_cny"] for row in round_trips)
    slippage = sum(row["slippage_cny"] for row in round_trips)
    cumulative = 0.0
    high_water = 0.0
    max_drawdown = 0.0
    for row in round_trips:
        cumulative += row["net_pnl_cny"]
        high_water = max(high_water, cumulative)
        max_drawdown = max(max_drawdown, high_water - cumulative)
    decisive = wins + losses
    return {
        "completed_round_trip_count": completed,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / decisive if decisive else None,
        "gross_pnl_cny": round(gross, 6),
        "fee_cny": round(fee, 6),
        "slippage_cny": round(slippage, 6),
        "post_cost_pnl_cny": round(net, 6) if completed > 0 else None,
        "expectancy_cny": round(net / completed, 6) if completed > 0 else None,
        "max_drawdown_cny": round(max_drawdown, 6) if completed > 0 else None,
        "fee_evidence_sample_count": completed,
        "stability_score": None,
        "stability_method": "not_available_requires_independent_multi_regime_evidence",
    }


def _append_once(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def build_futures_maturity_projection(
    *,
    review_path: str | Path,
    authority_state: Mapping[str, Any],
    fresh_start_trade_date: Any,
    trade_date: Any,
    generated_at: str,
) -> dict[str, Any]:
    """Build one deterministic, sim-only CNFutures maturity projection."""

    authority_scope = validate_futures_authority_state(authority_state)
    fresh_start = _compact_trade_date(fresh_start_trade_date)
    selected_trade_date = _compact_trade_date(trade_date)
    if selected_trade_date < fresh_start:
        raise CNFuturesMaturityError("trade_date_before_fresh_start")
    try:
        parsed_generated_at = datetime.fromisoformat(
            str(generated_at).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise CNFuturesMaturityError("generated_at_must_be_iso_timestamp") from exc
    if parsed_generated_at.tzinfo is None:
        raise CNFuturesMaturityError("generated_at_timezone_required")

    source = Path(review_path)
    summaries = _read_review_summaries(source)
    source_sha256 = (
        hashlib.sha256(source.read_bytes()).hexdigest()
        if source.exists()
        else hashlib.sha256(b"").hexdigest()
    )

    filtered: list[dict[str, Any]] = []
    excluded_pre_fresh = 0
    excluded_future = 0
    excluded_wrong_market = 0
    excluded_authority_mismatch = 0
    excluded_missing_authority_scope = 0
    forward_label_updates: list[dict[str, Any]] = []
    for summary in summaries:
        if summary.get("record_type") == FORWARD_LABEL_UPDATE_TYPE:
            forward_label_updates.append(summary)
            continue
        market = str(summary.get("market") or "").strip().lower()
        if market not in {"cn_futures", "cnfutures"}:
            excluded_wrong_market += 1
            continue
        date = _compact_trade_date(summary.get("date") or summary.get("trade_date"))
        if date < fresh_start:
            excluded_pre_fresh += 1
            continue
        if date > selected_trade_date:
            excluded_future += 1
            continue
        authority_status = _review_authority_status(summary, authority_scope)
        if authority_status == "mismatch":
            excluded_authority_mismatch += 1
            continue
        if authority_status == "missing":
            excluded_missing_authority_scope += 1
            continue
        filtered.append(summary)

    seen_sessions: set[str] = set()
    seen_observations: set[str] = set()
    observation_match_keys: set[tuple[str, ...]] = set()
    effective_observations: list[dict[str, Any]] = []
    effective_execution: list[dict[str, Any]] = []
    effective_nonexecution_sessions: list[dict[str, Any]] = []
    accepted_review_rows: set[int] = set()
    duplicate_sessions = 0
    duplicate_observations = 0
    invalid_lineage_samples = 0
    invalid_record_hash_samples = 0
    invalid_execution_evidence_samples = 0
    invalid_round_trip_evidence_samples = 0
    invalid_forward_label_evidence_count = 0
    missing_session_identity = 0
    missing_observation_identity = 0
    zero_maturity_weight_samples = 0
    round_trips: list[dict[str, float]] = []
    labels_by_record_identity: dict[str, dict[str, dict[str, Any]]] = {}
    target_sessions: dict[str, dict[str, Any]] = {}
    session_identity_by_match_key: dict[tuple[str, ...], str] = {}
    accepted_forward_label_updates = 0
    invalid_forward_label_updates = 0
    orphan_forward_label_updates = 0
    zero_weight_forward_label_updates = 0
    future_forward_label_updates = 0
    superseded_forward_label_updates = 0
    forward_labels = 0
    pending_labels = 0

    for summary_index, summary in enumerate(filtered):
        date = _compact_trade_date(summary.get("date") or summary.get("trade_date"))
        observations = summary.get("observation_samples")
        if isinstance(observations, list):
            for raw in observations:
                if not isinstance(raw, Mapping):
                    continue
                observation_id = str(raw.get("observation_id") or "").strip()
                if not observation_id:
                    missing_observation_identity += 1
                    continue
                if observation_id in seen_observations:
                    duplicate_observations += 1
                    continue
                seen_observations.add(observation_id)
                if not _record_hash_valid(raw):
                    invalid_record_hash_samples += 1
                    continue
                if not _valid_lineage(raw, authority_scope):
                    invalid_lineage_samples += 1
                    continue
                row = dict(raw)
                row["trade_date"] = date
                if _maturity_weight(row) <= 0:
                    zero_maturity_weight_samples += 1
                    continue
                labels, _ready, _pending, invalid_labels = _validated_labels(row)
                labels_by_record_identity[f"observation:{observation_id}"] = labels
                invalid_forward_label_evidence_count += invalid_labels
                effective_observations.append(row)
                observation_match_keys.add(_observation_match_key(row, date))
                accepted_review_rows.add(summary_index)

        sessions = summary.get("session_decisions")
        if isinstance(sessions, list):
            for raw in sessions:
                if (
                    not isinstance(raw, Mapping)
                    or raw.get("_row_type") != SESSION_ROW_TYPE
                ):
                    continue
                identity = str(raw.get("_identity") or "").strip()
                if not identity:
                    missing_session_identity += 1
                    continue
                if identity in seen_sessions:
                    duplicate_sessions += 1
                    continue
                seen_sessions.add(identity)
                if not _record_hash_valid(raw):
                    invalid_record_hash_samples += 1
                    continue
                if not _valid_lineage(raw, authority_scope):
                    invalid_lineage_samples += 1
                    continue
                row = dict(raw)
                row["trade_date"] = date
                target_sessions[identity] = row
                if _maturity_weight(row) <= 0:
                    zero_maturity_weight_samples += 1
                    continue
                labels, _ready, _pending, invalid_labels = _validated_labels(row)
                record_type = str(row.get("record_type") or "").strip().lower()
                if (
                    record_type == "simulated_fill"
                    and row.get("execution_eligible") is True
                    and row.get("counterfactual_only") is not True
                ):
                    execution = _mapping(row.get("execution_evidence"))
                    evidence_valid, _ = validate_execution_evidence(
                        execution,
                        source_snapshot_sha256=str(
                            row.get("source_snapshot_sha256") or ""
                        ),
                    )
                    if (
                        not evidence_valid
                        or execution.get("execution_lineage_id")
                        != authority_scope["execution_lineage_id"]
                    ):
                        invalid_execution_evidence_samples += 1
                        continue
                    effective_execution.append(row)
                    round_trip, _round_trip_reason = _validated_round_trip_evidence(
                        row,
                        authority_scope=authority_scope,
                    )
                    if round_trip is not None:
                        round_trips.append(round_trip)
                    elif (
                        execution.get("capital_commit_action")
                        == "position_close_commit"
                    ):
                        invalid_round_trip_evidence_samples += 1
                else:
                    effective_nonexecution_sessions.append(row)
                    session_identity_by_match_key[_observation_match_key(row, date)] = (
                        identity
                    )
                labels_by_record_identity[f"session:{identity}"] = labels
                invalid_forward_label_evidence_count += invalid_labels
                accepted_review_rows.add(summary_index)

    latest_label_updates: dict[
        tuple[str, str], tuple[datetime, str, dict[str, Any]]
    ] = {}
    for update in forward_label_updates:
        target_identity = str(update.get("target_identity") or "").strip()
        target = target_sessions.get(target_identity)
        if target is None:
            orphan_forward_label_updates += 1
            continue
        if _maturity_weight(target) <= 0:
            zero_weight_forward_label_updates += 1
            continue
        labels, update_at, reason = _validated_forward_label_update(
            update,
            target=target,
            authority_scope=authority_scope,
            projection_at=parsed_generated_at,
        )
        if (
            reason == "forward_label_update_pit_or_cost_invalid"
            and (
                _aware_datetime(update.get("point_in_time_as_of"))
                or parsed_generated_at
            )
            > parsed_generated_at
        ):
            future_forward_label_updates += 1
            continue
        if reason != "complete" or update_at is None:
            invalid_forward_label_updates += 1
            continue
        accepted_forward_label_updates += 1
        update_id = str(update.get("update_id") or "")
        for horizon, label in labels.items():
            key = (target_identity, horizon)
            previous = latest_label_updates.get(key)
            if previous is None or update_at >= previous[0]:
                latest_label_updates[key] = (update_at, update_id, label)

    winning_update_ids = {
        update_id for _timestamp, update_id, _label in latest_label_updates.values()
    }
    superseded_forward_label_updates = max(
        0, accepted_forward_label_updates - len(winning_update_ids)
    )
    for (target_identity, horizon), (
        _timestamp,
        _update_id,
        label,
    ) in latest_label_updates.items():
        labels_by_record_identity.setdefault(f"session:{target_identity}", {})[
            horizon
        ] = label

    # The review writes one observation and one session decision for the same
    # hold/reject.  Keep the richer observation as the primary learning sample
    # while retaining the session row for rejection and coverage attribution.
    unmatched_nonexecution = [
        row
        for row in effective_nonexecution_sessions
        if _observation_match_key(row, str(row.get("trade_date") or ""))
        not in observation_match_keys
    ]

    learning_observations = [*effective_observations, *unmatched_nonexecution]
    sample_kpi_records: list[dict[str, Any]] = []
    for row in learning_observations:
        record_type = str(row.get("record_type") or "").lower()
        if record_type == "risk_reject":
            normalized_type = "risk_reject"
        elif row.get("counterfactual_only") is True:
            normalized_type = "counterfactual"
        else:
            normalized_type = "observation"
        record_identity = (
            f"observation:{row.get('observation_id')}"
            if row.get("observation_id")
            else f"session:{row.get('_identity')}"
        )
        selected_labels = dict(labels_by_record_identity.get(record_identity) or {})
        paired_session_identity = session_identity_by_match_key.get(
            _observation_match_key(row, str(row.get("trade_date") or ""))
        )
        if paired_session_identity:
            selected_labels.update(
                labels_by_record_identity.get(f"session:{paired_session_identity}")
                or {}
            )
        sample_kpi_records.append(
            _normalize_for_kpi(
                row,
                record_type=normalized_type,
                labels=selected_labels,
            )
        )
    for row in effective_execution:
        sample_kpi_records.append(
            _normalize_for_kpi(
                row,
                record_type="simulated_fill",
                labels=labels_by_record_identity.get(f"session:{row.get('_identity')}"),
            )
        )
    for record in sample_kpi_records:
        for label in _mapping(record.get("labels")).values():
            status = str(_mapping(label).get("status") or "").lower()
            if status == "ready":
                forward_labels += 1
            elif status == "pending_not_due":
                pending_labels += 1
    for sequence, round_trip in enumerate(round_trips):
        sample_kpi_records.append(
            {
                "record_type": "completed_round_trip",
                "style": "unknown",
                "sample_intent": "unclassified",
                "round_trip_complete": True,
                "gross_pnl_cny": round_trip["gross_pnl_cny"],
                "fee_cny": round_trip["fee_cny"],
                "slippage_cny": round_trip["slippage_cny"],
                "net_pnl_cny": round_trip["net_pnl_cny"],
                "timestamp": f"{selected_trade_date}:{sequence:06d}",
                "evidence_status": "complete",
                "real_trading_enabled": False,
            }
        )
    sample_kpi = build_sample_kpi(sample_kpi_records)

    performance = _aggregate_performance(round_trips)
    counterfactual_count = sum(
        1 for row in learning_observations if row.get("counterfactual_only") is True
    )
    risk_reject_count = sum(
        1
        for row in effective_nonexecution_sessions
        if str(row.get("record_type") or "").lower() == "risk_reject"
    )
    valid_sample_count = len(learning_observations) + len(effective_execution)

    coverage_rows = [*learning_observations, *effective_execution]
    products = sorted(
        {product for row in coverage_rows if (product := _row_product(row))}
    )
    regimes = sorted(
        {regime for row in coverage_rows if (regime := _volatility_regime(row))}
    )
    night_count = sum(1 for row in coverage_rows if _night_session(row))
    rollover_count = sum(1 for row in coverage_rows if _rollover_evidence(row))
    extreme_count = sum(1 for row in coverage_rows if _extreme_risk_evidence(row))
    margin_count = len(effective_execution)
    slippage_count = len(effective_execution)
    trading_days = sorted(
        {
            str(row.get("trade_date") or "")
            for row in coverage_rows
            if str(row.get("trade_date") or "")
        }
    )
    reason_distribution: Counter[str] = Counter()
    for row in effective_nonexecution_sessions:
        reason = _row_reason(row)
        if reason:
            reason_distribution[reason] += 1

    evidence = FuturesEvidence(
        valid_sample_count=valid_sample_count,
        completed_round_trip_count=performance["completed_round_trip_count"],
        capital_authority_id=CURRENT_CAPITAL_AUTHORITY_ID,
        authority_generation=CURRENT_AUTHORITY_GENERATION,
        variety_coverage_count=len(products),
        volatility_regime_count=len(regimes),
        night_session_coverage=night_count > 0,
        contract_rollover_handled=rollover_count > 0,
        extreme_risk_scenarios_covered=extreme_count,
        win_rate=performance["win_rate"],
        expectancy_cny=performance["expectancy_cny"],
        post_cost_pnl_cny=performance["post_cost_pnl_cny"],
        max_drawdown_cny=performance["max_drawdown_cny"],
        stability_score=None,
        human_confirmed=False,
    )
    assessment = asdict(assess_futures_maturity(evidence))
    blocking_reasons = list(assessment.get("blockers") or [])
    _append_once(blocking_reasons, "missing_independent_stability_evidence")
    if excluded_missing_authority_scope:
        _append_once(
            blocking_reasons, "review_rows_missing_embedded_capital_authority_scope"
        )
    if invalid_lineage_samples:
        _append_once(blocking_reasons, "invalid_point_in_time_lineage_samples_excluded")
    if missing_session_identity or missing_observation_identity:
        _append_once(blocking_reasons, "sample_identity_missing")
    if invalid_record_hash_samples:
        _append_once(blocking_reasons, "invalid_sample_hashes_excluded")
    if invalid_execution_evidence_samples:
        _append_once(blocking_reasons, "invalid_execution_evidence_samples_excluded")
    if invalid_round_trip_evidence_samples:
        _append_once(blocking_reasons, "invalid_round_trip_evidence_samples_excluded")
    if invalid_forward_label_evidence_count:
        _append_once(blocking_reasons, "invalid_forward_label_evidence_excluded")
    if invalid_forward_label_updates or orphan_forward_label_updates:
        _append_once(blocking_reasons, "invalid_forward_label_updates_excluded")
    if (
        performance["completed_round_trip_count"] > 0
        and performance["fee_evidence_sample_count"] <= 0
    ):
        _append_once(blocking_reasons, "missing_fee_evidence")
    if performance["completed_round_trip_count"] > 0 and slippage_count <= 0:
        _append_once(blocking_reasons, "missing_slippage_evidence")

    promotion_evidence_ready = bool(
        assessment.get("promotion_evidence_ready") and not blocking_reasons
    )
    assessment["blockers"] = blocking_reasons
    assessment["promotion_evidence_ready"] = promotion_evidence_ready
    assessment["total_trading_days"] = len(trading_days)
    assessment["evidence_summary"].update(
        {
            "simulation_trading_days": trading_days,
            "observation_counterfactual_count": len(learning_observations),
            "counterfactual_only_count": counterfactual_count,
            "execution_eligible_sample_count": len(effective_execution),
            "forward_label_count": forward_labels,
            "pending_forward_label_count": pending_labels,
            "margin_evidence_sample_count": margin_count,
            "fee_evidence_sample_count": performance["fee_evidence_sample_count"],
            "slippage_evidence_sample_count": slippage_count,
        }
    )

    projection = {
        "report_type": REPORT_TYPE,
        "evidence_source": EVIDENCE_SOURCE,
        "generated_at": str(generated_at),
        "trade_date": selected_trade_date,
        "market": "cnfutures",
        "capital_layer": "simulated",
        "account_type": "simulated",
        "authority_scope": authority_scope,
        "pool_cny": int(CURRENT_POOL_CNY),
        "margin_utilization_limit_pct": 0.50,
        "margin_utilization_limit_cny": int(CURRENT_MARGIN_LIMIT_CNY),
        "fresh_start_trade_date": fresh_start,
        "simulation_trading_days": trading_days,
        "total_simulation_trading_days": len(trading_days),
        "stage": assessment["stage"],
        "exploration_eligible": assessment["exploration_eligible"],
        "promotion_evidence_ready": promotion_evidence_ready,
        "promotion_policy_status": "manual_review_only_no_futures_live_date",
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
        "live_transition_authorized": False,
        "real_trading_enabled": False,
        "live_execution_enabled": False,
        "sample_counts": {
            "valid_sample_count": valid_sample_count,
            "observation_counterfactual_count": len(learning_observations),
            "counterfactual_only_count": counterfactual_count,
            "execution_eligible_sample_count": len(effective_execution),
            "completed_round_trip_count": performance["completed_round_trip_count"],
            "forward_label_count": forward_labels,
            "pending_forward_label_count": pending_labels,
            "risk_reject_count": risk_reject_count,
            "exploration_fill_count": _nonnegative_int(
                _mapping(sample_kpi.get("sample_layer_totals")).get("exploration_fill")
            ),
            "exploitation_fill_count": _nonnegative_int(
                _mapping(sample_kpi.get("sample_layer_totals")).get("exploitation_fill")
            ),
        },
        "coverage": {
            "products": products,
            "product_count": len(products),
            "volatility_regimes": regimes,
            "volatility_regime_count": len(regimes),
            "night_session_sample_count": night_count,
            "rollover_sample_count": rollover_count,
            "margin_evidence_sample_count": margin_count,
            "fee_evidence_sample_count": performance["fee_evidence_sample_count"],
            "slippage_evidence_sample_count": slippage_count,
            "extreme_risk_sample_count": extreme_count,
        },
        "performance": performance,
        "risk_rejection_reason_distribution": dict(sorted(reason_distribution.items())),
        "blocking_reasons": blocking_reasons,
        "maturity_assessment": assessment,
        "sample_kpi_projection": sample_kpi,
        "source_review_path": str(source.absolute()),
        "source_review_sha256": source_sha256,
        "source_review_row_count": len(summaries),
        "current_review_row_count": len(filtered),
        "accepted_review_row_count": len(accepted_review_rows),
        "excluded_pre_fresh_start_review_count": excluded_pre_fresh,
        "excluded_future_review_count": excluded_future,
        "excluded_wrong_market_review_count": excluded_wrong_market,
        "excluded_authority_mismatch_review_count": excluded_authority_mismatch,
        "excluded_missing_authority_scope_review_count": excluded_missing_authority_scope,
        "duplicate_session_decision_count": duplicate_sessions,
        "duplicate_observation_count": duplicate_observations,
        "invalid_lineage_sample_count": invalid_lineage_samples,
        "invalid_record_hash_sample_count": invalid_record_hash_samples,
        "invalid_execution_evidence_sample_count": invalid_execution_evidence_samples,
        "invalid_round_trip_evidence_sample_count": invalid_round_trip_evidence_samples,
        "invalid_forward_label_evidence_count": invalid_forward_label_evidence_count,
        "accepted_forward_label_update_count": accepted_forward_label_updates,
        "superseded_forward_label_update_count": superseded_forward_label_updates,
        "invalid_forward_label_update_count": invalid_forward_label_updates,
        "orphan_forward_label_update_count": orphan_forward_label_updates,
        "future_forward_label_update_count": future_forward_label_updates,
        "zero_maturity_weight_sample_count": zero_maturity_weight_samples,
        "zero_maturity_weight_forward_label_update_count": zero_weight_forward_label_updates,
        "missing_session_identity_count": missing_session_identity,
        "missing_observation_identity_count": missing_observation_identity,
        "orders_created": 0,
        "emails_sent": 0,
        "accounts_created": 0,
    }
    projection[PROJECTION_HASH_FIELD] = canonical_futures_maturity_projection_sha256(
        projection
    )
    return projection


__all__ = [
    "CNFuturesMaturityError",
    "CURRENT_AUTHORITY_GENERATION",
    "CURRENT_CAPITAL_AUTHORITY_ID",
    "EVIDENCE_SOURCE",
    "REPORT_TYPE",
    "build_futures_maturity_projection",
    "canonical_futures_maturity_projection_sha256",
    "validate_futures_maturity_projection_hash",
    "validate_futures_authority_state",
    "validate_futures_review_safety",
]
