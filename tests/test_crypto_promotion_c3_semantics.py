from types import SimpleNamespace
from unittest.mock import Mock, patch

from Crypto.promotion import CryptoStrategyPromotion


def test_crypto_scorecard_waits_for_automatic_scientific_gate_not_human_review() -> None:
    promotion = CryptoStrategyPromotion.__new__(CryptoStrategyPromotion)
    promotion.config = SimpleNamespace(
        promotion=SimpleNamespace(min_shadow_trades=10, min_positive_days_pct=0.55)
    )
    promotion.records = [{"strategy": "candidate-a"}]
    promotion.train_end = "2026-08-01"

    validator = Mock()
    validator.evaluate.return_value = {
        "oos_count": 20,
        "win_rate": 0.60,
        "total_pnl": 1.0,
        "sample_quality": {"score": 80},
    }

    with patch("Crypto.promotion.CryptoForwardValidation", return_value=validator):
        score = promotion.score("candidate-a", as_of="2026-08-16")

    assert score["promotion_authority"] is False
    assert score["automatic_promotion_enabled"] is False
    assert score["eligible_for_sim"] is False
    assert score["manual_review_required"] is False
    assert score["human_approval_required"] is False
    assert score["automatic_scientific_gate_required"] is True
    assert score["lifecycle_state"] == "automatic_scientific_gate_pending"
    assert score["lifecycle_blocker"] == "crypto_c3_registry_not_implemented"
    assert score["real_execution"] is False
