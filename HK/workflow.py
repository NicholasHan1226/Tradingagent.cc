#!/usr/bin/env python3
"""HK Phase D P0 workflow entry points."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from HK.common import HKConfig
from HK.market_data import HKMarketData
from HK.shadow_runner import HKShadowRunner
from HK.simulator import HKSimulator
from shared.markets.config_schema import MarketToolConfig


class HKWorkflow:
    """P0 HK shadow workflow with no live broker capability."""

    def __init__(
        self,
        config: MarketToolConfig | None = None,
        reader: Any | None = None,
        signals_root: Path | str | None = None,
    ) -> None:
        self.config = config or HKConfig()
        self.market_data = HKMarketData(config=self.config, reader=reader)
        self.simulator = HKSimulator(config=self.config, market_data=self.market_data)
        self.shadow_runner = HKShadowRunner(
            config=self.config,
            market_data=self.market_data,
            simulator=self.simulator,
            signals_root=signals_root,
        )

    def run_hk_shadow_cycle(self, as_of: str) -> dict[str, Any]:
        return self.shadow_runner.run_shadow(as_of)


def run_hk_shadow_cycle(
    as_of: str,
    *,
    reader: Any | None = None,
    signals_root: Path | str | None = None,
) -> dict[str, Any]:
    return HKWorkflow(reader=reader, signals_root=signals_root).run_hk_shadow_cycle(as_of)


__all__ = ["HKWorkflow", "run_hk_shadow_cycle"]
