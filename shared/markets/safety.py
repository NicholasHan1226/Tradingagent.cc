#!/usr/bin/env python3
"""Safety guards for shared multi-market tooling."""

from __future__ import annotations

from collections.abc import Iterable
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
        "real_execution",
        "direct_execution",
        "direct_execution_enabled",
        "live",
    }
    present = sorted(
        path
        for path, key, value in _iter_payload_items(payload)
        if key in unsafe_keys and _is_truthy_payload_value(value)
    )
    if present:
        raise RuntimeError(
            f"{context}: real/live execution is rejected in simulated market tools; "
            f"unsafe fields={present}"
        )

    for path, key, raw_value in _iter_payload_items(payload):
        if key not in {"capital_layer", "account_type", "execution_mode", "mode", "broker_mode"}:
            continue
        value = str(raw_value or "").strip().lower()
        if value in {"real", "live", "broker", "exchange"}:
            raise RuntimeError(
                f"{context}: real/live execution is rejected in simulated market tools; "
                f"unsafe fields={[path]}"
            )


def _iter_payload_items(value: Any, prefix: str = "") -> Iterable[tuple[str, str, Any]]:
    if isinstance(value, dict):
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            path = f"{prefix}.{key}" if prefix else key
            yield path, key, raw_value
            yield from _iter_payload_items(raw_value, path)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _iter_payload_items(item, f"{prefix}[{index}]")


def _is_truthy_payload_value(value: Any) -> bool:
    if value in (None, "", False):
        return False
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off", "none", "null"}
    return bool(value)
