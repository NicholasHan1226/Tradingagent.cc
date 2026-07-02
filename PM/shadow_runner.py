#!/usr/bin/env python3
"""PM Phase D shadow runner — probability-edge scoring and variant comparison.

Runs shadow-market cycles for Polymarket using probability-domain scoring.
Compares strategies by edge × kelly × hold variants. Never executes real
orders or connects to live venues.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.data.reader import TradingagentDataReader
from shared.execution.signal_state_machine import SignalStateConflict, SignalStateMachine
from shared.markets.base_tools import BaseShadowRunner
from shared.markets.config_schema import MarketToolConfig
from shared.markets.safety import reject_real_execution_payload

from PM.common import clamp_probability
from PM.market_data import PMMarketData
from PM.scoring import score_market
from PM.simulator import PMSimulator

TRADINGAGENT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SIGNALS_ROOT = TRADINGAGENT_ROOT / "signals"


class PMShadowRunner(BaseShadowRunner):
    """Probability-edge shadow runner for Polymarket strategies.

    Each cycle:
      1. Read universe of active markets.
      2. Score each market on probability value, liquidity, event clarity,
         time-to-settlement, and sentiment.
      3. Filter to top candidates by combined score.
      4. Generate simulated orders for each candidate.
      5. Execute through PMSimulator (local mock, no live CLOB).
      6. Write shadow records for review.
    """

    def __init__(
        self,
        config: MarketToolConfig | None = None,
        market_data: PMMarketData | None = None,
        simulator: PMSimulator | None = None,
        signals_root: Path | str | None = None,
    ) -> None:
        if config is None:
            from PM.common import load_pm_config
            config = load_pm_config().to_market_tool_config()
        if market_data is None:
            market_data = PMMarketData(config)
        if simulator is None:
            simulator = PMSimulator(config, market_data)

        super().__init__(
            market="pm",
            config=config,
            market_data=market_data,
            simulator=simulator,
        )
        self.signals_root = Path(signals_root) if signals_root is not None else DEFAULT_SIGNALS_ROOT

    # --- Abstract method implementations --------------------------------------

    def run_shadow(self, date: str) -> dict[str, Any]:
        """Run a complete shadow-market cycle for the given date.

        Returns a summary dict with all positions, scores, and fills.
        """
        cycle_id = f"pm-shadow-{date}-{uuid.uuid4().hex[:8]}"
        run_time = datetime.now(timezone.utc).isoformat()

        # 1. Get universe
        universe = self.market_data.get_universe(date)
        if not universe:
            return {
                "cycle_id": cycle_id,
                "date": date,
                "market": "pm",
                "status": "empty_universe",
                "signals_count": 0,
                "positions": [],
                "run_at": run_time,
                "message": "No active markets found for date",
            }

        # 2. Get & score signals
        signals = self.get_signals(date)
        scored = self._score_signals(signals, date)

        # 3. Filter by combined score threshold
        threshold = 0.50
        candidates = [s for s in scored if s.get("combined", 0.0) >= threshold]
        candidates.sort(key=lambda s: s.get("combined", 0.0), reverse=True)

        # Limit to max positions
        max_positions = self.config.risk.max_positions
        candidates = candidates[:max_positions]

        # 4. Generate and simulate orders
        positions: list[dict[str, Any]] = []
        account = {
            "account_id": "pm_shadow",
            "capital_layer": "shadow",
            "date": date,
        }

        for candidate in candidates:
            order = self._candidate_to_order(candidate, date)
            try:
                fill = self.simulator.simulate(order, account)
                positions.append({
                    **fill,
                    "score": candidate.get("combined", 0.0),
                    "score_breakdown": {k: v for k, v in candidate.items()
                                        if k != "combined"},
                })
            except Exception as exc:
                positions.append({
                    "market_id": candidate.get("market_id", ""),
                    "status": "error",
                    "error": str(exc),
                })

        # 5. Compile summary
        result = {
            "cycle_id": cycle_id,
            "date": date,
            "market": "pm",
            "status": "completed",
            "universe_size": len(universe),
            "signals_count": len(signals),
            "candidates_count": len(candidates),
            "filled_count": sum(1 for p in positions if p.get("status") == "filled"),
            "positions": positions,
            "run_at": run_time,
            "mode": "shadow",
            "live_clob": False,
        }

        # 6. Write shadow record
        self.write_shadow_record(result)

        return result

    def get_signals(self, date: str) -> list[dict[str, Any]]:
        """Return candidate market IDs for the given date.

        Reads active markets from the data source, filtering to those
        with usable price data.
        """
        universe = self.market_data.get_universe(date)
        if not universe:
            return []

        signals: list[dict[str, Any]] = []
        for market_id in universe:
            price = self.market_data.get_latest_price(market_id, date)
            if price is not None and price > 0:
                signals.append({
                    "market_id": str(market_id),
                    "latest_price": round(clamp_probability(price), 4),
                    "date": date,
                })

        return signals

    def write_shadow_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """Persist PM shadow records under ``signals/shadow/pending``."""
        record = dict(record or {})
        reject_real_execution_payload(record, context="PMShadowRunner.record")
        positions = [dict(pos) for pos in record.get("positions", []) if isinstance(pos, dict)]
        cards = [self._build_shadow_card(record, pos) for pos in positions] or [
            self._build_shadow_card(record, {})
        ]

        machine = SignalStateMachine(self.signals_root / "shadow")
        results: list[dict[str, Any]] = []
        for card in cards:
            try:
                result = machine.write_pending(card)
                result["queue_scope"] = "shadow"
            except SignalStateConflict as exc:
                result = {
                    "order_id": card["order_id"],
                    "status": "duplicate",
                    "recorded": False,
                    "message": str(exc),
                    "signal_card": card,
                    "queue_scope": "shadow",
                }
            results.append(result)

        if len(results) == 1:
            return results[0]
        return {
            "status": "written",
            "queue_scope": "shadow",
            "written": sum(1 for result in results if result.get("status") == "pending"),
            "records": results,
        }

    # --- Internal methods -----------------------------------------------------

    def _score_signals(
        self, signals: list[dict[str, Any]], date: str
    ) -> list[dict[str, Any]]:
        """Score all signals using PM probability-domain scoring."""
        scored: list[dict[str, Any]] = []
        for signal in signals:
            market_id = str(signal.get("market_id", ""))
            if not market_id:
                continue
            try:
                score = score_market(
                    market_id=market_id,
                    date=date,
                    data_reader=self.reader,
                )
                scored.append({**signal, **score})
            except Exception:
                # If scoring fails, assign neutral score and continue
                scored.append({
                    **signal,
                    "combined": 0.5,
                    "market": "pm",
                    "score_model": "pm_fallback",
                })
        return scored

    @staticmethod
    def _candidate_to_order(
        candidate: dict[str, Any],
        date: str,
    ) -> dict[str, Any]:
        """Convert a scored candidate into a simulated order."""
        combined = clamp_probability(candidate.get("combined", 0.5))
        probability_value = clamp_probability(
            candidate.get("probability_value", candidate.get("latest_price", 0.5))
        )

        # Edge-driven side selection
        edge = probability_value - 0.5
        if abs(edge) < 0.02:
            # No meaningful edge, skip or neutral
            side = "buy"
            outcome = "yes"
            price = 0.5
        elif edge > 0:
            side = "buy"
            outcome = "yes"
            price = clamp_probability(probability_value - 0.01)  # slightly below market
        else:
            side = "sell"
            outcome = "no"
            price = clamp_probability(probability_value + 0.01)  # slightly above market

        return {
            "market_id": str(candidate.get("market_id", "")),
            "side": side,
            "outcome": outcome,
            "quantity": 1,
            "price": round(price, 4),
            "date": date,
            "score": round(combined, 4),
        }

    @staticmethod
    def _build_shadow_card(record: dict[str, Any], position: dict[str, Any]) -> dict[str, Any]:
        date = str(record.get("date") or position.get("trade_date") or position.get("date") or "")
        market_id = str(position.get("market_id") or position.get("symbol") or record.get("cycle_id") or "unknown")
        side = str(position.get("side") or "buy").strip().lower()
        order_id = str(
            position.get("order_id")
            or f"PM-SHADOW-{date}-{market_id}-{side}-{uuid.uuid4().hex[:8]}"
        ).replace("/", "-")
        now = datetime.now(timezone.utc).isoformat()
        return {
            "order_id": order_id,
            "market_id": market_id,
            "symbol": market_id,
            "market": "pm",
            "direction": side,
            "side": side,
            "quantity": float(position.get("quantity", 1) or 1),
            "price": float(position.get("fill_price", position.get("price", 0.5)) or 0.5),
            "strategy_name": "pm_shadow",
            "timestamp": now,
            "created_at": now,
            "status": "pending",
            "capital_layer": "shadow",
            "account_type": "shadow",
            "manual_confirm_required": False,
            "direct_execution": False,
            "real_execution": False,
            "valid_until": date,
            "idempotency_key": f"SHADOW:pm:pm_shadow:{date}:{market_id}:{side}",
            "source": "PMShadowRunner",
            "reason": "probability-market shadow record",
            "belief_score": position.get("score"),
            "evidence_refs": [f"pm_shadow_cycle:{record.get('cycle_id', '')}"],
            "cycle_id": record.get("cycle_id", ""),
        }


__all__ = ["PMShadowRunner"]
