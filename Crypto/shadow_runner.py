#!/usr/bin/env python3
"""Crypto shadow runner for public-data local simulation."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from Crypto.common import CryptoConfig, load_crypto_config, reject_real_execution_payload
from Crypto.market_data import CryptoMarketData
from Crypto.simulator import CryptoSimulator
from shared.execution.shadow_signal import write_shadow_signal
from shared.markets.base_tools import BaseShadowRunner


class CryptoShadowRunner(BaseShadowRunner):
    """Generate Crypto shadow candidates and write only shadow queue cards."""

    def __init__(
        self,
        config: CryptoConfig | None = None,
        market_data: CryptoMarketData | None = None,
        simulator: CryptoSimulator | None = None,
        *,
        signals_dir: Path | str | None = None,
    ) -> None:
        resolved = config or load_crypto_config()
        data = market_data or CryptoMarketData(resolved)
        sim = simulator or CryptoSimulator(resolved, data)
        super().__init__("crypto", resolved, data, sim)
        reject_real_execution_payload(
            {
                "capital_layer": self.config.capital.default_layer,
                "live_broker": self.config.safety.live_broker_enabled,
            },
            context="CryptoShadowRunner.config",
        )
        self.signals_root = Path(signals_dir) if signals_dir is not None else Path.cwd() / "signals"

    def run_shadow(self, date: str) -> dict[str, Any]:
        signals = self.get_signals(date)
        fills: list[dict[str, Any]] = []
        written = 0

        for signal in signals:
            fill = self.simulator.simulate(signal, {"account_type": "shadow", "capital_layer": "shadow"})
            write_result = self.write_shadow_record(
                {
                    "cycle_id": f"crypto-shadow-{date}",
                    "date": date,
                    "market": "crypto",
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
            "market": "crypto",
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
        for symbol in self.market_data.get_universe(date):
            rows = self.market_data.get_daily(symbol, "", date)
            latest = _last_positive_close(rows)
            if latest is None:
                continue
            previous = _previous_positive_close(rows)
            if previous is not None and latest <= previous:
                continue
            signals.append(
                {
                    "order_id": _safe_order_id(f"CRYPTO-SHADOW-{symbol}-{date}"),
                    "market": "crypto",
                    "symbol": str(symbol).upper(),
                    "side": "buy",
                    "quantity": 0.001,
                    "trade_date": date,
                    "score": round(((latest - previous) / previous), 6) if previous else 0.01,
                    "capital_layer": "shadow",
                    "account_type": "shadow",
                    "direct_execution": False,
                    "real_execution": False,
                }
            )
            break
        return signals

    def write_shadow_record(self, record: dict[str, Any]) -> dict[str, Any]:
        reject_real_execution_payload(record, context="CryptoShadowRunner.write_shadow_record")
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
        order_id = _safe_order_id(str(fill.get("order_id") or record.get("order_id") or f"CRYPTO-SHADOW-{symbol}-{uuid.uuid4().hex[:8]}"))
        card = {
            "order_id": order_id,
            "idempotency_key": f"shadow:crypto:{record.get('date', '')}:{symbol}:{order_id}",
            "cycle_id": record.get("cycle_id", f"crypto-shadow-{record.get('date', '')}"),
            "date": record.get("date") or fill.get("trade_date") or fill.get("date"),
            "market": "crypto",
            "symbol": symbol,
            "side": fill.get("side", "buy"),
            "quantity": fill.get("filled_qty", fill.get("quantity")),
            "avg_price": fill.get("avg_price"),
            "status": "pending",
            "queue_scope": "shadow",
            "capital_layer": "shadow",
            "account_type": "shadow",
            "direct_execution": False,
            "real_execution": False,
            "source": "CryptoShadowRunner",
            "simulated_fill": fill,
            "created_at": _now_iso(),
        }
        return write_shadow_signal(card, self.signals_root)


def _last_positive_close(rows: list[dict[str, Any]]) -> float | None:
    for row in reversed(rows):
        value = _to_float(row.get("close"))
        if value is not None and value > 0:
            return value
    return None


def _previous_positive_close(rows: list[dict[str, Any]]) -> float | None:
    seen_latest = False
    for row in reversed(rows):
        value = _to_float(row.get("close"))
        if value is None or value <= 0:
            continue
        if not seen_latest:
            seen_latest = True
            continue
        return value
    return None


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_order_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return cleaned.strip("-") or f"CRYPTO-SHADOW-{uuid.uuid4().hex[:12]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = ["CryptoShadowRunner"]
