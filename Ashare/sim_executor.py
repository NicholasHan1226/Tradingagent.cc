#!/usr/bin/env python3
"""A-share simulated executor backed by the Mac Mini file bridge.

Production simulated orders default to the server-local paper loop. The Mac Mini
Hermes/Tonghuashun bridge is retained as an explicitly enabled secondary route
(``ASHARE_SIM_HERMES_ENABLED=1`` or ``config["hermes_enabled"]=True``), but is
not required for server-side simulated training data.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from shared.execution.signal_state_machine import SignalStateMachine
from shared.execution.sim_engine import SimExecutionEngine, SimOrder
from shared.execution.sim_broker import SimResult
from shared.execution.sim_executor_registry import register_sim_executor
from shared.execution.webhook_sender import send_sim_signal_to_mini


DEFAULT_SIGNALS_DIR = Path("/opt/investment/tradingagent/signals")
MARKET = "ashare"
SIM_ACCOUNT = "ashare_sim"
CN_TZ = ZoneInfo("Asia/Shanghai")


def _now_cn() -> datetime:
    return datetime.now(CN_TZ)


def _account_name(account: dict[str, Any] | str | None) -> str:
    if isinstance(account, dict):
        for key in ("account", "account_id", "account_name", "name"):
            value = str(account.get(key, "")).strip()
            if value:
                return value
    value = str(account or "").strip()
    return value or SIM_ACCOUNT


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_supported_ashare_code(code: Any) -> bool:
    raw = str(code or "").strip().upper()
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


def _reject(order_id: str, code: str, message: str) -> SimResult:
    return SimResult(
        status="rejected",
        filled_qty=0,
        avg_price=0.0,
        fee=0.0,
        message=message,
        order_id=order_id,
        market=MARKET,
        raw_response={
            "mode": "pre_bridge_validation",
            "code": code,
            "reason": message,
        },
    )


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _parse_session_now(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    else:
        parsed = _now_cn()
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _is_regular_trading_session(now: datetime) -> bool:
    from Ashare.t_plus_1 import is_trading_day

    if not is_trading_day(now.date()):
        return False
    current = now.time()
    return (time(9, 30) <= current <= time(11, 30)) or (time(13, 0) <= current <= time(14, 57))


def _market_session_rejection(config: dict[str, Any]) -> str:
    if _coerce_bool(config.get("bypass_market_hours"), False):
        return ""
    enforce = _coerce_bool(os.environ.get("ASHARE_SIM_ENFORCE_MARKET_HOURS"), True)
    enforce = _coerce_bool(config.get("enforce_market_hours"), enforce)
    if not enforce:
        return ""
    now = _parse_session_now(config.get("market_session_now") or config.get("now"))
    if _is_regular_trading_session(now):
        return ""
    return f"market_closed: A-share simulated execution only runs during 09:30-11:30 or 13:00-14:57 Asia/Shanghai; now={now.isoformat(timespec='seconds')}"


def _first_value(*values: Any, default: Any = None) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return default


def _snapshot_field_source(
    field: str,
    order: dict[str, Any],
    config: dict[str, Any],
    card: dict[str, Any],
    snapshot: dict[str, Any],
) -> str:
    for owner_name, owner in (("order", order), ("config", config)):
        source_snapshot = owner.get("market_snapshot")
        if isinstance(source_snapshot, dict) and source_snapshot.get(field) not in (None, ""):
            return f"{owner_name}.market_snapshot.{field}"
    if order.get(field) not in (None, ""):
        return f"order.{field}"
    if config.get(field) not in (None, ""):
        return f"config.{field}"
    if field in {"ask_price", "bid_price", "last_price"} and card.get("price") not in (None, ""):
        if snapshot.get(field) == card.get("price"):
            return "signal_card.price"
    if snapshot.get(field) not in (None, ""):
        return f"snapshot.{field}"
    return ""


def _fill_evidence_from_snapshot(
    order: dict[str, Any],
    config: dict[str, Any],
    card: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    side = str(card.get("side") or order.get("side") or "buy").lower()
    quote_field = "ask_price" if side == "buy" else "bid_price"
    quote_source = _snapshot_field_source(quote_field, order, config, card, snapshot)
    last_source = _snapshot_field_source("last_price", order, config, card, snapshot)
    volume_source = (
        _snapshot_field_source("bar_volume", order, config, card, snapshot)
        or _snapshot_field_source("volume", order, config, card, snapshot)
        or _snapshot_field_source("vol", order, config, card, snapshot)
    )
    source_class = "market_data" if quote_source and quote_source != "signal_card.price" else "signal_card_price"
    return {
        "fill_price_field": quote_field,
        "fill_price_source": quote_source or last_source or "unknown",
        "fill_price_source_class": source_class,
        "quote_price": snapshot.get(quote_field),
        "last_price": snapshot.get("last_price"),
        "last_price_source": last_source,
        "bar_volume": _first_value(snapshot.get("bar_volume"), snapshot.get("volume"), snapshot.get("vol")),
        "bar_volume_source": volume_source,
        "bar_time": _first_value(order.get("bar_time"), order.get("trade_time"), config.get("bar_time"), config.get("trade_time"), snapshot.get("bar_time"), snapshot.get("trade_time")),
    }


def _date_iso(value: Any, fallback: str) -> str:
    raw = str(value or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    if len(raw) >= 10 and raw[4:5] == "-" and raw[7:8] == "-":
        return raw[:10]
    return fallback


def _snapshot_from_payload(
    order: dict[str, Any],
    account: dict[str, Any] | str | None,
    config: dict[str, Any],
    card: dict[str, Any],
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for source in (config.get("market_snapshot"), order.get("market_snapshot")):
        if isinstance(source, dict):
            snapshot.update(source)

    price = float(card.get("price") or 0.0)
    side = str(card.get("side") or "buy").lower()
    bar_liquidity = _first_value(
        order.get("bar_volume"),
        order.get("volume"),
        order.get("vol"),
        config.get("bar_volume"),
        config.get("volume"),
        config.get("vol"),
    )
    default_size = None if bar_liquidity is not None else card.get("quantity")
    if side == "buy":
        snapshot.setdefault("ask_price", _first_value(order.get("ask_price"), config.get("ask_price"), price))
        ask_size = _first_value(order.get("ask_size"), config.get("ask_size"), default_size)
        if ask_size is not None:
            snapshot.setdefault("ask_size", ask_size)
    else:
        snapshot.setdefault("bid_price", _first_value(order.get("bid_price"), config.get("bid_price"), price))
        bid_size = _first_value(order.get("bid_size"), config.get("bid_size"), default_size)
        if bid_size is not None:
            snapshot.setdefault("bid_size", bid_size)
    snapshot.setdefault("last_price", _first_value(order.get("last_price"), config.get("last_price"), price))
    available_qty = _first_value(order.get("available_qty"), config.get("available_qty"), default_size)
    if available_qty is not None:
        snapshot.setdefault("available_qty", available_qty)

    for key in (
        "previous_close",
        "pre_close",
        "reference_price",
        "upper_limit",
        "lower_limit",
        "price_limit_pct",
        "bar_volume",
        "volume",
        "vol",
        "volatility",
        "volatility_bps",
        "queue_position",
        "participation_cap",
        "liquidity_multiplier",
        "market_impact_multiplier",
        "counterparty_profile",
        "market_environment",
    ):
        value = _first_value(order.get(key), config.get(key))
        if value is not None:
            snapshot.setdefault(key, value)

    if isinstance(account, dict):
        cash_available = _first_value(account.get("cash_available"), account.get("cash"), account.get("available_cash"))
        sellable_qty = _first_value(account.get("sellable_qty"), account.get("available_position"))
    else:
        cash_available = None
        sellable_qty = None
    cash_available = _first_value(order.get("cash_available"), config.get("cash_available"), cash_available)
    sellable_qty = _first_value(order.get("sellable_qty"), config.get("sellable_qty"), sellable_qty)
    if cash_available is None or sellable_qty is None:
        try:
            from shared.execution.local_sim_ledger import get_local_sim_account_snapshot

            account_snapshot = get_local_sim_account_snapshot(
                account or _account_name(account),
                symbol=str(card.get("ts_code") or ""),
                trade_date=str(card.get("valid_until") or order.get("trade_date") or order.get("date") or ""),
                starting_cash=_first_value(
                    config.get("starting_cash"),
                    config.get("initial_capital"),
                    account.get("initial_capital") if isinstance(account, dict) else None,
                    account.get("cash") if isinstance(account, dict) else None,
                    default=200_000.0,
                ),
            )
        except Exception:
            account_snapshot = {}
        if cash_available is None:
            cash_available = account_snapshot.get("cash_available")
        if sellable_qty is None:
            sellable_qty = account_snapshot.get("sellable_qty")
    if cash_available is not None:
        snapshot.setdefault("cash_available", cash_available)
    if sellable_qty is not None:
        snapshot.setdefault("sellable_qty", sellable_qty)
    return snapshot


def _execute_server_local(
    order: dict[str, Any],
    account: dict[str, Any] | str | None,
    config: dict[str, Any],
    card: dict[str, Any],
) -> SimResult:
    safe_metadata = dict(order)
    safe_card = dict(card)
    safe_card["direct_execution"] = False
    safe_metadata["signal_card"] = safe_card
    market_snapshot = _snapshot_from_payload(order, account, config, card)
    fill_evidence = _fill_evidence_from_snapshot(order, config, card, market_snapshot)
    safe_metadata["fill_evidence"] = fill_evidence
    safe_metadata["fill_price_source"] = fill_evidence["fill_price_source"]
    safe_metadata["fill_price_source_class"] = fill_evidence["fill_price_source_class"]
    sim_order = SimOrder(
        symbol=str(card["ts_code"]),
        side=str(card.get("side") or "buy"),
        quantity=int(card["quantity"]),
        limit_price=float(card["price"]),
        order_type=str(order.get("order_type") or config.get("order_type") or "market"),
        time_in_force=str(order.get("time_in_force") or config.get("time_in_force") or "day"),
        market=MARKET,
        order_id=str(card["order_id"]),
        metadata=safe_metadata,
    )
    engine = SimExecutionEngine(MARKET, profile=config.get("sim_engine_profile"))
    record = engine.submit_order(sim_order, market_snapshot)
    status = "pending" if record.state == "open" else record.state
    fee = float((record.fees or {}).get("total", 0.0) or 0.0)
    reason_suffix = f": {record.reason}" if getattr(record, "reason", "") else ""
    return SimResult(
        status=status,
        filled_qty=int(record.filled_qty or 0),
        avg_price=float(record.avg_fill_price or 0.0),
        fee=fee,
        message=f"Server-local A-share simulated fill via matching engine: {record.state}{reason_suffix}",
        order_id=sim_order.order_id,
        market=MARKET,
        raw_response={
            "mode": "server_local_sim_engine",
            "hermes_enabled": False,
            "signal_card": card,
            "market_snapshot": market_snapshot,
            "fill_evidence": fill_evidence,
            "engine_record": record.as_dict(),
        },
    )


def _signal_card(
    order: dict[str, Any],
    account: dict[str, Any] | str | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    now = datetime.now().astimezone()
    today = now.date().isoformat()
    trade_date = _date_iso(order.get("trade_date") or order.get("date"), today)
    order_id = str(order.get("order_id") or f"SIM-ASHARE-{now.strftime('%Y%m%d%H%M%S')}")
    price = _coerce_float(order.get("price", order.get("limit_price", order.get("mid_price"))), 0.0)
    quantity = _coerce_int(order.get("quantity", order.get("qty", order.get("filled_qty"))), 0)
    side = str(order.get("side", order.get("direction", "buy"))).lower().strip() or "buy"
    return {
        "order_id": order_id,
        "market": MARKET,
        "ts_code": str(order.get("ts_code") or order.get("symbol") or "").strip(),
        "direction": side,
        "side": side,
        "quantity": quantity,
        "price": price,
        "trigger_price": price,
        "status": "pending",
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real_trading_enabled": False,
        "account": _account_name(account),
        "manual_confirm_required": False,
        "direct_execution": False,
        "dry_run": bool(config.get("dry_run", False)),
        "strategy_name": str(order.get("strategy_name") or "ashare_sim_executor"),
        "timestamp": now.isoformat(timespec="seconds"),
        "valid_until": str(config.get("valid_until") or trade_date),
        "idempotency_key": str(order.get("idempotency_key") or order_id),
        "source": "ashare_sim_executor_file_bridge",
        "bridge": "mini_hermes_file_bridge",
        "t_plus_1": {
            "sellable_from": str(config.get("sellable_from") or trade_date),
            "sellable_date": str(config.get("sellable_date") or trade_date),
        },
        "notes": "Hermes/Mac Mini bridge is reserved; server-local simulated execution is primary unless explicitly enabled.",
    }


def ashare_sim_execute(
    order: dict[str, Any],
    account: dict[str, Any] | str | None = None,
    config: dict[str, Any] | None = None,
) -> SimResult:
    """Queue an A-share simulated order for Mini execution, or fill via mock."""

    config = dict(config or {})
    card = _signal_card(order, account, config)
    order_id = str(card["order_id"])
    code = str(card.get("ts_code") or "").strip().upper()
    if not _is_supported_ashare_code(code):
        return _reject(order_id, code, f"unsupported or non-A-share code: {code}")
    if int(card.get("quantity") or 0) <= 0 or float(card.get("price") or 0.0) <= 0:
        return _reject(order_id, code, "non-positive quantity or price")
    mock_mode = bool(
        config.get("mock")
        or config.get("mock_filled")
        or config.get("mock_mini_filled")
    )

    if mock_mode:
        return SimResult(
            status="filled",
            filled_qty=int(card["quantity"]),
            avg_price=float(card["price"]),
            fee=float(config.get("mock_fee", 0.0) or 0.0),
            message="Local mock fill for A-share simulated execution tests",
            order_id=order_id,
            market=MARKET,
            raw_response={
                "mode": "mock_filled",
                "signal_card": card,
            },
        )

    market_rejection = _market_session_rejection(config)
    if market_rejection:
        return _reject(order_id, code, market_rejection)

    hermes_enabled = bool(config.get("hermes_enabled")) or os.environ.get("ASHARE_SIM_HERMES_ENABLED", "0") == "1"
    if not hermes_enabled:
        return _execute_server_local(order, account, config, card)

    signals_dir = Path(config.get("signals_dir") or DEFAULT_SIGNALS_DIR)
    env_webhook = os.environ.get("ASHARE_SIM_WEBHOOK_ENABLED")
    if "webhook" in config:
        webhook_enabled = bool(config.get("webhook"))
    elif signals_dir == DEFAULT_SIGNALS_DIR:
        # Hermes is a reserved secondary route. When explicitly enabled, the
        # production path can still attempt the Mini webhook unless separately
        # disabled for rollback.
        webhook_enabled = env_webhook != "0"
    else:
        webhook_enabled = env_webhook == "1"
    webhook_result: dict[str, Any] | None = None

    if webhook_enabled:
        webhook_kwargs: dict[str, Any] = {}
        for config_key, arg_name in (
            ("webhook_url", "url"),
            ("webhook_secret", "secret"),
            ("webhook_timeout", "timeout"),
            ("webhook_retries", "retries"),
        ):
            if config.get(config_key) not in (None, ""):
                webhook_kwargs[arg_name] = config[config_key]
        webhook_result = send_sim_signal_to_mini(card, **webhook_kwargs)
        if webhook_result.get("success"):
            return SimResult(
                status="pending",
                filled_qty=0,
                avg_price=0.0,
                fee=0.0,
                message="Sent to Mac Mini Hermes webhook for simulated execution",
                order_id=order_id,
                market=MARKET,
                raw_response={
                    "mode": "mini_webhook_sent",
                    "webhook": webhook_result,
                    "signal_card": card,
                },
            )

    machine = SignalStateMachine(signals_dir)
    queued = machine.write_pending(card)
    mode = "file_bridge_pending_after_webhook_failed" if webhook_result else "file_bridge_pending"
    return SimResult(
        status="pending",
        filled_qty=0,
        avg_price=0.0,
        fee=0.0,
        message="Queued for Mac Mini Hermes file bridge execution",
        order_id=order_id,
        market=MARKET,
        raw_response={
            "mode": mode,
            "webhook": webhook_result or {},
            "signals_dir": str(signals_dir),
            "signal_path": queued.get("signal_path", ""),
            "signal_card": queued.get("signal_card", card),
        },
    )


register_sim_executor(MARKET, ashare_sim_execute)


__all__ = ["ashare_sim_execute"]
