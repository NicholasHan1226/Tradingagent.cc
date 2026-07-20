from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from CNFutures import sim_runner
from shared.capital.market_policy import MarketPolicy
from shared.execution.execution_lineage import (
    build_execution_lineage,
    require_execution_lineage,
)
from shared.execution.sim_broker import SimResult, execute_sim_order
from shared.execution import sim_executor_registry


CRYPTO_CONTRACT = "tradingagent.crypto.paper_broker.v1"
CRYPTO_AUTHORITY = "crypto-shadow-sim-v1"
CNF_CONTRACT = "tradingagent.cnfutures.paper_broker.v1"
CNF_AUTHORITY = "cn-futures-capital-v1"


def _bound_account(
    market: str,
    *,
    account_id: str,
    contract: str,
    authority_id: str,
    generation: int,
) -> dict[str, object]:
    return {
        "account_id": account_id,
        "market": market,
        "broker_contract": contract,
        "authority_id": authority_id,
        "authority_generation": generation,
    }


@pytest.fixture(autouse=True)
def _restore_registry() -> None:
    original = deepcopy(sim_executor_registry._SIM_EXECUTORS)
    sim_executor_registry._SIM_EXECUTORS.clear()
    try:
        yield
    finally:
        sim_executor_registry._SIM_EXECUTORS.clear()
        sim_executor_registry._SIM_EXECUTORS.update(original)


def _register_filled_stub(market: str, contract: str, authority_id: str, calls: list):
    def execute(order, account, config) -> SimResult:
        calls.append((order, account, config))
        return SimResult(
            status="filled",
            filled_qty=1,
            avg_price=1.0,
            order_id=str(order.get("order_id") or ""),
            market=market,
            broker_contract=contract,
            authority_id=authority_id,
        )

    sim_executor_registry.register_sim_executor(
        market,
        execute,
        simulation_contract=contract,
        authority_id=authority_id,
    )


@pytest.mark.parametrize(
    ("market", "contract", "authority_id"),
    [
        ("crypto", CRYPTO_CONTRACT, CRYPTO_AUTHORITY),
        ("cn_futures", CNF_CONTRACT, CNF_AUTHORITY),
    ],
)
def test_dispatch_rejects_ashare_account_in_another_market_lane(
    market: str,
    contract: str,
    authority_id: str,
) -> None:
    calls: list[object] = []
    _register_filled_stub(market, contract, authority_id, calls)

    result = execute_sim_order(
        order={"order_id": f"WRONG-{market}", "authority_generation": 2},
        market=market,
        account=_bound_account(
            market,
            account_id="ashare_sim",
            contract=contract,
            authority_id=authority_id,
            generation=2,
        ),
        config={},
    )

    assert result.status == "failed"
    assert result.raw_response["reason"] == "sim_account_binding_invalid"
    assert calls == []


def test_dispatch_rejects_unbound_governed_account_before_executor() -> None:
    calls: list[object] = []
    _register_filled_stub("crypto", CRYPTO_CONTRACT, CRYPTO_AUTHORITY, calls)

    result = execute_sim_order(
        order={"order_id": "UNBOUND-CRYPTO", "authority_generation": 2},
        market="crypto",
        account={"account_id": "crypto_sim"},
        config={},
    )

    assert result.status == "failed"
    assert result.raw_response["reason"] == "sim_account_binding_invalid"
    assert calls == []


def test_dispatch_propagates_matching_rotated_generation_without_minting_it() -> None:
    calls: list[tuple[dict, dict, dict]] = []
    _register_filled_stub("cn_futures", CNF_CONTRACT, CNF_AUTHORITY, calls)

    result = execute_sim_order(
        order={"order_id": "CNF-GEN-2", "authority_generation": 2},
        market="cn_futures",
        account=_bound_account(
            "cn_futures",
            account_id="cn_futures_sim",
            contract=CNF_CONTRACT,
            authority_id=CNF_AUTHORITY,
            generation=2,
        ),
        config={},
    )

    assert result.status == "filled"
    assert result.authority_generation == 2
    assert calls[0][0]["authority_generation"] == 2
    assert calls[0][1]["authority_generation"] == 2
    assert calls[0][2]["authority_generation"] == 2


def test_dispatch_rejects_order_and_account_generation_disagreement() -> None:
    calls: list[object] = []
    _register_filled_stub("cn_futures", CNF_CONTRACT, CNF_AUTHORITY, calls)

    result = execute_sim_order(
        order={"order_id": "CNF-MIXED-GEN", "authority_generation": 1},
        market="cn_futures",
        account=_bound_account(
            "cn_futures",
            account_id="cn_futures_sim",
            contract=CNF_CONTRACT,
            authority_id=CNF_AUTHORITY,
            generation=2,
        ),
        config={},
    )

    assert result.status == "failed"
    assert result.raw_response["reason"] == "sim_input_binding_mismatch"
    assert calls == []


def test_execution_lineage_accepts_and_preserves_positive_rotated_generation() -> None:
    lineage = build_execution_lineage(
        lineage_started_at="2026-07-12T00:00:00+08:00",
        point_in_time_as_of="2026-07-13T10:00:00+08:00",
        authority_generation=2,
    )

    assert lineage["authority_generation"] == 2
    assert require_execution_lineage(lineage) == lineage


def test_market_policy_accepts_positive_rotated_generation(tmp_path: Path) -> None:
    source = Path("shared/capital/cn_futures_capital_policy.yaml")
    payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    payload["authority_generation"] = 2
    rotated = tmp_path / "cn_futures_capital_policy.yaml"
    rotated.write_text(yaml.safe_dump(payload), encoding="utf-8")

    assert MarketPolicy.load("cn_futures", path=rotated).authority_generation == 2


def test_cn_futures_provider_accepts_positive_rotated_generation() -> None:
    state = {
        "source": "market_capital_ledger",
        "reconciled": True,
        "fresh": True,
        "market": "cn_futures",
        "authority_id": CNF_AUTHORITY,
        "authority_generation": 2,
        "execution_lineage_id": "cnf-rotated-generation-2",
        "trade_date": "20260710",
        "initial_equity_cny": 50_000.0,
        "equity_cny": 50_000.0,
        "available_margin": 25_000.0,
        "margin_utilization_limit_cny": 25_000.0,
        "margin_used_cny": 0.0,
        "unrealized_pnl_cny": 0.0,
        "event_id": "MCAP-ROTATED-2",
        "event_checksum": "a" * 64,
        "cumulative_pnl": 0.0,
        "daily_realized_pnl": 0.0,
        "max_daily_loss": 1_500.0,
        "consecutive_losses": 0,
        "max_consecutive_losses": 3,
        "high_water_equity": 50_000.0,
        "max_drawdown": 3_500.0,
        "real_trading_enabled": False,
    }

    accepted, reason = sim_runner._validate_market_capital_provider_state(
        state,
        trade_date="20260710",
    )

    assert reason == "market_capital_state_reconciled"
    assert accepted is not None
    assert accepted["authority_generation"] == 2


def test_cn_futures_runner_binds_rotated_generation_and_aware_decision_time(
    tmp_path: Path,
) -> None:
    class Adapter:
        universe_filter = {"max_symbols": 1, "min_distinct_products": 1}

        def get_strategy_config(self) -> dict[str, object]:
            return {
                "styles": {
                    "trend": {
                        "risk_per_trade": 0.10,
                        "max_margin_usage": 0.30,
                        "weight": 1.0,
                        "no_overnight": True,
                    }
                }
            }

        def get_intraday_universe(self, date: str, interval: str) -> list[str]:
            return ["RB2610.SHF"]

    class Reader:
        def get_bars_intraday(self, *args, **kwargs) -> list[dict[str, object]]:
            return [
                {
                    "bar_time": "2026-07-10 09:30:00",
                    "close": 3_490.0,
                    "volume": 1_000,
                },
                {
                    "bar_time": "2026-07-10 09:35:00",
                    "close": 3_500.0,
                    "volume": 1_000,
                },
            ]

    state = {
        "source": "market_capital_ledger",
        "reconciled": True,
        "fresh": True,
        "market": "cn_futures",
        "authority_id": CNF_AUTHORITY,
        "authority_generation": 2,
        "execution_lineage_id": "cnf-rotated-generation-2",
        "trade_date": "20260710",
        "initial_equity_cny": 50_000.0,
        "equity_cny": 50_000.0,
        "available_margin": 25_000.0,
        "margin_utilization_limit_cny": 25_000.0,
        "margin_used_cny": 0.0,
        "unrealized_pnl_cny": 0.0,
        "event_id": "MCAP-ROTATED-2",
        "event_checksum": "a" * 64,
        "cumulative_pnl": 0.0,
        "daily_realized_pnl": 0.0,
        "max_daily_loss": 1_500.0,
        "consecutive_losses": 0,
        "max_consecutive_losses": 3,
        "high_water_equity": 50_000.0,
        "max_drawdown": 3_500.0,
        "real_trading_enabled": False,
    }
    reservation = {
        "approved": True,
        "reason": "reserved",
        "reservation_id": "RES-GEN-2",
        "event_id": "EVT-GEN-2",
        "reference_id": "ORDER-GEN-2",
        "risk_unit_key": "RB2610.SHF",
        "authority_id": CNF_AUTHORITY,
        "authority_generation": 2,
        "amount_cny": 4_550.0,
        "trade_date": "20260710",
        "point_in_time_as_of": "2026-07-10T09:35:00+08:00",
        "lineage_sha256": "b" * 64,
        "execution_lineage_id": "cnf-rotated-generation-2",
        "event_checksum": "c" * 64,
        "fee_cash_cny": 3.0,
        "real_trading_enabled": False,
    }
    rejected = SimResult(
        status="rejected",
        message="test stop after dispatch",
        market="cn_futures",
        broker_contract=CNF_CONTRACT,
        authority_id=CNF_AUTHORITY,
        authority_generation=2,
        raw_response={"reason": "test_stop"},
    )
    run_now = datetime.fromisoformat("2026-07-10T09:36:00+08:00")

    with (
        patch.object(
            sim_runner,
            "get_cn_futures_capital_provider_state",
            return_value=state,
        ),
        patch.object(
            sim_runner,
            "generate_style_signal",
            return_value={"action": "buy", "side": "buy", "price": 3_500.0},
        ),
        patch.object(
            sim_runner,
            "_reserve_cn_futures_market_margin",
            return_value=reservation,
        ) as reserve,
        patch.object(
            sim_runner,
            "execute_sim_order",
            return_value=rejected,
        ) as execute,
    ):
        sim_runner.run_multi_style_simulation(
            Adapter(),
            "20260710",
            Reader(),
            signals_dir=tmp_path / "signals",
            review_path=tmp_path / "review.jsonl",
            now=run_now,
        )

    reserve_kwargs = reserve.call_args.kwargs
    assert reserve_kwargs["authority_id"] == CNF_AUTHORITY
    assert reserve_kwargs["authority_generation"] == 2
    order = execute.call_args.kwargs["order"]
    account = execute.call_args.kwargs["account"]
    assert order["decision_time"] == "2026-07-10T09:36:00+08:00"
    assert order["authority_generation"] == 2
    assert account == {
        "account_id": "cn_futures_sim",
        "market": "cn_futures",
        "broker_contract": CNF_CONTRACT,
        "authority_id": CNF_AUTHORITY,
        "authority_generation": 2,
        "capital_layer": "simulated",
        "account_type": "simulated",
    }
