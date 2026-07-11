#!/usr/bin/env python3
"""Refresh A-share simulated accounts from exact post-close daily bars."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from Ashare.forward_validation import build_forward_validation_report
from Ashare.epoch_review import validate_review_epoch
from Ashare.portfolio_evolution import write_portfolio_evolution
from shared.data.reader import TradingagentDataReader
from shared.execution import local_sim_ledger
from shared.execution.sim_account_epoch import epoch_capital_cny, read_epoch_state
from shared.review.daily_review import run_daily_review


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REVIEW_DIR = ROOT / "shared" / "review" / "ashare"
PRICE_SEMANTICS = "formal_daily_close_exact_trade_date"
REPORT_SCHEMA_VERSION = 2


def _compact_date(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())[:8]


def _safe_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed > 0 and parsed == parsed else 0.0


def _epoch_fields(epoch_state: dict[str, Any]) -> dict[str, Any]:
    epoch_id = int(epoch_state.get("current_epoch_id") or 1)
    return {
        "capital_epoch": epoch_id,
        "capital_cny": float(epoch_state.get("capital_cny") or epoch_capital_cny(epoch_id)),
        "epoch_cutover_timestamp": str(
            epoch_state.get("cutover_timestamp") or epoch_state.get("activated_at") or ""
        ),
    }


def load_formal_close_prices(
    trade_date: str,
    positions: dict[str, dict[str, Any]],
    *,
    reader: Any | None = None,
) -> dict[str, Any]:
    target_date = _compact_date(trade_date)
    active_reader = reader or TradingagentDataReader()
    prices: dict[str, float] = {}
    missing: list[str] = []
    for symbol in sorted(positions):
        try:
            rows = active_reader.get_bars_daily("Ashare", symbol, target_date, target_date) or []
        except Exception:  # noqa: BLE001
            rows = []
        exact = next(
            (
                row for row in rows
                if isinstance(row, dict)
                and _compact_date(str(row.get("trade_date") or row.get("date") or "")) == target_date
                and _safe_float(row.get("close")) > 0
            ),
            None,
        )
        if exact is None:
            missing.append(symbol)
            continue
        prices[symbol] = _safe_float(exact.get("close"))
    return {
        "status": "pass" if not missing else "fail",
        "trade_date": target_date,
        "position_count": len(positions),
        "price_count": len(prices),
        "prices": prices,
        "missing_symbols": missing,
        "price_semantics": PRICE_SEMANTICS,
        "data_source": "SharedSignals API exact-date daily bars",
    }


def _write_report(review_dir: Path, report: dict[str, Any]) -> None:
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "formal_close_latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with (review_dir / "formal_close_history.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")


def _completed_report(
    review_dir: Path,
    trade_date: str,
    epoch_fields: dict[str, Any],
) -> dict[str, Any] | None:
    latest = review_dir / "formal_close_latest.json"
    if not latest.exists():
        return None
    try:
        report = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(report, dict):
        return None
    epoch_id = int(epoch_fields["capital_epoch"])
    if epoch_id > 1:
        epoch_matches, _ = validate_review_epoch(
            report,
            current_epoch_id=epoch_id,
            current_cutover_timestamp=str(epoch_fields["epoch_cutover_timestamp"]),
        )
    else:
        epoch_matches = int(report.get("capital_epoch") or 1) == 1
    if (
        epoch_matches
        and report.get("status") == "pass"
        and int(report.get("schema_version") or 0) >= REPORT_SCHEMA_VERSION
        and _compact_date(str(report.get("trade_date") or "")) == trade_date
    ):
        return {**report, "idempotent_skip": True}
    return None


def _ashare_daily_review_summary(review: dict[str, Any]) -> dict[str, Any]:
    layers = review.get("capital_layer_reviews") if isinstance(review, dict) else None
    simulated = layers.get("simulated") if isinstance(layers, dict) else None
    markets = simulated.get("market_reviews") if isinstance(simulated, dict) else None
    ashare = markets.get("ashare") if isinstance(markets, dict) else None
    if not isinstance(ashare, dict):
        return {"status": "unavailable"}
    return {
        "status": "current" if not ashare.get("stale") else "stale",
        "ledger_realized_pnl": ashare.get("ledger_realized_pnl"),
        "ledger_unrealized_pnl": ashare.get("ledger_unrealized_pnl"),
        "ledger_total_pnl": ashare.get("ledger_total_pnl"),
        "ledger_market_value": ashare.get("ledger_market_value"),
        "ledger_pnl_source": ashare.get("ledger_pnl_source"),
    }


def run_formal_close_refresh(
    *,
    trade_date: str = "",
    reader: Any | None = None,
    review_dir: Path = DEFAULT_REVIEW_DIR,
) -> dict[str, Any]:
    target_date = _compact_date(trade_date) or date.today().strftime("%Y%m%d")
    epoch_fields = _epoch_fields(read_epoch_state())
    completed = _completed_report(review_dir, target_date, epoch_fields)
    if completed is not None:
        return completed
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    pnl = local_sim_ledger.get_local_sim_pnl(account=None, mark_prices=None)
    positions = pnl.get("positions") if isinstance(pnl, dict) else {}
    positions = positions if isinstance(positions, dict) else {}
    close_evidence = load_formal_close_prices(target_date, positions, reader=reader)
    base = {
        **epoch_fields,
        "report_type": "ashare_formal_close_refresh",
        "schema_version": REPORT_SCHEMA_VERSION,
        "market": "ashare",
        "trade_date": target_date,
        "generated_at": generated_at,
        "price_semantics": PRICE_SEMANTICS,
        "real_trading_enabled": False,
        "close_evidence": close_evidence,
    }
    if not positions:
        report = {**base, "status": "pass", "reason": "no_open_positions"}
        _write_report(review_dir, report)
        return report
    if close_evidence["status"] != "pass":
        report = {**base, "status": "fail", "reason": "formal_close_incomplete"}
        _write_report(review_dir, report)
        return report

    prices = dict(close_evidence["prices"])
    snapshot = local_sim_ledger.refresh_local_sim_snapshot(mark_prices=prices)
    formal_pnl = local_sim_ledger.get_local_sim_pnl(account=None, mark_prices=prices)
    portfolio = write_portfolio_evolution(
        trade_date=target_date,
        review_dir=review_dir,
        mark_prices=prices,
    )
    forward = build_forward_validation_report(
        date=target_date,
        reader=reader,
        output=review_dir / "forward_validation_latest.json",
        history=review_dir / "forward_validation.jsonl",
    )
    daily = run_daily_review(target_date, session="close")
    report = {
        **base,
        "status": "pass",
        "reason": "formal_close_refresh_complete",
        "snapshot": snapshot,
        "formal_pnl": {
            key: formal_pnl.get(key)
            for key in (
                "realized_pnl",
                "unrealized_pnl",
                "total_pnl",
                "market_value",
                "cash_available",
            )
        },
        "portfolio_evolution": {
            "state": portfolio.get("state"),
            "valuation_status": portfolio.get("valuation_status"),
            "tier_experiment_refresh": portfolio.get("tier_experiment_refresh"),
        },
        "forward_validation": {
            "strategy_label_count": forward.get("strategy_label_count"),
            "pending_count": forward.get("pending_count"),
        },
        "daily_review": {
            "session": daily.get("session"),
            **_ashare_daily_review_summary(daily),
        },
    }
    _write_report(review_dir, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", default="")
    parser.add_argument("--review-dir", type=Path, default=DEFAULT_REVIEW_DIR)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    report = run_formal_close_refresh(
        trade_date=args.trade_date,
        review_dir=args.review_dir,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return 0 if report.get("status") == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
