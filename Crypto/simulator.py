#!/usr/bin/env python3
"""Fail-closed compatibility shell for the retired Crypto simulator."""

from __future__ import annotations

from typing import Any, NoReturn

from Crypto.common import (
    CryptoConfig,
    load_crypto_config,
    reject_real_execution_payload,
)
from Crypto.market_data import CryptoMarketData
from Crypto.sim_executor import CryptoLegacyExecutionRetired
from Crypto.capital_policy import CRYPTO_CAPITAL_AUTHORITY_ID
from shared.markets.base_tools import BaseSimulator


class CryptoSimulator(BaseSimulator):
    """Keep the legacy import surface while denying all direct fills."""

    def __init__(
        self,
        config: CryptoConfig | None = None,
        market_data: CryptoMarketData | None = None,
    ) -> None:
        resolved_config = config or load_crypto_config()
        super().__init__(
            "crypto",
            resolved_config,
            market_data or CryptoMarketData(resolved_config),
        )

    def simulate(self, order: dict[str, Any], account: dict[str, Any]) -> NoReturn:
        """Reject the old no-ledger fill path."""

        reject_real_execution_payload(order, context="CryptoSimulator.order")
        reject_real_execution_payload(account, context="CryptoSimulator.account")
        raise CryptoLegacyExecutionRetired(
            "CryptoSimulator:legacy_runtime_retired; execution requires the "
            f"{CRYPTO_CAPITAL_AUTHORITY_ID} append-only ledger runtime"
        )

    def fill_price(self, symbol: str, date: str) -> float | None:
        """Retain read-only price lookup for historical diagnostics."""

        return self.market_data.get_latest_price(symbol, date)


__all__ = ["CryptoSimulator"]
