#!/usr/bin/env python3
"""US P2 historical bars replay for shadow rules."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from shared.markets.analytics import close_series, safe_float
from shared.markets.config_schema import MarketToolConfig
from shared.markets.safety import assert_no_live_broker, assert_no_real_execution, reject_real_execution_payload
from US.common import USConfig


class USHistoricalReplay:
    """Replay long-only shadow rules on US historical bars."""

    def __init__(self, config: MarketToolConfig | None = None) -> None:
        self.config = config or USConfig()
        assert_no_real_execution(self.config)
        assert_no_live_broker(self.config)

    def replay(
        self,
        bars_by_symbol: dict[str, list[dict[str, Any]]],
        rules: list[dict[str, Any]],
        *,
        initial_cash: float | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        for rule in rules:
            reject_real_execution_payload(rule, context="USHistoricalReplay.rule")
        series_by_symbol: dict[str, list[tuple[str, float]]] = {}
        for symbol, rows in bars_by_symbol.items():
            for row in rows:
                reject_real_execution_payload(row, context="USHistoricalReplay.bars")
            series_by_symbol[symbol.upper()] = close_series(rows)

        cash = safe_float(initial_cash, self.config.capital.initial_capital)
        equity = cash
        trades: list[dict[str, Any]] = []
        for rule in rules:
            symbol = str(rule.get("symbol") or "").upper()
            series = series_by_symbol.get(symbol, [])
            lookback = max(1, int(safe_float(rule.get("lookback"), 1)))
            threshold = safe_float(rule.get("threshold"), 0.0)
            size_pct = min(float(self.config.risk.max_single_position_pct), max(0.0, safe_float(rule.get("size_pct"), 0.05)))
            for idx in range(lookback, len(series) - 1):
                prev_price = series[idx - lookback][1]
                date, price = series[idx]
                next_date, next_price = series[idx + 1]
                if prev_price <= 0 or price / prev_price - 1.0 < threshold:
                    continue
                pnl = equity * size_pct * (next_price / price - 1.0)
                equity += pnl
                trades.append({
                    "symbol": symbol,
                    "entry_date": date,
                    "exit_date": next_date,
                    "pnl": round(pnl, 6),
                    "capital_layer": "shadow",
                })
        wins = sum(1 for trade in trades if safe_float(trade.get("pnl")) > 0)
        return {
            "market": "us",
            "currency": "USD",
            "as_of": as_of,
            "capital_layer": "shadow",
            "account_type": "shadow",
            "real_execution": False,
            "initial_cash": cash,
            "ending_equity": round(equity, 6),
            "total_pnl": round(equity - cash, 6),
            "trade_count": len(trades),
            "win_rate": round(wins / len(trades), 6) if trades else 0.0,
            "trades": trades,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }


__all__ = ["USHistoricalReplay"]

