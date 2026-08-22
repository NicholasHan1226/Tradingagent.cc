from __future__ import annotations

from Crypto.forty_symbol_observation_runtime import (
    FORTY_SYMBOL_INVOCATION_BUDGET_SECONDS,
)


def test_forty_symbol_runtime_has_independent_bounded_budget() -> None:
    assert FORTY_SYMBOL_INVOCATION_BUDGET_SECONDS == 300.0
