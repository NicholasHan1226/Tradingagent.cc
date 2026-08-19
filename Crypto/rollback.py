"""Crypto Rollback Mechanism - C3 Implementation.

Provides deterministic rollback to previous champion when current champion
is demoted or retired.
"""

from typing import Optional, Dict, Any
from Crypto.registry import CryptoChampionRegistry, ChampionRecord


class RollbackManager:
    """Manages rollback to previous champion."""
    
    def __init__(self, registry: CryptoChampionRegistry):
        """Initialize rollback manager.
        
        Args:
            registry: Champion registry instance
        """
        self.registry = registry
        self.rollback_history = {}  # symbol -> list of rollback records
    
    def rollback_to_previous(
        self,
        symbol: str,
        reason: str,
    ) -> Dict[str, Any]:
        """Rollback to previous champion for a symbol.
        
        Args:
            symbol: Trading symbol
            reason: Reason for rollback
            
        Returns:
            Dict with rollback result
        """
        # Get current champion (try common strategy types)
        current = None
        for stype in ["factor", "champion", "strategy"]:
            current = self.registry.get_active_champion(symbol, stype)
            if current:
                break
        
        if not current:
            return {
                "success": False,
                "error": "no_current_champion",
            }
        
        # Get history for this symbol
        history = self.registry.get_champion_history(symbol, current.strategy_type)
        
        # Find previous active champion
        previous = None
        for record in reversed(history):
            if record.champion_id != current.champion_id:
                if record.status == "active":
                    previous = record
                    break
        
        if not previous:
            return {
                "success": False,
                "error": "no_previous_champion",
            }
        
        # Reactivate previous champion
        self.registry.reactivate_champion(previous.champion_id, reason=f"rollback_from_{current.champion_id}")
        
        # Retire current champion
        self.registry.retire_champion(current.champion_id, reason)
        
        # Record rollback
        if symbol not in self.rollback_history:
            self.rollback_history[symbol] = []
        
        rollback_record = {
            "symbol": symbol,
            "from_champion_id": current.champion_id,
            "to_champion_id": previous.champion_id,
            "reason": reason,
            "timestamp": previous.promoted_at,  # Use previous champion's timestamp
        }
        self.rollback_history[symbol].append(rollback_record)
        
        return {
            "success": True,
            "previous_champion_id": previous.champion_id,
            "previous_strategy_id": previous.strategy_id,
            "current_champion_id": current.champion_id,
            "reason": reason,
        }
    
    def get_rollback_candidates(self, symbol: str) -> list[Dict[str, Any]]:
        """Get potential rollback candidates for a symbol.
        
        Args:
            symbol: Trading symbol
            
        Returns:
            List of candidate champions (only latest state for each champion)
        """
        history = self.registry.get_history(symbol)
        
        # Group by champion_id and keep only the latest record
        # Also track registration order (index in history)
        latest_by_id = {}
        for idx, record in enumerate(history):
            cid = record.champion_id
            if cid not in latest_by_id or record.promoted_at > latest_by_id[cid][0].promoted_at:
                latest_by_id[cid] = (record, idx)
        
        candidates = []
        for record, idx in latest_by_id.values():
            if record.status in ["active", "demoted"]:
                candidates.append({
                    "champion_id": record.champion_id,
                    "model_id": record.strategy_id,
                    "state": record.status,
                    "promoted_at": record.promoted_at,
                    "_registration_order": idx,
                })
        
        # Sort by promoted_at descending, then by registration order descending
        candidates.sort(key=lambda x: (x["promoted_at"], x["_registration_order"]), reverse=True)
        
        # Remove internal field
        for candidate in candidates:
            candidate.pop("_registration_order", None)
        
        return candidates
    
    def get_rollback_history(self, symbol: str) -> list[Dict[str, Any]]:
        """Get rollback history for a symbol.
        
        Args:
            symbol: Trading symbol
            
        Returns:
            List of rollback records
        """
        return self.rollback_history.get(symbol, [])
    
    def validate_rollback(self, symbol: str) -> Dict[str, Any]:
        """Validate if rollback is possible for a symbol.
        
        Args:
            symbol: Trading symbol
            
        Returns:
            Dict with validation result
        """
        current = self.registry.get_active_champion(symbol, "champion")
        if not current:
            return {
                "can_rollback": False,
                "reason": "no_current_champion",
            }
        
        candidates = self.get_rollback_candidates(symbol)
        # Exclude current champion
        candidates = [c for c in candidates if c["champion_id"] != current.champion_id]
        
        if not candidates:
            return {
                "can_rollback": False,
                "reason": "no_previous_champion",
            }
        
        return {
            "can_rollback": True,
            "candidates": candidates,
        }
