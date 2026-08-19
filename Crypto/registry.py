"""Crypto Champion/Challenger Registry

Append-only registry for tracking champion history with evidence-bound receipts.
"""
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class ChampionRecord:
    """Immutable record of a champion promotion"""
    champion_id: str  # content-addressed ID (receipt SHA256)
    strategy_id: str
    symbol: str
    strategy_type: str  # "factor" | "strategy"
    promoted_at: str  # ISO timestamp
    receipt_sha256: str  # promotion receipt file SHA256
    receipt_path: str  # relative path to receipt file
    evidence_summary: dict = field(default_factory=dict)
    status: str = "active"  # "active" | "demoted" | "retired"
    demoted_at: Optional[str] = None
    demotion_reason: Optional[str] = None
    demotion_receipt_sha256: Optional[str] = None
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> "ChampionRecord":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
    
    def is_active(self) -> bool:
        return self.status == "active"


class CryptoChampionRegistry:
    """Append-only registry for Crypto champions
    
    Storage: shared/review/crypto/champion_registry.jsonl
    Format: One JSON object per line (append-only)
    """
    
    def __init__(self, registry_path: Optional[Path] = None):
        if registry_path is None:
            # Default path relative to project root
            project_root = Path(__file__).parent.parent
            registry_path = project_root / "shared" / "review" / "crypto" / "champion_registry.jsonl"
        
        self.registry_path = registry_path
        self._ensure_directory()
    
    def _ensure_directory(self) -> None:
        """Ensure registry directory exists"""
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
    
    def register_champion(self, record: ChampionRecord) -> None:
        """Register a new champion (append-only)
        
        Args:
            record: ChampionRecord to register
            
        Raises:
            ValueError: If champion_id already exists
        """
        # Check for duplicate
        existing = self.get_by_id(record.champion_id)
        if existing is not None:
            raise ValueError(f"Champion {record.champion_id} already registered")
        
        # Append to registry
        with open(self.registry_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
    
    def get_by_id(self, champion_id: str) -> Optional[ChampionRecord]:
        """Get champion record by ID (returns latest version)
        
        Args:
            champion_id: Champion ID to look up
            
        Returns:
            Latest ChampionRecord if found, None otherwise
        """
        return self.get_latest_record(champion_id)
    
    def get_active_champion(self, symbol: str, strategy_type: str) -> Optional[ChampionRecord]:
        """Get active champion for symbol/strategy
        
        Args:
            symbol: Trading symbol (e.g., "BTCUSDT")
            strategy_type: "factor" or "strategy"
            
        Returns:
            Active ChampionRecord if found, None otherwise
        """
        if not self.registry_path.exists():
            return None
        
        latest_active = None
        with open(self.registry_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if (data.get("symbol") == symbol and 
                    data.get("strategy_type") == strategy_type and 
                    data.get("status") == "active"):
                    latest_active = ChampionRecord.from_dict(data)
        
        return latest_active
    
    def get_champion_history(self, symbol: str, strategy_type: str) -> list[ChampionRecord]:
        """Get all champion records for symbol/strategy (including demoted)
        
        Args:
            symbol: Trading symbol
            strategy_type: "factor" or "strategy"
            
        Returns:
            List of ChampionRecord, ordered by promotion time
        """
        if not self.registry_path.exists():
            return []
        
        history = []
        with open(self.registry_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if (data.get("symbol") == symbol and 
                    data.get("strategy_type") == strategy_type):
                    history.append(ChampionRecord.from_dict(data))
        
        return history
    
    def get_all_active_champions(self) -> list[ChampionRecord]:
        """Get all active champions across all symbols/strategies
        
        Returns:
            List of active ChampionRecord
        """
        if not self.registry_path.exists():
            return []
        
        active = {}
        with open(self.registry_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if data.get("status") == "active":
                    key = (data.get("symbol"), data.get("strategy_type"))
                    active[key] = ChampionRecord.from_dict(data)
        
        return list(active.values())
    
    def update_record(self, record: ChampionRecord) -> None:
        """Update a champion record (for demotion/retirement)
        
        This appends a new version of the record to the registry.
        The latest record for each champion_id is the authoritative version.
        
        Args:
            record: Updated ChampionRecord
        """
        # Verify record exists
        existing = self.get_by_id(record.champion_id)
        if existing is None:
            raise ValueError(f"Champion {record.champion_id} not found")
        
        # Append updated record
        with open(self.registry_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
    
    def demote_champion(self, champion_id: str, reason: str, demotion_receipt_sha256: Optional[str] = None) -> ChampionRecord:
        """Demote a champion
        
        Args:
            champion_id: Champion ID to demote
            reason: Demotion reason
            demotion_receipt_sha256: Optional demotion receipt SHA256
            
        Returns:
            Updated ChampionRecord
            
        Raises:
            ValueError: If champion not found or already demoted
        """
        record = self.get_by_id(champion_id)
        if record is None:
            raise ValueError(f"Champion {champion_id} not found")
        
        if not record.is_active():
            raise ValueError(f"Champion {champion_id} already {record.status}")
        
        # Update record
        record.status = "demoted"
        record.demoted_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        record.demotion_reason = reason
        record.demotion_receipt_sha256 = demotion_receipt_sha256
        
        # Append updated record
        self.update_record(record)
        
        return record
    
    def retire_champion(self, champion_id: str, reason: str) -> ChampionRecord:
        """Retire a champion (permanent removal from active consideration)
        
        Args:
            champion_id: Champion ID to retire
            reason: Retirement reason
            
        Returns:
            Updated ChampionRecord
            
        Raises:
            ValueError: If champion not found or already retired
        """
        record = self.get_by_id(champion_id)
        if record is None:
            raise ValueError(f"Champion {champion_id} not found")
        
        if record.status == "retired":
            raise ValueError(f"Champion {champion_id} already retired")
        
        # Update record
        record.status = "retired"
        record.demoted_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        record.demotion_reason = reason
        
        # Append updated record
        self.update_record(record)
        
        return record
    
    def get_latest_record(self, champion_id: str) -> Optional[ChampionRecord]:
        """Get the latest version of a champion record
        
        Since the registry is append-only, there may be multiple versions
        of the same champion_id (e.g., after demotion). This returns the
        latest version.
        
        Args:
            champion_id: Champion ID
            
        Returns:
            Latest ChampionRecord if found, None otherwise
        """
        if not self.registry_path.exists():
            return None
        
        latest = None
        with open(self.registry_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if data.get("champion_id") == champion_id:
                    latest = ChampionRecord.from_dict(data)
        
        return latest
    
    def count(self) -> int:
        """Count total records in registry (including duplicates)
        
        Returns:
            Number of lines in registry file
        """
        if not self.registry_path.exists():
            return 0
        
        with open(self.registry_path, "r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    
    def count_unique_champions(self) -> int:
        """Count unique champion IDs
        
        Returns:
            Number of unique champion_id values
        """
        if not self.registry_path.exists():
            return 0
        
        champion_ids = set()
        with open(self.registry_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                champion_ids.add(data.get("champion_id"))
        
        return len(champion_ids)
