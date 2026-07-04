#!/usr/bin/env python3
"""Server-local simulated ledger for A-share backup fills."""

from __future__ import annotations

import fcntl
import errno
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

LOCAL_SIM_DIR = Path(__file__).resolve().parent.parent / "logs" / "local_sim"
LOCAL_SIM_TRADES = LOCAL_SIM_DIR / "local_sim_trades.jsonl"
LOCAL_SIM_POSITIONS = LOCAL_SIM_DIR / "local_sim_positions.json"
LOCAL_SIM_PNL = LOCAL_SIM_DIR / "local_sim_pnl.json"
LOCAL_SIM_LOCK = LOCAL_SIM_DIR / ".local_sim.lock"
DEFAULT_ACCOUNT = "ashare_server_sim"
LOCK_RETRY_ATTEMPTS = 3
LOCK_RETRY_DELAY_SECONDS = 0.1


def _bj_today() -> date:
    """Return today's date in Beijing time (UTC+8)."""
    from datetime import timedelta as _td
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo("Asia/Shanghai")
    except Exception:
        tz = timezone(_td(hours=8))
    return datetime.now(tz).date()


@dataclass
class LocalSimTrade:
    trade_id: str = field(default_factory=lambda: f"LSIM-{uuid.uuid4().hex[:12]}")
    order_id: str = ""
    idempotency_key: str = ""
    market: str = "ashare"
    account: str = DEFAULT_ACCOUNT
    trade_date: str = field(default_factory=lambda: _bj_today().isoformat())
    ts_code: str = ""
    side: str = ""
    quantity: int = 0
    requested_price: float = 0.0
    filled_price: float = 0.0
    slippage_bps: float = 0.0
    amount: float = 0.0
    commission: float = 0.0
    stamp_duty: float = 0.0
    net_amount: float = 0.0
    status: str = "filled"
    source: str = "server_local_sim_backup"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    linked_execution_status: str = ""
    note: str = ""


@contextmanager
def _lock() -> Iterator[None]:
    LOCAL_SIM_DIR.mkdir(parents=True, exist_ok=True)
    with LOCAL_SIM_LOCK.open("a+", encoding="utf-8") as handle:
        _acquire_exclusive_lock(handle.fileno(), LOCAL_SIM_LOCK)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _acquire_exclusive_lock(fd: int, lock_path: Path) -> None:
    last_error: OSError | None = None
    retry_errnos = {errno.EACCES, errno.EAGAIN, getattr(errno, "EWOULDBLOCK", errno.EAGAIN)}
    for attempt in range(1, LOCK_RETRY_ATTEMPTS + 1):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in retry_errnos:
                raise
            last_error = exc
            if attempt < LOCK_RETRY_ATTEMPTS:
                time.sleep(LOCK_RETRY_DELAY_SECONDS * attempt)
    raise TimeoutError(f"Could not acquire local sim lock {lock_path} after {LOCK_RETRY_ATTEMPTS} attempts") from last_error


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


def _account_name(account: Any) -> str:
    if isinstance(account, dict):
        for key in ("account", "account_id", "account_name", "name"):
            value = str(account.get(key) or "").strip()
            if value:
                return value
    value = str(account or "").strip()
    return value or DEFAULT_ACCOUNT


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


def _trade_date(value: Any) -> str:
    raw = str(value or "").strip()
    if len(raw) >= 8 and raw[:8].isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    if len(raw) >= 10:
        return raw[:10].replace("/", "-")
    return date.today().isoformat()


def _load_trades_unlocked() -> list[dict[str, Any]]:
    if not LOCAL_SIM_TRADES.exists():
        return []
    rows: list[dict[str, Any]] = []
    with LOCAL_SIM_TRADES.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _append_trade_unlocked(trade: LocalSimTrade) -> None:
    LOCAL_SIM_TRADES.parent.mkdir(parents=True, exist_ok=True)
    with LOCAL_SIM_TRADES.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(trade), ensure_ascii=False) + "\n")


def _replay_account(trades: list[dict[str, Any]], account: str | None = None) -> dict[str, Any]:
    positions: dict[str, dict[str, Any]] = {}
    realized_pnl = 0.0
    total_trades = 0
    buys = 0
    sells = 0
    for trade in trades:
        if account is not None and str(trade.get("account") or "") != account:
            continue
        if str(trade.get("status") or "") != "filled":
            continue
        code = str(trade.get("ts_code") or "").upper()
        if not code:
            continue
        qty = _safe_float(trade.get("quantity"), 0.0)
        net_amount = _safe_float(trade.get("net_amount"), 0.0)
        filled_price = _safe_float(trade.get("filled_price"), 0.0)
        side = str(trade.get("side") or "").lower()
        pos = positions.setdefault(code, {"quantity": 0.0, "cost_basis": 0.0, "last_price": 0.0, "trades": 0})
        total_trades += 1
        if side == "buy":
            pos["quantity"] += qty
            pos["cost_basis"] += net_amount
            pos["last_price"] = filled_price or pos["last_price"]
            pos["trades"] += 1
            buys += 1
            continue
        if side != "sell" or qty <= 0 or pos["quantity"] <= 0:
            continue
        sell_qty = min(qty, pos["quantity"])
        avg_cost = pos["cost_basis"] / pos["quantity"] if pos["quantity"] else 0.0
        released_cost = round(avg_cost * sell_qty, 2)
        pos["quantity"] -= sell_qty
        pos["cost_basis"] = round(pos["cost_basis"] - released_cost, 2)
        pos["last_price"] = filled_price or pos["last_price"]
        pos["trades"] += 1
        realized_pnl += net_amount - released_cost
        sells += 1
        if pos["quantity"] <= 0:
            pos["quantity"] = 0.0
            pos["cost_basis"] = 0.0
    clean_positions: dict[str, dict[str, Any]] = {}
    market_value = 0.0
    unrealized = 0.0
    for code, pos in positions.items():
        qty = float(pos.get("quantity") or 0.0)
        if qty <= 0:
            continue
        cost = round(float(pos.get("cost_basis") or 0.0), 2)
        last_price = round(float(pos.get("last_price") or 0.0), 6)
        value = round(qty * last_price, 2)
        row_unrealized = round(value - cost, 2)
        clean_positions[code] = {
            "quantity": int(qty) if abs(qty - round(qty)) < 1e-12 else round(qty, 6),
            "cost_basis": cost,
            "avg_cost": round(cost / qty, 4) if qty else 0.0,
            "last_price": last_price,
            "market_value": value,
            "unrealized_pnl": row_unrealized,
            "trades": int(pos.get("trades") or 0),
        }
        market_value += value
        unrealized += row_unrealized
    return {
        "account": account or "all",
        "total_trades": total_trades,
        "buys": buys,
        "sells": sells,
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized, 2),
        "market_value": round(market_value, 2),
        "total_pnl": round(realized_pnl + unrealized, 2),
        "positions": clean_positions,
    }


def _persist_unlocked(trades: list[dict[str, Any]]) -> None:
    accounts = sorted({str(t.get("account") or DEFAULT_ACCOUNT) for t in trades if t.get("account")})
    positions = {account: _replay_account(trades, account)["positions"] for account in accounts}
    pnl = {account: _replay_account(trades, account) for account in accounts}
    LOCAL_SIM_POSITIONS.write_text(json.dumps(positions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    LOCAL_SIM_PNL.write_text(json.dumps(pnl, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def record_local_sim_order(
    order: dict[str, Any],
    market: str,
    account: dict[str, Any] | str | None = None,
    config: dict[str, Any] | None = None,
    receipt: Any | None = None,
) -> dict[str, Any]:
    market_key = str(market or "").lower().strip()
    if market_key != "ashare":
        return {"status": "skipped", "recorded": False, "reason": f"unsupported market={market_key}"}
    config = dict(config or {})
    code = str(order.get("ts_code") or order.get("symbol") or "").strip().upper()
    if not _is_regular_ashare_symbol(code):
        return {"status": "rejected", "recorded": False, "reason": f"unsupported or non-A-share code: {code}"}
    side = str(order.get("side") or order.get("direction") or "buy").lower().strip()
    if side not in {"buy", "sell"}:
        return {"status": "rejected", "recorded": False, "reason": f"invalid side: {side}"}
    quantity = _safe_int(order.get("quantity") or order.get("qty") or order.get("filled_qty"), 0)
    requested_price = _safe_float(order.get("price") or order.get("limit_price") or order.get("mid_price"), 0.0)
    if quantity <= 0 or requested_price <= 0:
        return {"status": "rejected", "recorded": False, "reason": "non-positive quantity or price"}
    order_id = str(order.get("order_id") or f"LSIM-ASHARE-{datetime.now().strftime('%Y%m%d%H%M%S')}")
    idempotency_key = str(order.get("idempotency_key") or order_id)
    account_name = _account_name(account or order.get("account") or DEFAULT_ACCOUNT)
    if 'local_sim_slippage_bps' in config:
        slippage_bps = _safe_float(config.get('local_sim_slippage_bps'), 5.0)
    else:
        slippage_bps = _safe_float(os.environ.get('ASHARE_LOCAL_SIM_SLIPPAGE_BPS'), 5.0)
    filled_price = requested_price * (1.0 + slippage_bps / 10000.0) if side == "buy" else requested_price * (1.0 - slippage_bps / 10000.0)
    filled_price = round(filled_price, 4)
    amount = round(quantity * filled_price, 2)
    commission = round(max(amount * 0.00025, 5.0), 2)
    stamp_duty = round(amount * 0.0005, 2) if side == "sell" else 0.0
    net_amount = round(amount + commission + stamp_duty, 2) if side == "buy" else round(amount - commission - stamp_duty, 2)
    linked_status = str(getattr(receipt, "status", "") or (receipt.get("status") if isinstance(receipt, dict) else "") or "")
    trade = LocalSimTrade(
        order_id=order_id,
        idempotency_key=idempotency_key,
        market=market_key,
        account=account_name,
        trade_date=_trade_date(order.get("trade_date") or order.get("valid_until") or order.get("date")),
        ts_code=code,
        side=side,
        quantity=quantity,
        requested_price=round(requested_price, 6),
        filled_price=filled_price,
        slippage_bps=slippage_bps,
        amount=amount,
        commission=commission,
        stamp_duty=stamp_duty,
        net_amount=net_amount,
        linked_execution_status=linked_status,
        note=str(order.get("note") or "server backup fill for A-share simulated signal"),
    )
    with _lock():
        trades = _load_trades_unlocked()
        for existing in trades:
            if str(existing.get("idempotency_key") or "") == idempotency_key:
                return {"status": "duplicate", "recorded": False, "trade_id": existing.get("trade_id", ""), "idempotency_key": idempotency_key, "account": account_name}
        if side == "sell":
            current = _replay_account(trades, account_name)["positions"].get(code, {})
            if quantity > _safe_int(current.get("quantity"), 0):
                return {"status": "rejected", "recorded": False, "reason": f"sell quantity {quantity} exceeds local simulated position {current.get('quantity', 0)} for {code}", "account": account_name}
        _append_trade_unlocked(trade)
        trades.append(asdict(trade))
        _persist_unlocked(trades)
    return {
        "status": "filled",
        "recorded": True,
        "trade_id": trade.trade_id,
        "order_id": order_id,
        "idempotency_key": idempotency_key,
        "account": account_name,
        "filled_qty": quantity,
        "avg_price": filled_price,
        "net_amount": net_amount,
        "ledger": "server_local_sim_backup",
    }


def get_local_sim_pnl(account: str | None = None) -> dict[str, Any]:
    with _lock():
        return _replay_account(_load_trades_unlocked(), account)
