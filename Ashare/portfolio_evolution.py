#!/usr/bin/env python3
"""A-share portfolio-level evolution evidence.

The A-share server-local simulator does not run the multi-style evolution
engine used by Crypto/PM/US. This module records whether strategy-valid
portfolio samples reached the review/evolution layer without pretending that a
single A-share account has per-style attribution.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from shared.review.pnl_summary import sim_ledger_pnl_summary
from shared.review.sample_quality import strategy_valid_trades, summarize_sample_quality
from shared.review.sim_ledger_reader import load_sim_trades_between, load_sim_trades_for_date


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REVIEW_DIR = ROOT / "shared" / "review" / "ashare"
LATEST_PATH = DEFAULT_REVIEW_DIR / "portfolio_evolution_latest.json"
LOG_PATH = DEFAULT_REVIEW_DIR / "portfolio_evolution_log.jsonl"


def _today_compact() -> str:
    return date.today().strftime("%Y%m%d")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed == parsed else default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _load_tier_manifest(review_dir: Path) -> dict[str, Any]:
    path = review_dir / "tier_experiments_latest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _tier_rankings(tier_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    rankings: list[dict[str, Any]] = []
    accounts = tier_manifest.get("accounts") if isinstance(tier_manifest.get("accounts"), list) else []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        pnl = account.get("pnl") if isinstance(account.get("pnl"), dict) else {}
        rankings.append(
            {
                "style_name": str(account.get("account") or ""),
                "trades": _safe_int(account.get("trade_count")),
                "pnl": round(_safe_float(pnl.get("total_pnl")), 6),
                "realized_pnl": round(_safe_float(pnl.get("realized_pnl")), 6),
                "unrealized_pnl": round(_safe_float(pnl.get("unrealized_pnl")), 6),
                "capital": _safe_float(account.get("capital")),
                "pnl_source": "ashare_capital_tier_experiment",
            }
        )
    return rankings


def _action_for_samples(
    *,
    strategy_sample_count: int,
    pnl: dict[str, Any],
    min_samples: int,
) -> tuple[str, str]:
    if strategy_sample_count <= 0:
        return "wait_for_strategy_samples", "no_strategy_valid_samples"
    if strategy_sample_count < min_samples:
        return "observe", "sample_insufficient"
    total_pnl = _safe_float(pnl.get("total_pnl"))
    if total_pnl < 0:
        return "tighten_risk", "negative_mark_to_market_pnl"
    if total_pnl > 0:
        return "expand_risk", "positive_mark_to_market_pnl"
    return "observe", "flat_mark_to_market_pnl"


def build_portfolio_evolution(
    *,
    trade_date: str | None = None,
    review_dir: Path | str | None = None,
    local_trades_path: Path | str | None = None,
    min_samples: int = 5,
) -> dict[str, Any]:
    """Build read-only portfolio evolution evidence for A-share."""

    target_date = str(trade_date or _today_compact()).replace("-", "")[:8]
    review_path = Path(review_dir) if review_dir is not None else DEFAULT_REVIEW_DIR
    local_path = Path(local_trades_path) if local_trades_path is not None else None
    day_trades = load_sim_trades_for_date(target_date, markets=("ashare",), local_trades_path=local_path)
    all_trades = load_sim_trades_between("19000101", target_date, markets=("ashare",), local_trades_path=local_path)
    day_quality = summarize_sample_quality(day_trades)
    cumulative_quality = summarize_sample_quality(all_trades)
    day_strategy_trades = strategy_valid_trades(day_trades)
    cumulative_strategy_trades = strategy_valid_trades(all_trades)
    pnl_by_market = sim_ledger_pnl_summary(markets=("ashare",), local_trades_path=local_path)
    pnl = pnl_by_market.get("ashare", {})
    tier_manifest = _load_tier_manifest(review_path)
    tier_rankings = _tier_rankings(tier_manifest)
    strategy_sample_count = _safe_int(cumulative_quality.get("strategy_sample_valid_count"))
    action, reason = _action_for_samples(
        strategy_sample_count=strategy_sample_count,
        pnl=pnl,
        min_samples=max(1, int(min_samples)),
    )
    state = "observed" if strategy_sample_count > 0 else "waiting"
    if reason == "sample_insufficient":
        state = "sample_insufficient"

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "market": "ashare",
        "trade_date": target_date,
        "state": state,
        "actions": [
            {
                "action": action,
                "reason": reason,
                "min_samples": max(1, int(min_samples)),
                "strategy_sample_count": strategy_sample_count,
                "total_pnl": round(_safe_float(pnl.get("total_pnl")), 6),
            }
        ],
        "rankings": [
            {
                "style_name": "ashare_portfolio",
                "trades": strategy_sample_count,
                "pnl": round(_safe_float(pnl.get("total_pnl")), 6),
                "realized_pnl": round(_safe_float(pnl.get("realized_pnl")), 6),
                "unrealized_pnl": round(_safe_float(pnl.get("unrealized_pnl")), 6),
                "pnl_source": pnl.get("pnl_source", ""),
            }
        ] + tier_rankings,
        "weights": {
            "ashare_portfolio": {
                "status": "active",
                "weight": 1.0,
                "scope": "portfolio_account",
            }
        },
        "sample_quality": {
            "today": day_quality,
            "cumulative": cumulative_quality,
        },
        "strategy_sample_count": strategy_sample_count,
        "today_strategy_sample_count": len(day_strategy_trades),
        "cumulative_strategy_sample_count": len(cumulative_strategy_trades),
        "validation_sample_count": _safe_int(cumulative_quality.get("validation_sample_count")),
        "tier_experiments": {
            "account_count": len(tier_rankings),
            "accounts": [
                {
                    "account": row.get("style_name"),
                    "capital": row.get("capital"),
                    "trades": row.get("trades"),
                    "pnl": row.get("pnl"),
                }
                for row in tier_rankings
            ],
        },
        "pnl": {
            "total_pnl": round(_safe_float(pnl.get("total_pnl")), 6),
            "realized_pnl": round(_safe_float(pnl.get("realized_pnl")), 6),
            "unrealized_pnl": round(_safe_float(pnl.get("unrealized_pnl")), 6),
            "strategy_total_pnl": round(_safe_float(pnl.get("strategy_total_pnl")), 6),
            "market_value": round(_safe_float(pnl.get("market_value")), 6),
            "cash": pnl.get("cash"),
            "equity": pnl.get("equity"),
            "open_position_count": _safe_int(pnl.get("open_position_count")),
            "missing_mark_count": _safe_int(pnl.get("missing_mark_count")),
            "pnl_source": pnl.get("pnl_source", ""),
        },
        "read_only": True,
        "real_trading_enabled": False,
    }
    report["latest_path"] = _display_path(review_path / LATEST_PATH.name)
    report["log_path"] = _display_path(review_path / LOG_PATH.name)
    return report


def write_portfolio_evolution(
    *,
    trade_date: str | None = None,
    review_dir: Path | str | None = None,
    local_trades_path: Path | str | None = None,
    min_samples: int = 5,
) -> dict[str, Any]:
    review_path = Path(review_dir) if review_dir is not None else DEFAULT_REVIEW_DIR
    review_path.mkdir(parents=True, exist_ok=True)
    from Ashare.tier_experiments import write_tier_ledgers

    write_tier_ledgers(source_trades_path=local_trades_path, review_dir=review_path)
    report = build_portfolio_evolution(
        trade_date=trade_date,
        review_dir=review_path,
        local_trades_path=local_trades_path,
        min_samples=min_samples,
    )
    latest = review_path / LATEST_PATH.name
    log = review_path / LOG_PATH.name
    latest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", default="")
    parser.add_argument("--review-dir", type=Path, default=DEFAULT_REVIEW_DIR)
    parser.add_argument("--local-trades-path", type=Path, default=None)
    parser.add_argument("--min-samples", type=int, default=5)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)

    if args.write:
        report = write_portfolio_evolution(
            trade_date=args.trade_date or None,
            review_dir=args.review_dir,
            local_trades_path=args.local_trades_path,
            min_samples=args.min_samples,
        )
    else:
        report = build_portfolio_evolution(
            trade_date=args.trade_date or None,
            review_dir=args.review_dir,
            local_trades_path=args.local_trades_path,
            min_samples=args.min_samples,
        )
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
