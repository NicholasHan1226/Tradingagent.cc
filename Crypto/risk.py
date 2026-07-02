#!/usr/bin/env python3
"""Crypto P2 public-data background risk scoring."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from Crypto.common import CryptoConfig, load_crypto_config
from shared.markets.analytics import returns_from_bars, safe_float, volatility
from shared.markets.safety import assert_public_data_only, reject_real_execution_payload


class CryptoRiskBackground:
    """Score funding, news, and volatility risk using public data only."""

    def __init__(self, config: CryptoConfig | None = None) -> None:
        self.config = config or load_crypto_config()
        assert_public_data_only(self.config)

    def score(
        self,
        *,
        symbol: str,
        funding_rates: list[dict[str, Any]] | None = None,
        news_events: list[dict[str, Any]] | None = None,
        bars: list[dict[str, Any]] | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        for row in list(funding_rates or []) + list(news_events or []) + list(bars or []):
            reject_real_execution_payload(row, context="CryptoRiskBackground.input")

        funding_component = self._funding_component(funding_rates or [])
        news_component = self._news_component(news_events or [])
        volatility_component = self._volatility_component(bars or [])
        score = round(0.35 * funding_component + 0.25 * news_component + 0.40 * volatility_component, 2)
        return {
            "market": "crypto",
            "symbol": symbol.upper(),
            "as_of": as_of,
            "capital_layer": "shadow",
            "account_type": "shadow",
            "public_data_only": True,
            "real_execution": False,
            "risk_score": score,
            "risk_level": "high" if score >= 70 else "medium" if score >= 35 else "low",
            "components": {
                "funding": round(funding_component, 2),
                "news": round(news_component, 2),
                "volatility": round(volatility_component, 2),
            },
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    @staticmethod
    def _funding_component(rows: list[dict[str, Any]]) -> float:
        rates = [abs(safe_float(row.get("funding_rate", row.get("rate")))) for row in rows]
        if not rates:
            return 0.0
        avg = sum(rates[-8:]) / min(len(rates), 8)
        return min(100.0, avg / 0.001 * 40.0)

    @staticmethod
    def _news_component(rows: list[dict[str, Any]]) -> float:
        severities = []
        for row in rows:
            if str(row.get("sentiment", "")).lower() in {"negative", "bearish", "risk"}:
                severities.append(max(25.0, safe_float(row.get("severity"), 0.5) * 100.0))
            elif str(row.get("category", "")).lower() in {"hack", "regulatory", "liquidation"}:
                severities.append(max(50.0, safe_float(row.get("severity"), 0.7) * 100.0))
        return min(100.0, sum(severities))

    @staticmethod
    def _volatility_component(rows: list[dict[str, Any]]) -> float:
        returns = list(returns_from_bars(rows).values())
        daily_vol = volatility(returns[-20:])
        return min(100.0, daily_vol / 0.05 * 100.0)


__all__ = ["CryptoRiskBackground"]

