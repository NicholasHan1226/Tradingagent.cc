from __future__ import annotations

from unittest.mock import patch

from shared.review.weekly_review import review_week


def test_two_positive_weeks_only_nominate_manual_review_never_auto_promote() -> None:
    state = {
        "strategies": {
            "ashare:simulated:ashare_sim:trend_breakout": {
                "consecutive_positive_weeks": 1,
                "consecutive_below50_weeks": 0,
            }
        }
    }
    trades = [
        {
            "market": "ashare",
            "capital_layer": "simulated",
            "account_scope": "ashare_sim",
            "strategy": "trend_breakout",
            "pnl": 20.0,
            "status": "filled",
            "fill_price": 10.0,
            "quantity": 100,
        }
    ]
    with (
        patch("shared.review.weekly_review._read_json", return_value=state),
        patch("shared.review.weekly_review._write_json"),
        patch("shared.review.weekly_review._append_log"),
        patch("shared.review.weekly_review.sim_ledger_pnl_summary", return_value={}),
        patch("shared.review.weekly_review.strategy_valid_trades", return_value=trades),
    ):
        result = review_week(trades, strategies=["trend_breakout"])

    account = result["market_reviews"]["ashare"]["capital_layer_reviews"]["simulated"][
        "account_reviews"
    ]["ashare_sim"]
    assert account["strategies_to_promote"] == []
    assert account["strategies_for_manual_review"] == ["trend_breakout"]
    assert account["automatic_promotion_enabled"] is False
    assert account["automatic_risk_expansion_enabled"] is False
