"""Tests for Crypto Rollback Mechanism."""

import pytest
from datetime import datetime, timezone
from Crypto.rollback import RollbackManager
from Crypto.registry import CryptoChampionRegistry, ChampionRecord


class TestRollbackManager:
    """Test rollback manager."""
    
    def test_create_rollback_manager(self, tmp_path):
        """Test creating rollback manager."""
        registry = CryptoChampionRegistry(tmp_path / "registry.jsonl")
        manager = RollbackManager(registry)
        assert manager is not None
    
    def test_rollback_to_previous(self, tmp_path):
        """Test rollback to previous champion."""
        registry = CryptoChampionRegistry(tmp_path / "registry.jsonl")
        
        # Register two champions
        now = datetime.now(timezone.utc)
        champ_1 = ChampionRecord(
            champion_id="champ_1",
            strategy_id="model_1",
            symbol="BTCUSDT",
            strategy_type="factor",
            promoted_at=now.isoformat(),
            receipt_sha256="abc123",
            receipt_path="path/to/receipt",
        )
        champ_2 = ChampionRecord(
            champion_id="champ_2",
            strategy_id="model_2",
            symbol="BTCUSDT",
            strategy_type="factor",
            promoted_at=now.isoformat(),
            receipt_sha256="def456",
            receipt_path="path/to/receipt2",
        )
        registry.register_champion(champ_1)
        registry.register_champion(champ_2)
        
        manager = RollbackManager(registry)
        result = manager.rollback_to_previous("BTCUSDT", "performance_degradation")
        
        assert result["success"] is True
        assert result["previous_champion_id"] == "champ_1"
        assert result["current_champion_id"] == "champ_2"
    
    def test_rollback_no_current_champion(self, tmp_path):
        """Test rollback with no current champion."""
        registry = CryptoChampionRegistry(tmp_path / "registry.jsonl")
        manager = RollbackManager(registry)
        
        result = manager.rollback_to_previous("BTCUSDT", "test")
        assert result["success"] is False
        assert result["error"] == "no_current_champion"
    
    def test_rollback_no_previous_champion(self, tmp_path):
        """Test rollback with no previous champion."""
        registry = CryptoChampionRegistry(tmp_path / "registry.jsonl")
        
        # Register only one champion
        now = datetime.now(timezone.utc)
        champ_1 = ChampionRecord(
            champion_id="champ_1",
            strategy_id="model_1",
            symbol="BTCUSDT",
            strategy_type="factor",
            promoted_at=now.isoformat(),
            receipt_sha256="abc123",
            receipt_path="path/to/receipt",
        )
        registry.register_champion(champ_1)
        
        manager = RollbackManager(registry)
        result = manager.rollback_to_previous("BTCUSDT", "test")
        
        assert result["success"] is False
        assert result["error"] == "no_previous_champion"
    
    def test_get_rollback_candidates(self, tmp_path):
        """Test getting rollback candidates."""
        registry = CryptoChampionRegistry(tmp_path / "registry.jsonl")
        
        # Register multiple champions
        now = datetime.now(timezone.utc)
        for i in range(3):
            champ = ChampionRecord(
                champion_id=f"champ_{i}",
                strategy_id=f"model_{i}",
                symbol="BTCUSDT",
                strategy_type="factor",
                promoted_at=now.isoformat(),
                receipt_sha256=f"hash_{i}",
                receipt_path=f"path/to/receipt_{i}",
            )
            registry.register_champion(champ)
        
        manager = RollbackManager(registry)
        candidates = manager.get_rollback_candidates("BTCUSDT")
        
        assert len(candidates) == 3
        assert candidates[0]["champion_id"] == "champ_2"  # Most recent first
    
    def test_rollback_history(self, tmp_path):
        """Test rollback history tracking."""
        registry = CryptoChampionRegistry(tmp_path / "registry.jsonl")
        
        # Register champions
        now = datetime.now(timezone.utc)
        champ_1 = ChampionRecord(
            champion_id="champ_1",
            strategy_id="model_1",
            symbol="BTCUSDT",
            strategy_type="factor",
            promoted_at=now.isoformat(),
            receipt_sha256="abc123",
            receipt_path="path/to/receipt",
        )
        champ_2 = ChampionRecord(
            champion_id="champ_2",
            strategy_id="model_2",
            symbol="BTCUSDT",
            strategy_type="factor",
            promoted_at=now.isoformat(),
            receipt_sha256="def456",
            receipt_path="path/to/receipt2",
        )
        registry.register_champion(champ_1)
        registry.register_champion(champ_2)
        
        manager = RollbackManager(registry)
        manager.rollback_to_previous("BTCUSDT", "test_reason")
        
        history = manager.get_rollback_history("BTCUSDT")
        assert len(history) == 1
        assert history[0]["reason"] == "test_reason"
