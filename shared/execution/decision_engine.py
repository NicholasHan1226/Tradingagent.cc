#!/usr/bin/env python3
"""Investment Manager Decision Engine — synthesizes research, risk, and sizing into actionable decisions."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

@dataclass
class DecisionResult:
    symbol: str; market: str; action: str  # BUY/SELL/HOLD/SKIP
    confidence: float; conviction: str  # high/medium/low
    position_pct: float; stop_loss_pct: float; take_profit_pct: float
    entry_price: float | None; reason: str; risk_flags: list[str] = field(default_factory=list)
    capital_layer: str = 'simulated'; generated_at: str = ''

class DecisionEngine:
    """Synthesizes multi-perspective research + risk + sizing into decisions."""
    
    def __init__(self, market: str = 'Ashare'):
        self.market = market
        self.min_consensus = 0.50
        self.max_single_position = 0.10
        self.require_bull_bear_agreement = True
    
    def decide(self, fundamental: dict, perspectives: dict, risk: dict, capital: dict) -> DecisionResult:
        # Compute weighted consensus
        weights = {'bull': 0.25, 'bear': -0.25, 'macro': 0.20, 'technical': 0.30}
        weighted = sum(
            weights.get(k, 0) * float(v.get('score', 0) or 0) / 100
            for k, v in perspectives.items()
        )
        fundamental_score = float(fundamental.get('composite_score', 50)) / 100
        risk_score = 1.0 - float(risk.get('risk_score', 50)) / 100
        
        consensus = (weighted * 0.4 + fundamental_score * 0.35 + risk_score * 0.25)
        conviction = 'high' if consensus > 0.65 else ('medium' if consensus > 0.50 else 'low')
        
        # Decision logic
        if consensus > 0.60 and risk_score > 0.5:
            action = 'BUY'
            position = min(self.max_single_position, consensus * 0.15)
        elif consensus > 0.45:
            action = 'HOLD'
            position = 0
        else:
            action = 'SKIP'
            position = 0
        
        # Risk flags
        flags = []
        if float(perspectives.get('bear', {}).get('score', 0)) > 70: flags.append('bearish_divergence')
        if float(risk.get('max_drawdown_pct', 0)) > 15: flags.append('high_drawdown')
        if float(fundamental.get('debt_equity', 1)) > 3: flags.append('high_leverage')
        
        return DecisionResult(
            symbol=fundamental.get('symbol', ''), market=self.market,
            action=action, confidence=round(consensus, 4), conviction=conviction,
            position_pct=round(position, 4), stop_loss_pct=0.05, take_profit_pct=0.15,
            entry_price=None, reason=f'consensus={round(consensus,2)} fundamental={round(fundamental_score,2)}',
            risk_flags=flags, capital_layer='simulated',
            generated_at=datetime.now(timezone.utc).isoformat()
        )

    def portfolio_rebalance(self, decisions: list[DecisionResult], total_capital: float, max_positions: int = 10) -> dict:
        buys = [d for d in decisions if d.action == 'BUY']
        buys.sort(key=lambda d: d.confidence, reverse=True)
        selected = buys[:max_positions]
        
        total_allocated = sum(d.position_pct for d in selected)
        scale = min(1.0, 1.0 / total_allocated) if total_allocated > 1.0 else 1.0
        
        return {
            'market': self.market,
            'capital': total_capital,
            'positions': len(selected),
            'allocated_pct': round(total_allocated * scale * 100, 2),
            'idle_pct': round((1 - total_allocated * scale) * 100, 2),
            'decisions': [
                {'symbol': d.symbol, 'action': d.action, 'position_pct': round(d.position_pct * scale * 100, 2),
                 'confidence': d.confidence, 'conviction': d.conviction, 'flags': d.risk_flags}
                for d in selected
            ],
            'generated_at': datetime.now(timezone.utc).isoformat()
        }
