#!/usr/bin/env python3
"""HK P2 portfolio optimizer: HKD lot sizing and sector caps."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from HK.common import HKConfig
from shared.markets.analytics import safe_float
from shared.markets.config_schema import MarketToolConfig
from shared.markets.safety import assert_no_live_broker, assert_no_real_execution, reject_real_execution_payload


class HKPortfolioOptimizer:
    """Size HK shadow positions in HKD while enforcing sector caps."""

    def __init__(
        self,
        config: MarketToolConfig | None = None,
        *,
        sector_cap: float = 0.30,
    ) -> None:
        self.config = config or HKConfig()
        assert_no_real_execution(self.config)
        assert_no_live_broker(self.config)
        self.sector_cap = float(sector_cap)

    def optimize(
        self,
        candidates: list[dict[str, Any]],
        *,
        capital_hkd: float | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        for row in candidates:
            reject_real_execution_payload(row, context="HKPortfolioOptimizer.candidate")

        capital = safe_float(capital_hkd, self.config.capital.initial_capital)
        max_single = float(self.config.risk.max_single_position_pct)
        sector_used: dict[str, float] = {}
        positions: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for row in sorted(candidates, key=lambda item: safe_float(item.get("score"), 0.0), reverse=True):
            symbol = _normalize_hk_symbol(row.get("symbol"))
            price = safe_float(row.get("price_hkd", row.get("price")))
            lot_size = max(1, int(safe_float(row.get("lot_size"), 100)))
            sector = str(row.get("sector") or "unknown")
            target_weight = min(max_single, max(0.0, safe_float(row.get("target_weight"), max_single)))
            projected_sector = sector_used.get(sector, 0.0) + target_weight
            if price <= 0:
                skipped.append({"symbol": symbol, "reason": "missing_price"})
                continue
            sector_already_has = sector_used.get(sector, 0.0) > 0
            if sector_already_has and projected_sector > self.sector_cap:
                skipped.append({"symbol": symbol, "reason": "sector_cap", "sector": sector, "projected_weight": round(projected_sector, 6)})
                continue
            raw_shares = int((capital * target_weight) // (price * lot_size)) * lot_size
            if raw_shares <= 0:
                skipped.append({"symbol": symbol, "reason": "below_lot_size"})
                continue
            notional = raw_shares * price
            realized_weight = notional / capital if capital > 0 else 0.0
            sector_used[sector] = sector_used.get(sector, 0.0) + realized_weight
            positions.append({
                "symbol": symbol,
                "sector": sector,
                "shares": raw_shares,
                "lot_size": lot_size,
                "price_hkd": price,
                "notional_hkd": round(notional, 2),
                "target_weight": round(realized_weight, 6),
                "capital_layer": "shadow",
            })

        return {
            "market": "hk",
            "currency": "HKD",
            "as_of": as_of,
            "capital_layer": "shadow",
            "account_type": "shadow",
            "real_execution": False,
            "sector_cap": self.sector_cap,
            "sector_weights": {sector: round(weight, 6) for sector, weight in sector_used.items()},
            "positions": positions,
            "skipped": skipped,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }


def _normalize_hk_symbol(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if raw.endswith(".HK"):
        digits = raw[:-3]
    else:
        digits = raw
    return f"{digits.zfill(5)}.HK" if digits.isdigit() else raw


__all__ = ["HKPortfolioOptimizer"]
