"""Run the preregistered cost-floor Challenger in a separate paper root.

This adapter intentionally has no CLI or systemd unit.  A release must first
provide a new, isolated epoch manifest and independently verify its root.  It
therefore cannot be pointed at the active G5 epoch by accident.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from Crypto.cost_aware_challenger import evaluate_cost_aware_challenger
from Crypto.delayed_paper_round_trip import run_crypto_delayed_paper_round_trip_once
from Crypto.fixture_sim.contracts import _assert_simulation_only


COST_AWARE_CHALLENGER_RUNNER_CONTRACT = (
    "tradingagent.crypto.cost_aware_challenger_runner.v1"
)


def run_cost_aware_challenger_once(
    *,
    port: Any,
    profile: Any,
    request: Any,
    output_root: Path | str,
    paper_fill_capacities: Mapping[str, Decimal] | None = None,
) -> dict[str, Any]:
    """Accumulate one causal shadow cycle in the caller's isolated root."""

    _assert_simulation_only()
    result = run_crypto_delayed_paper_round_trip_once(
        port=port,
        profile=profile,
        request=request,
        output_root=output_root,
        paper_fill_capacities=paper_fill_capacities,
        decision_evaluator=evaluate_cost_aware_challenger,
    )
    return {
        **result,
        "challenger_runner_contract": COST_AWARE_CHALLENGER_RUNNER_CONTRACT,
    }


__all__ = [
    "COST_AWARE_CHALLENGER_RUNNER_CONTRACT",
    "run_cost_aware_challenger_once",
]
