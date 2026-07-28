"""Bounded fixture qualification for the network-closed Crypto simulator."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Mapping

from .contracts import (
    ALLOWED_SOURCE_KINDS,
    ALLOWED_SYMBOLS,
    BAR_KEYS,
    EXECUTABLE_QUOTE_KEYS,
    FIXTURE_CONTRACT,
    FIXTURE_TOP_LEVEL_KEYS,
    FORBIDDEN_LLM_AUTHORITY_KEYS,
    INSTRUMENT_KEYS,
    LLM_EVIDENCE_KEYS,
    METADATA_KEYS,
    WIRE_CONTRACT,
    CryptoEvidenceError,
    CryptoSafetyError,
    ExecutableSpotQuote,
    QualifiedFixtureEvidence,
    SpotBar5m,
    SpotInstrumentRules,
    _assert_simulation_only,
    _aware_utc,
    _canonical_value,
    _decimal,
    _is_step_aligned,
    _lineage_has_content,
    _nested_forbidden_keys,
    _require_exact_keys,
    _scan_forbidden_payload,
    _sha256,
    _state,
)


def _parse_rules(payload: Any, *, symbol: str) -> SpotInstrumentRules:
    payload = _require_exact_keys(
        payload,
        INSTRUMENT_KEYS,
        scope="instrument_rules",
    )
    return SpotInstrumentRules(
        symbol=symbol,
        base_asset=str(payload.get("base_asset") or "").strip().upper(),
        quote_asset=str(payload.get("quote_asset") or "").strip().upper(),
        price_tick=_decimal(
            payload.get("price_tick"), field_name="price_tick", positive=True
        ),
        quantity_step=_decimal(
            payload.get("quantity_step"), field_name="quantity_step", positive=True
        ),
        min_quantity=_decimal(
            payload.get("min_quantity"), field_name="min_quantity", positive=True
        ),
        min_notional=_decimal(
            payload.get("min_notional"), field_name="min_notional", positive=True
        ),
    )


def _parse_bar(payload: Any, *, symbol: str, rules: SpotInstrumentRules) -> SpotBar5m:
    payload = _require_exact_keys(payload, BAR_KEYS, scope="bar")
    bar_symbol = str(payload.get("symbol") or "").strip().upper()
    if bar_symbol != symbol:
        raise CryptoEvidenceError("bar_symbol_mismatch")
    open_time = _aware_utc(payload.get("open_time"), field_name="bar_open_time")
    close_time = _aware_utc(payload.get("close_time"), field_name="bar_close_time")
    if open_time.minute % 5 != 0 or close_time != open_time + timedelta(minutes=5):
        raise CryptoEvidenceError("bar_5m_alignment_invalid")
    if payload.get("closed") is not True:
        raise CryptoEvidenceError("bar_must_be_closed")
    open_price = _decimal(payload.get("open"), field_name="bar_open", positive=True)
    high = _decimal(payload.get("high"), field_name="bar_high", positive=True)
    low = _decimal(payload.get("low"), field_name="bar_low", positive=True)
    close = _decimal(payload.get("close"), field_name="bar_close", positive=True)
    volume = _decimal(payload.get("volume"), field_name="bar_volume", nonnegative=True)
    if high < max(open_price, close) or low > min(open_price, close) or high < low:
        raise CryptoEvidenceError("bar_ohlc_invalid")
    for field_name, price in (
        ("bar_open", open_price),
        ("bar_high", high),
        ("bar_low", low),
        ("bar_close", close),
    ):
        if not _is_step_aligned(price, rules.price_tick):
            raise CryptoEvidenceError(f"{field_name}_off_tick")
    return SpotBar5m(
        symbol=symbol,
        open_time=open_time,
        close_time=close_time,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        closed=True,
    )


def _parse_executable_quote(
    payload: Any,
    *,
    symbol: str,
    rules: SpotInstrumentRules,
    decision_observed_at,
) -> ExecutableSpotQuote:
    payload = _require_exact_keys(
        payload,
        EXECUTABLE_QUOTE_KEYS,
        scope="next_executable_quote",
    )
    quote_symbol = str(payload.get("symbol") or "").strip().upper()
    if quote_symbol != symbol:
        raise CryptoEvidenceError("execution_quote_symbol_mismatch")
    observed_at = _aware_utc(
        payload.get("observed_at"),
        field_name="execution_quote_observed_at",
        require_minute_alignment=False,
    )
    if observed_at < decision_observed_at:
        raise CryptoEvidenceError("execution_quote_precedes_decision_observation")
    if observed_at - decision_observed_at > timedelta(minutes=5):
        raise CryptoEvidenceError("execution_quote_lag_exceeded")
    bid = _decimal(payload.get("bid"), field_name="execution_quote_bid", positive=True)
    ask = _decimal(payload.get("ask"), field_name="execution_quote_ask", positive=True)
    for field_name, price in (("bid", bid), ("ask", ask)):
        if not _is_step_aligned(price, rules.price_tick):
            raise CryptoEvidenceError(f"execution_quote_{field_name}_off_tick")
    return ExecutableSpotQuote(
        symbol=symbol,
        observed_at=observed_at,
        bid=bid,
        ask=ask,
    )


def _validate_llm_sidecar(
    value: Any,
) -> tuple[str | None, bool, Mapping[str, Any] | None]:
    if value is None:
        return None, False, None
    if isinstance(value, Mapping):
        forbidden = sorted(
            set(_nested_forbidden_keys(value, FORBIDDEN_LLM_AUTHORITY_KEYS))
        )
        if forbidden:
            raise CryptoSafetyError("llm_evidence_contains_decision_authority_fields")
    value = _require_exact_keys(
        value,
        LLM_EVIDENCE_KEYS,
        scope="llm_evidence",
    )
    if str(value.get("mode") or "").strip().lower() != "offline_fixture":
        raise CryptoSafetyError("llm_evidence_must_be_offline_fixture")
    if str(value.get("authority") or "").strip().lower() != "none":
        raise CryptoSafetyError("llm_evidence_authority_must_be_none")
    if value.get("network_used") is not False:
        raise CryptoSafetyError("llm_evidence_network_used_must_be_false")
    if type(value.get("evidence_id")) is not str or not value["evidence_id"].strip():
        raise CryptoEvidenceError("llm_evidence_id_must_be_string")
    if type(value.get("summary")) is not str:
        raise CryptoEvidenceError("llm_summary_must_be_string")
    canonical = _canonical_value(value)
    return _sha256(canonical), True, canonical


def qualify_fixture_evidence(payload: Mapping[str, Any]) -> QualifiedFixtureEvidence:
    """Qualify one fixture without inventing a TradingDatas dataset identifier."""

    _assert_simulation_only()
    if not isinstance(payload, Mapping):
        raise CryptoEvidenceError("fixture_must_be_object")
    _scan_forbidden_payload(payload)
    _require_exact_keys(payload, FIXTURE_TOP_LEVEL_KEYS, scope="fixture")
    if str(payload.get("contract") or "") != FIXTURE_CONTRACT:
        raise CryptoEvidenceError("fixture_contract_invalid")
    fixture_id = str(payload.get("fixture_id") or "").strip()
    if not fixture_id:
        raise CryptoEvidenceError("fixture_id_required")
    source_kind = str(payload.get("source_kind") or "").strip().lower()
    if source_kind not in ALLOWED_SOURCE_KINDS:
        raise CryptoEvidenceError("fixture_source_kind_not_allowed")
    if _canonical_value(payload.get("wire_contract")) != WIRE_CONTRACT:
        raise CryptoEvidenceError(
            "tradingdatas_wire_contract_must_be_catalog_query_only"
        )
    symbol = str(payload.get("symbol") or "").strip().upper()
    if symbol not in ALLOWED_SYMBOLS:
        raise CryptoEvidenceError("fixture_symbol_not_allowed")
    as_of = _aware_utc(payload.get("as_of"), field_name="fixture_as_of")

    metadata = _require_exact_keys(
        payload.get("metadata"),
        METADATA_KEYS,
        scope="fixture_metadata",
    )
    _require_exact_keys(
        metadata.get("freshness"),
        frozenset({"state"}),
        scope="fixture_freshness",
    )
    _require_exact_keys(
        metadata.get("quality"),
        frozenset({"state"}),
        scope="fixture_quality",
    )
    if (
        _state(metadata.get("state")) != "ready"
        or metadata.get("degraded") is not False
    ):
        raise CryptoEvidenceError("fixture_metadata_not_ready")
    if _state(metadata.get("freshness")) not in {"fresh", "current", "pass", "passed"}:
        raise CryptoEvidenceError("fixture_freshness_failed")
    if _state(metadata.get("quality")) not in {"pass", "passed", "good"}:
        raise CryptoEvidenceError("fixture_quality_failed")
    receipt_id = str(metadata.get("receipt_id") or "").strip()
    if not receipt_id:
        raise CryptoEvidenceError("fixture_receipt_id_required")
    lineage = metadata.get("lineage")
    _require_exact_keys(
        lineage,
        frozenset({"source", "fixture_id"}),
        scope="fixture_lineage",
    )
    if not _lineage_has_content(lineage):
        raise CryptoEvidenceError("fixture_lineage_required")
    if (
        lineage.get("fixture_id") != fixture_id
        or lineage.get("source") != f"checked_in_{source_kind}"
    ):
        raise CryptoEvidenceError("fixture_lineage_binding_invalid")
    observed_at = _aware_utc(
        metadata.get("observed_at"),
        field_name="observed_at",
        require_minute_alignment=False,
    )
    data_through = _aware_utc(metadata.get("data_through"), field_name="data_through")
    if data_through > observed_at or data_through > as_of:
        raise CryptoEvidenceError("fixture_timestamp_order_invalid")
    if observed_at - data_through >= timedelta(minutes=6):
        raise CryptoEvidenceError("fixture_closed_bar_observation_lag_exceeded")

    rules = _parse_rules(payload.get("instrument"), symbol=symbol)
    raw_bars = payload.get("bars_5m")
    if not isinstance(raw_bars, list) or len(raw_bars) < 12:
        raise CryptoEvidenceError("fixture_requires_at_least_twelve_closed_5m_bars")
    bars = tuple(_parse_bar(row, symbol=symbol, rules=rules) for row in raw_bars)
    for previous, current in zip(bars, bars[1:]):
        if current.open_time != previous.close_time:
            raise CryptoEvidenceError("fixture_5m_bar_gap_or_overlap")
    if bars[-1].close_time != data_through or data_through != as_of:
        raise CryptoEvidenceError("fixture_last_closed_bar_must_bind_execution_slot")

    next_executable_quote = _parse_executable_quote(
        payload.get("next_executable_quote"),
        symbol=symbol,
        rules=rules,
        decision_observed_at=observed_at,
    )
    llm_sha, llm_present, llm_payload = _validate_llm_sidecar(
        payload.get("llm_evidence")
    )
    market_material = dict(payload)
    market_material.pop("llm_evidence", None)
    return QualifiedFixtureEvidence(
        fixture_id=fixture_id,
        source_kind=source_kind,
        symbol=symbol,
        as_of=as_of,
        receipt_id=receipt_id,
        observed_at=observed_at,
        data_through=data_through,
        lineage=_canonical_value(lineage),
        rules=rules,
        bars_5m=bars,
        next_executable_quote=next_executable_quote,
        market_evidence_sha256=_sha256(market_material),
        llm_evidence_sha256=llm_sha,
        llm_evidence_present=llm_present,
        llm_evidence_payload=llm_payload,
    )
