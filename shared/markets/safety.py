#!/usr/bin/env python3
"""Safety guards for shared multi-market tooling."""

from __future__ import annotations

from typing import Any


class SafetyViolation(RuntimeError):
    """Raised when a market tool config crosses a shadow/simulated boundary."""


def assert_shadow_or_sim_only(config: Any) -> None:
    capital = config.capital
    allowed_layers = set(getattr(capital, "allowed_layers", ()) or ())
    default_layer = getattr(capital, "default_layer", "")
    unsafe_layers = allowed_layers.difference({"shadow", "simulated"})
    if unsafe_layers or default_layer not in {"shadow", "simulated"}:
        raise SafetyViolation(
            f"{config.market}: market tools are shadow/simulated only; "
            f"default_layer={default_layer!r}, allowed_layers={sorted(allowed_layers)!r}"
        )


def assert_no_real_execution(config: Any) -> None:
    safety = config.safety
    if getattr(safety, "real_money_enabled", False):
        raise SafetyViolation(f"{config.market}: real-money execution is disabled in shared market tools")
    if getattr(safety, "direct_execution_enabled", False):
        raise SafetyViolation(f"{config.market}: direct execution is disabled in shared market tools")
    assert_shadow_or_sim_only(config)


def assert_public_data_only(config: Any) -> None:
    safety = config.safety
    if getattr(safety, "live_broker_enabled", False):
        raise SafetyViolation(f"{config.market}: live broker access is not public data")
    assert_no_real_execution(config)
