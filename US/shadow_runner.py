#!/usr/bin/env python3
"""US shadow runner using local public-data simulation only."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.execution.shadow_signal import write_shadow_signal
from shared.markets.base_tools import BaseShadowRunner
from shared.markets.config_schema import MarketToolConfig
from shared.markets.safety import reject_real_execution_payload
from US.common import USConfig
from US.market_data import USMarketData
from US.simulator import USSimulator


class USShadowRunner(BaseShadowRunner):
    """Generate US equity shadow candidates and write shadow pending cards."""

    def __init__(
        self,
        config: MarketToolConfig | None = None,
        market_data: USMarketData | None = None,
        simulator: USSimulator | None = None,
        *,
        signals_root: Path | str | None = None,
    ) -> None:
        resolved = config or USConfig()
        data = market_data or USMarketData(config=resolved)
        sim = simulator or USSimulator(config=resolved, market_data=data)
        super().__init__("us", resolved, data, sim)
        self.signals_root = Path(signals_root) if signals_root is not None else Path.cwd() / "signals"

    def run_shadow(self, date: str) -> dict[str, Any]:
        signals = self.get_signals(date)
        fills: list[dict[str, Any]] = []
        written = 0

        for signal in signals:
            fill = self.simulator.simulate(signal, {"account_type": "shadow", "capital_layer": "shadow"})
            write_result = self.write_shadow_record(
                {
                    "cycle_id": f"us-shadow-{date}",
                    "date": date,
                    "market": "us",
                    "signal": signal,
                    "positions": [fill],
                }
            )
            fill["shadow_write"] = write_result
            fills.append(fill)
            if write_result.get("status") in {"pending", "filled", "partial"}:
                written += 1

        return {
            "status": "ok",
            "market": "us",
            "date": date,
            "mode": "shadow",
            "capital_layer": "shadow",
            "account_type": "shadow",
            "real_execution": False,
            "signals_count": len(signals),
            "positions": fills,
            "written": written,
            "pending_count": sum(1 for item in fills if item.get("shadow_write", {}).get("status") == "pending"),
            "filled_count": sum(1 for item in fills if item.get("shadow_write", {}).get("status") in {"filled", "partial"}),
        }

    def get_signals(self, date: str) -> list[dict[str, Any]]:
        signals: list[dict[str, Any]] = []
        limit = max(1, int(self.config.risk.max_positions))
        for symbol in self.market_data.get_universe(date):
            price = self.market_data.get_latest_price(symbol, date)
            if price is None or price <= 0:
                continue
            signals.append(
                {
                    "order_id": _safe_order_id(f"US-SHADOW-{symbol}-{date}"),
                    "market": "us",
                    "symbol": str(symbol).upper(),
                    "side": "buy",
                    "quantity": 1,
                    "trade_date": date,
                    "price": price,
                    "score": 0.01,
                    "capital_layer": "shadow",
                    "account_type": "shadow",
                    "direct_execution": False,
                    "real_execution": False,
                }
            )
            if len(signals) >= limit:
                break
        return signals

    def write_shadow_record(self, record: dict[str, Any]) -> dict[str, Any]:
        reject_real_execution_payload(record, context="USShadowRunner.write_shadow_record")
        positions = record.get("positions")
        rows = positions if isinstance(positions, list) and positions else [record]
        results = [self._write_one(record, row) for row in rows if isinstance(row, dict)]
        if not results:
            return {"status": "empty", "queue_scope": "shadow", "written": 0}
        if len(results) == 1:
            return results[0]
        return {
            "status": "ok",
            "queue_scope": "shadow",
            "written": sum(1 for item in results if item.get("status") == "pending"),
            "results": results,
        }

    def _write_one(self, record: dict[str, Any], fill: dict[str, Any]) -> dict[str, Any]:
        symbol = str(fill.get("symbol") or record.get("symbol") or "").upper()
        order_id = _safe_order_id(str(fill.get("order_id") or record.get("order_id") or f"US-SHADOW-{symbol}-{uuid.uuid4().hex[:8]}"))
        card = {
            "order_id": order_id,
            "idempotency_key": f"shadow:us:{record.get('date', '')}:{symbol}:{order_id}",
            "cycle_id": record.get("cycle_id", f"us-shadow-{record.get('date', '')}"),
            "date": record.get("date") or fill.get("trade_date") or fill.get("date"),
            "market": "us",
            "symbol": symbol,
            "side": fill.get("side", "buy"),
            "quantity": fill.get("quantity"),
            "avg_price": fill.get("avg_price"),
            "currency": fill.get("currency", self.config.capital.currency),
            "status": "pending",
            "queue_scope": "shadow",
            "capital_layer": "shadow",
            "account_type": "shadow",
            "direct_execution": False,
            "real_execution": False,
            "source": "USShadowRunner",
            "simulated_fill": fill,
            "created_at": _now_iso(),
        }
        return write_shadow_signal(card, self.signals_root)


def _safe_order_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return cleaned.strip("-") or f"US-SHADOW-{uuid.uuid4().hex[:12]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = ["USShadowRunner"]
