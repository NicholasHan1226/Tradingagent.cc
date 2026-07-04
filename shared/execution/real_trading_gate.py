#!/usr/bin/env python3
"""Hard safety gates for any real-money trading signal.

All real trading gates are fail-closed. Importing this module does not enable
real trading; every public gate raises ``SafetyViolation`` on failure.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from shared.accounting import position_ledger
from shared.markets.safety import SafetyViolation

TRADINGAGENT_ROOT = Path(__file__).resolve().parents[2]
SIGNALS_DIR = Path(os.environ.get("TRADINGAGENT_SIGNALS_DIR", TRADINGAGENT_ROOT / "signals"))
DEFAULT_HALT_FILES = (
    SIGNALS_DIR / "real" / "emergency_stop.json",
    SIGNALS_DIR / "real" / "HALT",
    SIGNALS_DIR / "executor_halt.json",
)
CHINA_TZ = ZoneInfo("Asia/Shanghai")
TRUTHY = {"1", "true", "yes", "y", "on", "enabled", "enable"}


@dataclass(frozen=True)
class GateResult:
    """Result returned by successful gate calls."""

    passed: bool
    failed: bool
    reason: str
    gate: str = ""


def _pass(gate: str, reason: str = "passed") -> GateResult:
    return GateResult(passed=True, failed=False, reason=reason, gate=gate)


def _raise(gate: str, reason: str) -> None:
    raise SafetyViolation(f"{gate}: {reason}")


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in TRUTHY


def _money(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        _raise("capital_limits", f"{field} must be numeric")
    if parsed <= 0:
        _raise("capital_limits", f"{field} must be positive")
    return parsed


def _optional_money(value: Any, field: str, default: Decimal = Decimal("0")) -> Decimal:
    if value in (None, ""):
        return default
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        _raise("capital_limits", f"{field} must be numeric")
    if parsed < 0:
        _raise("capital_limits", f"{field} must be non-negative")
    return parsed


def _order_notional(order: dict[str, Any]) -> Decimal:
    for field in ("notional", "amount", "order_amount", "estimated_amount"):
        value = order.get(field)
        if value not in (None, ""):
            return _money(value, field)
    quantity = _money(order.get("quantity"), "quantity")
    price = _money(order.get("price", order.get("limit_price", order.get("execution_price"))), "price")
    return quantity * price


def _date_part(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = str(value).strip()
    if "T" in text:
        return text.split("T", 1)[0]
    return text[:10]


def validate_real_trading_enabled(env: dict[str, str] | None = None) -> GateResult:
    """Require explicit process-level enablement.

    ``REAL_TRADING_ENABLED`` is disabled by default. Only clear truthy values
    such as ``1`` or ``enabled`` pass.
    """

    env = env or os.environ
    if not _truthy(env.get("REAL_TRADING_ENABLED")):
        _raise("real_trading_enabled", "REAL_TRADING_ENABLED is not enabled")
    return _pass("real_trading_enabled")


def require_explicit_approval(
    token: str | None = None,
    *,
    expected_token: str | None = None,
    env: dict[str, str] | None = None,
) -> GateResult:
    """Require a manual confirmation token before a real order can advance."""

    env = env or os.environ
    expected = expected_token or env.get("REAL_TRADING_APPROVAL_TOKEN")
    provided = token or env.get("REAL_TRADING_MANUAL_CONFIRMATION_TOKEN")
    if not expected:
        _raise("explicit_approval", "REAL_TRADING_APPROVAL_TOKEN is not configured")
    if not provided:
        _raise("explicit_approval", "manual confirmation token is missing")
    if provided != expected:
        _raise("explicit_approval", "manual confirmation token does not match")
    return _pass("explicit_approval")


def validate_capital_limits(
    order: dict[str, Any],
    max_per_order: Any,
    max_daily: Any,
) -> GateResult:
    """Enforce hard per-order and daily real-money notional caps."""

    if not isinstance(order, dict):
        _raise("capital_limits", "order must be a dict")
    per_order_cap = _money(max_per_order, "max_per_order")
    daily_cap = _money(max_daily, "max_daily")
    notional = _order_notional(order)
    daily_used = _optional_money(
        order.get("daily_notional_used", order.get("daily_used", order.get("daily_real_notional_used"))),
        "daily_notional_used",
    )
    if notional > per_order_cap:
        _raise("capital_limits", f"order notional {notional} exceeds max_per_order {per_order_cap}")
    if daily_used + notional > daily_cap:
        _raise("capital_limits", f"daily notional {daily_used + notional} exceeds max_daily {daily_cap}")
    return _pass("capital_limits", f"notional={notional}")


def validate_market_hours(now: datetime | None = None) -> GateResult:
    """Require an A-share trading day and continuous auction session."""

    now = now or datetime.now(CHINA_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CHINA_TZ)
    local_now = now.astimezone(CHINA_TZ)

    from Ashare.t_plus_1 import is_trading_day

    if not is_trading_day(local_now.date()):
        _raise("market_hours", f"{local_now.date().isoformat()} is not an A-share trading day")
    current = local_now.time()
    in_session = (
        time(9, 30) <= current <= time(11, 30)
        or time(13, 0) <= current <= time(14, 57)
    )
    if not in_session:
        _raise("market_hours", f"{local_now.isoformat(timespec='seconds')} is outside real trading session")
    return _pass("market_hours")


def _position_entry_date(order: dict[str, Any], positions: list[dict[str, Any]] | None = None) -> str:
    for field in ("entry_date", "position_open_date", "open_date", "sellable_from"):
        value = _date_part(order.get(field))
        if value:
            return value
    t_plus_1 = order.get("t_plus_1")
    if isinstance(t_plus_1, dict):
        for field in ("entry_date", "position_open_date", "open_date", "sellable_from"):
            value = _date_part(t_plus_1.get(field))
            if value:
                return value

    symbol = str(order.get("ts_code") or order.get("symbol") or "").strip()
    if not symbol:
        return ""
    source_positions = positions
    if source_positions is None:
        try:
            source_positions = position_ledger.get_positions(capital_layer="real")
        except TimeoutError:
            logging.getLogger("tradingagent.gate").warning(
                "T+1 validation skipped — position_ledger lock timeout"
            )
            return ""
    matches = [row for row in source_positions if str(row.get("ts_code") or row.get("symbol") or "").strip() == symbol]
    if len(matches) != 1:
        return ""
    return _date_part(matches[0].get("entry_date") or matches[0].get("open_date"))


def validate_t1_settlement(
    order: dict[str, Any],
    *,
    trade_date: date | datetime | str | None = None,
    positions: list[dict[str, Any]] | None = None,
) -> GateResult:
    """Enforce A-share T+1 before any real sell/reduce order."""

    if not isinstance(order, dict):
        _raise("t1_settlement", "order must be a dict")
    side = str(order.get("direction", order.get("side", ""))).strip().lower()
    if side not in {"sell", "reduce"}:
        return _pass("t1_settlement", "buy-side order")

    resolved_trade_date = (
        trade_date
        or _date_part(order.get("trade_date"))
        or _date_part(order.get("current_trade_date"))
        or _date_part(order.get("current_date"))
        or _date_part(order.get("timestamp"))
    )
    if not resolved_trade_date:
        _raise("t1_settlement", "sell-side order requires trade_date")
    entry_date = _position_entry_date(order, positions=positions)
    if not entry_date:
        _raise("t1_settlement", "sell-side order requires a unique real position entry_date")

    from Ashare.t_plus_1 import can_sell, next_trading_day

    if not can_sell(entry_date, resolved_trade_date):
        sellable_date = next_trading_day(entry_date).isoformat()
        _raise(
            "t1_settlement",
            f"entry_date={entry_date}, sellable_date={sellable_date}, trade_date={resolved_trade_date}",
        )
    return _pass("t1_settlement")


def emergency_stop_check(halt_files: list[Path | str] | tuple[Path | str, ...] | None = None) -> GateResult:
    """Reject real trading when an emergency halt marker exists."""

    if _truthy(os.environ.get("REAL_TRADING_EMERGENCY_STOP")):
        _raise("emergency_stop", "REAL_TRADING_EMERGENCY_STOP is active")
    configured = os.environ.get("REAL_TRADING_HALT_FILE")
    files = list(halt_files or DEFAULT_HALT_FILES)
    if configured:
        files.append(Path(configured))
    for raw_path in files:
        path = Path(raw_path)
        if path.exists():
            _raise("emergency_stop", f"halt file exists: {path}")
    return _pass("emergency_stop")


def run_real_order_gates(
    order: dict[str, Any],
    *,
    approval_token: str | None = None,
    max_per_order: Any | None = None,
    max_daily: Any | None = None,
    now: datetime | None = None,
    positions: list[dict[str, Any]] | None = None,
    halt_files: list[Path | str] | tuple[Path | str, ...] | None = None,
) -> GateResult:
    """Run every real-order gate and raise ``SafetyViolation`` on first fail."""

    emergency_stop_check(halt_files=halt_files)
    validate_real_trading_enabled()
    require_explicit_approval(approval_token)
    validate_capital_limits(
        order,
        max_per_order if max_per_order is not None else os.environ.get("REAL_TRADING_MAX_PER_ORDER"),
        max_daily if max_daily is not None else os.environ.get("REAL_TRADING_MAX_DAILY"),
    )
    validate_market_hours(now=now)
    validate_t1_settlement(order, trade_date=now.date().isoformat() if isinstance(now, datetime) else None, positions=positions)
    return _pass("all_gates", "all real trading gates passed")
