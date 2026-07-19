"""Strict A-share capital-position authority reconciliation.

The market-capital replay owns the current A-share position set.  Every other
position source is untrusted until its complete authority envelope and its
normalized position evidence match that replay.  Callers can therefore block
before ordinary risk, capacity, or rebalance logic observes legacy state.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Mapping

from shared.execution.execution_lineage import (
    ASHARE_AUTHORITY_GENERATION,
    ASHARE_CAPITAL_AUTHORITY_ID,
    ASHARE_EXECUTION_LINEAGE_ID,
)

CAPITAL_POSITION_SOURCE_MISMATCH = "capital_position_source_mismatch"
POSITION_AUTHORITY_SCHEMA_VERSION = "ashare-capital-position-authority.v2"
_HEX64 = re.compile(r"^[a-f0-9]{64}$")
_ASHARE_SYMBOL = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _compact_date(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits if len(digits) == 8 else ""


def _canonical_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    suffixes = {
        ".XSHG": ".SH",
        ".XSHE": ".SZ",
        ".XBSE": ".BJ",
        ".BSE": ".BJ",
    }
    for source_suffix, target_suffix in suffixes.items():
        if symbol.endswith(source_suffix):
            symbol = f"{symbol[: -len(source_suffix)]}{target_suffix}"
            break
    return symbol if _ASHARE_SYMBOL.fullmatch(symbol) else ""


def _strict_integer(value: Any, *, positive: bool = False) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not number.is_integer():
        return None
    integer = int(number)
    if integer < 0 or (positive and integer <= 0):
        return None
    return integer


def _strict_count(value: Any, *, positive: bool = False) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or (positive and value <= 0):
        return None
    return value


def _position_quantity(row: Mapping[str, Any]) -> Any:
    for key in ("quantity", "position_qty", "current_qty", "running_quantity"):
        if key in row:
            return row.get(key)
    return None


def normalize_ashare_positions(
    value: Any,
) -> tuple[list[dict[str, Any]] | None, dict[str, dict[str, Any]], str]:
    """Return strict canonical positive positions and descriptive source rows."""

    raw_rows: list[tuple[Any, Any, dict[str, Any]]] = []
    if isinstance(value, Mapping):
        for raw_symbol, raw_position in value.items():
            if isinstance(raw_position, Mapping):
                detail = dict(raw_position)
                symbol = (
                    detail.get("ts_code")
                    or detail.get("symbol")
                    or detail.get("risk_unit_key")
                    or raw_symbol
                )
                quantity = _position_quantity(detail)
            else:
                detail = {"ts_code": raw_symbol, "quantity": raw_position}
                symbol = raw_symbol
                quantity = raw_position
            raw_rows.append((symbol, quantity, detail))
    elif isinstance(value, list):
        for raw_position in value:
            if not isinstance(raw_position, Mapping):
                return None, {}, "position_row_invalid"
            detail = dict(raw_position)
            symbol = (
                detail.get("ts_code")
                or detail.get("symbol")
                or detail.get("risk_unit_key")
            )
            raw_rows.append((symbol, _position_quantity(detail), detail))
    else:
        return None, {}, "positions_container_invalid"

    normalized: list[dict[str, Any]] = []
    details: dict[str, dict[str, Any]] = {}
    seen_symbols: set[str] = set()
    for raw_symbol, raw_quantity, detail in raw_rows:
        symbol = _canonical_symbol(raw_symbol)
        if not symbol:
            return None, {}, f"position_symbol_invalid:{raw_symbol}"
        if symbol in seen_symbols:
            return None, {}, f"duplicate_position_symbol:{symbol}"
        seen_symbols.add(symbol)
        quantity = _strict_integer(raw_quantity)
        if quantity is None:
            return None, {}, f"position_quantity_invalid:{symbol}"
        if quantity == 0:
            continue
        normalized.append({"ts_code": symbol, "quantity": quantity})
        details[symbol] = detail
    normalized.sort(key=lambda row: row["ts_code"])
    return normalized, details, "approved"


def ashare_positions_fingerprint(value: Any) -> str:
    normalized, _, reason = normalize_ashare_positions(value)
    if normalized is None:
        raise ValueError(reason)
    return canonical_sha256(normalized)


def _blocked(reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": "blocked", "reason": reason, **extra}


def _normalize_current_authority_scope(
    value: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if value is None:
        # Legacy compatibility only.  Current composition supplies the
        # authority envelope observed from the canonical ledger so a lineage
        # rotation does not require a source edit.
        return {
            "capital_authority_id": ASHARE_CAPITAL_AUTHORITY_ID,
            "authority_generation": ASHARE_AUTHORITY_GENERATION,
            "execution_lineage_id": ASHARE_EXECUTION_LINEAGE_ID,
        }
    if not isinstance(value, Mapping):
        return None
    authority_id = str(value.get("capital_authority_id") or "").strip()
    generation = value.get("authority_generation")
    lineage_id = str(value.get("execution_lineage_id") or "").strip()
    if (
        authority_id != ASHARE_CAPITAL_AUTHORITY_ID
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation <= 0
        or not lineage_id
    ):
        return None
    return {
        "capital_authority_id": authority_id,
        "authority_generation": generation,
        "execution_lineage_id": lineage_id,
    }


def ashare_capital_state_audit(
    capital_state: Any,
    authority_view: Mapping[str, Any],
    *,
    source_name: str,
) -> dict[str, Any]:
    """Preserve raw authority identity even when strict validation blocks."""

    snapshot = capital_state if isinstance(capital_state, Mapping) else {}
    return {
        "source_name": source_name,
        "source": str(snapshot.get("source") or ""),
        "status": str(authority_view.get("status") or "blocked"),
        "reason": str(authority_view.get("reason") or ""),
        "source_sha256": str(
            authority_view.get("capital_state_sha256")
            or canonical_sha256(capital_state)
        ),
        "authority_view_checksum": str(
            authority_view.get("authority_view_checksum") or ""
        ),
        "authority_id": str(
            authority_view.get("authority_id") or snapshot.get("authority_id") or ""
        ),
        "authority_generation": authority_view.get(
            "authority_generation", snapshot.get("authority_generation")
        ),
        "authority_checksum": str(
            authority_view.get("authority_checksum")
            or snapshot.get("event_checksum")
            or ""
        ),
        "execution_lineage_id": str(
            authority_view.get("execution_lineage_id")
            or snapshot.get("execution_lineage_id")
            or ""
        ),
        "trade_date": _compact_date(
            authority_view.get("trade_date") or snapshot.get("trade_date")
        ),
    }


def build_ashare_capital_position_authority_view(
    capital_state: Any,
    trade_date: str,
    *,
    current_authority_scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and freeze one replayable market-capital position view."""

    if not isinstance(capital_state, Mapping):
        return _blocked("ashare_capital_unavailable")
    expected_authority = _normalize_current_authority_scope(current_authority_scope)
    if expected_authority is None:
        return _blocked("ashare_current_authority_scope_invalid")
    expected_date = _compact_date(trade_date)
    if not expected_date:
        return _blocked("ashare_capital_trade_date_invalid")
    authority_id = str(capital_state.get("authority_id") or "")
    generation = capital_state.get("authority_generation")
    lineage_id = str(capital_state.get("execution_lineage_id") or "")
    if str(capital_state.get("source") or "") != "market_capital_ledger":
        return _blocked("ashare_capital_source_invalid")
    if authority_id != expected_authority["capital_authority_id"]:
        return _blocked("ashare_capital_authority_mismatch")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation != expected_authority["authority_generation"]
    ):
        return _blocked("ashare_capital_generation_mismatch")
    if lineage_id != expected_authority["execution_lineage_id"]:
        return _blocked("ashare_capital_execution_lineage_mismatch")
    if str(capital_state.get("market") or "").lower() != "ashare":
        return _blocked("ashare_capital_market_mismatch")
    if str(capital_state.get("currency") or "") != "CNY":
        return _blocked("ashare_capital_currency_mismatch")
    if _compact_date(capital_state.get("trade_date")) != expected_date:
        return _blocked("ashare_capital_trade_date_mismatch")
    if (
        capital_state.get("fresh") is not True
        or capital_state.get("reconciled") is not True
    ):
        return _blocked("ashare_capital_not_reconciled_for_trade_date")
    if capital_state.get("real_trading_enabled") is not False:
        return _blocked("ashare_capital_real_trading_flag_invalid")
    if not str(capital_state.get("event_id") or "").strip():
        return _blocked("ashare_capital_event_missing")

    event_checksum = str(capital_state.get("event_checksum") or "").lower()
    checksum_status = str(capital_state.get("checksum_status") or "").lower()
    checksum_last = str(capital_state.get("checksum_last") or "").lower()
    checksum_event_count = _strict_count(
        capital_state.get("checksum_event_count"), positive=True
    )
    if (
        checksum_status != "valid"
        or not _HEX64.fullmatch(event_checksum)
        or not _HEX64.fullmatch(checksum_last)
        or checksum_last != event_checksum
        or checksum_event_count is None
    ):
        return _blocked("ashare_capital_checksum_invalid")

    if "positions_quantity_by_risk_unit" not in capital_state:
        return _blocked("ashare_capital_position_state_incomplete")
    positions_value = capital_state.get("positions_quantity_by_risk_unit")
    if not isinstance(positions_value, Mapping):
        return _blocked("ashare_capital_position_state_invalid")
    positions, _, normalization_reason = normalize_ashare_positions(positions_value)
    if positions is None:
        return _blocked(
            "ashare_capital_position_state_invalid", detail=normalization_reason
        )
    position_count = len(positions)
    declared_count = _strict_count(capital_state.get("position_count"))
    if declared_count is None or declared_count != position_count:
        return _blocked("ashare_capital_position_count_invalid")
    positions_fingerprint = canonical_sha256(positions)
    declared_fingerprint = str(capital_state.get("positions_fingerprint") or "").lower()
    if (
        not _HEX64.fullmatch(declared_fingerprint)
        or declared_fingerprint != positions_fingerprint
    ):
        return _blocked("ashare_capital_positions_fingerprint_invalid")

    replay_key = {
        "schema_version": POSITION_AUTHORITY_SCHEMA_VERSION,
        "market": "ashare",
        "trade_date": expected_date,
        "authority_id": authority_id,
        "authority_generation": generation,
        "execution_lineage_id": lineage_id,
        "authority_checksum": event_checksum,
        "checksum_status": checksum_status,
        "checksum_event_count": checksum_event_count,
        "checksum_last": checksum_last,
        "positions_fingerprint": positions_fingerprint,
        "position_count": position_count,
    }
    return {
        "status": "verified",
        "reason": "approved",
        **replay_key,
        "authority_view_checksum": canonical_sha256(replay_key),
        "authority_event_id": str(capital_state.get("event_id") or ""),
        "authority_event_checksum": event_checksum,
        "capital_state_sha256": canonical_sha256(dict(capital_state)),
        "position_evidence": "positions_quantity_by_risk_unit",
        "positions": positions,
        "real_trading_enabled": False,
    }


def _source_metadata(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    generation = snapshot.get("authority_generation")
    return {
        "authority_id": str(snapshot.get("authority_id") or ""),
        "authority_generation": generation,
        "execution_lineage_id": str(snapshot.get("execution_lineage_id") or ""),
        "authority_checksum": str(snapshot.get("authority_checksum") or "").lower(),
        "trade_date": _compact_date(snapshot.get("trade_date")),
        "position_count": _strict_count(snapshot.get("position_count")),
        "positions_fingerprint": str(
            snapshot.get("positions_fingerprint") or ""
        ).lower(),
        "source": str(snapshot.get("source") or ""),
        "position_source_status": str(
            snapshot.get("position_source_status") or ""
        ).lower(),
    }


def _concurrent_authority_mismatch(
    authority_before: Mapping[str, Any],
    authority_after: Mapping[str, Any],
    final_capital_state: Any,
) -> dict[str, Any]:
    before_audit = ashare_capital_state_audit(
        {}, authority_before, source_name="market_capital_before"
    )
    after_audit = ashare_capital_state_audit(
        final_capital_state,
        authority_after,
        source_name="market_capital_after",
    )
    return {
        **dict(authority_before),
        "status": "blocked",
        "reason": CAPITAL_POSITION_SOURCE_MISMATCH,
        "source_audit": [before_audit, after_audit],
        "mismatches": [
            {
                "source_name": "market_capital_after",
                "fields": ["concurrent_authority_read_binding"],
                "source_sha256": after_audit["source_sha256"],
                "execution_lineage_id": after_audit["execution_lineage_id"],
            }
        ],
        "positions": [],
    }


def reconcile_ashare_position_sources(
    capital_state: Any,
    trade_date: str,
    *,
    sources: Mapping[str, Any],
    preferred_source: str,
    final_capital_state: Any | None = None,
    current_authority_scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify a stable authority replay and every supplied position source."""

    authority = build_ashare_capital_position_authority_view(
        capital_state,
        trade_date,
        current_authority_scope=current_authority_scope,
    )
    if authority.get("status") != "verified":
        return {
            **authority,
            "source_audit": [
                ashare_capital_state_audit(
                    capital_state,
                    authority,
                    source_name="market_capital_before",
                )
            ],
            "mismatches": [],
            "positions": [],
        }
    authority_before = authority
    if final_capital_state is not None:
        authority_after = build_ashare_capital_position_authority_view(
            final_capital_state,
            trade_date,
            current_authority_scope=current_authority_scope,
        )
        if (
            authority_after.get("status") != "verified"
            or authority_after.get("authority_view_checksum")
            != authority.get("authority_view_checksum")
            or authority_after.get("capital_state_sha256")
            != authority.get("capital_state_sha256")
        ):
            return _concurrent_authority_mismatch(
                authority, authority_after, final_capital_state
            )
        authority = authority_after

    source_audit: list[dict[str, Any]] = [
        ashare_capital_state_audit(
            capital_state,
            authority_before,
            source_name="market_capital_before",
        )
    ]
    if final_capital_state is not None:
        source_audit.append(
            ashare_capital_state_audit(
                final_capital_state,
                authority,
                source_name="market_capital_after",
            )
        )
    mismatches: list[dict[str, Any]] = []
    details_by_source: dict[str, dict[str, dict[str, Any]]] = {}
    if not sources or preferred_source not in sources:
        return {
            **authority,
            "status": "blocked",
            "reason": CAPITAL_POSITION_SOURCE_MISMATCH,
            "source_audit": source_audit,
            "mismatches": [
                {
                    "source_name": str(preferred_source),
                    "fields": ["source_missing"],
                    "source_sha256": "",
                    "execution_lineage_id": "",
                }
            ],
            "positions": [],
        }

    for source_name, raw_snapshot in sources.items():
        snapshot = raw_snapshot if isinstance(raw_snapshot, Mapping) else {}
        metadata = _source_metadata(snapshot)
        normalized, details, normalization_reason = normalize_ashare_positions(
            snapshot.get("positions") if "positions" in snapshot else None
        )
        audit: dict[str, Any] = {
            "source_name": str(source_name),
            "source": metadata["source"],
            "source_sha256": canonical_sha256(raw_snapshot),
            "authority_id": metadata["authority_id"],
            "authority_generation": metadata["authority_generation"],
            "execution_lineage_id": metadata["execution_lineage_id"],
            "authority_checksum": metadata["authority_checksum"],
            "trade_date": metadata["trade_date"],
            "declared_position_count": metadata["position_count"],
            "declared_positions_fingerprint": metadata["positions_fingerprint"],
            "normalization_reason": normalization_reason,
            "position_source_status": metadata["position_source_status"],
        }
        fields: list[str] = []
        if normalized is None:
            position_count = None
            fingerprint = ""
            fields.append("positions")
        else:
            position_count = len(normalized)
            fingerprint = canonical_sha256(normalized)
            details_by_source[str(source_name)] = details
            if position_count != authority["position_count"]:
                fields.append("position_count")
            if fingerprint != authority["positions_fingerprint"]:
                fields.append("positions_fingerprint")
            if metadata["position_count"] is None:
                fields.append("position_count_missing")
            elif metadata["position_count"] != position_count:
                fields.append("declared_position_count")
            if not _HEX64.fullmatch(metadata["positions_fingerprint"]):
                fields.append("positions_fingerprint_missing")
            elif metadata["positions_fingerprint"] != fingerprint:
                fields.append("declared_positions_fingerprint")
        audit.update(
            {
                "position_count": position_count,
                "positions_fingerprint": fingerprint,
            }
        )

        expected_identity = {
            "authority_id": authority["authority_id"],
            "authority_generation": authority["authority_generation"],
            "execution_lineage_id": authority["execution_lineage_id"],
            "authority_checksum": authority["authority_checksum"],
            "trade_date": authority["trade_date"],
        }
        for field, expected in expected_identity.items():
            actual = metadata[field]
            if actual is None or actual == "":
                fields.append(f"{field}_missing")
            elif actual != expected:
                fields.append(field)
        if isinstance(metadata["authority_generation"], bool) or not isinstance(
            metadata["authority_generation"], int
        ):
            fields.append("authority_generation")
        if not metadata["source"]:
            fields.append("source_missing")
        if metadata["position_source_status"] != "ready":
            fields.append("position_source_status")
        if fields:
            audit["status"] = "mismatch"
            mismatches.append(
                {
                    "source_name": str(source_name),
                    "fields": sorted(set(fields)),
                    "source_sha256": audit["source_sha256"],
                    "execution_lineage_id": metadata["execution_lineage_id"],
                }
            )
        else:
            audit["status"] = "verified"
        source_audit.append(audit)

    if mismatches:
        return {
            **authority,
            "status": "blocked",
            "reason": CAPITAL_POSITION_SOURCE_MISMATCH,
            "source_audit": source_audit,
            "mismatches": mismatches,
            "positions": [],
        }

    preferred_details = details_by_source.get(str(preferred_source), {})
    resolved_positions: list[dict[str, Any]] = []
    for canonical in authority["positions"]:
        detail = dict(preferred_details.get(canonical["ts_code"], {}))
        detail.update(canonical)
        detail.update(
            {
                "capital_authority_id": authority["authority_id"],
                "authority_generation": authority["authority_generation"],
                "execution_lineage_id": authority["execution_lineage_id"],
                "capital_authority_checksum": authority["authority_checksum"],
                "positions_fingerprint": authority["positions_fingerprint"],
                "position_authority_verified": True,
                "capital_layer": "simulated",
                "account_type": "simulated",
            }
        )
        resolved_positions.append(detail)
    return {
        **authority,
        "status": "verified",
        "reason": "approved",
        "source_audit": source_audit,
        "mismatches": [],
        "positions": resolved_positions,
    }


__all__ = [
    "CAPITAL_POSITION_SOURCE_MISMATCH",
    "POSITION_AUTHORITY_SCHEMA_VERSION",
    "ashare_capital_state_audit",
    "ashare_positions_fingerprint",
    "build_ashare_capital_position_authority_view",
    "canonical_sha256",
    "normalize_ashare_positions",
    "reconcile_ashare_position_sources",
]
