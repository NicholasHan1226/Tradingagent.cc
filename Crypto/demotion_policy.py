"""Crypto Demotion Policy - Evidence-based champion demotion rules."""

from dataclasses import dataclass
from typing import Dict, Any


@dataclass
class DemotionCriteria:
    """Criteria for demoting a champion."""
    min_consecutive_failures: int = 3
    max_drawdown_pct: float = 10.0
    min_sharpe_ratio: float = 0.5
    min_win_rate: float = 0.4
    evaluation_window_days: int = 30


class DemotionPolicy:
    """Policy for evaluating champion demotion."""
    
    def __init__(self, criteria: DemotionCriteria = None):
        self.criteria = criteria or DemotionCriteria()
    
    def evaluate(self, champion_id: str, performance: Dict[str, Any]) -> Dict[str, Any]:
        """
        Evaluate whether a champion should be demoted.
        
        Args:
            champion_id: Champion identifier
            performance: Performance metrics
            
        Returns:
            Evaluation result with demotion decision
        """
        reasons = []
        
        # Check consecutive failures
        consecutive_failures = performance.get("consecutive_failures", 0)
        if consecutive_failures >= self.criteria.min_consecutive_failures:
            reasons.append(
                f"consecutive_failures={consecutive_failures} "
                f">= {self.criteria.min_consecutive_failures}"
            )
        
        # Check drawdown
        drawdown_pct = performance.get("drawdown_pct", 0.0)
        if drawdown_pct >= self.criteria.max_drawdown_pct:
            reasons.append(
                f"drawdown={drawdown_pct:.2f}% >= {self.criteria.max_drawdown_pct}%"
            )
        
        # Check Sharpe ratio
        sharpe = performance.get("sharpe_ratio", 0.0)
        if sharpe < self.criteria.min_sharpe_ratio:
            reasons.append(
                f"sharpe={sharpe:.2f} < {self.criteria.min_sharpe_ratio}"
            )
        
        # Check win rate
        win_rate = performance.get("win_rate", 0.0)
        if win_rate < self.criteria.min_win_rate:
            reasons.append(
                f"win_rate={win_rate:.2f} < {self.criteria.min_win_rate}"
            )
        
        should_demote = len(reasons) > 0
        
        return {
            "champion_id": champion_id,
            "should_demote": should_demote,
            "reasons": reasons,
            "performance": performance,
            "criteria": {
                "min_consecutive_failures": self.criteria.min_consecutive_failures,
                "max_drawdown_pct": self.criteria.max_drawdown_pct,
                "min_sharpe_ratio": self.criteria.min_sharpe_ratio,
                "min_win_rate": self.criteria.min_win_rate,
            },
        }
