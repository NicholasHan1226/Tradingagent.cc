#!/usr/bin/env python3
"""Read-only, per-market metrics for the internal dashboard.

Monetary fields stay inside their market result.  The module deliberately
does not build a synthetic "All Markets" equity, PnL, return or drawdown.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.governance.market_lanes import ACTIVE_RUNTIME_MARKETS


OUT = Path(__file__).resolve().parent / "metrics_dashboard.json"
MARKET_CONFIG = {
    "ashare": {"display_name": "Ashare", "currency": "CNY"},
    "cn_futures": {"display_name": "CNFutures", "currency": "CNY"},
    "crypto": {"display_name": "Crypto", "currency": "USDT"},
}


def compute(review_root: Path | str | None = None) -> dict[str, Any]:
    # Keep the argument for API compatibility. Retired StyleRunner artifacts
    # under review_root are deliberately not read as current evidence.
    _ = Path(review_root) if review_root is not None else None
    metrics: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "coverage": {
            "candidates_scanned": 0,
            "signals_generated": 0,
            "coverage_pct": 0,
        },
        "success": {"signals_fired": 0, "successful": 0, "success_rate": 0},
        "markets": {},
        "all_markets": {
            "market_count": len(ACTIVE_RUNTIME_MARKETS),
            "monetary_aggregation": "forbidden",
        },
    }
    try:
        from shared.review.pnl_summary import sim_ledger_pnl_summary

        ledger_pnl = sim_ledger_pnl_summary()
    except Exception as exc:  # noqa: BLE001
        ledger_pnl = {}
        metrics["pnl_error"] = f"{exc.__class__.__name__}:{exc}"

    for market_key in ACTIVE_RUNTIME_MARKETS:
        config = MARKET_CONFIG[market_key]
        market_metrics: dict[str, Any] = {
            "market": market_key,
            "currency": config["currency"],
            "current_authority": "sim_ledger_pnl_summary",
            "retired_style_artifacts_ignored": True,
            "errors": [],
        }
        ledger = ledger_pnl.get(market_key)
        if isinstance(ledger, dict):
            raw_accounts = ledger.get("account_summaries")
            account_summaries: dict[str, dict[str, Any]] = {}
            unscoped_account_count = 0
            if isinstance(raw_accounts, dict):
                for raw_scope, raw_account in raw_accounts.items():
                    if not isinstance(raw_account, dict):
                        continue
                    account_scope = str(
                        raw_account.get("account_scope") or raw_scope or ""
                    ).strip()
                    if not account_scope:
                        unscoped_account_count += 1
                        continue
                    account_summaries[account_scope] = {
                        "market": market_key,
                        "capital_layer": str(
                            raw_account.get("capital_layer") or "simulated"
                        ),
                        "account_scope": account_scope,
                        "currency": config["currency"],
                        "realized_pnl": raw_account.get("realized_pnl"),
                        "unrealized_pnl": raw_account.get("unrealized_pnl"),
                        "total_pnl": raw_account.get("total_pnl"),
                        "market_value": raw_account.get("market_value"),
                        "open_position_count": int(
                            raw_account.get("open_position_count") or 0
                        ),
                        "missing_mark_count": int(
                            raw_account.get("missing_mark_count") or 0
                        ),
                        "pnl_source": raw_account.get("pnl_source"),
                        "monetary_state": str(
                            raw_account.get("monetary_state") or "available"
                        ),
                    }
            market_metrics["ledger_pnl"] = {
                "currency": config["currency"],
                "account_count": len(account_summaries),
                "unscoped_account_count": unscoped_account_count,
                "account_summaries": account_summaries,
                "open_position_count": ledger.get("open_position_count"),
                "missing_mark_count": ledger.get("missing_mark_count"),
                "monetary_state": ledger.get("monetary_state"),
                "monetary_aggregation": "forbidden_across_accounts",
            }
        metrics["markets"][config["display_name"]] = market_metrics
    return metrics


if __name__ == "__main__":
    import json

    output = compute()
    OUT.write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Metrics written to {OUT}")
