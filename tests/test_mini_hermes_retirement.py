from __future__ import annotations

from pathlib import Path

import shared.execution as execution


ROOT = Path(__file__).resolve().parents[1]

RETIRED_SOURCE_PATHS = (
    "mini/mini_consumer.py",
    "mini/README.md",
    "mini/AGENTS.md",
    "shared/execution/hermes_bridge.py",
    "shared/execution/webhook_sender.py",
    "shared/execution/signals_real.py",
    "shared/execution/signal_card_schema.json",
    "shared/execution/fill_card_schema.json",
    "shared/execution/positions_snapshot_schema.json",
    "tests/test_mini_consumer.py",
    "tests/test_webhook.py",
)


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_retired_bridge_sources_are_absent() -> None:
    present = [path for path in RETIRED_SOURCE_PATHS if (ROOT / path).exists()]
    assert present == []


def test_execution_package_does_not_export_a_real_signal_queue() -> None:
    assert not hasattr(execution, "RealSignalQueue")
    assert "RealSignalQueue" not in execution.__all__


def test_ashare_uses_market_specific_server_local_paper_broker() -> None:
    source = _text("Ashare/sim_executor.py")
    assert 'PAPER_BROKER_CONTRACT = "tradingagent.ashare.paper_broker.v1"' in source
    assert "ashare_server_local_paper_broker" in source
    assert "urllib" not in source
    assert "SignalStateMachine" not in source
    assert "mini_hermes_bridge_retired" in source


def test_wrapper_has_no_network_or_mini_health_fallback() -> None:
    wrapper = _text("shared/wrappers/job_ashare_sim_exec.sh")
    assert "block_retired_ashare_runtime" in wrapper
    assert "retired" in wrapper
    assert "exit 78" in wrapper
    assert "urlopen" not in wrapper
    assert "9865" not in wrapper
    assert "MINI_HEALTH" not in wrapper
    assert "ASHARE_SIM_HERMES_ENABLED" not in wrapper
    assert "ASHARE_SIM_WEBHOOK_ENABLED" not in wrapper


def test_runtime_health_has_no_retired_bridge_probe() -> None:
    health = _text("shared/runtime_test/market_health.py")
    assert "DEFAULT_MINI_HEALTH_URL" not in health
    assert "_check_mini_health" not in health
    assert "_check_optional_mini_health" not in health
    assert "9865" not in health


def test_front_does_not_advertise_retired_execution_contracts() -> None:
    capabilities = _text("front/src/api/tradingAgentCapabilities.ts")
    integration = _text("front/docs/integration.md")
    for retired in (
        "signal_card_schema.json",
        "fill_card_schema.json",
        "positions_snapshot_schema.json",
    ):
        assert retired not in capabilities
        assert retired not in integration
    assert "shared/governance/market_lanes.yaml" in capabilities
    assert "A股、CNFutures、Crypto" in capabilities


def test_generic_state_machine_and_real_trading_hard_gate_remain() -> None:
    assert (ROOT / "shared/execution/signal_state_machine.py").is_file()
    assert (ROOT / "shared/execution/real_trading_gate.py").is_file()
