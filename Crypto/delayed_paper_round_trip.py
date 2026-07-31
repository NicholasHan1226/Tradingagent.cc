"""Candidate closed-5m runner for the independent round-trip capital generation."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from Crypto.delayed_paper_ledger import (
    DECISION_LEDGER_CONTRACT,
    CryptoDelayedPaperObservationStore,
    _canonical_value,
    _non_authority_fields,
    _sha256,
)
from Crypto.delayed_paper_runner import (
    FROZEN_SYMBOLS,
    _prepare_observation,
    _snapshot_to_observation,
)
from Crypto.fixture_sim.contracts import _assert_simulation_only
from Crypto.five_minute_data import CryptoFiveMinuteSnapshot
from Crypto.round_trip_capital import (
    ROUND_TRIP_CAPITAL_POLICY,
    run_round_trip_fixture_cycle,
)


ROUND_TRIP_RUNNER_CONTRACT = "tradingagent.crypto.delayed_paper_round_trip_runner.v1"


def _business_digest(result: Mapping[str, Any]) -> str:
    return _sha256(
        {
            "cycle_id": result.get("cycle_id"),
            "exit_policy_id": result.get("exit_policy_id"),
            "exit_reason": result.get("exit_reason"),
            "order": result.get("order"),
            "receipt": result.get("receipt"),
        }
    )


def _capital_input(
    *,
    fixture: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> dict[str, Any]:
    quote = fixture.get("next_executable_quote")
    instrument = fixture.get("instrument")
    metadata = fixture.get("metadata")
    if (
        not isinstance(quote, Mapping)
        or not isinstance(instrument, Mapping)
        or not isinstance(metadata, Mapping)
    ):
        raise RuntimeError("round_trip_prepared_fixture_invalid")
    return {
        "fixture_id": fixture.get("fixture_id"),
        "symbol": fixture.get("symbol"),
        "execution_slot": decision.get("execution_slot"),
        "decision": {
            "action": decision.get("action"),
            "regime_return": decision.get("regime_return"),
            "decision_return": decision.get("decision_return"),
            "decision_id": decision.get("decision_id"),
        },
        "quote": {
            "bid": quote.get("bid"),
            "ask": quote.get("ask"),
        },
        "instrument": {
            "price_tick": instrument.get("price_tick"),
            "quantity_step": instrument.get("quantity_step"),
            "min_quantity": instrument.get("min_quantity"),
            "min_notional": instrument.get("min_notional"),
        },
        "evidence_receipt_id": metadata.get("receipt_id"),
        "market_evidence_sha256": decision.get("market_evidence_sha256"),
        "champion_id": decision.get("champion_id"),
        "champion_sha256": decision.get("champion_sha256"),
    }


def _event(
    *,
    observation: Mapping[str, Any],
    symbol: str,
    result: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> dict[str, Any]:
    order = result.get("order")
    receipt = result.get("receipt")
    material = {
        "observation_id": observation["observation_id"],
        "symbol": symbol,
        "cycle_id": result["cycle_id"],
    }
    return {
        "contract": DECISION_LEDGER_CONTRACT,
        "event_id": f"crypto-round-trip-decision-{_sha256(material)[:24]}",
        "event_type": "decision",
        "market": "crypto",
        "market_session": "24x7",
        "observation_id": observation["observation_id"],
        "observation_content_sha256": observation["observation_content_sha256"],
        "symbol": symbol,
        "disposition": (
            "round_trip_observation_only"
            if order is None
            else f"round_trip_{receipt['status']}"
        ),
        "decision_id": decision.get("decision_id"),
        "decision_action": decision.get("action"),
        "decision_reason": decision.get("reason"),
        "run_id": result["cycle_id"],
        "business_bundle_sha256": _business_digest(result),
        "round_trip_order_side": (
            order.get("side") if isinstance(order, Mapping) else None
        ),
        "round_trip_receipt_status": (
            receipt.get("status") if isinstance(receipt, Mapping) else None
        ),
        "exit_reason": result.get("exit_reason"),
        **_non_authority_fields(),
    }


def _execute(
    *,
    store: CryptoDelayedPaperObservationStore,
    observation: Mapping[str, Any],
    output_root: Path | str,
    prepared: Mapping[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    recovered_pending: bool,
    paper_fill_capacities: Mapping[str, Decimal] | None,
) -> dict[str, Any]:
    symbols: dict[str, Any] = {}
    idempotent = True
    for symbol in FROZEN_SYMBOLS:
        fixture, counterfactual, decision = prepared[symbol]
        result = run_round_trip_fixture_cycle(
            _capital_input(fixture=fixture, decision=decision),
            output_root=output_root,
            paper_fill_capacity=(
                paper_fill_capacities.get(symbol)
                if paper_fill_capacities is not None
                else None
            ),
        )
        bundle = {
            "run_id": result["cycle_id"],
            "business_bundle_sha256": _business_digest(result),
            "decision": decision,
            "capital": result,
            **_non_authority_fields(),
        }
        event = _event(
            observation=observation,
            symbol=symbol,
            result=result,
            decision=decision,
        )
        store.append_event(event)
        symbols[symbol] = {
            "disposition": event["disposition"],
            "bundle": bundle,
            "capital": result,
            "counterfactual": counterfactual,
            "idempotent_replay": result["idempotent_replay"],
        }
        idempotent = idempotent and bool(result["idempotent_replay"])
    capital = symbols[FROZEN_SYMBOLS[-1]]["capital"]["capital"]
    result = {
        "contract": ROUND_TRIP_RUNNER_CONTRACT,
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
        "capital_authority_id": ROUND_TRIP_CAPITAL_POLICY.authority_id,
        "capital_generation": ROUND_TRIP_CAPITAL_POLICY.generation,
        "aggregate_with_prior_generations": False,
        "idempotent_replay": idempotent,
        "symbols": symbols,
        "capital": capital,
        **_non_authority_fields(),
    }
    store.mark_complete(observation, result)
    return _canonical_value(result)


def run_crypto_delayed_paper_round_trip_once(
    *,
    port: Any,
    profile: Any,
    request: Any,
    output_root: Path | str,
    paper_fill_capacities: Mapping[str, Decimal] | None = None,
) -> dict[str, Any]:
    """Run/recover one two-symbol observation against generation-2 capital."""

    _assert_simulation_only()
    if paper_fill_capacities is not None and any(
        symbol not in FROZEN_SYMBOLS
        or not isinstance(capacity, Decimal)
        or not capacity.is_finite()
        or capacity < 0
        for symbol, capacity in paper_fill_capacities.items()
    ):
        raise RuntimeError("round_trip_fill_capacities_invalid")
    store = CryptoDelayedPaperObservationStore(output_root)
    with store.cycle():
        pending = store.pending_observation()
        if pending is not None:
            prepared = _prepare_observation(pending, llm_evidence=None)
            return _execute(
                store=store,
                observation=pending,
                output_root=output_root,
                prepared=prepared,
                recovered_pending=True,
                paper_fill_capacities=paper_fill_capacities,
            )
        snapshot = port.load_snapshot(profile=profile, request=request)
        if not isinstance(snapshot, CryptoFiveMinuteSnapshot):
            raise RuntimeError("round_trip_snapshot_type_invalid")
        snapshot.verify_against(profile=profile, request=request)
        observation = _snapshot_to_observation(snapshot)
        prepared = _prepare_observation(observation, llm_evidence=None)
        accepted = store.accept(observation)
        return _execute(
            store=store,
            observation=accepted,
            output_root=output_root,
            prepared=prepared,
            recovered_pending=False,
            paper_fill_capacities=paper_fill_capacities,
        )


__all__ = [
    "ROUND_TRIP_RUNNER_CONTRACT",
    "run_crypto_delayed_paper_round_trip_once",
]
