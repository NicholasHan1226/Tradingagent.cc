from __future__ import annotations

import builtins
from pathlib import Path
from typing import Any

from Ashare import sim_executor as ashare_sim_executor
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
    account = {"cash": 50_000.0, "positions": {}}

    def failing_executor(
        order: dict[str, Any],
        account: dict[str, Any],
        config: dict[str, Any],
    ) -> Any:
        account["cash"] = 0.0
        raise RuntimeError("formal executor unavailable after dispatch")

    monkeypatch.setattr(
        ashare_sim_executor,
        "ashare_sim_execute",
        failing_executor,
    )

    receipt = execution_router.route(_ashare_order(account), "sim")

    assert receipt["executed"] is False
    assert receipt["result"] == {
        "status": "failed",
        "reason": "ashare_sim_executor_failed",
        "filled_qty": 0,
        "avg_price": 0.0,
        "fee": 0.0,
        "recorded": False,
        "legacy_fallback_used": False,
        "message": "A-share simulated executor failed: RuntimeError: formal executor unavailable after dispatch",
        "order_id": "ASHARE-NO-LEGACY-FALLBACK",
    }
    assert account == {"cash": 50_000.0, "positions": {}}
    assert not legacy_ledger.exists()


def test_missing_ashare_executor_is_explicitly_unavailable_without_legacy_fill(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    legacy_ledger = _isolate_logs(monkeypatch, tmp_path)
    real_import = builtins.__import__

    def import_without_ashare_executor(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "Ashare.sim_executor":
            raise ModuleNotFoundError("formal A-share executor not installed")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_without_ashare_executor)

    receipt = execution_router.route(_ashare_order(), "sim")

    assert receipt["executed"] is False
    assert receipt["result"]["status"] == "unavailable"
    assert receipt["result"]["reason"] == "ashare_sim_executor_unavailable"
    assert receipt["result"]["filled_qty"] == 0
    assert receipt["result"]["recorded"] is False
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


def test_non_ashare_registry_fallback_behavior_is_unchanged(monkeypatch: Any) -> None:
    monkeypatch.setattr(sim_executor_registry, "_SIM_EXECUTORS", {})

    assert (
        sim_executor_registry.get_sim_executor("us")
        is sim_executor_registry.local_sim_executor
    )
    assert (
        sim_executor_registry.get_sim_executor("hk")
        is sim_executor_registry.local_sim_executor
    )
    assert (
        sim_executor_registry.get_sim_executor("cn_futures")
        is sim_executor_registry.local_sim_executor
    )
