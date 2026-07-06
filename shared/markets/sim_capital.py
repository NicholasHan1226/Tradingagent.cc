#!/usr/bin/env python3
"""Shared simulated-capital defaults for market runners."""

from __future__ import annotations

import os

DEFAULT_SIM_CAPITAL_CNY = 200_000.0
DEFAULT_USD_CNY = 7.2
DEFAULT_HKD_CNY = 0.92


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
        return value if value > 0 and value == value else default
    except (TypeError, ValueError):
        return default


def fx_to_cny(market: str) -> float:
    key = str(market or "").strip().lower().replace("-", "_")
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


def default_sim_capital(market: str, *, capital_cny: float = DEFAULT_SIM_CAPITAL_CNY) -> float:
    fx = fx_to_cny(market)
    return round(float(capital_cny) / fx, 6) if fx > 0 else float(capital_cny)


__all__ = ["DEFAULT_SIM_CAPITAL_CNY", "DEFAULT_USD_CNY", "DEFAULT_HKD_CNY", "default_sim_capital", "fx_to_cny"]
