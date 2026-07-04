#!/usr/bin/env python3
"""PM shadow runner for local probability-market simulation."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PM.common import clamp_probability, load_pm_config
from PM.market_data import PMMarketData
from PM.simulator import PMSimulator
from shared.execution.signal_state_machine import SignalStateConflict, SignalStateMachine
from shared.markets.base_tools import BaseShadowRunner
from shared.markets.config_schema import MarketToolConfig
from shared.markets.safety import reject_real_execution_payload


class PMShadowRunner(BaseShadowRunner):
    """Generate PM shadow candidates and write them to the shadow queue."""

    def __init__(
        self,
        config: MarketToolConfig | None = None,
        market_data: PMMarketData | None = None,
        simulator: PMSimulator | None = None,
    ) -> None:
        resolved = _resolve_config(config)
        data = market_data or PMMarketData(resolved)
        sim = simulator or PMSimulator(config=resolved, market_data=data)
        super().__init__("pm", resolved, data, sim)
        self.signals_root = Path.cwd() / "signals"

    def run_shadow(self, date: str) -> dict[str, Any]:
        universe = self.market_data.get_universe(date)
        signals = self.get_signals(date)
        positions: list[dict[str, Any]] = []
        written = 0

        for signal in signals:
            fill = self.simulator.simulate(signal, {"account_id": "pm_shadow", "account_type": "shadow"})
            fill["score"] = signal.get("score", 0.0)
            write_result = self.write_shadow_record(
                {
                    "cycle_id": f"pm-shadow-{date}",
                    "date": date,
                    "market": "pm",
                    "signal": signal,
                    "positions": [fill],
                }
            )
            fill["shadow_write"] = write_result
            positions.append(fill)
            if write_result.get("status") == "pending":
                written += 1

        return {
            "status": "ok",
            "market": "pm",
            "date": date,
            "mode": "shadow",
            "capital_layer": "shadow",
            "real_execution": False,
            "universe_size": len(universe),
            "signals_count": len(signals),
            "candidates_count": len(signals),
            "positions": positions,
            "written": written,
            "pending_count": written,
        }

    def get_signals(self, date: str) -> list[dict[str, Any]]:
        signals: list[dict[str, Any]] = []
        max_positions = max(1, int(self.config.risk.max_positions))
        for market_id in self.market_data.get_universe(date):
            price = self.market_data.get_latest_price(market_id, date)
            if price is None:
                price = 0.5
            price = clamp_probability(price)
            if price <= 0 or price >= 1:
                continue
            edge = max(0.01, 0.55 - price)
            signals.append(
                {
                    "order_id": _safe_order_id(f"PM-SHADOW-{market_id}-{date}"),
                    "market": "pm",
                    "market_id": str(market_id),
                    "symbol": str(market_id),
                    "side": "buy",
                    "outcome": "yes",
                    "quantity": 1,
                    "price": clamp_probability(price + 0.02),
                    "trade_date": date,
                    "date": date,
                    "score": round(edge, 4),
                    "capital_layer": "shadow",
                    "account_type": "shadow",
                    "direct_execution": False,
                    "real_execution": False,
                }
            )
            if len(signals) >= max_positions:
                break
        return signals

    def write_shadow_record(self, record: dict[str, Any]) -> dict[str, Any]:
        reject_real_execution_payload(record, context="PMShadowRunner.write_shadow_record")
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

    def _write_one(self, record: dict[str, Any], position: dict[str, Any]) -> dict[str, Any]:
        order_id = _safe_order_id(str(position.get("order_id") or record.get("order_id") or f"PM-SHADOW-{uuid.uuid4().hex[:12]}"))
        card = {
            "order_id": order_id,
            "idempotency_key": f"shadow:pm:{record.get('date', '')}:{order_id}",
            "cycle_id": record.get("cycle_id", f"pm-shadow-{record.get('date', '')}"),
            "date": record.get("date") or position.get("trade_date") or position.get("date"),
            "market": "pm",
            "market_id": position.get("market_id") or record.get("market_id") or position.get("symbol"),
            "side": position.get("side", "buy"),
            "outcome": position.get("outcome", "YES"),
            "quantity": position.get("quantity", position.get("filled_qty", 1)),
            "fill_price": position.get("fill_price", position.get("avg_price", position.get("price"))),
            "status": "pending",
            "queue_scope": "shadow",
            "capital_layer": "shadow",
            "account_type": "shadow",
            "direct_execution": False,
            "real_execution": False,
            "live_clob": False,
            "source": "PMShadowRunner",
            "simulated_fill": position,
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


def _resolve_config(config: Any | None) -> MarketToolConfig:
    if config is None:
        return load_pm_config().to_market_tool_config()
    to_market_tool_config = getattr(config, "to_market_tool_config", None)
    if callable(to_market_tool_config):
        return to_market_tool_config()
    return config


def _safe_order_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return cleaned.strip("-") or f"PM-SHADOW-{uuid.uuid4().hex[:12]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = ["PMShadowRunner"]
