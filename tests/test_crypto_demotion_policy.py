"""Tests for Crypto Demotion Policy."""

import pytest
from Crypto.demotion_policy import DemotionPolicy, DemotionCriteria


class TestDemotionPolicy:
    """Test DemotionPolicy."""
    
    def test_create_policy(self):
        """Test creating a demotion policy."""
        policy = DemotionPolicy()
        assert policy.criteria.min_consecutive_failures == 3
        assert policy.criteria.max_drawdown_pct == 10.0
    
    def test_create_policy_with_custom_criteria(self):
        """Test creating a policy with custom criteria."""
        criteria = DemotionCriteria(
            min_consecutive_failures=5,
            max_drawdown_pct=15.0,
        )
        policy = DemotionPolicy(criteria)
        assert policy.criteria.min_consecutive_failures == 5
        assert policy.criteria.max_drawdown_pct == 15.0
    
    def test_evaluate_no_demotion(self):
        """Test evaluation with good performance."""
        policy = DemotionPolicy()
        performance = {
            "consecutive_failures": 0,
            "drawdown_pct": 2.0,
            "sharpe_ratio": 1.5,
            "win_rate": 0.6,
        }
        result = policy.evaluate("champ_001", performance)
        assert result["should_demote"] is False
        assert len(result["reasons"]) == 0
    
    def test_evaluate_demote_consecutive_failures(self):
        """Test demotion due to consecutive failures."""
        policy = DemotionPolicy()
        performance = {
            "consecutive_failures": 5,
            "drawdown_pct": 2.0,
            "sharpe_ratio": 1.5,
            "win_rate": 0.6,
        }
        result = policy.evaluate("champ_001", performance)
        assert result["should_demote"] is True
        assert len(result["reasons"]) > 0
        assert "consecutive_failures" in result["reasons"][0]
    
    def test_evaluate_demote_drawdown(self):
        """Test demotion due to high drawdown."""
        policy = DemotionPolicy()
        performance = {
            "consecutive_failures": 0,
            "drawdown_pct": 15.0,
            "sharpe_ratio": 1.5,
            "win_rate": 0.6,
        }
        result = policy.evaluate("champ_001", performance)
        assert result["should_demote"] is True
        assert any("drawdown" in r for r in result["reasons"])
    
    def test_evaluate_demote_low_sharpe(self):
        """Test demotion due to low Sharpe ratio."""
        policy = DemotionPolicy()
        performance = {
            "consecutive_failures": 0,
            "drawdown_pct": 2.0,
            "sharpe_ratio": 0.3,
            "win_rate": 0.6,
        }
        result = policy.evaluate("champ_001", performance)
        assert result["should_demote"] is True
        assert any("sharpe" in r for r in result["reasons"])
    
    def test_evaluate_demote_low_win_rate(self):
        """Test demotion due to low win rate."""
        policy = DemotionPolicy()
        performance = {
            "consecutive_failures": 0,
            "drawdown_pct": 2.0,
            "sharpe_ratio": 1.5,
            "win_rate": 0.3,
        }
        result = policy.evaluate("champ_001", performance)
        assert result["should_demote"] is True
        assert any("win_rate" in r for r in result["reasons"])
    
    def test_evaluate_multiple_reasons(self):
        """Test demotion with multiple reasons."""
        policy = DemotionPolicy()
        performance = {
            "consecutive_failures": 5,
            "drawdown_pct": 15.0,
            "sharpe_ratio": 0.3,
            "win_rate": 0.3,
        }
        result = policy.evaluate("champ_001", performance)
        assert result["should_demote"] is True
        assert len(result["reasons"]) == 4
