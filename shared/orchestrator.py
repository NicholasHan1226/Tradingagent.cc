#!/usr/bin/env python3
"""Market-agnostic shadow trading orchestrator."""

from __future__ import annotations

import json
import inspect
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from shared.markets.base import MarketAdapter
from shared.notify import email_sender

ROOT = Path(__file__).resolve().parent.parent
SIGNALS_DIR = ROOT / "signals"
ASHARE_OPPORTUNITY_COST_MIN_ENTRY_SCORE = 0.70
ASHARE_OPPORTUNITY_COST_MIN_SCORE_GAP = 0.18

StageFn = Callable[..., Any]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _date_iso(date_value: str) -> str:
    raw = str(date_value or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


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
    if str(market or "").strip().lower() == "ashare" and str(side or "").strip().lower() == "buy":
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
        execute_sim_order=getattr(sim_broker, "execute_sim_order", sim_broker.simulate_order),
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


def _stage_error(stage: str, exc: Exception, *, capital_layer: str = "shadow") -> dict[str, Any]:
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


def _latest_price(reader: Any, market: str, symbol: str, date: str, default: float) -> float:
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


def _latest_volatility(reader: Any, market: str, symbol: str, date: str, default: float) -> float:
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
    return max((variance ** 0.5) * (252 ** 0.5), 0.01)


def _write_pending_signal(card: dict[str, Any], signals_dir: Path = SIGNALS_DIR) -> dict[str, Any]:
    from shared.execution.signal_state_machine import SignalStateConflict, SignalStateMachine

    # Shadow records are research/paper-tracking signals. Keep their pending lifecycle
    # for review and email de-duplication, but isolate them from executable queues.
    layer = str(card.get("capital_layer") or "").strip().lower()
    direct_execution = bool(card.get("direct_execution"))
    state_root = signals_dir / "shadow" if layer == "shadow" and not direct_execution else signals_dir
    symbol = str(card.get("ts_code") or card.get("symbol") or card.get("code") or "").strip()
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
    from shared.execution.signal_state_machine import SignalStateConflict, SignalStateMachine

    machine = SignalStateMachine(signals_dir)
    status = str(receipt.get("status", "")).strip().lower()
    retryable = bool(receipt.get("retryable")) or status in {"pending", "queued", "retryable", "unfilled"}
    rejected = status in {"rejected", "reject", "failed", "failure", "error", "cancelled", "canceled"}
    raw_response = receipt.get("raw_response") if isinstance(receipt.get("raw_response"), dict) else {}
    existing_signal_path = str(receipt.get("signal_path") or raw_response.get("signal_path") or "")

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

    webhook_payload = raw_response.get("webhook") if isinstance(raw_response.get("webhook"), dict) else {}
    mini_webhook_sent = raw_response.get("mode") == "mini_webhook_sent" or bool(webhook_payload.get("success"))
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
        return {"order_id": card.get("order_id", ""), "status": "pending", "pending_signal": pending}
    if rejected:
        reason = str(receipt.get("message") or receipt.get("reason") or status or "sim order rejected")
        engine_record = raw_response.get("engine_record") if isinstance(raw_response.get("engine_record"), dict) else {}
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
        failed = machine.fail(str(card.get("order_id", "")), reason=reason, details=failure_details)
        return {"order_id": card.get("order_id", ""), "status": "failed", "pending_signal": pending, "failed_signal": failed}

    fill_info = dict(receipt)
    if "filled_quantity" in fill_info and "filled_qty" not in fill_info:
        fill_info["filled_qty"] = fill_info["filled_quantity"]
    if "filled_qty" in fill_info and "filled_quantity" not in fill_info:
        fill_info["filled_quantity"] = fill_info["filled_qty"]
    fill_info.setdefault("filled_price", card.get("price", 0.0))
    fill_info.setdefault("filled_quantity", card.get("quantity", 0))
    fill_info.setdefault("filled_qty", fill_info.get("filled_quantity", card.get("quantity", 0)))
    claimed = machine.claim(str(card.get("order_id", "")), worker_id="sim_loop")
    running = machine.mark_running(str(card.get("order_id", "")), worker_id="sim_loop")
    filled = machine.fill(str(card.get("order_id", "")), fill_info)
    return {
        "order_id": card.get("order_id", ""),
        "status": "filled",
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


def _sim_idempotency_key(market: str, account: str, symbol: str, date: str, side: str) -> str:
    date_key = _compact_date_key(date)
    parts = ("SIM", market.lower(), account, date_key, symbol.upper(), side.lower())
    return ":".join(str(part).replace("/", "-").replace(" ", "_") for part in parts)


def _shadow_idempotency_key(market: str, account: str, symbol: str, date: str, side: str) -> str:
    date_key = _compact_date_key(date)
    parts = ("SHADOW", market.lower(), account, date_key, symbol.upper(), side.lower())
    return ":".join(str(part).replace("/", "-").replace(" ", "_") for part in parts)


def _signal_card_date_key(card: dict[str, Any], fallback_name: str = "") -> str:
    for key in ("trade_date", "date", "valid_until", "timestamp", "filled_at", "received_at", "created_at"):
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
    states = ("pending", "claimed", "running", "filled", "failed", "partial", "expired", "cancelled")
    for state in states:
        state_dir = signals_dir / state
        if not state_dir.exists():
            continue
        for path in sorted(state_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                card = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if str(card.get("idempotency_key") or "") == idempotency_key:
                return {"state": state, "path": str(path), "order_id": card.get("order_id"), "idempotency_key": idempotency_key}
            card_symbol = str(card.get("ts_code") or card.get("symbol") or card.get("code") or "").upper()
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
            return {
                "state": state,
                "path": str(path),
                "order_id": card.get("order_id"),
                "idempotency_key": card.get("idempotency_key"),
                "matched_by": "same_day_symbol_side",
                "account": account,
            }
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
        idempotency_key = order.get("idempotency_key") or _shadow_idempotency_key(market, account, symbol, date, side)
    else:
        idempotency_key = order.get("idempotency_key") or order_id
    return {
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


def _send_template_email_now(
    sender: StageFn | None,
    template_name: str,
    data: dict[str, Any],
    *,
    subject: str,
) -> dict[str, Any]:
    if sender is None:
        return {"status": "skipped", "reason": "email sender unavailable", "template": template_name}
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
    weight = _safe_float(position.get("weight"), _safe_float(risk.get("adjusted_weight"), 0.0))
    total_score = score.get("total", score.get("combined", score.get("belief_score", "--")))
    reasons = risk.get("reasons") if isinstance(risk.get("reasons"), list) else []
    condition = "; ".join(str(item) for item in reasons if item) or f"{market} shadow signal generated"
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
            "moneyflow": score.get("moneyflow", "--"),
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
    filled_price = _safe_float(receipt.get("filled_price"), _safe_float(order.get("price"), 0.0))
    requested_price = _safe_float(order.get("price"), 0.0)
    quantity = _safe_int(receipt.get("filled_quantity", receipt.get("filled_qty")), _safe_int(order.get("quantity"), 0))
    slippage_pct = 0.0
    if requested_price > 0:
        slippage_pct = ((filled_price / requested_price) - 1.0) * 100
    fill_time = str(receipt.get("fill_time") or receipt.get("filled_at") or _now_iso())
    order_id = str(receipt.get("order_id") or card.get("order_id") or order.get("order_id") or "")
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
    is_ashare_sim = str(market or "").strip().lower() == "ashare" and str(capital_layer or "").strip().lower() == "simulated"
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


def _rank_symbols_by_score(symbols: list[str], scores_by_symbol: dict[str, dict[str, Any]]) -> list[str]:
    indexed = list(enumerate(symbols))

    def score_key(item: tuple[int, str]) -> tuple[float, int]:
        index, symbol = item
        score = scores_by_symbol.get(symbol) or {}
        return (_safe_float(score.get("combined", score.get("score")), 0.0), -index)

    return [symbol for _, symbol in sorted(indexed, key=score_key, reverse=True)]


def _candidate_pool_default(market: str, capital_layer: str, symbols: list[str]) -> dict[str, list[str]]:
    if str(market or "").strip().lower() == "ashare" and str(capital_layer or "").strip().lower() == "simulated":
        return {"candidate": [], "watch": [], "holdings": [], "universe": list(symbols)}
    return {"candidate": list(symbols), "watch": [], "holdings": [], "universe": list(symbols)}


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
    capital: float,
    existing_positions: list[dict[str, Any]],
    available_cash: float | None,
    orders: list[dict[str, Any]],
    scores_by_symbol: dict[str, dict[str, Any]],
    skipped_candidates: list[dict[str, Any]],
    risk_rejections: list[dict[str, Any]],
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
        cash_value = max(0.0, capital - sum(_position_value(position, capital) for position in existing_positions if isinstance(position, dict)))
    total_checked = max(1, len(orders) + len(skipped_candidates) + len(risk_rejections))
    candidates: list[dict[str, Any]] = []
    for order in orders:
        symbol = str(order.get("ts_code") or "")
        score = dict(scores_by_symbol.get(symbol, {}))
        score["ts_code"] = symbol
        score["weight"] = _safe_float(order.get("weight"), 0.0)
        score["belief_score"] = _safe_float(order.get("belief_score"), score.get("belief_score", 0.0))
        candidates.append(score)

    plan = plan_capital(
        holdings,
        cash_value,
        candidates=candidates,
        dynamic=True,
        total_capital=capital,
        market_context={
            "risk_rejection_rate": len(risk_rejections) / total_checked,
            "data_issue_rate": len(skipped_candidates) / total_checked,
        },
    ).to_dict()
    plan["enabled"] = True
    plan["market"] = market
    plan["existing_position_count"] = len({
        _position_symbol(position)
        for position in existing_positions
        if isinstance(position, dict) and _position_symbol(position)
    })
    plan["cash_source"] = "account_snapshot" if available_cash is not None and _safe_float(available_cash, -1.0) >= 0 else "capital_minus_positions"
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
        shares = int(budget // price)
        shares = (shares // 100) * 100
        if shares <= 0:
            position["shares"] = 0
            position["amount"] = 0.0
            position["weight"] = 0.0
            position["target_amount"] = round(budget, 2)
            continue
        amount = shares * price
        position["shares"] = shares
        position["amount"] = round(amount, 2)
        position["weight"] = round(amount / max(capital, 1.0), 6)
        position["target_amount"] = round(budget, 2)


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
    target_positions = _safe_int(capital_plan.get("target_positions"), max_portfolio_positions)
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
    if str(market).lower() != "ashare" or replacement_capacity <= 0 or not capital_plan.get("enabled"):
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
        if str(order.get("ts_code") or "") and _safe_float(budgets.get(str(order.get("ts_code") or "")), 0.0) <= 0
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
    updated["max_new_positions"] = max(_safe_int(updated.get("max_new_positions"), 0), len(allocated))
    return updated


def _position_symbol(position: dict[str, Any]) -> str:
    return str(position.get("ts_code") or position.get("symbol") or position.get("code") or "").strip()


def _position_quantity(position: dict[str, Any]) -> int:
    return _safe_int(position.get("quantity", position.get("shares", position.get("position_qty"))), 0)


def _position_sellable_quantity(position: dict[str, Any]) -> int:
    explicit = position.get("sellable_quantity", position.get("sellable_qty", position.get("available_qty")))
    if explicit is not None:
        return max(0, _safe_int(explicit, 0))
    return max(0, _position_quantity(position))


def _position_avg_price(position: dict[str, Any]) -> float:
    return _safe_float(position.get("avg_price", position.get("avg_cost", position.get("cost"))), 0.0)


def _position_last_price(position: dict[str, Any], fallback: float = 0.0) -> float:
    return _safe_float(
        position.get("last_price", position.get("mark_price", position.get("current_price", position.get("price")))),
        fallback,
    )


def _merge_ashare_sell_row(base: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    merged["shares"] = _safe_int(merged.get("shares"), 0) + _safe_int(row.get("shares"), 0)
    merged["quantity"] = _safe_int(merged.get("quantity"), 0) + _safe_int(row.get("quantity"), 0)
    merged["amount"] = round(_safe_float(merged.get("amount"), 0.0) + _safe_float(row.get("amount"), 0.0), 2)
    if merged["shares"] > 0 and merged["amount"] > 0:
        merged["price"] = round(merged["amount"] / merged["shares"], 4)
    merged["weight"] = -abs(_safe_float(merged.get("weight"), 0.0)) - abs(_safe_float(row.get("weight"), 0.0))
    reasons: list[str] = []
    for reason in [*(merged.get("rebalance_reasons") or []), *(row.get("rebalance_reasons") or [])]:
        if reason and reason not in reasons:
            reasons.append(str(reason))
    merged["rebalance_reasons"] = reasons
    merged["reason"] = ",".join(reasons)
    merged["has_score"] = bool(merged.get("has_score") or row.get("has_score"))
    merged["combined"] = min(_safe_float(merged.get("combined"), 0.0), _safe_float(row.get("combined"), 0.0))
    merged["pnl_pct"] = min(_safe_float(merged.get("pnl_pct"), 0.0), _safe_float(row.get("pnl_pct"), 0.0))
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
        candidate_symbol = str(candidate.get("ts_code") or candidate.get("symbol") or "").strip()
        if not candidate_symbol or candidate_symbol == symbol:
            continue
        score = scores_by_symbol.get(candidate_symbol) or {}
        combined = _safe_float(score.get("combined", score.get("score", candidate.get("combined", candidate.get("score")))), 0.0)
        if combined > best_score:
            best_score = combined
            best = {"ts_code": candidate_symbol, "combined": combined}
    return best


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

    target_positions = _safe_int(capital_plan.get("target_positions"), max_portfolio_positions)
    if target_positions < 0:
        target_positions = max_portfolio_positions
    sellable: list[dict[str, Any]] = []
    planned_by_symbol: dict[str, dict[str, Any]] = {}
    candidates_for_replacement = list(buy_candidates or [])
    existing_count = len({_position_symbol(position) for position in existing_positions if isinstance(position, dict) and _position_symbol(position)})
    effective_target = min(target_positions if target_positions > 0 else max_portfolio_positions, max_portfolio_positions)

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
        combined = _safe_float(score.get("combined", score.get("score", position.get("combined", position.get("score")))), 0.0)
        avg_price = _position_avg_price(position)
        price = _position_last_price(position, avg_price or default_price)
        mapped_market = str(score.get("market") or market)
        mapped_symbol = str(score.get("mapped_symbol") or symbol)
        price = _latest_price(reader, mapped_market, mapped_symbol, date, price or default_price)
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
            has_score
            and not reasons
            and effective_target > 0
            and existing_count >= effective_target
            and opportunity_score >= ASHARE_OPPORTUNITY_COST_MIN_ENTRY_SCORE
            and opportunity_gap >= ASHARE_OPPORTUNITY_COST_MIN_SCORE_GAP
        ):
            reasons.append("opportunity_cost")
        sellable.append(
            {
                "ts_code": symbol,
                "side": "sell",
                "shares": (sellable_quantity // 100) * 100,
                "quantity": quantity,
                "price": price,
                "weight": -abs(_safe_float(position.get("weight"), _position_value(position, capital) / max(capital, 1.0))),
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
                    "min_score_gap": ASHARE_OPPORTUNITY_COST_MIN_SCORE_GAP,
                } if "opportunity_cost" in reasons else {},
                "risk_audit_id": "",
            }
        )

    sellable_by_symbol: dict[str, dict[str, Any]] = {}
    for row in sellable:
        if row["shares"] <= 0:
            continue
        symbol = str(row["ts_code"])
        if symbol in sellable_by_symbol:
            sellable_by_symbol[symbol] = _merge_ashare_sell_row(sellable_by_symbol[symbol], row)
        else:
            sellable_by_symbol[symbol] = dict(row)

    for row in sellable_by_symbol.values():
        if row["rebalance_reasons"]:
            planned_by_symbol[row["ts_code"]] = row

    compression_target = target_positions if target_positions > 0 else max_portfolio_positions
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
    sells.sort(key=lambda row: (str(row.get("reason") or ""), _safe_float(row.get("combined"), 0.0)))
    return {
        "enabled": True,
        "target_positions": target_positions,
        "existing_position_count": existing_count,
        "planned_sell_count": len(sells),
        "sells": sells,
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
        for key in ("sim_capital", "cash", "available_cash", "equity", "net_liquidation", "shadow_capital"):
            value = _safe_float(source.get(key), -1.0)
            if value >= 0:
                return value
    return _default_capital_for_market(config.get("market"))


def _account_available_cash(account: Any, config: dict[str, Any], capital: float, existing_positions: list[dict[str, Any]]) -> float:
    for source in (account if isinstance(account, dict) else {}, config):
        if not isinstance(source, dict):
            continue
        for key in ("cash_available", "available_cash", "cash"):
            value = _safe_float(source.get(key), -1.0)
            if value >= 0:
                return value
    return max(0.0, capital - sum(_position_value(position, capital) for position in existing_positions if isinstance(position, dict)))


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
    return {"session": session, "trade_date": date, "capital_layer": capital_layer, "raw_result": result}


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
    return payload


def _execute_sim_order(deps: OrchestratorDeps, order: dict[str, Any], account: Any) -> dict[str, Any]:
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
        elif len(params) >= 2 and not any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in params.values()):
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
        if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    first_name = positional[0].name.lower() if positional else ""
    second_name = positional[1].name.lower() if len(positional) > 1 else ""
    if first_name in {"symbol", "ts_code", "ticker", "market_id"} and second_name in {"date", "trade_date"}:
        return False
    return (
        any(parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in signature.parameters.values())
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
        return {str(symbol): dict(score) for symbol, score in raw.items() if isinstance(score, dict)}
    if not isinstance(raw, list):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for item in raw:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], dict):
            normalized[str(item[0])] = dict(item[1])
        elif isinstance(item, dict):
            symbol = str(item.get("symbol") or item.get("ts_code") or item.get("market_id") or "").strip()
            scores = item.get("scores") if isinstance(item.get("scores"), dict) else item
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
        raw = deps.score_universe(date=date, universe=symbols, data_reader=reader, market=market)
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
                lambda mapped_market=mapped_market, mapped_symbols=mapped_symbols: _score_universe_for_market(
                    deps,
                    mapped_market,
                    mapped_symbols,
                    date,
                    reader,
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
                lambda mapped_market=mapped_market, mapped_symbol=mapped_symbol: _score_stock_for_market(
                    deps,
                    mapped_market,
                    mapped_symbol,
                    date,
                    reader,
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
        return deps.build_pool(date=date, universe=universe, market=market, reader=reader)
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


def _candidate_layer_breakdown(pool: dict[str, Any], universe_count: int) -> dict[str, int]:
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
        snapshot["evidence_coverage"] = round(_safe_float(score.get("evidence_coverage"), 0.0), 4)
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
    evidence_reason_summary: dict[str, dict[str, int]] = {name: {} for name in dimensions}
    evidence_source_summary: dict[str, dict[str, int]] = {name: {} for name in dimensions}
    evidence_coverage_distribution = {"zero": 0, "low": 0, "medium": 0, "high": 0, "full": 0}
    all_neutral_symbols: list[str] = []
    all_missing_evidence_symbols: list[str] = []
    all_missing_evidence_symbol_reasons: list[dict[str, Any]] = []
    evidence_coverage_values: list[float] = []
    for symbol, score in scores_by_symbol.items():
        if not isinstance(score, dict):
            continue
        combined = _safe_float(score.get("combined"), 0.0)
        neutral_dimensions = 0
        missing_evidence_dimensions = set(score.get("missing_evidence_dimensions") or [])
        evidence_sources = score.get("evidence_sources") if isinstance(score.get("evidence_sources"), dict) else {}
        for name in missing_evidence_dimensions:
            if name in missing_evidence_counts:
                missing_evidence_counts[name] += 1
        for name in dimensions:
            info = evidence_sources.get(name) if isinstance(evidence_sources, dict) else None
            if not isinstance(info, dict):
                continue
            if info.get("has_evidence") is False:
                missing_evidence_counts[name] += 1 if name not in missing_evidence_dimensions else 0
                reason = str(info.get("reason") or "missing_evidence")
                source = str(info.get("source") or "unknown")
                evidence_reason_summary[name][reason] = evidence_reason_summary[name].get(reason, 0) + 1
                evidence_source_summary[name][source] = evidence_source_summary[name].get(source, 0) + 1
        all_evidence_missing = len(missing_evidence_dimensions) >= len(dimensions)
        if isinstance(evidence_sources, dict) and evidence_sources:
            all_evidence_missing = all(
                isinstance(evidence_sources.get(name), dict) and evidence_sources[name].get("has_evidence") is False
                for name in dimensions
            )
        if all_evidence_missing:
            all_missing_evidence_symbols.append(str(symbol))
            if len(all_missing_evidence_symbol_reasons) < max(1, limit):
                reasons = {}
                for name in dimensions:
                    info = evidence_sources.get(name) if isinstance(evidence_sources, dict) else None
                    if isinstance(info, dict) and info.get("has_evidence") is False:
                        reasons[name] = info.get("reason") or "missing_evidence"
                    elif name in missing_evidence_dimensions:
                        reasons[name] = "missing_evidence"
                all_missing_evidence_symbol_reasons.append({"symbol": str(symbol), "reasons": reasons})
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
                source_info = evidence_sources.get(name) if isinstance(evidence_sources, dict) else None
                if name in missing_evidence_dimensions or (isinstance(source_info, dict) and source_info.get("has_evidence") is False):
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
    candidate_count = sum(1 for combined, _, _ in rows if combined >= candidate_threshold)
    watch_count = sum(1 for combined, _, _ in rows if watch_threshold <= combined < candidate_threshold)
    neutral_total = sum(neutral_counts.values())
    neutral_ratio = neutral_total / max(1, len(rows) * len(dimensions))
    all_neutral_ratio = len(all_neutral_symbols) / max(1, len(rows))
    all_missing_evidence_ratio = len(all_missing_evidence_symbols) / max(1, len(rows))
    missing_default_like_total = sum(missing_and_default_like_counts.values())
    missing_default_like_ratio = missing_default_like_total / max(1, len(rows) * len(dimensions))
    avg_evidence_coverage = sum(evidence_coverage_values) / max(1, len(evidence_coverage_values))
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
            if rows and (all_missing_evidence_ratio >= 0.5 or missing_default_like_ratio >= 0.5 or all_neutral_ratio >= 0.5)
            else "research_dimensions_mostly_neutral"
            if rows and neutral_ratio >= 0.75
            else "ok"
        ),
        "average_evidence_coverage": round(avg_evidence_coverage, 4),
        "evidence_coverage_distribution": evidence_coverage_distribution,
        "all_missing_evidence_symbol_count": len(all_missing_evidence_symbols),
        "all_missing_evidence_symbol_ratio": round(all_missing_evidence_ratio, 4),
        "all_missing_evidence_symbol_sample": all_missing_evidence_symbols[: max(1, limit)],
        "all_missing_evidence_symbol_reason_sample": all_missing_evidence_symbol_reasons,
        "top_scores": top_scores,
        "all_neutral_symbol_sample": all_neutral_symbols[: max(1, limit)],
        "neutral_default_like_dimension_counts": neutral_counts,
        "missing_dimension_counts": missing_counts,
        "missing_evidence_dimension_counts": missing_evidence_counts,
        "missing_and_default_like_dimension_counts": missing_and_default_like_counts,
        "missing_and_default_like_dimension_ratio": round(missing_default_like_ratio, 4),
        "evidence_reason_summary": {name: dict(counts) for name, counts in evidence_reason_summary.items() if counts},
        "evidence_source_summary": {name: dict(counts) for name, counts in evidence_source_summary.items() if counts},
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

    conditions = generate_conditions(pool=pool, scores_map=scores_by_symbol, date=date, reader=reader, market=market)
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
    market = _safe_stage("adapter.get_market", errors, market_adapter.get_market, default="unknown")
    account = _safe_stage("adapter.get_shadow_account", errors, market_adapter.get_shadow_account, default=f"{market}_shadow")
    config = _strategy_config(market_adapter)
    capital = _safe_float(config.get("shadow_capital"), _default_capital_for_market(market))
    method = str(config.get("portfolio_method", "conviction_weighted"))
    regime = str(config.get("regime", "unknown"))
    max_candidates = max(1, int(config.get("max_candidates", 20)))
    score_limit = max(max_candidates, int(config.get("score_universe_limit", max_candidates)))
    default_price = _safe_float(config.get("default_price"), 1.0)
    default_volatility = _safe_float(config.get("default_volatility"), 0.20)
    market_rules = config.get("market_rules") if isinstance(config.get("market_rules"), dict) else {}
    lot_size = _safe_float(market_rules.get("lot_size"), 1.0) if isinstance(market_rules, dict) else 1.0

    universe = _safe_stage("screening.universe", errors, lambda: market_adapter.get_universe(date), default=[])
    stage_calls.append("screening.universe")
    if not isinstance(universe, list):
        errors.append({"stage": "screening.universe", "status": "degraded", "error": "adapter returned non-list", "capital_layer": "shadow"})
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
        lambda: _build_pool_for_market(deps, market, date, list(scores_by_symbol), reader, scores_by_symbol),
        default=_candidate_pool_default(market, "shadow", list(scores_by_symbol)),
    )
    stage_calls.append("screening.candidate_pool")
    if not isinstance(pool, dict):
        pool = _candidate_pool_default(market, "shadow", list(scores_by_symbol))
    condition_lifecycle = _safe_stage(
        "screening.condition_lifecycle",
        errors,
        lambda: _run_condition_lifecycle(market, pool, scores_by_symbol, date, reader),
        default={"condition_count": 0, "trigger_replay_count": 0, "filled_replay_count": 0, "conditions": [], "trigger_replay": []},
    )
    stage_calls.append("screening.condition_lifecycle")

    candidates = _rank_symbols_by_score(
        _candidate_symbols(pool, list(scores_by_symbol), market=market, capital_layer="shadow"),
        scores_by_symbol,
    )[:max_candidates]
    orders_for_portfolio: list[dict[str, Any]] = []
    skipped_candidates: list[dict[str, Any]] = []
    signal_audit_by_symbol = {audit["ts_code"]: audit for audit in audits if audit.get("stage") == "signal"}

    for symbol in candidates:
        mapped_market, mapped_symbol = _safe_stage(
            "adapter.map_symbol_to_reader",
            errors,
            lambda symbol=symbol: market_adapter.map_symbol_to_reader(symbol),
            default=(market, symbol),
        )
        score = scores_by_symbol.get(symbol, {"combined": 0.5, "market": mapped_market, "capital_layer": "shadow"})
        parent = signal_audit_by_symbol.get(symbol, {}).get("audit_id", "")
        debate = _safe_stage(
            "adversarial.bull_bear_debate",
            errors,
            lambda symbol=mapped_symbol, score=score: deps.debate(symbol, score),
            default={"ts_code": mapped_symbol, "belief_score": 0.5, "bull_case": "degraded", "bear_case": "degraded"},
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
        volatility = _latest_volatility(reader, mapped_market, mapped_symbol, date, default_volatility)
        if price <= 0:
            skipped_candidates.append({"symbol": symbol, "reason": "missing_or_non_positive_price", "price": price, "capital_layer": "shadow"})
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
        risk = _safe_stage("risk.pre_trade_check", errors, lambda: deps.risk_check(risk_order, {"positions": []}), default={"approved": False, "adjusted_weight": 0.0, "reasons": ["degraded"]})
        stage_calls.append("risk.pre_trade_check")
        if not isinstance(risk, dict):
            risk = {"approved": False, "adjusted_weight": 0.0, "reasons": ["invalid risk result"]}
        risk_audit = _record_audit(
            deps,
            "risk",
            symbol,
            parent_audit_id=decision_audit["audit_id"],
            payload=risk,
            metadata={"date": date, "account": account},
        )
        audits.append(risk_audit)
        if not risk.get("approved") or _safe_float(risk.get("adjusted_weight"), 0.0) <= 0:
            continue
        orders_for_portfolio.append({
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
        })

    portfolio = _safe_stage(
        "portfolio.constructor",
        errors,
        lambda: deps.construct(orders_for_portfolio, capital, method=method, regime=regime),
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
            errors.append({"stage": "execution.shadow_broker", "status": "skipped", "symbol": symbol, "reason": "non-positive quantity or price", "capital_layer": "shadow"})
            continue
        trade = _safe_stage("execution.shadow_broker", errors, lambda order=order: deps.record_shadow(order, account), default={"recorded": False, "status": "degraded"})
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
        risk = {"approved": True, "adjusted_weight": position.get("weight"), "adjustments": [], "reasons": []}
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
        pending = _safe_stage("signals.pending", errors, lambda card=card: _write_pending_signal(card, signals_dir), default={"status": "degraded", "recorded": False})
        stage_calls.append("signals.pending")
        email_notification = {"status": "skipped", "reason": "signal not newly pending", "template": "trading_signal"}
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
            payload={"pending_signal": pending, "email_notification": email_notification, "capital_layer": "shadow"},
            metadata={"date": date, "account": account},
        )
        audits.append(result_audit)
        records.append({"symbol": symbol, "order": order, "trade": trade, "pending_signal": pending, "email_notification": email_notification})

    review = _safe_stage("review.daily_review", errors, lambda: deps.run_review(date, session="close"), default={"session": "close", "error": "degraded", "trade_date": date})
    stage_calls.append("review.daily_review")

    return {
        "market": market,
        "date": date,
        "capital_layer": "shadow",
        "account": account,
        "state": "degraded" if errors else "ok",
        "stage_calls": stage_calls,
        "universe_count": len(universe),
        "candidate_count": len(candidates),
        "order_count": len(orders_for_portfolio),
        "skipped_candidate_count": len(skipped_candidates),
        "skipped_candidates": skipped_candidates[:20],
        "recorded_count": sum(1 for record in records if record["trade"].get("recorded")),
        "portfolio": portfolio,
        "records": records,
        "audit_events": audits,
        "errors": errors,
        "review": review,
        "condition_lifecycle": condition_lifecycle,
        "generated_at": _now_iso(),
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
    market = _safe_stage("adapter.get_market", errors, market_adapter.get_market, default="unknown", capital_layer=capital_layer)
    sim_account_getter = getattr(market_adapter, "get_sim_account", None)
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
    config = _strategy_config(market_adapter)
    account = _account_name(account_obj, f"{market}_simulated")
    existing_positions = _account_positions(account_obj, config)
    capital = _account_capital(account_obj, config)
    account_cash_available = _account_available_cash(account_obj, config, capital, existing_positions)
    method = str(config.get("portfolio_method", "conviction_weighted"))
    regime = str(config.get("regime", "unknown"))
    max_candidates = max(1, int(config.get("max_candidates", 20)))
    score_limit = max(max_candidates, int(config.get("score_universe_limit", max_candidates)))
    max_portfolio_positions = max(1, int(config.get("max_portfolio_positions", config.get("max_positions", 9999))))
    default_price = _safe_float(config.get("default_price"), 1.0)
    default_volatility = _safe_float(config.get("default_volatility"), 0.20)

    universe = _safe_stage("screening.universe", errors, lambda: market_adapter.get_universe(date), default=[], capital_layer=capital_layer)
    stage_calls.append("screening.universe")
    if not isinstance(universe, list):
        errors.append({"stage": "screening.universe", "status": "degraded", "error": "adapter returned non-list", "capital_layer": capital_layer})
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

    pool = _safe_stage(
        "screening.candidate_pool",
        errors,
        lambda: _build_pool_for_market(deps, market, date, list(scores_by_symbol), reader, scores_by_symbol),
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
        default={"condition_count": 0, "trigger_replay_count": 0, "filled_replay_count": 0, "conditions": [], "trigger_replay": []},
        capital_layer=capital_layer,
    )
    stage_calls.append("screening.condition_lifecycle")

    candidates = _rank_symbols_by_score(
        _candidate_symbols(pool, list(scores_by_symbol), market=market, capital_layer=capital_layer),
        scores_by_symbol,
    )[:max_candidates]
    layer_breakdown = _candidate_layer_breakdown(pool, len(universe))
    candidate_decisions: dict[str, dict[str, Any]] = {
        str(symbol): {
            **_candidate_score_snapshot(str(symbol), scores_by_symbol.get(str(symbol), {})),
            "layer": "candidate",
            "status": "selected_for_review",
        }
        for symbol in candidates
    }
    candidate_layers = {
        str(symbol): "candidate"
        for symbol in ((pool.get("candidate", []) if isinstance(pool, dict) else []) or [])
        if symbol
    }
    orders_for_portfolio: list[dict[str, Any]] = []
    skipped_candidates: list[dict[str, Any]] = []
    risk_rejections: list[dict[str, Any]] = []
    execution_skips: list[dict[str, Any]] = []
    signal_audit_by_symbol = {audit["ts_code"]: audit for audit in audits if audit.get("stage") == "signal"}
    risk_portfolio = {
        "positions": list(existing_positions),
        "total_exposure": sum(_safe_float(position.get("weight"), 0.0) for position in existing_positions),
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
        score = scores_by_symbol.get(symbol, {"combined": 0.5, "market": mapped_market, "capital_layer": capital_layer, "account_type": account_type})
        parent = signal_audit_by_symbol.get(symbol, {}).get("audit_id", "")
        debate = _safe_stage(
            "adversarial.bull_bear_debate",
            errors,
            lambda symbol=mapped_symbol, score=score: deps.debate(symbol, score),
            default={"ts_code": mapped_symbol, "belief_score": 0.5, "bull_case": "degraded", "bear_case": "degraded"},
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
            payload={"debate": debate, "capital_layer": capital_layer, "account_type": account_type},
            metadata={"date": date, "account": account, "account_type": account_type},
            capital_layer=capital_layer,
        )
        audits.append(decision_audit)

        price = _latest_price(reader, mapped_market, mapped_symbol, date, default_price)
        volatility = _latest_volatility(reader, mapped_market, mapped_symbol, date, default_volatility)
        if price <= 0:
            skipped_candidates.append({"symbol": symbol, "reason": "missing_or_non_positive_price", "price": price, "capital_layer": capital_layer})
            candidate_decisions.setdefault(symbol, {"symbol": symbol})["price"] = price
            candidate_decisions[symbol]["status"] = "dropped"
            candidate_decisions[symbol]["drop_reason"] = "missing_or_non_positive_price"
            continue
        candidate_decisions.setdefault(symbol, {"symbol": symbol})["price"] = round(price, 4)
        candidate_decisions[symbol]["belief_score"] = round(_safe_float(debate.get("belief_score"), 0.5), 4)
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
        risk = _safe_stage(
            "risk.pre_trade_check",
            errors,
            lambda: deps.risk_check(risk_order, risk_portfolio),
            default={"approved": False, "adjusted_weight": 0.0, "reasons": ["degraded"]},
            capital_layer=capital_layer,
        )
        stage_calls.append("risk.pre_trade_check")
        if not isinstance(risk, dict):
            risk = {"approved": False, "adjusted_weight": 0.0, "reasons": ["invalid risk result"]}
        risk["capital_layer"] = capital_layer
        risk["account_type"] = account_type
        candidate_decisions[symbol]["proposed_weight"] = round(_safe_float(proposed_weight, 0.0), 6)
        candidate_decisions[symbol]["risk_approved"] = bool(risk.get("approved"))
        candidate_decisions[symbol]["risk_adjusted_weight"] = round(_safe_float(risk.get("adjusted_weight"), 0.0), 6)
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
        if not risk.get("approved") or _safe_float(risk.get("adjusted_weight"), 0.0) <= 0:
            risk_rejections.append(
                {
                    "symbol": symbol,
                    "approved": bool(risk.get("approved")),
                    "adjusted_weight": _safe_float(risk.get("adjusted_weight"), 0.0),
                    "reasons": risk.get("reasons", []),
                    "capital_layer": capital_layer,
                }
            )
            candidate_decisions[symbol]["status"] = "dropped"
            candidate_decisions[symbol]["drop_reason"] = "risk_rejected"
            continue
        candidate_decisions[symbol]["status"] = "risk_approved"
        orders_for_portfolio.append({
            "ts_code": symbol,
            "belief_score": _safe_float(debate.get("belief_score"), 0.5),
            "volatility": volatility,
            "sector": str(score.get("sector", "unknown")),
            "price": price,
            "weight": _safe_float(risk.get("adjusted_weight"), proposed_weight),
            "risk_audit_id": risk_audit["audit_id"],
            "mapped_market": mapped_market,
            "mapped_symbol": mapped_symbol,
        })
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
        risk_portfolio["total_exposure"] = _safe_float(risk_portfolio.get("total_exposure"), 0.0) + _safe_float(
            risk.get("adjusted_weight"),
            proposed_weight,
        )

    ranked_orders_for_portfolio = sorted(
        orders_for_portfolio,
        key=lambda row: (
            _safe_float(scores_by_symbol.get(str(row.get("ts_code")), {}).get("combined"), 0.0),
            _safe_float(row.get("belief_score"), 0.0),
        ),
        reverse=True,
    )
    capital_plan = _ashare_dynamic_capital_plan(
        market=market,
        capital=capital,
        existing_positions=existing_positions,
        available_cash=account_cash_available,
        orders=orders_for_portfolio,
        scores_by_symbol=scores_by_symbol,
        skipped_candidates=skipped_candidates,
        risk_rejections=risk_rejections,
    )
    stage_calls.append("portfolio.capital_plan")
    rebalance = _ashare_rebalance_plan(
        market=market,
        date=date,
        reader=reader,
        existing_positions=existing_positions,
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
    base_position_capacity = _max_new_positions(existing_positions, max_portfolio_positions)
    if capital_plan.get("enabled"):
        base_position_capacity = min(base_position_capacity, _safe_int(capital_plan.get("max_new_positions"), 0))
    replacement_capacity = _ashare_post_sell_buy_capacity(
        market=market,
        existing_positions=existing_positions,
        capital_plan=capital_plan,
        rebalance=rebalance,
        max_portfolio_positions=max_portfolio_positions,
    )
    position_capacity = max(base_position_capacity, replacement_capacity)
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
                "capital_plan_capacity_zero" if position_capacity <= 0 else "position_capacity_limit"
            )
        else:
            candidate_decisions[symbol]["status"] = "portfolio_input"
    orders_for_portfolio = [
        order
        for order in ranked_orders_for_portfolio
        if str(order.get("ts_code") or "") not in planned_sell_symbols
    ][:position_capacity]
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
        lambda: deps.construct(orders_for_portfolio, capital, method=method, regime=regime),
        default={"positions": [], "total_weight": 0.0, "cash_weight": 1.0},
        capital_layer=capital_layer,
    )
    stage_calls.append("portfolio.constructor")
    if not isinstance(portfolio, dict):
        portfolio = {"positions": [], "total_weight": 0.0, "cash_weight": 1.0}
    portfolio["capital_layer"] = capital_layer
    portfolio["account_type"] = account_type
    portfolio["existing_positions"] = existing_positions
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
        shares = _safe_float(position.get("shares"), 0.0) if isinstance(position, dict) else 0.0
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
            candidate_decisions[symbol]["portfolio_amount"] = round(_safe_float(position.get("amount"), 0.0), 2)
    capital_plan_decision = {
        "enabled": bool(capital_plan.get("enabled")),
        "risk_mode": capital_plan.get("risk_mode"),
        "target_positions": capital_plan.get("target_positions"),
        "max_new_positions": capital_plan.get("max_new_positions"),
        "position_capacity": position_capacity,
        "base_position_capacity": base_position_capacity,
        "replacement_capacity": replacement_capacity,
        "available_cash": round(account_cash_available, 2),
        "reasons": capital_plan.get("reasons", []),
        "notes": capital_plan.get("notes", []),
    }
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
    execution_positions = [*(rebalance.get("sells", []) or []), *((portfolio.get("positions", []) or []))]
    for position in execution_positions:
        if not isinstance(position, dict) or not position.get("ts_code"):
            continue
        symbol = str(position["ts_code"])
        meta = order_meta.get(symbol, {})
        side = str(position.get("side") or "buy").lower()
        quantity = _execution_quantity(market, side, position.get("shares"))
        order_id = _make_order_id("SIM-", market, symbol, date)
        idempotency_key = _sim_idempotency_key(market, account, symbol, date, side)
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
            "note": str(position.get("reason") or f"orchestrator sim loop {market} {date}"),
            "candidate_pool_layer": str(position.get("candidate_pool_layer") or candidate_layers.get(symbol) or ("rebalance" if side == "sell" else "candidate")),
            "execution_source": (
                "ashare_candidate_layer"
                if str(market).lower() == "ashare" and side == "buy"
                else "ashare_rebalance_sell"
                if str(market).lower() == "ashare" and side == "sell"
                else "orchestrator_sim_loop"
            ),
        }
        if order["quantity"] <= 0 or order["price"] <= 0:
            skip = {"stage": "execution.sim_broker", "status": "skipped", "symbol": symbol, "reason": "non-positive quantity or price", "capital_layer": capital_layer}
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
            email_notification = {"status": "skipped", "reason": "duplicate same-day sim signal", "template": "trade_receipt"}
            records.append({"symbol": symbol, "order": order, "receipt": receipt, "signal_result": signal_result, "email_notification": email_notification})
            continue
        receipt = _safe_stage(
            "execution.sim_broker",
            errors,
            lambda order=order: _execute_sim_order(deps, order, account_obj),
            default={"order_id": order_id, "status": "failed", "message": "degraded"},
            capital_layer=capital_layer,
        )
        stage_calls.append("execution.sim_broker")
        if not isinstance(receipt, dict):
            receipt = {"order_id": order_id, "status": "failed", "message": "invalid sim broker receipt"}
        receipt.setdefault("order_id", order_id)
        receipt["capital_layer"] = capital_layer
        receipt["account_type"] = account_type
        execution_audit = _record_audit(
            deps,
            "execution",
            symbol,
            parent_audit_id=str(meta.get("risk_audit_id", "")),
            payload={"order": order, "sim_broker": receipt, "capital_layer": capital_layer, "account_type": account_type},
            metadata={"date": date, "account": account, "account_type": account_type},
            capital_layer=capital_layer,
        )
        audits.append(execution_audit)
        risk = {"approved": True, "adjusted_weight": position.get("weight"), "adjustments": [], "reasons": [], "capital_layer": capital_layer, "account_type": account_type}
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
            lambda card=card, receipt=receipt: _write_execution_signal(card, receipt, signals_dir),
            default={"status": "degraded", "recorded": False},
            capital_layer=capital_layer,
        )
        stage_calls.append("signals.sim_execution")
        email_notification = {"status": "skipped", "reason": "sim order not filled", "template": "trade_receipt"}
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
            payload={"signal_result": signal_result, "email_notification": email_notification, "capital_layer": capital_layer, "account_type": account_type},
            metadata={"date": date, "account": account, "account_type": account_type},
            capital_layer=capital_layer,
        )
        audits.append(result_audit)
        records.append({"symbol": symbol, "order": order, "receipt": receipt, "signal_result": signal_result, "email_notification": email_notification})

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
        lambda: _run_review_for_layer(deps, date, session="close", capital_layer=capital_layer),
        default={"session": "close", "error": "degraded", "trade_date": date, "capital_layer": capital_layer},
        capital_layer=capital_layer,
    )
    stage_calls.append("review.daily_review")

    filled_count = sum(1 for record in records if record["signal_result"].get("status") == "filled")
    failed_count = sum(1 for record in records if record["signal_result"].get("status") == "failed")
    pending_count = sum(1 for record in records if record["signal_result"].get("status") == "pending")
    duplicate_count = sum(1 for record in records if record["signal_result"].get("status") == "duplicate")
    planned_order_count = len(execution_positions)
    portfolio_positions = len([row for row in (portfolio.get("positions", []) or []) if isinstance(row, dict) and row.get("ts_code")])
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
        score_diagnostics=_score_diagnostics(scores_by_symbol, actual_candidate_count=len(candidates)) if str(market).lower() == "ashare" else None,
        candidate_layer_breakdown=layer_breakdown if str(market).lower() == "ashare" else None,
        candidate_decision_trace=list(candidate_decisions.values()) if str(market).lower() == "ashare" else None,
        capital_plan_decision=capital_plan_decision if str(market).lower() == "ashare" else None,
        portfolio_decision=portfolio_decision if str(market).lower() == "ashare" else None,
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
        "duplicate_count": duplicate_count,
        "no_trade_explanation": no_trade_explanation,
        "candidate_layer_breakdown": layer_breakdown,
        "candidate_decision_trace": list(candidate_decisions.values())[:20],
        "capital_plan_decision": capital_plan_decision,
        "portfolio_decision": portfolio_decision,
        "capital_plan": capital_plan,
        "capital_plan_log": capital_plan_log,
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
    session = "lunch" if str(lunch_or_close).lower() in {"lunch", "midday", "day"} else "close"
    shadow_trades = _load_shadow_trades_for_date(date)
    try:
        result = deps.run_review(date, session=session)
    except Exception as exc:
        result = {"session": session, "trade_date": date, "error": str(exc)}
    if isinstance(result, dict):
        result.update({
            "market": market,
            "capital_layer": "shadow",
            "job": "orchestrator_daily_review",
            "generated_at": _now_iso(),
            "shared_shadow_trade_count": len(shadow_trades),
        })
    return result


__all__ = [
    "MarketAdapter",
    "OrchestratorDeps",
    "run_daily_review",
    "run_sim_loop",
    "run_shadow_loop",
]
