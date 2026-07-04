#!/usr/bin/env python3
"""Automated simulated daily pipeline across markets."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
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
        if side == "buy":
            snapshot.setdefault("ask_price", order.get("ask_price", price))
            snapshot.setdefault("ask_size", order.get("ask_size", quantity))
            snapshot.setdefault("cash_available", account.get("cash_available", account.get("cash", account.get("initial_capital"))))
        else:
            snapshot.setdefault("bid_price", order.get("bid_price", price))
            snapshot.setdefault("bid_size", order.get("bid_size", quantity))
            if order.get("sellable_qty") is not None:
                snapshot.setdefault("sellable_qty", order.get("sellable_qty"))
        snapshot.setdefault("last_price", order.get("last_price", price))
        snapshot.setdefault("available_qty", order.get("available_qty", quantity))
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
            decision = self.decision_engine.decide(
                candidate=report["candidate"],
                fundamental=report["fundamental"],
                research=report["multi_perspective"],
                market=market,
                as_of=trade_date,
            )
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
        portfolio = self.decision_engine.portfolio_rebalance(
            decisions,
            market=market,
            as_of=trade_date,
            capital=self.initial_capital,
        )
        reject_real_execution_payload(portfolio, context=f"AutoPipeline.{market}.portfolio")
        return portfolio

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
            reject_real_execution_payload(signal, context=f"AutoPipeline.{market}.signal")
            signals.append(signal)
        return signals

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
