#!/usr/bin/env python3
"""US Phase D P0 workflow entry points."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from shared.markets.config_schema import MarketToolConfig
from US.common import USConfig
from US.market_data import USMarketData
from US.shadow_runner import USShadowRunner
from US.simulator import USSimulator


class USWorkflow:
    """P0 US shadow workflow with no live broker capability."""

    def __init__(
        self,
        config: MarketToolConfig | None = None,
        reader: Any | None = None,
        signals_root: Path | str | None = None,
    ) -> None:
        self.config = config or USConfig()
        self.market_data = USMarketData(config=self.config, reader=reader)
        self.simulator = USSimulator(config=self.config, market_data=self.market_data)
        self.shadow_runner = USShadowRunner(
            config=self.config,
            market_data=self.market_data,
            simulator=self.simulator,
            signals_root=signals_root,
        )

    def run_us_shadow_cycle(self, as_of: str) -> dict[str, Any]:
        return self.shadow_runner.run_shadow(as_of)


def run_us_shadow_cycle(
    as_of: str,
    *,
    reader: Any | None = None,
    signals_root: Path | str | None = None,
) -> dict[str, Any]:
    return USWorkflow(reader=reader, signals_root=signals_root).run_us_shadow_cycle(as_of)
