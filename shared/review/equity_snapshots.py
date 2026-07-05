#!/usr/bin/env python3
"""Write simulated equity snapshots from existing SimLedger states.

The dashboard treats these snapshots as the preferred source for the live
performance curve. This writer is append-only and never creates orders,
signals, receipts or real-money records.
"""

from __future__ import annotations

import argparse
import json
from datetime import date as date_cls
from pathlib import Path
from typing import Any

from shared.accounting.sim_ledger import SimLedger
from shared.review.pnl_summary import DEFAULT_SIM_LEDGER_ROOT, load_mark_prices_for_positions


DEFAULT_MARKETS = ("ashare", "crypto", "pm", "us", "cn_futures")


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else default
    except (TypeError, ValueError):
        return default


def _active_positions(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    positions = state.get("positions") if isinstance(state, dict) else {}
    if not isinstance(positions, dict):
        return {}
    active: dict[str, dict[str, Any]] = {}
    for symbol, position in positions.items():
        if not isinstance(position, dict):
            continue
        if _safe_float(position.get("quantity")) > 1e-12:
            active[str(symbol)] = position
    return active


def _discover_style_ledgers(ledger_root: Path, markets: tuple[str, ...]) -> list[tuple[str, str, Path]]:
    ledgers: list[tuple[str, str, Path]] = []
    for market in markets:
        market_dir = ledger_root / market
        if not market_dir.exists():
            continue
        for style_dir in sorted(item for item in market_dir.iterdir() if item.is_dir()):
            if (style_dir / "positions.json").exists() or (style_dir / "trade_journal.jsonl").exists():
                ledgers.append((market, style_dir.name, style_dir))
    return ledgers


def write_sim_ledger_equity_snapshots(
    *,
    markets: list[str] | tuple[str, ...] | set[str] | None = None,
    ledger_root: Path | str | None = None,
    trade_date: str | None = None,
    benchmark_return: float = 0.0,
    target_return_pct: float = 0.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Append one mark-to-market row per simulated style ledger.

    Returns an operational summary suitable for cron logs and health checks.
    """

    target_markets = tuple(str(market).lower().strip() for market in (markets or DEFAULT_MARKETS) if str(market).strip())
    root = Path(ledger_root) if ledger_root is not None else DEFAULT_SIM_LEDGER_ROOT
    snapshot_date = trade_date or date_cls.today().strftime("%Y%m%d")

    rows: list[dict[str, Any]] = []
    totals = {
        "ledger_count": 0,
        "written_count": 0,
        "skipped_count": 0,
        "open_position_count": 0,
        "missing_mark_count": 0,
        "total_equity": 0.0,
        "total_pnl": 0.0,
    }
    for market, style, style_dir in _discover_style_ledgers(root, target_markets):
        totals["ledger_count"] += 1
        state = _read_json(style_dir / "positions.json", {})
        positions = _active_positions(state)
        if not positions:
            totals["skipped_count"] += 1
            rows.append({
                "market": market,
                "style": style,
                "ledger_path": str(style_dir),
                "status": "skipped_no_open_positions",
            })
            continue

        prices = load_mark_prices_for_positions(positions, market, trade_date=snapshot_date)
        ledger = SimLedger(style_dir)
        if dry_run:
            payload = ledger.total_pnl(prices=prices)
            payload.update({
                "date": snapshot_date,
                "source": "dry_run",
                "pnl_source": "sim_ledger_mark_to_market"
                if int(payload.get("missing_mark_count") or 0) == 0
                else "sim_ledger_cost_fallback",
                "capital_layer": "simulated",
                "real_execution": False,
            })
        else:
            payload = ledger.daily_mark_to_market(
                prices,
                date=snapshot_date,
                benchmark_return=benchmark_return,
                target_return_pct=target_return_pct,
            )
            totals["written_count"] += 1

        missing = int(payload.get("missing_mark_count") or 0)
        open_count = int(payload.get("open_position_count") or len(positions))
        equity = _safe_float(payload.get("equity") or payload.get("total_equity"))
        pnl = _safe_float(payload.get("total_pnl") or payload.get("pnl"))
        totals["open_position_count"] += open_count
        totals["missing_mark_count"] += missing
        totals["total_equity"] += equity
        totals["total_pnl"] += pnl
        rows.append({
            "market": market,
            "style": style,
            "ledger_path": str(style_dir),
            "snapshot_path": str(style_dir / "daily_mark_to_market.jsonl"),
            "status": "dry_run" if dry_run else "written",
            "equity": round(equity, 8),
            "total_pnl": round(pnl, 8),
            "open_position_count": open_count,
            "missing_mark_count": missing,
            "pnl_source": payload.get("pnl_source") or "sim_ledger_mark_to_market",
        })

    totals["total_equity"] = round(totals["total_equity"], 8)
    totals["total_pnl"] = round(totals["total_pnl"], 8)
    return {
        "date": snapshot_date,
        "ledger_root": str(root),
        "markets": list(target_markets),
        "dry_run": dry_run,
        "totals": totals,
        "ledgers": rows,
    }


def _parse_markets(raw: str) -> list[str]:
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Write simulated equity snapshots for the dashboard.")
    parser.add_argument("--markets", default=",".join(DEFAULT_MARKETS), help="Comma-separated market list.")
    parser.add_argument("--ledger-root", default=str(DEFAULT_SIM_LEDGER_ROOT), help="Sim ledger root.")
    parser.add_argument("--date", default="", help="Snapshot date in YYYYMMDD. Defaults to today.")
    parser.add_argument("--benchmark-return", type=float, default=0.0, help="Benchmark return as a decimal, e.g. 0.01.")
    parser.add_argument("--target-return-pct", type=float, default=0.0, help="Target return in percent.")
    parser.add_argument("--dry-run", action="store_true", help="Inspect without writing snapshot rows.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    args = parser.parse_args()

    result = write_sim_ledger_equity_snapshots(
        markets=_parse_markets(args.markets),
        ledger_root=args.ledger_root,
        trade_date=args.date or None,
        benchmark_return=args.benchmark_return,
        target_return_pct=args.target_return_pct,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["write_sim_ledger_equity_snapshots"]
