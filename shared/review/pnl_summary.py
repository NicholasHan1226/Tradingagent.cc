#!/usr/bin/env python3
"""Unified simulated PnL summary: realized_pnl + mark-to-market unrealized_pnl.

This module reads the server-local simulated ledger (A-share) and the active
market ``SimLedger`` journals (CNFutures/Crypto).  Results remain keyed by
market; this module never creates an all-market monetary total.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from shared.accounting.sim_ledger import SimLedger
from shared.execution.local_sim_ledger import (
    DEFAULT_ACCOUNT as CURRENT_ASHARE_SIM_ACCOUNT,
    LOCAL_SIM_TRADES as CURRENT_ASHARE_SIM_TRADES,
)
from shared.governance.market_lanes import (
    ACTIVE_RUNTIME_MARKETS,
    canonical_runtime_market,
)
from shared.review.sample_quality import classify_trade_sample, summarize_sample_quality


TRADINGAGENT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SIM_LEDGER_ROOT = TRADINGAGENT_ROOT / "shared" / "logs" / "sim_ledger"
DEFAULT_LOCAL_SIM_TRADES = CURRENT_ASHARE_SIM_TRADES
DEFAULT_MARKETS = ACTIVE_RUNTIME_MARKETS


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else default
    except (TypeError, ValueError):
        return default


def _row_time_key(row: dict[str, Any]) -> str:
    return str(
        row.get("trade_date")
        or row.get("price_time")
        or row.get("latest_price_time")
        or row.get("collected_at")
        or row.get("bar_time")
        or row.get("open_time")
        or ""
    )


def _row_price(row: dict[str, Any]) -> float:
    for key in (
        "close",
        "adjusted_close",
        "price",
        "latest_price",
        "last_price",
        "market_price",
        "yes_price",
        "bestBid",
        "bestAsk",
    ):
        price = _safe_float(row.get(key), 0.0)
        if price > 0:
            return price
    return 0.0


def _latest_priced(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    priced = [row for row in rows if isinstance(row, dict) and _row_price(row) > 0]
    if not priced:
        return None
    return sorted(priced, key=_row_time_key)[-1]


def _lookback_window(end_date: str, days: int = 14) -> tuple[str, str]:
    try:
        end = datetime.strptime(end_date, "%Y%m%d").date()
    except (TypeError, ValueError):
        end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _read_jsonl_dicts(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except OSError:
        return []
    return rows


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _latest_prices_from_journal(path: Path) -> dict[str, float]:
    """Return the latest fill_price per symbol from a trade journal."""
    prices: dict[str, float] = {}
    for row in _read_jsonl_dicts(path):
        symbol = str(row.get("symbol") or row.get("ts_code") or "").strip()
        price = _safe_float(row.get("fill_price") or row.get("price"), 0.0)
        if symbol and price > 0:
            prices[symbol] = price
    return prices


def _replay_journal_to_ledger(
    journal_path: Path, ledger: SimLedger, market: str
) -> None:
    """Replay a trade journal into a fresh SimLedger instance.

    This keeps the summary computation independent of the persisted ``positions.json``
    state and avoids mutating production ledger files.
    """
    for row in _read_jsonl_dicts(journal_path):
        symbol = str(row.get("symbol") or row.get("ts_code") or "").strip()
        side = str(row.get("side") or "").lower()
        qty = _safe_float(
            row.get("fill_qty") or row.get("quantity") or row.get("filled_qty"), 0.0
        )
        price = _safe_float(row.get("fill_price") or row.get("price"), 0.0)
        if not symbol or side not in {"buy", "sell"} or qty <= 0 or price <= 0:
            continue
        order_id = str(
            row.get("order_id")
            or row.get("fill_id")
            or f"REPLAY-{uuid.uuid4().hex[:12]}"
        )
        order = {
            "symbol": symbol,
            "side": side,
            "quantity": qty,
            "limit_price": price,
            "order_type": "market",
            "market": market,
            "order_id": order_id,
            "submitted_at": str(
                row.get("timestamp") or row.get("fill_time") or _now_iso()
            ),
        }
        fill = {
            "fill_id": str(
                row.get("fill_id") or f"REPLAY-FILL-{uuid.uuid4().hex[:12]}"
            ),
            "order_id": order_id,
            "fill_price": price,
            "fill_qty": qty,
            "fill_time": str(
                row.get("timestamp") or row.get("fill_time") or _now_iso()
            ),
            "slippage_bps": _safe_float(row.get("slippage_bps"), 0.0),
            "counterparty": str(row.get("counterparty") or "simulated"),
        }
        fees = dict(row.get("fees")) if isinstance(row.get("fees"), dict) else {}
        ledger.record_fill(order, fill, fees=fees)


def _market_account_envelope(
    market: str,
    account_summaries: dict[str, dict[str, Any]],
    *,
    journal_count: int,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    """Expose money only when exactly one authoritative account is present.

    Position and error counts may be summed for health reporting. Monetary
    fields are never summed across accounts; callers must consume
    ``account_summaries`` by exact ``account_scope``.
    """
    account_count = len(account_summaries)
    envelope: dict[str, Any] = {
        "market": market,
        "capital_layer": "simulated",
        "account_count": account_count,
        "journal_count": journal_count,
        "account_summaries": account_summaries,
        "open_position_count": sum(
            int(record.get("open_position_count") or 0)
            for record in account_summaries.values()
        ),
        "missing_mark_count": sum(
            int(record.get("missing_mark_count") or 0)
            for record in account_summaries.values()
        ),
        "errors": list(errors or []),
        "monetary_aggregation": "forbidden_across_accounts",
    }
    if account_count == 1:
        account_scope, account = next(iter(account_summaries.items()))
        account_monetary_state = str(
            account.get("monetary_state") or "available"
        ).strip()
        envelope.update(account)
        envelope.update(
            {
                "market": market,
                "capital_layer": "simulated",
                "account_scope": account_scope,
                "account_count": 1,
                "journal_count": journal_count,
                "account_summaries": account_summaries,
                "errors": list(errors or []),
                "monetary_state": (
                    "available_single_account"
                    if account_monetary_state == "available"
                    else account_monetary_state
                ),
                "monetary_aggregation": (
                    "single_account_projection"
                    if account_monetary_state == "available"
                    else "unavailable_account_authority"
                ),
            }
        )
        return envelope

    reason = "multiple_accounts" if account_count > 1 else "no_account_authority"
    envelope.update(
        {
            "account_scope": None,
            "realized_pnl": None,
            "unrealized_pnl": None,
            "total_pnl": None,
            "market_value": None,
            "pnl_source": None,
            "mark_authority": None,
            "monetary_state": f"unavailable_{reason}",
        }
    )
    return envelope


def _aggregate_style_ledgers(market: str, ledger_root: Path) -> dict[str, Any]:
    """Summarize each style ledger as an independent simulated account."""
    market_dir = ledger_root / market
    journals = (
        sorted(market_dir.glob("*/trade_journal.jsonl")) if market_dir.exists() else []
    )
    account_summaries: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    for journal in journals:
        style_name = journal.parent.name
        account_scope = f"{market}:simulated:{style_name}"
        try:
            # Replay the journal into a temporary ledger so we never mutate the
            # production positions.json state while computing a read-only summary.
            with tempfile.TemporaryDirectory() as tmp:
                tmp_root = Path(tmp) / style_name
                tmp_root.mkdir(parents=True)
                shutil.copy(journal, tmp_root / "trade_journal.jsonl")
                ledger = SimLedger(tmp_root, starting_cash=0.0)
                _replay_journal_to_ledger(journal, ledger, market)
                prices = _latest_prices_from_journal(journal)
                pnl = ledger.total_pnl(prices=prices if prices else None)
            account_summaries[account_scope] = {
                "market": market,
                "capital_layer": "simulated",
                "account_scope": account_scope,
                "account_scope_source": "style_ledger_path",
                "strategy": style_name,
                "realized_pnl": round(_safe_float(pnl.get("realized_pnl")), 6),
                "unrealized_pnl": round(_safe_float(pnl.get("unrealized_pnl")), 6),
                "total_pnl": round(_safe_float(pnl.get("total_pnl")), 6),
                "market_value": round(_safe_float(pnl.get("market_value")), 6),
                "open_position_count": int(pnl.get("open_position_count") or 0),
                "missing_mark_count": int(pnl.get("missing_mark_count") or 0),
                # This compatibility summary has no independent market-data
                # reader. Open positions use the latest fill price only.
                "pnl_source": "sim_ledger_journal_fill_price_fallback",
                "mark_authority": "journal_fill_price_fallback",
                "source_ledger": str(journal),
                "monetary_state": "available",
            }
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{style_name}: {exc.__class__.__name__}: {exc}")
            account_summaries[account_scope] = {
                "market": market,
                "capital_layer": "simulated",
                "account_scope": account_scope,
                "account_scope_source": "style_ledger_path",
                "strategy": style_name,
                "realized_pnl": None,
                "unrealized_pnl": None,
                "total_pnl": None,
                "market_value": None,
                "open_position_count": 0,
                "missing_mark_count": 0,
                "pnl_source": None,
                "mark_authority": None,
                "source_ledger": str(journal),
                "monetary_state": "unavailable_read_error",
                "error": f"{exc.__class__.__name__}: {exc}",
            }

    envelope = _market_account_envelope(
        market,
        account_summaries,
        journal_count=len(journals),
        errors=errors,
    )
    envelope["style_count"] = len(account_summaries)
    return envelope


def _ashare_local_sim_summary(
    local_trades_path: Path | None,
    mark_prices: dict[str, float] | None,
) -> dict[str, Any]:
    """Return A-share server-local simulated PnL with optional mark-to-market."""
    try:
        from shared.execution import local_sim_ledger

        # If the test harness has patched local_sim_ledger paths, respect those.
        original_local_sim_trades = local_sim_ledger.LOCAL_SIM_TRADES
        if local_trades_path is not None:
            local_sim_ledger.LOCAL_SIM_TRADES = local_trades_path
        try:
            pnl = local_sim_ledger.get_local_sim_pnl(
                account=None, mark_prices=mark_prices
            )
            audit_pnl = local_sim_ledger.get_local_sim_pnl(
                account=None,
                mark_prices=mark_prices,
                include_validation_samples=True,
            )
            strategy_pnl = local_sim_ledger.get_local_sim_pnl(
                account=None,
                mark_prices=mark_prices,
                trade_filter=lambda row: bool(
                    classify_trade_sample(row).get("strategy_sample_valid")
                ),
            )
            sample_quality = summarize_sample_quality(
                _read_jsonl_dicts(local_sim_ledger.LOCAL_SIM_TRADES)
            )
        finally:
            local_sim_ledger.LOCAL_SIM_TRADES = original_local_sim_trades
    except Exception as exc:  # noqa: BLE001
        return {
            "realized_pnl": None,
            "unrealized_pnl": None,
            "total_pnl": None,
            "strategy_realized_pnl": None,
            "strategy_unrealized_pnl": None,
            "strategy_total_pnl": None,
            "audit_realized_pnl": None,
            "audit_unrealized_pnl": None,
            "audit_total_pnl": None,
            "market_value": None,
            "strategy_market_value": None,
            "audit_market_value": None,
            "equity": None,
            "cash": None,
            "open_position_count": 0,
            "strategy_open_position_count": 0,
            "audit_open_position_count": 0,
            "missing_mark_count": 0,
            "sample_quality": {
                "total_count": 0,
                "strategy_sample_valid_count": 0,
                "validation_sample_count": 0,
                "invalid_strategy_sample_count": 0,
                "by_classification": {},
                "by_reason": {},
            },
            "pnl_source": "ashare_local_sim_error",
            "monetary_state": "unavailable_read_error",
            "error": f"{exc.__class__.__name__}: {exc}",
        }

    positions = pnl.get("positions") or {}
    missing_mark_count = 0
    if mark_prices is None:
        # Without external mark prices we still report unrealized, but flag it as
        # trade-price fallback rather than true mark-to-market.
        pnl_source = "ashare_local_sim_trade_price_fallback"
    else:
        pnl_source = "ashare_local_sim_mark_to_market"
        for code in positions:
            if code not in mark_prices:
                missing_mark_count += 1

    return {
        "realized_pnl": round(_safe_float(pnl.get("realized_pnl")), 6),
        "unrealized_pnl": round(_safe_float(pnl.get("unrealized_pnl")), 6),
        "total_pnl": round(_safe_float(pnl.get("total_pnl")), 6),
        "strategy_realized_pnl": round(
            _safe_float(strategy_pnl.get("realized_pnl")), 6
        ),
        "strategy_unrealized_pnl": round(
            _safe_float(strategy_pnl.get("unrealized_pnl")), 6
        ),
        "strategy_total_pnl": round(_safe_float(strategy_pnl.get("total_pnl")), 6),
        "audit_realized_pnl": round(_safe_float(audit_pnl.get("realized_pnl")), 6),
        "audit_unrealized_pnl": round(_safe_float(audit_pnl.get("unrealized_pnl")), 6),
        "audit_total_pnl": round(_safe_float(audit_pnl.get("total_pnl")), 6),
        "market_value": round(_safe_float(pnl.get("market_value")), 6),
        "strategy_market_value": round(
            _safe_float(strategy_pnl.get("market_value")), 6
        ),
        "audit_market_value": round(_safe_float(audit_pnl.get("market_value")), 6),
        "equity": round(_safe_float(pnl.get("equity")), 6) if "equity" in pnl else None,
        "cash": round(_safe_float(pnl.get("cash")), 6) if "cash" in pnl else None,
        "open_position_count": len(positions),
        "strategy_open_position_count": len(strategy_pnl.get("positions") or {}),
        "audit_open_position_count": len(audit_pnl.get("positions") or {}),
        "missing_mark_count": missing_mark_count,
        "sample_quality": sample_quality,
        "pnl_source": pnl_source,
        "monetary_state": "available",
    }


def sim_ledger_pnl_summary(
    markets: list[str] | tuple[str, ...] | set[str] | None = None,
    *,
    ledger_root: Path | str | None = None,
    local_trades_path: Path | str | None = None,
    ashare_mark_prices: dict[str, float] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return realized + unrealized PnL per simulated market.

    Args:
        markets: markets to include. Defaults to all production sim markets.
        ledger_root: root of per-style SimLedger journals.
        local_trades_path: path to A-share server-local trade journal.
        ashare_mark_prices: optional symbol -> close price map for A-share
            mark-to-market. If absent, A-share unrealized uses last trade price.

    Returns:
        {market: {
            realized_pnl, unrealized_pnl, total_pnl, market_value,
            open_position_count, missing_mark_count, pnl_source, ...
        }}
    """
    requested_markets = tuple(markets) if markets is not None else DEFAULT_MARKETS
    target_markets = tuple(
        dict.fromkeys(canonical_runtime_market(market) for market in requested_markets)
    )
    root = Path(ledger_root) if ledger_root is not None else DEFAULT_SIM_LEDGER_ROOT
    local_path = (
        Path(local_trades_path)
        if local_trades_path is not None
        else DEFAULT_LOCAL_SIM_TRADES
    )

    result: dict[str, dict[str, Any]] = {}
    for market_key in target_markets:
        if market_key == "ashare":
            mark_prices = ashare_mark_prices
            if mark_prices is None:
                positions = _ashare_local_positions(local_path)
                mark_prices = (
                    load_mark_prices_for_positions(positions, "ashare")
                    if positions
                    else None
                )
            ashare_summary = _ashare_local_sim_summary(local_path, mark_prices)
            ashare_summary.update(
                {
                    "market": "ashare",
                    "capital_layer": "simulated",
                    "account_scope": CURRENT_ASHARE_SIM_ACCOUNT,
                    "account_scope_source": "documented_single_ashare_sim_account",
                }
            )
            result[market_key] = _market_account_envelope(
                "ashare",
                {CURRENT_ASHARE_SIM_ACCOUNT: ashare_summary},
                journal_count=1 if local_path.exists() else 0,
                errors=(
                    [str(ashare_summary["error"])]
                    if ashare_summary.get("error")
                    else []
                ),
            )
        else:
            result[market_key] = _aggregate_style_ledgers(market_key, root)
    return result


def _ashare_local_positions(local_trades_path: Path) -> dict[str, dict[str, Any]]:
    try:
        from shared.execution import local_sim_ledger

        original_local_sim_trades = local_sim_ledger.LOCAL_SIM_TRADES
        local_sim_ledger.LOCAL_SIM_TRADES = local_trades_path
        try:
            pnl = local_sim_ledger.get_local_sim_pnl(account=None, mark_prices=None)
        finally:
            local_sim_ledger.LOCAL_SIM_TRADES = original_local_sim_trades
    except Exception:  # noqa: BLE001
        return {}
    positions = pnl.get("positions") if isinstance(pnl, dict) else {}
    return positions if isinstance(positions, dict) else {}


def load_mark_prices_for_positions(
    positions: dict[str, dict[str, Any]],
    market: str,
    *,
    trade_date: str = "",
    reader: Any | None = None,
) -> dict[str, float]:
    """Load marks through an explicitly injected market data port.

    The former implementation rebuilt an implicit legacy client and defaulted
    to localhost:8082.  Absence of an injected fixture/TradingDatas V1 adapter
    now returns no marks so the accounting caller can report a missing-mark or
    cost-basis fallback without silently changing data authority.
    """
    market_key = canonical_runtime_market(market)
    prices: dict[str, float] = {}
    if not positions or reader is None:
        return prices
    date = trade_date or __import__(
        "datetime", fromlist=["date"]
    ).date.today().strftime("%Y%m%d")
    start, end = _lookback_window(date)

    if market_key == "crypto":
        get_crypto = getattr(reader, "get_crypto_klines", None)
        if not callable(get_crypto):
            return prices
        for symbol in positions:
            try:
                latest = _latest_priced(get_crypto(symbol=symbol, limit=50) or [])
                if latest:
                    prices[symbol] = _row_price(latest)
            except Exception:  # noqa: BLE001
                continue
        return prices

    get_bars = getattr(reader, "get_bars_daily", None)
    if not callable(get_bars):
        return prices
    reader_market = {
        "ashare": "Ashare",
        "cn_futures": "Futures",
    }[market_key]
    for symbol in positions:
        try:
            latest = _latest_priced(get_bars(reader_market, symbol, start, end) or [])
            if latest:
                prices[symbol] = _row_price(latest)
        except Exception:  # noqa: BLE001
            continue
    return prices


__all__ = ["sim_ledger_pnl_summary", "load_mark_prices_for_positions"]
