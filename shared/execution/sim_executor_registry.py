#!/usr/bin/env python3
"""Registry for market-specific simulated-account executors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from shared.governance.market_lanes import load_market_lanes

SimExecutor = Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], Any]


@dataclass(frozen=True)
class SimExecutorBinding:
    """One market's explicit paper-broker binding."""

    market: str
    simulation_contract: str
    authority_id: str
    fn: SimExecutor


_SIM_EXECUTORS: dict[str, SimExecutorBinding] = {}
_TEST_ONLY_LEGACY_CONFIG_KEY = "_test_only_ashare_legacy_simulator_token"
_TEST_ONLY_LEGACY_TOKEN = object()


def _normalize_market(market: str | None) -> str:
    return str(market or "").lower().strip()


def _governed_lane(market_key: str):
    registry = load_market_lanes()
    try:
        return registry.get_for_runtime_market(market_key)
    except ValueError:
        return None


def register_sim_executor(
    market: str,
    fn: SimExecutor,
    *,
    simulation_contract: str = "",
    authority_id: str = "",
) -> SimExecutor:
    """Register an executor with its market-specific contract and authority."""
    market_key = _normalize_market(market)
    if not market_key:
        raise ValueError("market is required")
    if not callable(fn):
        raise TypeError("sim executor must be callable")
    lane = _governed_lane(market_key)
    if lane is not None:
        if lane.authority_state != "current_verified_simulated":
            raise ValueError(
                "generic executor registration disabled for "
                f"market={market_key} authority_state={lane.authority_state}"
            )
        expected_contract = lane.broker_boundary.simulation_contract
        if simulation_contract != expected_contract:
            raise ValueError(
                f"market {market_key} requires simulation_contract={expected_contract}"
            )
        if authority_id != lane.authority_id:
            raise ValueError(
                f"market {market_key} requires authority_id={lane.authority_id}"
            )
    else:
        raise ValueError(
            f"market {market_key} is not an owned market lane; registration denied"
        )
    binding = SimExecutorBinding(
        market=market_key,
        simulation_contract=simulation_contract,
        authority_id=authority_id,
        fn=fn,
    )
    existing = _SIM_EXECUTORS.get(market_key)
    if existing is not None and existing != binding:
        raise ValueError(f"sim executor already registered for market={market_key}")
    _SIM_EXECUTORS[market_key] = binding
    return fn


def local_sim_executor(
    order: dict[str, Any],
    account: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> Any:
    """Fallback executor that wraps the legacy local slippage simulator.

    The A-share legacy simulator is retired from normal dispatch.  It can only
    be reached through :func:`build_test_only_legacy_sim_executor`, which adds
    a process-local token that cannot arrive through a serialized order/config.
    No market receives this fallback implicitly from the registry.
    """
    from .sim_broker import SimResult, simulate_order

    market_key = _normalize_market(order.get("market"))
    config_payload = dict(config or {})
    test_only_token = config_payload.pop(_TEST_ONLY_LEGACY_CONFIG_KEY, None)
    if market_key == "ashare" and test_only_token is not _TEST_ONLY_LEGACY_TOKEN:
        return SimResult(
            status="failed",
            filled_qty=0,
            avg_price=0.0,
            fee=0.0,
            message="A-share legacy simulator is disabled outside explicit test-only injection",
            order_id=str(order.get("order_id", "")),
            market=market_key,
            raw_response={
                "recorded": False,
                "reason": "ashare_legacy_simulator_disabled",
                "legacy_fallback_used": False,
            },
        )

    result = simulate_order(order)
    return SimResult(
        status=result.get("status", "failed"),
        filled_qty=int(result.get("filled_quantity", 0) or 0),
        avg_price=float(result.get("filled_price", 0.0) or 0.0),
        fee=float(result.get("fee", 0.0) or 0.0),
        message=str(result.get("message", "local slippage simulation fallback")),
        order_id=str(result.get("order_id", order.get("order_id", ""))),
        market=str(order.get("market", "")),
        raw_response=result,
    )


def build_test_only_legacy_sim_executor(market: str) -> SimExecutor:
    """Return an explicitly test-only wrapper around the retired simulator."""
    market_key = _normalize_market(market)
    if market_key != "ashare":
        raise ValueError("test-only legacy executor is restricted to market=ashare")

    def _execute(
        order: dict[str, Any],
        account: dict[str, Any],
        config: dict[str, Any],
    ) -> Any:
        test_order = dict(order)
        test_order["market"] = market_key
        test_config = dict(config or {})
        test_config[_TEST_ONLY_LEGACY_CONFIG_KEY] = _TEST_ONLY_LEGACY_TOKEN
        return local_sim_executor(test_order, account, test_config)

    return _execute


def get_sim_executor(market: str | None) -> SimExecutor | None:
    """Return only an explicitly registered executor; unknown markets fail closed."""
    market_key = _normalize_market(market)
    lane = _governed_lane(market_key)
    if lane is None or lane.authority_state != "current_verified_simulated":
        return None
    registered = _SIM_EXECUTORS.get(market_key)
    if registered is not None:
        return registered.fn
    return None


def get_sim_executor_binding(market: str | None) -> SimExecutorBinding | None:
    """Return the immutable binding for a governed market."""
    market_key = _normalize_market(market)
    lane = _governed_lane(market_key)
    if lane is None or lane.authority_state != "current_verified_simulated":
        return None
    return _SIM_EXECUTORS.get(market_key)
