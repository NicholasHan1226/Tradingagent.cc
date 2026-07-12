#!/usr/bin/env python3
"""Investment Manager Decision Engine — synthesizes research, risk, and sizing into actionable decisions."""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class DecisionResult:
    symbol: str
    market: str
    action: str  # BUY/SELL/HOLD/SKIP
    confidence: float
    conviction: str  # high/medium/low
    position_pct: float
    stop_loss_pct: float
    take_profit_pct: float
    entry_price: float | None
    reason: str
    risk_flags: list[str] = field(default_factory=list)
    capital_layer: str = "simulated"
    generated_at: str = ""


class DecisionEngine:
    """Synthesizes multi-perspective research + risk + sizing into decisions."""

    def __init__(self, market: str = "Ashare"):
        self.market = market
        self.min_consensus = 0.50
        self.max_single_position = 0.10
        self.require_bull_bear_agreement = True

    def decide(
        self, fundamental: dict, perspectives: dict, risk: dict, capital: dict
    ) -> DecisionResult:
        # Compute weighted consensus
        weights = {"bull": 0.25, "bear": -0.25, "macro": 0.20, "technical": 0.30}
        weighted = sum(
            weights.get(k, 0) * float(v.get("score", 0) or 0) / 100
            for k, v in perspectives.items()
        )
        fundamental_score = float(fundamental.get("composite_score", 50)) / 100
        risk_score = 1.0 - float(risk.get("risk_score", 50)) / 100

        consensus = weighted * 0.4 + fundamental_score * 0.35 + risk_score * 0.25
        conviction = (
            "high" if consensus > 0.65 else ("medium" if consensus > 0.50 else "low")
        )

        # Decision logic
        if consensus > 0.60 and risk_score > 0.5:
            action = "BUY"
            position = min(self.max_single_position, consensus * 0.15)
        elif consensus > 0.45:
            action = "HOLD"
            position = 0
        else:
            action = "SKIP"
            position = 0

        # Risk flags
        flags = []
        if float(perspectives.get("bear", {}).get("score", 0)) > 70:
            flags.append("bearish_divergence")
        if float(risk.get("max_drawdown_pct", 0)) > 15:
            flags.append("high_drawdown")
        if float(fundamental.get("debt_equity", 1)) > 3:
            flags.append("high_leverage")

        return DecisionResult(
            symbol=fundamental.get("symbol", ""),
            market=self.market,
            action=action,
            confidence=round(consensus, 4),
            conviction=conviction,
            position_pct=round(position, 4),
            stop_loss_pct=0.05,
            take_profit_pct=0.15,
            entry_price=None,
            reason=f"consensus={round(consensus, 2)} fundamental={round(fundamental_score, 2)}",
            risk_flags=flags,
            capital_layer="simulated",
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    def portfolio_rebalance(
        self,
        decisions: list[DecisionResult | dict[str, Any]],
        total_capital: float | None = None,
        max_positions: int | None = None,
        *,
        market: str | None = None,
        as_of: str | None = None,
        capital: float | None = None,
    ) -> dict[str, Any]:
        """Build a bounded simulated portfolio without forcing cash deployment.

        The auto pipeline passes dictionary decisions plus ``market/as_of/capital``;
        the legacy direct caller passes dataclasses plus positional capital.  This
        method accepts both contracts and only scales risk down when a market cap
        would be exceeded.
        """

        market_name = str(market or self.market or "").strip()
        market_key = market_name.lower().replace("-", "_")
        is_ashare = market_key in {"ashare", "a_share", "a股"}
        capital_value = float(capital if capital is not None else total_capital or 0.0)
        if capital_value <= 0:
            raise ValueError("capital must be positive")

        hard_position_cap = 8 if is_ashare else 10
        requested_cap = (
            hard_position_cap if max_positions is None else max(0, int(max_positions))
        )
        position_cap = (
            min(requested_cap, hard_position_cap) if is_ashare else requested_cap
        )
        gross_cap = 0.90 if is_ashare else 1.0

        def value(
            decision: DecisionResult | dict[str, Any], key: str, default: Any = None
        ) -> Any:
            if isinstance(decision, dict):
                return decision.get(key, default)
            return getattr(decision, key, default)

        buys = [
            decision
            for decision in decisions
            if str(value(decision, "action", "")).strip().lower() == "buy"
        ]
        buys.sort(
            key=lambda decision: float(
                value(decision, "confidence", value(decision, "belief_score", 0.0))
                or 0.0
            ),
            reverse=True,
        )
        selected = buys[:position_cap]

        requested_weights = [
            max(0.0, float(value(decision, "position_pct", 0.0) or 0.0))
            for decision in selected
        ]
        if is_ashare:
            requested_weights = [min(weight, 0.15) for weight in requested_weights]
        total_requested = sum(requested_weights)
        scale = min(1.0, gross_cap / total_requested) if total_requested > 0 else 1.0

        positions: list[dict[str, Any]] = []
        for decision, requested_weight in zip(selected, requested_weights):
            weight = requested_weight * scale
            if weight <= 0:
                continue
            symbol = str(
                value(decision, "ts_code", value(decision, "symbol", "")) or ""
            )
            positions.append(
                {
                    "market": market_key or market_name,
                    "symbol": symbol,
                    "ts_code": symbol,
                    "side": "buy",
                    "price": value(decision, "price"),
                    "belief_score": value(
                        decision,
                        "belief_score",
                        value(decision, "confidence", 0.0),
                    ),
                    "conviction": value(decision, "conviction", 0.0),
                    "position_pct": round(weight, 6),
                    "trade_date": str(as_of or ""),
                    "capital_layer": "simulated",
                    "account_type": "simulated",
                    "real_execution": False,
                    "direct_execution": False,
                }
            )

        allocated = sum(float(row["position_pct"]) for row in positions)
        return {
            "market": market_key or market_name,
            "capital": capital_value,
            "positions": positions,
            "position_count": len(positions),
            "allocated_pct": round(allocated, 6),
            "idle_pct": round(max(0.0, 1.0 - allocated), 6),
            "trade_date": str(as_of or ""),
            "capital_layer": "simulated",
            "account_type": "simulated",
            "real_execution": False,
            "direct_execution": False,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
