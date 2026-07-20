#!/usr/bin/env python3
"""Shared Crypto Phase D configuration helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from Crypto.capital_policy import DEFAULT_CRYPTO_SIM_CAPITAL_USDT
from shared.markets.config_schema import (
    CapitalConfig,
    MarketToolConfig,
    SessionConfig,
    validate_market_config,
)

MARKET = "crypto"
TRADINGDATAS_MARKET_CONTEXT = "Crypto"
CURRENCY = "USDT"
SESSION_TYPE = "24x7"


@dataclass(frozen=True)
class CryptoConfig(MarketToolConfig):
    """Crypto market config constrained to public-data shadow/simulated tools."""

    market: str = MARKET
    capital: CapitalConfig | dict[str, Any] = field(
        default_factory=lambda: CapitalConfig(
            initial_capital=DEFAULT_CRYPTO_SIM_CAPITAL_USDT,
            currency=CURRENCY,
        )
    )
    session: SessionConfig | dict[str, Any] = field(
        default_factory=lambda: SessionConfig(timezone="UTC", type=SESSION_TYPE)
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        validate_market_config(self)
        if self.market != MARKET:
            raise ValueError(f"CryptoConfig.market must be {MARKET!r}")
        if self.capital.currency != CURRENCY:
            raise ValueError(f"CryptoConfig currency must be {CURRENCY}")
        if self.capital.initial_capital != DEFAULT_CRYPTO_SIM_CAPITAL_USDT:
            raise ValueError(
                "CryptoConfig initial_capital must be "
                f"{DEFAULT_CRYPTO_SIM_CAPITAL_USDT:g} {CURRENCY}"
            )
        if self.session.type != SESSION_TYPE:
            raise ValueError("CryptoConfig session.type must be 24x7")


def load_crypto_config(root: Path | str | None = None) -> CryptoConfig:
    """Load ``Crypto/config.yaml`` as a Crypto-specific MarketToolConfig."""

    base = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    path = base / "Crypto" / "config.yaml"
    if not path.exists():
        path = Path(__file__).resolve().parent / "config.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Crypto config must be a mapping: {path}")
    payload.setdefault("market", MARKET)
    capital = payload.setdefault("capital", {})
    if not isinstance(capital, dict):
        raise ValueError(f"Crypto capital config must be a mapping: {path}")
    capital.setdefault("initial_capital", DEFAULT_CRYPTO_SIM_CAPITAL_USDT)
    return CryptoConfig(**payload)


def reject_real_execution_payload(
    payload: dict[str, Any] | None, *, context: str
) -> None:
    """Reject order/account/config fields that imply live exchange execution."""

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
    }
    present = sorted(
        key
        for key in unsafe_keys
        if key in payload and payload.get(key) not in (None, "", False)
    )
    if present:
        raise RuntimeError(
            f"{context}: Crypto Phase D is public-data local mock only; unsafe fields={present}"
        )

    for key in ("capital_layer", "account_type", "execution_mode", "mode"):
        value = str(payload.get(key) or "").strip().lower()
        if value in {"real", "live", "broker", "exchange"}:
            raise RuntimeError(
                f"{context}: real/live execution is rejected for Crypto Phase D"
            )


__all__ = [
    "CURRENCY",
    "MARKET",
    "SESSION_TYPE",
    "TRADINGDATAS_MARKET_CONTEXT",
    "CryptoConfig",
    "load_crypto_config",
    "reject_real_execution_payload",
]
