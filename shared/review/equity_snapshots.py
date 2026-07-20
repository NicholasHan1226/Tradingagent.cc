#!/usr/bin/env python3
"""Write simulated equity snapshots from existing SimLedger states.

The dashboard treats these snapshots as the preferred source for the live
performance curve. This writer is append-only and never creates orders,
signals, receipts or real-money records.
"""

from __future__ import annotations

import argparse
import json
from datetime import date as date_cls, datetime, timezone
from pathlib import Path
from typing import Any

from shared.accounting.sim_ledger import SimLedger
from shared.execution.local_sim_ledger import (
    DEFAULT_ACCOUNT as ASHARE_SIM_ACCOUNT_SCOPE,
)
from shared.governance.market_lanes import (
    ACTIVE_RUNTIME_MARKETS,
    canonical_runtime_market,
)
from shared.markets.sim_capital import default_sim_capital
from shared.review.pnl_summary import (
    DEFAULT_SIM_LEDGER_ROOT,
    load_mark_prices_for_positions,
)


DEFAULT_MARKETS = ACTIVE_RUNTIME_MARKETS
DEFAULT_LOCAL_SIM_DIR = DEFAULT_SIM_LEDGER_ROOT.parent / "local_sim"
DEFAULT_ASHARE_SIM_CAPITAL = default_sim_capital("ashare")
MARKET_CURRENCIES = {
    "ashare": "CNY",
    "cn_futures": "CNY",
    "crypto": "USDT",
}


def _style_account_scope(market: str, style: str) -> str:
    market_key = canonical_runtime_market(market)
    style_key = str(style or "").strip()
    if not style_key:
        raise ValueError("style_ledger_account_scope_unavailable")
    return f"{market_key}:simulated:{style_key}"


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


def _market_currency(market: str) -> str:
    return MARKET_CURRENCIES[canonical_runtime_market(market)]


def _with_native_currency_fields(
    payload: dict[str, Any], market: str
) -> dict[str, Any]:
    """Label native amounts without inventing a cross-market FX authority."""

    currency = _market_currency(market)
    payload["currency"] = currency
    payload["display_currency"] = currency
    payload["fx_conversion_status"] = "not_applied"
    return payload


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


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _append_jsonl(path: Path, payload: dict[str, Any], *, dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _account_mtm_drawdown(
    rows: list[dict[str, Any]],
    *,
    current_date: str,
    current_equity: float,
    capital_base: float,
) -> dict[str, Any]:
    """Rebuild drawdown from one authoritative account equity per trading day."""

    by_date: dict[str, float] = {}
    for row in rows:
        trade_date = str(row.get("date") or "").replace("-", "")[:8]
        equity = _safe_float(row.get("equity", row.get("total_equity")), 0.0)
        if len(trade_date) == 8 and equity > 0.0:
            by_date[trade_date] = equity
    by_date[str(current_date).replace("-", "")[:8]] = current_equity
    peak = capital_base if capital_base > 0.0 else current_equity
    max_drawdown = 0.0
    max_drawdown_peak = peak
    max_drawdown_trough = peak
    current_drawdown = 0.0
    for trade_date in sorted(by_date):
        equity = by_date[trade_date]
        peak = max(peak, equity)
        current_drawdown = peak - equity
        if current_drawdown > max_drawdown:
            max_drawdown = current_drawdown
            max_drawdown_peak = peak
            max_drawdown_trough = equity
    return {
        "max_drawdown_cny": round(max_drawdown, 6),
        "max_drawdown_pct": round(
            max_drawdown / max_drawdown_peak * 100.0
            if max_drawdown_peak > 0.0
            else 0.0,
            6,
        ),
        "current_drawdown_cny": round(current_drawdown, 6),
        "drawdown_peak_equity_cny": round(max_drawdown_peak, 6),
        "drawdown_trough_equity_cny": round(max_drawdown_trough, 6),
        "drawdown_observation_count": len(by_date),
        "drawdown_source": "account_daily_mtm_equity",
    }


def _ashare_starting_cash(local_sim_dir: Path) -> float:
    try:
        import os

        configured = _safe_float(os.environ.get("ASHARE_SIM_INITIAL_CASH"), 0.0)
        if configured > 0:
            return configured
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_ASHARE_SIM_CAPITAL


def _replay_ashare_local_sim(
    local_sim_dir: Path,
    mark_prices: dict[str, float] | None = None,
    *,
    starting_cash: float = DEFAULT_ASHARE_SIM_CAPITAL,
) -> dict[str, Any]:
    positions: dict[str, dict[str, Any]] = {}
    realized_pnl = 0.0
    trade_count = 0
    buys = 0
    sells = 0
    cash_available = float(starting_cash)
    for trade in _read_jsonl(local_sim_dir / "local_sim_trades.jsonl"):
        if str(trade.get("status") or "") != "filled":
            continue
        code = str(trade.get("ts_code") or trade.get("symbol") or "").strip().upper()
        if not code:
            continue
        qty = _safe_float(trade.get("quantity"), 0.0)
        if qty <= 0:
            continue
        side = str(trade.get("side") or "").lower()
        net_amount = _safe_float(trade.get("net_amount"), 0.0)
        filled_price = _safe_float(trade.get("filled_price"), 0.0)
        position = positions.setdefault(
            code, {"quantity": 0.0, "cost_basis": 0.0, "last_price": 0.0}
        )
        trade_count += 1
        if side == "buy":
            cash_available -= net_amount
            position["quantity"] += qty
            position["cost_basis"] += net_amount
            position["last_price"] = filled_price or position["last_price"]
            buys += 1
            continue
        if side != "sell" or position["quantity"] <= 0:
            continue
        sells += 1
        cash_available += net_amount
        sell_qty = min(qty, position["quantity"])
        avg_cost = (
            position["cost_basis"] / position["quantity"]
            if position["quantity"]
            else 0.0
        )
        released_cost = round(avg_cost * sell_qty, 2)
        position["quantity"] -= sell_qty
        position["cost_basis"] = round(position["cost_basis"] - released_cost, 2)
        position["last_price"] = filled_price or position["last_price"]
        realized_pnl += net_amount - released_cost
        if position["quantity"] <= 0:
            position["quantity"] = 0.0
            position["cost_basis"] = 0.0

    clean_positions: dict[str, dict[str, Any]] = {}
    market_value = 0.0
    unrealized_pnl = 0.0
    missing_mark_count = 0
    for code, position in positions.items():
        qty = _safe_float(position.get("quantity"), 0.0)
        if qty <= 0:
            continue
        cost = round(_safe_float(position.get("cost_basis"), 0.0), 2)
        last_price = _safe_float(position.get("last_price"), 0.0)
        if mark_prices is not None and code in mark_prices:
            mark_price = _safe_float(mark_prices.get(code), last_price)
        else:
            mark_price = last_price
            if mark_prices is not None:
                missing_mark_count += 1
        value = round(qty * mark_price, 2)
        row_unrealized = round(value - cost, 2)
        clean_positions[code] = {
            "quantity": int(qty) if abs(qty - round(qty)) < 1e-12 else round(qty, 6),
            "cost_basis": cost,
            "avg_cost": round(cost / qty, 4) if qty else 0.0,
            "last_price": round(last_price, 6),
            "mark_price": round(mark_price, 6),
            "market_value": value,
            "unrealized_pnl": row_unrealized,
        }
        market_value += value
        unrealized_pnl += row_unrealized

    return {
        "total_trades": trade_count,
        "buys": buys,
        "sells": sells,
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "market_value": round(market_value, 2),
        "total_pnl": round(realized_pnl + unrealized_pnl, 2),
        "cash_available": round(cash_available, 2),
        "positions": clean_positions,
        "missing_mark_count": missing_mark_count,
    }


def _write_ashare_local_sim_snapshot(
    *,
    ledger_root: Path,
    local_sim_dir: Path,
    snapshot_date: str,
    benchmark_return: float | None,
    target_return_pct: float,
    dry_run: bool,
) -> dict[str, Any]:
    capital_base = _ashare_starting_cash(local_sim_dir)
    no_mark_pnl = _replay_ashare_local_sim(
        local_sim_dir, mark_prices=None, starting_cash=capital_base
    )
    positions = no_mark_pnl.get("positions") if isinstance(no_mark_pnl, dict) else {}
    if not isinstance(positions, dict):
        positions = {}
    prices = load_mark_prices_for_positions(
        positions, "ashare", trade_date=snapshot_date
    )
    pnl = _replay_ashare_local_sim(
        local_sim_dir, mark_prices=prices, starting_cash=capital_base
    )
    total_pnl = _safe_float(pnl.get("total_pnl"), 0.0)
    cash = round(_safe_float(pnl.get("cash_available"), capital_base), 6)
    market_value = round(_safe_float(pnl.get("market_value"), 0.0), 6)
    equity = round(cash + market_value, 6)
    missing = int(pnl.get("missing_mark_count") or 0)
    benchmark_available = benchmark_return is not None
    benchmark_value = float(benchmark_return) if benchmark_available else None
    benchmark_pnl = equity * benchmark_value if benchmark_value is not None else None
    snapshot_path = ledger_root / "ashare" / "ashare_sim" / "daily_mark_to_market.jsonl"
    drawdown = _account_mtm_drawdown(
        _read_jsonl(snapshot_path),
        current_date=snapshot_date,
        current_equity=equity,
        capital_base=capital_base,
    )
    snapshot = {
        "account_scope": ASHARE_SIM_ACCOUNT_SCOPE,
        "account_scope_source": "documented_single_ashare_sim_account",
        "account_type": "simulated",
        "benchmark_pnl": round(benchmark_pnl, 6) if benchmark_pnl is not None else None,
        "benchmark_return": benchmark_value,
        "benchmark_return_pct": (
            round(benchmark_value * 100.0, 6) if benchmark_value is not None else None
        ),
        "benchmark_status": "available" if benchmark_available else "unavailable",
        "capital_base": round(capital_base, 6),
        "capital_layer": "simulated",
        "cash": cash,
        "date": snapshot_date,
        "equity": equity,
        "market": "ashare",
        "market_value": market_value,
        **drawdown,
        "missing_mark_count": missing,
        "open_position_count": len(pnl.get("positions") or {}),
        "pnl": total_pnl,
        "pnl_vs_benchmark": (
            round(total_pnl - benchmark_pnl, 6) if benchmark_pnl is not None else None
        ),
        "pnl_source": "ashare_local_sim_mark_to_market"
        if missing == 0
        else "ashare_local_sim_trade_price_fallback",
        "positions": list((pnl.get("positions") or {}).values()),
        "real_execution": False,
        "realized_pnl": round(_safe_float(pnl.get("realized_pnl"), 0.0), 6),
        "return_pct": round(
            (total_pnl / capital_base * 100.0) if capital_base else 0.0, 6
        ),
        "source": "ashare_local_sim_daily_mark_to_market",
        "style": "ashare_sim",
        "target_return_pct": target_return_pct,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_equity": equity,
        "total_pnl": total_pnl,
        "trade_count": int(pnl.get("total_trades") or 0),
        "unrealized_pnl": round(_safe_float(pnl.get("unrealized_pnl"), 0.0), 6),
    }
    _with_native_currency_fields(snapshot, "ashare")
    _append_jsonl(snapshot_path, snapshot, dry_run=dry_run)
    return {
        "market": "ashare",
        "style": "ashare_sim",
        "account_scope": ASHARE_SIM_ACCOUNT_SCOPE,
        "account_scope_source": "documented_single_ashare_sim_account",
        "ledger_path": str(local_sim_dir),
        "snapshot_path": str(snapshot_path),
        "status": "dry_run" if dry_run else "written",
        "equity": round(equity, 8),
        "total_pnl": round(total_pnl, 8),
        "open_position_count": int(snapshot["open_position_count"]),
        "missing_mark_count": missing,
        "pnl_source": snapshot["pnl_source"],
    }


def _discover_style_ledgers(
    ledger_root: Path, markets: tuple[str, ...]
) -> list[tuple[str, str, Path]]:
    ledgers: list[tuple[str, str, Path]] = []
    for market in markets:
        market_dir = ledger_root / market
        if not market_dir.exists():
            continue
        for style_dir in sorted(item for item in market_dir.iterdir() if item.is_dir()):
            if (style_dir / "positions.json").exists() or (
                style_dir / "trade_journal.jsonl"
            ).exists():
                ledgers.append((market, style_dir.name, style_dir))
    return ledgers


def write_sim_ledger_equity_snapshots(
    *,
    markets: list[str] | tuple[str, ...] | set[str] | None = None,
    ledger_root: Path | str | None = None,
    local_sim_dir: Path | str | None = None,
    trade_date: str | None = None,
    benchmark_return: float | None = None,
    target_return_pct: float = 0.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Append one mark-to-market row per simulated style ledger.

    Returns an operational summary suitable for cron logs and health checks.
    """

    requested_markets = DEFAULT_MARKETS if markets is None else tuple(markets)
    target_markets = tuple(
        dict.fromkeys(canonical_runtime_market(market) for market in requested_markets)
    )
    if benchmark_return is not None and len(target_markets) != 1:
        raise ValueError("benchmark_return_requires_exactly_one_market")
    root = Path(ledger_root) if ledger_root is not None else DEFAULT_SIM_LEDGER_ROOT
    local_root = (
        Path(local_sim_dir) if local_sim_dir is not None else DEFAULT_LOCAL_SIM_DIR
    )
    snapshot_date = trade_date or date_cls.today().strftime("%Y%m%d")

    totals = {
        "ledger_count": 0,
        "written_count": 0,
        "skipped_count": 0,
        "open_position_count": 0,
        "missing_mark_count": 0,
    }
    per_market: dict[str, dict[str, Any]] = {
        market: {
            "currency": _market_currency(market),
            "ledger_count": 0,
            "written_count": 0,
            "skipped_count": 0,
            "open_position_count": 0,
            "missing_mark_count": 0,
            "ledgers": [],
        }
        for market in target_markets
    }
    if "ashare" in target_markets:
        ashare_row = _write_ashare_local_sim_snapshot(
            ledger_root=root,
            local_sim_dir=local_root,
            snapshot_date=snapshot_date,
            benchmark_return=benchmark_return,
            target_return_pct=target_return_pct,
            dry_run=dry_run,
        )
        totals["ledger_count"] += 1
        totals["written_count"] += 0 if dry_run else 1
        totals["open_position_count"] += int(ashare_row.get("open_position_count") or 0)
        totals["missing_mark_count"] += int(ashare_row.get("missing_mark_count") or 0)
        per_market["ashare"]["ledger_count"] += 1
        per_market["ashare"]["written_count"] += 0 if dry_run else 1
        per_market["ashare"]["open_position_count"] += int(
            ashare_row.get("open_position_count") or 0
        )
        per_market["ashare"]["missing_mark_count"] += int(
            ashare_row.get("missing_mark_count") or 0
        )
        per_market["ashare"]["ledgers"].append(ashare_row)

    style_markets = tuple(market for market in target_markets if market != "ashare")
    for market, style, style_dir in _discover_style_ledgers(root, style_markets):
        account_scope = _style_account_scope(market, style)
        totals["ledger_count"] += 1
        state = _read_json(style_dir / "positions.json", {})
        positions = _active_positions(state)
        if not positions:
            totals["skipped_count"] += 1
            per_market[market]["ledger_count"] += 1
            per_market[market]["skipped_count"] += 1
            skipped_row = {
                "market": market,
                "style": style,
                "account_scope": account_scope,
                "account_scope_source": "style_ledger_path",
                "ledger_path": str(style_dir),
                "status": "skipped_no_open_positions",
                "currency": _market_currency(market),
            }
            per_market[market]["ledgers"].append(skipped_row)
            continue

        prices = load_mark_prices_for_positions(
            positions, market, trade_date=snapshot_date
        )
        ledger = SimLedger(style_dir)
        if dry_run:
            payload = ledger.total_pnl(prices=prices)
            payload.update(
                {
                    "date": snapshot_date,
                    "source": "dry_run",
                    "pnl_source": "sim_ledger_mark_to_market"
                    if int(payload.get("missing_mark_count") or 0) == 0
                    else "sim_ledger_cost_fallback",
                    "capital_layer": "simulated",
                    "account_scope": account_scope,
                    "account_scope_source": "style_ledger_path",
                    "real_execution": False,
                    "benchmark_return": benchmark_return,
                    "benchmark_return_pct": (
                        round(float(benchmark_return) * 100.0, 6)
                        if benchmark_return is not None
                        else None
                    ),
                    "benchmark_pnl": None,
                    "pnl_vs_benchmark": None,
                    "benchmark_status": (
                        "available" if benchmark_return is not None else "unavailable"
                    ),
                }
            )
            _with_native_currency_fields(payload, market)
        else:
            currency_fields = _with_native_currency_fields({}, market)
            currency_fields.update(
                {
                    "account_scope": account_scope,
                    "account_scope_source": "style_ledger_path",
                }
            )
            currency_fields["benchmark_status"] = (
                "available" if benchmark_return is not None else "unavailable"
            )
            if benchmark_return is None:
                currency_fields.update(
                    {
                        "benchmark_return": None,
                        "benchmark_return_pct": None,
                        "benchmark_pnl": None,
                        "pnl_vs_benchmark": None,
                    }
                )
            payload = ledger.daily_mark_to_market(
                prices,
                date=snapshot_date,
                benchmark_return=benchmark_return,
                target_return_pct=target_return_pct,
                extra_fields=currency_fields,
            )
            _with_native_currency_fields(payload, market)
            totals["written_count"] += 1
            per_market[market]["written_count"] += 1

        missing = int(payload.get("missing_mark_count") or 0)
        open_count = int(payload.get("open_position_count") or len(positions))
        equity = _safe_float(payload.get("equity") or payload.get("total_equity"))
        pnl = _safe_float(payload.get("total_pnl") or payload.get("pnl"))
        totals["open_position_count"] += open_count
        totals["missing_mark_count"] += missing
        per_market[market]["ledger_count"] += 1
        per_market[market]["open_position_count"] += open_count
        per_market[market]["missing_mark_count"] += missing
        ledger_row = {
            "market": market,
            "style": style,
            "account_scope": account_scope,
            "account_scope_source": "style_ledger_path",
            "ledger_path": str(style_dir),
            "snapshot_path": str(style_dir / "daily_mark_to_market.jsonl"),
            "status": "dry_run" if dry_run else "written",
            "currency": _market_currency(market),
            "equity": round(equity, 8),
            "total_pnl": round(pnl, 8),
            "open_position_count": open_count,
            "missing_mark_count": missing,
            "pnl_source": payload.get("pnl_source") or "sim_ledger_mark_to_market",
        }
        per_market[market]["ledgers"].append(ledger_row)

    return {
        "date": snapshot_date,
        "ledger_root": str(root),
        "markets": list(target_markets),
        "dry_run": dry_run,
        "totals": totals,
        "all_markets_monetary_aggregation": "forbidden",
        "per_market": per_market,
    }


def _parse_markets(raw: str) -> list[str]:
    return [canonical_runtime_market(item) for item in raw.split(",") if item.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Write simulated equity snapshots for the dashboard."
    )
    parser.add_argument(
        "--markets",
        default=",".join(DEFAULT_MARKETS),
        help="Comma-separated market list.",
    )
    parser.add_argument(
        "--ledger-root", default=str(DEFAULT_SIM_LEDGER_ROOT), help="Sim ledger root."
    )
    parser.add_argument(
        "--local-sim-dir",
        default=str(DEFAULT_LOCAL_SIM_DIR),
        help="A-share server-local sim ledger directory.",
    )
    parser.add_argument(
        "--date", default="", help="Snapshot date in YYYYMMDD. Defaults to today."
    )
    parser.add_argument(
        "--benchmark-return",
        type=float,
        default=None,
        help="Verified benchmark return as a decimal; omitted means unavailable, not zero.",
    )
    parser.add_argument(
        "--target-return-pct", type=float, default=0.0, help="Target return in percent."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Inspect without writing snapshot rows."
    )
    parser.add_argument(
        "--pretty", action="store_true", help="Pretty-print JSON output."
    )
    args = parser.parse_args()

    result = write_sim_ledger_equity_snapshots(
        markets=_parse_markets(args.markets),
        ledger_root=args.ledger_root,
        local_sim_dir=args.local_sim_dir,
        trade_date=args.date or None,
        benchmark_return=args.benchmark_return,
        target_return_pct=args.target_return_pct,
        dry_run=args.dry_run,
    )
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["write_sim_ledger_equity_snapshots"]
