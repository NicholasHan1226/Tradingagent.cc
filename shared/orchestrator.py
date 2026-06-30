#!/usr/bin/env python3
"""Market-agnostic shadow trading orchestrator."""

from __future__ import annotations

import json
import inspect
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from shared.markets.base import MarketAdapter

ROOT = Path(__file__).resolve().parent.parent
SIGNALS_DIR = ROOT / "signals"

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
    from shared.screening.six_dimension_scorer import score_stock

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
    try:
        rows = reader.get_bars_daily(market, symbol, None, date)
    except Exception:
        return default
    if not rows:
        return default
    return max(_safe_float(rows[-1].get("close"), default), 0.0) or default


def _latest_volatility(reader: Any, market: str, symbol: str, date: str, default: float) -> float:
    try:
        rows = reader.get_bars_daily(market, symbol, None, date)
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

    machine = SignalStateMachine(signals_dir)
    try:
        return machine.write_pending(card)
    except SignalStateConflict as exc:
        return {
            "order_id": card.get("order_id", ""),
            "status": "duplicate",
            "recorded": False,
            "message": str(exc),
            "signal_card": card,
        }


def _write_execution_signal(
    card: dict[str, Any],
    receipt: dict[str, Any],
    signals_dir: Path = SIGNALS_DIR,
) -> dict[str, Any]:
    from shared.execution.signal_state_machine import SignalStateConflict, SignalStateMachine

    machine = SignalStateMachine(signals_dir)
    try:
        pending = machine.write_pending(card)
    except SignalStateConflict as exc:
        return {
            "order_id": card.get("order_id", ""),
            "status": "duplicate",
            "recorded": False,
            "message": str(exc),
            "signal_card": card,
        }

    status = str(receipt.get("status", "")).strip().lower()
    retryable = bool(receipt.get("retryable")) or status in {"pending", "queued", "retryable", "unfilled"}
    rejected = status in {"rejected", "reject", "failed", "failure", "error", "cancelled", "canceled"}
    if retryable:
        return {"order_id": card.get("order_id", ""), "status": "pending", "pending_signal": pending}
    if rejected:
        reason = str(receipt.get("message") or receipt.get("reason") or status or "sim order rejected")
        failed = machine.fail(str(card.get("order_id", "")), reason=reason)
        return {"order_id": card.get("order_id", ""), "status": "failed", "pending_signal": pending, "failed_signal": failed}

    fill_info = dict(receipt)
    if "filled_quantity" in fill_info and "filled_qty" not in fill_info:
        fill_info["filled_qty"] = fill_info["filled_quantity"]
    if "filled_qty" in fill_info and "filled_quantity" not in fill_info:
        fill_info["filled_quantity"] = fill_info["filled_qty"]
    fill_info.setdefault("filled_price", card.get("price", 0.0))
    fill_info.setdefault("filled_quantity", card.get("quantity", 0))
    fill_info.setdefault("filled_qty", fill_info.get("filled_quantity", card.get("quantity", 0)))
    filled = machine.fill(str(card.get("order_id", "")), fill_info)
    return {"order_id": card.get("order_id", ""), "status": "filled", "pending_signal": pending, "filled_signal": filled}


def _make_order_id(prefix: str, market: str, symbol: str, date: str) -> str:
    return f"{prefix}{market}-{symbol}-{date}-{uuid.uuid4().hex[:8]}".replace("/", "-")


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
    return {
        "order_id": order_id,
        "ts_code": symbol,
        "market": market,
        "direction": order.get("side", "buy"),
        "quantity": _safe_int(order.get("quantity"), 0),
        "price": _safe_float(order.get("price"), 0.0),
        "strategy_name": account,
        "timestamp": _now_iso(),
        "status": "pending",
        "capital_layer": capital_layer,
        "account_type": account_type,
        "manual_confirm_required": False,
        "direct_execution": direct_execution,
        "risk_check": {
            "passed": bool(risk.get("approved", False)),
            "adjusted_weight": risk.get("adjusted_weight"),
            "adjustments": risk.get("adjustments", []),
            "reasons": risk.get("reasons", []),
        },
        "shadow_trade_id": trade.get("trade_id", ""),
        "source_audit_id": audit_id,
        "valid_until": _date_iso(date),
        "idempotency_key": order_id,
        "evidence_refs": [audit_id],
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


def _candidate_symbols(pool: dict[str, Any], fallback_universe: list[str]) -> list[str]:
    symbols: list[str] = []
    for layer in ("holdings", "watch", "candidate"):
        values = pool.get(layer, []) if isinstance(pool, dict) else []
        if isinstance(values, list):
            symbols.extend(str(item) for item in values if item)
    if not symbols:
        symbols = [str(item) for item in fallback_universe if item]
    seen: set[str] = set()
    ordered: list[str] = []
    for symbol in symbols:
        if symbol not in seen:
            seen.add(symbol)
            ordered.append(symbol)
    return ordered


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


def _account_capital(account: Any, config: dict[str, Any]) -> float:
    for source in (account if isinstance(account, dict) else {}, config):
        if not isinstance(source, dict):
            continue
        for key in ("sim_capital", "cash", "available_cash", "equity", "net_liquidation", "shadow_capital"):
            value = _safe_float(source.get(key), -1.0)
            if value >= 0:
                return value
    return 100000.0


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


def _execute_sim_order(deps: OrchestratorDeps, order: dict[str, Any], account: Any) -> dict[str, Any]:
    if deps.execute_sim_order is None:
        raise RuntimeError("sim_broker.execute_sim_order is unavailable")
    try:
        signature = inspect.signature(deps.execute_sim_order)
    except (TypeError, ValueError):
        receipt = deps.execute_sim_order(order)
    else:
        params = signature.parameters
        if "account" in params:
            receipt = deps.execute_sim_order(order, account=account)
        elif "sim_account" in params:
            receipt = deps.execute_sim_order(order, sim_account=account)
        elif len(params) >= 2 and not any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in params.values()):
            receipt = deps.execute_sim_order(order, account)
        else:
            receipt = deps.execute_sim_order(order)
    return receipt if isinstance(receipt, dict) else {"status": "failed", "message": "invalid sim broker receipt"}


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


def _build_pool_for_market(
    deps: OrchestratorDeps,
    market: str,
    date: str,
    universe: list[str],
    reader: Any,
) -> Any:
    try:
        signature = inspect.signature(deps.build_pool)
    except (TypeError, ValueError):
        return deps.build_pool(date=date, universe=universe, market=market, reader=reader)
    if "market" in signature.parameters or "reader" in signature.parameters or "market_adapter" in signature.parameters:
        return deps.build_pool(date=date, universe=universe, market=market, reader=reader)
    return deps.build_pool(date=date, universe=universe)


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
    capital = _safe_float(config.get("shadow_capital"), 100000.0)
    method = str(config.get("portfolio_method", "conviction_weighted"))
    regime = str(config.get("regime", "unknown"))
    max_candidates = max(1, int(config.get("max_candidates", 20)))
    default_price = _safe_float(config.get("default_price"), 1.0)
    default_volatility = _safe_float(config.get("default_volatility"), 0.20)

    universe = _safe_stage("screening.universe", errors, lambda: market_adapter.get_universe(date), default=[])
    stage_calls.append("screening.universe")
    if not isinstance(universe, list):
        errors.append({"stage": "screening.universe", "status": "degraded", "error": "adapter returned non-list", "capital_layer": "shadow"})
        universe = []

    scores_by_symbol: dict[str, dict[str, Any]] = {}
    for symbol in universe[:max_candidates]:
        mapped_market, mapped_symbol = _safe_stage(
            "adapter.map_symbol_to_reader",
            errors,
            lambda symbol=symbol: market_adapter.map_symbol_to_reader(symbol),
            default=(market, symbol),
        )
        score = _safe_stage(
            "screening.six_dim",
            errors,
            lambda symbol=mapped_symbol: _score_stock_for_market(deps, market, symbol, date, reader),
            default={"combined": 0.5},
        )
        stage_calls.append("screening.six_dim")
        if not isinstance(score, dict):
            score = {"combined": 0.5}
        score["capital_layer"] = "shadow"
        score["market"] = market
        scores_by_symbol[symbol] = score
        audit = _record_audit(
            deps,
            "signal",
            symbol,
            payload={"scores": score, "market": market},
            metadata={"date": date, "account": account},
        )
        audits.append(audit)

    pool = _safe_stage(
        "screening.candidate_pool",
        errors,
        lambda: _build_pool_for_market(deps, market, date, list(scores_by_symbol), reader),
        default={"candidate": list(scores_by_symbol), "watch": [], "holdings": [], "universe": list(scores_by_symbol)},
    )
    stage_calls.append("screening.candidate_pool")
    if not isinstance(pool, dict):
        pool = {"candidate": list(scores_by_symbol), "watch": [], "holdings": [], "universe": list(scores_by_symbol)}

    candidates = _candidate_symbols(pool, list(scores_by_symbol))[:max_candidates]
    orders_for_portfolio: list[dict[str, Any]] = []
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
            "quantity": _safe_int(position.get("shares"), 0),
            "price": _safe_float(position.get("price"), 0.0),
            "trade_date": date,
            "capital_layer": "shadow",
            "note": f"orchestrator shadow loop {market} {date}",
        }
        if order["quantity"] <= 0 or order["price"] <= 0:
            errors.append({"stage": "execution.shadow_broker", "status": "skipped", "symbol": symbol, "reason": "non-positive quantity or price", "capital_layer": "shadow"})
            continue
        trade = _safe_stage("execution.shadow_broker", errors, lambda order=order: deps.record_shadow(order, account, market=market), default={"recorded": False, "status": "degraded"})
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
        result_audit = _record_audit(
            deps,
            "result",
            symbol,
            parent_audit_id=execution_audit["audit_id"],
            payload={"pending_signal": pending, "capital_layer": "shadow"},
            metadata={"date": date, "account": account},
        )
        audits.append(result_audit)
        records.append({"symbol": symbol, "order": order, "trade": trade, "pending_signal": pending})

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
        "recorded_count": sum(1 for record in records if record["trade"].get("recorded")),
        "portfolio": portfolio,
        "records": records,
        "audit_events": audits,
        "errors": errors,
        "review": review,
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
    method = str(config.get("portfolio_method", "conviction_weighted"))
    regime = str(config.get("regime", "unknown"))
    max_candidates = max(1, int(config.get("max_candidates", 20)))
    default_price = _safe_float(config.get("default_price"), 1.0)
    default_volatility = _safe_float(config.get("default_volatility"), 0.20)

    universe = _safe_stage("screening.universe", errors, lambda: market_adapter.get_universe(date), default=[], capital_layer=capital_layer)
    stage_calls.append("screening.universe")
    if not isinstance(universe, list):
        errors.append({"stage": "screening.universe", "status": "degraded", "error": "adapter returned non-list", "capital_layer": capital_layer})
        universe = []

    scores_by_symbol: dict[str, dict[str, Any]] = {}
    for symbol in universe[:max_candidates]:
        mapped_market, mapped_symbol = _safe_stage(
            "adapter.map_symbol_to_reader",
            errors,
            lambda symbol=symbol: market_adapter.map_symbol_to_reader(symbol),
            default=(market, symbol),
            capital_layer=capital_layer,
        )
        score = _safe_stage(
            "screening.six_dim",
            errors,
            lambda symbol=mapped_symbol: _score_stock_for_market(deps, market, symbol, date, reader),
            default={"combined": 0.5},
            capital_layer=capital_layer,
        )
        stage_calls.append("screening.six_dim")
        if not isinstance(score, dict):
            score = {"combined": 0.5}
        score["capital_layer"] = capital_layer
        score["account_type"] = account_type
        score["market"] = market
        scores_by_symbol[symbol] = score
        audit = _record_audit(
            deps,
            "signal",
            symbol,
            payload={"scores": score, "market": market, "capital_layer": capital_layer},
            metadata={"date": date, "account": account, "account_type": account_type},
            capital_layer=capital_layer,
        )
        audits.append(audit)

    pool = _safe_stage(
        "screening.candidate_pool",
        errors,
        lambda: _build_pool_for_market(deps, market, date, list(scores_by_symbol), reader),
        default={"candidate": list(scores_by_symbol), "watch": [], "holdings": [], "universe": list(scores_by_symbol)},
        capital_layer=capital_layer,
    )
    stage_calls.append("screening.candidate_pool")
    if not isinstance(pool, dict):
        pool = {"candidate": list(scores_by_symbol), "watch": [], "holdings": [], "universe": list(scores_by_symbol)}

    candidates = _candidate_symbols(pool, list(scores_by_symbol))[:max_candidates]
    orders_for_portfolio: list[dict[str, Any]] = []
    signal_audit_by_symbol = {audit["ts_code"]: audit for audit in audits if audit.get("stage") == "signal"}
    risk_portfolio = {
        "positions": existing_positions,
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
            continue
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

    order_meta = {order["ts_code"]: order for order in orders_for_portfolio}
    for position in portfolio.get("positions", []) or []:
        if not isinstance(position, dict) or not position.get("ts_code"):
            continue
        symbol = str(position["ts_code"])
        meta = order_meta.get(symbol, {})
        order_id = _make_order_id("SIM-", market, symbol, date)
        order = {
            "order_id": order_id,
            "ts_code": symbol,
            "side": "buy",
            "quantity": _safe_int(position.get("shares"), 0),
            "price": _safe_float(position.get("price"), 0.0),
            "mid_price": _safe_float(position.get("price"), 0.0),
            "limit_price": _safe_float(position.get("price"), 0.0),
            "order_type": "market",
            "trade_date": date,
            "strategy_name": account,
            "market": market,
            "capital_layer": capital_layer,
            "account_type": account_type,
            "note": f"orchestrator sim loop {market} {date}",
        }
        if order["quantity"] <= 0 or order["price"] <= 0:
            errors.append({"stage": "execution.sim_broker", "status": "skipped", "symbol": symbol, "reason": "non-positive quantity or price", "capital_layer": capital_layer})
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
        result_audit = _record_audit(
            deps,
            "result",
            symbol,
            parent_audit_id=execution_audit["audit_id"],
            payload={"signal_result": signal_result, "capital_layer": capital_layer, "account_type": account_type},
            metadata={"date": date, "account": account, "account_type": account_type},
            capital_layer=capital_layer,
        )
        audits.append(result_audit)
        records.append({"symbol": symbol, "order": order, "receipt": receipt, "signal_result": signal_result})

    review = _safe_stage(
        "review.daily_review",
        errors,
        lambda: _run_review_for_layer(deps, date, session="close", capital_layer=capital_layer),
        default={"session": "close", "error": "degraded", "trade_date": date, "capital_layer": capital_layer},
        capital_layer=capital_layer,
    )
    stage_calls.append("review.daily_review")

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
        "order_count": len(orders_for_portfolio),
        "filled_count": sum(1 for record in records if record["signal_result"].get("status") == "filled"),
        "failed_count": sum(1 for record in records if record["signal_result"].get("status") == "failed"),
        "pending_count": sum(1 for record in records if record["signal_result"].get("status") == "pending"),
        "portfolio": portfolio,
        "records": records,
        "audit_events": audits,
        "errors": errors,
        "review": review,
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
