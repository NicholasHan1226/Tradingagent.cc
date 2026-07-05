#!/usr/bin/env python3
"""Automated simulated daily pipeline across markets."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from shared.data.reader import TradingagentDataReader
from shared.execution.decision_engine import DecisionEngine
from shared.markets.evolution_engine import evaluate_and_adjust
from shared.markets.performance_tracker import load_style_weights
from shared.markets.safety import reject_real_execution_payload
from shared.markets.style_config import styles_dir_for_market
from shared.markets.style_runner import StyleRunner
from shared.research.multi_perspective import MultiPerspectiveAnalyzer
from shared.screening.fundamental_analyzer import FundamentalAnalyzer


TRADINGAGENT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REVIEW_ROOT = TRADINGAGENT_ROOT / "shared" / "review"
ACTIVE_MARKETS = ("crypto", "us", "pm", "ashare")
PIPELINE_STAGES = (
    ("pre_market_scan", "07:30", "load candidates"),
    ("research_phase", "09:00", "fundamental and multi-perspective research"),
    ("decision_phase", "09:25", "consensus and portfolio rebalance"),
    ("execute_sim", "09:30", "simulated style execution"),
    ("daily_review", "16:00", "daily review and evolution cycle"),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _normalize_market(market: Any) -> str:
    value = str(market or "").strip().lower()
    aliases = {"crypto": "crypto", "us": "us", "pm": "pm", "ashare": "ashare", "a": "ashare"}
    return aliases.get(value, value)


def _unwrap_rows(rows: Any) -> list[dict[str, Any]]:
    if rows is None:
        return []
    if isinstance(rows, dict):
        rows = rows.get("data", [rows])
    if isinstance(rows, (str, int, float)):
        rows = [{"symbol": str(rows)}]
    if not isinstance(rows, list):
        return []
    result: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, str):
            result.append({"symbol": row})
            continue
        if not isinstance(row, dict):
            continue
        data = row.get("data")
        result.append(dict(data) if isinstance(data, dict) else dict(row))
    return result


def _candidate_symbol(candidate: dict[str, Any]) -> str:
    for key in ("ts_code", "symbol", "market_id", "pair", "id", "condition_id"):
        value = str(candidate.get(key) or "").strip()
        if value:
            return value
    return ""


def _candidate_price(candidate: dict[str, Any], market: str) -> float:
    for key in ("price", "latest_price", "close", "last_price", "market_price", "yes_price", "probability"):
        try:
            value = float(candidate.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0 and value == value:
            return value
    return 0.5 if market == "pm" else 1.0


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else default
    except (TypeError, ValueError):
        return default


def _first_present(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return default


def _safe_call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except TypeError:
        filtered = {key: value for key, value in kwargs.items() if key in {"as_of", "market"}}
        try:
            return fn(*args, **filtered)
        except TypeError:
            return fn(*args)


class LocalStyleSimulator:
    """No-broker simulator used when a market has no dedicated simulator."""

    def __init__(self, market: str) -> None:
        self.market = _normalize_market(market)

    def simulate(self, order: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
        reject_real_execution_payload(order, context=f"LocalStyleSimulator.{self.market}.order")
        reject_real_execution_payload(account, context=f"LocalStyleSimulator.{self.market}.account")
        quantity = float(order.get("quantity", order.get("qty", 0.0)) or 0.0)
        price = float(order.get("price", order.get("limit_price", 0.0)) or 0.0)
        if quantity <= 0 or price <= 0:
            return {
                "status": "rejected",
                "market": self.market,
                "reason": "missing_quantity_or_price",
                "capital_layer": "simulated",
                "account_type": "simulated",
                "real_execution": False,
            }
        from shared.execution.sim_engine import SimExecutionEngine, SimOrder

        side = str(order.get("side") or "buy").strip().lower()
        snapshot = dict(order.get("market_snapshot") or {})
        has_bar_liquidity = any(order.get(key) not in (None, "") for key in ("bar_volume", "volume", "vol"))
        default_size = None if has_bar_liquidity else quantity
        if side == "buy":
            snapshot.setdefault("ask_price", order.get("ask_price", price))
            ask_size = order.get("ask_size", default_size)
            if ask_size is not None:
                snapshot.setdefault("ask_size", ask_size)
            snapshot.setdefault("cash_available", account.get("cash_available", account.get("cash", account.get("initial_capital"))))
        else:
            snapshot.setdefault("bid_price", order.get("bid_price", price))
            bid_size = order.get("bid_size", default_size)
            if bid_size is not None:
                snapshot.setdefault("bid_size", bid_size)
            if order.get("sellable_qty") is not None:
                snapshot.setdefault("sellable_qty", order.get("sellable_qty"))
        snapshot.setdefault("last_price", order.get("last_price", price))
        available_qty = order.get("available_qty", default_size)
        if available_qty is not None:
            snapshot.setdefault("available_qty", available_qty)
        for key in (
            "bar_volume",
            "volume",
            "vol",
            "previous_close",
            "pre_close",
            "reference_price",
            "upper_limit",
            "lower_limit",
            "queue_position",
            "participation_cap",
            "liquidity_multiplier",
            "market_impact_multiplier",
            "counterparty_profile",
            "market_environment",
        ):
            if order.get(key) not in (None, ""):
                snapshot.setdefault(key, order.get(key))
        sim_order = SimOrder(
            symbol=str(order.get("symbol") or order.get("ts_code") or order.get("market_id") or ""),
            side=side,
            quantity=quantity,
            limit_price=price,
            order_type=str(order.get("order_type") or "market"),
            time_in_force=str(order.get("time_in_force") or "day"),
            market=self.market,
            order_id=str(order.get("order_id") or ""),
            metadata=dict(order),
        )
        record = SimExecutionEngine(self.market).submit_order(sim_order, snapshot)
        status = "pending" if record.state == "open" else record.state
        fee = float((record.fees or {}).get("total", 0.0) or 0.0)
        return {
            "status": status,
            "market": self.market,
            "symbol": order.get("symbol") or order.get("ts_code") or order.get("market_id"),
            "side": side,
            "quantity": quantity,
            "filled_qty": record.filled_qty,
            "avg_price": record.avg_fill_price,
            "notional": round(record.filled_qty * record.avg_fill_price, 6),
            "fee": fee,
            "reason": record.reason,
            "broker": "local_matching_engine",
            "engine_record": record.as_dict(),
            "capital_layer": "simulated",
            "account_type": "simulated",
            "real_execution": False,
        }


def default_simulator_factory(market: str) -> Any:
    market_key = _normalize_market(market)
    if market_key == "crypto":
        from Crypto.simulator import CryptoSimulator

        return CryptoSimulator()
    if market_key == "us":
        from US.simulator import USSimulator

        return USSimulator()
    if market_key == "pm":
        from PM.simulator import PMSimulator

        return PMSimulator()
    return LocalStyleSimulator(market_key)


class AutoPipeline:
    """Daily pipeline: universe -> research -> decision -> sim execution -> evolution."""

    def __init__(
        self,
        *,
        reader: Any | None = None,
        decision_engine: Any | None = None,
        fundamental_analyzer: Any | None = None,
        perspective_analyzer: Any | None = None,
        simulator_factory: Callable[[str], Any] | None = None,
        style_runner_cls: type[StyleRunner] = StyleRunner,
        evolution_fn: Callable[..., dict[str, Any]] = evaluate_and_adjust,
        review_root: Path | str | None = None,
        styles_dir_by_market: dict[str, Path | str] | None = None,
        max_candidates: int = 25,
        initial_capital: float = 100_000.0,
    ) -> None:
        self.reader = reader or TradingagentDataReader()
        self.decision_engine = decision_engine or DecisionEngine(market="crypto")
        self.fundamental_analyzer = fundamental_analyzer or FundamentalAnalyzer(reader=self.reader)
        self.perspective_analyzer = perspective_analyzer or MultiPerspectiveAnalyzer(reader=self.reader)
        self.simulator_factory = simulator_factory or default_simulator_factory
        self.style_runner_cls = style_runner_cls
        self.evolution_fn = evolution_fn
        self.review_root = Path(review_root) if review_root is not None else DEFAULT_REVIEW_ROOT
        self.styles_dir_by_market = {
            _normalize_market(key): Path(value)
            for key, value in (styles_dir_by_market or {}).items()
        }
        self.max_candidates = max(1, int(max_candidates))
        self.initial_capital = float(initial_capital)

    def run(
        self,
        *,
        trade_date: str | None = None,
        markets: tuple[str, ...] | list[str] | None = None,
        stage: str = "all",
    ) -> dict[str, Any]:
        trade_date = trade_date or _today_compact()
        market_keys = tuple(_normalize_market(market) for market in (markets or ACTIVE_MARKETS))
        if stage == "daily_review":
            market_results = [self._run_review_only(market, trade_date) for market in market_keys]
        else:
            market_results = [self.run_market(market, trade_date=trade_date) for market in market_keys]
        result = {
            "job": "auto_pipeline",
            "trade_date": trade_date,
            "generated_at": _now_iso(),
            "capital_layer": "simulated",
            "account_type": "simulated",
            "real_execution": False,
            "direct_execution": False,
            "stage": stage,
            "stage_schedule": self.stage_schedule(),
            "markets": market_results,
        }
        self._write_result(result, trade_date)
        return result

    def run_market(self, market: str, *, trade_date: str) -> dict[str, Any]:
        market_key = _normalize_market(market)
        candidates = self.load_universe(market_key, trade_date)
        research = self.run_research(market_key, candidates, trade_date)
        decisions = self.run_decisions(market_key, research, trade_date)
        portfolio = self.run_portfolio(market_key, decisions, trade_date)
        execution = self.run_execution(market_key, portfolio, decisions, trade_date)
        review = self.run_review(market_key, trade_date)
        return {
            "market": market_key,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "real_execution": False,
            "stages": {
                "pre_market_scan": {
                    "planned_time": "07:30",
                    "state": "ok",
                    "candidate_count": len(candidates),
                    "candidates": candidates,
                },
                "research_phase": {
                    "planned_time": "09:00",
                    "state": "ok",
                    "report_count": len(research),
                    "reports": research,
                },
                "decision_phase": {
                    "planned_time": "09:25",
                    "state": "ok",
                    "decision_count": len(decisions),
                    "portfolio": portfolio,
                    "decisions": decisions,
                },
                "execute_sim": {
                    "planned_time": "09:30",
                    **execution,
                },
                "daily_review": {
                    "planned_time": "16:00",
                    **review,
                },
            },
        }

    def load_universe(self, market: str, trade_date: str) -> list[dict[str, Any]]:
        raw: Any = []
        method = getattr(self.reader, "get_universe", None)
        if callable(method):
            for args, kwargs in [
                ((market, trade_date), {}),
                ((market,), {"date": trade_date}),
                ((), {"market": market, "date": trade_date}),
                ((), {"market": market, "as_of": trade_date}),
            ]:
                try:
                    raw = method(*args, **kwargs)
                    break
                except TypeError:
                    continue
        if not raw and market == "pm":
            pm_method = getattr(self.reader, "get_pm_markets", None)
            if callable(pm_method):
                raw = pm_method(limit=self.max_candidates)
        if not raw:
            assets_method = getattr(self.reader, "get_assets", None)
            if not callable(assets_method):
                shared = getattr(self.reader, "shared", None)
                assets_method = getattr(shared, "get_assets", None)
            if callable(assets_method):
                for market_name in (market, market.capitalize(), market.upper(), "Ashare" if market == "ashare" else market):
                    raw = assets_method(market_name)
                    if raw:
                        break

        candidates: list[dict[str, Any]] = []
        unsafe_count = 0
        for row in _unwrap_rows(raw):
            item = dict(row)
            symbol = _candidate_symbol(item)
            if not symbol:
                continue
            try:
                reject_real_execution_payload(item, context=f"AutoPipeline.{market}.candidate")
            except RuntimeError:
                unsafe_count += 1
                continue
            item.setdefault("symbol", symbol)
            item.setdefault("ts_code", symbol)
            item.setdefault("market", market)
            item.setdefault("price", _candidate_price(item, market))
            item["capital_layer"] = "simulated"
            item["account_type"] = "simulated"
            item["real_execution"] = False
            item["direct_execution"] = False
            candidates.append(item)
            if len(candidates) >= self.max_candidates:
                break
        if unsafe_count:
            candidates.append(
                {
                    "symbol": "__unsafe_candidates_skipped__",
                    "market": market,
                    "skipped": unsafe_count,
                    "capital_layer": "simulated",
                    "account_type": "simulated",
                    "real_execution": False,
                    "direct_execution": False,
                }
            )
        return [item for item in candidates if item.get("symbol") != "__unsafe_candidates_skipped__"]

    def run_research(
        self,
        market: str,
        candidates: list[dict[str, Any]],
        trade_date: str,
    ) -> list[dict[str, Any]]:
        reports: list[dict[str, Any]] = []
        for candidate in candidates:
            symbol = _candidate_symbol(candidate)
            if not symbol:
                continue
            try:
                fundamental = _safe_call(self.fundamental_analyzer.analyze, symbol, as_of=trade_date)
            except Exception as exc:
                fundamental = {
                    "ts_code": symbol,
                    "as_of": trade_date,
                    "scores": {"composite": 50.0},
                    "red_flags": [{"flag": "fundamental_error", "severity": "medium", "detail": str(exc)}],
                    "capital_layer": "research_only",
                }
            try:
                perspective = self.perspective_analyzer.analyze(
                    symbol,
                    as_of=trade_date,
                    market=market,
                    fundamental_report=fundamental,
                )
            except TypeError:
                perspective = _safe_call(self.perspective_analyzer.analyze, symbol, as_of=trade_date, market=market)
            except Exception as exc:
                perspective = {
                    "ts_code": symbol,
                    "as_of": trade_date,
                    "consensus": {"score": 50.0, "direction": "neutral", "conviction_level": "low"},
                    "disagreement_areas": [{"area": "research_error", "detail": str(exc)}],
                    "capital_layer": "research_only",
                }
            reports.append(
                {
                    "market": market,
                    "symbol": symbol,
                    "candidate": candidate,
                    "fundamental": fundamental,
                    "multi_perspective": perspective,
                    "capital_layer": "simulated",
                    "account_type": "simulated",
                    "real_execution": False,
                }
            )
        return reports

    def run_decisions(
        self,
        market: str,
        research: list[dict[str, Any]],
        trade_date: str,
    ) -> list[dict[str, Any]]:
        decisions: list[dict[str, Any]] = []
        for report in research:
            decision = self._decide(report, market, trade_date)
            decision["capital_layer"] = "simulated"
            decision["account_type"] = "simulated"
            decision["real_execution"] = False
            decision["direct_execution"] = False
            reject_real_execution_payload(decision, context=f"AutoPipeline.{market}.decision")
            decisions.append(decision)
        return decisions

    def run_portfolio(
        self,
        market: str,
        decisions: list[dict[str, Any]],
        trade_date: str,
    ) -> dict[str, Any]:
        try:
            portfolio = self.decision_engine.portfolio_rebalance(
                decisions,
                market=market,
                as_of=trade_date,
                capital=self.initial_capital,
            )
        except TypeError:
            portfolio = self._fallback_portfolio_rebalance(market, decisions, trade_date)
        if not isinstance(portfolio.get("positions"), list):
            portfolio = self._fallback_portfolio_rebalance(market, decisions, trade_date)
        reject_real_execution_payload(portfolio, context=f"AutoPipeline.{market}.portfolio")
        return portfolio

    def _decide(self, report: dict[str, Any], market: str, trade_date: str) -> dict[str, Any]:
        try:
            result = self.decision_engine.decide(
                candidate=report["candidate"],
                fundamental=report["fundamental"],
                research=report["multi_perspective"],
                market=market,
                as_of=trade_date,
            )
        except TypeError:
            result = self.decision_engine.decide(
                self._legacy_fundamental(report),
                self._legacy_perspectives(report),
                self._legacy_risk(report),
                {"capital": self.initial_capital, "market": market, "as_of": trade_date},
            )
        return self._decision_dict(result, report, market)

    def _decision_dict(self, result: Any, report: dict[str, Any], market: str) -> dict[str, Any]:
        data = asdict(result) if is_dataclass(result) else dict(result or {})
        candidate = dict(report.get("candidate") or {})
        symbol = str(data.get("symbol") or candidate.get("symbol") or candidate.get("ts_code") or "").strip()
        raw_action = str(data.get("action") or data.get("decision") or "watch").lower()
        action = "buy" if raw_action in {"buy", "long"} else "sell" if raw_action in {"sell", "short", "reduce"} else "watch"
        confidence = _safe_float(data.get("confidence", data.get("belief_score")), 0.5)
        conviction = self._conviction_value(data.get("conviction"), confidence)
        price = _safe_float(candidate.get("price", candidate.get("latest_price", candidate.get("close"))), _candidate_price(candidate, market))
        return {
            **data,
            "market": market,
            "symbol": symbol,
            "ts_code": symbol,
            "action": action,
            "side": "buy" if action == "buy" else "sell" if action == "sell" else "watch",
            "price": price,
            "belief_score": confidence,
            "conviction": conviction,
            "position_pct": _safe_float(data.get("position_pct"), 0.0),
            "capital_layer": "simulated",
            "account_type": "simulated",
            "real_execution": False,
            "direct_execution": False,
        }

    @staticmethod
    def _conviction_value(value: Any, confidence: float) -> float:
        if isinstance(value, str):
            return {"high": 0.8, "medium": 0.65, "low": 0.5}.get(value.lower(), confidence)
        parsed = _safe_float(value, 0.0)
        return parsed if parsed > 0 else confidence

    @staticmethod
    def _legacy_fundamental(report: dict[str, Any]) -> dict[str, Any]:
        fundamental = dict(report.get("fundamental") or {})
        candidate = dict(report.get("candidate") or {})
        scores = fundamental.get("scores") if isinstance(fundamental.get("scores"), dict) else {}
        fundamental.setdefault("symbol", candidate.get("symbol") or candidate.get("ts_code") or report.get("symbol"))
        fundamental.setdefault("composite_score", scores.get("composite", 50.0))
        return fundamental

    @staticmethod
    def _legacy_perspectives(report: dict[str, Any]) -> dict[str, Any]:
        perspective = dict(report.get("multi_perspective") or {})
        if any(key in perspective for key in ("bull", "bear", "macro", "technical")):
            return perspective
        consensus = perspective.get("consensus") if isinstance(perspective.get("consensus"), dict) else {}
        score = _safe_float(consensus.get("score"), 50.0)
        return {
            "bull": {"score": score},
            "bear": {"score": max(0.0, 100.0 - score)},
            "macro": {"score": score},
            "technical": {"score": score},
        }

    @staticmethod
    def _legacy_risk(report: dict[str, Any]) -> dict[str, Any]:
        fundamental = dict(report.get("fundamental") or {})
        red_flags = fundamental.get("red_flags") if isinstance(fundamental.get("red_flags"), list) else []
        return {"risk_score": min(90.0, 20.0 + 10.0 * len(red_flags))}

    def _fallback_portfolio_rebalance(self, market: str, decisions: list[dict[str, Any]], trade_date: str) -> dict[str, Any]:
        buys = [decision for decision in decisions if str(decision.get("action") or "").lower() == "buy"]
        buys.sort(key=lambda row: _safe_float(row.get("belief_score"), 0.0), reverse=True)
        positions: list[dict[str, Any]] = []
        for decision in buys[:10]:
            position_pct = _safe_float(decision.get("position_pct"), 0.0)
            if position_pct <= 0:
                position_pct = min(0.10, max(0.01, _safe_float(decision.get("belief_score"), 0.5) * 0.10))
            positions.append(
                {
                    "market": market,
                    "symbol": decision.get("symbol"),
                    "ts_code": decision.get("ts_code") or decision.get("symbol"),
                    "side": "buy",
                    "price": decision.get("price"),
                    "belief_score": decision.get("belief_score", 0.5),
                    "conviction": decision.get("conviction", 0.5),
                    "position_pct": round(position_pct, 6),
                    "trade_date": trade_date,
                    "capital_layer": "simulated",
                    "account_type": "simulated",
                    "real_execution": False,
                    "direct_execution": False,
                }
            )
        return {
            "market": market,
            "capital": self.initial_capital,
            "positions": positions,
            "position_count": len(positions),
            "allocated_pct": round(sum(_safe_float(row.get("position_pct"), 0.0) for row in positions), 6),
            "trade_date": trade_date,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "real_execution": False,
            "direct_execution": False,
        }

    def run_execution(
        self,
        market: str,
        portfolio: dict[str, Any],
        decisions: list[dict[str, Any]],
        trade_date: str,
    ) -> dict[str, Any]:
        signals = self._signals_from_positions(market, portfolio, decisions, trade_date)
        simulator = self.simulator_factory(market)
        styles_dir = self._styles_dir(market)
        weights_before = load_style_weights(market, review_root=self.review_root)
        result = self.style_runner_cls(
            market,
            simulator,
            styles_dir=styles_dir,
            review_root=self.review_root,
        ).run(
            signals,
            date=trade_date,
            account={
                "account_id": f"{market}_auto_pipeline_sim",
                "initial_capital": self.initial_capital,
                "capital_layer": "simulated",
                "account_type": "simulated",
                "real_execution": False,
                "direct_execution": False,
            },
        )
        return {
            "state": "ok",
            "capital_layer": "simulated",
            "account_type": "simulated",
            "real_execution": False,
            "signals": signals,
            "weights_loaded": weights_before,
            "style_result": result,
            "performance_records": len(result.get("style_comparison", []) or []),
        }

    def run_review(self, market: str, trade_date: str) -> dict[str, Any]:
        try:
            result = self.evolution_fn(market, review_root=self.review_root)
        except TypeError:
            result = self.evolution_fn(market)
        result = dict(result or {})
        result.update(
            {
                "state": result.get("state", "observed"),
                "trade_date": trade_date,
                "capital_layer": "simulated",
                "account_type": "simulated",
                "real_execution": False,
            }
        )
        return result

    def _run_review_only(self, market: str, trade_date: str) -> dict[str, Any]:
        return {
            "market": market,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "real_execution": False,
            "stages": {
                "daily_review": {
                    "planned_time": "16:00",
                    **self.run_review(market, trade_date),
                }
            },
        }

    def _signals_from_positions(
        self,
        market: str,
        portfolio: dict[str, Any],
        decisions: list[dict[str, Any]],
        trade_date: str,
    ) -> list[dict[str, Any]]:
        by_symbol = {str(decision.get("ts_code") or decision.get("symbol")): decision for decision in decisions}
        signals: list[dict[str, Any]] = []
        for position in portfolio.get("positions", []) or []:
            symbol = str(position.get("ts_code") or position.get("symbol") or "")
            decision = by_symbol.get(symbol, {})
            signal = {
                "market": market,
                "symbol": symbol,
                "ts_code": symbol,
                "side": position.get("side", "buy"),
                "price": position.get("price") or decision.get("price") or _candidate_price(decision, market),
                "belief_score": position.get("belief_score", decision.get("belief_score", 0.5)),
                "conviction": position.get("conviction", decision.get("conviction", 0.5)),
                "strategy_name": "auto_pipeline",
                "trade_date": trade_date,
                "capital_layer": "simulated",
                "account_type": "simulated",
                "real_execution": False,
                "direct_execution": False,
            }
            snapshot = self._market_snapshot(market, symbol, trade_date)
            if snapshot:
                signal["market_snapshot"] = snapshot
                for key in (
                    "last_price",
                    "close",
                    "bar_volume",
                    "volume",
                    "previous_close",
                    "pre_close",
                    "reference_price",
                ):
                    if snapshot.get(key) not in (None, ""):
                        signal.setdefault(key, snapshot[key])
            reject_real_execution_payload(signal, context=f"AutoPipeline.{market}.signal")
            signals.append(signal)
        return signals

    def _market_snapshot(self, market: str, symbol: str, trade_date: str) -> dict[str, Any]:
        snapshot: dict[str, Any] = {}
        market_names = self._reader_market_names(market)
        intraday = self._latest_intraday_bar(market_names, symbol, trade_date)
        if intraday:
            close = _safe_float(_first_present(intraday, "close", "price", "last_price"), 0.0)
            volume = _safe_float(_first_present(intraday, "bar_volume", "volume", "vol"), 0.0)
            if close > 0:
                snapshot["last_price"] = close
                snapshot["close"] = close
            if volume > 0:
                snapshot["bar_volume"] = volume
                snapshot["volume"] = volume
            for key in ("bar_time", "trade_time", "ask_price", "ask_size", "bid_price", "bid_size"):
                if intraday.get(key) not in (None, ""):
                    snapshot[key] = intraday[key]

        daily_rows = self._recent_daily_bars(market_names, symbol, trade_date)
        if daily_rows:
            latest = daily_rows[-1]
            latest_close = _safe_float(_first_present(latest, "close", "price", "last_price"), 0.0)
            if latest_close > 0:
                snapshot.setdefault("last_price", latest_close)
                snapshot.setdefault("close", latest_close)
            pre_close = _safe_float(_first_present(latest, "pre_close", "previous_close", "prev_close"), 0.0)
            if pre_close <= 0 and len(daily_rows) >= 2:
                pre_close = _safe_float(_first_present(daily_rows[-2], "close", "price", "last_price"), 0.0)
            if pre_close > 0:
                snapshot["previous_close"] = pre_close
                snapshot["pre_close"] = pre_close
                snapshot["reference_price"] = pre_close
            daily_volume = _safe_float(_first_present(latest, "volume", "vol"), 0.0)
            if daily_volume > 0:
                snapshot.setdefault("volume", daily_volume)
        return snapshot

    @staticmethod
    def _reader_market_names(market: str) -> tuple[str, ...]:
        market_key = _normalize_market(market)
        names = [market_key, market_key.capitalize(), market_key.upper()]
        if market_key == "ashare":
            names.insert(0, "Ashare")
        return tuple(dict.fromkeys(names))

    def _latest_intraday_bar(self, market_names: tuple[str, ...], symbol: str, trade_date: str) -> dict[str, Any]:
        get_bars = getattr(self.reader, "get_bars_intraday", None)
        if not callable(get_bars):
            return {}
        for market_name in market_names:
            for interval in ("5min", "5m"):
                try:
                    rows = get_bars(market_name, symbol, interval, trade_date, trade_date)
                except Exception:
                    continue
                valid = [dict(row) for row in _unwrap_rows(rows) if isinstance(row, dict)]
                if valid:
                    return valid[-1]
        return {}

    def _recent_daily_bars(self, market_names: tuple[str, ...], symbol: str, trade_date: str) -> list[dict[str, Any]]:
        get_bars = getattr(self.reader, "get_bars_daily", None)
        if not callable(get_bars):
            return []
        start_date = self._lookback_start(trade_date)
        for market_name in market_names:
            try:
                rows = get_bars(market_name, symbol, start_date, trade_date)
            except Exception:
                continue
            valid = [dict(row) for row in _unwrap_rows(rows) if isinstance(row, dict)]
            if valid:
                return valid
        return []

    @staticmethod
    def _lookback_start(trade_date: str) -> str:
        raw = str(trade_date or "").strip()
        for fmt in ("%Y%m%d", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(raw, fmt)
                return (parsed - timedelta(days=14)).strftime(fmt)
            except ValueError:
                continue
        return ""

    def _styles_dir(self, market: str) -> Path:
        return self.styles_dir_by_market.get(market) or styles_dir_for_market(market)

    @staticmethod
    def stage_schedule() -> list[dict[str, str]]:
        return [
            {"stage": stage, "planned_time": planned_time, "description": description}
            for stage, planned_time, description in PIPELINE_STAGES
        ]

    def _write_result(self, result: dict[str, Any], trade_date: str) -> None:
        output_dir = self.review_root / "auto_pipeline"
        output_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        (output_dir / f"{trade_date}.json").write_text(payload, encoding="utf-8")
        (output_dir / "latest.json").write_text(payload, encoding="utf-8")


def run_auto_pipeline(
    *,
    trade_date: str | None = None,
    markets: tuple[str, ...] | list[str] | None = None,
    stage: str = "all",
    review_root: Path | str | None = None,
    max_candidates: int = 25,
) -> dict[str, Any]:
    return AutoPipeline(review_root=review_root, max_candidates=max_candidates).run(
        trade_date=trade_date,
        markets=markets,
        stage=stage,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run TradingAgent simulated auto pipeline")
    parser.add_argument("--date", dest="trade_date", default=None)
    parser.add_argument("--market", action="append", dest="markets", default=None)
    parser.add_argument("--stage", default="all", choices=("all", "daily_review"))
    parser.add_argument("--review-root", default=None)
    parser.add_argument("--max-candidates", type=int, default=25)
    args = parser.parse_args(argv)
    result = run_auto_pipeline(
        trade_date=args.trade_date,
        markets=tuple(args.markets) if args.markets else None,
        stage=args.stage,
        review_root=args.review_root,
        max_candidates=args.max_candidates,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ACTIVE_MARKETS",
    "AutoPipeline",
    "LocalStyleSimulator",
    "PIPELINE_STAGES",
    "run_auto_pipeline",
]
