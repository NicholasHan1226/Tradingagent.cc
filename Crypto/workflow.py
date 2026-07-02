#!/usr/bin/env python3
"""Crypto Phase D workflow entrypoints."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from Crypto.common import CryptoConfig, load_crypto_config, reject_real_execution_payload
from Crypto.market_data import CryptoMarketData
from Crypto.shadow_runner import CryptoShadowRunner
from Crypto.simulator import CryptoSimulator


class CryptoWorkflow:
    """Wire public Crypto data, local mock simulation, and shadow queue writes."""

    def __init__(
        self,
        config: CryptoConfig | None = None,
        *,
        reader: Any | None = None,
        signals_dir: Path | str | None = None,
    ) -> None:
        self.config = config or load_crypto_config()
        reject_real_execution_payload(
            {
                "capital_layer": self.config.capital.default_layer,
                "live_broker": self.config.safety.live_broker_enabled,
            },
            context="CryptoWorkflow.config",
        )
        self.market_data = CryptoMarketData(self.config, reader=reader)
        self.simulator = CryptoSimulator(self.config, self.market_data)
        self.shadow_runner = CryptoShadowRunner(
            self.config,
            self.market_data,
            self.simulator,
            signals_dir=signals_dir,
        )

    def run_crypto_shadow_cycle(self, as_of: str) -> dict[str, Any]:
        result = self.shadow_runner.run_shadow(as_of)
        result.update(
            {
                "workflow": "crypto_shadow_cycle",
                "currency": self.config.capital.currency,
                "session": self.config.session.type,
                "public_data_only": True,
                "real_execution": False,
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )
        return result


def run_crypto_shadow_cycle(as_of: str) -> dict[str, Any]:
    """Run one Crypto shadow cycle using the checked-in Crypto config."""

    return CryptoWorkflow().run_crypto_shadow_cycle(as_of)


__all__ = ["CryptoWorkflow", "run_crypto_shadow_cycle"]
