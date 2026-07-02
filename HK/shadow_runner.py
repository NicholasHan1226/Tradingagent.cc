#!/usr/bin/env python3
"""HK Phase D P0 shadow runner writing only to signals/shadow."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.execution.signal_state_machine import SignalStateMachine
from shared.markets.base_tools import BaseShadowRunner
from shared.markets.config_schema import MarketToolConfig
from HK.common import HKConfig
from HK.market_data import HKMarketData
from HK.simulator import HKSimulator


TRADINGAGENT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SIGNALS_ROOT = TRADINGAGENT_ROOT / "signals"


class HKShadowRunner(BaseShadowRunner):
    """Generate HK shadow records and isolate them from execution queues."""

    def __init__(
        self,
        config: MarketToolConfig | None = None,
        market_data: HKMarketData | None = None,
        simulator: HKSimulator | None = None,
        signals_root: Path | str | None = None,
    ) -> None:
        cfg = config or HKConfig()
        data = market_data or HKMarketData(config=cfg)
        sim = simulator or HKSimulator(config=cfg, market_data=data)
        super().__init__("hk", cfg, data, sim)
        self.signals_root = Path(signals_root) if signals_root is not None else DEFAULT_SIGNALS_ROOT

    def get_signals(self, date: str) -> list[dict[str, Any]]:
        signals: list[dict[str, Any]] = []
        for symbol in self.market_data.get_universe(date):
            price = self.market_data.get_latest_price(symbol, date)
            if price is None or price < self.config.universe.min_close:
                continue
            signals.append(
                {
                    "market": "hk",
                    "symbol": symbol,
                    "side": "buy",
                    "quantity": 100,
                    "trade_date": date,
                    "price": price,
                    "strategy_name": "hk_phase_d_p0_shadow",
                }
            )
            if len(signals) >= self.config.risk.max_positions:
                break
        return signals

    def write_shadow_record(self, record: dict[str, Any]) -> dict[str, Any]:
        card = dict(record)
        symbol = str(card.get("symbol") or "").strip().upper()
        date = str(card.get("trade_date") or "")
        card.update(
            {
                "order_id": card.get("order_id") or f"HK-SHADOW-{date}-{symbol}-{uuid.uuid4().hex[:8]}",
                "market": "hk",
                "capital_layer": "shadow",
                "account_type": "shadow",
                "direct_execution": False,
                "real_execution": False,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        machine = SignalStateMachine(self.signals_root / "shadow")
        result = machine.write_pending(card)
        result["queue_scope"] = "shadow"
        return result

    def run_shadow(self, date: str) -> dict[str, Any]:
        signals = self.get_signals(date)
        written = [self.write_shadow_record(signal) for signal in signals]
        return {
            "market": "hk",
            "date": date,
            "status": "ok",
            "signals": len(signals),
            "written": len(written),
            "queue_scope": "shadow",
            "records": written,
        }
