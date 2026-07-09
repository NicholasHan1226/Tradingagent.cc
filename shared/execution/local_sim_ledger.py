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
from datetime import date, datetime, time as dt_time, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from shared.markets.sim_capital import default_sim_capital

LOCAL_SIM_DIR = Path(__file__).resolve().parent.parent / "logs" / "local_sim"
LOCAL_SIM_TRADES = LOCAL_SIM_DIR / "local_sim_trades.jsonl"
LOCAL_SIM_POSITIONS = LOCAL_SIM_DIR / "local_sim_positions.json"
LOCAL_SIM_PNL = LOCAL_SIM_DIR / "local_sim_pnl.json"
LOCAL_SIM_LOCK = LOCAL_SIM_DIR / ".local_sim.lock"
LOCAL_SIM_POSITIONS_SNAPSHOT = Path(__file__).resolve().parents[2] / "signals" / "positions" / "simulated_ashare_positions.json"
LOCAL_SIM_RECEIPTS = Path(__file__).resolve().parents[2] / "signals" / "sim_execution_receipts.jsonl"
DEFAULT_ACCOUNT = "ashare_server_sim"
ASHARE_SIM_DEFAULT_CASH = default_sim_capital("ashare")
LOCK_RETRY_ATTEMPTS = 3
LOCK_RETRY_DELAY_SECONDS = 0.1
CHECKSUM_KEYS = {"payload_sha256", "receipt_sha256", "checksum", "sha256"}
CN_TZ = ZoneInfo("Asia/Shanghai")


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
    candidate_pool_layer: str = ""
    execution_source: str = ""
    fill_price_source: str = ""
    fill_price_source_class: str = ""
    fill_evidence: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    trade_timestamp_bj: str = ""
    ashare_session_valid: bool = True
    ashare_session_rejection: str = ""
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


def _parse_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _is_ashare_regular_session(ts: datetime) -> bool:
    try:
        from Ashare.t_plus_1 import is_trading_day

        if not is_trading_day(ts.date()):
            return False
    except Exception:
        if ts.weekday() >= 5:
            return False
    current = ts.time()
    return (dt_time(9, 30) <= current <= dt_time(11, 30)) or (dt_time(13, 0) <= current <= dt_time(14, 57))


def _ashare_session_metadata(market: Any, symbol: Any, created_at: str) -> dict[str, Any]:
    if str(market or "").strip().lower() != "ashare" or not _is_regular_ashare_symbol(symbol):
        return {"trade_timestamp_bj": "", "ashare_session_valid": True, "ashare_session_rejection": ""}
    ts = _parse_timestamp(created_at) or datetime.now(CN_TZ)
    session_valid = _is_ashare_regular_session(ts)
    return {
        "trade_timestamp_bj": ts.isoformat(timespec="seconds"),
        "ashare_session_valid": session_valid,
        "ashare_session_rejection": "" if session_valid else "outside_regular_session_09:30-11:30_13:00-14:57",
    }


def _ashare_provenance_error(side: str, candidate_pool_layer: str, execution_source: str) -> str:
    side_key = str(side or "").lower().strip()
    layer = str(candidate_pool_layer or "").lower().strip()
    source = str(execution_source or "").lower().strip()
    if side_key == "buy" and not (layer == "candidate" and source == "ashare_candidate_layer"):
        return "A-share simulated buy requires candidate_pool_layer=candidate and execution_source=ashare_candidate_layer"
    if side_key == "sell" and source != "ashare_rebalance_sell":
        return "A-share simulated sell requires execution_source=ashare_rebalance_sell"
    return ""


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
                "fill_price_source": trade.fill_price_source,
                "fill_price_source_class": trade.fill_price_source_class,
                "fill_evidence": trade.fill_evidence,
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
    return cash if cash > 0 else ASHARE_SIM_DEFAULT_CASH


def _starting_cash_for_bootstrap(value: Any = None) -> float:
    cash = _safe_float(value, 0.0)
    if cash <= 0:
        cash = _safe_float(os.environ.get("ASHARE_SIM_INITIAL_CASH"), 0.0)
    return cash if cash > 0 else ASHARE_SIM_DEFAULT_CASH


def _sim_account_snapshot_unlocked(
    trades: list[dict[str, Any]],
    *,
    account: str,
    symbol: str = "",
    trade_date: str = "",
    starting_cash: float = ASHARE_SIM_DEFAULT_CASH,
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
    starting_cash: Any = ASHARE_SIM_DEFAULT_CASH,
    include_validation_samples: bool = False,
) -> dict[str, Any]:
    """Return server-local simulated cash and T+1 sellable quantity snapshot."""

    account_name = _account_name(account or DEFAULT_ACCOUNT)
    with _lock():
        trades = _load_trades_unlocked()
        if not include_validation_samples:
            trades = _strategy_trades_only(trades)
        return _sim_account_snapshot_unlocked(
            trades,
            account=account_name,
            symbol=symbol,
            trade_date=trade_date,
            starting_cash=_starting_cash(starting_cash),
        )


def _append_trade_unlocked(trade: LocalSimTrade) -> None:
    LOCAL_SIM_TRADES.parent.mkdir(parents=True, exist_ok=True)
    with LOCAL_SIM_TRADES.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(trade), ensure_ascii=False) + "\n")


def _is_strategy_sample_trade(trade: dict[str, Any]) -> bool:
    """Return whether a trade may consume active A-share strategy capital."""

    try:
        from shared.review.sample_quality import classify_trade_sample
    except Exception:
        return True
    try:
        return bool(classify_trade_sample(trade).get("strategy_sample_valid"))
    except Exception:
        return True


def _strategy_trades_only(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [trade for trade in trades if _is_strategy_sample_trade(trade)]


def _replay_account(
    trades: list[dict[str, Any]],
    account: str | None = None,
    mark_prices: dict[str, float] | None = None,
    starting_cash: float = ASHARE_SIM_DEFAULT_CASH,
) -> dict[str, Any]:
    positions: dict[str, dict[str, Any]] = {}
    realized_pnl = 0.0
    total_trades = 0
    buys = 0
    sells = 0
    cash_available = float(starting_cash)
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
            cash_available -= net_amount
            pos["quantity"] += qty
            pos["cost_basis"] += net_amount
            pos["last_price"] = filled_price or pos["last_price"]
            pos["trades"] += 1
            buys += 1
            continue
        if side != "sell" or qty <= 0 or pos["quantity"] <= 0:
            continue
        cash_available += net_amount
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
        "cash_available": round(cash_available, 2),
        "positions": clean_positions,
    }


def _persist_unlocked(trades: list[dict[str, Any]]) -> None:
    accounts = sorted({str(t.get("account") or DEFAULT_ACCOUNT) for t in trades if t.get("account")})
    strategy_trades = _strategy_trades_only(trades)
    positions = {account: _replay_account(strategy_trades, account)["positions"] for account in accounts}
    pnl = {account: _replay_account(strategy_trades, account) for account in accounts}
    audit_positions = {account: _replay_account(trades, account)["positions"] for account in accounts}
    audit_pnl = {account: _replay_account(trades, account) for account in accounts}
    LOCAL_SIM_POSITIONS.write_text(json.dumps(positions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    LOCAL_SIM_PNL.write_text(json.dumps(pnl, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_positions_snapshot(positions, pnl, audit_positions=audit_positions, audit_pnl=audit_pnl)


def _write_positions_snapshot(
    positions: dict[str, dict[str, Any]],
    pnl: dict[str, dict[str, Any]],
    *,
    bootstrap: dict[str, Any] | None = None,
    audit_positions: dict[str, dict[str, Any]] | None = None,
    audit_pnl: dict[str, dict[str, Any]] | None = None,
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
        "account_view": "strategy_samples_only",
        "audit_positions_by_account": audit_positions or positions,
        "audit_pnl": audit_pnl or pnl,
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
            try:
                existing = json.loads(LOCAL_SIM_POSITIONS_SNAPSHOT.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
            existing_cash = _safe_float(existing.get("cash_available"), -1.0) if isinstance(existing, dict) else -1.0
            existing_bootstrap = str(existing.get("bootstrap_state") or "") if isinstance(existing, dict) else ""
            if existing_bootstrap != "no_trades_yet" or abs(existing_cash - cash) < 0.01:
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
    linked_status = str(getattr(receipt, "status", "") or (receipt.get("status") if isinstance(receipt, dict) else "") or "")
    linked_avg_price = _safe_float(getattr(receipt, "avg_price", None) or (receipt.get("avg_price") if isinstance(receipt, dict) else None), 0.0)
    raw_response = getattr(receipt, "raw_response", None)
    if raw_response is None and isinstance(receipt, dict):
        raw_response = receipt.get("raw_response")
    if not isinstance(raw_response, dict):
        raw_response = {}
    metadata = order.get("metadata") if isinstance(order.get("metadata"), dict) else {}
    fill_evidence = raw_response.get("fill_evidence") if isinstance(raw_response.get("fill_evidence"), dict) else metadata.get("fill_evidence")
    if not isinstance(fill_evidence, dict):
        fill_evidence = {}
    fill_price_source = str(order.get("fill_price_source") or metadata.get("fill_price_source") or fill_evidence.get("fill_price_source") or "")
    fill_price_source_class = str(order.get("fill_price_source_class") or metadata.get("fill_price_source_class") or fill_evidence.get("fill_price_source_class") or "")
    if 'local_sim_slippage_bps' in config:
        slippage_bps = _safe_float(config.get('local_sim_slippage_bps'), 5.0)
    else:
        slippage_bps = _safe_float(os.environ.get('ASHARE_LOCAL_SIM_SLIPPAGE_BPS'), 5.0)
    if linked_avg_price > 0:
        filled_price = linked_avg_price
        if requested_price > 0:
            direction = 1.0 if side == "buy" else -1.0
            slippage_bps = round(((filled_price / requested_price) - 1.0) * 10000.0 * direction, 6)
    else:
        filled_price = requested_price * (1.0 + slippage_bps / 10000.0) if side == "buy" else requested_price * (1.0 - slippage_bps / 10000.0)
    filled_price = round(filled_price, 4)
    amount = round(quantity * filled_price, 2)
    commission = round(max(amount * 0.00025, 5.0), 2)
    stamp_duty = round(amount * 0.0005, 2) if side == "sell" else 0.0
    net_amount = round(amount + commission + stamp_duty, 2) if side == "buy" else round(amount - commission - stamp_duty, 2)
    candidate_pool_layer = str(order.get("candidate_pool_layer") or metadata.get("candidate_pool_layer") or "")
    execution_source = str(order.get("execution_source") or metadata.get("execution_source") or "")
    if linked_status and linked_status not in {"filled", "partial"}:
        return {
            "status": linked_status,
            "recorded": False,
            "reason": "server-local A-share ledger records filled/partial receipts only",
            "order_id": order_id,
            "idempotency_key": idempotency_key,
            "account": account_name,
        }
    provenance_error = _ashare_provenance_error(side, candidate_pool_layer, execution_source)
    if provenance_error:
        return {
            "status": "rejected",
            "recorded": False,
            "reason": provenance_error,
            "order_id": order_id,
            "idempotency_key": idempotency_key,
            "account": account_name,
        }
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    session_metadata = _ashare_session_metadata(market_key, code, created_at)
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
        candidate_pool_layer=candidate_pool_layer,
        execution_source=execution_source,
        fill_price_source=fill_price_source,
        fill_price_source_class=fill_price_source_class,
        fill_evidence=fill_evidence,
        created_at=created_at,
        trade_timestamp_bj=str(session_metadata["trade_timestamp_bj"]),
        ashare_session_valid=bool(session_metadata["ashare_session_valid"]),
        ashare_session_rejection=str(session_metadata["ashare_session_rejection"]),
        linked_execution_status=linked_status,
        note=str(order.get("note") or "server backup fill for A-share simulated signal"),
    )
    with _lock():
        trades = _load_trades_unlocked()
        for existing in trades:
            if str(existing.get("idempotency_key") or "") == idempotency_key:
                return {"status": "duplicate", "recorded": False, "trade_id": existing.get("trade_id", ""), "idempotency_key": idempotency_key, "account": account_name}
        starting_cash = _starting_cash(
            config.get("starting_cash")
            or config.get("initial_capital")
            or (account.get("initial_capital") if isinstance(account, dict) else None)
            or (account.get("sim_capital") if isinstance(account, dict) else None)
            or ASHARE_SIM_DEFAULT_CASH
        )
        if side == "buy":
            current = _replay_account(_strategy_trades_only(trades), account_name, starting_cash=starting_cash)
            cash_available = _safe_float(current.get("cash_available"), 0.0)
            if cash_available + 1e-9 < net_amount:
                return {
                    "status": "rejected",
                    "recorded": False,
                    "reason": "insufficient_cash",
                    "order_id": order_id,
                    "idempotency_key": idempotency_key,
                    "account": account_name,
                    "cash_available": round(cash_available, 2),
                    "required_cash": round(net_amount, 2),
                }
        if side == "sell":
            current = _replay_account(_strategy_trades_only(trades), account_name, starting_cash=starting_cash)["positions"].get(code, {})
            if quantity > _safe_int(current.get("quantity"), 0):
                return {"status": "rejected", "recorded": False, "reason": f"sell quantity {quantity} exceeds local simulated position {current.get('quantity', 0)} for {code}", "account": account_name}
        _append_trade_unlocked(trade)
        trades.append(asdict(trade))
        _persist_unlocked(trades)
        _append_receipt_unlocked(
            _build_signed_receipt(
                order=order,
                trade=trade,
                market=market_key,
                account=account_name,
                status="filled",
                extra={
                    "candidate_pool_layer": candidate_pool_layer,
                    "execution_source": execution_source,
                    "fill_price_source": fill_price_source,
                    "fill_price_source_class": fill_price_source_class,
                    "fill_evidence": fill_evidence,
                },
            )
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
        "fill_price_source": fill_price_source,
        "fill_price_source_class": fill_price_source_class,
        "net_amount": net_amount,
        "ledger": "server_local_sim_backup",
        "receipt_path": str(LOCAL_SIM_RECEIPTS),
    }


def get_local_sim_pnl(
    account: str | None = None,
    mark_prices: dict[str, float] | None = None,
    trade_filter: Any | None = None,
    include_validation_samples: bool = False,
) -> dict[str, Any]:
    with _lock():
        trades = _load_trades_unlocked()
        if callable(trade_filter):
            trades = [trade for trade in trades if trade_filter(trade)]
        elif not include_validation_samples:
            trades = _strategy_trades_only(trades)
        return _replay_account(trades, account, mark_prices=mark_prices)
