#!/usr/bin/env python3
"""Shadow broker: record orders without executing. Multi-strategy parallel."""

from __future__ import annotations

import fcntl
import json
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

SHADOW_DIR = Path(__file__).resolve().parent.parent / "logs" / "shadow"
SHADOW_TRADES = SHADOW_DIR / "shadow_trades.jsonl"
SHADOW_POSITIONS = SHADOW_DIR / "shadow_positions.json"
SHADOW_PNL = SHADOW_DIR / "shadow_pnl.json"
SHADOW_LOCK = SHADOW_DIR / ".shadow.lock"


@dataclass
class ShadowTrade:
    trade_id: str = field(default_factory=lambda: f"SHADOW-{uuid.uuid4().hex[:12]}")
    strategy_name: str = ""
    trade_date: str = field(default_factory=lambda: date.today().isoformat())
    ts_code: str = ""
    side: str = ""
    quantity: int = 0
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
    if layer in {"shadow", "sim", "real"}:
        return layer
    raise ValueError(f"capital_layer must be one of shadow/sim/real, got {value}")


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


def _load_trades_unlocked(strategy_name: str | None = None) -> list[dict[str, Any]]:
    if not SHADOW_TRADES.exists():
        return []
    trades = []
    with open(SHADOW_TRADES, "r", encoding="utf-8") as fh:
        for line in fh:
            try:
                trade = json.loads(line)
            except json.JSONDecodeError:
                continue
            if strategy_name is None or trade.get("strategy_name") == strategy_name:
                trade["capital_layer"] = _normalize_capital_layer(trade.get("capital_layer", "shadow"))
                trades.append(trade)
    trades.sort(key=lambda item: (item.get("trade_date", ""), item.get("created_at", ""), item.get("trade_id", "")))
    return trades


def _load_trades(strategy_name: str | None = None) -> list[dict[str, Any]]:
    with _shadow_lock():
        return _load_trades_unlocked(strategy_name)


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
) -> dict[str, Any]:
    cutoff = _parse_date(as_of_date) if as_of_date else None
    selected = trades if trades is not None else _load_trades(strategy_name)
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
        trade_day = _parse_date(trade.get("trade_date"))
        if cutoff and trade_day > cutoff:
            continue

        code = str(trade.get("ts_code", ""))
        if not code:
            continue
        position = positions.setdefault(code, {"quantity": 0, "cost_basis": 0.0, "trades": 0})
        qty = int(trade.get("quantity", 0) or 0)
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
        qty = int(position["quantity"])
        if qty <= 0:
            continue
        avg_cost = round(position["cost_basis"] / qty, 4) if qty > 0 else 0.0
        clean_positions[code] = {
            "quantity": qty,
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


def record_shadow(order: dict[str, Any], strategy_name: str) -> dict[str, Any]:
    ts_code = order.get("ts_code", "")
    side = str(order.get("side", "")).lower()
    quantity = int(order.get("quantity", 0))
    price = float(order.get("price", 0.0))
    commission = float(order.get("commission", 0.0))
    trade_date = _parse_date(order.get("trade_date")).isoformat()
    capital_layer = _normalize_capital_layer(order.get("capital_layer", "shadow"))

    if not ts_code:
        return {"trade_id": "", "status": "rejected", "recorded": False, "message": "Missing ts_code"}
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
    }


def get_shadow_pnl(
    strategy_name: str,
    date: str | None = None,
    *,
    trades: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    target_date = _parse_date(date).isoformat() if date else datetime.now().strftime("%Y-%m-%d")
    state = _replay_strategy_state(strategy_name, target_date, trades=trades)
    return {
        "strategy": strategy_name,
        "date": target_date,
        "total_trades": state["total_trades"],
        "buys": state["buys"],
        "sells": state["sells"],
        "total_cost": state["total_cost"],
        "total_proceeds": state["total_proceeds"],
        "realized_pnl": state["realized_pnl"],
        "positions": state["positions"],
    }


def list_strategies() -> list[str]:
    trades = _load_trades()
    return sorted(set(t.get("strategy_name", "") for t in trades if t.get("strategy_name")))


def get_all_shadow_pnl(date: str | None = None) -> dict[str, dict[str, Any]]:
    strategies = list_strategies()
    return {strategy: get_shadow_pnl(strategy, date) for strategy in strategies}
