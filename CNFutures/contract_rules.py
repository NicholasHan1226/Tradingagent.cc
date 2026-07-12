#!/usr/bin/env python3
"""Static China futures contract rules for simulation.

These defaults are bootstrap rules for local simulation only. Production
trading must source live exchange / futures-company contract metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ContractRule:
    """Contract metadata needed for simulated order cost estimates."""

    product: str
    exchange: str
    contract_multiplier: int
    tick_size: float
    margin_rate: float
    open_fee_rate: float
    close_fee_rate: float
    price_limit_rate: float
    modeled_overnight_gap_pct: float
    modeled_slippage_bps: float
    open_fee_type: str = "rate"
    close_fee_type: str = "rate"
    night_session: bool = False
    night_session_end_minute: Optional[int] = None


_PRODUCT_RULES: dict[str, ContractRule] = {
    "rb": ContractRule(
        product="rb",
        exchange="SHFE",
        contract_multiplier=10,
        tick_size=1.0,
        margin_rate=0.13,
        open_fee_rate=0.0001,
        close_fee_rate=0.0001,
        price_limit_rate=0.07,
        modeled_overnight_gap_pct=0.03,
        modeled_slippage_bps=2.0,
        night_session=True,
        night_session_end_minute=23 * 60,
    ),
    "cu": ContractRule(
        product="cu",
        exchange="SHFE",
        contract_multiplier=5,
        tick_size=10.0,
        margin_rate=0.12,
        open_fee_rate=0.00005,
        close_fee_rate=0.00005,
        price_limit_rate=0.06,
        modeled_overnight_gap_pct=0.025,
        modeled_slippage_bps=2.0,
        night_session=True,
        night_session_end_minute=60,
    ),
    "i": ContractRule(
        product="i",
        exchange="DCE",
        contract_multiplier=100,
        tick_size=0.5,
        margin_rate=0.15,
        open_fee_rate=0.0001,
        close_fee_rate=0.0001,
        price_limit_rate=0.11,
        modeled_overnight_gap_pct=0.04,
        modeled_slippage_bps=2.0,
        night_session=True,
        night_session_end_minute=23 * 60,
    ),
    "m": ContractRule(
        product="m",
        exchange="DCE",
        contract_multiplier=10,
        tick_size=1.0,
        margin_rate=0.10,
        open_fee_rate=1.5,
        close_fee_rate=1.5,
        price_limit_rate=0.07,
        modeled_overnight_gap_pct=0.03,
        modeled_slippage_bps=2.0,
        open_fee_type="fixed_per_lot",
        close_fee_type="fixed_per_lot",
        night_session=True,
        night_session_end_minute=23 * 60,
    ),
    "if": ContractRule(
        product="if",
        exchange="CFFEX",
        contract_multiplier=300,
        tick_size=0.2,
        margin_rate=0.12,
        open_fee_rate=0.000023,
        close_fee_rate=0.000023,
        price_limit_rate=0.10,
        modeled_overnight_gap_pct=0.03,
        modeled_slippage_bps=2.0,
        night_session=False,
    ),
    "ih": ContractRule(
        product="ih",
        exchange="CFFEX",
        contract_multiplier=300,
        tick_size=0.2,
        margin_rate=0.12,
        open_fee_rate=0.000023,
        close_fee_rate=0.000023,
        price_limit_rate=0.10,
        modeled_overnight_gap_pct=0.03,
        modeled_slippage_bps=2.0,
        night_session=False,
    ),
    "ic": ContractRule(
        product="ic",
        exchange="CFFEX",
        contract_multiplier=200,
        tick_size=0.2,
        margin_rate=0.14,
        open_fee_rate=0.000023,
        close_fee_rate=0.000023,
        price_limit_rate=0.10,
        modeled_overnight_gap_pct=0.03,
        modeled_slippage_bps=2.0,
        night_session=False,
    ),
    "im": ContractRule(
        product="im",
        exchange="CFFEX",
        contract_multiplier=200,
        tick_size=0.2,
        margin_rate=0.14,
        open_fee_rate=0.000023,
        close_fee_rate=0.000023,
        price_limit_rate=0.10,
        modeled_overnight_gap_pct=0.03,
        modeled_slippage_bps=2.0,
        night_session=False,
    ),
}


def normalize_product(symbol: str) -> str:
    """Return the alpha product prefix for a futures symbol."""

    value = str(symbol or "").strip().lower()
    base = value.split(".", 1)[0]
    product = ""
    for ch in base:
        if not ch.isalpha():
            break
        product += ch
    if not product:
        raise ValueError("futures symbol is required")
    return product


def is_executable_contract_symbol(symbol: str) -> bool:
    """Return whether ``symbol`` looks like a concrete tradable contract."""

    value = str(symbol or "").strip().lower()
    base = value.split(".", 1)[0]
    try:
        product = normalize_product(value)
    except ValueError:
        return False
    suffix = base[len(product) :]
    return suffix.isdigit() and len(suffix) >= 3


def get_contract_rule(symbol: str) -> ContractRule:
    """Return static contract rules for ``symbol``."""

    product = normalize_product(symbol)
    try:
        return _PRODUCT_RULES[product]
    except KeyError as exc:
        raise ValueError(f"unsupported China futures product: {product}") from exc


def night_session_end_minute(symbol: str) -> int | None:
    """Return the product-specific night-session close minute, if known."""

    rule = get_contract_rule(symbol)
    if not rule.night_session:
        return None
    return rule.night_session_end_minute


__all__ = [
    "ContractRule",
    "get_contract_rule",
    "is_executable_contract_symbol",
    "night_session_end_minute",
    "normalize_product",
]
