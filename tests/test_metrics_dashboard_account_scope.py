from __future__ import annotations

from unittest.mock import patch

from shared.review import metrics_dashboard


def test_dashboard_exposes_money_only_by_explicit_account_scope() -> None:
    with patch(
        "shared.review.pnl_summary.sim_ledger_pnl_summary",
        return_value={
            "crypto": {
                "account_count": 2,
                "open_position_count": 2,
                "missing_mark_count": 0,
                "monetary_state": "unavailable_multiple_accounts",
                "total_pnl": None,
                "account_summaries": {
                    "crypto:simulated:grid": {
                        "account_scope": "crypto:simulated:grid",
                        "capital_layer": "simulated",
                        "total_pnl": 10.0,
                        "market_value": 100.0,
                    },
                    "crypto:simulated:momentum": {
                        "account_scope": "crypto:simulated:momentum",
                        "capital_layer": "simulated",
                        "total_pnl": -3.0,
                        "market_value": 200.0,
                    },
                },
            }
        },
    ):
        result = metrics_dashboard.compute()

    ledger = result["markets"]["Crypto"]["ledger_pnl"]
    assert ledger["account_count"] == 2
    assert ledger["monetary_aggregation"] == "forbidden_across_accounts"
    assert "total_pnl" not in ledger
    assert "market_value" not in ledger
    assert ledger["account_summaries"]["crypto:simulated:grid"]["total_pnl"] == 10.0
    assert ledger["account_summaries"]["crypto:simulated:momentum"]["total_pnl"] == -3.0


def test_dashboard_legacy_market_money_without_account_scope_fails_closed() -> None:
    with patch(
        "shared.review.pnl_summary.sim_ledger_pnl_summary",
        return_value={
            "cn_futures": {
                "total_pnl": 999.0,
                "market_value": 10_000.0,
                "open_position_count": 1,
                "missing_mark_count": 0,
            }
        },
    ):
        result = metrics_dashboard.compute()

    ledger = result["markets"]["CNFutures"]["ledger_pnl"]
    assert ledger["account_count"] == 0
    assert ledger["account_summaries"] == {}
    assert "total_pnl" not in ledger
    assert "market_value" not in ledger
