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


def assert_no_live_broker(config: Any) -> None:
    safety = config.safety
    if getattr(safety, "live_broker_enabled", False):
        raise SafetyViolation(f"{config.market}: live broker access is disabled in shared market tools")


def assert_public_data_only(config: Any) -> None:
    assert_no_live_broker(config)
    assert_no_real_execution(config)


def reject_real_execution_payload(payload: dict[str, Any] | None, *, context: str) -> None:
    """Reject order/account/config fields that imply live or real execution."""

    payload = dict(payload or {})
    unsafe_keys = {
        "api_key",
        "api_secret",
        "secret_key",
        "private_key",
        "signature",
        "signed",
        "signed_binance",
        "binance_signed",
        "withdraw",
        "transfer",
        "live_broker",
        "live_broker_enabled",
        "real_money_enabled",
        "direct_execution_enabled",
    }
    present = sorted(
        key
        for key in unsafe_keys
        if key in payload and payload.get(key) not in (None, "", False)
    )
    if present:
        raise RuntimeError(
            f"{context}: real/live execution is rejected in simulated market tools; "
            f"unsafe fields={present}"
        )

    for key in ("capital_layer", "account_type", "execution_mode", "mode", "broker_mode"):
        value = str(payload.get(key) or "").strip().lower()
        if value in {"real", "live", "broker", "exchange"}:
            raise RuntimeError(
                f"{context}: real/live execution is rejected in simulated market tools"
            )
