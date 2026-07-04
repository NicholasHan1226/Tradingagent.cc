#!/usr/bin/env python3
"""Automated multi-style simulation runner for China futures."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.execution.signal_state_machine import SignalStateConflict, SignalStateMachine
from shared.execution.sim_broker import execute_sim_order

from . import MARKET
from .adapter import CNFuturesAdapter, READER_MARKET
from .contract_rules import get_contract_rule
from .margin_model import estimate_order_cost
from .review import append_review
from .signal_engine import generate_style_signal
from . import sim_executor as _sim_executor  # noqa: F401  # Ensure registry side effect.


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if result == result else default
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _read_daily_bars(reader: Any, symbol: str, date: str) -> list[dict[str, Any]]:
    get_bars = getattr(reader, "get_bars_daily", None)
    if not callable(get_bars):
        return []
    try:
        rows = get_bars(READER_MARKET, symbol, None, date)
    except TypeError:
        rows = get_bars(market=READER_MARKET, symbol=symbol, end=date)
    except Exception:
        return []
    return [dict(row) for row in rows or [] if isinstance(row, dict)]


def _quantity_for_style(
    *,
    symbol: str,
    price: float,
    capital: float,
    style: dict[str, Any],
) -> int:
    cost = estimate_order_cost(symbol=symbol, side="buy", quantity=1, price=price)
    margin_per_lot = max(cost.margin_required, 1.0)
    max_margin_usage = min(max(_safe_float(style.get("max_margin_usage"), 0.20), 0.01), 0.80)
    risk_per_trade = min(max(_safe_float(style.get("risk_per_trade"), 0.02), 0.001), max_margin_usage)
    margin_budget = capital * min(max_margin_usage, risk_per_trade)
    return max(1, int(margin_budget // margin_per_lot))


def _signal_card(
    *,
    date: str,
    style_name: str,
    symbol: str,
    order: dict[str, Any],
    receipt: dict[str, Any],
    signal: dict[str, Any],
) -> dict[str, Any]:
    order_id = str(order["order_id"])
    raw = receipt.get("raw_response") if isinstance(receipt.get("raw_response"), dict) else {}
    return {
        "order_id": order_id,
        "ts_code": symbol,
        "symbol": symbol,
        "market": MARKET,
        "reader_market": READER_MARKET,
        "direction": order["side"],
        "side": order["side"],
        "quantity": _safe_int(order.get("quantity"), 0),
        "price": _safe_float(order.get("price"), 0.0),
        "trigger_price": _safe_float(order.get("price"), 0.0),
        "status": "pending",
        "capital_layer": "simulated",
        "account_type": "simulated",
        "account": f"cn_futures_sim_{style_name}",
        "strategy_name": style_name,
        "manual_confirm_required": False,
        "direct_execution": True,
        "real_trading_enabled": False,
        "valid_until": date,
        "timestamp": _now_iso(),
        "idempotency_key": order_id,
        "source": "cn_futures_multi_style_simulation",
        "signal": signal,
        "margin_required": raw.get("margin_required"),
        "fee": receipt.get("fee"),
    }


def _write_filled_signal(signals_dir: Path, card: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    machine = SignalStateMachine(signals_dir)
    try:
        machine.write_pending(card)
    except SignalStateConflict:
        return {"status": "duplicate", "order_id": card.get("order_id", "")}
    machine.claim(str(card["order_id"]), worker_id="cn_futures_sim")
    machine.mark_running(str(card["order_id"]), worker_id="cn_futures_sim")
    fill_info = {
        "filled_price": receipt.get("avg_price", card.get("price", 0.0)),
        "filled_quantity": receipt.get("filled_qty", card.get("quantity", 0)),
        "filled_qty": receipt.get("filled_qty", card.get("quantity", 0)),
        "fee": receipt.get("fee", 0.0),
        "fill_time": _now_iso(),
        "raw_response": receipt.get("raw_response", {}),
    }
    filled = machine.fill(str(card["order_id"]), fill_info)
    return {"status": "filled", "order_id": card.get("order_id", ""), "filled_signal": filled}


def run_multi_style_simulation(
    adapter: CNFuturesAdapter,
    date: str,
    reader: Any,
    *,
    signals_dir: Path,
    review_path: Path | None = None,
) -> dict[str, Any]:
    """Run all configured futures styles through simulated execution."""

    config = adapter.get_strategy_config()
    styles = config.get("styles") if isinstance(config.get("styles"), dict) else {}
    account = adapter.get_sim_account()
    capital = _safe_float(account.get("sim_capital") if isinstance(account, dict) else None, 100_000.0)
    errors: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    universe = adapter.get_universe(date)
    if not universe:
        errors.append({"stage": "universe", "market": MARKET, "error": "empty_futures_universe"})
    if not styles:
        errors.append({"stage": "strategy", "market": MARKET, "error": "empty_strategy_styles"})

    for style_name, style_config in styles.items():
        style = dict(style_config or {})
        style.setdefault("name", style_name)
        for symbol in universe:
            bars = _read_daily_bars(reader, symbol, date)
            if not bars:
                errors.append({"stage": "data", "symbol": symbol, "style": style_name, "error": "missing_daily_bars"})
                continue
            signal = generate_style_signal(symbol, bars, style)
            if signal.get("action") == "hold":
                continue
            price = _safe_float(signal.get("price"), 0.0)
            if price <= 0:
                errors.append({"stage": "signal", "symbol": symbol, "style": style_name, "error": "invalid_price"})
                continue
            try:
                rule = get_contract_rule(symbol)
                quantity = _quantity_for_style(symbol=symbol, price=price, capital=capital, style=style)
            except Exception as exc:
                errors.append({"stage": "risk", "symbol": symbol, "style": style_name, "error": str(exc)})
                continue
            order_id = f"SIM-CNF-{style_name}-{symbol}-{date}".replace("/", "-")
            order = {
                "order_id": order_id,
                "symbol": symbol,
                "ts_code": symbol,
                "side": signal.get("side", signal.get("action", "buy")),
                "quantity": quantity,
                "price": price,
                "strategy_name": style_name,
                "market": MARKET,
                "capital_layer": "simulated",
                "account_type": "simulated",
                "contract_multiplier": rule.contract_multiplier,
            }
            receipt_obj = execute_sim_order(
                order=order,
                market=MARKET,
                account={"account": f"cn_futures_sim_{style_name}", "capital_layer": "simulated", "account_type": "simulated"},
                config={"fee_mode": "round_trip_estimate", "style": style_name},
            )
            receipt = {
                "status": receipt_obj.status,
                "filled_qty": receipt_obj.filled_qty,
                "avg_price": receipt_obj.avg_price,
                "fee": receipt_obj.fee,
                "message": receipt_obj.message,
                "capital_layer": receipt_obj.capital_layer,
                "account_type": receipt_obj.account_type,
                "order_id": receipt_obj.order_id,
                "market": receipt_obj.market,
                "raw_response": receipt_obj.raw_response,
            }
            card = _signal_card(date=date, style_name=style_name, symbol=symbol, order=order, receipt=receipt, signal=signal)
            signal_result = _write_filled_signal(signals_dir, card, receipt)
            records.append(
                {
                    "date": date,
                    "market": MARKET,
                    "style": style_name,
                    "symbol": symbol,
                    "signal": signal,
                    "order": order,
                    "receipt": receipt,
                    "signal_card": card,
                    "signal_result": signal_result,
                    "capital_layer": "simulated",
                    "account_type": "simulated",
                }
            )

    review = append_review(date=date, market=MARKET, records=records, errors=errors, path=review_path)
    return {
        "market": MARKET,
        "reader_market": READER_MARKET,
        "date": date,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "state": "degraded" if errors else "ok",
        "universe_count": len(universe),
        "style_count": len(styles),
        "record_count": len(records),
        "filled_count": sum(1 for record in records if record["receipt"].get("status") == "filled"),
        "records": records,
        "errors": errors,
        "review": review,
        "real_trading_enabled": False,
        "generated_at": _now_iso(),
    }


__all__ = ["run_multi_style_simulation"]
