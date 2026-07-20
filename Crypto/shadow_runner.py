#!/usr/bin/env python3
"""Fail-closed tombstone for the retired Crypto shadow queue writer."""

from __future__ import annotations

from pathlib import Path
from typing import Any, NoReturn

from Crypto.capital_policy import CRYPTO_CAPITAL_AUTHORITY_ID
from Crypto.common import CryptoConfig
from Crypto.market_data import CryptoMarketData
from Crypto.sim_executor import CryptoLegacyExecutionRetired
from Crypto.simulator import CryptoSimulator


def _raise_retired() -> NoReturn:
    raise CryptoLegacyExecutionRetired(
        "CryptoShadowRunner:legacy_runtime_retired; cannot write signals or fills; use "
        f"the {CRYPTO_CAPITAL_AUTHORITY_ID} capital-backed fixture runtime"
    )


class CryptoShadowRunner:
    """Preserve the old symbol while denying construction and all writes."""

    def __init__(
        self,
        config: CryptoConfig | None = None,
        market_data: CryptoMarketData | None = None,
        simulator: CryptoSimulator | None = None,
        *,
        signals_dir: Path | str | None = None,
    ) -> None:
        del config, market_data, simulator, signals_dir
        _raise_retired()

    def run_shadow(self, date: str) -> NoReturn:
        del date
        _raise_retired()

    def get_signals(self, date: str) -> NoReturn:
        del date
        _raise_retired()

    def write_shadow_record(self, record: dict[str, Any]) -> NoReturn:
        del record
        _raise_retired()


__all__ = ["CryptoShadowRunner"]
