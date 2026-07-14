#!/usr/bin/env python3
"""Market-agnostic shadow trading orchestrator."""

from __future__ import annotations

import json
import inspect
import math
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import shared.capital as market_capital
from shared.capital.ashare_position_authority import (
    CAPITAL_POSITION_SOURCE_MISMATCH,
    ashare_capital_state_audit,
    build_ashare_capital_position_authority_view,
    canonical_sha256,
    normalize_ashare_positions,
    reconcile_ashare_position_sources,
)
from shared.execution import local_sim_ledger
from shared.execution.execution_reality import ashare_execution_reality
from shared.markets.base import MarketAdapter
from shared.notify import email_sender

ROOT = Path(__file__).resolve().parent.parent
SIGNALS_DIR = ROOT / "signals"
ASHARE_OPPORTUNITY_COST_MIN_ENTRY_SCORE = 0.70
ASHARE_OPPORTUNITY_COST_MIN_SCORE_GAP = 0.12
ASHARE_EXECUTION_BAR_MAX_AGE_MINUTES = 15
CN_TZ = ZoneInfo("Asia/Shanghai")

StageFn = Callable[..., Any]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _date_iso(date_value: str) -> str:
    raw = str(date_value or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


def _ashare_sellable_date(date_value: str, side: str) -> str:
    trade_date = _date_iso(date_value)
    if side.lower() != "buy" or not trade_date:
        return trade_date
    try:
        from Ashare.t_plus_1 import next_sellable_date

        return next_sellable_date(trade_date).isoformat()
    except Exception:
        return trade_date


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


def _lookback_start(date: str, calendar_days: int = 14) -> str:
    raw = str(date or "").replace("-", "")[:8]
    try:
        target = datetime.strptime(raw, "%Y%m%d")
    except ValueError:
        return ""
    return (target - timedelta(days=calendar_days)).strftime("%Y%m%d")


def _safe_quantity(value: Any, default: int | float = 0) -> int | float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result:
        return default
    if abs(result - round(result)) < 1e-12:
        return int(round(result))
    return result


def _execution_quantity(market: str, side: str, quantity: Any) -> int:
    value = _safe_int(quantity, 0)
    if (
        str(market or "").strip().lower() == "ashare"
        and str(side or "").strip().lower() == "buy"
    ):
        return (value // 100) * 100
    return value


def _strategy_config(adapter: MarketAdapter) -> dict[str, Any]:
    try:
        config = adapter.get_strategy_config()
    except Exception:
        return {}
    return config if isinstance(config, dict) else {}


@dataclass
class OrchestratorDeps:
    score_stock: StageFn
    build_pool: StageFn
    debate: StageFn
    risk_check: StageFn
    construct: StageFn
    size_position: StageFn
    record_shadow: StageFn
    run_review: StageFn
    record_audit_event: StageFn
    execute_sim_order: StageFn | None = None
    send_email: StageFn | None = None
    score_universe: StageFn | None = None


def _default_deps() -> OrchestratorDeps:
    from shared.accounting.trade_audit_trail import record_event
    from shared.adversarial.bull_bear_debate import debate
    from shared.execution import sim_broker
    from shared.execution.shadow_broker import record_shadow
    from shared.portfolio.constructor import construct
    from shared.portfolio.position_sizer import size_position
    from shared.review.daily_review import run_daily_review as review
    from shared.risk.pre_trade_check import check
    from shared.screening.candidate_pool import build_pool
    from shared.screening.six_dimension_scorer import score_stock, score_universe

    return OrchestratorDeps(
        score_stock=score_stock,
        build_pool=build_pool,
        debate=debate,
        risk_check=check,
        construct=construct,
        size_position=size_position,
        record_shadow=record_shadow,
        run_review=review,
        record_audit_event=record_event,
        execute_sim_order=getattr(
            sim_broker, "execute_sim_order", sim_broker.simulate_order
        ),
        send_email=email_sender.send_email,
        score_universe=score_universe,
    )


def _record_audit(
    deps: OrchestratorDeps,
    stage: str,
    symbol: str,
    *,
    parent_audit_id: str = "",
    payload: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    capital_layer: str = "shadow",
) -> dict[str, Any]:
    payload = dict(payload or {})
    metadata = {"capital_layer": capital_layer, **dict(metadata or {})}
    kwargs: dict[str, Any] = {
        "stage": stage,
        "ts_code": symbol,
        "parent_audit_id": parent_audit_id,
        "metadata": metadata,
    }
    if stage == "signal":
        kwargs["signal_data"] = payload
    elif stage == "decision":
        kwargs["decision_data"] = payload
    elif stage == "risk":
        kwargs["risk_data"] = payload
    elif stage == "execution":
        kwargs["execution_data"] = payload
    elif stage == "result":
        kwargs["result_data"] = payload
    return deps.record_audit_event(**kwargs)


def _stage_error(
    stage: str, exc: Exception, *, capital_layer: str = "shadow"
) -> dict[str, Any]:
    return {
        "stage": stage,
        "status": "degraded",
        "error_type": exc.__class__.__name__,
        "error": str(exc),
        "capital_layer": capital_layer,
    }


def _safe_stage(
    stage: str,
    errors: list[dict[str, Any]],
    func: Callable[[], Any],
    *,
    default: Any,
    capital_layer: str = "shadow",
) -> Any:
    try:
        return func()
    except Exception as exc:
        errors.append(_stage_error(stage, exc, capital_layer=capital_layer))
        return default


def _latest_price(
    reader: Any, market: str, symbol: str, date: str, default: float
) -> float:
    get_intraday = getattr(reader, "get_bars_intraday", None)
    if callable(get_intraday):
        try:
            intraday_rows = get_intraday(market, symbol, "5m", date, date)
        except Exception:
            intraday_rows = []
        for row in reversed(intraday_rows or []):
            price = _safe_float(
                row.get("close", row.get("last_price", row.get("price"))),
                0.0,
            )
            if price > 0:
                return price
    try:
        rows = reader.get_bars_daily(market, symbol, _lookback_start(date), date)
    except Exception:
        return default
    if not rows:
        return default
    return max(_safe_float(rows[-1].get("close"), default), 0.0) or default


def _latest_execution_market_snapshot(
    reader: Any,
    market: str,
    symbol: str,
    date: str,
    side: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return provenance for a real 5-minute execution reference, if available."""
    get_intraday = getattr(reader, "get_bars_intraday", None)
    if not callable(get_intraday):
        return {}
    try:
        rows = get_intraday(market, symbol, "5m", date, date)
    except Exception:
        return {}
    for row in reversed(rows or []):
        if not isinstance(row, dict):
            continue
        price = _safe_float(
            row.get("close", row.get("last_price", row.get("price"))), 0.0
        )
        bar_time = str(row.get("bar_time") or row.get("trade_time") or "").strip()
        bar_volume = _safe_float(
            row.get("volume", row.get("vol", row.get("bar_volume"))), 0.0
        )
        if price <= 0 or not bar_time or bar_volume <= 0:
            continue
        reference_now = (
            now.astimezone(CN_TZ) if now and now.tzinfo else now or datetime.now(CN_TZ)
        )
        if _compact_date_key(date) == reference_now.strftime("%Y%m%d"):
            try:
                parsed_bar_time = datetime.fromisoformat(
                    bar_time.replace("Z", "+00:00")
                )
                if parsed_bar_time.tzinfo is None:
                    parsed_bar_time = parsed_bar_time.replace(tzinfo=CN_TZ)
                age_minutes = (
                    reference_now - parsed_bar_time.astimezone(CN_TZ)
                ).total_seconds() / 60.0
            except ValueError:
                continue
            if age_minutes < -5 or age_minutes > ASHARE_EXECUTION_BAR_MAX_AGE_MINUTES:
                continue
        quote_field = "ask_price" if str(side).lower() == "buy" else "bid_price"
        return {
            quote_field: price,
            "last_price": price,
            "bar_time": bar_time,
            "bar_volume": bar_volume,
            "provider": str(row.get("provider") or "sharedsignals_api_realtime_5min"),
        }
    return {}


def _latest_volatility(
    reader: Any, market: str, symbol: str, date: str, default: float
) -> float:
    try:
        rows = reader.get_bars_daily(market, symbol, _lookback_start(date, 45), date)
    except Exception:
        return default
    closes = [_safe_float(row.get("close"), 0.0) for row in rows[-21:]]
    closes = [close for close in closes if close > 0]
    if len(closes) < 2:
        return default
    returns = [(closes[idx] / closes[idx - 1]) - 1.0 for idx in range(1, len(closes))]
    mean = sum(returns) / len(returns)
    variance = sum((ret - mean) ** 2 for ret in returns) / len(returns)
    return max((variance**0.5) * (252**0.5), 0.01)


def _write_pending_signal(
    card: dict[str, Any], signals_dir: Path = SIGNALS_DIR
) -> dict[str, Any]:
    from shared.execution.signal_state_machine import (
        SignalStateConflict,
        SignalStateMachine,
    )

    # Shadow records are research/paper-tracking signals. Keep their pending lifecycle
    # for review and email de-duplication, but isolate them from executable queues.
    layer = str(card.get("capital_layer") or "").strip().lower()
    direct_execution = bool(card.get("direct_execution"))
    state_root = (
        signals_dir / "shadow"
        if layer == "shadow" and not direct_execution
        else signals_dir
    )
    symbol = str(
        card.get("ts_code") or card.get("symbol") or card.get("code") or ""
    ).strip()
    market = str(card.get("market") or "").strip()
    side = str(card.get("direction") or card.get("side") or "buy").strip()
    date_key = _signal_card_date_key(card, str(card.get("order_id") or ""))
    account = str(card.get("strategy_name") or card.get("account") or "").strip()
    account_type = str(card.get("account_type") or layer or "shadow").strip()
    if layer == "shadow" and symbol and market and date_key:
        existing = _find_existing_sim_signal(
            state_root,
            market=market,
            account=account,
            symbol=symbol,
            date=date_key,
            side=side,
            capital_layer=layer,
            account_type=account_type,
            idempotency_key=str(card.get("idempotency_key") or ""),
        )
        if existing is not None:
            return {
                "order_id": card.get("order_id", ""),
                "status": "duplicate",
                "recorded": False,
                "message": "same-day shadow signal already exists",
                "existing_signal": existing,
                "signal_card": card,
                "queue_scope": "shadow",
            }

    machine = SignalStateMachine(state_root)
    try:
        result = machine.write_pending(card)
        if state_root != signals_dir:
            result["queue_scope"] = "shadow"
        return result
    except SignalStateConflict as exc:
        return {
            "order_id": card.get("order_id", ""),
            "status": "duplicate",
            "recorded": False,
            "message": str(exc),
            "signal_card": card,
            "queue_scope": "shadow" if state_root != signals_dir else "execution",
        }


def _write_execution_signal(
    card: dict[str, Any],
    receipt: dict[str, Any],
    signals_dir: Path = SIGNALS_DIR,
) -> dict[str, Any]:
    from shared.execution.signal_state_machine import (
        SignalStateConflict,
        SignalStateMachine,
    )

    machine = SignalStateMachine(signals_dir)
    status = str(receipt.get("status", "")).strip().lower()
    retryable = bool(receipt.get("retryable")) or status in {
        "pending",
        "queued",
        "retryable",
        "unfilled",
    }
    rejected = status in {
        "rejected",
        "reject",
        "failed",
        "failure",
        "error",
        "cancelled",
        "canceled",
    }
    terminal_filled = status == "filled"
    partial_filled = status == "partial"
    if not (retryable or rejected or terminal_filled or partial_filled):
        rejected = True
        receipt = {
            **receipt,
            "status": "failed",
            "reason": f"unsupported_or_missing_receipt_status:{status or 'missing'}",
            "message": "Simulated execution receipt status is missing or unsupported",
        }
        status = "failed"
    actual_quantity = _safe_int(
        receipt.get("filled_qty", receipt.get("filled_quantity")),
        0,
    )
    actual_price = _safe_float(
        receipt.get("avg_price", receipt.get("filled_price")),
        0.0,
    )
    if (terminal_filled or partial_filled) and (
        actual_quantity <= 0 or actual_price <= 0
    ):
        rejected = True
        terminal_filled = False
        partial_filled = False
        receipt = {
            **receipt,
            "status": "failed",
            "reason": "filled_receipt_missing_actual_price_or_quantity",
            "message": "Simulated fill receipt lacks positive actual quantity or price",
        }
        status = "failed"
    if (
        str(card.get("market") or "").strip().lower() == "ashare"
        and terminal_filled
        and receipt.get("execution_eligible") is not True
    ):
        rejected = True
        terminal_filled = False
        receipt = {
            **receipt,
            "status": "failed",
            "reason": "ashare_fill_not_execution_eligible",
            "message": "A-share fill lacks authoritative execution eligibility",
        }
        status = "failed"
    raw_response = (
        receipt.get("raw_response")
        if isinstance(receipt.get("raw_response"), dict)
        else {}
    )
    existing_signal_path = str(
        receipt.get("signal_path") or raw_response.get("signal_path") or ""
    )

    if retryable and existing_signal_path:
        return {
            "order_id": card.get("order_id", ""),
            "status": "pending",
            "pending_signal": {
                "status": "pending",
                "signal_path": existing_signal_path,
                "signal_card": raw_response.get("signal_card", card),
                "source": "sim_executor",
            },
        }

    webhook_payload = (
        raw_response.get("webhook")
        if isinstance(raw_response.get("webhook"), dict)
        else {}
    )
    mini_webhook_sent = raw_response.get("mode") == "mini_webhook_sent" or bool(
        webhook_payload.get("success")
    )
    if retryable and mini_webhook_sent:
        return {
            "order_id": card.get("order_id", ""),
            "status": "pending",
            "pending_signal": {
                "status": "pending",
                "signal_card": raw_response.get("signal_card", card),
                "source": "mini_webhook",
                "webhook": webhook_payload,
            },
        }

    try:
        pending = machine.write_pending(card)
    except SignalStateConflict as exc:
        if retryable:
            return {
                "order_id": card.get("order_id", ""),
                "status": "pending",
                "pending_signal": {
                    "status": "pending",
                    "signal_card": card,
                    "source": "existing_state_machine_card",
                },
                "message": str(exc),
            }
        return {
            "order_id": card.get("order_id", ""),
            "status": "duplicate",
            "recorded": False,
            "message": str(exc),
            "signal_card": card,
        }

    if retryable:
        return {
            "order_id": card.get("order_id", ""),
            "status": "pending",
            "pending_signal": pending,
        }
    if rejected:
        reason = str(
            receipt.get("message")
            or receipt.get("reason")
            or status
            or "sim order rejected"
        )
        engine_record = (
            raw_response.get("engine_record")
            if isinstance(raw_response.get("engine_record"), dict)
            else {}
        )
        failure_details = {
            "receipt_status": status,
            "receipt_message": str(receipt.get("message") or ""),
            "receipt_reason": str(receipt.get("reason") or ""),
            "filled_qty": receipt.get("filled_qty", receipt.get("filled_quantity", 0)),
            "avg_price": receipt.get("avg_price", receipt.get("filled_price", 0.0)),
            "engine_state": engine_record.get("state"),
            "engine_reason": engine_record.get("reason"),
            "raw_mode": raw_response.get("mode"),
        }
        failed = machine.fail(
            str(card.get("order_id", "")), reason=reason, details=failure_details
        )
        return {
            "order_id": card.get("order_id", ""),
            "status": "failed",
            "pending_signal": pending,
            "failed_signal": failed,
        }

    fill_info = dict(receipt)
    if "filled_quantity" in fill_info and "filled_qty" not in fill_info:
        fill_info["filled_qty"] = fill_info["filled_quantity"]
    if "filled_qty" in fill_info and "filled_quantity" not in fill_info:
        fill_info["filled_quantity"] = fill_info["filled_qty"]
    fill_info["filled_price"] = actual_price
    fill_info["avg_price"] = actual_price
    fill_info["filled_quantity"] = actual_quantity
    fill_info["filled_qty"] = actual_quantity
    claimed = machine.claim(str(card.get("order_id", "")), worker_id="sim_loop")
    running = machine.mark_running(str(card.get("order_id", "")), worker_id="sim_loop")
    filled = machine.fill(
        str(card.get("order_id", "")),
        fill_info,
        partial=partial_filled,
    )
    return {
        "order_id": card.get("order_id", ""),
        "status": "partial" if partial_filled else "filled",
        "pending_signal": pending,
        "claimed_signal": claimed,
        "running_signal": running,
        "filled_signal": filled,
    }


def _make_order_id(prefix: str, market: str, symbol: str, date: str) -> str:
    return f"{prefix}{market}-{symbol}-{date}-{uuid.uuid4().hex[:8]}".replace("/", "-")


def _compact_date_key(value: Any) -> str:
    raw = str(value or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    return raw.replace("-", "")[:8]


def _sim_idempotency_key(
    market: str, account: str, symbol: str, date: str, side: str
) -> str:
    date_key = _compact_date_key(date)
    parts = ("SIM", market.lower(), account, date_key, symbol.upper(), side.lower())
    return ":".join(str(part).replace("/", "-").replace(" ", "_") for part in parts)


MAX_RECOVERABLE_ASHARE_SIM_RETRIES = 2


def _recoverable_ashare_cash_failure(card: dict[str, Any], state: str) -> bool:
    if state != "failed":
        return False
    if str(card.get("market") or "").lower() != "ashare":
        return False
    if str(card.get("capital_layer") or "").lower() != "simulated":
        return False
    details = (
        card.get("failure_details")
        if isinstance(card.get("failure_details"), dict)
        else {}
    )
    if str(details.get("raw_mode") or "") != "server_local_sim_engine":
        return False
    reason = " ".join(
        str(value or "")
        for value in (
            card.get("failure_reason"),
            details.get("receipt_message"),
            details.get("receipt_reason"),
        )
    ).lower()
    return "insufficient cash" in reason or "insufficient_cash" in reason


def _recoverable_retry_context(
    card: dict[str, Any], state: str
) -> dict[str, Any] | None:
    if not _recoverable_ashare_cash_failure(card, state):
        return None
    retry_attempt = _safe_int(card.get("retry_attempt"), 0)
    if retry_attempt >= MAX_RECOVERABLE_ASHARE_SIM_RETRIES:
        return None
    return {
        "retry_of": str(card.get("order_id") or ""),
        "retry_attempt": retry_attempt + 1,
    }


def _shadow_idempotency_key(
    market: str, account: str, symbol: str, date: str, side: str
) -> str:
    date_key = _compact_date_key(date)
    parts = ("SHADOW", market.lower(), account, date_key, symbol.upper(), side.lower())
    return ":".join(str(part).replace("/", "-").replace(" ", "_") for part in parts)


def _signal_card_date_key(card: dict[str, Any], fallback_name: str = "") -> str:
    for key in (
        "trade_date",
        "date",
        "valid_until",
        "timestamp",
        "filled_at",
        "received_at",
        "created_at",
    ):
        value = card.get(key)
        date_key = _compact_date_key(value)
        if len(date_key) == 8 and date_key.isdigit():
            return date_key
    return _compact_date_key(fallback_name)


def _find_existing_sim_signal(
    signals_dir: Path,
    *,
    market: str,
    account: str,
    symbol: str,
    date: str,
    side: str,
    capital_layer: str,
    account_type: str,
    idempotency_key: str,
) -> dict[str, Any] | None:
    target_market = market.lower()
    target_symbol = symbol.upper()
    target_date = _compact_date_key(date)
    target_side = side.lower()
    states = (
        "pending",
        "claimed",
        "running",
        "filled",
        "failed",
        "partial",
        "expired",
        "cancelled",
    )
    for state in states:
        state_dir = signals_dir / state
        if not state_dir.exists():
            continue
        for path in sorted(
            state_dir.glob("*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        ):
            try:
                card = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            retry_context = _recoverable_retry_context(card, state)
            if str(card.get("idempotency_key") or "") == idempotency_key:
                result = {
                    "state": state,
                    "path": str(path),
                    "order_id": card.get("order_id"),
                    "idempotency_key": idempotency_key,
                }
                if retry_context:
                    result.update(retry_context)
                    result["retryable"] = True
                return result
            card_symbol = str(
                card.get("ts_code") or card.get("symbol") or card.get("code") or ""
            ).upper()
            if card_symbol != target_symbol:
                continue
            card_market = str(card.get("market") or "").lower()
            if card_market and card_market != target_market:
                continue
            if not card_market and f"{target_market}-" not in path.name.lower():
                continue
            card_layer = str(card.get("capital_layer") or "").lower()
            if card_layer and card_layer != capital_layer.lower():
                continue
            card_account_type = str(card.get("account_type") or "").lower()
            if card_account_type and card_account_type != account_type.lower():
                continue
            card_side = str(card.get("direction") or card.get("side") or "buy").lower()
            if card_side != target_side:
                continue
            if _signal_card_date_key(card, path.name) != target_date:
                continue
            result = {
                "state": state,
                "path": str(path),
                "order_id": card.get("order_id"),
                "idempotency_key": card.get("idempotency_key"),
                "matched_by": "same_day_symbol_side",
                "account": account,
            }
            if retry_context:
                result.update(retry_context)
                result["retryable"] = True
            return result
    return None


def _build_signal_card(
    *,
    market: str,
    symbol: str,
    account: str,
    date: str,
    order: dict[str, Any],
    risk: dict[str, Any],
    trade: dict[str, Any],
    audit_id: str,
    order_id: str | None = None,
    order_id_prefix: str = "SHADOW-",
    capital_layer: str = "shadow",
    account_type: str = "shadow",
    direct_execution: bool = False,
) -> dict[str, Any]:
    order_id = order_id or _make_order_id(order_id_prefix, market, symbol, date)
    side = str(order.get("side", "buy"))
    if capital_layer == "shadow":
        idempotency_key = order.get("idempotency_key") or _shadow_idempotency_key(
            market, account, symbol, date, side
        )
    else:
        idempotency_key = order.get("idempotency_key") or order_id
    card = {
        "order_id": order_id,
        "ts_code": symbol,
        "market": market,
        "direction": order.get("side", "buy"),
        "quantity": _safe_quantity(order.get("quantity"), 0),
        "price": _safe_float(order.get("price"), 0.0),
        "strategy_name": account,
        "timestamp": _now_iso(),
        "status": "pending",
        "capital_layer": capital_layer,
        "account_type": account_type,
        "manual_confirm_required": False,
        "direct_execution": direct_execution,
        "candidate_pool_layer": str(order.get("candidate_pool_layer") or ""),
        "execution_source": str(order.get("execution_source") or ""),
        "risk_check": {
            "passed": bool(risk.get("approved", False)),
            "adjusted_weight": risk.get("adjusted_weight"),
            "adjustments": risk.get("adjustments", []),
            "reasons": risk.get("reasons", []),
        },
        "shadow_trade_id": trade.get("trade_id", ""),
        "source_audit_id": audit_id,
        "valid_until": _date_iso(date),
        "idempotency_key": idempotency_key,
        "evidence_refs": [audit_id],
    }
    for key in (
        "capital_scope",
        "capital_authority_id",
        "authority_generation",
        "execution_lineage_id",
        "execution_lineage_sha256",
        "point_in_time_as_of",
        "market_capital_required",
        "market_capital_reference_id",
        "market_capital_reservation_id",
        "market_capital_event_id",
        "market_capital_risk_unit_key",
        "market_reserved_gross_cny",
        "real_trading_enabled",
    ):
        if key in order:
            card[key] = order.get(key)
    if order.get("hypothesis_id"):
        card["hypothesis_id"] = order.get("hypothesis_id")
    if isinstance(order.get("research_hypothesis"), dict):
        card["research_hypothesis"] = order.get("research_hypothesis")
    if order.get("retry_of"):
        card["retry_of"] = str(order.get("retry_of"))
        card["retry_attempt"] = _safe_int(order.get("retry_attempt"), 0)
    if str(market).lower() == "ashare":
        sellable_date = _ashare_sellable_date(date, side)
        card["t_plus_1"] = {
            "sellable_from": sellable_date,
            "sellable_date": sellable_date,
        }
    return card


def _send_template_email_now(
    sender: StageFn | None,
    template_name: str,
    data: dict[str, Any],
    *,
    subject: str,
) -> dict[str, Any]:
    if sender is None:
        return {
            "status": "skipped",
            "reason": "email sender unavailable",
            "template": template_name,
        }
    html_body = email_sender.render_template_html(template_name, data)
    channel = email_sender._channel_key_for_template(template_name)
    recipient = email_sender.get_channel(template_name)["to"]
    plain_body = str(data.get("summary") or f"{subject}\n请查看 HTML 邮件内容。")
    result = sender(
        recipient,
        subject,
        plain_body,
        html_body,
        channel=channel,
        rate_limit_type=template_name,
    )
    if isinstance(result, dict):
        return result
    return {"status": "unknown", "template": template_name, "raw_result": result}


def _trading_signal_email_data(
    *,
    market: str,
    symbol: str,
    date: str,
    account: str,
    order: dict[str, Any],
    position: dict[str, Any],
    score: dict[str, Any],
    risk: dict[str, Any],
    card: dict[str, Any],
) -> dict[str, Any]:
    quantity = _safe_quantity(order.get("quantity"), 0)
    price = _safe_float(order.get("price"), 0.0)
    weight = _safe_float(
        position.get("weight"), _safe_float(risk.get("adjusted_weight"), 0.0)
    )
    total_score = score.get(
        "total", score.get("combined", score.get("belief_score", "--"))
    )
    moneyflow_score = score.get("moneyflow", score.get("capital", "--"))
    reasons = risk.get("reasons") if isinstance(risk.get("reasons"), list) else []
    condition = (
        "; ".join(str(item) for item in reasons if item)
        or f"{market} shadow signal generated"
    )
    return {
        "date": _date_iso(date),
        "ts_code": symbol,
        "name": str(score.get("name", "")),
        "current_price": price,
        "trigger_condition": condition,
        "scores": {
            "macro": score.get("macro", "--"),
            "event": score.get("event", "--"),
            "fundamental": score.get("fundamental", "--"),
            "moneyflow": moneyflow_score,
            "capital": moneyflow_score,
            "technical": score.get("technical", "--"),
            "sentiment": score.get("sentiment", "--"),
            "total": total_score,
        },
        "action": order.get("side", "buy"),
        "position_size": {
            "shares": quantity,
            "amount": quantity * price,
            "pct_of_capital": weight * 100 if 0 <= weight <= 1 else weight,
        },
        "capital_layer": "shadow",
        "account_type": "shadow",
        "account": account,
        "order_id": card.get("order_id", ""),
        "summary": f"影子盘新信号: {symbol} {order.get('side', 'buy')} {quantity} @ {price}",
    }


def _trade_receipt_email_data(
    *,
    market: str,
    symbol: str,
    date: str,
    account: str,
    order: dict[str, Any],
    receipt: dict[str, Any],
    card: dict[str, Any],
) -> dict[str, Any]:
    filled_price = _safe_float(
        receipt.get("filled_price"), _safe_float(order.get("price"), 0.0)
    )
    requested_price = _safe_float(order.get("price"), 0.0)
    quantity = _safe_int(
        receipt.get("filled_quantity", receipt.get("filled_qty")),
        _safe_int(order.get("quantity"), 0),
    )
    slippage_pct = 0.0
    if requested_price > 0:
        slippage_pct = ((filled_price / requested_price) - 1.0) * 100
    fill_time = str(receipt.get("fill_time") or receipt.get("filled_at") or _now_iso())
    order_id = str(
        receipt.get("order_id") or card.get("order_id") or order.get("order_id") or ""
    )
    return {
        "date": _date_iso(date),
        "ts_code": symbol,
        "name": "",
        "direction": order.get("side", "buy"),
        "quantity": quantity,
        "filled_price": filled_price,
        "requested_price": requested_price,
        "slippage_pct": slippage_pct,
        "fill_time": fill_time,
        "order_id": order_id,
        "commission": _safe_float(receipt.get("commission"), 0.0),
        "stamp_duty": _safe_float(receipt.get("stamp_duty"), 0.0),
        "transfer_fee": _safe_float(receipt.get("transfer_fee"), 0.0),
        "fee": _safe_float(
            receipt.get("fee"),
            _safe_float(receipt.get("commission"), 0.0)
            + _safe_float(receipt.get("stamp_duty"), 0.0)
            + _safe_float(receipt.get("transfer_fee"), 0.0),
        ),
        "execution_reality_model_version": str(
            receipt.get("execution_reality_model_version") or ""
        ),
        "commission_schedule_status": str(
            receipt.get("commission_schedule_status") or ""
        ),
        "commission_schedule_version": str(
            receipt.get("commission_schedule_version") or ""
        ),
        "channel": "sim",
        "market": market,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "account": account,
        "summary": f"模拟盘成交回执: {symbol} {order.get('side', 'buy')} {quantity} @ {filled_price}",
    }


def _load_shadow_trades_for_date(date: str) -> list[dict[str, Any]]:
    from shared.execution import shadow_broker

    target = _date_iso(date)
    rows: list[dict[str, Any]] = []
    path = shadow_broker.SHADOW_TRADES
    try:
        if not path.exists():
            return rows
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("trade_date") == target:
                    normalized = dict(row)
                    normalized["capital_layer"] = "shadow"
                    rows.append(normalized)
    except Exception:
        return []
    return rows


def _candidate_symbols(
    pool: dict[str, Any],
    fallback_universe: list[str],
    *,
    market: str = "",
    capital_layer: str = "shadow",
) -> list[str]:
    symbols: list[str] = []
    is_ashare_sim = (
        str(market or "").strip().lower() == "ashare"
        and str(capital_layer or "").strip().lower() == "simulated"
    )
    layers = ("candidate",) if is_ashare_sim else ("holdings", "watch", "candidate")
    for layer in layers:
        values = pool.get(layer, []) if isinstance(pool, dict) else []
        if isinstance(values, list):
            symbols.extend(str(item) for item in values if item)
    if not symbols and not is_ashare_sim:
        symbols = [str(item) for item in fallback_universe if item]
    seen: set[str] = set()
    ordered: list[str] = []
    for symbol in symbols:
        if symbol not in seen:
            seen.add(symbol)
            ordered.append(symbol)
    return ordered


def _rank_symbols_by_score(
    symbols: list[str], scores_by_symbol: dict[str, dict[str, Any]]
) -> list[str]:
    indexed = list(enumerate(symbols))

    def score_key(item: tuple[int, str]) -> tuple[float, int]:
        index, symbol = item
        score = scores_by_symbol.get(symbol) or {}
        return (_safe_float(score.get("combined", score.get("score")), 0.0), -index)

    return [symbol for _, symbol in sorted(indexed, key=score_key, reverse=True)]


def _candidate_pool_default(
    market: str, capital_layer: str, symbols: list[str]
) -> dict[str, list[str]]:
    if (
        str(market or "").strip().lower() == "ashare"
        and str(capital_layer or "").strip().lower() == "simulated"
    ):
        return {"candidate": [], "watch": [], "holdings": [], "universe": list(symbols)}
    return {
        "candidate": list(symbols),
        "watch": [],
        "holdings": [],
        "universe": list(symbols),
    }


def _max_new_positions(
    existing_positions: list[dict[str, Any]],
    max_portfolio_positions: int,
) -> int:
    if max_portfolio_positions <= 0:
        return 0
    existing_codes = {
        str(position.get("ts_code") or position.get("symbol") or "").strip()
        for position in existing_positions
        if isinstance(position, dict)
    }
    existing_codes.discard("")
    return max(0, max_portfolio_positions - len(existing_codes))


def _position_value(position: dict[str, Any], capital: float) -> float:
    for key in ("value", "market_value", "amount"):
        value = _safe_float(position.get(key), 0.0)
        if value > 0:
            return value
    weight = _safe_float(position.get("weight"), 0.0)
    if 0 < weight <= 1:
        return weight * capital
    return 0.0


def _ashare_dynamic_capital_plan(
    *,
    market: str,
    date: str,
    capital: float,
    existing_positions: list[dict[str, Any]],
    available_cash: float | None,
    orders: list[dict[str, Any]],
    scores_by_symbol: dict[str, dict[str, Any]],
    skipped_candidates: list[dict[str, Any]],
    risk_rejections: list[dict[str, Any]],
    sample_adjustment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if str(market).lower() != "ashare":
        return {"enabled": False, "market": market}
    try:
        from Ashare.capital_plan import plan_capital
    except Exception as exc:
        return {
            "enabled": False,
            "market": market,
            "status": "degraded",
            "error": str(exc),
        }

    holdings = []
    for position in existing_positions:
        if not isinstance(position, dict):
            continue
        holding = dict(position)
        holding["value"] = _position_value(position, capital)
        holdings.append(holding)

    cash_value = _safe_float(available_cash, -1.0)
    if cash_value < 0:
        cash_value = max(
            0.0,
            capital
            - sum(
                _position_value(position, capital)
                for position in existing_positions
                if isinstance(position, dict)
            ),
        )
    candidates: list[dict[str, Any]] = []
    for order in orders:
        symbol = str(order.get("ts_code") or "")
        score = dict(scores_by_symbol.get(symbol, {}))
        score["ts_code"] = symbol
        score["weight"] = _safe_float(order.get("weight"), 0.0)
        score["belief_score"] = _safe_float(
            order.get("belief_score"), score.get("belief_score", 0.0)
        )
        if order.get("sample_intent"):
            score["sample_intent"] = str(order.get("sample_intent"))
        candidates.append(score)

    sample_context = sample_adjustment if isinstance(sample_adjustment, dict) else {}
    min_strategy_samples = _safe_float(sample_context.get("min_strategy_samples"), 5.0)
    if min_strategy_samples <= 0:
        min_strategy_samples = 5.0
    evolution_decision: dict[str, Any] = {}
    evolution_context: dict[str, Any] = {}
    try:
        from Ashare.evolution_controller import (
            decision_market_context,
            load_latest_decision,
        )

        evolution_decision = load_latest_decision()
        lineage_manifest = local_sim_ledger.get_local_sim_execution_lineage_manifest()
        authority_scope = {
            "capital_authority_id": str(
                lineage_manifest.get("capital_authority_id") or ""
            ),
            "authority_generation": lineage_manifest.get("authority_generation"),
            "execution_lineage_id": str(
                lineage_manifest.get("execution_lineage_id") or ""
            ),
        }
        evolution_context = decision_market_context(
            evolution_decision,
            target_trade_date=date,
            authority_scope=authority_scope,
        )
        if not evolution_context:
            strategy_sample_valid_count = _safe_float(
                sample_context.get("strategy_sample_valid_count"),
                min_strategy_samples,
            )
            evolution_decision = {
                "state": "evidence_pending",
                "recommended_action": "observe_and_label_candidates",
                "reasons": ["evidence_unavailable"],
                "policy": {},
            }
            evolution_context = {
                "strategy_sample_valid_count": strategy_sample_valid_count,
                "min_strategy_samples": min_strategy_samples,
                "evolution_recommended_action": "observe_and_label_candidates",
                "evidence_usable": False,
                "evidence_rejection_reason": "evidence_unavailable",
                "sample_collection_authorized": strategy_sample_valid_count
                < min_strategy_samples,
            }
    except Exception:
        evolution_decision = {
            "state": "evidence_pending",
            "recommended_action": "observe_and_label_candidates",
            "reasons": ["evidence_unavailable"],
            "policy": {},
        }
        evolution_context = {
            "strategy_sample_valid_count": 0.0,
            "min_strategy_samples": min_strategy_samples,
            "evolution_recommended_action": "observe_and_label_candidates",
            "evidence_usable": False,
            "evidence_rejection_reason": "evidence_unavailable",
        }
    relative_exploration_authorized = any(
        str(order.get("sample_intent") or "").lower() == "exploration"
        for order in orders
    )
    relevant_risk_rejections = risk_rejections
    relevant_data_issues = skipped_candidates
    if relative_exploration_authorized:
        # Mature strategy thresholds and unrelated candidates may be lower in
        # exploration mode, but the selected exploration candidate still had
        # to pass the same per-order hard risk and data gates above.
        relevant_risk_rejections = [
            row
            for row in risk_rejections
            if str(row.get("sample_intent") or "").lower() == "exploration"
        ]
        relevant_data_issues = [
            row
            for row in skipped_candidates
            if str(row.get("sample_intent") or "").lower() == "exploration"
        ]
    total_checked = max(
        1,
        len(orders) + len(relevant_data_issues) + len(relevant_risk_rejections),
    )
    market_context = {
        "risk_rejection_rate": len(relevant_risk_rejections) / total_checked,
        "data_issue_rate": len(relevant_data_issues) / total_checked,
        "strategy_sample_valid_count": _safe_float(
            sample_context.get("strategy_sample_valid_count"),
            min_strategy_samples,
        ),
        "min_strategy_samples": min_strategy_samples,
        "relative_exploration_authorized": relative_exploration_authorized,
        "relative_exploration_selection_method": "relative_rank_top_quantile",
        "existing_exploration_new_positions": _safe_int(
            sample_context.get("existing_exploration_new_positions"), 0
        ),
        "exploration_daily_realized_pnl_cny": _safe_float(
            sample_context.get("exploration_daily_realized_pnl_cny"), 0.0
        ),
        "exploration_daily_loss_cny": _safe_float(
            sample_context.get("exploration_daily_loss_cny"), 0.0
        ),
    }
    market_context.update(evolution_context)
    plan = plan_capital(
        holdings,
        cash_value,
        candidates=candidates,
        dynamic=True,
        total_capital=capital,
        market_context=market_context,
    ).to_dict()
    plan["enabled"] = True
    plan["market"] = market
    plan["existing_position_count"] = len(
        {
            _position_symbol(position)
            for position in existing_positions
            if isinstance(position, dict) and _position_symbol(position)
        }
    )
    if evolution_decision:
        plan["evolution_decision"] = {
            "state": evolution_decision.get("state"),
            "recommended_action": evolution_decision.get("recommended_action"),
            "reasons": evolution_decision.get("reasons", []),
            "policy": evolution_decision.get("policy", {}),
            "evidence_usable": evolution_context.get("evidence_usable", False),
            "evidence_rejection_reason": evolution_context.get(
                "evidence_rejection_reason", ""
            ),
        }
    plan["cash_source"] = (
        "account_snapshot"
        if available_cash is not None and _safe_float(available_cash, -1.0) >= 0
        else "capital_minus_positions"
    )
    return plan


def _apply_position_budgets(
    *,
    market: str,
    portfolio: dict[str, Any],
    order_meta: dict[str, dict[str, Any]],
    capital_plan: dict[str, Any],
    capital: float,
) -> None:
    if str(market).lower() != "ashare" or not capital_plan.get("enabled"):
        return
    budgets = capital_plan.get("position_budget_by_symbol")
    if not isinstance(budgets, dict) or not budgets:
        return
    for position in portfolio.get("positions", []) or []:
        if not isinstance(position, dict):
            continue
        symbol = str(position.get("ts_code") or "")
        budget = _safe_float(budgets.get(symbol), 0.0)
        if budget <= 0:
            continue
        meta = order_meta.get(symbol, {})
        price = _safe_float(position.get("price"), _safe_float(meta.get("price"), 0.0))
        if price <= 0:
            continue
        requested_budget = budget
        risk_weight_cap = min(0.15, max(0.0, _safe_float(meta.get("weight"), 0.0)))
        risk_capped_budget = min(requested_budget, capital * risk_weight_cap)
        shares = int(risk_capped_budget // price)
        shares = (shares // 100) * 100
        worst_case_gross = 0.0
        while shares > 0:
            reservation_order = {
                **meta,
                **position,
                "ts_code": symbol,
                "price": price,
                "quantity": shares,
            }
            worst_case_gross = _estimate_ashare_market_reservation(reservation_order)
            if 0 < worst_case_gross <= risk_capped_budget + 1e-9:
                break
            shares -= 100
            worst_case_gross = 0.0
        position["requested_budget"] = round(requested_budget, 2)
        position["risk_capped_budget"] = round(risk_capped_budget, 2)
        position["worst_case_gross_cny"] = round(worst_case_gross, 2)
        position["budget_cap_reason"] = (
            "risk_adjusted_weight_cap"
            if risk_capped_budget < requested_budget
            else "capital_plan_budget"
        )
        if shares <= 0:
            position["shares"] = 0
            position["amount"] = 0.0
            position["weight"] = 0.0
            position["target_amount"] = round(requested_budget, 2)
            continue
        amount = shares * price
        position["shares"] = shares
        position["amount"] = round(amount, 2)
        position["weight"] = round(amount / max(capital, 1.0), 6)
        position["target_amount"] = round(requested_budget, 2)


def _ashare_post_sell_buy_capacity(
    *,
    market: str,
    existing_positions: list[dict[str, Any]],
    capital_plan: dict[str, Any],
    rebalance: dict[str, Any],
    max_portfolio_positions: int,
) -> int:
    if str(market).lower() != "ashare" or max_portfolio_positions <= 0:
        return 0
    target_positions = _safe_int(
        capital_plan.get("target_positions"), max_portfolio_positions
    )
    if target_positions <= 0:
        return 0
    target_positions = min(target_positions, max_portfolio_positions)
    planned_sell_symbols = {
        str(row.get("ts_code") or "")
        for row in (rebalance.get("sells", []) or [])
        if isinstance(row, dict) and str(row.get("ts_code") or "")
    }
    if not planned_sell_symbols:
        return 0
    existing_symbols = {
        _position_symbol(position)
        for position in existing_positions
        if isinstance(position, dict) and _position_symbol(position)
    }
    post_sell_count = len(existing_symbols - planned_sell_symbols)
    return max(0, target_positions - post_sell_count)


def _augment_ashare_replacement_budgets(
    *,
    market: str,
    capital_plan: dict[str, Any],
    rebalance: dict[str, Any],
    orders_for_portfolio: list[dict[str, Any]],
    replacement_capacity: int,
    capital: float,
) -> dict[str, Any]:
    if (
        str(market).lower() != "ashare"
        or replacement_capacity <= 0
        or not capital_plan.get("enabled")
    ):
        return capital_plan
    released_cash = sum(
        _safe_float(row.get("amount"), 0.0)
        for row in (rebalance.get("sells", []) or [])
        if isinstance(row, dict)
    )
    if released_cash <= 0:
        return capital_plan
    try:
        from Ashare.capital_plan import MAX_POSITION_VALUE, MIN_POSITION_VALUE
    except Exception:
        MAX_POSITION_VALUE = 70_000
        MIN_POSITION_VALUE = 50_000
    if released_cash < MIN_POSITION_VALUE:
        return capital_plan

    budgets = dict(capital_plan.get("position_budget_by_symbol") or {})
    candidates = [
        order
        for order in orders_for_portfolio[:replacement_capacity]
        if str(order.get("ts_code") or "")
        and _safe_float(budgets.get(str(order.get("ts_code") or "")), 0.0) <= 0
    ]
    if not candidates:
        return capital_plan

    max_single_pct = _safe_float(capital_plan.get("max_single_position_pct"), 0.0)
    max_single_value = MAX_POSITION_VALUE
    if max_single_pct > 0:
        max_single_value = min(MAX_POSITION_VALUE, max(0.0, capital * max_single_pct))
    remaining = released_cash
    allocated: list[dict[str, Any]] = []
    for index, order in enumerate(candidates):
        if remaining < MIN_POSITION_VALUE:
            break
        remaining_slots = max(1, min(len(candidates), replacement_capacity) - index)
        budget = min(remaining / remaining_slots, max_single_value)
        budget = max(MIN_POSITION_VALUE, min(budget, remaining))
        if budget < MIN_POSITION_VALUE:
            break
        symbol = str(order.get("ts_code") or "")
        budgets[symbol] = round(budget, 2)
        allocated.append({"ts_code": symbol, "budget": round(budget, 2)})
        remaining -= budget

    if not allocated:
        return capital_plan
    updated = dict(capital_plan)
    updated["position_budget_by_symbol"] = budgets
    updated["replacement_budget"] = {
        "enabled": True,
        "released_cash": round(released_cash, 2),
        "allocated_cash": round(sum(row["budget"] for row in allocated), 2),
        "replacement_capacity": replacement_capacity,
        "allocations": allocated,
    }
    updated["max_new_positions"] = max(
        _safe_int(updated.get("max_new_positions"), 0), len(allocated)
    )
    return updated


def _position_symbol(position: dict[str, Any]) -> str:
    return str(
        position.get("ts_code") or position.get("symbol") or position.get("code") or ""
    ).strip()


def _position_quantity(position: dict[str, Any]) -> int:
    return _safe_int(
        position.get("quantity", position.get("shares", position.get("position_qty"))),
        0,
    )


def _position_sellable_quantity(position: dict[str, Any]) -> int:
    explicit = position.get(
        "sellable_quantity", position.get("sellable_qty", position.get("available_qty"))
    )
    if explicit is not None:
        return max(0, _safe_int(explicit, 0))
    return max(0, _position_quantity(position))


def _position_avg_price(position: dict[str, Any]) -> float:
    return _safe_float(
        position.get("avg_price", position.get("avg_cost", position.get("cost"))), 0.0
    )


def _position_last_price(position: dict[str, Any], fallback: float = 0.0) -> float:
    return _safe_float(
        position.get(
            "last_price",
            position.get(
                "mark_price", position.get("current_price", position.get("price"))
            ),
        ),
        fallback,
    )


def _merge_ashare_sell_row(base: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    merged["shares"] = _safe_int(merged.get("shares"), 0) + _safe_int(
        row.get("shares"), 0
    )
    merged["quantity"] = _safe_int(merged.get("quantity"), 0) + _safe_int(
        row.get("quantity"), 0
    )
    merged["amount"] = round(
        _safe_float(merged.get("amount"), 0.0) + _safe_float(row.get("amount"), 0.0), 2
    )
    if merged["shares"] > 0 and merged["amount"] > 0:
        merged["price"] = round(merged["amount"] / merged["shares"], 4)
    merged["weight"] = -abs(_safe_float(merged.get("weight"), 0.0)) - abs(
        _safe_float(row.get("weight"), 0.0)
    )
    reasons: list[str] = []
    for reason in [
        *(merged.get("rebalance_reasons") or []),
        *(row.get("rebalance_reasons") or []),
    ]:
        if reason and reason not in reasons:
            reasons.append(str(reason))
    merged["rebalance_reasons"] = reasons
    merged["reason"] = ",".join(reasons)
    merged["has_score"] = bool(merged.get("has_score") or row.get("has_score"))
    merged["combined"] = min(
        _safe_float(merged.get("combined"), 0.0), _safe_float(row.get("combined"), 0.0)
    )
    merged["pnl_pct"] = min(
        _safe_float(merged.get("pnl_pct"), 0.0), _safe_float(row.get("pnl_pct"), 0.0)
    )
    return merged


def _ashare_best_replacement_candidate(
    *,
    symbol: str,
    buy_candidates: list[dict[str, Any]],
    scores_by_symbol: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    best: dict[str, Any] = {}
    best_score = 0.0
    for candidate in buy_candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_symbol = str(
            candidate.get("ts_code") or candidate.get("symbol") or ""
        ).strip()
        if not candidate_symbol or candidate_symbol == symbol:
            continue
        score = scores_by_symbol.get(candidate_symbol) or {}
        combined = _safe_float(
            score.get(
                "combined",
                score.get("score", candidate.get("combined", candidate.get("score"))),
            ),
            0.0,
        )
        if combined > best_score:
            best_score = combined
            best = {"ts_code": candidate_symbol, "combined": combined}
    return best


def _read_json_dict(path: Path | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _sample_kpi_evidence_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Return diagnostics from the current SampleJournal KPI projection.

    The projection is deliberately informational here.  It cannot change
    entry thresholds, promote a style, or expand risk; those decisions remain
    manual-only while the capital-growth simulation gathers evidence.
    """

    if not payload:
        return {
            "status": "missing_current_sample_kpi",
            "source": "sample_journal_kpi",
            "automatic_promotion_enabled": False,
            "automatic_risk_expansion_enabled": False,
        }

    authority = (
        payload.get("authority_scope")
        if isinstance(payload.get("authority_scope"), dict)
        else {}
    )
    source_valid = (
        payload.get("report_type") == "sample_journal_kpi"
        and payload.get("evidence_source") == "sample_journal_kpi"
        and payload.get("real_trading_enabled") is not True
        and payload.get("live_execution_enabled") is not True
        and authority.get("capital_authority_id") == "ashare-capital-v1"
        and authority.get("authority_generation") == 1
        and bool(str(authority.get("execution_lineage_id") or "").strip())
    )
    if not source_valid:
        return {
            "status": "invalid_current_sample_kpi",
            "source": "sample_journal_kpi",
            "automatic_promotion_enabled": False,
            "automatic_risk_expansion_enabled": False,
        }

    layer_totals = (
        payload.get("sample_layer_totals")
        if isinstance(payload.get("sample_layer_totals"), dict)
        else {}
    )
    styles = payload.get("styles") if isinstance(payload.get("styles"), dict) else {}
    prediction_count = 0
    ready_forward_label_count = 0
    for style in styles.values():
        if not isinstance(style, dict):
            continue
        prediction_count += _safe_int(style.get("prediction_count"), 0)
        label_counts = (
            style.get("forward_label_counts")
            if isinstance(style.get("forward_label_counts"), dict)
            else {}
        )
        for statuses in label_counts.values():
            if not isinstance(statuses, dict):
                continue
            ready_forward_label_count += _safe_int(statuses.get("ready"), 0)
            ready_forward_label_count += _safe_int(statuses.get("labeled"), 0)

    scientific = (
        payload.get("scientific_evidence")
        if isinstance(payload.get("scientific_evidence"), dict)
        else {}
    )
    return {
        "status": "available",
        "source": "sample_journal_kpi",
        "trade_date": str(payload.get("trade_date") or ""),
        "authority_scope": dict(authority),
        "prediction_count": prediction_count,
        "observation_counterfactual_count": _safe_int(
            layer_totals.get("observation_counterfactual"), 0
        ),
        "exploration_fill_count": _safe_int(layer_totals.get("exploration_fill"), 0),
        "exploitation_fill_count": _safe_int(layer_totals.get("exploitation_fill"), 0),
        "completed_round_trip_count": _safe_int(
            layer_totals.get("completed_round_trip"), 0
        ),
        "ready_forward_label_count": ready_forward_label_count,
        "promotion_evidence_ready": scientific.get("promotion_evidence_ready") is True,
        "ignored_unsafe_policy_claims": bool(
            payload.get("automatic_promotion_enabled") is True
            or payload.get("automatic_risk_expansion_enabled") is True
        ),
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
    }


def _ashare_opportunity_cost_thresholds(
    *,
    market: str,
    date: str,
    min_entry_score: float,
    min_score_gap: float,
    existing_positions: list[dict[str, Any]],
    scores_by_symbol: dict[str, dict[str, Any]],
    sample_kpi_path: Path | None = None,
    local_trades_path: Path | None = None,
    lookback_days: int = 30,
) -> dict[str, Any]:
    """Return dynamic opportunity-cost thresholds for A-share rebalancing.

    The hard floor for min_score_gap is preserved (0.12).  Recent forward-
    Only current holding quality can widen the required gap.  SampleJournal KPI
    evidence is attached for diagnostics but cannot auto-tighten the strategy.
    ``local_trades_path`` remains a no-op compatibility argument so retired
    local-trade sample scoring cannot silently regain policy authority.
    """
    if str(market).lower() != "ashare":
        return {
            "enabled": False,
            "market": market,
            "min_entry_score": min_entry_score,
            "min_score_gap": min_score_gap,
            "action": "disabled",
            "reasons": ["non_ashare_market"],
        }

    effective_min_entry_score = min_entry_score
    effective_min_score_gap = min_score_gap
    reasons: list[str] = []

    del local_trades_path
    sample_kpi = _read_json_dict(
        sample_kpi_path
        or (ROOT / "shared" / "review" / "ashare" / "sample_kpi_latest.json")
    )
    sample_evidence_summary = _sample_kpi_evidence_summary(sample_kpi)

    adjustment = 0.0

    # Quality of existing holdings.
    position_scores: list[float] = []
    for position in existing_positions:
        if not isinstance(position, dict):
            continue
        symbol = _position_symbol(position)
        score = scores_by_symbol.get(symbol) or {}
        combined = _safe_float(
            score.get(
                "combined",
                score.get("score", position.get("combined", position.get("score"))),
            ),
            0.0,
        )
        if combined > 0:
            position_scores.append(combined)
    avg_position_score = (
        sum(position_scores) / len(position_scores) if position_scores else 0.0
    )

    if 0 < avg_position_score < 0.60:
        adjustment += 0.04
        reasons.append("low_position_score")
    elif 0 < avg_position_score < 0.65:
        adjustment += 0.03
        reasons.append("low_position_score")
    elif 0 < avg_position_score < 0.70:
        adjustment += 0.02
        reasons.append("low_position_score")
    elif 0 < avg_position_score < 0.75:
        adjustment += 0.01
        reasons.append("moderate_position_score")

    # Cap the widening to avoid over-engineering; hard floor is preserved below.
    adjustment = min(adjustment, 0.10)
    effective_min_score_gap = round(min_score_gap + adjustment, 4)

    if adjustment >= 0.08:
        action = "paused_opportunity_cost"
    elif adjustment > 0:
        action = "widened_gap"
    else:
        action = "standard_gap"

    return {
        "enabled": True,
        "market": market,
        "min_entry_score": effective_min_entry_score,
        "min_score_gap": effective_min_score_gap,
        "base_min_score_gap": min_score_gap,
        "gap_adjustment": round(adjustment, 4),
        "action": action,
        "reasons": reasons,
        "lookback_days": lookback_days,
        "average_position_score": round(avg_position_score, 4),
        "sample_evidence_summary": sample_evidence_summary,
    }


def _ashare_rebalance_plan(
    *,
    market: str,
    date: str,
    reader: Any,
    existing_positions: list[dict[str, Any]],
    capital_plan: dict[str, Any],
    scores_by_symbol: dict[str, dict[str, Any]],
    max_portfolio_positions: int,
    default_price: float,
    capital: float,
    buy_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if str(market).lower() != "ashare":
        return {"enabled": False, "planned_sell_count": 0, "sells": []}

    dynamic_thresholds = _ashare_opportunity_cost_thresholds(
        market=market,
        date=date,
        min_entry_score=ASHARE_OPPORTUNITY_COST_MIN_ENTRY_SCORE,
        min_score_gap=ASHARE_OPPORTUNITY_COST_MIN_SCORE_GAP,
        existing_positions=existing_positions,
        scores_by_symbol=scores_by_symbol,
    )
    effective_min_entry_score = _safe_float(
        dynamic_thresholds.get("min_entry_score"),
        ASHARE_OPPORTUNITY_COST_MIN_ENTRY_SCORE,
    )
    effective_min_score_gap = _safe_float(
        dynamic_thresholds.get("min_score_gap"), ASHARE_OPPORTUNITY_COST_MIN_SCORE_GAP
    )
    opportunity_cost_paused = (
        dynamic_thresholds.get("action") == "paused_opportunity_cost"
    )

    target_positions = _safe_int(
        capital_plan.get("target_positions"), max_portfolio_positions
    )
    if target_positions < 0:
        target_positions = max_portfolio_positions
    sellable: list[dict[str, Any]] = []
    planned_by_symbol: dict[str, dict[str, Any]] = {}
    candidates_for_replacement = list(buy_candidates or [])
    existing_count = len(
        {
            _position_symbol(position)
            for position in existing_positions
            if isinstance(position, dict) and _position_symbol(position)
        }
    )
    # Defensive mode (target_positions=0) must not allow opportunity_cost
    # rebalancing.  Stop-loss and score_drop risk sells are preserved because
    # they are evaluated before the opportunity_cost gate below.
    effective_target = (
        0 if target_positions == 0 else min(target_positions, max_portfolio_positions)
    )

    for position in existing_positions:
        if not isinstance(position, dict):
            continue
        symbol = _position_symbol(position)
        if not symbol:
            continue
        quantity = _position_quantity(position)
        sellable_quantity = _position_sellable_quantity(position)
        if quantity <= 0 or sellable_quantity <= 0:
            continue
        score = scores_by_symbol.get(symbol) or {}
        has_score = bool(score)
        combined = _safe_float(
            score.get(
                "combined",
                score.get("score", position.get("combined", position.get("score"))),
            ),
            0.0,
        )
        avg_price = _position_avg_price(position)
        price = _position_last_price(position, avg_price or default_price)
        mapped_market = str(score.get("market") or market)
        mapped_symbol = str(score.get("mapped_symbol") or symbol)
        price = _latest_price(
            reader, mapped_market, mapped_symbol, date, price or default_price
        )
        pnl_pct = ((price / avg_price) - 1.0) if avg_price > 0 and price > 0 else 0.0
        reasons: list[str] = []
        if pnl_pct <= -0.08:
            reasons.append("stop_loss")
        if has_score and combined < 0.55:
            reasons.append("score_drop")
        opportunity_candidate = _ashare_best_replacement_candidate(
            symbol=symbol,
            buy_candidates=candidates_for_replacement,
            scores_by_symbol=scores_by_symbol,
        )
        opportunity_score = _safe_float(opportunity_candidate.get("combined"), 0.0)
        opportunity_gap = opportunity_score - combined
        if (
            not opportunity_cost_paused
            and has_score
            and not reasons
            and effective_target > 0
            and existing_count >= effective_target
            and opportunity_score >= effective_min_entry_score
            and opportunity_gap >= effective_min_score_gap
        ):
            reasons.append("opportunity_cost")
        sellable.append(
            {
                "ts_code": symbol,
                "side": "sell",
                "shares": (sellable_quantity // 100) * 100,
                "quantity": quantity,
                "price": price,
                "weight": -abs(
                    _safe_float(
                        position.get("weight"),
                        _position_value(position, capital) / max(capital, 1.0),
                    )
                ),
                "amount": round(((sellable_quantity // 100) * 100) * price, 2),
                "sector": str(position.get("sector", "unknown")),
                "reason": ",".join(reasons) if reasons else "",
                "rebalance_reasons": reasons,
                "combined": combined,
                "has_score": has_score,
                "pnl_pct": pnl_pct,
                "opportunity_cost": {
                    "candidate": opportunity_candidate.get("ts_code", ""),
                    "candidate_score": round(opportunity_score, 4),
                    "score_gap": round(opportunity_gap, 4),
                    "min_score_gap": effective_min_score_gap,
                    "base_min_score_gap": ASHARE_OPPORTUNITY_COST_MIN_SCORE_GAP,
                    "action": dynamic_thresholds.get("action"),
                }
                if "opportunity_cost" in reasons
                else {},
                "risk_audit_id": "",
            }
        )

    sellable_by_symbol: dict[str, dict[str, Any]] = {}
    for row in sellable:
        if row["shares"] <= 0:
            continue
        symbol = str(row["ts_code"])
        if symbol in sellable_by_symbol:
            sellable_by_symbol[symbol] = _merge_ashare_sell_row(
                sellable_by_symbol[symbol], row
            )
        else:
            sellable_by_symbol[symbol] = dict(row)

    for row in sellable_by_symbol.values():
        if row["rebalance_reasons"]:
            planned_by_symbol[row["ts_code"]] = row

    compression_target = (
        target_positions if target_positions > 0 else max_portfolio_positions
    )
    excess_count = max(0, existing_count - compression_target)
    if excess_count > 0:
        compression_pool = [
            row
            for row in sellable_by_symbol.values()
            if row["shares"] > 0 and row["ts_code"] not in planned_by_symbol
        ]
        compression_pool.sort(
            key=lambda row: (
                _safe_float(row.get("combined"), 0.0) if row.get("has_score") else -1.0,
                _safe_float(row.get("pnl_pct"), 0.0),
                _safe_float(row.get("amount"), 0.0),
            )
        )
        for row in compression_pool[:excess_count]:
            reasons = list(row.get("rebalance_reasons") or [])
            reasons.append("portfolio_compression")
            row["rebalance_reasons"] = reasons
            row["reason"] = ",".join(reasons)
            planned_by_symbol[row["ts_code"]] = row

    sells = list(planned_by_symbol.values())
    sells.sort(
        key=lambda row: (
            str(row.get("reason") or ""),
            _safe_float(row.get("combined"), 0.0),
        )
    )
    return {
        "enabled": True,
        "target_positions": target_positions,
        "existing_position_count": existing_count,
        "planned_sell_count": len(sells),
        "sells": sells,
        "dynamic_thresholds": dynamic_thresholds,
    }


def _write_ashare_capital_plan_log(
    *,
    market: str,
    date: str,
    account: str,
    capital_plan: dict[str, Any],
    rebalance: dict[str, Any],
    planned_buy_count: int,
    capital_layer: str,
    account_type: str,
    review_root: Path | None = None,
) -> dict[str, Any]:
    if str(market).lower() != "ashare":
        return {"status": "skipped", "reason": "non_ashare_market"}
    base_review_root = review_root or (ROOT / "shared" / "review")
    target_dir = base_review_root / "ashare"
    target_dir.mkdir(parents=True, exist_ok=True)
    compact = str(date or "").replace("-", "")[:8]
    path = target_dir / f"capital_plan_{compact}.jsonl"
    row = {
        "market": market,
        "date": date,
        "trade_date": _date_iso(date),
        "account": account,
        "capital_layer": capital_layer,
        "account_type": account_type,
        "capital_plan": capital_plan,
        "rebalance": {
            "enabled": bool(rebalance.get("enabled")),
            "target_positions": rebalance.get("target_positions"),
            "existing_position_count": rebalance.get("existing_position_count", 0),
            "planned_sell_count": rebalance.get("planned_sell_count", 0),
            "sells": rebalance.get("sells", [])[:20],
        },
        "planned_buy_count": planned_buy_count,
        "generated_at": _now_iso(),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return {"status": "written", "path": str(path), "rows": 1}


def _write_ashare_post_execution_capital_plan_refresh(
    *,
    market: str,
    date: str,
    account: str,
    capital_plan: dict[str, Any],
    position_authority: dict[str, Any],
    capital_layer: str,
    account_type: str,
    position_change_count: int,
    review_root: Path | None = None,
) -> dict[str, Any]:
    if str(market).lower() != "ashare":
        return {"status": "skipped", "reason": "non_ashare_market"}
    if position_change_count <= 0:
        return {"status": "skipped", "reason": "no_position_changes"}
    if position_authority.get("status") != "verified":
        return {
            "status": "blocked",
            "reason": str(
                position_authority.get("reason")
                or "ashare_post_execution_position_authority_invalid"
            ),
            "source_audit": position_authority.get("source_audit", []),
            "position_source_mismatches": position_authority.get("mismatches", []),
        }
    cash = _strict_finite_number(position_authority.get("capital_cash_available"))
    position_count = position_authority.get("position_count")
    if (
        cash is None
        or cash < 0.0
        or isinstance(position_count, bool)
        or not isinstance(position_count, int)
        or position_count < 0
    ):
        return {
            "status": "blocked",
            "reason": "ashare_post_execution_position_authority_incomplete",
            "source_audit": position_authority.get("source_audit", []),
        }
    sample_adjustment = capital_plan.get("sample_adjustment", {})
    refreshed_plan = {
        "enabled": True,
        "refresh_phase": "post_execution",
        "refresh_reason": "executed_position_changes",
        "available_cash": round(cash, 2),
        "cash_source": "market_capital_authority_post_execution",
        "capital_authority_checksum": position_authority.get("authority_checksum"),
        "positions_fingerprint": position_authority.get("positions_fingerprint"),
        "existing_position_count": position_count,
        "target_positions": capital_plan.get("target_positions", position_count),
        "max_new_positions": 0,
        "risk_mode": "post_execution_observation",
        "position_budget_by_symbol": {},
        "suggested_buys": [],
        "reasons": ["post_execution_refresh"],
        "notes": [
            "latest row reflects account state after simulated fills; it does not create new orders"
        ],
    }
    if sample_adjustment:
        refreshed_plan["sample_adjustment"] = sample_adjustment
    return _write_ashare_capital_plan_log(
        market=market,
        date=date,
        account=account,
        capital_plan=refreshed_plan,
        rebalance={
            "enabled": True,
            "target_positions": refreshed_plan["target_positions"],
            "existing_position_count": position_count,
            "planned_sell_count": 0,
            "sells": [],
        },
        planned_buy_count=0,
        capital_layer=capital_layer,
        account_type=account_type,
        review_root=review_root,
    )


def _exclusion_rows(
    rows: list[dict[str, Any]],
    *,
    kind: str,
    market: str,
    date: str,
    account: str,
    capital_layer: str,
    account_type: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        payload = dict(row)
        payload.update(
            {
                "kind": kind,
                "market": market,
                "date": date,
                "trade_date": _date_iso(date),
                "account": account,
                "capital_layer": capital_layer,
                "account_type": account_type,
                "generated_at": _now_iso(),
            }
        )
        result.append(payload)
    return result


def _write_sim_execution_exclusions(
    *,
    market: str,
    date: str,
    account: str,
    skipped_candidates: list[dict[str, Any]],
    risk_rejections: list[dict[str, Any]],
    execution_skips: list[dict[str, Any]],
    capital_layer: str,
    account_type: str,
    review_root: Path | None = None,
) -> dict[str, Any]:
    rows = [
        *_exclusion_rows(
            skipped_candidates,
            kind="skipped_candidate",
            market=market,
            date=date,
            account=account,
            capital_layer=capital_layer,
            account_type=account_type,
        ),
        *_exclusion_rows(
            risk_rejections,
            kind="risk_rejection",
            market=market,
            date=date,
            account=account,
            capital_layer=capital_layer,
            account_type=account_type,
        ),
        *_exclusion_rows(
            execution_skips,
            kind="execution_skip",
            market=market,
            date=date,
            account=account,
            capital_layer=capital_layer,
            account_type=account_type,
        ),
    ]
    if not rows:
        return {"status": "empty", "rows": 0}
    base_review_root = review_root or (ROOT / "shared" / "review")
    target_dir = base_review_root / str(market or "unknown").lower()
    target_dir.mkdir(parents=True, exist_ok=True)
    compact = str(date or "").replace("-", "")[:8]
    path = target_dir / f"execution_exclusions_{compact}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return {"status": "written", "path": str(path), "rows": len(rows)}


def _account_name(account: Any, default: str) -> str:
    if isinstance(account, dict):
        for key in ("account", "account_id", "account_name", "name", "strategy_name"):
            value = str(account.get(key, "")).strip()
            if value:
                return value
        return default
    value = str(account or "").strip()
    return value or default


def _load_sim_account_for_trade_date(
    getter: Callable[..., Any],
    trade_date: str,
    *,
    position_authority: dict[str, Any] | None = None,
) -> Any:
    """Pass pre-read authority inputs only to adapters that support them."""

    try:
        signature = inspect.signature(getter)
    except (TypeError, ValueError):
        return getter()
    kwargs: dict[str, Any] = {}
    if "trade_date" in signature.parameters:
        kwargs["trade_date"] = trade_date
    if "position_authority" in signature.parameters:
        kwargs["position_authority"] = position_authority
    return getter(**kwargs)


def _account_positions(account: Any, config: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[Any] = []
    if isinstance(account, dict):
        sources.extend([account.get("positions"), account.get("holdings")])
    sources.extend([config.get("sim_positions"), config.get("positions")])
    positions: list[dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, list):
            continue
        for row in source:
            if isinstance(row, dict):
                normalized = dict(row)
                normalized["capital_layer"] = "simulated"
                normalized.setdefault("account_type", "simulated")
                positions.append(normalized)
    return positions


def _default_capital_for_market(market: Any) -> float:
    from shared.markets.sim_capital import default_sim_capital

    return default_sim_capital(str(market or "ashare"))


def _account_capital(account: Any, config: dict[str, Any]) -> float:
    for source in (account if isinstance(account, dict) else {}, config):
        if not isinstance(source, dict):
            continue
        for key in (
            "sim_capital",
            "cash",
            "available_cash",
            "equity",
            "net_liquidation",
            "shadow_capital",
        ):
            value = _safe_float(source.get(key), -1.0)
            if value >= 0:
                return value
    return _default_capital_for_market(config.get("market"))


def _account_available_cash(
    account: Any,
    config: dict[str, Any],
    capital: float,
    existing_positions: list[dict[str, Any]],
) -> float:
    for source in (account if isinstance(account, dict) else {}, config):
        if not isinstance(source, dict):
            continue
        for key in ("cash_available", "available_cash", "cash"):
            value = _safe_float(source.get(key), -1.0)
            if value >= 0:
                return value
    return max(
        0.0,
        capital
        - sum(
            _position_value(position, capital)
            for position in existing_positions
            if isinstance(position, dict)
        ),
    )


def _ashare_authoritative_account_view(
    account: Any,
    trade_date: str,
    *,
    position_authority: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load the one server-local A-share strategy account used for planning."""

    from shared.execution.execution_lineage import (
        ASHARE_AUTHORITY_GENERATION,
        ASHARE_CAPITAL_AUTHORITY_ID,
        ASHARE_EXECUTION_LINEAGE_ID,
    )

    account_name = _account_name(account, "ashare_sim")
    if account_name != "ashare_sim":
        raise RuntimeError("ashare_authoritative_account_must_be_ashare_sim")
    if position_authority is None:
        raw_capital_state = market_capital.load_market_capital_provider_state(
            "ashare", trade_date
        )
        position_authority = build_ashare_capital_position_authority_view(
            raw_capital_state, trade_date
        )
    if position_authority.get("status") != "verified":
        raise RuntimeError("ashare_capital_position_authority_invalid")
    snapshot = local_sim_ledger.get_local_sim_account_snapshot(
        account_name,
        trade_date=trade_date,
        starting_cash=50_000.0,
        position_authority=position_authority,
    )
    pnl = local_sim_ledger.get_local_sim_pnl(
        account_name,
        trade_date=trade_date,
        position_authority=position_authority,
    )
    expected_authority = {
        "capital_authority_id": ASHARE_CAPITAL_AUTHORITY_ID,
        "authority_generation": ASHARE_AUTHORITY_GENERATION,
        "execution_lineage_id": ASHARE_EXECUTION_LINEAGE_ID,
    }
    for source_name, payload in (("snapshot", snapshot), ("pnl", pnl)):
        if not isinstance(payload, dict) or payload.get("status") != "ready":
            raise RuntimeError("ashare_fresh_execution_lineage_unavailable")
        if payload.get("real_trading_enabled") is not False:
            raise RuntimeError(f"ashare_local_account_{source_name}_not_simulated")
        if any(payload.get(key) != value for key, value in expected_authority.items()):
            raise RuntimeError(f"ashare_local_account_{source_name}_lineage_mismatch")
    lot_positions = snapshot.get("positions") if isinstance(snapshot, dict) else {}
    pnl_positions = pnl.get("positions") if isinstance(pnl, dict) else {}
    if not isinstance(lot_positions, dict) or not isinstance(pnl_positions, dict):
        raise RuntimeError("ashare_local_account_positions_unavailable")
    normalized_lots, _, lot_reason = normalize_ashare_positions(lot_positions)
    normalized_pnl, _, pnl_reason = normalize_ashare_positions(pnl_positions)
    if normalized_lots is None:
        raise RuntimeError(f"ashare_local_account_snapshot_invalid:{lot_reason}")
    if normalized_pnl is None:
        raise RuntimeError(f"ashare_local_account_pnl_invalid:{pnl_reason}")
    if canonical_sha256(normalized_lots) != canonical_sha256(normalized_pnl):
        raise RuntimeError("ashare_local_account_position_sources_mismatch")
    expected_date = _compact_date_key(trade_date)
    expected_fingerprint = canonical_sha256(normalized_lots)
    expected_count = len(normalized_lots)
    envelope_fields = (
        "source",
        "position_source_status",
        "authority_id",
        "authority_generation",
        "execution_lineage_id",
        "authority_checksum",
        "trade_date",
        "position_count",
        "positions_fingerprint",
    )
    for source_name, payload in (("snapshot", snapshot), ("pnl", pnl)):
        if any(field not in payload for field in envelope_fields):
            raise RuntimeError(
                f"ashare_local_account_{source_name}_position_envelope_missing"
            )
        if not str(payload.get("source") or "").strip():
            raise RuntimeError(f"ashare_local_account_{source_name}_source_missing")
        if payload.get("position_source_status") != "ready":
            raise RuntimeError(
                f"ashare_local_account_{source_name}_position_source_not_ready"
            )
        if payload.get("authority_id") != ASHARE_CAPITAL_AUTHORITY_ID:
            raise RuntimeError(f"ashare_local_account_{source_name}_authority_mismatch")
        if (
            isinstance(payload.get("authority_generation"), bool)
            or payload.get("authority_generation") != ASHARE_AUTHORITY_GENERATION
        ):
            raise RuntimeError(
                f"ashare_local_account_{source_name}_generation_mismatch"
            )
        if payload.get("execution_lineage_id") != ASHARE_EXECUTION_LINEAGE_ID:
            raise RuntimeError(f"ashare_local_account_{source_name}_lineage_mismatch")
        checksum = str(payload.get("authority_checksum") or "").lower()
        if len(checksum) != 64 or any(ch not in "0123456789abcdef" for ch in checksum):
            raise RuntimeError(f"ashare_local_account_{source_name}_checksum_invalid")
        if _compact_date_key(payload.get("trade_date")) != expected_date:
            raise RuntimeError(
                f"ashare_local_account_{source_name}_trade_date_mismatch"
            )
        if (
            isinstance(payload.get("position_count"), bool)
            or payload.get("position_count") != expected_count
        ):
            raise RuntimeError(
                f"ashare_local_account_{source_name}_position_count_mismatch"
            )
        if str(payload.get("positions_fingerprint") or "").lower() != (
            expected_fingerprint
        ):
            raise RuntimeError(
                f"ashare_local_account_{source_name}_positions_fingerprint_mismatch"
            )
    if snapshot["authority_checksum"] != pnl["authority_checksum"]:
        raise RuntimeError("ashare_local_account_authority_checksum_mismatch")
    positions: list[dict[str, Any]] = []
    for symbol in sorted(set(lot_positions) | set(pnl_positions)):
        lots = (
            lot_positions.get(symbol)
            if isinstance(lot_positions.get(symbol), dict)
            else {}
        )
        economics = (
            pnl_positions.get(symbol)
            if isinstance(pnl_positions.get(symbol), dict)
            else {}
        )
        quantity = _safe_int(lots.get("quantity", economics.get("quantity")), 0)
        if quantity <= 0:
            continue
        market_value = _safe_float(economics.get("market_value"), 0.0)
        avg_cost = _safe_float(economics.get("avg_cost"), 0.0)
        last_price = _safe_float(
            economics.get("mark_price", economics.get("last_price")),
            avg_cost,
        )
        positions.append(
            {
                "ts_code": symbol,
                "quantity": quantity,
                "sellable_quantity": _safe_int(lots.get("sellable_quantity"), 0),
                "avg_price": avg_cost,
                "last_price": last_price,
                "market_value": market_value,
                "weight": round(market_value / 50_000.0, 8),
                "capital_layer": "simulated",
                "account_type": "simulated",
                "account_source": "server_local_sim_ledger",
                "sample_intent": str(
                    economics.get("sample_intent", lots.get("sample_intent", "")) or ""
                ),
                "exploration_exposure_cny": round(
                    max(
                        _safe_float(
                            economics.get(
                                "exploration_exposure_cny",
                                lots.get("exploration_exposure_cny"),
                            ),
                            0.0,
                        ),
                        0.0,
                    ),
                    2,
                ),
            }
        )
    cash_available = _safe_float(snapshot.get("cash_available"), -1.0)
    if cash_available < 0:
        raise RuntimeError("ashare_local_account_cash_unavailable")
    view = {
        "account": account_name,
        "capital_cny": 50_000.0,
        "cash_available": round(cash_available, 2),
        "positions": positions,
        "source": "server_local_sim_ledger",
        "trade_date": _compact_date_key(trade_date),
        **expected_authority,
        "authority_id": snapshot["authority_id"],
        "authority_checksum": snapshot["authority_checksum"],
        "position_count": expected_count,
        "positions_fingerprint": expected_fingerprint,
        "position_source_status": "ready",
        "real_trading_enabled": False,
    }
    return view


def _ashare_position_sources_from_account(
    account_obj: Any,
    local_account_view: dict[str, Any],
) -> dict[str, Any]:
    """Preserve only source-owned canonical envelopes for reconciliation."""

    raw_account = account_obj if isinstance(account_obj, dict) else {}
    adapter_source: dict[str, Any] = {
        "source": str(raw_account.get("source") or "ashare_adapter_position_snapshot"),
        "position_source_status": raw_account.get("position_source_status"),
        "positions": raw_account.get("positions")
        if "positions" in raw_account
        else None,
    }
    for key in (
        "authority_id",
        "authority_generation",
        "execution_lineage_id",
        "authority_checksum",
        "trade_date",
        "position_count",
        "positions_fingerprint",
    ):
        if key in raw_account:
            adapter_source[key] = raw_account[key]
    sources: dict[str, Any] = {
        "server_local": dict(local_account_view),
        "strategy_adapter": adapter_source,
    }
    if "strategy_positions" in raw_account:
        strategy_envelope = raw_account.get("strategy_position_envelope")
        sources["strategy_positions"] = (
            dict(strategy_envelope)
            if isinstance(strategy_envelope, dict)
            else {
                "source": str(
                    raw_account.get("strategy_position_source")
                    or "ashare_adapter_strategy_positions"
                ),
                "position_source_status": "blocked",
                "positions": raw_account.get("strategy_positions"),
            }
        )
    return sources


def _strict_finite_number(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _validate_ashare_market_capital_state(
    state: Any,
    trade_date: str,
) -> tuple[dict[str, Any] | None, str]:
    """Validate the independent fresh-start A-share capital authority.

    This is an execution gate only.  Observation/counterfactual sampling does
    not depend on it, and a 5% drawdown only tightens size; 7% halts new risk.
    """

    if not isinstance(state, dict):
        return None, "ashare_capital_unavailable"
    expected_date = _compact_date_key(trade_date)
    if str(state.get("source") or "") != "market_capital_ledger":
        return None, "ashare_capital_source_invalid"
    if str(state.get("authority_id") or "") != "ashare-capital-v1":
        return None, "ashare_capital_authority_mismatch"
    if state.get("authority_generation") != 1:
        return None, "ashare_capital_generation_mismatch"
    if str(state.get("market") or "").lower() != "ashare":
        return None, "ashare_capital_market_mismatch"
    if str(state.get("currency") or "") != "CNY":
        return None, "ashare_capital_currency_mismatch"
    if state.get("real_trading_enabled") is not False:
        return None, "ashare_capital_real_trading_flag_invalid"
    if state.get("fresh") is not True or state.get("reconciled") is not True:
        return None, "ashare_capital_not_reconciled_for_trade_date"
    if _compact_date_key(state.get("trade_date")) != expected_date:
        return None, "ashare_capital_trade_date_mismatch"
    if not str(state.get("event_id") or "").strip():
        return None, "ashare_capital_event_missing"
    if not str(state.get("execution_lineage_id") or "").strip():
        return None, "ashare_capital_execution_lineage_missing"
    required_numbers = (
        "initial_equity_cny",
        "equity_cny",
        "cash_balance_cny",
        "positions_market_value_cny",
        "frozen_order_cash_cny",
        "realized_pnl_cny",
        "unrealized_pnl_cny",
        "reserved_capital_cny",
        "active_reservations_cny",
        "available_to_reserve_cny",
        "stock_gross_exposure_limit_cny",
        "single_name_cap_cny",
        "capital_utilization_rate",
        "daily_mtm_change",
        "daily_realized_pnl",
        "max_daily_loss",
        "high_water_equity",
        "max_drawdown",
    )
    numbers = {key: _strict_finite_number(state.get(key)) for key in required_numbers}
    if any(value is None for value in numbers.values()):
        return None, "ashare_capital_state_incomplete"
    if not math.isclose(numbers["initial_equity_cny"], 50_000.0, abs_tol=0.01):
        return None, "ashare_capital_initial_equity_mismatch"
    if not math.isclose(
        numbers["equity_cny"],
        numbers["cash_balance_cny"] + numbers["positions_market_value_cny"],
        abs_tol=0.01,
    ):
        return None, "ashare_capital_equity_reconciliation_failed"
    if not math.isclose(
        numbers["reserved_capital_cny"],
        numbers["active_reservations_cny"],
        abs_tol=0.01,
    ):
        return None, "ashare_capital_reservation_reconciliation_failed"
    for key in (
        "equity_cny",
        "cash_balance_cny",
        "positions_market_value_cny",
        "frozen_order_cash_cny",
        "reserved_capital_cny",
        "active_reservations_cny",
        "available_to_reserve_cny",
        "capital_utilization_rate",
    ):
        if numbers[key] < 0.0:
            return None, f"ashare_capital_negative_{key}"
    consecutive_losses = state.get("consecutive_losses")
    max_consecutive_losses = state.get("max_consecutive_losses")
    if (
        not isinstance(consecutive_losses, int)
        or isinstance(consecutive_losses, bool)
        or consecutive_losses < 0
        or not isinstance(max_consecutive_losses, int)
        or isinstance(max_consecutive_losses, bool)
        or max_consecutive_losses <= 0
    ):
        return None, "ashare_capital_loss_streak_state_invalid"
    if numbers["max_daily_loss"] <= 0 or numbers["max_drawdown"] <= 0:
        return None, "ashare_capital_risk_budget_invalid"
    policy = market_capital.MarketPolicy.load("ashare")
    expected_daily_loss = policy.initial_equity_cny * policy.daily_loss_pause_pct
    expected_drawdown_halt = policy.initial_equity_cny * policy.drawdown_halt_pct
    if (
        not math.isclose(
            numbers["stock_gross_exposure_limit_cny"],
            policy.stock_gross_exposure_limit_cny,
            abs_tol=0.01,
        )
        or not math.isclose(
            numbers["single_name_cap_cny"],
            policy.single_name_cap_cny,
            abs_tol=0.01,
        )
        or not math.isclose(
            numbers["max_daily_loss"], expected_daily_loss, abs_tol=0.01
        )
        or max_consecutive_losses != policy.max_consecutive_losses
        or not math.isclose(
            numbers["max_drawdown"], expected_drawdown_halt, abs_tol=0.01
        )
    ):
        return None, "ashare_capital_policy_mismatch"
    committed = (
        numbers["positions_market_value_cny"]
        + numbers["frozen_order_cash_cny"]
        + numbers["active_reservations_cny"]
    )
    expected_available = max(
        0.0,
        min(
            numbers["cash_balance_cny"]
            - numbers["frozen_order_cash_cny"]
            - numbers["active_reservations_cny"],
            policy.stock_gross_exposure_limit_cny - committed,
        ),
    )
    if not math.isclose(
        numbers["available_to_reserve_cny"], expected_available, abs_tol=0.01
    ):
        return None, "ashare_capital_available_capacity_mismatch"
    if numbers["daily_mtm_change"] <= -abs(numbers["max_daily_loss"]):
        return None, "ashare_capital_daily_loss_pause"
    if consecutive_losses >= max_consecutive_losses:
        return None, "ashare_capital_consecutive_loss_pause"
    drawdown = max(0.0, numbers["high_water_equity"] - numbers["equity_cny"])
    if drawdown >= numbers["max_drawdown"]:
        return None, "ashare_capital_drawdown_halt"
    validated = dict(state)
    drawdown_tighten = policy.initial_equity_cny * policy.drawdown_tighten_pct
    validated["drawdown_cny"] = round(drawdown, 2)
    validated["drawdown_tightened"] = drawdown >= drawdown_tighten
    validated["new_risk_allowed"] = True
    validated["risk_multiplier"] = (
        policy.drawdown_tighten_risk_multiplier
        if validated["drawdown_tightened"]
        else 1.0
    )
    return validated, (
        "approved_drawdown_tightened" if validated["drawdown_tightened"] else "approved"
    )


_ASHARE_NEW_RISK_PAUSE_REASONS = frozenset(
    {
        "ashare_capital_daily_loss_pause",
        "ashare_capital_consecutive_loss_pause",
        "ashare_capital_drawdown_halt",
    }
)


def _validate_ashare_position_capital_state(
    state: Any,
    trade_date: str,
) -> tuple[dict[str, Any] | None, str]:
    """Keep structurally valid capital state available to position authority.

    The legacy capital validator intentionally returns ``None`` for the three
    policy states that pause *new* risk.  Those states occur only after every
    authority, reconciliation, date, policy, and numeric contract check has
    passed, so position replay remains valid while buy/open/add eligibility is
    false.  Every other validation failure remains a full fail-closed block.
    """

    validated, reason = _validate_ashare_market_capital_state(state, trade_date)
    if validated is not None:
        result = dict(validated)
        result["new_risk_allowed"] = True
        result["new_risk_reason"] = ""
        return result, reason
    if reason not in _ASHARE_NEW_RISK_PAUSE_REASONS or not isinstance(state, dict):
        return None, reason

    policy = market_capital.MarketPolicy.load("ashare")
    drawdown = max(
        0.0,
        _safe_float(state.get("high_water_equity"), 0.0)
        - _safe_float(state.get("equity_cny"), 0.0),
    )
    result = dict(state)
    result["drawdown_cny"] = round(drawdown, 2)
    result["drawdown_tightened"] = (
        drawdown >= policy.initial_equity_cny * policy.drawdown_tighten_pct
    )
    result["new_risk_allowed"] = False
    result["new_risk_reason"] = reason
    result["risk_multiplier"] = 0.0
    return result, reason


def _resolve_ashare_position_authority_for_entry(
    market_adapter: MarketAdapter,
    date: str,
    *,
    errors: list[dict[str, Any]],
    stage_calls: list[str],
    stage_prefix: str,
    capital_layer: str,
) -> dict[str, Any]:
    """Resolve every A-share position source before an entry may call risk."""

    before_stage = f"{stage_prefix}.market_capital_before"
    raw_before = _safe_stage(
        before_stage,
        errors,
        lambda: market_capital.load_market_capital_provider_state("ashare", date),
        default=None,
        capital_layer=capital_layer,
    )
    stage_calls.append(before_stage)
    validated_before, validation_reason = _validate_ashare_position_capital_state(
        raw_before, date
    )
    authority_before = build_ashare_capital_position_authority_view(raw_before, date)
    if validated_before is None or authority_before.get("status") != "verified":
        reason = (
            str(
                validation_reason
                if validated_before is None
                else authority_before.get("reason")
            )
            or "ashare_capital_state_invalid"
        )
        return {
            **authority_before,
            "status": "blocked",
            "reason": reason,
            "source_audit": [
                ashare_capital_state_audit(
                    raw_before,
                    authority_before,
                    source_name="market_capital_before",
                )
            ],
            "mismatches": [],
            "positions": [],
        }

    getter = getattr(market_adapter, "get_sim_account", None)
    account_stage = f"{stage_prefix}.adapter_position_source"
    account_obj = _safe_stage(
        account_stage,
        errors,
        (
            lambda: (
                _load_sim_account_for_trade_date(
                    getter, date, position_authority=authority_before
                )
                if callable(getter)
                else {"account": "ashare_sim", "position_source_status": "blocked"}
            )
        ),
        default={
            "account": "ashare_sim",
            "positions": None,
            "position_source_status": "blocked",
        },
        capital_layer=capital_layer,
    )
    stage_calls.append(account_stage)
    if _account_name(account_obj, "ashare_sim") != "ashare_sim":
        return {
            **authority_before,
            "status": "blocked",
            "reason": CAPITAL_POSITION_SOURCE_MISMATCH,
            "source_audit": [
                ashare_capital_state_audit(
                    raw_before,
                    authority_before,
                    source_name="market_capital_before",
                )
            ],
            "mismatches": [
                {
                    "source_name": "strategy_adapter",
                    "fields": ["account_identity"],
                    "source_sha256": canonical_sha256(account_obj),
                    "execution_lineage_id": "",
                }
            ],
            "positions": [],
        }

    local_stage = f"{stage_prefix}.server_local_position_source"
    local_view = _safe_stage(
        local_stage,
        errors,
        lambda: _ashare_authoritative_account_view(
            account_obj, date, position_authority=authority_before
        ),
        default={
            "account": "ashare_sim",
            "source": "server_local_sim_ledger_unavailable",
            "position_source_status": "blocked",
            "positions": None,
            "trade_date": _compact_date_key(date),
            "real_trading_enabled": False,
        },
        capital_layer=capital_layer,
    )
    stage_calls.append(local_stage)
    sources = _ashare_position_sources_from_account(account_obj, local_view)

    after_stage = f"{stage_prefix}.market_capital_after"
    raw_after = _safe_stage(
        after_stage,
        errors,
        lambda: market_capital.load_market_capital_provider_state("ashare", date),
        default=None,
        capital_layer=capital_layer,
    )
    stage_calls.append(after_stage)
    final_validated, final_reason = _validate_ashare_position_capital_state(
        raw_after, date
    )
    result = reconcile_ashare_position_sources(
        raw_before,
        date,
        sources=sources,
        preferred_source="server_local",
        final_capital_state=raw_after,
    )
    if final_validated is None and result.get("status") == "verified":
        return {
            **result,
            "status": "blocked",
            "reason": CAPITAL_POSITION_SOURCE_MISMATCH,
            "positions": [],
            "mismatches": [
                {
                    "source_name": "market_capital_after",
                    "fields": ["capital_state_validation"],
                    "source_sha256": canonical_sha256(raw_after),
                    "execution_lineage_id": str(
                        (raw_after or {}).get("execution_lineage_id")
                        if isinstance(raw_after, dict)
                        else ""
                    ),
                    "detail": final_reason,
                }
            ],
        }
    if result.get("status") == "verified":
        positions_market_value = max(
            0.0,
            _safe_float(final_validated.get("positions_market_value_cny"), 0.0),
        )
        result = {
            **result,
            "capital_cash_available": max(
                0.0,
                min(
                    _safe_float(final_validated.get("cash_balance_cny"), 0.0),
                    _safe_float(final_validated.get("available_to_reserve_cny"), 0.0),
                ),
            ),
            "capital_positions_market_value_cny": positions_market_value,
            "capital_total_exposure": min(1.0, positions_market_value / 50_000.0),
            "new_risk_allowed": bool(final_validated.get("new_risk_allowed")),
            "new_risk_reason": str(final_validated.get("new_risk_reason") or ""),
        }
    return result


def _ashare_price_limit_pct(order: dict[str, Any]) -> float:
    snapshot = (
        order.get("market_snapshot")
        if isinstance(order.get("market_snapshot"), dict)
        else {}
    )
    explicit = _safe_float(
        snapshot.get("price_limit_pct", order.get("price_limit_pct")),
        0.0,
    )
    if explicit > 0:
        return min(explicit, 0.30)
    symbol = str(order.get("ts_code") or "").strip().upper()
    return ashare_execution_reality().price_limit_pct(
        symbol=symbol,
        board=str(snapshot.get("board") or order.get("board") or ""),
        risk_warning=bool(
            snapshot.get("risk_warning")
            or snapshot.get("is_st")
            or order.get("risk_warning")
            or order.get("is_st")
        ),
    )


def _estimate_ashare_market_reservation(order: dict[str, Any]) -> float:
    """Reserve worst-case price-limit gross plus versioned buy-side fees."""

    quantity = _safe_int(order.get("quantity"), 0)
    snapshot = (
        order.get("market_snapshot")
        if isinstance(order.get("market_snapshot"), dict)
        else {}
    )
    references = [
        _safe_float(order.get("price"), 0.0),
        _safe_float(order.get("limit_price"), 0.0),
        _safe_float(snapshot.get("ask_price"), 0.0),
        _safe_float(snapshot.get("last_price"), 0.0),
    ]
    base_price = max(references)
    upper_limit = _safe_float(snapshot.get("upper_limit"), 0.0)
    if upper_limit <= 0:
        reference_price = max(
            base_price,
            _safe_float(snapshot.get("previous_close"), 0.0),
            _safe_float(snapshot.get("pre_close"), 0.0),
            _safe_float(snapshot.get("reference_price"), 0.0),
        )
        upper_limit = reference_price * (1.0 + _ashare_price_limit_pct(order))
    ceiling_price = max(base_price, upper_limit)
    if quantity <= 0 or ceiling_price <= 0:
        return 0.0
    amount = round(quantity * ceiling_price, 2)
    fees = ashare_execution_reality().calculate_fees("buy", amount)
    return round(amount + float(fees["total"]), 2)


def _capture_ashare_market_capital_head(
    expected_event_id: str = "",
) -> dict[str, str]:
    """Capture a stable public-ledger head for the immutable fill outbox."""

    expected = str(expected_event_id or "").strip()
    try:
        policy = market_capital.MarketPolicy.load("ashare")
        ledger = market_capital.MarketCapitalLedger(
            market_capital.market_capital_root("ashare"),
            policy=policy,
        )
        before = ledger.snapshot()
        if not expected:
            expected = str(before.event_id or "").strip()
        chain = ledger.validate_checksum_chain()
        after = ledger.snapshot()
    except Exception:
        return {}
    checksum = str(chain.get("last_checksum") or "")
    if (
        not expected
        or chain.get("status") != "valid"
        or before.event_id != expected
        or after.event_id != expected
        or len(checksum) != 64
        or any(char not in "0123456789abcdef" for char in checksum.lower())
    ):
        return {}
    return {"event_id": expected, "checksum": checksum}


def _reserve_ashare_market_order(
    order: dict[str, Any],
    market_state: dict[str, Any] | None,
    market_state_reason: str,
) -> dict[str, Any]:
    if market_state is None:
        return {"approved": False, "reason": market_state_reason}
    amount = _estimate_ashare_market_reservation(order)
    if amount <= 0:
        return {"approved": False, "reason": "invalid_ashare_reservation_amount"}
    risk_unit_key = str(order.get("ts_code") or "").strip().upper()
    authority_id = str(order.get("capital_authority_id") or "").strip()
    authority_generation = order.get("authority_generation")
    execution_lineage_id = str(order.get("execution_lineage_id") or "").strip()
    point_in_time_as_of = str(order.get("point_in_time_as_of") or "").strip()
    lineage_sha256 = str(order.get("execution_lineage_sha256") or "").strip()
    lineage_valid = (
        risk_unit_key
        and authority_id == str(market_state.get("authority_id") or "")
        and authority_generation == market_state.get("authority_generation") == 1
        and execution_lineage_id == str(market_state.get("execution_lineage_id") or "")
        and point_in_time_as_of
        and len(lineage_sha256) == 64
        and all(char in "0123456789abcdef" for char in lineage_sha256.lower())
    )
    if not lineage_valid:
        return {"approved": False, "reason": "ashare_capital_lineage_missing"}
    reference_id = (
        f"AMCAP:{authority_generation}:{execution_lineage_id}:"
        f"{order['idempotency_key']}"
    )
    try:
        decision = market_capital.reserve_market_capital(
            "ashare",
            market_capital.MarketCapitalReservationRequest(
                market="ashare",
                reference_id=reference_id,
                risk_unit_key=risk_unit_key,
                worst_case_amount_cny=amount,
                authority_id=authority_id,
                authority_generation=authority_generation,
                trade_date=str(
                    market_state.get("trade_date") or order.get("trade_date") or ""
                ),
                point_in_time_as_of=point_in_time_as_of,
                lineage_sha256=lineage_sha256,
                execution_lineage_id=execution_lineage_id,
            ),
        )
    except Exception as exc:
        return {
            "approved": False,
            "reason": f"ashare_capital_reserve_error:{exc.__class__.__name__}",
        }
    result = {
        "approved": bool(decision.approved),
        "reason": str(decision.reason or "ashare_capital_reservation_rejected"),
        "reservation_id": str(decision.reservation_id or ""),
        "event_id": str(decision.event_id or ""),
        "reference_id": reference_id,
        "amount_cny": amount,
        "authority_id": authority_id,
        "authority_generation": authority_generation,
        "execution_lineage_id": execution_lineage_id,
        "lineage_sha256": lineage_sha256,
        "point_in_time_as_of": point_in_time_as_of,
        "risk_unit_key": risk_unit_key,
    }
    existing_reservation = result["reason"] in {
        "idempotent_reservation",
        "reservation_closed",
    }
    captured_head: dict[str, str] = {}
    if result["approved"] and not existing_reservation:
        captured_head = _capture_ashare_market_capital_head(result["event_id"])
        if not captured_head:
            return {
                **result,
                "approved": False,
                "reason": "ashare_capital_head_capture_failed",
            }
    if result["approved"] or result["reason"] == "reservation_closed":
        order.update(
            {
                "capital_scope": "strategy",
                "market_capital_required": True,
                "market_capital_reference_id": reference_id,
                "market_capital_reservation_id": result["reservation_id"],
                "market_capital_event_id": result["event_id"],
                "market_reserved_gross_cny": amount,
                "risk_unit_key": risk_unit_key,
                "market_capital_risk_unit_key": risk_unit_key,
                "real_trading_enabled": False,
            }
        )
        if captured_head:
            order.update(
                {
                    "market_capital_expected_head_event_id": captured_head["event_id"],
                    "market_capital_expected_head_checksum": captured_head["checksum"],
                }
            )
    return result


def _receipt_from_local_ashare_trade(
    trade: dict[str, Any],
    order: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(trade, dict):
        return None
    if str(trade.get("capital_scope") or "") != "strategy":
        return None
    if (
        str(trade.get("capital_authority_id") or "") != "ashare-capital-v1"
        or trade.get("authority_generation") != 1
        or str(trade.get("execution_lineage_id") or "")
        != str(order.get("execution_lineage_id") or "")
    ):
        return None
    if str(trade.get("market_capital_reservation_id") or "") != str(
        order.get("market_capital_reservation_id") or ""
    ):
        return None
    if (
        str(trade.get("market_capital_reference_id") or "")
        != str(order.get("market_capital_reference_id") or "")
        or str(trade.get("market_capital_event_id") or "")
        != str(order.get("market_capital_event_id") or "")
        or str(trade.get("market_capital_risk_unit_key") or "").upper()
        != str(order.get("risk_unit_key") or "").upper()
    ):
        return None
    status = str(trade.get("status") or "").strip().lower()
    quantity = _safe_int(trade.get("quantity"), 0)
    price = _safe_float(trade.get("filled_price"), 0.0)
    if (
        status not in {"filled", "partial"}
        or quantity <= 0
        or price <= 0
        or not local_sim_ledger._verified_ashare_execution_evidence(
            trade.get("fill_evidence"),
            trade.get("fill_price_source_class"),
        )
    ):
        return None
    backup = dict(trade)
    backup.update({"recorded": True, "ledger": "server_local_sim_backup"})
    commission = max(0.0, _safe_float(trade.get("commission"), 0.0))
    stamp_duty = max(0.0, _safe_float(trade.get("stamp_duty"), 0.0))
    transfer_fee = max(0.0, _safe_float(trade.get("transfer_fee"), 0.0))
    fill_evidence = (
        trade.get("fill_evidence")
        if isinstance(trade.get("fill_evidence"), dict)
        else {}
    )
    quote_price = _safe_float(
        fill_evidence.get("quote_price"),
        _safe_float(order.get("price"), price),
    )
    return {
        "order_id": order.get("order_id", ""),
        "status": status,
        "filled_qty": quantity,
        "filled_quantity": quantity,
        "avg_price": price,
        "filled_price": price,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "execution_eligible": status == "filled",
        "commission": round(commission, 6),
        "stamp_duty": round(stamp_duty, 6),
        "transfer_fee": round(transfer_fee, 6),
        "fee": round(commission + stamp_duty + transfer_fee, 6),
        "execution_reality_model_version": trade.get(
            "execution_reality_model_version", ""
        ),
        "commission_schedule_status": trade.get("commission_schedule_status", ""),
        "commission_schedule_version": trade.get("commission_schedule_version", ""),
        "slippage_cny": round(abs(price - quote_price) * quantity, 6),
        "filled_at": str(
            trade.get("created_at")
            or trade.get("filled_at")
            or order.get("point_in_time_as_of")
            or ""
        ),
        "recovery_source": "server_local_trade_log",
        "raw_response": {"local_sim_backup": backup},
    }


def _recover_local_ashare_receipt(
    order: dict[str, Any],
    account: str,
) -> dict[str, Any] | None:
    try:
        trade = local_sim_ledger.get_local_sim_trade_by_idempotency(
            str(order.get("idempotency_key") or ""),
            account=account,
        )
    except Exception:
        return None
    return _receipt_from_local_ashare_trade(trade, order) if trade else None


def _release_ashare_market_reservation(
    order: dict[str, Any],
    amount: float,
    reason: str,
    suffix: str,
) -> dict[str, Any]:
    try:
        return market_capital.release_market_capital(
            "ashare",
            str(order.get("market_capital_reservation_id") or ""),
            amount,
            reason,
            reference_id=(
                f"AMCAPREL:{order.get('authority_generation')}:"
                f"{order.get('execution_lineage_id')}:"
                f"{order.get('idempotency_key')}:{suffix}"
            ),
        )
    except Exception as exc:
        return {
            "status": "market_capital_release_error",
            "error": f"{exc.__class__.__name__}: {exc}",
            "real_trading_enabled": False,
        }


def _settle_ashare_market_receipt(
    order: dict[str, Any],
    receipt: dict[str, Any],
    account: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Require an atomic capital fill-commit for an immutable local fill."""

    recovered = _recover_local_ashare_receipt(order, account)
    if recovered is not None:
        receipt = recovered
    status = str(receipt.get("status") or "").strip().lower()
    reserved = _safe_float(order.get("market_reserved_gross_cny"), 0.0)
    if status in {"pending", "queued", "open", "retryable", "unfilled"}:
        return receipt, {
            "status": "reservation_retained_pending",
            "reservation_id": order.get("market_capital_reservation_id", ""),
            "amount_cny": reserved,
        }
    if status not in {"filled", "partial"}:
        released = _release_ashare_market_reservation(
            order,
            reserved,
            "ashare_terminal_without_fill",
            "terminal",
        )
        return receipt, {"status": "terminal_release", "release": released}

    raw = (
        receipt.get("raw_response")
        if isinstance(receipt.get("raw_response"), dict)
        else {}
    )
    backup = (
        raw.get("local_sim_backup")
        if isinstance(raw.get("local_sim_backup"), dict)
        else {}
    )
    backup_recorded = backup.get("recorded") is True
    lineage_matches = (
        str(backup.get("market_capital_reference_id") or "")
        == str(order.get("market_capital_reference_id") or "")
        and str(backup.get("market_capital_reservation_id") or "")
        == str(order.get("market_capital_reservation_id") or "")
        and str(backup.get("market_capital_event_id") or "")
        == str(order.get("market_capital_event_id") or "")
        and str(backup.get("execution_lineage_id") or "")
        == str(order.get("execution_lineage_id") or "")
        and backup.get("authority_generation") == order.get("authority_generation")
    )
    filled_qty = _safe_int(
        receipt.get("filled_qty", receipt.get("filled_quantity")),
        0,
    )
    filled_price = _safe_float(
        receipt.get("avg_price", receipt.get("filled_price")),
        0.0,
    )
    actual_cash_debit = _safe_float(backup.get("net_amount"), 0.0)
    evidence_verified = local_sim_ledger._verified_ashare_execution_evidence(
        backup.get("fill_evidence"),
        backup.get("fill_price_source_class"),
    )
    if (
        not backup_recorded
        or not lineage_matches
        or not evidence_verified
        or filled_qty <= 0
        or filled_price <= 0
        or actual_cash_debit <= 0
        or actual_cash_debit > reserved + 1e-9
    ):
        pending_receipt = dict(receipt)
        pending_receipt.update(
            {
                "execution_eligible": False,
                "reason": "authoritative_local_fill_missing",
                "message": "A-share fill retained for observation; capital fill-commit evidence is incomplete",
            }
        )
        return pending_receipt, {
            "status": "fill_commit_pending",
            "reason": "authoritative_local_fill_missing",
            "reservation_retained": True,
        }
    receipt = dict(receipt)
    commission = max(0.0, _safe_float(backup.get("commission"), 0.0))
    stamp_duty = max(0.0, _safe_float(backup.get("stamp_duty"), 0.0))
    transfer_fee = max(0.0, _safe_float(backup.get("transfer_fee"), 0.0))
    fill_evidence = (
        backup.get("fill_evidence")
        if isinstance(backup.get("fill_evidence"), dict)
        else {}
    )
    quote_price = _safe_float(
        fill_evidence.get("quote_price"),
        _safe_float(order.get("price"), filled_price),
    )
    receipt["commission"] = round(commission, 6)
    receipt["stamp_duty"] = round(stamp_duty, 6)
    receipt["transfer_fee"] = round(transfer_fee, 6)
    receipt["fee"] = round(commission + stamp_duty + transfer_fee, 6)
    receipt["execution_reality_model_version"] = str(
        backup.get("execution_reality_model_version") or ""
    )
    receipt["commission_schedule_status"] = str(
        backup.get("commission_schedule_status") or ""
    )
    receipt["commission_schedule_version"] = str(
        backup.get("commission_schedule_version") or ""
    )
    receipt["slippage_cny"] = round(
        abs(filled_price - quote_price) * filled_qty,
        6,
    )
    receipt["filled_at"] = str(
        backup.get("created_at")
        or backup.get("filled_at")
        or receipt.get("fill_time")
        or order.get("point_in_time_as_of")
        or ""
    )
    outbox = _dispatch_ashare_market_outbox(account)
    matching_actions = [
        action
        for action in outbox.get("actions", [])
        if isinstance(action, dict)
        and action.get("action") == "fill_commit"
        and str(action.get("reservation_id") or "")
        == str(order.get("market_capital_reservation_id") or "")
        and str(action.get("idempotency_key") or "")
        == str(order.get("idempotency_key") or "")
    ]
    committed_action = matching_actions[-1] if matching_actions else {}
    commit_result = (
        committed_action.get("last_result")
        if isinstance(committed_action.get("last_result"), dict)
        else {}
    )
    commit_success = (
        committed_action.get("status") == "completed"
        and commit_result.get("committed") is True
        and str(commit_result.get("status") or "") in {"committed", "idempotent"}
    )
    terminal_partial = status == "partial" and backup.get("partial_terminal") is True
    terminal_fill = status == "filled" or terminal_partial
    receipt["execution_eligible"] = bool(commit_success and terminal_fill)
    if not commit_success:
        receipt["reason"] = "market_capital_fill_commit_pending"
        receipt["message"] = (
            "A-share local fill retained as observation until capital fill-commit succeeds"
        )
    settlement = {
        "status": "fill_committed" if commit_success else "fill_commit_pending",
        "reservation_id": str(order.get("market_capital_reservation_id") or ""),
        "reservation_retained": not commit_success or not terminal_fill,
        "terminal": terminal_fill,
        "outbox_status": str(outbox.get("status") or "unknown"),
        "commit_result": dict(commit_result),
    }
    return receipt, settlement


def _reconcile_ashare_sell_receipt(
    order: dict[str, Any],
    receipt: dict[str, Any],
    account: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bind an immutable local sell fill to one atomic capital sell commit."""

    recovered = _recover_local_ashare_receipt(order, account)
    if recovered is not None:
        receipt = recovered
    status = str(receipt.get("status") or "").strip().lower()
    if status not in {"filled", "partial"}:
        return receipt, {
            "status": "sell_not_filled",
            "terminal": status not in {"pending", "queued", "open", "retryable"},
        }
    raw = (
        receipt.get("raw_response")
        if isinstance(receipt.get("raw_response"), dict)
        else {}
    )
    backup = (
        raw.get("local_sim_backup")
        if isinstance(raw.get("local_sim_backup"), dict)
        else {}
    )
    quantity = _safe_int(receipt.get("filled_qty", receipt.get("filled_quantity")), 0)
    price = _safe_float(receipt.get("avg_price", receipt.get("filled_price")), 0.0)
    valid = (
        backup.get("recorded") is True
        and str(backup.get("capital_scope") or "") == "strategy"
        and str(backup.get("capital_authority_id") or "") == "ashare-capital-v1"
        and backup.get("authority_generation") == 1
        and str(backup.get("execution_lineage_id") or "")
        == str(order.get("execution_lineage_id") or "")
        and backup.get("market_capital_required") is True
        and bool(str(backup.get("market_capital_expected_head_event_id") or ""))
        and len(str(backup.get("market_capital_expected_head_checksum") or "")) == 64
        and quantity > 0
        and price > 0
        and local_sim_ledger._verified_ashare_execution_evidence(
            backup.get("fill_evidence"),
            backup.get("fill_price_source_class"),
        )
    )
    if not valid:
        rejected = dict(receipt)
        rejected.update(
            {
                "status": "rejected",
                "filled_qty": 0,
                "filled_quantity": 0,
                "avg_price": 0.0,
                "filled_price": 0.0,
                "execution_eligible": False,
                "reason": "authoritative_local_fill_missing",
                "message": "A-share sell fill lacks authoritative local ledger evidence",
            }
        )
        return rejected, {
            "status": "sell_commit_pending",
            "reason": "authoritative_local_fill_missing",
            "terminal": False,
        }
    reconciled = dict(receipt)
    commission = max(0.0, _safe_float(backup.get("commission"), 0.0))
    stamp_duty = max(0.0, _safe_float(backup.get("stamp_duty"), 0.0))
    transfer_fee = max(0.0, _safe_float(backup.get("transfer_fee"), 0.0))
    fill_evidence = (
        backup.get("fill_evidence")
        if isinstance(backup.get("fill_evidence"), dict)
        else {}
    )
    quote_price = _safe_float(
        fill_evidence.get("quote_price"),
        _safe_float(order.get("price"), price),
    )
    reconciled["commission"] = round(commission, 6)
    reconciled["stamp_duty"] = round(stamp_duty, 6)
    reconciled["transfer_fee"] = round(transfer_fee, 6)
    reconciled["fee"] = round(commission + stamp_duty + transfer_fee, 6)
    reconciled["execution_reality_model_version"] = str(
        backup.get("execution_reality_model_version") or ""
    )
    reconciled["commission_schedule_status"] = str(
        backup.get("commission_schedule_status") or ""
    )
    reconciled["commission_schedule_version"] = str(
        backup.get("commission_schedule_version") or ""
    )
    reconciled["slippage_cny"] = round(abs(price - quote_price) * quantity, 6)
    reconciled["filled_at"] = str(
        backup.get("created_at")
        or backup.get("filled_at")
        or receipt.get("fill_time")
        or order.get("point_in_time_as_of")
        or ""
    )
    outbox = _dispatch_ashare_market_outbox(account)
    matching_actions = [
        action
        for action in outbox.get("actions", [])
        if isinstance(action, dict)
        and action.get("action") == "ashare_sell_commit"
        and str(action.get("idempotency_key") or "")
        == str(order.get("idempotency_key") or "")
        and str(action.get("risk_unit_key") or "").upper()
        == str(order.get("risk_unit_key") or "").upper()
    ]
    committed_action = matching_actions[-1] if matching_actions else {}
    commit_result = (
        committed_action.get("last_result")
        if isinstance(committed_action.get("last_result"), dict)
        else {}
    )
    commit_success = (
        committed_action.get("status") == "completed"
        and commit_result.get("committed") is True
        and str(commit_result.get("status") or "") in {"committed", "idempotent"}
    )
    terminal_partial = status == "partial" and backup.get("partial_terminal") is True
    terminal_fill = status == "filled" or terminal_partial
    reconciled["execution_eligible"] = bool(commit_success and terminal_fill)
    if not commit_success:
        reconciled["reason"] = "market_capital_sell_commit_pending"
        reconciled["message"] = (
            "A-share local sell fill retained as observation until the atomic "
            "capital sell commit succeeds"
        )
    settlement = {
        "status": ("sell_committed" if commit_success else "sell_commit_pending"),
        "terminal": terminal_fill,
        "outbox_status": str(outbox.get("status") or "unknown"),
        "commit_result": dict(commit_result),
    }
    return reconciled, settlement


def _dispatch_ashare_market_outbox(account: str) -> dict[str, Any]:
    try:
        result = local_sim_ledger.replay_local_sim_market_capital_outbox()
    except Exception as exc:
        return {
            "status": "outbox_unavailable",
            "error": f"{exc.__class__.__name__}: {exc}",
            "actions": [],
            "account": account,
        }
    return {**dict(result), "account": account}


def _ashare_strategy_account_view(
    account: Any,
    existing_positions: list[dict[str, Any]],
    available_cash: float,
) -> tuple[list[dict[str, Any]], float, dict[str, Any]]:
    if not isinstance(account, dict):
        return existing_positions, available_cash, {}
    strategy_positions = account.get("strategy_positions")
    strategy_cash = _safe_float(account.get("strategy_cash_available"), -1.0)
    adjustment = (
        account.get("capital_plan_sample_adjustment")
        if isinstance(account.get("capital_plan_sample_adjustment"), dict)
        else {}
    )
    if isinstance(strategy_positions, list):
        positions = [dict(row) for row in strategy_positions if isinstance(row, dict)]
    else:
        positions = existing_positions
    original_strategy_cash = strategy_cash
    if strategy_cash < 0:
        strategy_cash = available_cash
    else:
        # Cap strategy cash to real account cash so the capital plan does not
        # budget beyond what the account can actually pay.  Keep original
        # values for diagnostics.
        strategy_cash = min(strategy_cash, available_cash)
    adjusted = dict(adjustment)
    if original_strategy_cash >= 0 and original_strategy_cash > available_cash:
        adjusted["original_strategy_cash_available"] = round(original_strategy_cash, 2)
        adjusted["strategy_cash_capped_to_account"] = True
    if not adjusted:
        return positions, strategy_cash, {}
    adjusted["account_position_count"] = len(
        {
            _position_symbol(position)
            for position in existing_positions
            if isinstance(position, dict) and _position_symbol(position)
        }
    )
    adjusted["strategy_position_count"] = len(
        {
            _position_symbol(position)
            for position in positions
            if isinstance(position, dict) and _position_symbol(position)
        }
    )
    adjusted["account_cash_available"] = round(available_cash, 2)
    adjusted["strategy_cash_available"] = round(strategy_cash, 2)
    return positions, strategy_cash, adjusted


def _run_review_for_layer(
    deps: OrchestratorDeps,
    date: str,
    *,
    session: str,
    capital_layer: str,
) -> dict[str, Any]:
    try:
        signature = inspect.signature(deps.run_review)
    except (TypeError, ValueError):
        result = deps.run_review(date, session=session)
    else:
        if "capital_layer" in signature.parameters:
            result = deps.run_review(date, session=session, capital_layer=capital_layer)
        else:
            result = deps.run_review(date, session=session)
    if isinstance(result, dict):
        result = dict(result)
        result["capital_layer"] = capital_layer
        return result
    return {
        "session": session,
        "trade_date": date,
        "capital_layer": capital_layer,
        "raw_result": result,
    }


def _coerce_sim_receipt(receipt: Any, order: dict[str, Any]) -> dict[str, Any]:
    if isinstance(receipt, dict):
        payload = dict(receipt)
    elif is_dataclass(receipt):
        payload = asdict(receipt)
    elif hasattr(receipt, "__dict__"):
        payload = {
            key: getattr(receipt, key)
            for key in (
                "status",
                "filled_qty",
                "filled_quantity",
                "avg_price",
                "filled_price",
                "fee",
                "message",
                "capital_layer",
                "account_type",
                "order_id",
                "market",
                "raw_response",
            )
            if hasattr(receipt, key)
        }
    else:
        return {"status": "failed", "message": "invalid sim broker receipt"}

    payload.setdefault("order_id", order.get("order_id", ""))
    if "filled_qty" in payload and "filled_quantity" not in payload:
        payload["filled_quantity"] = payload["filled_qty"]
    if "filled_quantity" in payload and "filled_qty" not in payload:
        payload["filled_qty"] = payload["filled_quantity"]
    if "avg_price" in payload and "filled_price" not in payload:
        payload["filled_price"] = payload["avg_price"]
    payload.setdefault("capital_layer", "simulated")
    payload.setdefault("account_type", "simulated")
    if not str(payload.get("status") or "").strip():
        payload["status"] = "failed"
        payload.setdefault("message", "sim broker receipt missing status")
        payload.setdefault("reason", "missing_receipt_status")
    return payload


def _execute_sim_order(
    deps: OrchestratorDeps, order: dict[str, Any], account: Any
) -> dict[str, Any]:
    if deps.execute_sim_order is None:
        raise RuntimeError("sim_broker.execute_sim_order is unavailable")
    try:
        signature = inspect.signature(deps.execute_sim_order)
    except (TypeError, ValueError):
        receipt = deps.execute_sim_order(order)
    else:
        params = signature.parameters
        kwargs: dict[str, Any] = {}
        if "market" in params:
            kwargs["market"] = str(order.get("market") or "").lower().strip()
        if "account" in params:
            kwargs["account"] = account
        elif "sim_account" in params:
            kwargs["sim_account"] = account
        if kwargs:
            receipt = deps.execute_sim_order(order, **kwargs)
        elif len(params) >= 2 and not any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in params.values()
        ):
            receipt = deps.execute_sim_order(order, account)
        else:
            receipt = deps.execute_sim_order(order)
    return _coerce_sim_receipt(receipt, order)


def _supports_market_aware_score(score_stock: StageFn) -> bool:
    try:
        signature = inspect.signature(score_stock)
    except (TypeError, ValueError):
        return True
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    first_name = positional[0].name.lower() if positional else ""
    second_name = positional[1].name.lower() if len(positional) > 1 else ""
    if first_name in {"symbol", "ts_code", "ticker", "market_id"} and second_name in {
        "date",
        "trade_date",
    }:
        return False
    return (
        any(
            parameter.kind == inspect.Parameter.VAR_POSITIONAL
            for parameter in signature.parameters.values()
        )
        or len(positional) >= 4
        or first_name.startswith("market")
        or "market" in signature.parameters
    )


def _score_stock_for_market(
    deps: OrchestratorDeps,
    market: str,
    symbol: str,
    date: str,
    reader: Any,
) -> Any:
    if _supports_market_aware_score(deps.score_stock):
        return deps.score_stock(market, symbol, reader, date)
    return deps.score_stock(symbol, date, data_reader=reader)


def _normalize_batch_scores(raw: Any) -> dict[str, dict[str, Any]]:
    if isinstance(raw, dict):
        if "data" in raw:
            return _normalize_batch_scores(raw.get("data"))
        return {
            str(symbol): dict(score)
            for symbol, score in raw.items()
            if isinstance(score, dict)
        }
    if not isinstance(raw, list):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for item in raw:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], dict):
            normalized[str(item[0])] = dict(item[1])
        elif isinstance(item, dict):
            symbol = str(
                item.get("symbol") or item.get("ts_code") or item.get("market_id") or ""
            ).strip()
            scores = (
                item.get("scores") if isinstance(item.get("scores"), dict) else item
            )
            if symbol and isinstance(scores, dict):
                normalized[symbol] = dict(scores)
    return normalized


def _score_universe_for_market(
    deps: OrchestratorDeps,
    market: str,
    symbols: list[str],
    date: str,
    reader: Any,
) -> dict[str, dict[str, Any]]:
    if deps.score_universe is None or not symbols:
        return {}
    try:
        raw = deps.score_universe(
            date=date, universe=symbols, data_reader=reader, market=market
        )
    except TypeError:
        try:
            raw = deps.score_universe(date, symbols, data_reader=reader, market=market)
        except TypeError:
            raw = deps.score_universe(date, symbols)
    return _normalize_batch_scores(raw)


def _score_symbols_with_batch(
    deps: OrchestratorDeps,
    market_adapter: MarketAdapter,
    market: str,
    universe: list[Any],
    date: str,
    reader: Any,
    *,
    max_candidates: int,
    errors: list[dict[str, Any]],
    stage_calls: list[str],
    audits: list[dict[str, Any]],
    account: str,
    capital_layer: str,
    account_type: str | None = None,
) -> dict[str, dict[str, Any]]:
    score_plan: list[tuple[str, str, str]] = []
    for symbol_value in universe[:max_candidates]:
        symbol = str(symbol_value)
        mapped_market, mapped_symbol = _safe_stage(
            "adapter.map_symbol_to_reader",
            errors,
            lambda symbol=symbol: market_adapter.map_symbol_to_reader(symbol),
            default=(market, symbol),
            capital_layer=capital_layer,
        )
        score_plan.append((symbol, str(mapped_market), str(mapped_symbol)))

    batch_scores: dict[tuple[str, str], dict[str, Any]] = {}
    if deps.score_universe is not None:
        grouped: dict[str, list[str]] = {}
        for _, mapped_market, mapped_symbol in score_plan:
            grouped.setdefault(mapped_market, []).append(mapped_symbol)
        for mapped_market, mapped_symbols in grouped.items():
            result = _safe_stage(
                "screening.six_dim_batch",
                errors,
                lambda mapped_market=mapped_market, mapped_symbols=mapped_symbols: (
                    _score_universe_for_market(
                        deps,
                        mapped_market,
                        mapped_symbols,
                        date,
                        reader,
                    )
                ),
                default={},
                capital_layer=capital_layer,
            )
            stage_calls.append("screening.six_dim_batch")
            if isinstance(result, dict):
                for mapped_symbol, score in result.items():
                    if isinstance(score, dict):
                        batch_scores[(mapped_market, str(mapped_symbol))] = dict(score)

    scores_by_symbol: dict[str, dict[str, Any]] = {}
    for symbol, mapped_market, mapped_symbol in score_plan:
        score = batch_scores.get((mapped_market, mapped_symbol))
        if score is None:
            score = _safe_stage(
                "screening.six_dim",
                errors,
                lambda mapped_market=mapped_market, mapped_symbol=mapped_symbol: (
                    _score_stock_for_market(
                        deps,
                        mapped_market,
                        mapped_symbol,
                        date,
                        reader,
                    )
                ),
                default={"combined": 0.5},
                capital_layer=capital_layer,
            )
            stage_calls.append("screening.six_dim")
        if not isinstance(score, dict):
            score = {"combined": 0.5}
        score["capital_layer"] = capital_layer
        if account_type:
            score["account_type"] = account_type
        score["market"] = market
        scores_by_symbol[symbol] = score
        payload = {"scores": score, "market": market, "capital_layer": capital_layer}
        metadata = {"date": date, "account": account}
        if account_type:
            metadata["account_type"] = account_type
        audits.append(
            _record_audit(
                deps,
                "signal",
                symbol,
                payload=payload,
                metadata=metadata,
                capital_layer=capital_layer,
            )
        )
    return scores_by_symbol


def _build_pool_for_market(
    deps: OrchestratorDeps,
    market: str,
    date: str,
    universe: list[str],
    reader: Any,
    scores_by_symbol: dict[str, dict[str, Any]] | None = None,
) -> Any:
    try:
        signature = inspect.signature(deps.build_pool)
    except (TypeError, ValueError):
        return deps.build_pool(
            date=date, universe=universe, market=market, reader=reader
        )
    kwargs: dict[str, Any] = {"date": date, "universe": universe}
    if "market" in signature.parameters:
        kwargs["market"] = market
    if "reader" in signature.parameters:
        kwargs["reader"] = reader
    if scores_by_symbol is not None and "scores_by_symbol" in signature.parameters:
        kwargs["scores_by_symbol"] = scores_by_symbol
    elif scores_by_symbol is not None and "scores_map" in signature.parameters:
        kwargs["scores_map"] = scores_by_symbol
    if (
        "market" in signature.parameters
        or "reader" in signature.parameters
        or "market_adapter" in signature.parameters
        or "scores_by_symbol" in signature.parameters
        or "scores_map" in signature.parameters
    ):
        return deps.build_pool(**kwargs)
    return deps.build_pool(date=date, universe=universe)


def _sim_no_trade_explanation(
    *,
    universe_count: int,
    candidate_count: int,
    order_count: int,
    portfolio_positions: int,
    filled_count: int,
    failed_count: int,
    pending_count: int,
    duplicate_count: int,
    skipped_candidates: list[dict[str, Any]],
    risk_rejections: list[dict[str, Any]],
    execution_skips: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    score_diagnostics: dict[str, Any] | None = None,
    candidate_layer_breakdown: dict[str, Any] | None = None,
    candidate_decision_trace: list[dict[str, Any]] | None = None,
    capital_plan_decision: dict[str, Any] | None = None,
    portfolio_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if filled_count > 0:
        category = "filled"
        action = "review_filled_receipts"
    elif universe_count <= 0:
        category = "no_universe"
        action = "check_sharedsignals_assets_and_daily_coverage"
    elif candidate_count <= 0:
        category = "no_candidates"
        action = "check_candidate_pool_thresholds_and_universe_filter"
    elif skipped_candidates and len(skipped_candidates) >= candidate_count:
        category = "all_candidates_missing_price"
        action = "check_sharedsignals_daily_or_realtime_prices"
    elif order_count <= 0 and risk_rejections:
        category = "all_rejected_by_risk"
        action = "review_risk_rejections"
    elif (
        order_count <= 0
        and candidate_count > 0
        and isinstance(capital_plan_decision, dict)
        and (
            _safe_int(capital_plan_decision.get("position_capacity"), 0) <= 0
            or _safe_int(capital_plan_decision.get("target_positions"), 0) <= 0
        )
    ):
        category = "capital_plan_defensive"
        action = "review_capital_plan_dynamic_profile"
    elif order_count <= 0:
        category = "no_portfolio_orders"
        action = "check_position_sizing_and_portfolio_constructor"
    elif portfolio_positions <= 0:
        category = "portfolio_empty"
        action = "check_capital_lot_size_and_constructor_output"
    elif duplicate_count > 0 and failed_count <= 0 and pending_count <= 0:
        category = "duplicate_existing_signal"
        action = "review_same_day_idempotency_state"
    elif execution_skips:
        category = "execution_skipped"
        action = "review_execution_skip_reasons"
    elif failed_count > 0:
        category = "execution_failed"
        action = "review_failed_receipts"
    elif pending_count > 0:
        category = "pending_execution"
        action = "review_pending_signal_state"
    elif errors:
        category = "degraded_errors"
        action = "review_orchestrator_errors"
    else:
        category = "no_filled_sim_orders"
        action = "review_full_sim_run"
    return {
        "category": category,
        "action": action,
        "counts": {
            "universe": universe_count,
            "candidates": candidate_count,
            "orders": order_count,
            "portfolio_positions": portfolio_positions,
            "filled": filled_count,
            "failed": failed_count,
            "pending": pending_count,
            "duplicates": duplicate_count,
            "skipped_candidates": len(skipped_candidates),
            "risk_rejections": len(risk_rejections),
            "execution_skips": len(execution_skips),
            "errors": len(errors),
        },
        "sample_skipped_candidates": skipped_candidates[:10],
        "sample_risk_rejections": risk_rejections[:10],
        "sample_execution_skips": execution_skips[:10],
        "sample_errors": errors[:5],
        "score_diagnostics": score_diagnostics or {},
        "candidate_layer_breakdown": candidate_layer_breakdown or {},
        "candidate_decision_trace": (candidate_decision_trace or [])[:10],
        "capital_plan_decision": capital_plan_decision or {},
        "portfolio_decision": portfolio_decision or {},
    }


def _candidate_layer_breakdown(
    pool: dict[str, Any], universe_count: int
) -> dict[str, int]:
    def _count(name: str) -> int:
        values = pool.get(name, []) if isinstance(pool, dict) else []
        return len(values or []) if isinstance(values, list) else 0

    return {
        "holdings": _count("holdings"),
        "watch": _count("watch"),
        "candidate": _count("candidate"),
        "fundamental": _count("fundamental"),
        "universe": universe_count,
    }


def _candidate_score_snapshot(symbol: str, score: dict[str, Any]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "symbol": symbol,
        "combined": round(_safe_float(score.get("combined"), 0.0), 4),
        "sector": str(score.get("sector", "unknown")),
    }
    if "evidence_coverage" in score:
        snapshot["evidence_coverage"] = round(
            _safe_float(score.get("evidence_coverage"), 0.0), 4
        )
    missing = score.get("missing_evidence_dimensions")
    if isinstance(missing, list):
        snapshot["missing_evidence_dimensions"] = [str(item) for item in missing[:6]]
    return snapshot


def _score_diagnostics(
    scores_by_symbol: dict[str, dict[str, Any]],
    *,
    limit: int = 10,
    actual_candidate_count: int | None = None,
) -> dict[str, Any]:
    dimensions = ("macro", "event", "fundamental", "capital", "technical", "sentiment")
    candidate_threshold = 0.55
    watch_threshold = 0.45
    rows: list[tuple[float, str, dict[str, Any]]] = []
    neutral_counts = {name: 0 for name in dimensions}
    missing_counts = {name: 0 for name in dimensions}
    missing_evidence_counts = {name: 0 for name in dimensions}
    missing_and_default_like_counts = {name: 0 for name in dimensions}
    evidence_reason_summary: dict[str, dict[str, int]] = {
        name: {} for name in dimensions
    }
    evidence_source_summary: dict[str, dict[str, int]] = {
        name: {} for name in dimensions
    }
    evidence_coverage_distribution = {
        "zero": 0,
        "low": 0,
        "medium": 0,
        "high": 0,
        "full": 0,
    }
    all_neutral_symbols: list[str] = []
    all_missing_evidence_symbols: list[str] = []
    all_missing_evidence_symbol_reasons: list[dict[str, Any]] = []
    evidence_coverage_values: list[float] = []
    batch_inactive_dimensions: set[str] = set()
    batch_evidence_availability: dict[str, float] = {}
    for symbol, score in scores_by_symbol.items():
        if not isinstance(score, dict):
            continue
        combined = _safe_float(score.get("combined"), 0.0)
        inactive_dimensions = score.get("batch_inactive_dimensions")
        if isinstance(inactive_dimensions, (list, tuple, set)):
            batch_inactive_dimensions.update(
                str(item) for item in inactive_dimensions if str(item)
            )
        availability = score.get("batch_evidence_availability")
        if isinstance(availability, dict):
            for name, value in availability.items():
                batch_evidence_availability[str(name)] = round(
                    _safe_float(value, 0.0), 4
                )
        neutral_dimensions = 0
        missing_evidence_dimensions = set(
            score.get("missing_evidence_dimensions") or []
        )
        evidence_sources = (
            score.get("evidence_sources")
            if isinstance(score.get("evidence_sources"), dict)
            else {}
        )
        for name in missing_evidence_dimensions:
            if name in missing_evidence_counts:
                missing_evidence_counts[name] += 1
        for name in dimensions:
            info = (
                evidence_sources.get(name)
                if isinstance(evidence_sources, dict)
                else None
            )
            if not isinstance(info, dict):
                continue
            if info.get("has_evidence") is False:
                missing_evidence_counts[name] += (
                    1 if name not in missing_evidence_dimensions else 0
                )
                reason = str(info.get("reason") or "missing_evidence")
                source = str(info.get("source") or "unknown")
                evidence_reason_summary[name][reason] = (
                    evidence_reason_summary[name].get(reason, 0) + 1
                )
                evidence_source_summary[name][source] = (
                    evidence_source_summary[name].get(source, 0) + 1
                )
        all_evidence_missing = len(missing_evidence_dimensions) >= len(dimensions)
        if isinstance(evidence_sources, dict) and evidence_sources:
            all_evidence_missing = all(
                isinstance(evidence_sources.get(name), dict)
                and evidence_sources[name].get("has_evidence") is False
                for name in dimensions
            )
        if all_evidence_missing:
            all_missing_evidence_symbols.append(str(symbol))
            if len(all_missing_evidence_symbol_reasons) < max(1, limit):
                reasons = {}
                for name in dimensions:
                    info = (
                        evidence_sources.get(name)
                        if isinstance(evidence_sources, dict)
                        else None
                    )
                    if isinstance(info, dict) and info.get("has_evidence") is False:
                        reasons[name] = info.get("reason") or "missing_evidence"
                    elif name in missing_evidence_dimensions:
                        reasons[name] = "missing_evidence"
                all_missing_evidence_symbol_reasons.append(
                    {"symbol": str(symbol), "reasons": reasons}
                )
        if "evidence_coverage" in score:
            coverage = _safe_float(score.get("evidence_coverage"), 0.0)
            evidence_coverage_values.append(coverage)
            if coverage <= 0:
                evidence_coverage_distribution["zero"] += 1
            elif coverage < 0.34:
                evidence_coverage_distribution["low"] += 1
            elif coverage < 0.67:
                evidence_coverage_distribution["medium"] += 1
            elif coverage < 1.0:
                evidence_coverage_distribution["high"] += 1
            else:
                evidence_coverage_distribution["full"] += 1
        for name in dimensions:
            if name not in score:
                missing_counts[name] += 1
                continue
            value = _safe_float(score.get(name), 0.5)
            if 0.49 <= value <= 0.51:
                neutral_counts[name] += 1
                neutral_dimensions += 1
                source_info = (
                    evidence_sources.get(name)
                    if isinstance(evidence_sources, dict)
                    else None
                )
                if name in missing_evidence_dimensions or (
                    isinstance(source_info, dict)
                    and source_info.get("has_evidence") is False
                ):
                    missing_and_default_like_counts[name] += 1
        if neutral_dimensions == len(dimensions):
            all_neutral_symbols.append(str(symbol))
        rows.append((combined, str(symbol), score))
    rows.sort(key=lambda item: item[0], reverse=True)
    top_scores = []
    for combined, symbol, score in rows[: max(1, limit)]:
        row = {"symbol": symbol, "combined": round(combined, 4)}
        for name in dimensions:
            if name in score:
                row[name] = round(_safe_float(score.get(name), 0.5), 4)
        top_scores.append(row)
    candidate_count = sum(
        1 for combined, _, _ in rows if combined >= candidate_threshold
    )
    watch_count = sum(
        1
        for combined, _, _ in rows
        if watch_threshold <= combined < candidate_threshold
    )
    neutral_total = sum(neutral_counts.values())
    neutral_ratio = neutral_total / max(1, len(rows) * len(dimensions))
    all_neutral_ratio = len(all_neutral_symbols) / max(1, len(rows))
    all_missing_evidence_ratio = len(all_missing_evidence_symbols) / max(1, len(rows))
    missing_default_like_total = sum(missing_and_default_like_counts.values())
    missing_default_like_ratio = missing_default_like_total / max(
        1, len(rows) * len(dimensions)
    )
    avg_evidence_coverage = sum(evidence_coverage_values) / max(
        1, len(evidence_coverage_values)
    )
    if not rows:
        candidate_pool_status = "no_scored_symbols"
    elif actual_candidate_count == 0 and candidate_count > 0:
        candidate_pool_status = "pool_empty_despite_threshold_scores"
    elif candidate_count > 0:
        candidate_pool_status = "candidates_ready"
    elif watch_count > 0:
        candidate_pool_status = "strategy_threshold_not_met_watch_only"
    else:
        candidate_pool_status = "strategy_threshold_not_met"
    return {
        "scored_count": len(rows),
        "candidate_threshold": candidate_threshold,
        "watch_threshold": watch_threshold,
        "candidate_above_threshold_count": candidate_count,
        "actual_candidate_count": actual_candidate_count,
        "watch_above_threshold_count": watch_count,
        "max_combined": round(rows[0][0], 4) if rows else 0.0,
        "candidate_pool_status": candidate_pool_status,
        "neutral_dimension_ratio": round(neutral_ratio, 4),
        "all_neutral_symbol_count": len(all_neutral_symbols),
        "all_neutral_symbol_ratio": round(all_neutral_ratio, 4),
        "data_quality_status": (
            "missing_evidence_default_like"
            if rows
            and (
                all_missing_evidence_ratio >= 0.5
                or missing_default_like_ratio >= 0.5
                or all_neutral_ratio >= 0.5
            )
            else "research_dimensions_mostly_neutral"
            if rows and neutral_ratio >= 0.75
            else "ok"
        ),
        "average_evidence_coverage": round(avg_evidence_coverage, 4),
        "evidence_coverage_distribution": evidence_coverage_distribution,
        "all_missing_evidence_symbol_count": len(all_missing_evidence_symbols),
        "all_missing_evidence_symbol_ratio": round(all_missing_evidence_ratio, 4),
        "all_missing_evidence_symbol_sample": all_missing_evidence_symbols[
            : max(1, limit)
        ],
        "all_missing_evidence_symbol_reason_sample": all_missing_evidence_symbol_reasons,
        "top_scores": top_scores,
        "all_neutral_symbol_sample": all_neutral_symbols[: max(1, limit)],
        "neutral_default_like_dimension_counts": neutral_counts,
        "missing_dimension_counts": missing_counts,
        "missing_evidence_dimension_counts": missing_evidence_counts,
        "missing_and_default_like_dimension_counts": missing_and_default_like_counts,
        "missing_and_default_like_dimension_ratio": round(
            missing_default_like_ratio, 4
        ),
        "evidence_reason_summary": {
            name: dict(counts)
            for name, counts in evidence_reason_summary.items()
            if counts
        },
        "evidence_source_summary": {
            name: dict(counts)
            for name, counts in evidence_source_summary.items()
            if counts
        },
        "batch_inactive_dimensions": sorted(batch_inactive_dimensions),
        "batch_evidence_availability": batch_evidence_availability,
    }


def _run_condition_lifecycle(
    market: str,
    pool: dict[str, Any],
    scores_by_symbol: dict[str, dict[str, Any]],
    date: str,
    reader: Any,
) -> dict[str, Any]:
    from shared.screening.condition_generator import generate_conditions
    from shared.screening.condition_monitor import trigger_replay

    conditions = generate_conditions(
        pool=pool, scores_map=scores_by_symbol, date=date, reader=reader, market=market
    )
    replay = trigger_replay(conditions, date=date)
    return {
        "condition_count": len(conditions),
        "trigger_replay_count": len(replay),
        "filled_replay_count": sum(1 for row in replay if row.get("replay_fillable")),
        "conditions": conditions,
        "trigger_replay": replay,
    }


def run_shadow_loop(
    market_adapter: MarketAdapter,
    date: str,
    reader: Any,
    *,
    deps: OrchestratorDeps | None = None,
    signals_dir: Path = SIGNALS_DIR,
) -> dict[str, Any]:
    """Run screening -> debate -> risk -> portfolio -> shadow execution -> review."""

    deps = deps or _default_deps()
    errors: list[dict[str, Any]] = []
    stage_calls: list[str] = []
    audits: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    market = _safe_stage(
        "adapter.get_market", errors, market_adapter.get_market, default="unknown"
    )
    account = _safe_stage(
        "adapter.get_shadow_account",
        errors,
        market_adapter.get_shadow_account,
        default=f"{market}_shadow",
    )
    config = _strategy_config(market_adapter)
    is_ashare = str(market).lower() == "ashare"
    shadow_position_authority: dict[str, Any] = {
        "status": "not_applicable",
        "reason": "non_ashare_market",
        "source_audit": [],
        "mismatches": [],
        "positions": [],
    }
    if is_ashare:
        shadow_position_authority = _resolve_ashare_position_authority_for_entry(
            market_adapter,
            date,
            errors=errors,
            stage_calls=stage_calls,
            stage_prefix="capital.ashare_shadow_position_authority",
            capital_layer="shadow",
        )
    shadow_position_authority_reason = str(
        shadow_position_authority.get("reason")
        or "ashare_capital_position_authority_invalid"
    )
    shadow_new_risk_allowed = not is_ashare or (
        shadow_position_authority.get("status") == "verified"
        and shadow_position_authority.get("new_risk_allowed") is True
    )
    shadow_new_risk_reason = str(
        shadow_position_authority.get("new_risk_reason")
        or shadow_position_authority_reason
    )
    capital = _safe_float(
        config.get("shadow_capital"), _default_capital_for_market(market)
    )
    method = str(config.get("portfolio_method", "conviction_weighted"))
    regime = str(config.get("regime", "unknown"))
    max_candidates = max(1, int(config.get("max_candidates", 20)))
    score_limit = max(
        max_candidates, int(config.get("score_universe_limit", max_candidates))
    )
    default_price = _safe_float(config.get("default_price"), 1.0)
    default_volatility = _safe_float(config.get("default_volatility"), 0.20)
    market_rules = (
        config.get("market_rules")
        if isinstance(config.get("market_rules"), dict)
        else {}
    )
    lot_size = (
        _safe_float(market_rules.get("lot_size"), 1.0)
        if isinstance(market_rules, dict)
        else 1.0
    )

    universe = _safe_stage(
        "screening.universe",
        errors,
        lambda: market_adapter.get_universe(date),
        default=[],
    )
    stage_calls.append("screening.universe")
    if not isinstance(universe, list):
        errors.append(
            {
                "stage": "screening.universe",
                "status": "degraded",
                "error": "adapter returned non-list",
                "capital_layer": "shadow",
            }
        )
        universe = []

    scores_by_symbol = _score_symbols_with_batch(
        deps,
        market_adapter,
        market,
        universe,
        date,
        reader,
        max_candidates=score_limit,
        errors=errors,
        stage_calls=stage_calls,
        audits=audits,
        account=account,
        capital_layer="shadow",
    )

    pool = _safe_stage(
        "screening.candidate_pool",
        errors,
        lambda: _build_pool_for_market(
            deps, market, date, list(scores_by_symbol), reader, scores_by_symbol
        ),
        default=_candidate_pool_default(market, "shadow", list(scores_by_symbol)),
    )
    stage_calls.append("screening.candidate_pool")
    if not isinstance(pool, dict):
        pool = _candidate_pool_default(market, "shadow", list(scores_by_symbol))
    condition_lifecycle = _safe_stage(
        "screening.condition_lifecycle",
        errors,
        lambda: _run_condition_lifecycle(market, pool, scores_by_symbol, date, reader),
        default={
            "condition_count": 0,
            "trigger_replay_count": 0,
            "filled_replay_count": 0,
            "conditions": [],
            "trigger_replay": [],
        },
    )
    stage_calls.append("screening.condition_lifecycle")

    candidates = _rank_symbols_by_score(
        _candidate_symbols(
            pool, list(scores_by_symbol), market=market, capital_layer="shadow"
        ),
        scores_by_symbol,
    )[:max_candidates]
    orders_for_portfolio: list[dict[str, Any]] = []
    skipped_candidates: list[dict[str, Any]] = []
    signal_audit_by_symbol = {
        audit["ts_code"]: audit for audit in audits if audit.get("stage") == "signal"
    }

    for symbol in candidates:
        mapped_market, mapped_symbol = _safe_stage(
            "adapter.map_symbol_to_reader",
            errors,
            lambda symbol=symbol: market_adapter.map_symbol_to_reader(symbol),
            default=(market, symbol),
        )
        score = scores_by_symbol.get(
            symbol,
            {"combined": 0.5, "market": mapped_market, "capital_layer": "shadow"},
        )
        parent = signal_audit_by_symbol.get(symbol, {}).get("audit_id", "")
        debate = _safe_stage(
            "adversarial.bull_bear_debate",
            errors,
            lambda symbol=mapped_symbol, score=score: deps.debate(symbol, score),
            default={
                "ts_code": mapped_symbol,
                "belief_score": 0.5,
                "bull_case": "degraded",
                "bear_case": "degraded",
            },
        )
        stage_calls.append("adversarial.bull_bear_debate")
        if not isinstance(debate, dict):
            debate = {"belief_score": 0.5}
        decision_audit = _record_audit(
            deps,
            "decision",
            symbol,
            parent_audit_id=parent,
            payload={"debate": debate, "capital_layer": "shadow"},
            metadata={"date": date, "account": account},
        )
        audits.append(decision_audit)

        price = _latest_price(reader, mapped_market, mapped_symbol, date, default_price)
        volatility = _latest_volatility(
            reader, mapped_market, mapped_symbol, date, default_volatility
        )
        if price <= 0:
            skipped_candidates.append(
                {
                    "symbol": symbol,
                    "reason": "missing_or_non_positive_price",
                    "price": price,
                    "capital_layer": "shadow",
                }
            )
            continue
        proposed_weight = _safe_stage(
            "portfolio.position_sizer",
            errors,
            lambda debate=debate, volatility=volatility: deps.size_position(
                _safe_float(debate.get("belief_score"), 0.5), volatility, regime
            ),
            default=0.0,
        )
        stage_calls.append("portfolio.position_sizer")
        risk_order = {
            "ts_code": symbol,
            "weight": proposed_weight,
            "sector": str(score.get("sector", "unknown")),
            "turnover_wan": _safe_float(score.get("turnover_wan"), 0.0),
            "capital_layer": "shadow",
        }
        if is_ashare and shadow_position_authority.get("status") != "verified":
            risk = {
                "approved": False,
                "adjusted_weight": 0.0,
                "reasons": [shadow_position_authority_reason],
                "reason_code": shadow_position_authority_reason,
                "position_authority_status": "blocked",
                "position_source_audit": shadow_position_authority.get(
                    "source_audit", []
                ),
                "position_source_mismatches": shadow_position_authority.get(
                    "mismatches", []
                ),
            }
            stage_calls.append("capital.ashare_shadow_position_authority_gate")
        elif is_ashare and not shadow_new_risk_allowed:
            risk = {
                "approved": False,
                "adjusted_weight": 0.0,
                "reasons": [shadow_new_risk_reason],
                "reason_code": shadow_new_risk_reason,
                "position_authority_status": "verified",
                "new_risk_allowed": False,
                "position_source_audit": shadow_position_authority.get(
                    "source_audit", []
                ),
            }
            stage_calls.append("capital.ashare_shadow_new_risk_gate")
        else:
            risk = _safe_stage(
                "risk.pre_trade_check",
                errors,
                lambda: deps.risk_check(
                    risk_order,
                    {
                        "positions": shadow_position_authority.get("positions", [])
                        if is_ashare
                        else [],
                        "total_exposure": _safe_float(
                            shadow_position_authority.get("capital_total_exposure"),
                            0.0,
                        )
                        if is_ashare
                        else 0.0,
                    },
                ),
                default={
                    "approved": False,
                    "adjusted_weight": 0.0,
                    "reasons": ["degraded"],
                },
            )
            stage_calls.append("risk.pre_trade_check")
        if not isinstance(risk, dict):
            risk = {
                "approved": False,
                "adjusted_weight": 0.0,
                "reasons": ["invalid risk result"],
            }
        risk_audit = _record_audit(
            deps,
            "risk",
            symbol,
            parent_audit_id=decision_audit["audit_id"],
            payload=risk,
            metadata={"date": date, "account": account},
        )
        audits.append(risk_audit)
        if (
            not risk.get("approved")
            or _safe_float(risk.get("adjusted_weight"), 0.0) <= 0
        ):
            continue
        orders_for_portfolio.append(
            {
                "ts_code": symbol,
                "belief_score": _safe_float(debate.get("belief_score"), 0.5),
                "volatility": volatility,
                "sector": str(score.get("sector", "unknown")),
                "price": price,
                "weight": _safe_float(risk.get("adjusted_weight"), proposed_weight),
                "lot_size": lot_size,
                "risk_audit_id": risk_audit["audit_id"],
                "mapped_market": mapped_market,
                "mapped_symbol": mapped_symbol,
            }
        )

    portfolio = _safe_stage(
        "portfolio.constructor",
        errors,
        lambda: deps.construct(
            orders_for_portfolio, capital, method=method, regime=regime
        ),
        default={"positions": [], "total_weight": 0.0, "cash_weight": 1.0},
    )
    stage_calls.append("portfolio.constructor")
    if not isinstance(portfolio, dict):
        portfolio = {"positions": [], "total_weight": 0.0, "cash_weight": 1.0}

    order_meta = {order["ts_code"]: order for order in orders_for_portfolio}
    for position in portfolio.get("positions", []) or []:
        if not isinstance(position, dict) or not position.get("ts_code"):
            continue
        symbol = str(position["ts_code"])
        meta = order_meta.get(symbol, {})
        order = {
            "ts_code": symbol,
            "side": "buy",
            "quantity": _safe_quantity(position.get("shares"), 0),
            "price": _safe_float(position.get("price"), 0.0),
            "trade_date": date,
            "market": market,
            "capital_layer": "shadow",
            "note": f"orchestrator shadow loop {market} {date}",
        }
        if order["quantity"] <= 0 or order["price"] <= 0:
            errors.append(
                {
                    "stage": "execution.shadow_broker",
                    "status": "skipped",
                    "symbol": symbol,
                    "reason": "non-positive quantity or price",
                    "capital_layer": "shadow",
                }
            )
            continue
        trade = _safe_stage(
            "execution.shadow_broker",
            errors,
            lambda order=order: deps.record_shadow(order, account),
            default={"recorded": False, "status": "degraded"},
        )
        stage_calls.append("execution.shadow_broker")
        if not isinstance(trade, dict):
            trade = {"recorded": False, "status": "invalid"}
        execution_audit = _record_audit(
            deps,
            "execution",
            symbol,
            parent_audit_id=str(meta.get("risk_audit_id", "")),
            payload={"order": order, "shadow_broker": trade, "capital_layer": "shadow"},
            metadata={"date": date, "account": account},
        )
        audits.append(execution_audit)
        risk = {
            "approved": True,
            "adjusted_weight": position.get("weight"),
            "adjustments": [],
            "reasons": [],
        }
        card = _build_signal_card(
            market=market,
            symbol=symbol,
            account=account,
            date=date,
            order=order,
            risk=risk,
            trade=trade,
            audit_id=execution_audit["audit_id"],
        )
        pending = _safe_stage(
            "signals.pending",
            errors,
            lambda card=card: _write_pending_signal(card, signals_dir),
            default={"status": "degraded", "recorded": False},
        )
        stage_calls.append("signals.pending")
        email_notification = {
            "status": "skipped",
            "reason": "signal not newly pending",
            "template": "trading_signal",
        }
        if pending.get("status") == "pending":
            email_data = _trading_signal_email_data(
                market=market,
                symbol=symbol,
                date=date,
                account=account,
                order=order,
                position=position,
                score=scores_by_symbol.get(symbol, {}),
                risk=risk,
                card=card,
            )
            email_notification = _safe_stage(
                "notify.trading_signal",
                errors,
                lambda data=email_data: _send_template_email_now(
                    deps.send_email,
                    "trading_signal",
                    data,
                    subject=f"tradingagent 影子盘新信号 {symbol} {_date_iso(date)}",
                ),
                default={"status": "degraded", "template": "trading_signal"},
            )
            stage_calls.append("notify.trading_signal")
        result_audit = _record_audit(
            deps,
            "result",
            symbol,
            parent_audit_id=execution_audit["audit_id"],
            payload={
                "pending_signal": pending,
                "email_notification": email_notification,
                "capital_layer": "shadow",
            },
            metadata={"date": date, "account": account},
        )
        audits.append(result_audit)
        records.append(
            {
                "symbol": symbol,
                "order": order,
                "trade": trade,
                "pending_signal": pending,
                "email_notification": email_notification,
            }
        )

    review = _safe_stage(
        "review.daily_review",
        errors,
        lambda: deps.run_review(date, session="close"),
        default={"session": "close", "error": "degraded", "trade_date": date},
    )
    stage_calls.append("review.daily_review")

    return {
        "market": market,
        "date": date,
        "capital_layer": "shadow",
        "account": account,
        "state": (
            "degraded"
            if errors
            or (is_ashare and shadow_position_authority.get("status") != "verified")
            else "ok"
        ),
        "stage_calls": stage_calls,
        "universe_count": len(universe),
        "candidate_count": len(candidates),
        "order_count": len(orders_for_portfolio),
        "skipped_candidate_count": len(skipped_candidates),
        "skipped_candidates": skipped_candidates[:20],
        "recorded_count": sum(
            1 for record in records if record["trade"].get("recorded")
        ),
        "portfolio": portfolio,
        "records": records,
        "audit_events": audits,
        "errors": errors,
        "review": review,
        "condition_lifecycle": condition_lifecycle,
        "ashare_position_authority": shadow_position_authority,
        "ashare_position_authority_reason": shadow_position_authority_reason,
        "generated_at": _now_iso(),
    }


def _ashare_sample_debt(sample_adjustment: Any) -> bool:
    """Return debt established by the current SampleJournal authority."""

    if not isinstance(sample_adjustment, dict):
        return False
    if isinstance(sample_adjustment.get("sample_debt"), bool):
        return bool(sample_adjustment["sample_debt"])
    if sample_adjustment.get("sample_authority_reliable") is not True:
        return False
    count = _strict_finite_number(sample_adjustment.get("strategy_sample_valid_count"))
    minimum = _strict_finite_number(sample_adjustment.get("min_strategy_samples"))
    return bool(
        count is not None
        and minimum is not None
        and count >= 0.0
        and minimum > 0.0
        and count < minimum
    )


def _ashare_sample_journal_path(signals_dir: Path) -> Path:
    return signals_dir.parent / "shared" / "review" / "ashare" / "sample_journal.jsonl"


def _build_and_persist_ashare_observations(
    *,
    market_adapter: MarketAdapter,
    reader: Any,
    date: str,
    scores_by_symbol: dict[str, dict[str, Any]],
    config: dict[str, Any],
    journal_path: Path,
) -> dict[str, Any]:
    from Ashare.sample_pipeline import (
        build_candidate_observation,
        persist_candidate_observations,
    )

    style_states = config.get("style_states")
    if not isinstance(style_states, dict):
        style_states = None
    mg_value = config.get("marketgraph_enabled", False)
    mg_enabled = mg_value is True or str(mg_value).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    prediction_at = _now_iso()
    observations: list[dict[str, Any]] = []
    for symbol in sorted(scores_by_symbol):
        mapped_market, mapped_symbol = market_adapter.map_symbol_to_reader(symbol)
        observations.append(
            build_candidate_observation(
                symbol=symbol,
                trade_date=date,
                mapped_market=str(mapped_market),
                mapped_symbol=str(mapped_symbol),
                score=scores_by_symbol[symbol],
                reader=reader,
                prediction_at=prediction_at,
                mg_enabled=mg_enabled,
                style_states=style_states,
            )
        )
    persistence = persist_candidate_observations(
        observations,
        journal_path=journal_path,
    )
    return {
        "observations": observations,
        "observation_by_symbol": {
            str(row.get("symbol") or ""): row for row in observations
        },
        "persistence": persistence,
        "real_trading_enabled": False,
    }


def _ashare_exploration_fill_count(journal_path: Path, trade_date: str) -> int:
    from shared.execution.execution_lineage import (
        ASHARE_AUTHORITY_GENERATION,
        ASHARE_CAPITAL_AUTHORITY_ID,
        ASHARE_EXECUTION_LINEAGE_ID,
    )
    from shared.review.sample_journal import SampleJournal

    date_key = _compact_date_key(trade_date)
    return sum(
        1
        for row in SampleJournal(journal_path).read_events()
        if str(row.get("record_type") or "").lower() == "fill"
        and str(row.get("sample_intent") or "").lower() == "exploration"
        and row.get("execution_eligible") is True
        and str(row.get("capital_authority_id") or "") == ASHARE_CAPITAL_AUTHORITY_ID
        and row.get("authority_generation") == ASHARE_AUTHORITY_GENERATION
        and str(row.get("execution_lineage_id") or "") == ASHARE_EXECUTION_LINEAGE_ID
        and _compact_date_key(row.get("trade_date")) == date_key
    )


def _ashare_order_attribution(
    observation: Any,
    *,
    sample_intent: str,
    account: str,
    exploration_selection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from Ashare.sample_pipeline import execution_attribution

    if isinstance(observation, dict):
        attribution = execution_attribution(
            observation,
            sample_intent=sample_intent,
            selection=(
                exploration_selection if sample_intent == "exploration" else None
            ),
        )
    else:
        attribution = {
            "sample_intent": sample_intent,
            "primary_style": None,
            "supporting_styles": [],
            "style_scores": {},
            "style_versions": {},
            "decision_policy_version": "ashare-single-portfolio-intent-v1",
            "style_disagreement": {},
            "capital_authority": "ashare-capital-v1",
            "real_trading_enabled": False,
        }
    if not attribution.get("primary_style"):
        # The pre-existing six-dimension policy remains the exploitation
        # champion while all newly introduced orthogonal styles are shadow
        # challengers.  This is explicit attribution, not a fifth capital pool.
        attribution = dict(attribution)
        attribution["primary_style"] = "legacy_six_dimension_champion"
        attribution["style_scores"] = {
            **dict(attribution.get("style_scores") or {}),
            "legacy_six_dimension_champion": None,
        }
        attribution["style_versions"] = {
            **dict(attribution.get("style_versions") or {}),
            "legacy_six_dimension_champion": str(account or "ashare_sim"),
        }
    return attribution


def _persist_ashare_sample_outcomes(
    *,
    journal_path: Path,
    trade_date: str,
    records: list[dict[str, Any]],
    risk_rejections: list[dict[str, Any]],
    authoritative_account_view: dict[str, Any],
    ashare_capital_state: dict[str, Any] | None,
) -> dict[str, Any]:
    from Ashare.sample_pipeline import persist_simulation_outcomes
    from shared.review.sample_journal import SampleJournal

    outcome = persist_simulation_outcomes(
        journal_path=journal_path,
        trade_date=trade_date,
        records=records,
        risk_rejections=risk_rejections,
    )
    exposure = round(
        sum(
            max(
                0.0,
                _safe_float(
                    row.get("market_value", row.get("value", row.get("amount"))),
                    0.0,
                ),
            )
            for row in (authoritative_account_view.get("positions") or [])
            if isinstance(row, dict)
        ),
        2,
    )
    total_risk = exposure
    if isinstance(ashare_capital_state, dict):
        total_risk = round(
            total_risk
            + _safe_float(ashare_capital_state.get("active_reservations_cny"), 0.0),
            2,
        )
    portfolio_snapshot = {
        "source": str(
            authoritative_account_view.get("source") or "server_local_sim_ledger"
        ),
        "as_of": _now_iso(),
        "market": "ashare",
        "capital_authority_id": str(
            (ashare_capital_state or {}).get("authority_id") or ""
        ),
        "authority_generation": (ashare_capital_state or {}).get(
            "authority_generation"
        ),
        "execution_lineage_id": str(
            (ashare_capital_state or {}).get("execution_lineage_id") or ""
        ),
        "account_equity_cny": _safe_float(
            (ashare_capital_state or {}).get("equity_cny"), 0.0
        ),
        "total_risk_cny": round(total_risk, 2),
        "gross_exposure_cny": exposure,
        "real_trading_enabled": False,
    }
    kpi = SampleJournal(journal_path).build_kpi(portfolio_snapshot=portfolio_snapshot)
    return {
        **outcome,
        "kpi": kpi,
        "portfolio_snapshot": portfolio_snapshot,
        "real_trading_enabled": False,
    }


def run_sim_loop(
    market_adapter: MarketAdapter,
    date: str,
    reader: Any,
    *,
    deps: OrchestratorDeps | None = None,
    signals_dir: Path = SIGNALS_DIR,
) -> dict[str, Any]:
    """Run screening -> debate -> simulated risk/portfolio/execution -> review."""

    deps = deps or _default_deps()
    errors: list[dict[str, Any]] = []
    stage_calls: list[str] = []
    audits: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    capital_layer = "simulated"
    account_type = "simulated"
    market = _safe_stage(
        "adapter.get_market",
        errors,
        market_adapter.get_market,
        default="unknown",
        capital_layer=capital_layer,
    )
    config = _strategy_config(market_adapter)
    sample_journal_path = _ashare_sample_journal_path(signals_dir)
    is_ashare = str(market).lower() == "ashare"
    sim_account_getter = getattr(market_adapter, "get_sim_account", None)
    if is_ashare:
        # The adapter may read stale/legacy position files.  Do not invoke it
        # until the market-capital authority has passed its own strict gate.
        account_obj: Any = {"account": "ashare_sim"}
        account = "ashare_sim"
        adapter_positions: list[dict[str, Any]] = []
        adapter_capital = 50_000.0
        adapter_cash_available = 0.0
    else:
        if callable(sim_account_getter):
            account_obj = _safe_stage(
                "adapter.get_sim_account",
                errors,
                sim_account_getter,
                default={"account": f"{market}_simulated"},
                capital_layer=capital_layer,
            )
        else:
            account_obj = _safe_stage(
                "adapter.get_shadow_account",
                errors,
                market_adapter.get_shadow_account,
                default=f"{market}_simulated",
                capital_layer=capital_layer,
            )
        account = _account_name(account_obj, f"{market}_simulated")
        adapter_positions = _account_positions(account_obj, config)
        adapter_capital = _account_capital(account_obj, config)
        adapter_cash_available = _account_available_cash(
            account_obj,
            config,
            adapter_capital,
            adapter_positions,
        )
    existing_positions = adapter_positions
    capital = adapter_capital
    account_cash_available = adapter_cash_available
    strategy_positions = existing_positions
    strategy_cash_available = account_cash_available
    capital_plan_sample_adjustment: dict[str, Any] = {}
    authoritative_account_view: dict[str, Any] = {
        "source": "adapter",
        "capital_cny": capital,
        "cash_available": account_cash_available,
        "positions": existing_positions,
    }
    ashare_capital_outbox = {
        "status": "skipped",
        "reason": "non_ashare_market",
        "action_count": 0,
        "pending_count": 0,
    }
    ashare_capital_state: dict[str, Any] | None = None
    ashare_capital_state_reason = "non_ashare_market"
    ashare_position_authority: dict[str, Any] = {
        "status": "not_applicable",
        "reason": "non_ashare_market",
        "source_audit": [],
        "mismatches": [],
        "positions": [],
    }
    ashare_position_authority_reason = "non_ashare_market"
    ashare_new_risk_allowed = not is_ashare
    ashare_new_risk_reason = "non_ashare_market"
    local_exploration_state: dict[str, Any] = {
        "new_position_count": 0,
        "open_exposure_cny": 0.0,
        "daily_realized_pnl_cny": 0.0,
        "daily_loss_cny": 0.0,
        "real_trading_enabled": False,
    }
    if is_ashare:
        ashare_capital_outbox = _safe_stage(
            "capital.ashare_market_outbox_replay",
            errors,
            lambda: _dispatch_ashare_market_outbox("ashare_sim"),
            default={
                "status": "outbox_unavailable",
                "action_count": 0,
                "pending_count": 0,
            },
            capital_layer=capital_layer,
        )
        stage_calls.append("capital.ashare_market_outbox_replay")
        raw_ashare_capital_state = _safe_stage(
            "capital.ashare_market_state_before_positions",
            errors,
            lambda: market_capital.load_market_capital_provider_state("ashare", date),
            default=None,
            capital_layer=capital_layer,
        )
        stage_calls.append("capital.ashare_market_state_before_positions")
        ashare_capital_state, ashare_capital_state_reason = (
            _validate_ashare_position_capital_state(raw_ashare_capital_state, date)
        )
        authority_before = build_ashare_capital_position_authority_view(
            raw_ashare_capital_state, date
        )
        authority_status = authority_before.get("status")
        if ashare_capital_state is None or authority_status != "verified":
            capital_gate_reason = (
                str(
                    authority_before.get("reason")
                    if authority_status != "verified"
                    else ashare_capital_state_reason
                )
                or "ashare_capital_state_invalid"
            )
            ashare_capital_state = None
            ashare_capital_state_reason = capital_gate_reason
            ashare_position_authority = {
                **authority_before,
                "status": "blocked",
                "reason": capital_gate_reason,
                "source_audit": [
                    ashare_capital_state_audit(
                        raw_ashare_capital_state,
                        authority_before,
                        source_name="market_capital_before",
                    )
                ],
                "mismatches": [],
                "positions": [],
            }
        else:
            ashare_position_authority = {
                **authority_before,
                "source_audit": [
                    ashare_capital_state_audit(
                        raw_ashare_capital_state,
                        authority_before,
                        source_name="market_capital_before",
                    )
                ],
                "mismatches": [],
                "positions": [],
            }

        if ashare_capital_state is not None:
            if callable(sim_account_getter):
                account_obj = _safe_stage(
                    "adapter.get_sim_account_after_capital_gate",
                    errors,
                    lambda: _load_sim_account_for_trade_date(
                        sim_account_getter,
                        date,
                        position_authority=authority_before,
                    ),
                    default={
                        "account": "ashare_sim",
                        "positions": [],
                        "position_source_status": "blocked",
                    },
                    capital_layer=capital_layer,
                )
            else:
                account_obj = _safe_stage(
                    "adapter.get_shadow_account_after_capital_gate",
                    errors,
                    market_adapter.get_shadow_account,
                    default={
                        "account": "ashare_sim",
                        "positions": [],
                        "position_source_status": "blocked",
                    },
                    capital_layer=capital_layer,
                )
            account = _account_name(account_obj, "ashare_sim")
            adapter_positions = _account_positions(account_obj, config)
            adapter_capital = _account_capital(account_obj, config)
            adapter_cash_available = _account_available_cash(
                account_obj,
                config,
                adapter_capital,
                adapter_positions,
            )
            if account != "ashare_sim":
                ashare_position_authority = {
                    **authority_before,
                    "status": "blocked",
                    "reason": "ashare_authoritative_account_must_be_ashare_sim",
                    "source_audit": ashare_position_authority.get("source_audit", []),
                    "mismatches": [],
                    "positions": [],
                }
                errors.append(
                    {
                        "stage": "capital.ashare_account_authority",
                        "status": "blocked",
                        "error": "ashare_authoritative_account_must_be_ashare_sim",
                        "capital_layer": capital_layer,
                    }
                )
            else:
                local_account_view = _safe_stage(
                    "capital.ashare_authoritative_account",
                    errors,
                    lambda: _ashare_authoritative_account_view(
                        account_obj, date, position_authority=authority_before
                    ),
                    default={
                        "account": account,
                        "capital_cny": 50_000.0,
                        "cash_available": 0.0,
                        "positions": [],
                        "source": "server_local_sim_ledger_unavailable",
                        "trade_date": _compact_date_key(date),
                        "position_source_status": "blocked",
                        "real_trading_enabled": False,
                    },
                    capital_layer=capital_layer,
                )
                stage_calls.append("capital.ashare_authoritative_account")
                position_sources = _ashare_position_sources_from_account(
                    account_obj, local_account_view
                )

                raw_ashare_capital_state_after = _safe_stage(
                    "capital.ashare_market_state_after_positions",
                    errors,
                    lambda: market_capital.load_market_capital_provider_state(
                        "ashare", date
                    ),
                    default=None,
                    capital_layer=capital_layer,
                )
                stage_calls.append("capital.ashare_market_state_after_positions")
                final_validated_state, final_state_reason = (
                    _validate_ashare_position_capital_state(
                        raw_ashare_capital_state_after, date
                    )
                )
                ashare_position_authority = reconcile_ashare_position_sources(
                    raw_ashare_capital_state,
                    date,
                    sources=position_sources,
                    preferred_source="server_local",
                    final_capital_state=raw_ashare_capital_state_after,
                )
                if (
                    final_validated_state is None
                    and ashare_position_authority.get("status") == "verified"
                ):
                    ashare_position_authority = {
                        **ashare_position_authority,
                        "status": "blocked",
                        "reason": CAPITAL_POSITION_SOURCE_MISMATCH,
                        "positions": [],
                        "mismatches": [
                            {
                                "source_name": "market_capital_after",
                                "fields": ["capital_state_validation"],
                                "source_sha256": "",
                                "execution_lineage_id": "",
                                "detail": final_state_reason,
                            }
                        ],
                    }
                if ashare_position_authority.get("status") == "verified":
                    ashare_capital_state = final_validated_state
                    ashare_capital_state_reason = final_state_reason
                    capital_cash_available = min(
                        _safe_float(ashare_capital_state.get("cash_balance_cny"), 0.0),
                        _safe_float(
                            ashare_capital_state.get("available_to_reserve_cny"), 0.0
                        ),
                    )
                    capital_positions_market_value = max(
                        0.0,
                        _safe_float(
                            ashare_capital_state.get("positions_market_value_cny"),
                            0.0,
                        ),
                    )
                    ashare_position_authority = {
                        **ashare_position_authority,
                        "capital_cash_available": max(0.0, capital_cash_available),
                        "capital_positions_market_value_cny": (
                            capital_positions_market_value
                        ),
                        "capital_total_exposure": min(
                            1.0, capital_positions_market_value / 50_000.0
                        ),
                        "new_risk_allowed": bool(
                            ashare_capital_state.get("new_risk_allowed")
                        ),
                        "new_risk_reason": str(
                            ashare_capital_state.get("new_risk_reason") or ""
                        ),
                    }
                    authoritative_account_view = {
                        **local_account_view,
                        "reported_server_local_cash_available_cny": _safe_float(
                            local_account_view.get("cash_available"), 0.0
                        ),
                        "cash_available": max(0.0, capital_cash_available),
                        "positions": [
                            dict(row)
                            for row in ashare_position_authority.get("positions", [])
                            if isinstance(row, dict)
                        ],
                        "position_authority_status": "verified",
                        "position_count": ashare_position_authority.get(
                            "position_count"
                        ),
                        "positions_fingerprint": ashare_position_authority.get(
                            "positions_fingerprint"
                        ),
                        "capital_authority_checksum": ashare_position_authority.get(
                            "authority_checksum"
                        ),
                        "authority_view_checksum": ashare_position_authority.get(
                            "authority_view_checksum"
                        ),
                        "source_audit": ashare_position_authority.get(
                            "source_audit", []
                        ),
                    }
                else:
                    authoritative_account_view = {
                        "account": account,
                        "capital_cny": 50_000.0,
                        "cash_available": 0.0,
                        "positions": [],
                        "source": "market_capital_position_authority_blocked",
                        "trade_date": _compact_date_key(date),
                        "position_authority_status": "blocked",
                        "position_authority_reason": ashare_position_authority.get(
                            "reason"
                        ),
                        "source_audit": ashare_position_authority.get(
                            "source_audit", []
                        ),
                        "position_source_mismatches": ashare_position_authority.get(
                            "mismatches", []
                        ),
                        "real_trading_enabled": False,
                    }

        ashare_position_authority_reason = str(
            ashare_position_authority.get("reason")
            or ashare_capital_state_reason
            or "ashare_capital_position_authority_invalid"
        )
        ashare_new_risk_allowed = bool(
            ashare_position_authority.get("status") == "verified"
            and isinstance(ashare_capital_state, dict)
            and ashare_capital_state.get("new_risk_allowed") is True
        )
        ashare_new_risk_reason = (
            str(
                (ashare_capital_state or {}).get("new_risk_reason")
                if isinstance(ashare_capital_state, dict)
                else ""
            )
            or ashare_position_authority_reason
        )
        if ashare_position_authority.get("status") != "verified":
            authoritative_account_view = {
                "account": account,
                "capital_cny": 50_000.0,
                "cash_available": 0.0,
                "positions": [],
                "source": "market_capital_position_authority_blocked",
                "trade_date": _compact_date_key(date),
                "position_authority_status": "blocked",
                "position_authority_reason": ashare_position_authority_reason,
                "source_audit": ashare_position_authority.get("source_audit", []),
                "position_source_mismatches": ashare_position_authority.get(
                    "mismatches", []
                ),
                "real_trading_enabled": False,
            }
        if ashare_position_authority.get("status") == "verified":
            local_exploration_state = _safe_stage(
                "capital.ashare_exploration_state",
                errors,
                lambda: local_sim_ledger.get_local_sim_exploration_state(
                    account,
                    trade_date=date,
                    starting_cash=50_000.0,
                ),
                default={
                    "new_position_count": 1,
                    "open_exposure_cny": 7_500.0,
                    "daily_realized_pnl_cny": -225.0,
                    "daily_loss_cny": 225.0,
                    "real_trading_enabled": False,
                    "status": "unavailable_fail_closed",
                },
                capital_layer=capital_layer,
            )
            stage_calls.append("capital.ashare_exploration_state")
        existing_positions = [
            dict(row)
            for row in (ashare_position_authority.get("positions") or [])
            if isinstance(row, dict)
        ]
        capital = 50_000.0
        account_cash_available = (
            min(
                50_000.0,
                max(
                    0.0,
                    _safe_float(authoritative_account_view.get("cash_available"), 0.0),
                ),
            )
            if ashare_position_authority.get("status") == "verified"
            else 0.0
        )
        strategy_positions = existing_positions
        strategy_cash_available = account_cash_available
        adapter_reported_sample_adjustment = (
            dict(account_obj.get("capital_plan_sample_adjustment") or {})
            if isinstance(account_obj, dict)
            and isinstance(account_obj.get("capital_plan_sample_adjustment"), dict)
            else {}
        )
        from Ashare.adapter import build_current_sample_adjustment

        capital_plan_sample_adjustment = _safe_stage(
            "review.ashare_sample_debt_authority",
            errors,
            lambda: build_current_sample_adjustment(
                journal_path=sample_journal_path,
                sample_policy=(
                    config.get("sample_collection_policy")
                    if isinstance(config.get("sample_collection_policy"), dict)
                    else None
                ),
            ),
            default={
                "sample_authority_status": "sample_journal_unavailable",
                "sample_authority_reliable": False,
                "strategy_sample_valid_count": 0,
                "min_strategy_samples": 5,
                "sample_debt": True,
                "reason": "sample_journal_unavailable",
                "real_trading_enabled": False,
            },
            capital_layer=capital_layer,
        )
        stage_calls.append("review.ashare_sample_debt_authority")
        if not isinstance(capital_plan_sample_adjustment, dict):
            capital_plan_sample_adjustment = {
                "sample_authority_status": "sample_journal_unavailable",
                "sample_authority_reliable": False,
                "strategy_sample_valid_count": 0,
                "min_strategy_samples": 5,
                "sample_debt": True,
                "reason": "sample_journal_unavailable",
                "real_trading_enabled": False,
            }
        if adapter_reported_sample_adjustment:
            capital_plan_sample_adjustment["adapter_reported_sample_adjustment"] = (
                adapter_reported_sample_adjustment
            )
            for diagnostic_key in (
                "ignored_validation_sample_count",
                "account_trade_count",
            ):
                if diagnostic_key in adapter_reported_sample_adjustment:
                    capital_plan_sample_adjustment[diagnostic_key] = (
                        adapter_reported_sample_adjustment[diagnostic_key]
                    )
        capital_plan_sample_adjustment.update(
            {
                "existing_exploration_new_positions": max(
                    _safe_int(
                        capital_plan_sample_adjustment.get(
                            "existing_exploration_new_positions"
                        ),
                        0,
                    ),
                    _safe_int(local_exploration_state.get("new_position_count"), 1),
                ),
                "exploration_daily_realized_pnl_cny": min(
                    _safe_float(
                        capital_plan_sample_adjustment.get(
                            "exploration_daily_realized_pnl_cny"
                        ),
                        0.0,
                    ),
                    _safe_float(
                        local_exploration_state.get("daily_realized_pnl_cny"),
                        -225.0,
                    ),
                ),
                "exploration_daily_loss_cny": max(
                    _safe_float(
                        capital_plan_sample_adjustment.get(
                            "exploration_daily_loss_cny"
                        ),
                        0.0,
                    ),
                    _safe_float(local_exploration_state.get("daily_loss_cny"), 225.0),
                ),
                "existing_exploration_exposure_cny": max(
                    _safe_float(
                        capital_plan_sample_adjustment.get(
                            "existing_exploration_exposure_cny"
                        ),
                        0.0,
                    ),
                    _safe_float(
                        local_exploration_state.get("open_exposure_cny"),
                        7_500.0,
                    ),
                ),
            }
        )
    method = str(config.get("portfolio_method", "conviction_weighted"))
    regime = str(config.get("regime", "unknown"))
    max_candidates = max(1, int(config.get("max_candidates", 20)))
    score_limit = max(
        max_candidates, int(config.get("score_universe_limit", max_candidates))
    )
    max_portfolio_positions = max(
        1, int(config.get("max_portfolio_positions", config.get("max_positions", 9999)))
    )
    default_price = _safe_float(config.get("default_price"), 1.0)
    default_volatility = _safe_float(config.get("default_volatility"), 0.20)

    universe = _safe_stage(
        "screening.universe",
        errors,
        lambda: market_adapter.get_universe(date),
        default=[],
        capital_layer=capital_layer,
    )
    stage_calls.append("screening.universe")
    if not isinstance(universe, list):
        errors.append(
            {
                "stage": "screening.universe",
                "status": "degraded",
                "error": "adapter returned non-list",
                "capital_layer": capital_layer,
            }
        )
        universe = []

    scores_by_symbol = _score_symbols_with_batch(
        deps,
        market_adapter,
        market,
        universe,
        date,
        reader,
        max_candidates=score_limit,
        errors=errors,
        stage_calls=stage_calls,
        audits=audits,
        account=account,
        capital_layer=capital_layer,
        account_type=account_type,
    )

    ashare_observation_by_symbol: dict[str, dict[str, Any]] = {}
    sample_pipeline: dict[str, Any] = {
        "status": "skipped",
        "reason": "non_ashare_market",
        "observation": {
            "candidate_observation_count": 0,
            "prediction_count": 0,
            "real_trading_enabled": False,
        },
        "exploration_selection": {
            "status": "not_selected",
            "reason": "non_ashare_market",
            "selected_count": 0,
            "real_trading_enabled": False,
        },
        "outcomes": {
            "status": "skipped",
            "reason": "non_ashare_market",
            "real_trading_enabled": False,
        },
        "real_trading_enabled": False,
    }
    if str(market).lower() == "ashare":
        observation_stage = _safe_stage(
            "review.ashare_observation_samples",
            errors,
            lambda: _build_and_persist_ashare_observations(
                market_adapter=market_adapter,
                reader=reader,
                date=date,
                scores_by_symbol=scores_by_symbol,
                config=config,
                journal_path=sample_journal_path,
            ),
            default={
                "observations": [],
                "observation_by_symbol": {},
                "persistence": {
                    "status": "degraded",
                    "candidate_observation_count": 0,
                    "prediction_count": 0,
                    "real_trading_enabled": False,
                },
                "real_trading_enabled": False,
            },
            capital_layer=capital_layer,
        )
        stage_calls.append("review.ashare_observation_samples")
        if isinstance(observation_stage, dict):
            raw_by_symbol = observation_stage.get("observation_by_symbol")
            if isinstance(raw_by_symbol, dict):
                ashare_observation_by_symbol = {
                    str(symbol): dict(row)
                    for symbol, row in raw_by_symbol.items()
                    if isinstance(row, dict)
                }
            persistence = observation_stage.get("persistence")
            sample_pipeline = {
                **sample_pipeline,
                "status": "recording",
                "reason": "ashare_sample_pipeline_active",
                "observation": (
                    dict(persistence) if isinstance(persistence, dict) else {}
                ),
            }

    pool = _safe_stage(
        "screening.candidate_pool",
        errors,
        lambda: _build_pool_for_market(
            deps, market, date, list(scores_by_symbol), reader, scores_by_symbol
        ),
        default=_candidate_pool_default(market, capital_layer, list(scores_by_symbol)),
        capital_layer=capital_layer,
    )
    stage_calls.append("screening.candidate_pool")
    if not isinstance(pool, dict):
        pool = _candidate_pool_default(market, capital_layer, list(scores_by_symbol))
    condition_lifecycle = _safe_stage(
        "screening.condition_lifecycle",
        errors,
        lambda: _run_condition_lifecycle(market, pool, scores_by_symbol, date, reader),
        default={
            "condition_count": 0,
            "trigger_replay_count": 0,
            "filled_replay_count": 0,
            "conditions": [],
            "trigger_replay": [],
        },
        capital_layer=capital_layer,
    )
    stage_calls.append("screening.condition_lifecycle")

    normal_candidates = _rank_symbols_by_score(
        _candidate_symbols(
            pool, list(scores_by_symbol), market=market, capital_layer=capital_layer
        ),
        scores_by_symbol,
    )[:max_candidates]
    layer_breakdown = _candidate_layer_breakdown(pool, len(universe))
    candidate_layers = {
        str(symbol): "candidate"
        for symbol in (
            (pool.get("candidate", []) if isinstance(pool, dict) else []) or []
        )
        if symbol
    }
    candidates = list(normal_candidates)
    if str(market).lower() == "ashare":
        from Ashare.sample_pipeline import select_exploration_candidate

        safety_blockers: list[str] = []
        if ashare_capital_state is None:
            safety_blockers.append(ashare_capital_state_reason)
        if ashare_position_authority.get("status") != "verified":
            safety_blockers.append(ashare_position_authority_reason)
        if not ashare_new_risk_allowed:
            safety_blockers.append(ashare_new_risk_reason)
        if _safe_float(authoritative_account_view.get("cash_available"), 0.0) <= 0.0:
            safety_blockers.append("authoritative_cash_unavailable")
        if capital_plan_sample_adjustment.get("sample_authority_reliable") is not True:
            safety_blockers.append("sample_journal_authority_unavailable")
        journal_exploration_count = _safe_stage(
            "review.ashare_exploration_daily_count",
            errors,
            lambda: _ashare_exploration_fill_count(sample_journal_path, date),
            default=1,
            capital_layer=capital_layer,
        )
        stage_calls.append("review.ashare_exploration_daily_count")
        exploration_count = max(
            _safe_int(journal_exploration_count, 1),
            _safe_int(local_exploration_state.get("new_position_count"), 1),
        )
        exploration_selection = select_exploration_candidate(
            list(ashare_observation_by_symbol.values()),
            normal_candidate_symbols=normal_candidates,
            sample_debt=_ashare_sample_debt(capital_plan_sample_adjustment),
            existing_exploration_new_positions=_safe_int(exploration_count, 1),
            safety_blockers=safety_blockers,
        )
        if exploration_selection.get("status") == "selected":
            selected_symbol = str(exploration_selection.get("symbol") or "")
            if selected_symbol:
                # A safe non-mature candidate is evaluated as a standby under
                # the same hard risk gates.  It is activated below only when
                # the mature path produces no risk-approved order.
                candidates.append(selected_symbol)
                candidate_layers[selected_symbol] = "exploration"
                exploration_selection = {
                    **exploration_selection,
                    "activation_status": "standby",
                    "activation_reason": "awaiting_normal_strategy_outcome",
                }
        sample_pipeline["exploration_selection"] = exploration_selection
    candidate_decisions: dict[str, dict[str, Any]] = {
        str(symbol): {
            **_candidate_score_snapshot(
                str(symbol), scores_by_symbol.get(str(symbol), {})
            ),
            "layer": candidate_layers.get(str(symbol), "candidate"),
            "status": "selected_for_review",
        }
        for symbol in candidates
    }
    orders_for_portfolio: list[dict[str, Any]] = []
    skipped_candidates: list[dict[str, Any]] = []
    risk_rejections: list[dict[str, Any]] = []
    execution_skips: list[dict[str, Any]] = []
    signal_audit_by_symbol = {
        audit["ts_code"]: audit for audit in audits if audit.get("stage") == "signal"
    }
    risk_portfolio = {
        "positions": list(strategy_positions),
        "total_exposure": (
            _safe_float(ashare_position_authority.get("capital_total_exposure"), 0.0)
            if is_ashare
            else sum(
                _safe_float(position.get("weight"), 0.0)
                for position in strategy_positions
            )
        ),
        "capital_layer": capital_layer,
        "account_type": account_type,
    }

    for symbol in candidates:
        mapped_market, mapped_symbol = _safe_stage(
            "adapter.map_symbol_to_reader",
            errors,
            lambda symbol=symbol: market_adapter.map_symbol_to_reader(symbol),
            default=(market, symbol),
            capital_layer=capital_layer,
        )
        score = scores_by_symbol.get(
            symbol,
            {
                "combined": 0.5,
                "market": mapped_market,
                "capital_layer": capital_layer,
                "account_type": account_type,
            },
        )
        candidate_sample_intent = (
            "exploration"
            if str(market).lower() == "ashare"
            and candidate_layers.get(symbol) == "exploration"
            else "exploitation"
        )
        candidate_attribution: dict[str, Any] = {}
        if str(market).lower() == "ashare":
            candidate_attribution = _ashare_order_attribution(
                ashare_observation_by_symbol.get(symbol),
                sample_intent=candidate_sample_intent,
                account=account,
                exploration_selection=(
                    sample_pipeline.get("exploration_selection")
                    if isinstance(sample_pipeline.get("exploration_selection"), dict)
                    else None
                ),
            )
        parent = signal_audit_by_symbol.get(symbol, {}).get("audit_id", "")
        debate = _safe_stage(
            "adversarial.bull_bear_debate",
            errors,
            lambda symbol=mapped_symbol, score=score: deps.debate(symbol, score),
            default={
                "ts_code": mapped_symbol,
                "belief_score": 0.5,
                "bull_case": "degraded",
                "bear_case": "degraded",
            },
            capital_layer=capital_layer,
        )
        stage_calls.append("adversarial.bull_bear_debate")
        if not isinstance(debate, dict):
            debate = {"belief_score": 0.5}
        debate["capital_layer"] = capital_layer
        debate["account_type"] = account_type
        decision_audit = _record_audit(
            deps,
            "decision",
            symbol,
            parent_audit_id=parent,
            payload={
                "debate": debate,
                "capital_layer": capital_layer,
                "account_type": account_type,
            },
            metadata={"date": date, "account": account, "account_type": account_type},
            capital_layer=capital_layer,
        )
        audits.append(decision_audit)

        price = _latest_price(reader, mapped_market, mapped_symbol, date, default_price)
        volatility = _latest_volatility(
            reader, mapped_market, mapped_symbol, date, default_volatility
        )
        if price <= 0:
            skipped_candidates.append(
                {
                    "symbol": symbol,
                    "reason": "missing_or_non_positive_price",
                    "price": price,
                    "capital_layer": capital_layer,
                    "sample_intent": candidate_sample_intent,
                    **candidate_attribution,
                }
            )
            candidate_decisions.setdefault(symbol, {"symbol": symbol})["price"] = price
            candidate_decisions[symbol]["status"] = "dropped"
            candidate_decisions[symbol]["drop_reason"] = "missing_or_non_positive_price"
            continue
        candidate_decisions.setdefault(symbol, {"symbol": symbol})["price"] = round(
            price, 4
        )
        candidate_decisions[symbol]["belief_score"] = round(
            _safe_float(debate.get("belief_score"), 0.5), 4
        )
        proposed_weight = _safe_stage(
            "portfolio.position_sizer",
            errors,
            lambda debate=debate, volatility=volatility: deps.size_position(
                _safe_float(debate.get("belief_score"), 0.5), volatility, regime
            ),
            default=0.0,
            capital_layer=capital_layer,
        )
        stage_calls.append("portfolio.position_sizer")
        risk_order = {
            "ts_code": symbol,
            "weight": proposed_weight,
            "sector": str(score.get("sector", "unknown")),
            "turnover_wan": _safe_float(score.get("turnover_wan"), 0.0),
            "capital_layer": capital_layer,
            "account_type": account_type,
        }
        position_authority_blocked = (
            is_ashare and ashare_position_authority.get("status") != "verified"
        )
        if position_authority_blocked:
            risk = {
                "approved": False,
                "adjusted_weight": 0.0,
                "adjustments": [],
                "reasons": [ashare_position_authority_reason],
                "reason_code": ashare_position_authority_reason,
                "position_authority_status": "blocked",
                "position_source_audit": ashare_position_authority.get(
                    "source_audit", []
                ),
                "position_source_mismatches": ashare_position_authority.get(
                    "mismatches", []
                ),
            }
            stage_calls.append("capital.ashare_position_authority_gate")
        elif is_ashare and not ashare_new_risk_allowed:
            risk = {
                "approved": False,
                "adjusted_weight": 0.0,
                "adjustments": [],
                "reasons": [ashare_new_risk_reason],
                "reason_code": ashare_new_risk_reason,
                "position_authority_status": "verified",
                "new_risk_allowed": False,
                "position_source_audit": ashare_position_authority.get(
                    "source_audit", []
                ),
            }
            stage_calls.append("capital.ashare_new_risk_gate")
        else:
            risk = _safe_stage(
                "risk.pre_trade_check",
                errors,
                lambda: deps.risk_check(risk_order, risk_portfolio),
                default={
                    "approved": False,
                    "adjusted_weight": 0.0,
                    "reasons": ["degraded"],
                },
                capital_layer=capital_layer,
            )
            stage_calls.append("risk.pre_trade_check")
        if not isinstance(risk, dict):
            risk = {
                "approved": False,
                "adjusted_weight": 0.0,
                "reasons": ["invalid risk result"],
            }
        risk["capital_layer"] = capital_layer
        risk["account_type"] = account_type
        candidate_decisions[symbol]["proposed_weight"] = round(
            _safe_float(proposed_weight, 0.0), 6
        )
        candidate_decisions[symbol]["risk_approved"] = bool(risk.get("approved"))
        candidate_decisions[symbol]["risk_adjusted_weight"] = round(
            _safe_float(risk.get("adjusted_weight"), 0.0), 6
        )
        if risk.get("reasons"):
            candidate_decisions[symbol]["risk_reasons"] = risk.get("reasons", [])
        risk_audit = _record_audit(
            deps,
            "risk",
            symbol,
            parent_audit_id=decision_audit["audit_id"],
            payload=risk,
            metadata={"date": date, "account": account, "account_type": account_type},
            capital_layer=capital_layer,
        )
        audits.append(risk_audit)
        if (
            not risk.get("approved")
            or _safe_float(risk.get("adjusted_weight"), 0.0) <= 0
        ):
            risk_rejections.append(
                {
                    "symbol": symbol,
                    "approved": bool(risk.get("approved")),
                    "adjusted_weight": _safe_float(risk.get("adjusted_weight"), 0.0),
                    "reasons": risk.get("reasons", []),
                    "reason_code": risk.get("reason_code", ""),
                    "position_authority_status": risk.get(
                        "position_authority_status", ""
                    ),
                    "capital_layer": capital_layer,
                    "sample_intent": candidate_sample_intent,
                    **candidate_attribution,
                }
            )
            candidate_decisions[symbol]["status"] = "dropped"
            candidate_decisions[symbol]["drop_reason"] = (
                ashare_position_authority_reason
                if position_authority_blocked
                else ashare_new_risk_reason
                if is_ashare and not ashare_new_risk_allowed
                else "risk_rejected"
            )
            continue
        candidate_decisions[symbol]["status"] = "risk_approved"
        orders_for_portfolio.append(
            {
                "ts_code": symbol,
                "belief_score": _safe_float(debate.get("belief_score"), 0.5),
                "volatility": volatility,
                "sector": str(score.get("sector", "unknown")),
                "price": price,
                "weight": _safe_float(risk.get("adjusted_weight"), proposed_weight),
                "risk_audit_id": risk_audit["audit_id"],
                "mapped_market": mapped_market,
                "mapped_symbol": mapped_symbol,
                "candidate_pool_layer": candidate_layers.get(symbol, "candidate"),
                "sample_intent": candidate_sample_intent,
                **candidate_attribution,
            }
        )
        risk_portfolio["positions"].append(
            {
                "ts_code": symbol,
                "weight": _safe_float(risk.get("adjusted_weight"), proposed_weight),
                "sector": str(score.get("sector", "unknown")),
                "market": market,
                "capital_layer": capital_layer,
                "account_type": account_type,
            }
        )
        risk_portfolio["total_exposure"] = _safe_float(
            risk_portfolio.get("total_exposure"), 0.0
        ) + _safe_float(
            risk.get("adjusted_weight"),
            proposed_weight,
        )

    if str(market).lower() == "ashare":
        exploitation_orders = [
            row
            for row in orders_for_portfolio
            if str(row.get("sample_intent") or "").lower() == "exploitation"
        ]
        exploration_orders = [
            row
            for row in orders_for_portfolio
            if str(row.get("sample_intent") or "").lower() == "exploration"
        ]
        exploration_selection = sample_pipeline.get("exploration_selection")
        if not isinstance(exploration_selection, dict):
            exploration_selection = {}
        if exploitation_orders:
            orders_for_portfolio = exploitation_orders
            for standby in exploration_orders:
                standby_symbol = str(standby.get("ts_code") or "")
                if standby_symbol in candidate_decisions:
                    candidate_decisions[standby_symbol]["status"] = (
                        "standby_not_activated"
                    )
                    candidate_decisions[standby_symbol]["drop_reason"] = (
                        "normal_strategy_risk_approved_order_available"
                    )
            if exploration_orders:
                exploration_selection = {
                    **exploration_selection,
                    "status": "not_activated",
                    "reason": "normal_strategy_risk_approved_order_available",
                    "selected_count": 0,
                    "standby_selected_count": 1,
                    "activation_status": "not_activated",
                    "activation_reason": (
                        "normal_strategy_risk_approved_order_available"
                    ),
                }
        elif exploration_orders:
            # Exactly one standby can exist because the selection contract is
            # capped at one new exploration position per day.
            orders_for_portfolio = exploration_orders[:1]
            exploration_selection = {
                **exploration_selection,
                "status": "selected",
                "selected_count": 1,
                "activation_status": "activated",
                "activation_reason": "normal_strategy_no_risk_approved_order",
            }
        else:
            orders_for_portfolio = []
            if exploration_selection.get("activation_status") == "standby":
                exploration_selection = {
                    **exploration_selection,
                    "status": "not_activated",
                    "selected_count": 0,
                    "standby_selected_count": 1,
                    "activation_status": "not_activated",
                    "activation_reason": "exploration_hard_risk_gate_rejected",
                }
        sample_pipeline["exploration_selection"] = exploration_selection

    ranked_orders_for_portfolio = sorted(
        orders_for_portfolio,
        key=lambda row: (
            _safe_float(
                scores_by_symbol.get(str(row.get("ts_code")), {}).get("combined"), 0.0
            ),
            _safe_float(row.get("belief_score"), 0.0),
        ),
        reverse=True,
    )
    position_gate_blocked = (
        is_ashare and ashare_position_authority.get("status") != "verified"
    )
    if position_gate_blocked:
        # Do not call dynamic planning, capacity, or rebalance with an unknown
        # position set.  None is intentional: zero would fabricate empty risk.
        capital_plan = {
            "enabled": True,
            "status": "blocked",
            "reason": ashare_position_authority_reason,
            "risk_mode": "authority_blocked",
            "target_positions": None,
            "existing_position_count": None,
            "position_capacity": None,
            "remaining_position_slots": None,
            "max_new_positions": 0,
            "position_budget_by_symbol": {},
            "available_cash": 0.0,
            "cash_reserve": 0.0,
            "capacity_reason": ashare_position_authority_reason,
            "new_risk_allowed": False,
            "risk_tightening_active": False,
            "reasons": [ashare_position_authority_reason],
            "notes": ["position authority failed before ordinary capital planning"],
            "source_audit": ashare_position_authority.get("source_audit", []),
            "position_source_mismatches": ashare_position_authority.get(
                "mismatches", []
            ),
        }
        if capital_plan_sample_adjustment:
            capital_plan["sample_adjustment"] = capital_plan_sample_adjustment
        rebalance = {
            "enabled": True,
            "status": "blocked",
            "reason": ashare_position_authority_reason,
            "target_positions": None,
            "existing_position_count": None,
            "planned_sell_count": 0,
            "sells": [],
            "dynamic_thresholds": {},
        }
        planned_sell_symbols: set[str] = set()
        base_position_capacity = 0
        replacement_capacity = 0
        position_capacity = 0
        capacity_reason = ashare_position_authority_reason
        stage_calls.append("capital.ashare_position_authority_plan_gate")
    else:
        capital_plan = _ashare_dynamic_capital_plan(
            market=market,
            date=date,
            capital=capital,
            existing_positions=strategy_positions,
            available_cash=strategy_cash_available,
            orders=orders_for_portfolio,
            scores_by_symbol=scores_by_symbol,
            skipped_candidates=skipped_candidates,
            risk_rejections=risk_rejections,
            sample_adjustment=capital_plan_sample_adjustment,
        )
        if is_ashare:
            capital_plan["cash_source"] = "market_capital_authority"
            capital_plan["capital_authority_checksum"] = ashare_position_authority.get(
                "authority_checksum"
            )
        if capital_plan_sample_adjustment:
            capital_plan["sample_adjustment"] = capital_plan_sample_adjustment
        if is_ashare and ashare_capital_state is not None:
            if not ashare_new_risk_allowed:
                reasons = [
                    str(reason)
                    for reason in (capital_plan.get("reasons") or [])
                    if str(reason)
                ]
                if ashare_new_risk_reason not in reasons:
                    reasons.append(ashare_new_risk_reason)
                capital_plan.update(
                    {
                        "risk_mode": "new_risk_paused",
                        "max_new_positions": 0,
                        "position_budget_by_symbol": {},
                        "capacity_reason": ashare_new_risk_reason,
                        "new_risk_allowed": False,
                        "risk_tightening_active": True,
                        "risk_multiplier": 0.0,
                        "risk_tightening_reason": ashare_new_risk_reason,
                        "reasons": reasons,
                    }
                )
            elif ashare_capital_state.get("drawdown_tightened") is True:
                risk_multiplier = min(
                    1.0,
                    max(
                        0.0,
                        _safe_float(ashare_capital_state.get("risk_multiplier"), 0.0),
                    ),
                )
                budgets = capital_plan.get("position_budget_by_symbol")
                if isinstance(budgets, dict):
                    capital_plan["position_budget_by_symbol"] = {
                        str(symbol): round(
                            max(0.0, _safe_float(value, 0.0)) * risk_multiplier,
                            2,
                        )
                        for symbol, value in budgets.items()
                    }
                capital_plan["max_new_positions"] = min(
                    1,
                    max(0, _safe_int(capital_plan.get("max_new_positions"), 0)),
                )
                capital_plan["new_risk_allowed"] = risk_multiplier > 0.0
                capital_plan["risk_tightening_active"] = True
                capital_plan["risk_multiplier"] = risk_multiplier
                capital_plan["risk_tightening_reason"] = (
                    "ashare_drawdown_5_to_7pct_derisk"
                )
        stage_calls.append("portfolio.capital_plan")
        rebalance = _ashare_rebalance_plan(
            market=market,
            date=date,
            reader=reader,
            existing_positions=strategy_positions,
            capital_plan=capital_plan,
            scores_by_symbol=scores_by_symbol,
            max_portfolio_positions=max_portfolio_positions,
            default_price=default_price,
            capital=capital,
            buy_candidates=ranked_orders_for_portfolio,
        )
        stage_calls.append("portfolio.rebalance_plan")
        planned_sell_symbols = {
            str(row.get("ts_code") or "")
            for row in (rebalance.get("sells", []) or [])
            if isinstance(row, dict)
        }
        base_position_capacity = _max_new_positions(
            existing_positions, max_portfolio_positions
        )
        if is_ashare:
            base_position_capacity = _max_new_positions(
                strategy_positions, max_portfolio_positions
            )
        if capital_plan.get("enabled"):
            base_position_capacity = min(
                base_position_capacity,
                _safe_int(capital_plan.get("max_new_positions"), 0),
            )
        replacement_capacity = _ashare_post_sell_buy_capacity(
            market=market,
            existing_positions=strategy_positions,
            capital_plan=capital_plan,
            rebalance=rebalance,
            max_portfolio_positions=max_portfolio_positions,
        )
        if is_ashare and not ashare_new_risk_allowed:
            replacement_capacity = 0
        position_capacity = max(base_position_capacity, replacement_capacity)
        capacity_reason = str(capital_plan.get("capacity_reason") or "")
        if not capacity_reason and position_capacity <= 0:
            target_positions_for_reason = _safe_int(
                capital_plan.get("target_positions"), 0
            )
            existing_for_reason = len(
                strategy_positions if is_ashare else existing_positions
            )
            if (
                target_positions_for_reason > 0
                and existing_for_reason >= target_positions_for_reason
            ):
                capacity_reason = "target_positions_reached"
            elif _safe_float(capital_plan.get("available_cash"), 0.0) <= _safe_float(
                capital_plan.get("cash_reserve"), 0.0
            ):
                capacity_reason = "insufficient_investable_cash"
            else:
                capacity_reason = "capital_plan_capacity_zero"
    allowed_buy_symbols = {
        str(order.get("ts_code") or "")
        for order in [
            order
            for order in ranked_orders_for_portfolio
            if str(order.get("ts_code") or "") not in planned_sell_symbols
        ][:position_capacity]
    }
    for order in ranked_orders_for_portfolio:
        symbol = str(order.get("ts_code") or "")
        if not symbol or symbol not in candidate_decisions:
            continue
        if symbol in planned_sell_symbols:
            candidate_decisions[symbol]["status"] = "dropped"
            candidate_decisions[symbol]["drop_reason"] = "rebalance_planned_sell"
        elif symbol not in allowed_buy_symbols:
            candidate_decisions[symbol]["status"] = "dropped"
            candidate_decisions[symbol]["drop_reason"] = (
                capacity_reason
                if position_capacity <= 0 and capacity_reason
                else "position_capacity_limit"
            )
        else:
            candidate_decisions[symbol]["status"] = "portfolio_input"
    orders_for_portfolio = [
        order
        for order in ranked_orders_for_portfolio
        if str(order.get("ts_code") or "") not in planned_sell_symbols
    ][:position_capacity]
    if not position_gate_blocked:
        capital_plan = _augment_ashare_replacement_budgets(
            market=market,
            capital_plan=capital_plan,
            rebalance=rebalance,
            orders_for_portfolio=orders_for_portfolio,
            replacement_capacity=replacement_capacity,
            capital=capital,
        )

    portfolio = _safe_stage(
        "portfolio.constructor",
        errors,
        lambda: deps.construct(
            orders_for_portfolio, capital, method=method, regime=regime
        ),
        default={"positions": [], "total_weight": 0.0, "cash_weight": 1.0},
        capital_layer=capital_layer,
    )
    stage_calls.append("portfolio.constructor")
    if not isinstance(portfolio, dict):
        portfolio = {"positions": [], "total_weight": 0.0, "cash_weight": 1.0}
    portfolio["capital_layer"] = capital_layer
    portfolio["account_type"] = account_type
    portfolio["existing_positions"] = strategy_positions
    if strategy_positions != existing_positions:
        portfolio["account_positions"] = existing_positions
    portfolio["capital_plan"] = capital_plan

    order_meta = {order["ts_code"]: order for order in orders_for_portfolio}
    _apply_position_budgets(
        market=market,
        portfolio=portfolio,
        order_meta=order_meta,
        capital_plan=capital_plan,
        capital=capital,
    )
    portfolio_buy_positions = {
        str(row.get("ts_code") or ""): row
        for row in (portfolio.get("positions", []) or [])
        if isinstance(row, dict) and row.get("ts_code")
    }
    for symbol in allowed_buy_symbols:
        if symbol not in candidate_decisions:
            continue
        position = portfolio_buy_positions.get(symbol)
        shares = (
            _safe_float(position.get("shares"), 0.0)
            if isinstance(position, dict)
            else 0.0
        )
        if not isinstance(position, dict):
            candidate_decisions[symbol]["status"] = "dropped"
            candidate_decisions[symbol]["drop_reason"] = "portfolio_constructor_empty"
        elif shares <= 0:
            candidate_decisions[symbol]["status"] = "dropped"
            candidate_decisions[symbol]["drop_reason"] = "lot_size_or_budget_zero"
            candidate_decisions[symbol]["portfolio_shares"] = shares
        else:
            candidate_decisions[symbol]["status"] = "portfolio_position"
            candidate_decisions[symbol]["portfolio_shares"] = shares
            candidate_decisions[symbol]["portfolio_amount"] = round(
                _safe_float(position.get("amount"), 0.0), 2
            )
    capital_plan_decision = {
        "enabled": bool(capital_plan.get("enabled")),
        "risk_mode": capital_plan.get("risk_mode"),
        "target_positions": capital_plan.get("target_positions"),
        "max_new_positions": capital_plan.get("max_new_positions"),
        "position_capacity": None if position_gate_blocked else position_capacity,
        "base_position_capacity": (
            None if position_gate_blocked else base_position_capacity
        ),
        "replacement_capacity": (
            None if position_gate_blocked else replacement_capacity
        ),
        "existing_position_count": (
            None
            if position_gate_blocked
            else len(strategy_positions if is_ashare else existing_positions)
        ),
        "capacity_reason": capacity_reason,
        "available_cash": round(strategy_cash_available, 2),
        "account_cash_available": round(account_cash_available, 2),
        "reasons": capital_plan.get("reasons", []),
        "notes": capital_plan.get("notes", []),
    }
    if capital_plan_sample_adjustment:
        capital_plan_decision["sample_adjustment"] = capital_plan_sample_adjustment
    portfolio_decision = {
        "risk_approved_candidates": len(orders_for_portfolio),
        "ranked_risk_approved_candidates": len(ranked_orders_for_portfolio),
        "portfolio_positions": len(portfolio_buy_positions),
        "planned_sell_count": len(planned_sell_symbols),
        "allowed_buy_count": len(allowed_buy_symbols),
        "lot_or_budget_zero_count": sum(
            1
            for row in candidate_decisions.values()
            if row.get("drop_reason") == "lot_size_or_budget_zero"
        ),
    }
    capital_plan_log = _safe_stage(
        "review.capital_plan_log",
        errors,
        lambda: _write_ashare_capital_plan_log(
            market=market,
            date=date,
            account=account,
            capital_plan=capital_plan,
            rebalance=rebalance,
            planned_buy_count=len(orders_for_portfolio),
            capital_layer=capital_layer,
            account_type=account_type,
            review_root=signals_dir.parent / "shared" / "review",
        ),
        default={"status": "degraded", "rows": 0},
        capital_layer=capital_layer,
    )
    stage_calls.append("review.capital_plan_log")
    execution_positions = [
        *(rebalance.get("sells", []) or []),
        *(portfolio.get("positions", []) or []),
    ]
    for position in execution_positions:
        if not isinstance(position, dict) or not position.get("ts_code"):
            continue
        symbol = str(position["ts_code"])
        meta = order_meta.get(symbol, {})
        side = str(position.get("side") or "buy").lower()
        quantity = _execution_quantity(market, side, position.get("shares"))
        order_id = _make_order_id("SIM-", market, symbol, date)
        idempotency_key = _sim_idempotency_key(market, account, symbol, date, side)
        if side == "sell":
            candidate_pool_layer = (
                "ashare_rebalance_sell"
                if str(market).lower() == "ashare"
                else "rebalance"
            )
        else:
            candidate_pool_layer = str(
                position.get("candidate_pool_layer")
                or candidate_layers.get(symbol)
                or "candidate"
            )
        order = {
            "order_id": order_id,
            "idempotency_key": idempotency_key,
            "ts_code": symbol,
            "side": side,
            "quantity": quantity,
            "price": _safe_float(position.get("price"), 0.0),
            "mid_price": _safe_float(position.get("price"), 0.0),
            "limit_price": _safe_float(position.get("price"), 0.0),
            "order_type": "market",
            "trade_date": date,
            "strategy_name": account,
            "market": market,
            "capital_layer": capital_layer,
            "account_type": account_type,
            "note": str(
                position.get("reason") or f"orchestrator sim loop {market} {date}"
            ),
            "candidate_pool_layer": candidate_pool_layer,
            "execution_source": (
                "ashare_candidate_layer"
                if str(market).lower() == "ashare" and side == "buy"
                else "ashare_rebalance_sell"
                if str(market).lower() == "ashare" and side == "sell"
                else "orchestrator_sim_loop"
            ),
        }
        if str(market).lower() == "ashare":
            order["capital_scope"] = "strategy"
            order["real_trading_enabled"] = False
            if side == "buy":
                order["sample_intent"] = str(
                    meta.get("sample_intent")
                    or capital_plan.get("sample_intent")
                    or "exploitation"
                )
                for attribution_key in (
                    "attribution_status",
                    "execution_allowed_by_style_attribution",
                    "prediction_snapshot_role",
                    "primary_style",
                    "supporting_styles",
                    "style_scores",
                    "style_versions",
                    "decision_policy_version",
                    "style_disagreement",
                    "capital_authority",
                    "prediction_snapshot_id",
                    "base_snapshot_sha256",
                    "pair_id",
                    "selection_probability",
                    "propensity",
                    "exploration_policy_version",
                    "selection_seed_sha256",
                    "selection_method",
                    "epsilon",
                    "eligible_top_k_count",
                ):
                    if attribution_key in meta:
                        order[attribution_key] = deepcopy(meta.get(attribution_key))
                if meta.get("source_snapshot_sha256"):
                    order["prediction_source_snapshot_sha256"] = str(
                        meta["source_snapshot_sha256"]
                    )
            execution_market, execution_symbol = _safe_stage(
                "adapter.map_symbol_to_reader.execution",
                errors,
                lambda symbol=symbol: market_adapter.map_symbol_to_reader(symbol),
                default=(market, symbol),
            )
            market_snapshot = _latest_execution_market_snapshot(
                reader, execution_market, execution_symbol, date, side
            )
            if market_snapshot:
                order["market_snapshot"] = market_snapshot
            lineage_result = _safe_stage(
                "execution.ashare_lineage",
                errors,
                lambda: local_sim_ledger.build_local_sim_order_lineage(
                    point_in_time_as_of=_now_iso()
                ),
                default={
                    "status": "execution_lineage_unavailable",
                    "real_trading_enabled": False,
                },
                capital_layer=capital_layer,
            )
            if isinstance(lineage_result, dict):
                for lineage_key in (
                    "schema_version",
                    "capital_authority_id",
                    "authority_generation",
                    "execution_lineage_id",
                    "lineage_started_at",
                    "point_in_time_as_of",
                    "execution_lineage_sha256",
                ):
                    if lineage_key in lineage_result:
                        order[lineage_key] = lineage_result[lineage_key]
            order["risk_unit_key"] = symbol
            if side == "sell":
                sell_head = _capture_ashare_market_capital_head()
                order.update(
                    {
                        "market_capital_required": True,
                        "market_capital_risk_unit_key": symbol,
                    }
                )
                if sell_head:
                    order.update(
                        {
                            "market_capital_expected_head_event_id": sell_head[
                                "event_id"
                            ],
                            "market_capital_expected_head_checksum": sell_head[
                                "checksum"
                            ],
                        }
                    )
            score_snapshot = dict(scores_by_symbol.get(symbol, {}))
            sample_intent = str(
                order.get("sample_intent")
                or (
                    "exploration"
                    if "sample_collection_before_min_samples"
                    in (capital_plan.get("reasons") or [])
                    else "exploitation"
                )
            )
            try:
                from Ashare.sample_pipeline import build_research_hypothesis

                research_hypothesis = build_research_hypothesis(
                    trade_date=date,
                    symbol=symbol,
                    side=side,
                    execution_source=str(order.get("execution_source") or ""),
                    candidate_pool_layer=str(order.get("candidate_pool_layer") or ""),
                    score_snapshot=score_snapshot,
                    sample_intent=sample_intent,
                    capital_plan=capital_plan,
                )
                order["hypothesis_id"] = research_hypothesis["hypothesis_id"]
                order["research_hypothesis"] = research_hypothesis
                order["factor_snapshot"] = research_hypothesis.get(
                    "factor_snapshot", {}
                )
            except Exception:
                order["research_hypothesis"] = {
                    "status": "degraded",
                    "sample_intent": sample_intent,
                }
        if order["quantity"] <= 0 or order["price"] <= 0:
            skip = {
                "stage": "execution.sim_broker",
                "status": "skipped",
                "symbol": symbol,
                "reason": "non-positive quantity or price",
                "capital_layer": capital_layer,
            }
            execution_skips.append(skip)
            errors.append(skip)
            continue
        existing_signal = _find_existing_sim_signal(
            signals_dir,
            market=market,
            account=account,
            symbol=symbol,
            date=date,
            side=side,
            capital_layer=capital_layer,
            account_type=account_type,
            idempotency_key=idempotency_key,
        )
        if existing_signal and existing_signal.get("retryable"):
            retry_attempt = _safe_int(existing_signal.get("retry_attempt"), 0)
            order["retry_of"] = str(
                existing_signal.get("retry_of") or existing_signal.get("order_id") or ""
            )
            order["retry_attempt"] = retry_attempt
            order["idempotency_key"] = f"{idempotency_key}:retry{retry_attempt}"
            existing_signal = None
        if existing_signal:
            stage_calls.append("signals.sim_dedup")
            signal_result = {
                "order_id": order_id,
                "status": "duplicate",
                "recorded": False,
                "existing_signal": existing_signal,
            }
            receipt = {
                "order_id": order_id,
                "status": "duplicate",
                "message": "same-day simulated signal already exists",
                "existing_signal": existing_signal,
                "capital_layer": capital_layer,
                "account_type": account_type,
            }
            email_notification = {
                "status": "skipped",
                "reason": "duplicate same-day sim signal",
                "template": "trade_receipt",
            }
            records.append(
                {
                    "symbol": symbol,
                    "order": order,
                    "receipt": receipt,
                    "signal_result": signal_result,
                    "email_notification": email_notification,
                }
            )
            continue
        market_gate: dict[str, Any] = {
            "approved": True,
            "reason": "not_required",
        }
        market_settlement: dict[str, Any] = {
            "status": "not_required",
        }
        recovered_receipt: dict[str, Any] | None = None
        if str(market).lower() == "ashare" and side == "buy":
            market_gate = _reserve_ashare_market_order(
                order,
                ashare_capital_state,
                ashare_capital_state_reason,
            )
            stage_calls.append("capital.ashare_market_reserve")
            gate_reason = str(market_gate.get("reason") or "ashare_capital_rejected")
            if gate_reason in {"idempotent_reservation", "reservation_closed"}:
                recovered_receipt = _recover_local_ashare_receipt(order, account)
                market_gate["recovery_only"] = True
                if recovered_receipt is not None:
                    market_gate["approved"] = True
                    market_gate["recovered_local_fill"] = True
            if not market_gate.get("approved"):
                execution_skips.append(
                    {
                        "stage": "execution.ashare_capital",
                        "status": "blocked",
                        "symbol": symbol,
                        "reason": gate_reason,
                        "capital_layer": capital_layer,
                    }
                )
                receipt = {
                    "order_id": order_id,
                    "status": "rejected",
                    "reason": gate_reason,
                    "message": f"A-share capital gate rejected order: {gate_reason}",
                    "filled_qty": 0,
                    "filled_quantity": 0,
                    "avg_price": 0.0,
                    "filled_price": 0.0,
                    "capital_layer": capital_layer,
                    "account_type": account_type,
                    "execution_eligible": False,
                    "raw_response": {"market_capital_gate": market_gate},
                }
            elif market_gate.get("recovery_only"):
                if recovered_receipt is None:
                    receipt = {
                        "order_id": order_id,
                        "status": "pending",
                        "reason": "idempotent_reservation_reconciliation_required",
                        "message": "Existing reservation has no confirmed local fill; manual reconciliation required",
                        "filled_qty": 0,
                        "filled_quantity": 0,
                        "avg_price": 0.0,
                        "filled_price": 0.0,
                        "capital_layer": capital_layer,
                        "account_type": account_type,
                        "execution_eligible": False,
                        "retryable": True,
                        "raw_response": {"market_capital_gate": market_gate},
                    }
                else:
                    receipt = recovered_receipt
        if not (
            str(market).lower() == "ashare"
            and side == "buy"
            and (not market_gate.get("approved") or market_gate.get("recovery_only"))
        ):
            receipt = _safe_stage(
                "execution.sim_broker",
                errors,
                lambda order=order: _execute_sim_order(deps, order, account_obj),
                default={
                    "order_id": order_id,
                    "status": "failed",
                    "message": "degraded",
                },
                capital_layer=capital_layer,
            )
            stage_calls.append("execution.sim_broker")
        if not isinstance(receipt, dict):
            receipt = {
                "order_id": order_id,
                "status": "failed",
                "message": "invalid sim broker receipt",
            }
        if (
            str(market).lower() == "ashare"
            and side == "buy"
            and market_gate.get("approved")
        ):
            receipt, market_settlement = _settle_ashare_market_receipt(
                order,
                receipt,
                account,
            )
            stage_calls.append("capital.market_settlement")
        if str(market).lower() == "ashare" and side == "sell":
            receipt, market_settlement = _reconcile_ashare_sell_receipt(
                order,
                receipt,
                account,
            )
            stage_calls.append("capital.ashare_market_sell_outbox")
        receipt.setdefault("order_id", order_id)
        receipt["capital_layer"] = capital_layer
        receipt["account_type"] = account_type
        receipt["market_capital_gate"] = market_gate
        receipt["market_capital_settlement"] = market_settlement
        execution_audit = _record_audit(
            deps,
            "execution",
            symbol,
            parent_audit_id=str(meta.get("risk_audit_id", "")),
            payload={
                "order": order,
                "sim_broker": receipt,
                "market_capital_gate": market_gate,
                "market_capital_settlement": market_settlement,
                "capital_layer": capital_layer,
                "account_type": account_type,
            },
            metadata={"date": date, "account": account, "account_type": account_type},
            capital_layer=capital_layer,
        )
        audits.append(execution_audit)
        risk = {
            "approved": True,
            "adjusted_weight": position.get("weight"),
            "adjustments": [],
            "reasons": [],
            "capital_layer": capital_layer,
            "account_type": account_type,
        }
        card = _build_signal_card(
            market=market,
            symbol=symbol,
            account=account,
            date=date,
            order=order,
            risk=risk,
            trade=receipt,
            audit_id=execution_audit["audit_id"],
            order_id=order_id,
            order_id_prefix="SIM-",
            capital_layer=capital_layer,
            account_type=account_type,
            direct_execution=True,
        )
        signal_result = _safe_stage(
            "signals.sim_execution",
            errors,
            lambda card=card, receipt=receipt: _write_execution_signal(
                card, receipt, signals_dir
            ),
            default={"status": "degraded", "recorded": False},
            capital_layer=capital_layer,
        )
        stage_calls.append("signals.sim_execution")
        email_notification = {
            "status": "skipped",
            "reason": "sim order not filled",
            "template": "trade_receipt",
        }
        if signal_result.get("status") == "filled":
            email_data = _trade_receipt_email_data(
                market=market,
                symbol=symbol,
                date=date,
                account=account,
                order=order,
                receipt=receipt,
                card=card,
            )
            email_notification = _safe_stage(
                "notify.trade_receipt",
                errors,
                lambda data=email_data: _send_template_email_now(
                    deps.send_email,
                    "trade_receipt",
                    data,
                    subject=f"tradingagent 模拟盘成交回执 {symbol} {_date_iso(date)}",
                ),
                default={"status": "degraded", "template": "trade_receipt"},
                capital_layer=capital_layer,
            )
            stage_calls.append("notify.trade_receipt")
        result_audit = _record_audit(
            deps,
            "result",
            symbol,
            parent_audit_id=execution_audit["audit_id"],
            payload={
                "signal_result": signal_result,
                "email_notification": email_notification,
                "capital_layer": capital_layer,
                "account_type": account_type,
            },
            metadata={"date": date, "account": account, "account_type": account_type},
            capital_layer=capital_layer,
        )
        audits.append(result_audit)
        records.append(
            {
                "symbol": symbol,
                "order": order,
                "receipt": receipt,
                "market_capital_gate": market_gate,
                "market_capital_settlement": market_settlement,
                "signal_result": signal_result,
                "email_notification": email_notification,
            }
        )

    if str(market).lower() == "ashare":
        sample_outcomes = _safe_stage(
            "review.ashare_sample_outcomes",
            errors,
            lambda: _persist_ashare_sample_outcomes(
                journal_path=sample_journal_path,
                trade_date=date,
                records=records,
                risk_rejections=risk_rejections,
                authoritative_account_view=authoritative_account_view,
                ashare_capital_state=ashare_capital_state,
            ),
            default={
                "status": "degraded",
                "exploration_fill_count": 0,
                "exploitation_fill_count": 0,
                "risk_reject_count": 0,
                "skipped_outcome_count": 0,
                "real_trading_enabled": False,
            },
            capital_layer=capital_layer,
        )
        stage_calls.append("review.ashare_sample_outcomes")
        sample_pipeline["outcomes"] = sample_outcomes
        sample_pipeline["status"] = (
            "recorded" if sample_outcomes.get("status") == "recorded" else "degraded"
        )

    exclusion_log = _safe_stage(
        "review.execution_exclusions",
        errors,
        lambda: _write_sim_execution_exclusions(
            market=market,
            date=date,
            account=account,
            skipped_candidates=skipped_candidates,
            risk_rejections=risk_rejections,
            execution_skips=execution_skips,
            capital_layer=capital_layer,
            account_type=account_type,
            review_root=signals_dir.parent / "shared" / "review",
        ),
        default={"status": "degraded", "rows": 0},
        capital_layer=capital_layer,
    )
    stage_calls.append("review.execution_exclusions")

    review = _safe_stage(
        "review.daily_review",
        errors,
        lambda: _run_review_for_layer(
            deps, date, session="close", capital_layer=capital_layer
        ),
        default={
            "session": "close",
            "error": "degraded",
            "trade_date": date,
            "capital_layer": capital_layer,
        },
        capital_layer=capital_layer,
    )
    stage_calls.append("review.daily_review")

    filled_count = sum(
        1 for record in records if record["signal_result"].get("status") == "filled"
    )
    failed_count = sum(
        1 for record in records if record["signal_result"].get("status") == "failed"
    )
    pending_count = sum(
        1 for record in records if record["signal_result"].get("status") == "pending"
    )
    partial_count = sum(
        1 for record in records if record["signal_result"].get("status") == "partial"
    )
    duplicate_count = sum(
        1 for record in records if record["signal_result"].get("status") == "duplicate"
    )
    post_execution_capital_plan_refresh = {
        "status": "skipped",
        "reason": "non_ashare_market",
    }
    if str(market).lower() == "ashare":
        post_execution_position_authority = ashare_position_authority
        position_change_count = filled_count + partial_count
        if position_change_count > 0:
            post_execution_position_authority = (
                _resolve_ashare_position_authority_for_entry(
                    market_adapter,
                    date,
                    errors=errors,
                    stage_calls=stage_calls,
                    stage_prefix="capital.ashare_post_execution_position_authority",
                    capital_layer=capital_layer,
                )
            )
        post_execution_capital_plan_refresh = _safe_stage(
            "review.post_execution_capital_plan_refresh",
            errors,
            lambda: _write_ashare_post_execution_capital_plan_refresh(
                market=market,
                date=date,
                account=account,
                capital_plan=capital_plan,
                position_authority=post_execution_position_authority,
                capital_layer=capital_layer,
                account_type=account_type,
                position_change_count=position_change_count,
                review_root=signals_dir.parent / "shared" / "review",
            ),
            default={"status": "degraded", "rows": 0},
            capital_layer=capital_layer,
        )
        stage_calls.append("review.post_execution_capital_plan_refresh")
    planned_order_count = len(execution_positions)
    portfolio_positions = len(
        [
            row
            for row in (portfolio.get("positions", []) or [])
            if isinstance(row, dict) and row.get("ts_code")
        ]
    )
    no_trade_explanation = _sim_no_trade_explanation(
        universe_count=len(universe),
        candidate_count=len(candidates),
        order_count=planned_order_count,
        portfolio_positions=portfolio_positions,
        filled_count=filled_count,
        failed_count=failed_count,
        pending_count=pending_count,
        duplicate_count=duplicate_count,
        skipped_candidates=skipped_candidates,
        risk_rejections=risk_rejections,
        execution_skips=execution_skips,
        errors=errors,
        score_diagnostics=_score_diagnostics(
            scores_by_symbol, actual_candidate_count=len(candidates)
        )
        if str(market).lower() == "ashare"
        else None,
        candidate_layer_breakdown=layer_breakdown
        if str(market).lower() == "ashare"
        else None,
        candidate_decision_trace=list(candidate_decisions.values())
        if str(market).lower() == "ashare"
        else None,
        capital_plan_decision=capital_plan_decision
        if str(market).lower() == "ashare"
        else None,
        portfolio_decision=portfolio_decision
        if str(market).lower() == "ashare"
        else None,
    )

    return {
        "market": market,
        "date": date,
        "capital_layer": capital_layer,
        "account_type": account_type,
        "account": account,
        "state": "degraded" if errors else "ok",
        "stage_calls": stage_calls,
        "universe_count": len(universe),
        "candidate_count": len(candidates),
        "order_count": planned_order_count,
        "skipped_candidate_count": len(skipped_candidates),
        "skipped_candidates": skipped_candidates[:20],
        "risk_rejection_count": len(risk_rejections),
        "risk_rejections": risk_rejections[:20],
        "execution_skip_count": len(execution_skips),
        "execution_skips": execution_skips[:20],
        "execution_exclusion_log": exclusion_log,
        "filled_count": filled_count,
        "failed_count": failed_count,
        "pending_count": pending_count,
        "partial_count": partial_count,
        "duplicate_count": duplicate_count,
        "no_trade_explanation": no_trade_explanation,
        "candidate_layer_breakdown": layer_breakdown,
        "candidate_decision_trace": list(candidate_decisions.values())[:20],
        "capital_plan_decision": capital_plan_decision,
        "portfolio_decision": portfolio_decision,
        "capital_plan": capital_plan,
        "capital_plan_log": capital_plan_log,
        "post_execution_capital_plan_refresh": post_execution_capital_plan_refresh,
        "authoritative_account_view": authoritative_account_view,
        "adapter_account_diagnostics": {
            "capital_cny": adapter_capital,
            "cash_available": adapter_cash_available,
            "position_count": len(adapter_positions),
            "authoritative": str(market).lower() != "ashare",
        },
        "ashare_capital_state": ashare_capital_state,
        "ashare_capital_state_reason": ashare_capital_state_reason,
        "ashare_position_authority": ashare_position_authority,
        "ashare_position_authority_reason": ashare_position_authority_reason,
        "ashare_capital_outbox": ashare_capital_outbox,
        "sample_pipeline": sample_pipeline,
        "rebalance": rebalance,
        "portfolio": portfolio,
        "records": records,
        "audit_events": audits,
        "errors": errors,
        "review": review,
        "condition_lifecycle": condition_lifecycle,
        "generated_at": _now_iso(),
    }


def run_daily_review(
    market: str,
    date: str,
    lunch_or_close: str,
    *,
    deps: OrchestratorDeps | None = None,
) -> dict[str, Any]:
    """Run daily review for shadow trades and persist through review/data."""

    deps = deps or _default_deps()
    session = (
        "lunch"
        if str(lunch_or_close).lower() in {"lunch", "midday", "day"}
        else "close"
    )
    shadow_trades = _load_shadow_trades_for_date(date)
    try:
        result = deps.run_review(date, session=session)
    except Exception as exc:
        result = {"session": session, "trade_date": date, "error": str(exc)}
    if isinstance(result, dict):
        result.update(
            {
                "market": market,
                "capital_layer": "shadow",
                "job": "orchestrator_daily_review",
                "generated_at": _now_iso(),
                "shared_shadow_trade_count": len(shadow_trades),
            }
        )
    return result


__all__ = [
    "MarketAdapter",
    "OrchestratorDeps",
    "run_daily_review",
    "run_sim_loop",
    "run_shadow_loop",
]
