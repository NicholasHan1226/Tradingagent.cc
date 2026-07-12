"""Hash-bound execution evidence for CNFutures simulated fills."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from hashlib import sha256
from typing import Any, Mapping


SCHEMA_VERSION = "cn_futures.execution_evidence.v1"
CAPITAL_AUTHORITY_ID = "cn-futures-capital-v1"
AUTHORITY_GENERATION = 1
_FILL_EVIDENCE_TYPES = {
    "bar_volume_participation",
    "order_book_ask",
    "order_book_bid",
}
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _canonical_sha256(value: Any) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 and result == value else None


def _aware_timestamp(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.isoformat(timespec="seconds")


def _precommit_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(receipt)
    for key in (
        "cn_futures_capital_commit",
        "cn_futures_capital_commit_action_id",
        "capital_commit_status",
        "execution_evidence",
    ):
        payload.pop(key, None)
    return payload


def build_execution_evidence(
    *,
    order: Mapping[str, Any],
    receipt: Mapping[str, Any],
    capital_commit_request: Mapping[str, Any],
    capital_commit_result: Mapping[str, Any],
    source_snapshot_sha256: str,
) -> dict[str, Any]:
    """Build evidence only after the atomic capital commit completed."""

    if capital_commit_result.get("committed") is not True or str(
        capital_commit_result.get("status") or ""
    ) not in {"committed", "idempotent"}:
        raise ValueError("capital_commit_not_completed")
    expected_receipt_sha = str(capital_commit_request.get("receipt_sha256") or "")
    actual_receipt_sha = _canonical_sha256(_precommit_receipt(receipt))
    if expected_receipt_sha != actual_receipt_sha:
        raise ValueError("receipt_sha256_mismatch")

    raw = receipt.get("raw_response")
    if not isinstance(raw, Mapping):
        raise ValueError("receipt_raw_response_required")
    action = str(order.get("capital_commit_action") or "").strip()
    if action not in {"fill_commit", "position_close_commit"}:
        raise ValueError("capital_commit_action_invalid")
    filled_quantity = capital_commit_request.get(
        "actual_filled_quantity",
        capital_commit_request.get("actual_closed_quantity"),
    )
    local_state_sha = str(
        capital_commit_request.get("local_trade_sha256")
        or capital_commit_request.get("local_position_sha256")
        or ""
    ).lower()
    contract_spec_sha = str(
        capital_commit_request.get("contract_spec_sha256") or ""
    ).lower()
    if not contract_spec_sha:
        rule = raw.get("rule") if isinstance(raw.get("rule"), Mapping) else {}
        contract_spec_sha = _canonical_sha256(rule)
    contract_spec_version = str(
        capital_commit_request.get("contract_spec_version")
        or "cn-futures-executor-rule.v1"
    )
    snapshot = capital_commit_result.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    payload = {
        "schema_version": SCHEMA_VERSION,
        "capital_authority_id": str(capital_commit_request.get("authority_id") or ""),
        "authority_generation": capital_commit_request.get("authority_generation"),
        "execution_lineage_id": str(
            capital_commit_request.get("execution_lineage_id") or ""
        ),
        "order_id": str(order.get("order_id") or receipt.get("order_id") or ""),
        "symbol": str(
            order.get("symbol") or capital_commit_request.get("risk_unit_key") or ""
        ),
        "side": str(order.get("side") or capital_commit_request.get("side") or ""),
        "execution_fill_id": str(capital_commit_request.get("execution_fill_id") or ""),
        "filled_quantity": filled_quantity,
        "fill_price": capital_commit_request.get("actual_fill_price"),
        "requested_price": raw.get("requested_price"),
        "fee_cash_cny": capital_commit_request.get("actual_fee_cash_cny"),
        "slippage_bps": raw.get("slippage_bps"),
        "fill_evidence_type": str(raw.get("fill_evidence_type") or ""),
        "evidence_timestamp": str(raw.get("evidence_timestamp") or ""),
        "margin_required_cny": raw.get("margin_required"),
        "contract_multiplier": raw.get(
            "contract_multiplier",
            capital_commit_request.get("contract_multiplier"),
        ),
        "contract_spec_version": contract_spec_version,
        "contract_spec_sha256": contract_spec_sha,
        "receipt_sha256": expected_receipt_sha.lower(),
        "local_state_sha256": local_state_sha,
        "capital_commit_action": action,
        "capital_commit_action_id": str(order.get("capital_commit_action_id") or ""),
        "capital_commit_reference_id": str(
            order.get("capital_commit_reference_id") or ""
        ),
        "capital_commit_status": "committed",
        "capital_commit_event_id": str(capital_commit_result.get("event_id") or ""),
        "capital_commit_event_checksum": str(
            capital_commit_result.get("event_checksum")
            or snapshot.get("event_checksum")
            or ""
        ).lower(),
        "source_snapshot_sha256": str(source_snapshot_sha256 or "").lower(),
        "real_trading_enabled": False,
    }
    filled_quantity_number = _positive_int(payload["filled_quantity"])
    fill_price_number = _number(payload["fill_price"])
    requested_price_number = _number(payload["requested_price"])
    multiplier_number = _number(payload["contract_multiplier"])
    payload["slippage_cny"] = (
        round(
            abs(fill_price_number - requested_price_number)
            * filled_quantity_number
            * multiplier_number,
            6,
        )
        if filled_quantity_number is not None
        and fill_price_number is not None
        and requested_price_number is not None
        and multiplier_number is not None
        else None
    )
    valid, reason = validate_execution_evidence(
        {**payload, "execution_evidence_sha256": _canonical_sha256(payload)},
        source_snapshot_sha256=source_snapshot_sha256,
    )
    if not valid:
        raise ValueError(reason)
    payload["execution_evidence_sha256"] = _canonical_sha256(payload)
    return payload


def validate_execution_evidence(
    evidence: Mapping[str, Any],
    *,
    source_snapshot_sha256: str,
) -> tuple[bool, str]:
    if (
        not isinstance(evidence, Mapping)
        or evidence.get("schema_version") != SCHEMA_VERSION
    ):
        return False, "execution_evidence_schema_invalid"
    payload = dict(evidence)
    supplied_sha = str(payload.pop("execution_evidence_sha256", "")).lower()
    if not _SHA256.fullmatch(supplied_sha) or supplied_sha != _canonical_sha256(
        payload
    ):
        return False, "execution_evidence_sha256_mismatch"
    if payload.get("capital_authority_id") != CAPITAL_AUTHORITY_ID:
        return False, "execution_evidence_authority_invalid"
    if (
        type(payload.get("authority_generation")) is not int
        or payload.get("authority_generation") != AUTHORITY_GENERATION
    ):
        return False, "execution_evidence_generation_invalid"
    for key in (
        "execution_lineage_id",
        "order_id",
        "symbol",
        "execution_fill_id",
        "capital_commit_action_id",
        "capital_commit_reference_id",
        "capital_commit_event_id",
        "contract_spec_version",
    ):
        if not str(payload.get(key) or "").strip():
            return False, f"execution_evidence_{key}_missing"
    if str(payload.get("side") or "").lower() not in {"buy", "sell"}:
        return False, "execution_evidence_side_invalid"
    if _positive_int(payload.get("filled_quantity")) is None:
        return False, "execution_evidence_quantity_invalid"
    for key in (
        "fill_price",
        "requested_price",
        "margin_required_cny",
        "contract_multiplier",
    ):
        value = _number(payload.get(key))
        if value is None or value <= 0:
            return False, f"execution_evidence_{key}_invalid"
    for key in ("fee_cash_cny", "slippage_bps", "slippage_cny"):
        value = _number(payload.get(key))
        if value is None or value < 0:
            return False, f"execution_evidence_{key}_invalid"
    if payload.get("fill_evidence_type") not in _FILL_EVIDENCE_TYPES:
        return False, "execution_evidence_fill_source_invalid"
    if _aware_timestamp(payload.get("evidence_timestamp")) is None:
        return False, "execution_evidence_timestamp_invalid"
    expected_source_sha = str(source_snapshot_sha256 or "").lower()
    if (
        not _SHA256.fullmatch(expected_source_sha)
        or payload.get("source_snapshot_sha256") != expected_source_sha
    ):
        return False, "execution_evidence_source_sha256_mismatch"
    for key in (
        "contract_spec_sha256",
        "receipt_sha256",
        "local_state_sha256",
        "capital_commit_event_checksum",
    ):
        if not _SHA256.fullmatch(str(payload.get(key) or "").lower()):
            return False, f"execution_evidence_{key}_invalid"
    if payload.get("capital_commit_action") not in {
        "fill_commit",
        "position_close_commit",
    }:
        return False, "execution_evidence_commit_action_invalid"
    if payload.get("capital_commit_status") != "committed":
        return False, "execution_evidence_commit_status_invalid"
    if payload.get("real_trading_enabled") is not False:
        return False, "execution_evidence_real_trading_forbidden"
    return True, "complete"


def build_round_trip_evidence(
    *,
    entry_execution_evidence: Mapping[str, Any],
    exit_execution_evidence: Mapping[str, Any],
    closed_quantity: int,
    actual_fill_gross_pnl_cny: float,
) -> dict[str, Any]:
    """Bind one close to its actual open and all realized transaction costs."""

    for label, evidence in (
        ("entry", entry_execution_evidence),
        ("exit", exit_execution_evidence),
    ):
        valid, reason = validate_execution_evidence(
            evidence,
            source_snapshot_sha256=str(evidence.get("source_snapshot_sha256") or ""),
        )
        if not valid:
            raise ValueError(f"{label}_execution_evidence_invalid:{reason}")
    if entry_execution_evidence.get("capital_commit_action") != "fill_commit":
        raise ValueError("entry_fill_commit_required")
    if exit_execution_evidence.get("capital_commit_action") != "position_close_commit":
        raise ValueError("exit_position_close_commit_required")
    if (
        entry_execution_evidence.get("capital_authority_id")
        != exit_execution_evidence.get("capital_authority_id")
        or entry_execution_evidence.get("authority_generation")
        != exit_execution_evidence.get("authority_generation")
        or entry_execution_evidence.get("execution_lineage_id")
        != exit_execution_evidence.get("execution_lineage_id")
        or entry_execution_evidence.get("symbol")
        != exit_execution_evidence.get("symbol")
    ):
        raise ValueError("round_trip_authority_or_symbol_mismatch")
    quantity = _positive_int(closed_quantity)
    entry_quantity = _positive_int(entry_execution_evidence.get("filled_quantity"))
    exit_quantity = _positive_int(exit_execution_evidence.get("filled_quantity"))
    if (
        quantity is None
        or entry_quantity is None
        or exit_quantity is None
        or quantity > entry_quantity
        or quantity > exit_quantity
    ):
        raise ValueError("round_trip_quantity_invalid")
    actual_gross = _number(actual_fill_gross_pnl_cny)
    if actual_gross is None:
        raise ValueError("round_trip_actual_gross_invalid")
    entry_ratio = quantity / entry_quantity
    exit_ratio = quantity / exit_quantity
    fee = round(
        float(entry_execution_evidence["fee_cash_cny"]) * entry_ratio
        + float(exit_execution_evidence["fee_cash_cny"]) * exit_ratio,
        6,
    )
    slippage = round(
        float(entry_execution_evidence["slippage_cny"]) * entry_ratio
        + float(exit_execution_evidence["slippage_cny"]) * exit_ratio,
        6,
    )
    reference_gross = round(actual_gross + slippage, 6)
    net = round(reference_gross - fee - slippage, 6)
    payload = {
        "schema_version": "cn_futures.round_trip_evidence.v1",
        "capital_authority_id": entry_execution_evidence["capital_authority_id"],
        "authority_generation": entry_execution_evidence["authority_generation"],
        "execution_lineage_id": entry_execution_evidence["execution_lineage_id"],
        "symbol": entry_execution_evidence["symbol"],
        "entry_fill_id": entry_execution_evidence["execution_fill_id"],
        "exit_fill_id": exit_execution_evidence["execution_fill_id"],
        "entry_evidence_sha256": entry_execution_evidence["execution_evidence_sha256"],
        "exit_evidence_sha256": exit_execution_evidence["execution_evidence_sha256"],
        "closed_quantity": quantity,
        "round_trip_complete": True,
        "costs_cover": "round_trip",
        "gross_pnl_cny": reference_gross,
        "fee_cny": fee,
        "slippage_cny": slippage,
        "net_pnl_cny": net,
        "actual_fill_gross_pnl_cny": round(actual_gross, 6),
        "real_trading_enabled": False,
    }
    payload["round_trip_evidence_sha256"] = _canonical_sha256(payload)
    return payload


__all__ = [
    "AUTHORITY_GENERATION",
    "CAPITAL_AUTHORITY_ID",
    "SCHEMA_VERSION",
    "build_execution_evidence",
    "build_round_trip_evidence",
    "validate_execution_evidence",
]
