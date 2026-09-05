from __future__ import annotations

from pathlib import Path

import pytest

from Crypto.registry import (
    CRYPTO_C3_LIFECYCLE_BLOCKER,
    ChampionRecord,
    CryptoC3LifecycleRetired,
    CryptoChampionRegistry,
)
from shared.governance.retirement import RetiredRuntimeError


ROOT = Path(__file__).resolve().parents[1]
SHARED_CRYPTO_REVIEW = ROOT / "shared" / "review" / "crypto"


def test_registry_construction_is_retired_and_creates_no_shared_review() -> None:
    before = SHARED_CRYPTO_REVIEW.exists()
    with pytest.raises(CryptoC3LifecycleRetired, match=CRYPTO_C3_LIFECYCLE_BLOCKER):
        CryptoChampionRegistry()
    assert SHARED_CRYPTO_REVIEW.exists() is before


def test_registry_rejects_explicit_path_before_any_write(tmp_path: Path) -> None:
    target = tmp_path / "champion_registry.jsonl"
    with pytest.raises(CryptoC3LifecycleRetired, match="authority=none"):
        CryptoChampionRegistry(target)
    assert not target.exists()
    assert not target.parent.joinpath("champion_registry.jsonl").exists()


def test_champion_record_construction_is_retired() -> None:
    with pytest.raises(CryptoC3LifecycleRetired, match=CRYPTO_C3_LIFECYCLE_BLOCKER):
        ChampionRecord(
            champion_id="abc123",
            strategy_id="momentum_5m_v1",
            symbol="BTCUSDT",
            strategy_type="factor",
            promoted_at="2026-08-19T12:00:00Z",
            receipt_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            receipt_path="unused.json",
        )


def test_c3_lifecycle_error_is_retired_runtime_error() -> None:
    assert issubclass(CryptoC3LifecycleRetired, RetiredRuntimeError)
