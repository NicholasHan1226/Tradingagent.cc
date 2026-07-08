#!/usr/bin/env python3
"""Shared simulated-capital defaults for market runners."""

from __future__ import annotations

import os
from typing import Any

DEFAULT_SIM_CAPITAL_CNY = 200_000.0
DEFAULT_USD_BASE_CAPITAL = 10_000.0
DEFAULT_USD_CNY = 7.2
DEFAULT_HKD_CNY = 0.92
ALLOWED_CNY_TIERS = (50_000.0, 100_000.0, 200_000.0)

_MARKET_TIER_ENV = {
    "ashare": "ASHARE_SIM_CAPITAL_TIER",
    "a_share": "ASHARE_SIM_CAPITAL_TIER",
    "cn": "CN_FUTURES_SIM_CAPITAL_TIER",
    "cn_futures": "CN_FUTURES_SIM_CAPITAL_TIER",
    "cnfutures": "CN_FUTURES_SIM_CAPITAL_TIER",
    "futures": "CN_FUTURES_SIM_CAPITAL_TIER",
}


def _safe_float(value: Any | None, default: float) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
        return parsed if parsed > 0 and parsed == parsed else default
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    return _safe_float(os.environ.get(name), default)


def _normalize_market(market: str) -> str:
    return str(market or "").strip().lower().replace("-", "_")


def _is_allowed_cny_tier(value: Any | None) -> bool:
    return _safe_float(value, 0.0) in ALLOWED_CNY_TIERS


def _resolve_cny_capital(
    market_key: str,
    *,
    capital_cny: float | None = None,
    tier: str | float | None = None,
) -> float:
    """Resolve A-share/CN-futures simulated capital in CNY.

    Priority: explicit ``capital_cny`` > explicit ``tier`` > per-market env tier > default.
    Illegal tier values fall back to ``DEFAULT_SIM_CAPITAL_CNY``.
    """

    if capital_cny is not None:
        return (
            _safe_float(capital_cny, DEFAULT_SIM_CAPITAL_CNY)
            if _is_allowed_cny_tier(capital_cny)
            else DEFAULT_SIM_CAPITAL_CNY
        )
    if tier is not None:
        return (
            _safe_float(tier, DEFAULT_SIM_CAPITAL_CNY)
            if _is_allowed_cny_tier(tier)
            else DEFAULT_SIM_CAPITAL_CNY
        )
    env_name = _MARKET_TIER_ENV.get(market_key)
    if env_name:
        env_value = os.environ.get(env_name)
        if env_value is not None and env_value != "":
            return (
                _safe_float(env_value, DEFAULT_SIM_CAPITAL_CNY)
                if _is_allowed_cny_tier(env_value)
                else DEFAULT_SIM_CAPITAL_CNY
            )
    return DEFAULT_SIM_CAPITAL_CNY


def fx_to_cny(market: str) -> float:
    key = _normalize_market(market)
    if key in {"ashare", "a_share", "cn", "cn_futures", "cnfutures", "futures"}:
        return 1.0
    if key == "hk":
        return _env_float("TRADINGAGENT_HKD_CNY", DEFAULT_HKD_CNY)
    if key == "pm":
        return _env_float("TRADINGAGENT_USDC_CNY", _env_float("TRADINGAGENT_USD_CNY", DEFAULT_USD_CNY))
    if key == "crypto":
        return _env_float("TRADINGAGENT_USDT_CNY", _env_float("TRADINGAGENT_USD_CNY", DEFAULT_USD_CNY))
    if key == "us":
        return _env_float("TRADINGAGENT_USD_CNY", DEFAULT_USD_CNY)
    return 1.0


def default_sim_capital(
    market: str,
    *,
    capital_cny: float | None = None,
    tier: str | float | None = None,
) -> float:
    """Return the default simulated capital for a market in its native currency.

    - US/Crypto/PM default to ``DEFAULT_USD_BASE_CAPITAL`` (USD/USDT/USDC).
    - A-share and CN futures default to ``DEFAULT_SIM_CAPITAL_CNY`` CNY, with
      optional 50k/100k/200k tiers via ``capital_cny``, ``tier``, or per-market env.
    """

    key = _normalize_market(market)
    if key in {"ashare", "a_share", "cn", "cn_futures", "cnfutures", "futures"}:
        return round(_resolve_cny_capital(key, capital_cny=capital_cny, tier=tier), 6)

    fx = fx_to_cny(key)
    if capital_cny is not None:
        cny = _safe_float(capital_cny, DEFAULT_SIM_CAPITAL_CNY)
        return round(cny / fx, 6) if fx > 0 else round(cny, 6)

    if key in {"us", "crypto", "pm"}:
        return round(DEFAULT_USD_BASE_CAPITAL, 6)

    return round(DEFAULT_SIM_CAPITAL_CNY / fx, 6) if fx > 0 else float(DEFAULT_SIM_CAPITAL_CNY)


__all__ = [
    "DEFAULT_SIM_CAPITAL_CNY",
    "DEFAULT_USD_BASE_CAPITAL",
    "DEFAULT_USD_CNY",
    "DEFAULT_HKD_CNY",
    "ALLOWED_CNY_TIERS",
    "default_sim_capital",
    "fx_to_cny",
]
