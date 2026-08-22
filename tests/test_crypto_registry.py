"""Tests for Crypto Champion Registry"""
import json
import tempfile
from pathlib import Path
import pytest

from Crypto.registry import ChampionRecord, CryptoChampionRegistry


@pytest.fixture
def temp_registry_path():
    """Create a temporary registry file"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "test_registry.jsonl"


@pytest.fixture
def sample_champion_record():
    """Create a sample champion record"""
    return ChampionRecord(
        champion_id="abc123def456",
        strategy_id="momentum_5m_v1",
        symbol="BTCUSDT",
        strategy_type="factor",
        promoted_at="2026-08-19T12:00:00Z",
        receipt_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        receipt_path="Crypto/champion_promotions/BTCUSDT/factor/2026-08-19T12-00-00Z-abc123def456.json",
        evidence_summary={
            "total_trades": 150,
            "win_rate": 0.58,
            "sharpe_ratio": 1.5,
            "max_drawdown": -0.12
        }
    )


class TestChampionRecord:
    """Tests for ChampionRecord dataclass"""
    
    def test_create_record(self, sample_champion_record):
        """Test creating a champion record"""
        record = sample_champion_record
        assert record.champion_id == "abc123def456"
        assert record.symbol == "BTCUSDT"
        assert record.strategy_type == "factor"
        assert record.status == "active"
        assert record.is_active()
    
    def test_to_dict(self, sample_champion_record):
        """Test converting record to dict"""
        record = sample_champion_record
        data = record.to_dict()
        assert data["champion_id"] == "abc123def456"
        assert data["symbol"] == "BTCUSDT"
        assert data["status"] == "active"
    
    def test_from_dict(self, sample_champion_record):
        """Test creating record from dict"""
        record = sample_champion_record
        data = record.to_dict()
        restored = ChampionRecord.from_dict(data)
        assert restored.champion_id == record.champion_id
        assert restored.symbol == record.symbol
        assert restored.status == record.status


class TestCryptoChampionRegistry:
    """Tests for CryptoChampionRegistry"""
    
    def test_create_registry(self, temp_registry_path):
        """Test creating a new registry"""
        registry = CryptoChampionRegistry(temp_registry_path)
        assert registry.registry_path == temp_registry_path
        assert temp_registry_path.parent.exists()
    
    def test_register_champion(self, temp_registry_path, sample_champion_record):
        """Test registering a champion"""
        registry = CryptoChampionRegistry(temp_registry_path)
        registry.register_champion(sample_champion_record)
        
        # Verify file was created
        assert temp_registry_path.exists()
        
        # Verify record can be retrieved
        retrieved = registry.get_by_id(sample_champion_record.champion_id)
        assert retrieved is not None
        assert retrieved.champion_id == sample_champion_record.champion_id
    
    def test_register_duplicate_raises(self, temp_registry_path, sample_champion_record):
        """Test that registering duplicate champion raises error"""
        registry = CryptoChampionRegistry(temp_registry_path)
        registry.register_champion(sample_champion_record)
        
        with pytest.raises(ValueError, match="already registered"):
            registry.register_champion(sample_champion_record)
    
    def test_get_active_champion(self, temp_registry_path, sample_champion_record):
        """Test getting active champion"""
        registry = CryptoChampionRegistry(temp_registry_path)
        registry.register_champion(sample_champion_record)
        
        active = registry.get_active_champion("BTCUSDT", "factor")
        assert active is not None
        assert active.champion_id == sample_champion_record.champion_id
    
    def test_get_active_champion_not_found(self, temp_registry_path):
        """Test getting active champion when none exists"""
        registry = CryptoChampionRegistry(temp_registry_path)
        active = registry.get_active_champion("BTCUSDT", "factor")
        assert active is None
    
    def test_get_champion_history(self, temp_registry_path, sample_champion_record):
        """Test getting champion history"""
        registry = CryptoChampionRegistry(temp_registry_path)
        registry.register_champion(sample_champion_record)
        
        history = registry.get_champion_history("BTCUSDT", "factor")
        assert len(history) == 1
        assert history[0].champion_id == sample_champion_record.champion_id
    
    def test_get_all_active_champions(self, temp_registry_path, sample_champion_record):
        """Test getting all active champions"""
        registry = CryptoChampionRegistry(temp_registry_path)
        
        # Register multiple champions
        record1 = sample_champion_record
        record2 = ChampionRecord(
            champion_id="xyz789",
            strategy_id="mean_revert_v1",
            symbol="ETHUSDT",
            strategy_type="strategy",
            promoted_at="2026-08-19T13:00:00Z",
            receipt_sha256="abc123",
            receipt_path="path/to/receipt.json"
        )
        
        registry.register_champion(record1)
        registry.register_champion(record2)
        
        active = registry.get_all_active_champions()
        assert len(active) == 2
    
    def test_demote_champion(self, temp_registry_path, sample_champion_record):
        """Test demoting a champion"""
        registry = CryptoChampionRegistry(temp_registry_path)
        registry.register_champion(sample_champion_record)
        
        demoted = registry.demote_champion(
            sample_champion_record.champion_id,
            reason="Performance decay"
        )
        
        assert demoted.status == "demoted"
        assert demoted.demotion_reason == "Performance decay"
        assert demoted.demoted_at is not None
        assert not demoted.is_active()
        
        # Verify latest record is demoted
        latest = registry.get_latest_record(sample_champion_record.champion_id)
        assert latest.status == "demoted"
    
    def test_demote_already_demoted_raises(self, temp_registry_path, sample_champion_record):
        """Test that demoting already demoted champion raises error"""
        registry = CryptoChampionRegistry(temp_registry_path)
        registry.register_champion(sample_champion_record)
        registry.demote_champion(sample_champion_record.champion_id, "reason1")
        
        with pytest.raises(ValueError, match="already demoted"):
            registry.demote_champion(sample_champion_record.champion_id, "reason2")
    
    def test_retire_champion(self, temp_registry_path, sample_champion_record):
        """Test retiring a champion"""
        registry = CryptoChampionRegistry(temp_registry_path)
        registry.register_champion(sample_champion_record)
        
        retired = registry.retire_champion(
            sample_champion_record.champion_id,
            reason="Strategy deprecated"
        )
        
        assert retired.status == "retired"
        assert retired.demotion_reason == "Strategy deprecated"
    
    def test_count(self, temp_registry_path, sample_champion_record):
        """Test counting records"""
        registry = CryptoChampionRegistry(temp_registry_path)
        assert registry.count() == 0
        
        registry.register_champion(sample_champion_record)
        assert registry.count() == 1
        
        # Demote creates a new record
        registry.demote_champion(sample_champion_record.champion_id, "reason")
        assert registry.count() == 2
    
    def test_count_unique_champions(self, temp_registry_path, sample_champion_record):
        """Test counting unique champions"""
        registry = CryptoChampionRegistry(temp_registry_path)
        assert registry.count_unique_champions() == 0
        
        registry.register_champion(sample_champion_record)
        assert registry.count_unique_champions() == 1
        
        # Demote doesn't create a new unique champion
        registry.demote_champion(sample_champion_record.champion_id, "reason")
        assert registry.count_unique_champions() == 1
    
    def test_append_only(self, temp_registry_path, sample_champion_record):
        """Test that registry is append-only"""
        registry = CryptoChampionRegistry(temp_registry_path)
        registry.register_champion(sample_champion_record)
        
        # Get initial file size
        initial_size = temp_registry_path.stat().st_size
        
        # Demote (appends new record)
        registry.demote_champion(sample_champion_record.champion_id, "reason")
        
        # File should be larger
        final_size = temp_registry_path.stat().st_size
        assert final_size > initial_size
        
        # Original record should still be in file
        with open(temp_registry_path, "r") as f:
            lines = f.readlines()
        assert len(lines) == 2  # Original + demoted version
