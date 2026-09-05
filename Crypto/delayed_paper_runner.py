"""One local, 24x7 Crypto delayed-paper cycle over a validated 5m snapshot.

The runner is orchestration only.  It persists the accepted market observation
before any capital side effect, converts each symbol into the already-frozen
fixture contract, and calls :func:`run_fixture_auto_sim` as the sole capital
writer.  The thirteenth *closed* bar is never used by the Champion: only its
open is retained as an explicitly counterfactual next-bar quote, available
after that bar closes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
from pathlib import Path
from typing import Any, Callable, Mapping

from Crypto.fixture_auto_sim import (
    FROZEN_CHAMPION,
    WIRE_CONTRACT,
    evaluate_frozen_champion,
    qualify_fixture_evidence,
    run_fixture_auto_sim,
)
from Crypto.fixture_sim.contracts import (
    ALLOWED_SYMBOLS,
    FIXTURE_CONTRACT,
    QualifiedFixtureEvidence,
    TimeframeDecision,
    _assert_simulation_only,
    _validate_json_tree,
)
from Crypto.five_minute_data import (
    FIVE_MINUTES,
    REQUIRED_WINDOW_BARS,
    CryptoFiveMinuteDataError,
    CryptoFiveMinuteSnapshot,
)

from .delayed_paper_ledger import (
    DECISION_LEDGER_CONTRACT,
    OBSERVATION_CONTRACT,
    CryptoDelayedPaperLedgerError,
    CryptoDelayedPaperObservationStore,
    _canonical_value,
    _non_authority_fields,
    _sha256,
)


RUNNER_CONTRACT = "tradingagent.crypto.delayed_paper_runner.v1"
COUNTERFACTUAL_CONTRACT = "tradingagent.crypto.next_closed_bar_open.v1"
FROZEN_SYMBOLS = tuple(sorted(ALLOWED_SYMBOLS))
MAX_POSITIONS = 2
HALF_SPREAD_BPS = Decimal("1")
EXPECTED_DECISION_BARS = 12


def _utc(value: Any, reason: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CryptoFiveMinuteDataError(reason) from exc
    else:
        raise CryptoFiveMinuteDataError(reason)
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.utcoffset() != timedelta(0)
    ):
        raise CryptoFiveMinuteDataError("crypto_5m_timestamp_must_be_utc")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _ceil_minute(value: datetime) -> datetime:
    aligned = value.replace(second=0, microsecond=0)
    return aligned if aligned == value else aligned + timedelta(minutes=1)


def _decimal(value: Any, reason: str, *, nonnegative: bool = False) -> Decimal:
    if isinstance(value, (bool, float)) or value in (None, ""):
        raise CryptoFiveMinuteDataError(reason)
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CryptoFiveMinuteDataError(reason) from exc
    if (
        not parsed.is_finite()
        or (nonnegative and parsed < 0)
        or (not nonnegative and parsed <= 0)
    ):
        raise CryptoFiveMinuteDataError(reason)
    return parsed


def _floor_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _ceil_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


def _logical_close(bar: Any, open_time: datetime) -> tuple[datetime, datetime]:
    source_close = _utc(
        getattr(bar, "source_close_time", getattr(bar, "close_time", None)),
        "crypto_5m_close_time_invalid",
    )
    logical = _utc(
        getattr(bar, "close_time", open_time + FIVE_MINUTES),
        "crypto_5m_close_time_invalid",
    )
    expected = open_time + FIVE_MINUTES
    if logical == expected:
        pass
    elif logical == expected - timedelta(milliseconds=1):
        source_close = logical
        logical = expected
    else:
        raise CryptoFiveMinuteDataError("crypto_5m_bar_duration_invalid")
    if source_close not in {expected, expected - timedelta(milliseconds=1)}:
        raise CryptoFiveMinuteDataError("crypto_5m_source_close_time_invalid")
    return logical, source_close


def _normalized_bar(bar: Any, *, symbol: str) -> dict[str, Any]:
    if getattr(bar, "symbol", None) != symbol:
        raise CryptoFiveMinuteDataError("crypto_5m_symbol_invalid")
    if getattr(bar, "closed", True) is not True:
        raise CryptoFiveMinuteDataError("crypto_5m_bar_not_closed")
    open_time = _utc(getattr(bar, "open_time", None), "crypto_5m_open_time_invalid")
    if open_time.minute % 5 != 0 or open_time.second != 0 or open_time.microsecond != 0:
        raise CryptoFiveMinuteDataError("crypto_5m_timestamp_alignment_invalid")
    close_time, source_close_time = _logical_close(bar, open_time)
    prices = {
        name: _decimal(getattr(bar, name, None), f"crypto_5m_{name}_invalid")
        for name in ("open", "high", "low", "close")
    }
    if (
        prices["high"] < max(prices["open"], prices["close"])
        or prices["low"] > min(prices["open"], prices["close"])
        or prices["low"] > prices["high"]
    ):
        raise CryptoFiveMinuteDataError("crypto_5m_ohlc_invalid")
    volume = _decimal(
        getattr(bar, "volume", getattr(bar, "base_volume", None)),
        "crypto_5m_volume_invalid",
        nonnegative=True,
    )
    quote_volume_raw = getattr(bar, "quote_volume", None)
    quote_volume = (
        _decimal(
            quote_volume_raw,
            "crypto_5m_quote_volume_invalid",
            nonnegative=True,
        )
        if quote_volume_raw is not None
        else None
    )
    trade_count_raw = getattr(bar, "trade_count", None)
    if trade_count_raw is not None and (
        isinstance(trade_count_raw, bool)
        or not isinstance(trade_count_raw, int)
        or trade_count_raw < 0
    ):
        raise CryptoFiveMinuteDataError("crypto_5m_trade_count_invalid")
    return {
        "symbol": symbol,
        "open_time": _iso(open_time),
        "close_time": _iso(close_time),
        "source_close_time": _iso(source_close_time),
        "open": format(prices["open"], "f"),
        "high": format(prices["high"], "f"),
        "low": format(prices["low"], "f"),
        "close": format(prices["close"], "f"),
        "volume": format(volume, "f"),
        "quote_volume": (
            format(quote_volume, "f") if quote_volume is not None else None
        ),
        "trade_count": trade_count_raw,
        "closed": True,
        "source_row_sha256": getattr(bar, "source_row_sha256", None),
        "source_receipt_id": getattr(bar, "source_receipt_id", None),
        "source_lineage_sha256": getattr(bar, "source_lineage_sha256", None),
        "data_through": _canonical_value(getattr(bar, "data_through", None)),
        "observed_at": _canonical_value(getattr(bar, "observed_at", None)),
    }


def _normalized_rules(rule: Any, *, symbol: str) -> dict[str, Any]:
    if getattr(rule, "symbol", None) != symbol:
        raise CryptoFiveMinuteDataError("crypto_5m_instrument_rules_incomplete")
    values = {
        "symbol": symbol,
        "base_asset": str(getattr(rule, "base_asset", "")).strip().upper(),
        "quote_asset": str(getattr(rule, "quote_asset", "")).strip().upper(),
        "price_tick": format(
            _decimal(
                getattr(rule, "price_tick", None),
                "crypto_5m_rule_price_tick_invalid",
            ),
            "f",
        ),
        "quantity_step": format(
            _decimal(
                getattr(rule, "quantity_step", None),
                "crypto_5m_rule_quantity_step_invalid",
            ),
            "f",
        ),
        "min_quantity": format(
            _decimal(
                getattr(rule, "min_quantity", None),
                "crypto_5m_rule_min_quantity_invalid",
            ),
            "f",
        ),
        "min_notional": format(
            _decimal(
                getattr(rule, "min_notional", None),
                "crypto_5m_rule_min_notional_invalid",
            ),
            "f",
        ),
    }
    if (
        not values["base_asset"]
        or values["quote_asset"] != "USDT"
        or symbol != f"{values['base_asset']}{values['quote_asset']}"
    ):
        raise CryptoFiveMinuteDataError("crypto_5m_instrument_rule_invalid")
    return values


def _snapshot_to_observation(snapshot: Any) -> dict[str, Any]:
    if getattr(snapshot, "same_observation", None) is not True:
        raise CryptoFiveMinuteDataError("crypto_5m_same_observation_mismatch")
    symbols: dict[str, Any] = {}
    for symbol in FROZEN_SYMBOLS:
        bars = tuple(snapshot.bars_for(symbol))
        if len(bars) != REQUIRED_WINDOW_BARS:
            raise CryptoFiveMinuteDataError("crypto_5m_window_incomplete")
        normalized = [_normalized_bar(bar, symbol=symbol) for bar in bars]
        for previous, current in zip(normalized, normalized[1:]):
            if current["open_time"] != previous["close_time"]:
                raise CryptoFiveMinuteDataError("crypto_5m_bar_order_or_gap_invalid")
        rules = _normalized_rules(snapshot.rules_for(symbol), symbol=symbol)
        price_tick = Decimal(rules["price_tick"])
        for bar in normalized:
            for field_name in ("open", "high", "low", "close"):
                if Decimal(bar[field_name]) % price_tick != Decimal("0"):
                    raise CryptoFiveMinuteDataError("crypto_5m_price_off_tick")
        symbols[symbol] = {
            "bars": normalized,
            "instrument_rules": rules,
        }

    source_bindings = snapshot.source_bindings()
    if not isinstance(source_bindings, Mapping) or not source_bindings:
        raise CryptoFiveMinuteDataError("crypto_5m_source_bindings_missing")
    _validate_json_tree(
        source_bindings,
        path="crypto_5m_source_bindings",
        external=True,
    )
    source_observed_at: list[datetime] = []
    for proof in source_bindings.values():
        if not isinstance(proof, Mapping):
            raise CryptoFiveMinuteDataError("crypto_5m_source_bindings_invalid")
        source_observed_at.append(
            _utc(
                proof.get("observed_at"),
                "crypto_5m_source_observed_at_invalid",
            )
        )
    evidence_available_at = max(source_observed_at)
    market_slots = {
        item["bars"][EXPECTED_DECISION_BARS]["open_time"] for item in symbols.values()
    }
    if len(market_slots) != 1:
        raise CryptoFiveMinuteDataError("crypto_5m_cross_symbol_slot_mismatch")
    payload: dict[str, Any] = {
        "contract": OBSERVATION_CONTRACT,
        "market": "crypto",
        "market_session": "24x7",
        "input_interval": "5m_closed_only",
        "profile_sha256": getattr(snapshot, "profile_sha256", None),
        "market_content_sha256": getattr(snapshot, "market_content_sha256", None),
        "source_observation_sha256": getattr(snapshot, "observation_sha256", None),
        "same_observation": True,
        "market_slot": next(iter(market_slots)),
        "evidence_available_at": _iso(evidence_available_at),
        "source_bindings": _canonical_value(source_bindings),
        "symbols": symbols,
        "counterfactual_contract": {
            "contract": COUNTERFACTUAL_CONTRACT,
            "decision_bar_count": EXPECTED_DECISION_BARS,
            "execution_bar_index": EXPECTED_DECISION_BARS + 1,
            "execution_quote_kind": "next_closed_bar_open_counterfactual",
            "available_only_after_execution_bar_close": True,
            "half_spread_bps": format(HALF_SPREAD_BPS, "f"),
            "fixture_slippage_applied_downstream": True,
        },
        **_non_authority_fields(),
    }
    material_for_id = dict(payload)
    observation_id = f"crypto-delayed-observation-{_sha256(material_for_id)[:24]}"
    payload["observation_id"] = observation_id
    payload["observation_content_sha256"] = _sha256(payload)
    return _canonical_value(payload)


def _fixture_and_counterfactual(
    observation: Mapping[str, Any],
    *,
    symbol: str,
    llm_evidence: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    symbols = observation.get("symbols")
    if not isinstance(symbols, Mapping) or not isinstance(symbols.get(symbol), Mapping):
        raise CryptoFiveMinuteDataError("crypto_5m_window_incomplete")
    item = symbols[symbol]
    raw_bars = item.get("bars")
    rules = item.get("instrument_rules")
    if (
        not isinstance(raw_bars, list)
        or len(raw_bars) != REQUIRED_WINDOW_BARS
        or not isinstance(rules, Mapping)
    ):
        raise CryptoFiveMinuteDataError("crypto_5m_window_incomplete")
    decision_bars = raw_bars[:EXPECTED_DECISION_BARS]
    execution_bar = raw_bars[EXPECTED_DECISION_BARS]
    decision_data_through = _utc(
        decision_bars[-1].get("close_time"),
        "crypto_5m_decision_data_through_invalid",
    )
    market_slot = _utc(
        execution_bar.get("open_time"),
        "crypto_5m_execution_bar_open_invalid",
    )
    execution_bar_closed_at = _utc(
        execution_bar.get("close_time"),
        "crypto_5m_execution_bar_close_invalid",
    )
    bar_observed_at = _utc(
        execution_bar.get("observed_at"),
        "crypto_5m_execution_bar_observed_at_invalid",
    )
    evidence_available_at = _utc(
        observation.get("evidence_available_at"),
        "crypto_5m_evidence_available_at_invalid",
    )
    available_after = max(
        execution_bar_closed_at,
        bar_observed_at,
        evidence_available_at,
    )
    execution_observed_at = _ceil_minute(available_after)
    if market_slot != decision_data_through or (
        execution_bar_closed_at - market_slot != FIVE_MINUTES
    ):
        raise CryptoFiveMinuteDataError("crypto_5m_counterfactual_causality_invalid")

    open_price = Decimal(str(execution_bar["open"]))
    tick = Decimal(str(rules["price_tick"]))
    bid = _floor_step(
        open_price * (Decimal("1") - HALF_SPREAD_BPS / Decimal("10000")),
        tick,
    )
    ask = _ceil_step(
        open_price * (Decimal("1") + HALF_SPREAD_BPS / Decimal("10000")),
        tick,
    )
    if bid <= 0 or ask < bid:
        raise CryptoFiveMinuteDataError("crypto_5m_counterfactual_spread_invalid")
    fixture_bars = [
        {
            "symbol": symbol,
            "open_time": bar["open_time"],
            "close_time": bar["close_time"],
            "open": bar["open"],
            "high": bar["high"],
            "low": bar["low"],
            "close": bar["close"],
            "volume": bar["volume"],
            "closed": True,
        }
        for bar in decision_bars
    ]
    causal_material = {
        "symbol": symbol,
        "decision_bars": fixture_bars,
        "instrument_rules": rules,
        "execution_bar_open": format(open_price, "f"),
        "market_slot": _iso(market_slot),
        "bar_closed_at": _iso(execution_bar_closed_at),
        "available_after": _iso(available_after),
        "execution_observed_at": _iso(execution_observed_at),
        "spread_model_id": "symmetric-1bp-per-side-tick-rounded-v1",
        "half_spread_bps": format(HALF_SPREAD_BPS, "f"),
    }
    causal_sha256 = _sha256(causal_material)
    fixture_id = f"crypto-delayed-causal-{symbol.lower()}-{causal_sha256[:24]}"
    receipt_id = f"delayed-paper-receipt-{causal_sha256[:24]}"
    fixture = {
        "contract": FIXTURE_CONTRACT,
        "fixture_id": fixture_id,
        "source_kind": "mock",
        "wire_contract": WIRE_CONTRACT,
        "symbol": symbol,
        "as_of": _iso(decision_data_through),
        "metadata": {
            "state": "ready",
            "degraded": False,
            "freshness": {"state": "fresh"},
            "quality": {"state": "pass"},
            "receipt_id": receipt_id,
            "observed_at": _iso(available_after),
            "data_through": _iso(decision_data_through),
            "lineage": {
                "source": "checked_in_mock",
                "fixture_id": fixture_id,
            },
        },
        "instrument": {
            "base_asset": rules["base_asset"],
            "quote_asset": rules["quote_asset"],
            "price_tick": rules["price_tick"],
            "quantity_step": rules["quantity_step"],
            "min_quantity": rules["min_quantity"],
            "min_notional": rules["min_notional"],
        },
        "bars_5m": fixture_bars,
        "next_executable_quote": {
            "symbol": symbol,
            "observed_at": _iso(execution_observed_at),
            "bid": format(bid, "f"),
            "ask": format(ask, "f"),
        },
        "llm_evidence": (
            _canonical_value(llm_evidence) if llm_evidence is not None else None
        ),
    }
    counterfactual = {
        "contract": COUNTERFACTUAL_CONTRACT,
        "execution_quote_kind": "next_closed_bar_open_counterfactual",
        "market_slot": _iso(market_slot),
        "bar_closed_at": _iso(execution_bar_closed_at),
        "available_after": _iso(available_after),
        "execution_observed_at": _iso(execution_observed_at),
        "decision_data_through": _iso(decision_data_through),
        "reference_open": format(open_price, "f"),
        "spread_model": {
            "model_id": "symmetric-1bp-per-side-tick-rounded-v1",
            "half_spread_bps": format(HALF_SPREAD_BPS, "f"),
            "bid": format(bid, "f"),
            "ask": format(ask, "f"),
            "price_tick": format(tick, "f"),
        },
        "execution_occurs_at_market_slot": False,
        "counterfactual_only": True,
        "label_status": "pending",
        **_non_authority_fields(),
    }
    return fixture, counterfactual


def _decision_event(
    *,
    observation: Mapping[str, Any],
    symbol: str,
    disposition: str,
    bundle: Mapping[str, Any],
    counterfactual: Mapping[str, Any],
) -> dict[str, Any]:
    decision = bundle.get("decision")
    if not isinstance(decision, Mapping):
        raise RuntimeError("delayed_paper_bundle_decision_missing")
    material = {
        "observation_id": observation["observation_id"],
        "symbol": symbol,
        "decision_id": decision.get("decision_id"),
        "run_id": bundle.get("run_id"),
    }
    return {
        "contract": DECISION_LEDGER_CONTRACT,
        "event_id": f"crypto-delayed-decision-{_sha256(material)[:24]}",
        "event_type": "decision",
        "market": "crypto",
        "market_session": "24x7",
        "observation_id": observation["observation_id"],
        "observation_content_sha256": observation["observation_content_sha256"],
        "symbol": symbol,
        "disposition": disposition,
        "decision_id": decision.get("decision_id"),
        "decision_action": decision.get("action"),
        "decision_reason": decision.get("reason"),
        "run_id": bundle.get("run_id"),
        "business_bundle_sha256": bundle.get("business_bundle_sha256"),
        "counterfactual": counterfactual,
        **_non_authority_fields(),
    }


def _risk_reject_event(
    *,
    observation: Mapping[str, Any],
    symbol: str,
    reason_code: str,
    decision: Mapping[str, Any],
    counterfactual: Mapping[str, Any],
) -> dict[str, Any]:
    material = {
        "observation_id": observation["observation_id"],
        "symbol": symbol,
        "decision_id": decision.get("decision_id"),
        "reason_code": reason_code,
    }
    return {
        "contract": DECISION_LEDGER_CONTRACT,
        "event_id": f"crypto-delayed-risk-reject-{_sha256(material)[:24]}",
        "event_type": "risk_reject",
        "market": "crypto",
        "market_session": "24x7",
        "observation_id": observation["observation_id"],
        "observation_content_sha256": observation["observation_content_sha256"],
        "symbol": symbol,
        "disposition": "risk_rejected",
        "reason_code": reason_code,
        "decision_id": decision.get("decision_id"),
        "decision_action": decision.get("action"),
        "decision_reason": decision.get("reason"),
        "run_id": None,
        "business_bundle_sha256": None,
        "counterfactual": counterfactual,
        **_non_authority_fields(),
    }


def _prepare_observation(
    observation: Mapping[str, Any],
    *,
    llm_evidence: Mapping[str, Any] | None,
    decision_evaluator: Callable[[QualifiedFixtureEvidence], TimeframeDecision] = (
        evaluate_frozen_champion
    ),
) -> dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    """Prepare causal fixtures using one explicit, frozen decision evaluator.

    The default remains the existing Champion.  A detached challenger must pass
    its own deterministic evaluator and use a distinct output root; this helper
    never chooses, tunes, or promotes an evaluator.
    """
    if not callable(decision_evaluator):
        raise RuntimeError("delayed_paper_decision_evaluator_invalid")
    prepared: dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = {}
    for symbol in FROZEN_SYMBOLS:
        fixture, counterfactual = _fixture_and_counterfactual(
            observation,
            symbol=symbol,
            llm_evidence=llm_evidence,
        )
        evidence = qualify_fixture_evidence(fixture)
        decision = decision_evaluator(evidence)
        if not isinstance(decision, TimeframeDecision):
            raise RuntimeError("delayed_paper_decision_evaluator_invalid")
        prepared[symbol] = (
            fixture,
            counterfactual,
            decision.to_payload(),
        )
    return prepared


def _execute_observation(
    *,
    store: CryptoDelayedPaperObservationStore,
    observation: Mapping[str, Any],
    output_root: Path | str,
    llm_evidence: Mapping[str, Any] | None,
    recovered_pending: bool,
    prepared: Mapping[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]
    | None = None,
) -> dict[str, Any]:
    prepared_symbols = (
        dict(prepared)
        if prepared is not None
        else _prepare_observation(
            observation,
            llm_evidence=llm_evidence,
        )
    )
    account_valuation_fixtures = {
        symbol: prepared_symbols[symbol][0] for symbol in FROZEN_SYMBOLS
    }
    symbol_results: dict[str, Any] = {}
    for symbol in FROZEN_SYMBOLS:
        fixture, counterfactual, prepared_decision = prepared_symbols[symbol]
        core = run_fixture_auto_sim(
            fixture,
            output_root=output_root,
            account_valuation_fixtures=account_valuation_fixtures,
        )
        bundle = core["bundle"]
        risk_reject_reason = core.get("risk_reject_reason")
        if risk_reject_reason is not None:
            event = _risk_reject_event(
                observation=observation,
                symbol=symbol,
                reason_code=str(risk_reject_reason),
                decision=prepared_decision,
                counterfactual=counterfactual,
            )
            stored_event = store.append_event(event)
            symbol_results[symbol] = {
                "disposition": "risk_rejected",
                "bundle": bundle,
                "risk_reject": {
                    "event_id": stored_event["event_id"],
                    "reason_code": str(risk_reject_reason),
                    "decision": prepared_decision,
                },
                "counterfactual": counterfactual,
                "idempotent_replay": core["idempotent_replay"],
                "llm_sidecar": core.get("llm_sidecar"),
            }
            continue
        action = bundle["decision"]["action"]
        disposition = (
            "fixture_simulated_fill"
            if action == "buy" and bundle.get("paper_receipt") is not None
            else "observation_only"
        )
        event = _decision_event(
            observation=observation,
            symbol=symbol,
            disposition=disposition,
            bundle=bundle,
            counterfactual=counterfactual,
        )
        store.append_event(event)
        symbol_results[symbol] = {
            "disposition": disposition,
            "bundle": bundle,
            "counterfactual": counterfactual,
            "idempotent_replay": core["idempotent_replay"],
            "llm_sidecar": core.get("llm_sidecar"),
        }

    completed_bundles = [
        item["bundle"]
        for item in symbol_results.values()
        if isinstance(item.get("bundle"), Mapping)
    ]
    if completed_bundles:
        final_capital = completed_bundles[-1]["capital"]["final"]
        positions = final_capital.get("positions")
        if not isinstance(positions, Mapping) or len(positions) > MAX_POSITIONS:
            raise RuntimeError("crypto_delayed_paper_max_positions_exceeded")
    result = {
        "contract": RUNNER_CONTRACT,
        "status": "completed",
        "market": "crypto",
        "market_session": "24x7",
        "input_interval": "5m_closed_only",
        "regime_interval": "1h",
        "decision_interval": "15m",
        "execution_interval": "5m",
        "observation_id": observation["observation_id"],
        "observation_content_sha256": observation["observation_content_sha256"],
        "recovered_pending": recovered_pending,
        "max_positions": MAX_POSITIONS,
        "capital_effect": (
            "fixture_simulated_fill"
            if any(
                isinstance(bundle.get("paper_receipt"), Mapping)
                for bundle in completed_bundles
            )
            else "mark_only_risk_reconcile"
        ),
        "symbols": symbol_results,
        **_non_authority_fields(),
    }
    store.mark_complete(observation, result)
    return _canonical_value(result)


def _data_reject(
    *,
    store: CryptoDelayedPaperObservationStore,
    profile: Any,
    request: Any,
    reason_code: str,
    rejected_observation_sha256: str | None = None,
) -> dict[str, Any]:
    material = {
        "profile": _canonical_value(profile),
        "request": _canonical_value(request),
        "reason_code": reason_code,
        "rejected_observation_sha256": rejected_observation_sha256,
    }
    reject_id = f"crypto-delayed-data-reject-{_sha256(material)[:24]}"
    event = {
        "contract": DECISION_LEDGER_CONTRACT,
        "event_id": reject_id,
        "event_type": "data_reject",
        "market": "crypto",
        "market_session": "24x7",
        "reason_code": reason_code,
        "request_sha256": _sha256(material["request"]),
        "request_window_end": material["request"].get("window_end"),
        "request_observation_cutoff": material["request"].get(
            "observation_cutoff"
        ),
        "profile_sha256": (
            getattr(profile, "sha256", None) or _sha256(material["profile"])
        ),
        "rejected_observation_sha256": rejected_observation_sha256,
        **_non_authority_fields(),
    }
    store.append_event(event)
    return {
        "contract": RUNNER_CONTRACT,
        "status": "data_reject",
        "reason_code": reason_code,
        "market": "crypto",
        "market_session": "24x7",
        "max_positions": MAX_POSITIONS,
        "symbols": {},
        **_non_authority_fields(),
    }


def _run_locked_cycle(
    *,
    store: CryptoDelayedPaperObservationStore,
    port: Any,
    profile: Any,
    request: Any,
    output_root: Path | str,
    llm_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    pending = store.pending_observation()
    if pending is not None:
        return _execute_observation(
            store=store,
            observation=pending,
            output_root=output_root,
            llm_evidence=llm_evidence,
            recovered_pending=True,
        )

    try:
        snapshot = port.load_snapshot(profile=profile, request=request)
        if not isinstance(snapshot, CryptoFiveMinuteSnapshot):
            raise CryptoFiveMinuteDataError("crypto_5m_snapshot_type_invalid")
        snapshot.verify_against(profile=profile, request=request)
        observation = _snapshot_to_observation(snapshot)
        prepared = _prepare_observation(
            observation,
            llm_evidence=llm_evidence,
        )
    except CryptoFiveMinuteDataError as exc:
        reason_code = getattr(exc, "reason_code", None) or str(exc)
        return _data_reject(
            store=store,
            profile=profile,
            request=request,
            reason_code=reason_code,
        )
    try:
        accepted = store.accept(observation)
    except CryptoDelayedPaperLedgerError as exc:
        reason_codes = {
            "delayed_paper_slot_payload_conflict": ("crypto_5m_slot_payload_conflict"),
            "delayed_paper_slot_not_monotonic": ("crypto_5m_slot_not_monotonic"),
        }
        reason_code = reason_codes.get(str(exc))
        if reason_code is None:
            raise
        return _data_reject(
            store=store,
            profile=profile,
            request=request,
            reason_code=reason_code,
            rejected_observation_sha256=observation.get("observation_content_sha256"),
        )
    return _execute_observation(
        store=store,
        observation=accepted,
        output_root=output_root,
        llm_evidence=llm_evidence,
        recovered_pending=False,
        prepared=prepared,
    )


def run_crypto_delayed_paper_once(
    *,
    port: Any,
    profile: Any,
    request: Any,
    output_root: Path | str,
    llm_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run or recover one serialized two-symbol delayed-paper observation."""

    _assert_simulation_only()
    if llm_evidence is not None:
        _validate_json_tree(
            llm_evidence,
            path="crypto_delayed_paper_llm_evidence",
            external=True,
        )
    store = CryptoDelayedPaperObservationStore(output_root)
    with store.cycle():
        return _run_locked_cycle(
            store=store,
            port=port,
            profile=profile,
            request=request,
            output_root=output_root,
            llm_evidence=llm_evidence,
        )


__all__ = [
    "COUNTERFACTUAL_CONTRACT",
    "HALF_SPREAD_BPS",
    "MAX_POSITIONS",
    "RUNNER_CONTRACT",
    "run_crypto_delayed_paper_once",
]
