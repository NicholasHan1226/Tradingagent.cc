#!/usr/bin/env python3
"""CLI entrypoint for automated China futures simulation lanes."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from CNFutures.adapter import CNFuturesAdapter
    from CNFutures.sim_runner import DEFAULT_MAX_INTRADAY_BAR_AGE_MINUTES, run_multi_style_simulation
else:
    from .adapter import CNFuturesAdapter
    from .sim_runner import DEFAULT_MAX_INTRADAY_BAR_AGE_MINUTES, run_multi_style_simulation


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SIGNALS_DIR = ROOT / "signals"
DEFAULT_REVIEW_PATH = ROOT / "shared" / "review" / "data" / "cn_futures_sim_reviews.jsonl"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run China futures multi-style simulation.")
    parser.add_argument("--date", default=datetime.now().strftime("%Y%m%d"), help="Trading date, default: today as YYYYMMDD.")
    parser.add_argument("--signals-dir", type=Path, default=DEFAULT_SIGNALS_DIR, help="Tradings signal state directory.")
    parser.add_argument("--review-path", type=Path, default=DEFAULT_REVIEW_PATH, help="Append-only review JSONL path.")
    parser.add_argument("--max-symbols", type=int, default=None, help="Optional cap for futures universe size.")
    parser.add_argument("--cadence", default=os.environ.get("CN_FUTURES_SIM_CADENCE", "5min"), choices=("5min", "daily"), help="Simulation cadence, default: 5min.")
    parser.add_argument(
        "--max-intraday-bar-age-minutes",
        type=float,
        default=float(os.environ.get("CN_FUTURES_MAX_INTRADAY_BAR_AGE_MINUTES", DEFAULT_MAX_INTRADAY_BAR_AGE_MINUTES)),
        help="Reject 5-minute simulation if latest bar is older than this threshold.",
    )
    parser.add_argument("--json", action="store_true", help="Print compact JSON output.")
    return parser.parse_args()


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    records = result.get("records") if isinstance(result.get("records"), list) else []
    hold_summary = result.get("hold_reason_summary") if isinstance(result.get("hold_reason_summary"), dict) else {}
    by_reason = hold_summary.get("by_reason") if isinstance(hold_summary.get("by_reason"), dict) else {}
    top_hold_reason = max(by_reason.items(), key=lambda item: int(item[1] or 0))[0] if by_reason else ""
    latest_bar_time = ""
    for record in reversed(records):
        if isinstance(record, dict) and record.get("bar_time"):
            latest_bar_time = str(record.get("bar_time"))
            break
    return {
        "market": result.get("market"),
        "reader_market": result.get("reader_market"),
        "date": result.get("date"),
        "cadence": result.get("cadence"),
        "latest_bar_time": latest_bar_time,
        "state": result.get("state"),
        "capital_layer": result.get("capital_layer"),
        "account_type": result.get("account_type"),
        "universe_count": result.get("universe_count"),
        "style_count": result.get("style_count"),
        "record_count": result.get("record_count"),
        "filled_count": result.get("filled_count"),
        "hold_count": result.get("hold_count", 0),
        "top_hold_reason": top_hold_reason,
        "error_count": len(result.get("errors") or []),
        "review_path": str(DEFAULT_REVIEW_PATH),
        "real_trading_enabled": False,
        "max_intraday_bar_age_minutes": result.get("max_intraday_bar_age_minutes"),
    }


def main() -> int:
    args = _parse_args()
    if os.environ.get("CN_FUTURES_SIM_DISABLED", "").strip().lower() in {"1", "true", "yes"}:
        output = {
            "market": "cn_futures",
            "reader_market": "Futures",
            "date": args.date,
            "cadence": args.cadence,
            "latest_bar_time": "",
            "state": "paused",
            "capital_layer": "simulated",
            "account_type": "simulated",
            "universe_count": 0,
            "style_count": 0,
            "record_count": 0,
            "filled_count": 0,
            "error_count": 0,
            "review_path": str(args.review_path),
            "real_trading_enabled": False,
            "max_intraday_bar_age_minutes": args.max_intraday_bar_age_minutes,
            "disabled_reason": "CN_FUTURES_SIM_DISABLED is set",
        }
        if args.json:
            print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        else:
            print(
                "CNFutures simulation "
                f"cadence={output['cadence']} state={output['state']} filled={output['filled_count']} "
                f"styles={output['style_count']} universe={output['universe_count']} "
                f"real_trading_enabled={output['real_trading_enabled']}"
            )
        return 0
    universe_filter = {}
    if args.max_symbols is not None:
        universe_filter["max_symbols"] = max(1, args.max_symbols)
    try:
        adapter = CNFuturesAdapter(universe_filter=universe_filter)
        result = run_multi_style_simulation(
            adapter,
            str(args.date),
            adapter.reader or adapter,
            signals_dir=args.signals_dir,
            review_path=args.review_path,
            cadence=args.cadence,
            max_intraday_bar_age_minutes=args.max_intraday_bar_age_minutes,
        )
        output = _summary(result)
    except Exception as exc:  # noqa: BLE001
        output = {
            "market": "cn_futures",
            "reader_market": "Futures",
            "date": args.date,
            "cadence": args.cadence,
            "latest_bar_time": "",
            "state": "error",
            "capital_layer": "simulated",
            "account_type": "simulated",
            "universe_count": 0,
            "style_count": 0,
            "record_count": 0,
            "filled_count": 0,
            "hold_count": 0,
            "top_hold_reason": "",
            "error_count": 1,
            "errors": [f"{exc.__class__.__name__}: {exc}"],
            "review_path": str(args.review_path),
            "real_trading_enabled": False,
            "max_intraday_bar_age_minutes": args.max_intraday_bar_age_minutes,
        }
    output["review_path"] = str(args.review_path)
    if args.json:
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    else:
        print(
            "CNFutures simulation "
            f"cadence={output['cadence']} state={output['state']} filled={output['filled_count']} "
            f"styles={output['style_count']} universe={output['universe_count']} "
            f"real_trading_enabled={output['real_trading_enabled']}"
        )
    return 0 if output["state"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
