#!/usr/bin/env python3
"""US P2 portfolio optimizer with correlation-based position gating."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from shared.markets.analytics import correlation_matrix, max_abs_correlation, safe_float
from shared.markets.config_schema import MarketToolConfig
from shared.markets.safety import assert_no_live_broker, assert_no_real_execution, reject_real_execution_payload
from US.common import USConfig


class USPortfolioOptimizer:
    """Gate US shadow candidates when they duplicate existing exposures."""

    def __init__(self, config: MarketToolConfig | None = None, *, correlation_cap: float = 0.80) -> None:
        self.config = config or USConfig()
        assert_no_real_execution(self.config)
        assert_no_live_broker(self.config)
        self.correlation_cap = float(correlation_cap)

    def gate(
        self,
        candidates: list[dict[str, Any]],
        existing_positions: list[dict[str, Any]],
        bars_by_symbol: dict[str, list[dict[str, Any]]],
        *,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        for row in list(candidates) + list(existing_positions):
            reject_real_execution_payload(row, context="USPortfolioOptimizer.payload")
        for rows in bars_by_symbol.values():
            for row in rows:
                reject_real_execution_payload(row, context="USPortfolioOptimizer.bars")

        matrix = correlation_matrix({symbol.upper(): rows for symbol, rows in bars_by_symbol.items()})
        held = [str(row.get("symbol") or "").upper() for row in existing_positions if row.get("symbol")]
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        selected = list(held)
        for candidate in sorted(candidates, key=lambda row: safe_float(row.get("score"), 0.0), reverse=True):
            symbol = str(candidate.get("symbol") or "").upper()
            corr = max_abs_correlation(symbol, selected, matrix)
            if corr > self.correlation_cap:
                rejected.append({"symbol": symbol, "reason": "correlation_gate", "max_correlation": round(corr, 6)})
                continue
            selected.append(symbol)
            accepted.append(dict(candidate, symbol=symbol, capital_layer="shadow"))

        return {
            "market": "us",
            "currency": "USD",
            "as_of": as_of,
            "capital_layer": "shadow",
            "account_type": "shadow",
            "real_execution": False,
            "accepted": accepted,
            "rejected": rejected,
            "correlation_matrix": matrix,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }


__all__ = ["USPortfolioOptimizer"]

