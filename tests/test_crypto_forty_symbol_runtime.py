from __future__ import annotations

from Crypto.forty_symbol_observation_runtime import (
    FORTY_SYMBOL_INVOCATION_BUDGET_SECONDS,
    FORTY_SYMBOL_RUNTIME_CONTRACT,
)
from Crypto.ten_symbol_observation_runtime import FORTY_SYMBOL_RUNTIME_CONFIG


def test_forty_symbol_runtime_has_independent_bounded_budget() -> None:
    assert FORTY_SYMBOL_INVOCATION_BUDGET_SECONDS == 300.0


def test_forty_symbol_runtime_uses_its_own_receipt_contract() -> None:
    assert FORTY_SYMBOL_RUNTIME_CONTRACT == (
        "tradingagent.crypto.forty_symbol_observation_runtime.v1"
    )
    assert FORTY_SYMBOL_RUNTIME_CONTRACT == FORTY_SYMBOL_RUNTIME_CONFIG.runtime_contract
