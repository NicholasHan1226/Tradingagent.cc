#!/usr/bin/env python3
"""Crypto P2 portfolio optimizer: correlation matrix and vol-adaptive sizing."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from Crypto.common import CryptoConfig, load_crypto_config
from shared.markets.analytics import correlation_matrix, max_abs_correlation, returns_from_bars, safe_float, volatility
from shared.markets.safety import assert_no_real_execution, reject_real_execution_payload


class CryptoPortfolioOptimizer:
    """Build shadow/sim crypto weights from public bars and candidate scores."""

    def __init__(self, config: CryptoConfig | None = None, *, correlation_cap: float = 0.85) -> None:
        self.config = config or load_crypto_config()
        assert_no_real_execution(self.config)
        self.correlation_cap = float(correlation_cap)

    def optimize(
        self,
        candidates: list[dict[str, Any]],
        bars_by_symbol: dict[str, list[dict[str, Any]]],
        *,
        capital: float | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        for row in list(candidates):
            reject_real_execution_payload(row, context="CryptoPortfolioOptimizer.candidate")
        for rows in bars_by_symbol.values():
            for row in rows:
                reject_real_execution_payload(row, context="CryptoPortfolioOptimizer.bars")

        matrix = correlation_matrix(bars_by_symbol)
        accepted: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        selected_symbols: list[str] = []
        for candidate in sorted(candidates, key=lambda row: safe_float(row.get("score"), 0.0), reverse=True):
            symbol = str(candidate.get("symbol") or "").upper()
            if not symbol or symbol not in bars_by_symbol:
                skipped.append({"symbol": symbol, "reason": "missing_bars"})
                continue
            corr = max_abs_correlation(symbol, selected_symbols, matrix)
            if corr > self.correlation_cap:
                skipped.append({"symbol": symbol, "reason": "correlation_cap", "max_correlation": round(corr, 6)})
                continue
            selected_symbols.append(symbol)
            accepted.append(dict(candidate, symbol=symbol))

        raw_weights: dict[str, float] = {}
        for candidate in accepted:
            symbol = candidate["symbol"]
            returns = list(returns_from_bars(bars_by_symbol[symbol]).values())
            vol = max(volatility(returns[-20:]), 0.005)
            score = max(safe_float(candidate.get("score"), 1.0), 0.0)
            raw_weights[symbol] = score / vol

        total = sum(raw_weights.values())
        max_single = float(self.config.risk.max_single_position_pct)
        weights = {
            symbol: min(max_single, value / total) if total > 0 else 0.0
            for symbol, value in raw_weights.items()
        }
        weights = {symbol: round(weight, 6) for symbol, weight in weights.items()}
        base_capital = safe_float(capital, self.config.capital.initial_capital)
        positions = [
            {
                "symbol": symbol,
                "target_weight": weight,
                "notional": round(base_capital * weight, 2),
                "capital_layer": "shadow",
            }
            for symbol, weight in sorted(weights.items())
        ]
        return {
            "market": "crypto",
            "as_of": as_of,
            "currency": self.config.capital.currency,
            "capital_layer": "shadow",
            "account_type": "shadow",
            "real_execution": False,
            "correlation_matrix": matrix,
            "positions": positions,
            "skipped": skipped,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }


__all__ = ["CryptoPortfolioOptimizer"]
