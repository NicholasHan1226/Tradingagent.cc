#!/usr/bin/env python3
"""Owned market-lane simulated-capital defaults.

The values are native-currency account bootstrap amounts, not an FX conversion
service and not permission to aggregate capital across markets.
"""

from __future__ import annotations

from Crypto.capital_policy import DEFAULT_CRYPTO_SIM_CAPITAL_USDT
from shared.governance.market_lanes import canonical_runtime_market

DEFAULT_SIM_CAPITAL_CNY = 50_000.0
ALLOWED_CNY_TIERS = (50_000.0,)


def _normalize_market(market: str) -> str:
    return canonical_runtime_market(market)


def _resolve_cny_capital(
    market_key: str,
    *,
    capital_cny: float | None = None,
    tier: str | float | None = None,
) -> float:
    """Resolve A-share/CN-futures simulated capital in CNY.

    Both domestic markets have one current fresh-start 50k authority.  Legacy
    explicit 100k/200k tier inputs are deliberately ignored so a stale style,
    fixture, or environment cannot create a parallel runtime capital truth.
    """
    del market_key, capital_cny, tier
    return DEFAULT_SIM_CAPITAL_CNY


def default_sim_capital(
    market: str,
    *,
    capital_cny: float | None = None,
    tier: str | float | None = None,
) -> float:
    """Return one owned lane's fixed bootstrap capital in native currency.

    A-share and CN futures each have an independent 50,000 CNY authority.
    Crypto receives a compatibility projection derived from its isolated local
    fixture opening policy; this function is not a current/runtime capital
    authority. No value here converts or combines those accounts, and
    unknown/retired markets fail closed.
    """

    key = _normalize_market(market)
    if key in {"ashare", "cn_futures"}:
        return round(_resolve_cny_capital(key, capital_cny=capital_cny, tier=tier), 6)
    if key == "crypto":
        if capital_cny is not None or tier is not None:
            raise ValueError(
                "crypto capital is USDT-native; CNY/tier overrides are not allowed"
            )
        return DEFAULT_CRYPTO_SIM_CAPITAL_USDT
    raise ValueError(f"unsupported simulated-capital market: {key}")


__all__ = [
    "DEFAULT_SIM_CAPITAL_CNY",
    "DEFAULT_CRYPTO_SIM_CAPITAL_USDT",
    "ALLOWED_CNY_TIERS",
    "default_sim_capital",
]
