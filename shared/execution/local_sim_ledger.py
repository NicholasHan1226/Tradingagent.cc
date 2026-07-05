#!/usr/bin/env python3
"""Server-local simulated ledger for A-share backup fills."""

from __future__ import annotations

import fcntl
import errno
import hashlib
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
LOCAL_SIM_POSITIONS_SNAPSHOT = Path(__file__).resolve().parents[2] / "signals" / "positions" / "simulated_ashare_positions.json"
LOCAL_SIM_RECEIPTS = Path(__file__).resolve().parents[2] / "signals" / "sim_execution_receipts.jsonl"
DEFAULT_ACCOUNT = "ashare_server_sim"
LOCK_RETRY_ATTEMPTS = 3
LOCK_RETRY_DELAY_SECONDS = 0.1
CHECKSUM_KEYS = {"payload_sha256", "receipt_sha256", "checksum", "sha256"}


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


def _canonical_json(payload: dict[str, Any], *, drop_checksums: bool = False) -> bytes:
    data = {key: value for key, value in payload.items() if not (drop_checksums and key in CHECKSUM_KEYS)}
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _payload_sha256(payload: dict[str, Any], *, drop_checksums: bool = False) -> str:
    return hashlib.sha256(_canonical_json(payload, drop_checksums=drop_checksums)).hexdigest()


def _append_receipt_unlocked(receipt: dict[str, Any]) -> None:
    LOCAL_SIM_RECEIPTS.parent.mkdir(parents=True, exist_ok=True)
    with LOCAL_SIM_RECEIPTS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n")


def _build_signed_receipt(
    *,
    order: dict[str, Any],
    trade: LocalSimTrade | None,
    market: str,
    account: str,
    status: str,
    message: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "receipt_type": "server_local_sim",
        "source": "server_local_sim_backup",
        "market": market,
        "account": account,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "order_id": str(order.get("order_id") or (trade.order_id if trade else "")),
        "idempotency_key": str(order.get("idempotency_key") or (trade.idempotency_key if trade else "")),
        "symbol": str(order.get("ts_code") or order.get("symbol") or (trade.ts_code if trade else "")),
        "side": str(order.get("side") or order.get("direction") or (trade.side if trade else "")),
        "status": status,
        "success": status in {"filled", "partial"},
        "message": message,
        "receipt_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "payload_sha256": _payload_sha256(order),
    }
    if trade is not None:
        payload.update(
            {
                "trade_id": trade.trade_id,
                "trade_date": trade.trade_date,
                "filled_qty": trade.quantity,
                "avg_price": trade.filled_price,
                "commission": trade.commission,
                "stamp_duty": trade.stamp_duty,
                "net_amount": trade.net_amount,
            }
        )
    if extra:
        payload.update(extra)
    payload["receipt_sha256"] = _payload_sha256(payload, drop_checksums=True)
    return payload


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


def _can_sell_on(entry_date: Any, trade_date: Any) -> bool:
    try:
        from Ashare.t_plus_1 import can_sell

        return bool(can_sell(entry_date, trade_date))
    except Exception:
        try:
            return _trade_date(entry_date) < _trade_date(trade_date)
        except Exception:
            return False


def _starting_cash(value: Any) -> float:
    cash = _safe_float(value, 0.0)
    return cash if cash > 0 else 100_000.0


def _starting_cash_for_bootstrap(value: Any = None) -> float:
    cash = _safe_float(value, 0.0)
    if cash <= 0:
        cash = _safe_float(os.environ.get("ASHARE_SIM_INITIAL_CASH"), 0.0)
    return cash if cash > 0 else 20_000.0


def _sim_account_snapshot_unlocked(
    trades: list[dict[str, Any]],
    *,
    account: str,
    symbol: str = "",
    trade_date: str = "",
    starting_cash: float = 100_000.0,
) -> dict[str, Any]:
    lots_by_symbol: dict[str, list[dict[str, Any]]] = {}
    cash_available = float(starting_cash)
    as_of = _trade_date(trade_date)
    for trade in trades:
        if str(trade.get("account") or "") != account:
            continue
        if str(trade.get("status") or "") != "filled":
            continue
        code = str(trade.get("ts_code") or "").strip().upper()
        if not code:
            continue
        side = str(trade.get("side") or "").lower()
        qty = _safe_float(trade.get("quantity"), 0.0)
        net_amount = _safe_float(trade.get("net_amount"), 0.0)
        if qty <= 0:
            continue
        if side == "buy":
            cash_available -= net_amount
            lots_by_symbol.setdefault(code, []).append(
                {
                    "quantity": qty,
                    "trade_date": _trade_date(trade.get("trade_date")),
                    "cost_basis": net_amount,
                }
            )
            continue
        if side != "sell":
            continue
        cash_available += net_amount
        remaining = qty
        for lot in lots_by_symbol.get(code, []):
            if remaining <= 0:
                break
            lot_qty = _safe_float(lot.get("quantity"), 0.0)
            used = min(lot_qty, remaining)
            lot["quantity"] = round(lot_qty - used, 8)
            remaining -= used
    positions: dict[str, dict[str, Any]] = {}
    for code, lots in lots_by_symbol.items():
        open_lots = [lot for lot in lots if _safe_float(lot.get("quantity"), 0.0) > 0]
        quantity = sum(_safe_float(lot.get("quantity"), 0.0) for lot in open_lots)
        sellable_quantity = sum(
            _safe_float(lot.get("quantity"), 0.0)
            for lot in open_lots
            if _can_sell_on(lot.get("trade_date"), as_of)
        )
        if quantity <= 0:
            continue
        positions[code] = {
            "quantity": int(quantity) if abs(quantity - round(quantity)) < 1e-12 else round(quantity, 6),
            "sellable_quantity": int(sellable_quantity) if abs(sellable_quantity - round(sellable_quantity)) < 1e-12 else round(sellable_quantity, 6),
            "oldest_open_date": min(str(lot.get("trade_date") or "") for lot in open_lots if lot.get("trade_date")),
        }
    selected = positions.get(str(symbol or "").strip().upper(), {}) if symbol else {}
    return {
        "account": account,
        "trade_date": as_of,
        "cash_available": round(cash_available, 2),
        "sellable_qty": selected.get("sellable_quantity", 0 if symbol else None),
        "position_qty": selected.get("quantity", 0 if symbol else None),
        "positions": positions,
    }


def get_local_sim_account_snapshot(
    account: dict[str, Any] | str | None = None,
    *,
    symbol: str = "",
    trade_date: str = "",
    starting_cash: Any = 100_000.0,
) -> dict[str, Any]:
    """Return server-local simulated cash and T+1 sellable quantity snapshot."""

    account_name = _account_name(account or DEFAULT_ACCOUNT)
    with _lock():
        return _sim_account_snapshot_unlocked(
            _load_trades_unlocked(),
            account=account_name,
            symbol=symbol,
            trade_date=trade_date,
            starting_cash=_starting_cash(starting_cash),
        )


def _append_trade_unlocked(trade: LocalSimTrade) -> None:
    LOCAL_SIM_TRADES.parent.mkdir(parents=True, exist_ok=True)
    with LOCAL_SIM_TRADES.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(trade), ensure_ascii=False) + "\n")


def _replay_account(
    trades: list[dict[str, Any]],
    account: str | None = None,
    mark_prices: dict[str, float] | None = None,
) -> dict[str, Any]:
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
        mark_price = round(float(mark_prices.get(code, last_price)) if mark_prices else last_price, 6)
        value = round(qty * mark_price, 2)
        row_unrealized = round(value - cost, 2)
        clean_positions[code] = {
            "quantity": int(qty) if abs(qty - round(qty)) < 1e-12 else round(qty, 6),
            "cost_basis": cost,
            "avg_cost": round(cost / qty, 4) if qty else 0.0,
            "last_price": last_price,
            "mark_price": mark_price,
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
    _write_positions_snapshot(positions, pnl)


def _write_positions_snapshot(
    positions: dict[str, dict[str, Any]],
    pnl: dict[str, dict[str, Any]],
    *,
    bootstrap: dict[str, Any] | None = None,
) -> None:
    flat_positions: list[dict[str, Any]] = []
    for account, account_positions in positions.items():
        for ts_code, position in account_positions.items():
            flat_positions.append({
                "account": account,
                "ts_code": ts_code,
                "quantity": position.get("quantity", 0),
                "avg_price": position.get("avg_cost", 0.0),
                "last_price": position.get("last_price", 0.0),
                "market_value": position.get("market_value", 0.0),
                "unrealized_pnl": position.get("unrealized_pnl", 0.0),
                "capital_layer": "simulated",
                "account_type": "simulated",
                "source": "server_local_sim_backup",
            })
    payload = {
        "snapshot_id": "simulated_ashare_positions",
        "market": "ashare",
        "account_type": "simulated",
        "capital_layer": "simulated",
        "source": "server_local_sim_backup",
        "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "positions": flat_positions,
        "positions_by_account": positions,
        "pnl": pnl,
    }
    if bootstrap:
        payload.update(bootstrap)
    LOCAL_SIM_POSITIONS_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_SIM_POSITIONS_SNAPSHOT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ensure_local_sim_bootstrap_snapshot(
    account: dict[str, Any] | str | None = None,
    *,
    starting_cash: Any = None,
    trade_date: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Create an empty A-share simulated snapshot before the first local fill."""

    account_name = _account_name(account or "ashare_sim")
    cash = _starting_cash_for_bootstrap(starting_cash)
    with _lock():
        trades = _load_trades_unlocked()
        if trades:
            _persist_unlocked(trades)
            return {"status": "existing_trades", "written": False, "trade_count": len(trades), "account": account_name}
        if LOCAL_SIM_POSITIONS_SNAPSHOT.exists() and not force:
            return {"status": "snapshot_exists", "written": False, "trade_count": 0, "account": account_name}

        positions = {account_name: {}}
        pnl = {
            account_name: {
                "account": account_name,
                "total_trades": 0,
                "buys": 0,
                "sells": 0,
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "market_value": 0.0,
                "total_pnl": 0.0,
                "cash_available": round(cash, 2),
                "positions": {},
            }
        }
        LOCAL_SIM_POSITIONS.parent.mkdir(parents=True, exist_ok=True)
        LOCAL_SIM_POSITIONS.write_text(json.dumps(positions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        LOCAL_SIM_PNL.write_text(json.dumps(pnl, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _write_positions_snapshot(
            positions,
            pnl,
            bootstrap={
                "bootstrap_state": "no_trades_yet",
                "cash_available": round(cash, 2),
                "trade_date": _trade_date(trade_date),
            },
        )
    return {"status": "bootstrapped", "written": True, "trade_count": 0, "account": account_name, "cash_available": round(cash, 2)}


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
        _append_receipt_unlocked(
            _build_signed_receipt(order=order, trade=trade, market=market_key, account=account_name, status="filled")
        )
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
        "receipt_path": str(LOCAL_SIM_RECEIPTS),
    }


def get_local_sim_pnl(
    account: str | None = None,
    mark_prices: dict[str, float] | None = None,
) -> dict[str, Any]:
    with _lock():
        return _replay_account(_load_trades_unlocked(), account, mark_prices=mark_prices)
