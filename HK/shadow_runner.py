#!/usr/bin/env python3
"""HK shadow runner using local public-data simulation only."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from HK.common import HKConfig
from HK.market_data import HKMarketData
from HK.simulator import HKSimulator
from shared.execution.signal_state_machine import SignalStateConflict, SignalStateMachine
from shared.markets.base_tools import BaseShadowRunner
from shared.markets.config_schema import MarketToolConfig
from shared.markets.safety import reject_real_execution_payload


class HKShadowRunner(BaseShadowRunner):
    """Generate HK equity shadow candidates and write shadow pending cards."""

    def __init__(
        self,
        config: MarketToolConfig | None = None,
        market_data: HKMarketData | None = None,
        simulator: HKSimulator | None = None,
        *,
        signals_root: Path | str | None = None,
    ) -> None:
        resolved = config or HKConfig()
        data = market_data or HKMarketData(config=resolved)
        sim = simulator or HKSimulator(config=resolved, market_data=data)
        super().__init__("hk", resolved, data, sim)
        self.signals_root = Path(signals_root) if signals_root is not None else Path.cwd() / "signals"

    def run_shadow(self, date: str) -> dict[str, Any]:
        signals = self.get_signals(date)
        fills: list[dict[str, Any]] = []
        written = 0

        for signal in signals:
            fill = self.simulator.simulate(signal, {"account_type": "shadow", "capital_layer": "shadow"})
            write_result = self.write_shadow_record(
                {
                    "cycle_id": f"hk-shadow-{date}",
                    "date": date,
                    "market": "hk",
                    "signal": signal,
                    "positions": [fill],
                }
            )
            fill["shadow_write"] = write_result
            fills.append(fill)
            if write_result.get("status") == "pending":
                written += 1

        return {
            "status": "ok",
            "market": "hk",
            "date": date,
            "mode": "shadow",
            "capital_layer": "shadow",
            "account_type": "shadow",
            "real_execution": False,
            "signals_count": len(signals),
            "positions": fills,
            "written": written,
            "pending_count": written,
        }

    def get_signals(self, date: str) -> list[dict[str, Any]]:
        signals: list[dict[str, Any]] = []
        limit = max(1, int(self.config.risk.max_positions))
        for symbol in self.market_data.get_universe(date):
            price = self.market_data.get_latest_price(symbol, date)
            if price is None or price <= 0:
                continue
            normalized = self.market_data.adapter.normalize_symbol(symbol)
            signals.append(
                {
                    "order_id": _safe_order_id(f"HK-SHADOW-{normalized}-{date}"),
                    "market": "hk",
                    "symbol": normalized,
                    "side": "buy",
                    "quantity": 100,
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
        reject_real_execution_payload(record, context="HKShadowRunner.write_shadow_record")
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
        raw_symbol = str(fill.get("symbol") or record.get("symbol") or "")
        symbol = self.market_data.adapter.normalize_symbol(raw_symbol) if raw_symbol else raw_symbol
        order_id = _safe_order_id(str(fill.get("order_id") or record.get("order_id") or f"HK-SHADOW-{symbol}-{uuid.uuid4().hex[:8]}"))
        card = {
            "order_id": order_id,
            "idempotency_key": f"shadow:hk:{record.get('date', '')}:{symbol}:{order_id}",
            "cycle_id": record.get("cycle_id", f"hk-shadow-{record.get('date', '')}"),
            "date": record.get("date") or fill.get("trade_date") or fill.get("date"),
            "market": "hk",
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
            "source": "HKShadowRunner",
            "simulated_fill": fill,
            "created_at": _now_iso(),
        }
        state = SignalStateMachine(Path(self.signals_root) / "shadow")
        try:
            result = state.write_pending(card)
        except SignalStateConflict as exc:
            return {
                "order_id": order_id,
                "status": "duplicate",
                "queue_scope": "shadow",
                "message": str(exc),
            }
        result["queue_scope"] = "shadow"
        result["capital_layer"] = "shadow"
        return result


def _safe_order_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return cleaned.strip("-") or f"HK-SHADOW-{uuid.uuid4().hex[:12]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = ["HKShadowRunner"]
