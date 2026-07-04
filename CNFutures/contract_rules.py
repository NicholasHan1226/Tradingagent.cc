#!/usr/bin/env python3
"""Static China futures contract rules for simulation.

These defaults are bootstrap rules for local simulation only. Production
trading must source live exchange / futures-company contract metadata.
"""

from __future__ import annotations

from dataclasses import dataclass


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
    night_session: bool = False


_PRODUCT_RULES: dict[str, ContractRule] = {
    "rb": ContractRule(
        product="rb",
        exchange="SHFE",
        contract_multiplier=10,
        tick_size=1.0,
        margin_rate=0.13,
        open_fee_rate=0.0001,
        close_fee_rate=0.0001,
        night_session=True,
    ),
    "cu": ContractRule(
        product="cu",
        exchange="SHFE",
        contract_multiplier=5,
        tick_size=10.0,
        margin_rate=0.12,
        open_fee_rate=0.00005,
        close_fee_rate=0.00005,
        night_session=True,
    ),
    "i": ContractRule(
        product="i",
        exchange="DCE",
        contract_multiplier=100,
        tick_size=0.5,
        margin_rate=0.15,
        open_fee_rate=0.0001,
        close_fee_rate=0.0001,
        night_session=True,
    ),
    "m": ContractRule(
        product="m",
        exchange="DCE",
        contract_multiplier=10,
        tick_size=1.0,
        margin_rate=0.10,
        open_fee_rate=1.5,
        close_fee_rate=1.5,
        night_session=True,
    ),
    "if": ContractRule(
        product="if",
        exchange="CFFEX",
        contract_multiplier=300,
        tick_size=0.2,
        margin_rate=0.12,
        open_fee_rate=0.000023,
        close_fee_rate=0.000023,
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


def get_contract_rule(symbol: str) -> ContractRule:
    """Return static contract rules for ``symbol``."""

    product = normalize_product(symbol)
    try:
        return _PRODUCT_RULES[product]
    except KeyError as exc:
        raise ValueError(f"unsupported China futures product: {product}") from exc


__all__ = ["ContractRule", "get_contract_rule", "normalize_product"]
