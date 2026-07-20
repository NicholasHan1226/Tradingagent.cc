#!/usr/bin/env python3
"""Crypto fixture opening-policy candidate.

This module is the single code source for the local fixture account's opening
baseline. It grants no execution, durable-receipt, production, or live capital
authority. ``Crypto/config.yaml`` intentionally does not repeat the amount.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


CRYPTO_CAPITAL_CONTRACT = "tradingagent.crypto.capital_policy.v1"
CRYPTO_CAPITAL_AUTHORITY_ID = "crypto-capital-v1"
CRYPTO_CAPITAL_ACCOUNT_ID = "crypto_sim"
CRYPTO_CAPITAL_GENERATION = 1
CRYPTO_CAPITAL_CURRENCY = "USDT"
CRYPTO_CAPITAL_LAYER = "simulated"
CRYPTO_INITIAL_CAPITAL_USDT = Decimal("10000")

# Compatibility projection for the existing config dataclass, which currently
# accepts native Python numbers.  It is derived from the Decimal authority
# above and must never become an independently configured amount.
DEFAULT_CRYPTO_SIM_CAPITAL_USDT = float(CRYPTO_INITIAL_CAPITAL_USDT)


@dataclass(frozen=True)
class CryptoCapitalPolicy:
    """Immutable opening baseline for the isolated local fixture account."""

    contract: str = CRYPTO_CAPITAL_CONTRACT
    authority_id: str = CRYPTO_CAPITAL_AUTHORITY_ID
    account_id: str = CRYPTO_CAPITAL_ACCOUNT_ID
    generation: int = CRYPTO_CAPITAL_GENERATION
    generation_scope: str = "local_fixture_opening_baseline_only"
    currency: str = CRYPTO_CAPITAL_CURRENCY
    initial_cash: Decimal = CRYPTO_INITIAL_CAPITAL_USDT
    capital_layer: str = CRYPTO_CAPITAL_LAYER
    account_type: str = "simulated"
    real_trading_enabled: bool = False

    def __post_init__(self) -> None:
        if self.contract != CRYPTO_CAPITAL_CONTRACT:
            raise ValueError("Crypto capital contract is immutable")
        if self.authority_id != CRYPTO_CAPITAL_AUTHORITY_ID:
            raise ValueError("Crypto capital authority_id is immutable")
        if self.account_id != CRYPTO_CAPITAL_ACCOUNT_ID:
            raise ValueError("Crypto capital account_id is immutable")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation != CRYPTO_CAPITAL_GENERATION
        ):
            raise ValueError("Crypto capital generation is immutable")
        if self.generation_scope != "local_fixture_opening_baseline_only":
            raise ValueError("Crypto fixture generation scope is immutable")
        if self.currency != CRYPTO_CAPITAL_CURRENCY:
            raise ValueError("Crypto capital currency must be USDT")
        if (
            not isinstance(self.initial_cash, Decimal)
            or self.initial_cash != CRYPTO_INITIAL_CAPITAL_USDT
        ):
            raise ValueError("Crypto initial capital must be 10000 USDT")
        if self.capital_layer != "simulated" or self.account_type != "simulated":
            raise ValueError("Crypto capital policy must remain simulated")
        if self.real_trading_enabled is not False:
            raise ValueError("Crypto real trading must remain disabled")


CRYPTO_CAPITAL_POLICY = CryptoCapitalPolicy()

__all__ = [
    "CRYPTO_CAPITAL_ACCOUNT_ID",
    "CRYPTO_CAPITAL_AUTHORITY_ID",
    "CRYPTO_CAPITAL_CONTRACT",
    "CRYPTO_CAPITAL_CURRENCY",
    "CRYPTO_CAPITAL_GENERATION",
    "CRYPTO_CAPITAL_LAYER",
    "CRYPTO_CAPITAL_POLICY",
    "CRYPTO_INITIAL_CAPITAL_USDT",
    "CryptoCapitalPolicy",
    "DEFAULT_CRYPTO_SIM_CAPITAL_USDT",
]
