#!/usr/bin/env python3
"""Shadow broker: record orders without executing. Multi-strategy parallel."""

from __future__ import annotations

import fcntl
import json
import re
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

from shared.data.reader import SharedSignalsReader

SHADOW_DIR = Path(__file__).resolve().parent.parent / "logs" / "shadow"
SHADOW_TRADES = SHADOW_DIR / "shadow_trades.jsonl"
SHADOW_POSITIONS = SHADOW_DIR / "shadow_positions.json"
SHADOW_PNL = SHADOW_DIR / "shadow_pnl.json"
SHADOW_LOCK = SHADOW_DIR / ".shadow.lock"


@dataclass
class ShadowTrade:
    trade_id: str = field(default_factory=lambda: f"SHADOW-{uuid.uuid4().hex[:12]}")
    strategy_name: str = ""
    market: str = "unknown"
    trade_date: str = field(default_factory=lambda: date.today().isoformat())
    ts_code: str = ""
    side: str = ""
    quantity: float = 0.0
    price: float = 0.0
    amount: float = 0.0
    commission: float = 0.0
    net_amount: float = 0.0
    capital_layer: str = "shadow"
    status: str = "recorded"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    note: str = ""


@contextmanager
def _shadow_lock() -> Iterator[None]:
    _ensure_dirs()
    with open(SHADOW_LOCK, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _ensure_dirs() -> None:
    SHADOW_DIR.mkdir(parents=True, exist_ok=True)


def _normalize_capital_layer(value: str | None) -> str:
    layer = str(value or "shadow").strip().lower()
    if layer == "sim":
        layer = "simulated"
    if layer in {"shadow", "simulated", "real"}:
        return layer
    raise ValueError(f"capital_layer must be one of real/simulated/shadow, got {value}")


def _validate_shadow_capital_layer(value: str | None) -> str:
    layer = _normalize_capital_layer(value)
    if layer == "real":
        raise ValueError("shadow_broker cannot record real capital_layer trades")
    return layer


def _normalize_market(value: Any) -> str:
    market = str(value or "").strip().lower()
    return market or "unknown"


def _infer_market(value: Any, strategy_name: Any = "", note: Any = "") -> str:
    market = _normalize_market(value)
    if market != "unknown":
        return market
    hint = f"{strategy_name} {note}".lower()
    if "ashare" in hint or "a-share" in hint or "a股" in hint:
        return "ashare"
    if "crypto" in hint:
        return "crypto"
    if "pm" in hint or "polymarket" in hint:
        return "pm"
    if "us" in hint or "美股" in hint:
        return "us"
    return market


def _is_regular_ashare_symbol(symbol: Any) -> bool:
    raw = str(symbol or "").strip().upper()
    if "." in raw:
        digits, exchange = raw.split(".", 1)
    else:
        digits, exchange = raw, ""
    if not re.fullmatch(r"\d{6}", digits):
        return False
    if exchange == "SZ":
        return digits.startswith(("000", "001", "002", "003", "300", "301"))
    if exchange == "SH":
        return digits.startswith(("600", "601", "603", "605", "688", "689"))
    return digits.startswith(("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688", "689"))


def _normalize_quantity(value: Any) -> int | float:
    try:
        qty = float(value)
    except (TypeError, ValueError):
        return 0
    if qty != qty:
        return 0
    if abs(qty - round(qty)) < 1e-12:
        return int(round(qty))
    return round(qty, 12)


def _parse_date(value: str | None) -> date:
    if not value:
        return date.today()
    cleaned = str(value).replace("/", "-")
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid trade_date: {value}")


def _load_trades_unlocked(
    strategy_name: str | None = None,
    market: str | None = None,
) -> list[dict[str, Any]]:
    if not SHADOW_TRADES.exists():
        return []
    market_filter = _infer_market(market) if market is not None else None
    trades = []
    with open(SHADOW_TRADES, "r", encoding="utf-8") as fh:
        for line in fh:
            try:
                trade = json.loads(line)
            except json.JSONDecodeError:
                continue
            trade["capital_layer"] = _normalize_capital_layer(trade.get("capital_layer", "shadow"))
            trade["market"] = _normalize_market(trade.get("market"))
            if strategy_name is not None and trade.get("strategy_name") != strategy_name:
                continue
            if market_filter is not None and trade.get("market") != market_filter:
                continue
            trades.append(trade)
    trades.sort(key=lambda item: (item.get("trade_date", ""), item.get("created_at", ""), item.get("trade_id", "")))
    return trades


def _load_trades(
    strategy_name: str | None = None,
    market: str | None = None,
) -> list[dict[str, Any]]:
    with _shadow_lock():
        return _load_trades_unlocked(strategy_name, market)


def _append_trade_unlocked(trade: ShadowTrade) -> None:
    with open(SHADOW_TRADES, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(trade), ensure_ascii=False) + "\n")


def _json_dump_unlocked(path: Path, payload: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def _replay_strategy_state(
    strategy_name: str,
    as_of_date: str | None = None,
    *,
    trades: list[dict[str, Any]] | None = None,
    market: str | None = None,
) -> dict[str, Any]:
    cutoff = _parse_date(as_of_date) if as_of_date else None
    market_filter = _infer_market(market) if market is not None else None
    selected = trades if trades is not None else _load_trades(strategy_name, market=market)
    positions: dict[str, dict[str, Any]] = {}
    daily = {
        "total_trades": 0,
        "buys": 0,
        "sells": 0,
        "total_cost": 0.0,
        "total_proceeds": 0.0,
        "realized_pnl": 0.0,
    }

    for trade in selected:
        trade_market = _infer_market(trade.get("market"), trade.get("strategy_name"), trade.get("note"))
        if market_filter is not None and trade_market != market_filter:
            continue
        trade_day = _parse_date(trade.get("trade_date"))
        if cutoff and trade_day > cutoff:
            continue

        code = str(trade.get("ts_code", ""))
        if not code:
            continue
        if trade_market == "ashare" and not _is_regular_ashare_symbol(code):
            continue
        position = positions.setdefault(code, {"quantity": 0.0, "cost_basis": 0.0, "trades": 0})
        qty = float(trade.get("quantity", 0) or 0)
        net_amount = float(trade.get("net_amount", 0.0) or 0.0)
        side = str(trade.get("side", "")).lower()

        is_target_day = cutoff is None or trade_day == cutoff
        if is_target_day:
            daily["total_trades"] += 1

        if side == "buy":
            position["quantity"] += qty
            position["cost_basis"] = round(position["cost_basis"] + net_amount, 2)
            position["trades"] += 1
            if is_target_day:
                daily["buys"] += 1
                daily["total_cost"] += net_amount
            continue

        if side != "sell":
            continue

        if qty > position["quantity"]:
            continue

        avg_cost = position["cost_basis"] / position["quantity"] if position["quantity"] > 0 else 0.0
        released_cost = round(avg_cost * qty, 2)
        position["quantity"] -= qty
        position["cost_basis"] = round(position["cost_basis"] - released_cost, 2)
        position["trades"] += 1
        if position["quantity"] == 0:
            position["cost_basis"] = 0.0

        realized = round(net_amount - released_cost, 2)
        if is_target_day:
            daily["sells"] += 1
            daily["total_proceeds"] += net_amount
            daily["realized_pnl"] += realized

    clean_positions = {}
    for code, position in positions.items():
        qty = float(position["quantity"])
        if qty <= 0:
            continue
        clean_qty = _normalize_quantity(qty)
        avg_cost = round(position["cost_basis"] / qty, 4) if qty > 0 else 0.0
        clean_positions[code] = {
            "quantity": clean_qty,
            "cost_basis": round(position["cost_basis"], 2),
            "avg_cost": avg_cost,
            "trades": position["trades"],
        }

    daily["total_cost"] = round(daily["total_cost"], 2)
    daily["total_proceeds"] = round(daily["total_proceeds"], 2)
    daily["realized_pnl"] = round(daily["realized_pnl"], 2)
    return {"positions": clean_positions, **daily}


def _persist_snapshots_unlocked() -> None:
    trades = _load_trades_unlocked()
    strategies = sorted({t.get("strategy_name", "") for t in trades if t.get("strategy_name")})
    today = date.today().isoformat()
    positions_payload: dict[str, Any] = {}
    pnl_payload: dict[str, Any] = {}
    for strategy in strategies:
        strategy_trades = [t for t in trades if t.get("strategy_name") == strategy]
        positions_payload[strategy] = _replay_strategy_state(
            strategy, today, trades=strategy_trades
        )["positions"]
        pnl_payload[strategy] = get_shadow_pnl(strategy, today, trades=strategy_trades)
    _json_dump_unlocked(SHADOW_POSITIONS, positions_payload)
    _json_dump_unlocked(SHADOW_PNL, pnl_payload)


def record_shadow(
    order: dict[str, Any],
    strategy_name: str,
    market: str | None = None,
) -> dict[str, Any]:
    ts_code = order.get("ts_code", "")
    side = str(order.get("side", "")).lower()
    quantity = _normalize_quantity(order.get("quantity", 0))
    price = float(order.get("price", 0.0))
    commission = float(order.get("commission", 0.0))
    trade_date = _parse_date(order.get("trade_date")).isoformat()
    capital_layer = _validate_shadow_capital_layer(order.get("capital_layer", "shadow"))
    market_value = _infer_market(market if market is not None else order.get("market"), strategy_name, order.get("note", ""))

    if not ts_code:
        return {"trade_id": "", "status": "rejected", "recorded": False, "message": "Missing ts_code"}
    if market_value == "ashare" and not _is_regular_ashare_symbol(ts_code):
        return {
            "trade_id": "",
            "status": "rejected",
            "recorded": False,
            "message": f"Invalid A-share symbol for shadow broker: {ts_code}",
            "capital_layer": capital_layer,
            "market": market_value,
        }
    if side not in ("buy", "sell"):
        return {"trade_id": "", "status": "rejected", "recorded": False, "message": f"Invalid side: {side}"}
    if quantity <= 0:
        return {"trade_id": "", "status": "rejected", "recorded": False, "message": f"Invalid quantity: {quantity}"}
    if price <= 0:
        return {"trade_id": "", "status": "rejected", "recorded": False, "message": f"Invalid price: {price}"}

    amount = round(quantity * price, 2)
    if commission == 0.0:
        commission = max(amount * 0.00025, 5.0)
    stamp_duty = amount * 0.0005 if side == "sell" else 0.0
    total_cost = round(commission + stamp_duty, 2)
    net_amount = round(amount + total_cost, 2) if side == "buy" else round(amount - total_cost, 2)

    with _shadow_lock():
        trades = _load_trades_unlocked(strategy_name)
        state_before = _replay_strategy_state(strategy_name, trade_date, trades=trades)
        existing_position = state_before["positions"].get(ts_code, {"quantity": 0, "cost_basis": 0.0})
        if side == "sell" and quantity > int(existing_position.get("quantity", 0)):
            return {
                "trade_id": "",
                "status": "rejected",
                "recorded": False,
                "message": (
                    f"Sell quantity {quantity} exceeds existing shadow position "
                    f"{existing_position.get('quantity', 0)} for {ts_code}"
                ),
            }

        trade = ShadowTrade(
            strategy_name=strategy_name,
            market=market_value,
            trade_date=trade_date,
            ts_code=ts_code,
            side=side,
            quantity=quantity,
            price=price,
            amount=amount,
            commission=round(total_cost, 2),
            net_amount=net_amount,
            capital_layer=capital_layer,
            note=order.get("note", ""),
        )
        _append_trade_unlocked(trade)
        _persist_snapshots_unlocked()

    return {
        "trade_id": trade.trade_id,
        "status": "recorded",
        "recorded": True,
        "message": f"Shadow trade recorded for strategy {strategy_name}: {side} {quantity} {ts_code} @ {price}",
        "net_amount": trade.net_amount,
        "capital_layer": capital_layer,
        "market": market_value,
    }


def _latest_prices_from_trades(trades: list[dict[str, Any]], market: str | None = None) -> dict[str, float]:
    market_filter = _infer_market(market) if market is not None else None
    prices: dict[str, float] = {}
    for trade in trades:
        if market_filter is not None and _normalize_market(trade.get("market")) != market_filter:
            continue
        code = str(trade.get("ts_code") or "")
        if not code:
            continue
        try:
            price = float(trade.get("price") or 0.0)
        except (TypeError, ValueError):
            continue
        if price > 0:
            prices[code] = price
    return prices


def _strategy_market_hint(trades: list[dict[str, Any]]) -> str | None:
    markets = sorted({
        _infer_market(trade.get("market"), trade.get("strategy_name"), trade.get("note"))
        for trade in trades
        if _infer_market(trade.get("market"), trade.get("strategy_name"), trade.get("note")) != "unknown"
    })
    return markets[0] if len(markets) == 1 else None


def _latest_ashare_prices_from_reader(codes: list[str], target_date: str) -> tuple[dict[str, float], str]:
    if not codes:
        return {}, ""
    reader = SharedSignalsReader()
    prices: dict[str, float] = {}
    try:
        for code in codes:
            for market in ("Ashare", "ashare"):
                rows = reader.get_bars_daily(market, code, None, target_date)
                if not rows:
                    continue
                for row in reversed(rows):
                    try:
                        close = float(row.get("close") or 0.0)
                    except (TypeError, ValueError):
                        close = 0.0
                    if close > 0:
                        prices[code] = close
                        break
                if code in prices:
                    break
    except Exception:
        return {}, ""
    finally:
        try:
            reader.close()
        except Exception:
            pass
    return prices, "sharedsignals_market_bars_daily_close" if prices else ""


def _enrich_positions_with_unrealized(
    positions: dict[str, dict[str, Any]],
    prices: dict[str, float],
) -> tuple[dict[str, dict[str, Any]], dict[str, float]]:
    enriched: dict[str, dict[str, Any]] = {}
    totals = {"market_value": 0.0, "unrealized_pnl": 0.0}
    for code, position in positions.items():
        row = dict(position)
        qty = float(row.get("quantity") or 0.0)
        cost_basis = float(row.get("cost_basis") or 0.0)
        last_price = float(prices.get(code) or row.get("avg_cost") or 0.0)
        market_value = round(qty * last_price, 2)
        unrealized = round(market_value - cost_basis, 2)
        row.update({
            "last_price": round(last_price, 6),
            "market_value": market_value,
            "unrealized_pnl": unrealized,
        })
        enriched[code] = row
        totals["market_value"] += market_value
        totals["unrealized_pnl"] += unrealized
    totals["market_value"] = round(totals["market_value"], 2)
    totals["unrealized_pnl"] = round(totals["unrealized_pnl"], 2)
    return enriched, totals


def get_shadow_pnl(
    strategy_name: str,
    date: str | None = None,
    *,
    trades: list[dict[str, Any]] | None = None,
    market: str | None = None,
) -> dict[str, Any]:
    target_date = _parse_date(date).isoformat() if date else datetime.now().strftime("%Y-%m-%d")
    market_value = _normalize_market(market) if market is not None else None
    selected_trades = trades if trades is not None else _load_trades(strategy_name, market=market_value)
    effective_market = market_value or _strategy_market_hint(selected_trades)
    state = _replay_strategy_state(strategy_name, target_date, trades=selected_trades, market=effective_market)
    prices = _latest_prices_from_trades(selected_trades, market=effective_market)
    valuation_source = "latest_shadow_trade_price"
    if effective_market == "ashare":
        reader_prices, reader_source = _latest_ashare_prices_from_reader(list(state["positions"].keys()), target_date)
        if reader_prices:
            prices = {**prices, **reader_prices}
            valuation_source = reader_source
            if set(reader_prices) != set(state["positions"]):
                valuation_source = f"{reader_source}+latest_shadow_trade_price_fallback"
    positions, floating = _enrich_positions_with_unrealized(state["positions"], prices)
    realized = float(state["realized_pnl"] or 0.0)
    unrealized = float(floating["unrealized_pnl"] or 0.0)
    return {
        "strategy": strategy_name,
        "market": effective_market or "all",
        "date": target_date,
        "total_trades": state["total_trades"],
        "buys": state["buys"],
        "sells": state["sells"],
        "total_cost": state["total_cost"],
        "total_proceeds": state["total_proceeds"],
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "market_value": floating["market_value"],
        "total_pnl": round(realized + unrealized, 2),
        "valuation_source": valuation_source,
        "positions": positions,
    }


def list_strategies() -> list[str]:
    trades = _load_trades()
    return sorted(set(t.get("strategy_name", "") for t in trades if t.get("strategy_name")))


def get_all_shadow_pnl(
    date: str | None = None,
    market: str | None = None,
) -> dict[str, dict[str, Any]]:
    strategies = list_strategies()
    return {strategy: get_shadow_pnl(strategy, date, market=market) for strategy in strategies}
