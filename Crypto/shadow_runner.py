#!/usr/bin/env python3
"""Crypto Phase D shadow runner writing isolated signal cards."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from Crypto.common import CryptoConfig, load_crypto_config, reject_real_execution_payload
from Crypto.market_data import CryptoMarketData
from Crypto.simulator import CryptoSimulator
from shared.execution.signal_state_machine import SignalStateConflict, SignalStateMachine
from shared.markets.base_tools import BaseShadowRunner
from shared.orchestrator import SIGNALS_DIR


class CryptoShadowRunner(BaseShadowRunner):
    """Generate Crypto shadow cards under ``signals/shadow/*`` only."""

    def __init__(
        self,
        config: CryptoConfig | None = None,
        market_data: CryptoMarketData | None = None,
        simulator: CryptoSimulator | None = None,
        signals_dir: Path | str | None = None,
    ) -> None:
        resolved_config = config or load_crypto_config()
        resolved_market_data = market_data or CryptoMarketData(resolved_config)
        super().__init__("crypto", resolved_config, resolved_market_data, simulator or CryptoSimulator(resolved_config, resolved_market_data))
        self.signals_dir = Path(signals_dir) if signals_dir is not None else SIGNALS_DIR
        self.shadow_dir = self.signals_dir / "shadow"

    def run_shadow(self, date: str) -> dict[str, Any]:
        signals = self.get_signals(date)
        records = [self.write_shadow_record(signal) for signal in signals]
        return {
            "market": "crypto",
            "date": date,
            "capital_layer": "shadow",
            "account": "crypto_shadow",
            "signal_count": len(signals),
            "pending_count": sum(1 for record in records if record.get("status") == "pending"),
            "duplicate_count": sum(1 for record in records if record.get("status") == "duplicate"),
            "records": records,
            "signals_dir": str(self.shadow_dir),
            "real_execution": False,
        }

    def get_signals(self, date: str) -> list[dict[str, Any]]:
        signals: list[dict[str, Any]] = []
        for symbol in self.market_data.get_universe(date):
            rows = self.market_data.get_daily(symbol, "", date)
            if not rows:
                continue
            latest = rows[-1]
            price = self._safe_float(latest.get("close"), 0.0)
            if price <= 0:
                continue
            previous = self._previous_close(rows)
            momentum = ((price / previous) - 1.0) if previous and previous > 0 else 0.0
            if momentum < 0:
                continue
            signals.append(
                {
                    "symbol": symbol,
                    "ts_code": symbol,
                    "market": "crypto",
                    "direction": "buy",
                    "side": "buy",
                    "price": price,
                    "quantity": self._shadow_quantity(price),
                    "trade_date": date,
                    "capital_layer": "shadow",
                    "account_type": "shadow",
                    "strategy_name": "crypto_shadow",
                    "belief_score": round(min(0.95, 0.55 + momentum), 4),
                    "reason": "public daily bar momentum shadow signal",
                    "source": "CryptoMarketData.SharedSignals.market_bars_daily",
                    "real_execution": False,
                    "direct_execution": False,
                }
            )
            if len(signals) >= self.config.risk.max_positions:
                break
        return signals

    def write_shadow_record(self, record: dict[str, Any]) -> dict[str, Any]:
        reject_real_execution_payload(record, context="CryptoShadowRunner.record")
        card = self._build_card(record)
        machine = SignalStateMachine(self.shadow_dir)
        try:
            result = machine.write_pending(card)
            result["queue_scope"] = "shadow"
            return result
        except SignalStateConflict as exc:
            return {
                "order_id": card["order_id"],
                "status": "duplicate",
                "recorded": False,
                "message": str(exc),
                "signal_card": card,
                "queue_scope": "shadow",
            }

    def _build_card(self, record: dict[str, Any]) -> dict[str, Any]:
        symbol = str(record.get("symbol") or record.get("ts_code") or "").strip().upper()
        if not symbol:
            raise ValueError("Crypto shadow record symbol is required")
        date = str(record.get("trade_date") or record.get("date") or "").strip()
        side = str(record.get("side") or record.get("direction") or "buy").strip().lower()
        order_id = str(record.get("order_id") or f"SHADOW-crypto-{date}-{symbol}-{side}").replace("/", "-")
        idempotency_key = f"SHADOW:crypto:crypto_shadow:{date}:{symbol}:{side}"
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return {
            "order_id": order_id,
            "ts_code": symbol,
            "symbol": symbol,
            "market": "crypto",
            "direction": side,
            "quantity": self._safe_float(record.get("quantity"), 0.0),
            "price": self._safe_float(record.get("price"), 0.0),
            "strategy_name": "crypto_shadow",
            "timestamp": now,
            "status": "pending",
            "capital_layer": "shadow",
            "account_type": "shadow",
            "manual_confirm_required": False,
            "direct_execution": False,
            "valid_until": date,
            "idempotency_key": idempotency_key,
            "source": record.get("source", "CryptoShadowRunner"),
            "reason": record.get("reason", ""),
            "belief_score": record.get("belief_score"),
            "evidence_refs": [str(record.get("source", "SharedSignals.market_bars_daily"))],
        }

    def _shadow_quantity(self, price: float) -> float:
        notional = self.config.capital.initial_capital * self.config.risk.max_single_position_pct
        return round(notional / price, 8)

    @staticmethod
    def _previous_close(rows: list[dict[str, Any]]) -> float | None:
        if len(rows) < 2:
            return None
        return CryptoShadowRunner._safe_float(rows[-2].get("close"), 0.0)

    @staticmethod
    def _safe_float(value: Any, default: float) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return default
        return result if result == result else default


__all__ = ["CryptoShadowRunner"]
