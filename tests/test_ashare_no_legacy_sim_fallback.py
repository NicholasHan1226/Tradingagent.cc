from __future__ import annotations

from pathlib import Path
from typing import Any

from shared.execution import execution_router, sim_broker, sim_executor_registry


def _ashare_order(account: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "order_id": "ASHARE-NO-LEGACY-FALLBACK",
        "market": "ashare",
        "ts_code": "600000.SH",
        "side": "buy",
        "quantity": 100,
        "mid_price": 10.0,
        "price": 10.0,
        "candidate_pool_layer": "candidate",
        "sample_intent": "exploitation",
        "execution_source": "ashare_candidate_layer",
        "authority_generation": 1,
        "account": account or {"cash": 50_000.0},
    }


def _isolate_logs(monkeypatch: Any, tmp_path: Path) -> Path:
    legacy_ledger = tmp_path / "legacy_sim_orders.jsonl"
    monkeypatch.setattr(sim_broker, "SIM_LEDGER", legacy_ledger)
    monkeypatch.setattr(execution_router, "ROUTER_LOG", tmp_path / "routes.jsonl")
    return legacy_ledger


def test_ashare_executor_exception_fails_closed_without_legacy_fill_or_account_mutation(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    legacy_ledger = _isolate_logs(monkeypatch, tmp_path)
    account = {
        "account_id": "ashare_sim",
        "market": "ashare",
        "broker_contract": "tradingagent.ashare.paper_broker.v1",
        "authority_id": "ashare-capital-v1",
        "authority_generation": 1,
        "cash": 50_000.0,
        "positions": {},
    }

    def failing_executor(
        order: dict[str, Any],
        account: dict[str, Any],
        config: dict[str, Any],
    ) -> Any:
        account["cash"] = 0.0
        raise RuntimeError("formal executor unavailable after dispatch")

    monkeypatch.setitem(
        sim_executor_registry._SIM_EXECUTORS,
        "ashare",
        sim_executor_registry.SimExecutorBinding(
            market="ashare",
            simulation_contract="tradingagent.ashare.paper_broker.v1",
            authority_id="ashare-capital-v1",
            fn=failing_executor,
        ),
    )

    receipt = execution_router.route(_ashare_order(account), "sim")

    assert receipt["executed"] is False
    assert receipt["result"]["status"] == "failed"
    assert receipt["result"]["filled_qty"] == 0
    assert receipt["result"]["avg_price"] == 0.0
    assert "formal executor unavailable after dispatch" in receipt["result"]["message"]
    assert account == {
        "account_id": "ashare_sim",
        "market": "ashare",
        "broker_contract": "tradingagent.ashare.paper_broker.v1",
        "authority_id": "ashare-capital-v1",
        "authority_generation": 1,
        "cash": 50_000.0,
        "positions": {},
    }
    assert not legacy_ledger.exists()


def test_missing_ashare_executor_is_explicitly_unavailable_without_legacy_fill(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    legacy_ledger = _isolate_logs(monkeypatch, tmp_path)
    monkeypatch.setattr(sim_executor_registry, "_SIM_EXECUTORS", {})
    monkeypatch.setattr(sim_broker, "_ensure_builtin_executor", lambda _market: None)

    receipt = execution_router.route(_ashare_order(), "sim")

    assert receipt["executed"] is False
    assert receipt["result"]["status"] == "failed"
    assert receipt["result"]["filled_qty"] == 0
    assert "No simulated executor available for market=ashare" in receipt["result"]["message"]
    assert not legacy_ledger.exists()


def test_missing_market_is_rejected_before_sim_executor_dispatch(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    legacy_ledger = _isolate_logs(monkeypatch, tmp_path)
    order = _ashare_order()
    order.pop("market")

    def forbidden_executor(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("missing-market order reached simulated executor")

    monkeypatch.setattr(sim_broker, "execute_sim_order", forbidden_executor)

    receipt = execution_router.route(order, "sim")

    assert receipt["executed"] is False
    assert receipt["channel"] == "none"
    assert receipt["result"]["status"] == "failed"
    assert receipt["result"]["reason"] == "market_required"
    assert receipt["result"]["legacy_fallback_used"] is False
    assert not legacy_ledger.exists()


def test_ashare_registry_has_no_implicit_legacy_executor(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        sim_executor_registry,
        "_SIM_EXECUTORS",
        {
            key: value
            for key, value in sim_executor_registry._SIM_EXECUTORS.items()
            if key != "ashare"
        },
    )

    assert sim_executor_registry.get_sim_executor("ashare") is None


def test_legacy_ashare_simulator_requires_explicit_test_only_factory(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    legacy_ledger = _isolate_logs(monkeypatch, tmp_path)
    monkeypatch.setattr(
        sim_executor_registry,
        "_SIM_EXECUTORS",
        {
            key: value
            for key, value in sim_executor_registry._SIM_EXECUTORS.items()
            if key != "ashare"
        },
    )

    direct = sim_executor_registry.local_sim_executor(
        _ashare_order(),
        {},
        {},
    )
    assert direct.status == "failed"
    assert direct.filled_qty == 0
    assert direct.raw_response["reason"] == "ashare_legacy_simulator_disabled"
    assert not legacy_ledger.exists()

    executor = sim_executor_registry.build_test_only_legacy_sim_executor("ashare")
    injected = executor(_ashare_order(), {}, {})

    assert injected.status == "filled"
    assert injected.filled_qty == 100
    assert legacy_ledger.exists()


def test_registry_never_uses_implicit_cross_market_fallback(monkeypatch: Any) -> None:
    monkeypatch.setattr(sim_executor_registry, "_SIM_EXECUTORS", {})

    assert sim_executor_registry.get_sim_executor("us") is None
    assert sim_executor_registry.get_sim_executor("hk") is None
    assert sim_executor_registry.get_sim_executor("cn_futures") is None
