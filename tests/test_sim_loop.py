from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from shared.accounting import trade_audit_trail
from shared.execution import shadow_broker, sim_executor_registry
from shared.execution.signal_state_machine import read_json
from shared.markets.base import MarketAdapter
from shared.orchestrator import OrchestratorDeps, _apply_position_budgets, _build_signal_card, run_sim_loop


def test_ashare_position_budget_cannot_expand_risk_approved_weight():
    portfolio = {"positions": [{"ts_code": "600000.SH", "price": 10.0, "shares": 700, "weight": 0.14}]}
    _apply_position_budgets(
        market="ashare",
        portfolio=portfolio,
        order_meta={"600000.SH": {"price": 10.0, "weight": 0.14}},
        capital_plan={"enabled": True, "position_budget_by_symbol": {"600000.SH": 15_000.0}},
        capital=50_000.0,
    )
    position = portfolio["positions"][0]
    assert position["weight"] <= 0.14
    assert position["amount"] <= 7_000.0
    assert position["requested_budget"] == 15_000.0
    assert position["budget_cap_reason"] == "risk_adjusted_weight_cap"


def test_ashare_position_budget_rounding_cannot_cross_fifteen_percent():
    portfolio = {"positions": [{"ts_code": "600000.SH", "price": 7.31, "shares": 100, "weight": 0.01}]}
    _apply_position_budgets(
        market="ashare",
        portfolio=portfolio,
        order_meta={"600000.SH": {"price": 7.31, "weight": 0.15}},
        capital_plan={"enabled": True, "position_budget_by_symbol": {"600000.SH": 17_500.0}},
        capital=50_000.0,
    )
    assert portfolio["positions"][0]["amount"] <= 7_500.0
    assert portfolio["positions"][0]["weight"] <= 0.15


class StubSimAdapter(MarketAdapter):
    def get_universe(self, date: str) -> list[str]:
        return ["AAA"]

    def get_market(self) -> str:
        return "unit"

    def map_symbol_to_reader(self, symbol: str) -> tuple[str, str]:
        return "unit", symbol

    def get_strategy_config(self) -> dict[str, object]:
        return {
            "portfolio_method": "conviction_weighted",
            "regime": "growth",
            "max_candidates": 1,
            "default_price": 10.0,
            "default_volatility": 0.20,
        }

    def get_shadow_account(self) -> str:
        return "unit_shadow"

    def get_sim_account(self) -> dict[str, object]:
        return {
            "account": "unit_sim",
            "sim_capital": 50000.0,
            "positions": [
                {"ts_code": "HELD", "weight": 0.03, "sector": "unit"},
            ],
        }


class StubReader:
    def get_bars_daily(self, market: str, symbol: str, start: object = None, end: object = None) -> list[dict[str, float]]:
        return [{"close": 9.8}, {"close": 10.0}, {"close": 10.2}]


class SignalCardTPlusOneTest(unittest.TestCase):
    def test_ashare_sim_buy_signal_card_uses_next_trading_day_sellable_date(self) -> None:
        card = _build_signal_card(
            market="ashare",
            symbol="600030.SH",
            account="ashare_sim",
            date="20260709",
            order={
                "side": "buy",
                "quantity": 100,
                "price": 28.0,
                "candidate_pool_layer": "candidate",
                "execution_source": "ashare_candidate_layer",
            },
            risk={"approved": True},
            trade={"trade_id": "T1"},
            audit_id="AUDIT-T1",
            order_id="SIM-ashare-600030.SH-20260709-test",
            order_id_prefix="SIM-",
            capital_layer="simulated",
            account_type="simulated",
            direct_execution=True,
        )

        self.assertEqual(card["t_plus_1"]["sellable_from"], "2026-07-10")
        self.assertEqual(card["t_plus_1"]["sellable_date"], "2026-07-10")


class MultiCandidateSimAdapter(StubSimAdapter):
    def __init__(
        self,
        symbols: list[str],
        *,
        max_candidates: int = 3,
        score_universe_limit: int | None = None,
        max_portfolio_positions: int = 3,
        positions: list[dict[str, object]] | None = None,
        cash_available: float | None = None,
        strategy_positions: list[dict[str, object]] | None = None,
        strategy_cash_available: float | None = None,
        sample_adjustment: dict[str, object] | None = None,
    ) -> None:
        self.symbols = symbols
        self.max_candidates = max_candidates
        self.score_universe_limit = score_universe_limit or max_candidates
        self.max_portfolio_positions = max_portfolio_positions
        self.positions = positions or []
        self.cash_available = cash_available
        self.strategy_positions = strategy_positions
        self.strategy_cash_available = strategy_cash_available
        self.sample_adjustment = sample_adjustment

    def get_universe(self, date: str) -> list[str]:
        return list(self.symbols)

    def get_market(self) -> str:
        return "ashare"

    def get_strategy_config(self) -> dict[str, object]:
        return {
            "portfolio_method": "conviction_weighted",
            "regime": "ashare_default",
            "max_candidates": self.max_candidates,
            "score_universe_limit": self.score_universe_limit,
            "max_portfolio_positions": self.max_portfolio_positions,
            "default_price": 0.0,
            "default_volatility": 0.20,
        }

    def get_sim_account(self) -> dict[str, object]:
        payload: dict[str, object] = {"account": "ashare_sim", "sim_capital": 200000.0, "positions": list(self.positions)}
        if self.cash_available is not None:
            payload["cash_available"] = self.cash_available
        if self.strategy_positions is not None:
            payload["strategy_positions"] = list(self.strategy_positions)
        if self.strategy_cash_available is not None:
            payload["strategy_cash_available"] = self.strategy_cash_available
        if self.sample_adjustment is not None:
            payload["capital_plan_sample_adjustment"] = dict(self.sample_adjustment)
        return payload


def _patch_shadow_paths(testcase: unittest.TestCase, tmp_path: Path) -> None:
    shadow_dir = tmp_path / "shadow"
    for name, value in (
        ("SHADOW_DIR", shadow_dir),
        ("SHADOW_TRADES", shadow_dir / "shadow_trades.jsonl"),
        ("SHADOW_POSITIONS", shadow_dir / "shadow_positions.json"),
        ("SHADOW_PNL", shadow_dir / "shadow_pnl.json"),
        ("SHADOW_LOCK", shadow_dir / ".shadow.lock"),
    ):
        patcher = patch.object(shadow_broker, name, value)
        patcher.start()
        testcase.addCleanup(patcher.stop)


def _patch_audit_paths(testcase: unittest.TestCase, tmp_path: Path) -> None:
    ledger_dir = tmp_path / "logs"
    for name, value in (
        ("LEDGER_DIR", ledger_dir),
        ("AUDIT_TRAIL", ledger_dir / "trade_audit_trail.jsonl"),
    ):
        patcher = patch.object(trade_audit_trail, name, value)
        patcher.start()
        testcase.addCleanup(patcher.stop)


class SimLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp_path = Path(self.tmpdir.name)
        _patch_shadow_paths(self, self.tmp_path)
        _patch_audit_paths(self, self.tmp_path)
        self._sim_executor_snapshot = dict(sim_executor_registry._SIM_EXECUTORS)
        self.addCleanup(self._restore_sim_executors)
        self.calls: list[str] = []
        self.risk_portfolios: list[dict[str, object]] = []
        self.review_requests: list[dict[str, object]] = []
        self.executed_orders: list[dict[str, object]] = []

    def _restore_sim_executors(self) -> None:
        sim_executor_registry._SIM_EXECUTORS.clear()
        sim_executor_registry._SIM_EXECUTORS.update(self._sim_executor_snapshot)

    def _deps(self) -> OrchestratorDeps:
        def score_stock(market: str, symbol: str, data_reader: object = None, date: str | None = None) -> dict[str, object]:
            self.calls.append("screening")
            return {
                "combined": 0.72,
                "sector": "unit",
                "turnover_wan": 10000,
                "capital_layer": "simulated",
            }

        def build_pool(
            date: str,
            universe: list[str],
            market: str | None = None,
            reader: object | None = None,
        ) -> dict[str, list[str]]:
            self.calls.append("candidate_pool")
            return {"candidate": list(universe), "watch": [], "holdings": [], "universe": list(universe)}

        def debate(symbol: str, scores: dict[str, object]) -> dict[str, object]:
            self.calls.append("adversarial")
            self.assertEqual(scores["capital_layer"], "simulated")
            return {"ts_code": symbol, "belief_score": 0.70, "bull_case": "ok", "bear_case": "risk"}

        def risk_check(order: dict[str, object], portfolio: dict[str, object]) -> dict[str, object]:
            self.calls.append("risk")
            self.risk_portfolios.append(portfolio)
            self.assertEqual(order["capital_layer"], "simulated")
            self.assertEqual(order["account_type"], "simulated")
            self.assertEqual(portfolio["capital_layer"], "simulated")
            self.assertEqual(portfolio["account_type"], "simulated")
            self.assertEqual(portfolio["positions"][0]["ts_code"], "HELD")
            return {"approved": True, "adjusted_weight": order["weight"], "adjustments": ["ok"], "reasons": []}

        def construct(orders: list[dict[str, object]], capital: float, method: str, regime: str) -> dict[str, object]:
            self.calls.append("portfolio")
            self.assertEqual(capital, 50000.0)
            return {
                "method": method,
                "capital": capital,
                "positions": [
                    {
                        "ts_code": order["ts_code"],
                        "weight": order["weight"],
                        "shares": 10,
                        "amount": 100.0,
                        "sector": "unit",
                        "price": 10.0,
                    }
                    for order in orders
                ],
                "total_weight": sum(float(order["weight"]) for order in orders),
                "cash_weight": 0.95,
            }

        def size_position(belief_score: float, volatility: float, regime: str) -> float:
            self.calls.append("position_sizer")
            return 0.05

        def record_shadow(order: dict[str, object], account: str) -> dict[str, object]:
            raise AssertionError("run_sim_loop must not call record_shadow")

        def review(date: str, session: str = "close", capital_layer: str = "shadow") -> dict[str, object]:
            self.calls.append("review")
            self.review_requests.append({"date": date, "session": session, "capital_layer": capital_layer})
            return {"session": session, "trade_date": date, "capital_layer": capital_layer}

        def execute_sim_order(order: dict[str, object], account: object = None) -> dict[str, object]:
            self.calls.append("sim_broker")
            self.executed_orders.append(order)
            self.assertTrue(str(order["order_id"]).startswith("SIM-"))
            self.assertEqual(order["capital_layer"], "simulated")
            self.assertEqual(order["account_type"], "simulated")
            return {
                "order_id": order["order_id"],
                "status": "filled",
                "filled_price": 10.05,
                "filled_quantity": order["quantity"],
                "fill_time": "2026-06-30T10:00:00",
            }

        return OrchestratorDeps(
            score_stock=score_stock,
            build_pool=build_pool,
            debate=debate,
            risk_check=risk_check,
            construct=construct,
            size_position=size_position,
            record_shadow=record_shadow,
            run_review=review,
            record_audit_event=trade_audit_trail.record_event,
            execute_sim_order=execute_sim_order,
        )

    def _multi_candidate_deps(self) -> OrchestratorDeps:
        deps = self._deps()

        def risk_check(order: dict[str, object], portfolio: dict[str, object]) -> dict[str, object]:
            self.calls.append("risk")
            return {"approved": True, "adjusted_weight": order["weight"], "adjustments": ["ok"], "reasons": []}

        def construct(orders: list[dict[str, object]], capital: float, method: str, regime: str) -> dict[str, object]:
            self.calls.append("portfolio")
            return {
                "method": method,
                "capital": capital,
                "positions": [
                    {
                        "ts_code": order["ts_code"],
                        "weight": order["weight"],
                        "shares": 100,
                        "amount": 1000.0,
                        "sector": "unit",
                        "price": 10.0,
                    }
                    for order in orders
                ],
                "total_weight": sum(float(order["weight"]) for order in orders),
                "cash_weight": 0.95,
            }

        deps.risk_check = risk_check
        deps.construct = construct
        return deps

    def _high_score_deps(self) -> OrchestratorDeps:
        deps = self._multi_candidate_deps()

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            return [
                (
                    symbol,
                    {
                        "combined": 0.82,
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                )
                for symbol in universe
            ]

        deps.score_universe = score_universe
        return deps

    @contextmanager
    def _approved_expansion_evidence(self, trade_date: str):
        decision = {
            "capital_epoch": 2,
            "evidence_trade_date": trade_date,
            "state": "expansion_candidate",
            "recommended_action": "expand_risk_candidate",
            "reasons": ["positive_realized_pnl_after_all_gates"],
            "policy": {
                "strategy_sample_count": 20,
                "min_strategy_samples": 5,
                "sample_collection_min_score": 0.55,
            },
        }
        with (
            patch("Ashare.evolution_controller.load_latest_decision", return_value=decision),
            patch("shared.execution.sim_account_epoch.read_epoch_state", return_value={"current_epoch_id": 2}),
        ):
            yield

    def test_run_sim_loop_fills_signal_audit_and_review_as_simulated(self) -> None:
        result = run_sim_loop(
            StubSimAdapter(),
            "20260630",
            StubReader(),
            deps=self._deps(),
            signals_dir=self.tmp_path / "signals",
        )

        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["capital_layer"], "simulated")
        self.assertEqual(result["account_type"], "simulated")
        self.assertEqual(result["account"], "unit_sim")
        self.assertEqual(result["filled_count"], 1)
        self.assertEqual(result["failed_count"], 0)
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(result["portfolio"]["existing_positions"][0]["ts_code"], "HELD")
        for expected in ("screening", "candidate_pool", "adversarial", "risk", "position_sizer", "portfolio", "sim_broker", "review"):
            self.assertIn(expected, self.calls)

        filled_files = list((self.tmp_path / "signals" / "filled").glob("SIM-*.json"))
        self.assertEqual(len(filled_files), 1)
        self.assertFalse(list((self.tmp_path / "signals" / "pending").glob("SIM-*.json")))
        filled = read_json(filled_files[0])
        self.assertEqual(filled["capital_layer"], "simulated")
        self.assertEqual(filled["account_type"], "simulated")
        self.assertEqual(filled["status"], "filled")
        self.assertEqual(filled["filled_price"], 10.05)
        self.assertEqual(filled["filled_quantity"], 10)

        audit_rows = [
            json.loads(line)
            for line in trade_audit_trail.AUDIT_TRAIL.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual({row["stage"] for row in audit_rows}, {"signal", "decision", "risk", "execution", "result"})
        self.assertTrue(all(row.get("metadata", {}).get("capital_layer") == "simulated" for row in audit_rows))
        self.assertEqual(result["review"]["capital_layer"], "simulated")
        self.assertEqual(self.review_requests, [{"date": "20260630", "session": "close", "capital_layer": "simulated"}])
        self.assertFalse(shadow_broker.SHADOW_TRADES.exists())


    def test_write_execution_signal_does_not_duplicate_successful_mini_webhook(self) -> None:
        from shared.orchestrator import _write_execution_signal

        signals_dir = self.tmp_path / "signals"
        card = {
            "order_id": "SIM-ASHARE-WEBHOOK-NODUP",
            "ts_code": "600000.SH",
            "market": "ashare",
            "direction": "buy",
            "quantity": 100,
            "price": 10.0,
            "capital_layer": "simulated",
            "account_type": "simulated",
        }
        receipt = {
            "status": "pending",
            "raw_response": {
                "mode": "mini_webhook_sent",
                "webhook": {"success": True, "http_status": 200},
                "signal_card": card,
            },
        }

        result = _write_execution_signal(card, receipt, signals_dir=signals_dir)

        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["pending_signal"]["source"], "mini_webhook")
        self.assertEqual(list((signals_dir / "pending").glob("*.json")), [])

    def test_write_execution_signal_persists_failure_details(self) -> None:
        from shared.execution.signal_state_machine import read_json
        from shared.orchestrator import _write_execution_signal

        signals_dir = self.tmp_path / "signals_failure_details"
        card = {
            "order_id": "SIM-ASHARE-FAIL-DETAILS",
            "ts_code": "600000.SH",
            "market": "ashare",
            "direction": "buy",
            "quantity": 100,
            "price": 10.0,
            "capital_layer": "simulated",
            "account_type": "simulated",
        }
        receipt = {
            "order_id": "SIM-ASHARE-FAIL-DETAILS",
            "status": "rejected",
            "message": "Server-local A-share simulated fill via matching engine: rejected: insufficient_cash",
            "filled_qty": 0,
            "avg_price": 0.0,
            "raw_response": {
                "mode": "server_local_sim_engine",
                "engine_record": {"state": "rejected", "reason": "insufficient_cash"},
            },
        }

        result = _write_execution_signal(card, receipt, signals_dir=signals_dir)

        self.assertEqual(result["status"], "failed")
        failed_card = read_json(signals_dir / "failed" / "SIM-ASHARE-FAIL-DETAILS.json")
        self.assertIn("insufficient_cash", failed_card["failure_reason"])
        self.assertEqual(failed_card["failure_details"]["engine_reason"], "insufficient_cash")
        self.assertEqual(failed_card["failure_details"]["receipt_status"], "rejected")

    def test_run_sim_loop_skips_existing_same_day_sim_signal(self) -> None:
        signals_dir = self.tmp_path / "signals"
        filled_dir = signals_dir / "filled"
        filled_dir.mkdir(parents=True, exist_ok=True)
        existing = {
            "order_id": "SIM-unit-AAA-20260630-existing",
            "ts_code": "AAA",
            "market": "unit",
            "direction": "buy",
            "quantity": 10,
            "price": 10.0,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "valid_until": "2026-06-30",
            "status": "filled",
        }
        (filled_dir / "SIM-unit-AAA-20260630-existing.json").write_text(json.dumps(existing), encoding="utf-8")

        result = run_sim_loop(
            StubSimAdapter(),
            "20260630",
            StubReader(),
            deps=self._deps(),
            signals_dir=signals_dir,
        )

        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["filled_count"], 0)
        self.assertEqual(result["failed_count"], 0)
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(self.executed_orders, [])
        self.assertEqual(result["records"][0]["signal_result"]["status"], "duplicate")
        self.assertIn("signals.sim_dedup", result["stage_calls"])
        self.assertEqual(len(list(filled_dir.glob("SIM-*.json"))), 1)

    def test_run_sim_loop_retries_recoverable_ashare_cash_failure_once(self) -> None:
        signals_dir = self.tmp_path / "signals_retryable_cash"
        failed_dir = signals_dir / "failed"
        failed_dir.mkdir(parents=True, exist_ok=True)
        failed = {
            "order_id": "SIM-ashare-300418.SZ-20260630-original",
            "idempotency_key": "SIM:ashare:ashare_sim:20260630:300418.SZ:buy",
            "ts_code": "300418.SZ",
            "market": "ashare",
            "direction": "buy",
            "quantity": 100,
            "price": 10.0,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "valid_until": "2026-06-30",
            "status": "failed",
            "retry_attempt": 0,
            "failure_reason": "A-share server-local simulated fill rejected by ledger: insufficient cash",
            "failure_details": {
                "raw_mode": "server_local_sim_engine",
                "receipt_status": "rejected",
                "receipt_message": "A-share server-local simulated fill rejected by ledger: insufficient cash",
            },
        }
        (failed_dir / "SIM-ashare-300418.SZ-20260630-original.json").write_text(json.dumps(failed), encoding="utf-8")

        result = run_sim_loop(
            MultiCandidateSimAdapter(["300418.SZ"], max_candidates=1, score_universe_limit=1, max_portfolio_positions=1),
            "20260630",
            StubReader(),
            deps=self._multi_candidate_deps(),
            signals_dir=signals_dir,
        )

        self.assertEqual(result["filled_count"], 1)
        self.assertEqual(len(self.executed_orders), 1)
        self.assertEqual(self.executed_orders[0]["retry_of"], failed["order_id"])
        self.assertEqual(self.executed_orders[0]["retry_attempt"], 1)
        self.assertTrue(self.executed_orders[0]["idempotency_key"].endswith(":retry1"))
        self.assertTrue((failed_dir / "SIM-ashare-300418.SZ-20260630-original.json").exists())

    def test_run_sim_loop_stops_recoverable_ashare_retry_after_limit(self) -> None:
        signals_dir = self.tmp_path / "signals_retry_limit"
        failed_dir = signals_dir / "failed"
        failed_dir.mkdir(parents=True, exist_ok=True)
        failed = {
            "order_id": "SIM-ashare-300418.SZ-20260630-retry2",
            "idempotency_key": "SIM:ashare:ashare_sim:20260630:300418.SZ:buy:retry2",
            "ts_code": "300418.SZ",
            "market": "ashare",
            "direction": "buy",
            "quantity": 100,
            "price": 10.0,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "valid_until": "2026-06-30",
            "status": "failed",
            "retry_attempt": 2,
            "failure_reason": "A-share server-local simulated fill rejected by ledger: insufficient cash",
            "failure_details": {
                "raw_mode": "server_local_sim_engine",
                "receipt_status": "rejected",
                "receipt_message": "A-share server-local simulated fill rejected by ledger: insufficient cash",
            },
        }
        (failed_dir / "SIM-ashare-300418.SZ-20260630-retry2.json").write_text(json.dumps(failed), encoding="utf-8")

        result = run_sim_loop(
            MultiCandidateSimAdapter(["300418.SZ"], max_candidates=1, score_universe_limit=1, max_portfolio_positions=1),
            "20260630",
            StubReader(),
            deps=self._multi_candidate_deps(),
            signals_dir=signals_dir,
        )

        self.assertEqual(result["filled_count"], 0)
        self.assertEqual(self.executed_orders, [])
        self.assertEqual(result["records"][0]["signal_result"]["status"], "duplicate")

    def test_run_sim_loop_with_real_ashare_sim_broker_fills_locally_by_default(self) -> None:
        from Ashare.sim_executor import ashare_sim_execute

        sim_executor_registry.register_sim_executor("unit", ashare_sim_execute)

        received_markets: list[str] = []

        def execute_sim_order(order: dict[str, object], market: str, account: object = None) -> object:
            received_markets.append(market)
            ashare_order = dict(order)
            ashare_order["ts_code"] = "600000.SH"
            ashare_order["quantity"] = 100
            return ashare_sim_execute(
                ashare_order,
                account=account,
                config={"signals_dir": self.tmp_path / "signals", "bypass_market_hours": True},
            )

        deps = self._deps()
        deps.execute_sim_order = execute_sim_order
        result = run_sim_loop(
            StubSimAdapter(),
            "20260630",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals",
        )

        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["filled_count"], 1)
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(result["failed_count"], 0)
        pending_files = list((self.tmp_path / "signals" / "pending").glob("SIM-*.json"))
        filled_files = list((self.tmp_path / "signals" / "filled").glob("SIM-*.json"))
        self.assertEqual(len(pending_files), 0)
        self.assertEqual(len(filled_files), 1)
        filled = read_json(filled_files[0])
        self.assertEqual(filled["capital_layer"], "simulated")
        self.assertEqual(filled["account_type"], "simulated")
        self.assertEqual(received_markets, ["unit"])
        self.assertEqual(result["records"][0]["receipt"]["raw_response"]["mode"], "server_local_sim_engine")
        self.assertEqual(filled["filled_quantity"], 100)

    def test_run_sim_loop_reports_no_trade_risk_rejections(self) -> None:
        deps = self._deps()

        def reject_risk(order: dict[str, object], portfolio: dict[str, object]) -> dict[str, object]:
            return {"approved": False, "adjusted_weight": 0.0, "reasons": ["unit risk rejection"]}

        deps.risk_check = reject_risk

        result = run_sim_loop(
            StubSimAdapter(),
            "20260630",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals",
        )

        self.assertEqual(result["filled_count"], 0)
        self.assertEqual(result["order_count"], 0)
        self.assertEqual(result["risk_rejection_count"], 1)
        self.assertEqual(result["no_trade_explanation"]["category"], "all_rejected_by_risk")
        self.assertEqual(result["no_trade_explanation"]["counts"]["risk_rejections"], 1)
        self.assertEqual(result["risk_rejections"][0]["reasons"], ["unit risk rejection"])

    def test_run_sim_loop_ranks_candidates_by_combined_score_before_limit(self) -> None:
        scores = {"AAA": 0.10, "BBB": 0.92, "CCC": 0.81}
        deps = self._multi_candidate_deps()

        def score_universe(date: str, universe: list[str], data_reader: object = None, market: str = "ashare") -> list[tuple[str, dict[str, object]]]:
            return [
                (symbol, {"combined": scores[symbol], "sector": "unit", "turnover_wan": 10000, "capital_layer": "simulated"})
                for symbol in universe
            ]

        deps.score_universe = score_universe

        with self._approved_expansion_evidence("20260630"):
            result = run_sim_loop(
                MultiCandidateSimAdapter(["AAA", "BBB", "CCC"], max_candidates=2, score_universe_limit=3, max_portfolio_positions=2),
                "20260630",
                StubReader(),
                deps=deps,
                signals_dir=self.tmp_path / "signals_ranked",
            )

        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual([record["symbol"] for record in result["records"]], ["BBB", "CCC"])
        self.assertEqual([order["ts_code"] for order in self.executed_orders], ["BBB", "CCC"])
        self.assertTrue(all(order["candidate_pool_layer"] == "candidate" for order in self.executed_orders))
        self.assertTrue(all(order["execution_source"] == "ashare_candidate_layer" for order in self.executed_orders))

    def test_run_sim_loop_passes_precomputed_scores_to_candidate_pool(self) -> None:
        deps = self._multi_candidate_deps()
        received_scores: dict[str, dict[str, object]] = {}

        def score_universe(date: str, universe: list[str], data_reader: object = None, market: str = "ashare") -> list[tuple[str, dict[str, object]]]:
            return [
                ("AAA", {"combined": 0.54, "sector": "unit", "turnover_wan": 10000, "capital_layer": "simulated"}),
                ("BBB", {"combined": 0.92, "sector": "unit", "turnover_wan": 10000, "capital_layer": "simulated"}),
            ]

        def build_pool(
            date: str,
            universe: list[str],
            market: str | None = None,
            reader: object | None = None,
            scores_by_symbol: dict[str, dict[str, object]] | None = None,
        ) -> dict[str, list[str]]:
            self.calls.append("candidate_pool")
            received_scores.update(scores_by_symbol or {})
            return {
                "candidate": [
                    symbol
                    for symbol in universe
                    if float((scores_by_symbol or {}).get(symbol, {}).get("combined", 0.0)) >= 0.55
                ],
                "watch": [],
                "holdings": [],
                "universe": list(universe),
            }

        deps.score_universe = score_universe
        deps.build_pool = build_pool

        result = run_sim_loop(
            MultiCandidateSimAdapter(["AAA", "BBB"], max_candidates=2, score_universe_limit=2, max_portfolio_positions=2),
            "20260630",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_precomputed_scores",
        )

        self.assertEqual(received_scores["AAA"]["combined"], 0.54)
        self.assertEqual(received_scores["BBB"]["combined"], 0.92)
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual([record["symbol"] for record in result["records"]], ["BBB"])

    def test_run_sim_loop_caps_ashare_new_positions_to_configured_target(self) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(date: str, universe: list[str], data_reader: object = None, market: str = "ashare") -> list[tuple[str, dict[str, object]]]:
            return [
                (symbol, {"combined": 1.0 - index * 0.01, "sector": "unit", "turnover_wan": 10000, "capital_layer": "simulated"})
                for index, symbol in enumerate(universe)
            ]

        deps.score_universe = score_universe

        with self._approved_expansion_evidence("20260630"):
            result = run_sim_loop(
                MultiCandidateSimAdapter(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"], max_candidates=6, score_universe_limit=6, max_portfolio_positions=3),
                "20260630",
                StubReader(),
                deps=deps,
                signals_dir=self.tmp_path / "signals_capped",
            )

        self.assertEqual(result["order_count"], 3)
        self.assertEqual(result["filled_count"], 3)
        self.assertEqual([order["ts_code"] for order in self.executed_orders], ["AAA", "BBB", "CCC"])

    def test_run_sim_loop_uses_dynamic_capital_plan_to_block_weak_ashare_candidates(self) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(date: str, universe: list[str], data_reader: object = None, market: str = "ashare") -> list[tuple[str, dict[str, object]]]:
            scores = {"AAA": 0.52, "BBB": 0.50, "CCC": 0.48}
            return [
                (symbol, {"combined": scores[symbol], "sector": "unit", "turnover_wan": 10000, "capital_layer": "simulated"})
                for symbol in universe
            ]

        deps.score_universe = score_universe

        result = run_sim_loop(
            MultiCandidateSimAdapter(["AAA", "BBB", "CCC"], max_candidates=3, score_universe_limit=3, max_portfolio_positions=3),
            "20260630",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_dynamic_capital",
        )

        self.assertEqual(result["capital_plan"]["risk_mode"], "defensive")
        self.assertEqual(result["capital_plan"]["target_positions"], 0)
        self.assertEqual(result["order_count"], 0)
        self.assertEqual(result["filled_count"], 0)
        self.assertEqual(self.executed_orders, [])
        self.assertEqual(result["no_trade_explanation"]["category"], "capital_plan_defensive")
        self.assertEqual(result["no_trade_explanation"]["score_diagnostics"]["actual_candidate_count"], 3)
        self.assertEqual(result["no_trade_explanation"]["capital_plan_decision"]["risk_mode"], "defensive")
        self.assertEqual(result["no_trade_explanation"]["portfolio_decision"]["allowed_buy_count"], 0)
        self.assertEqual(
            {row["symbol"]: row["drop_reason"] for row in result["no_trade_explanation"]["candidate_decision_trace"]},
            {
                "AAA": "defensive_no_target_positions",
                "BBB": "defensive_no_target_positions",
                "CCC": "defensive_no_target_positions",
            },
        )
        self.assertEqual(result["candidate_layer_breakdown"]["candidate"], 3)
        self.assertEqual(result["capital_plan_decision"]["risk_mode"], "defensive")
        self.assertEqual(result["capital_plan_decision"]["target_positions"], 0)
        self.assertEqual(result["capital_plan_decision"]["position_capacity"], 0)
        self.assertEqual(result["capital_plan_decision"]["capacity_reason"], "defensive_no_target_positions")
        self.assertEqual(result["portfolio_decision"]["ranked_risk_approved_candidates"], 3)
        self.assertEqual(result["portfolio_decision"]["allowed_buy_count"], 0)
        self.assertEqual(
            {row["symbol"]: row["drop_reason"] for row in result["candidate_decision_trace"]},
            {
                "AAA": "defensive_no_target_positions",
                "BBB": "defensive_no_target_positions",
                "CCC": "defensive_no_target_positions",
            },
        )

    def test_run_sim_loop_ashare_does_not_trade_watch_layer(self) -> None:
        deps = self._multi_candidate_deps()

        def build_pool(
            date: str,
            universe: list[str],
            market: str | None = None,
            reader: object | None = None,
        ) -> dict[str, list[str]]:
            return {"candidate": [], "watch": list(universe), "holdings": [], "universe": list(universe)}

        deps.build_pool = build_pool

        def score_universe(date: str, universe: list[str], data_reader: object = None, market: str = "ashare") -> list[tuple[str, dict[str, object]]]:
            return [
                (symbol, {"combined": 0.92, "sector": "unit", "turnover_wan": 10000, "capital_layer": "simulated"})
                for symbol in universe
            ]

        deps.score_universe = score_universe

        result = run_sim_loop(
            MultiCandidateSimAdapter(["000001.SZ", "000002.SZ"], max_candidates=2, score_universe_limit=2, max_portfolio_positions=2),
            "20260630",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_watch_only",
        )

        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["order_count"], 0)
        self.assertEqual(result["filled_count"], 0)
        self.assertEqual(result["no_trade_explanation"]["category"], "no_candidates")
        self.assertEqual(self.executed_orders, [])

    def test_run_sim_loop_ashare_does_not_fallback_to_universe_when_pool_empty(self) -> None:
        deps = self._multi_candidate_deps()

        def build_pool(
            date: str,
            universe: list[str],
            market: str | None = None,
            reader: object | None = None,
        ) -> dict[str, list[str]]:
            return {"candidate": [], "watch": [], "holdings": [], "universe": list(universe)}

        deps.build_pool = build_pool

        def score_universe(date: str, universe: list[str], data_reader: object = None, market: str = "ashare") -> list[tuple[str, dict[str, object]]]:
            return [
                (symbol, {"combined": 0.95, "sector": "unit", "turnover_wan": 10000, "capital_layer": "simulated"})
                for symbol in universe
            ]

        deps.score_universe = score_universe

        result = run_sim_loop(
            MultiCandidateSimAdapter(["000001.SZ", "000002.SZ"], max_candidates=2, score_universe_limit=2, max_portfolio_positions=2),
            "20260630",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_empty_pool",
        )

        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["filled_count"], 0)
        diagnostics = result["no_trade_explanation"]["score_diagnostics"]
        self.assertEqual(diagnostics["scored_count"], 2)
        self.assertEqual(diagnostics["candidate_threshold"], 0.55)
        self.assertEqual(diagnostics["top_scores"][0]["combined"], 0.95)
        self.assertEqual(diagnostics["candidate_above_threshold_count"], 2)
        self.assertEqual(diagnostics["candidate_pool_status"], "pool_empty_despite_threshold_scores")
        self.assertEqual(self.executed_orders, [])

    def test_run_sim_loop_ashare_diagnoses_all_neutral_scores_as_missing_evidence(self) -> None:
        deps = self._multi_candidate_deps()

        def build_pool(
            date: str,
            universe: list[str],
            market: str | None = None,
            reader: object | None = None,
        ) -> dict[str, list[str]]:
            return {"candidate": [], "watch": list(universe), "holdings": [], "universe": list(universe)}

        deps.build_pool = build_pool

        def score_universe(date: str, universe: list[str], data_reader: object = None, market: str = "ashare") -> list[tuple[str, dict[str, object]]]:
            evidence_sources = {
                "macro": {"has_evidence": False, "source": "MarketGraph regime", "reason": "missing_regime"},
                "event": {"has_evidence": False, "source": "SharedSignals events + MarketGraph candidates", "reason": "no_matched_event_evidence"},
                "fundamental": {"has_evidence": False, "source": "SharedSignals fundamentals/factors", "reason": "missing_fundamental_rows"},
                "capital": {"has_evidence": False, "source": "SharedSignals capital flow/factors", "reason": "missing_capital_flow_rows"},
                "technical": {"has_evidence": False, "source": "SharedSignals daily bars", "reason": "insufficient_daily_bars"},
                "sentiment": {"has_evidence": False, "source": "SharedSignals/MarketGraph sentiment", "reason": "missing_sentiment_rows"},
            }
            return [
                (
                    symbol,
                    {
                        "combined": 0.5,
                        "macro": 0.5,
                        "event": 0.5,
                        "fundamental": 0.5,
                        "capital": 0.5,
                        "technical": 0.5,
                        "sentiment": 0.5,
                        "evidence_coverage": 0.0,
                        "missing_evidence_dimensions": ["macro", "event", "fundamental", "capital", "technical", "sentiment"],
                        "evidence_sources": evidence_sources,
                    },
                )
                for symbol in universe
            ]

        deps.score_universe = score_universe

        result = run_sim_loop(
            MultiCandidateSimAdapter(["000001.SZ", "000002.SZ"], max_candidates=2, score_universe_limit=2, max_portfolio_positions=2),
            "20260630",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_neutral_pool",
        )

        diagnostics = result["no_trade_explanation"]["score_diagnostics"]
        self.assertEqual(diagnostics["scored_count"], 2)
        self.assertEqual(diagnostics["all_neutral_symbol_count"], 2)
        self.assertEqual(diagnostics["data_quality_status"], "missing_evidence_default_like")
        self.assertEqual(diagnostics["all_neutral_symbol_sample"], ["000001.SZ", "000002.SZ"])
        self.assertEqual(diagnostics["missing_and_default_like_dimension_counts"]["capital"], 2)
        self.assertEqual(diagnostics["evidence_reason_summary"]["capital"]["missing_capital_flow_rows"], 2)
        self.assertEqual(diagnostics["evidence_coverage_distribution"]["zero"], 2)
        self.assertEqual(diagnostics["all_missing_evidence_symbol_reason_sample"][0]["reasons"]["technical"], "insufficient_daily_bars")

    def test_run_sim_loop_ashare_fails_closed_when_candidate_pool_errors(self) -> None:
        deps = self._multi_candidate_deps()

        def build_pool(
            date: str,
            universe: list[str],
            market: str | None = None,
            reader: object | None = None,
        ) -> dict[str, list[str]]:
            raise RuntimeError("candidate pool unavailable")

        deps.build_pool = build_pool

        def score_universe(date: str, universe: list[str], data_reader: object = None, market: str = "ashare") -> list[tuple[str, dict[str, object]]]:
            return [
                (symbol, {"combined": 0.95, "sector": "unit", "turnover_wan": 10000, "capital_layer": "simulated"})
                for symbol in universe
            ]

        deps.score_universe = score_universe

        result = run_sim_loop(
            MultiCandidateSimAdapter(["000001.SZ", "000002.SZ"], max_candidates=2, score_universe_limit=2, max_portfolio_positions=2),
            "20260630",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_pool_error",
        )

        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["filled_count"], 0)
        self.assertEqual(result["no_trade_explanation"]["category"], "no_candidates")
        self.assertTrue(any(error.get("stage") == "screening.candidate_pool" for error in result["errors"]))
        self.assertEqual(self.executed_orders, [])

    def test_run_sim_loop_uses_account_snapshot_cash_for_ashare_capital_plan(self) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(date: str, universe: list[str], data_reader: object = None, market: str = "ashare") -> list[tuple[str, dict[str, object]]]:
            return [
                (symbol, {"combined": 0.86 - index * 0.02, "sector": "unit", "turnover_wan": 10000, "capital_layer": "simulated"})
                for index, symbol in enumerate(universe)
            ]

        deps.score_universe = score_universe

        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["AAA", "BBB", "CCC"],
                max_candidates=3,
                score_universe_limit=3,
                max_portfolio_positions=3,
                cash_available=12_000.0,
            ),
            "20260630",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_cash_snapshot",
        )

        self.assertEqual(result["capital_plan"]["available_cash"], 12000.0)
        self.assertEqual(result["capital_plan"]["cash_source"], "account_snapshot")
        self.assertEqual(result["capital_plan"]["max_new_positions"], 0)
        self.assertEqual(result["filled_count"], 0)

    def test_run_sim_loop_excludes_validation_samples_from_ashare_capital_plan(self) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(date: str, universe: list[str], data_reader: object = None, market: str = "ashare") -> list[tuple[str, dict[str, object]]]:
            return [
                (symbol, {"combined": 0.82 - index * 0.02, "sector": "unit", "turnover_wan": 10000, "capital_layer": "simulated"})
                for index, symbol in enumerate(universe)
            ]

        deps.score_universe = score_universe
        validation_positions = [
            {"ts_code": "000101.SZ", "quantity": 100, "sellable_quantity": 100, "avg_price": 10.0, "last_price": 10.0, "market_value": 10000.0},
            {"ts_code": "000102.SZ", "quantity": 100, "sellable_quantity": 100, "avg_price": 10.0, "last_price": 10.0, "market_value": 10000.0},
            {"ts_code": "000103.SZ", "quantity": 100, "sellable_quantity": 100, "avg_price": 10.0, "last_price": 10.0, "market_value": 10000.0},
        ]

        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["AAA", "BBB", "CCC"],
                max_candidates=3,
                score_universe_limit=3,
                max_portfolio_positions=3,
                positions=validation_positions,
                cash_available=150000.0,  # Below strategy_cash to trigger cap, but enough for a buy
                strategy_positions=[],
                strategy_cash_available=200000.0,
                sample_adjustment={
                    "view": "strategy_valid_samples_only",
                    "ignored_validation_sample_count": 3,
                    "reason": "chain_validation_samples_do_not_consume_strategy_capital",
                },
            ),
            "20260630",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_validation_capital_view",
        )

        # Strategy cash (200k) is capped to real account cash (150k) for
        # budget safety; original uncapped value is preserved for diagnostics.
        self.assertEqual(result["capital_plan"]["available_cash"], 150000.0)
        self.assertEqual(result["capital_plan"]["existing_position_count"], 0)
        self.assertEqual(result["capital_plan"]["sample_adjustment"]["ignored_validation_sample_count"], 3)
        self.assertEqual(result["capital_plan"]["sample_adjustment"]["original_strategy_cash_available"], 200000.0)
        self.assertTrue(result["capital_plan"]["sample_adjustment"]["strategy_cash_capped_to_account"])
        self.assertEqual(result["capital_plan_decision"]["account_cash_available"], 150000.0)
        self.assertGreater(result["capital_plan_decision"]["position_capacity"], 0)

    def test_run_sim_loop_uses_sample_count_for_ashare_probe_position(self) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(date: str, universe: list[str], data_reader: object = None, market: str = "ashare") -> list[tuple[str, dict[str, object]]]:
            return [
                (symbol, {"combined": 0.60, "sector": "unit", "turnover_wan": 10000, "capital_layer": "simulated"})
                for symbol in universe
            ]

        deps.score_universe = score_universe
        strategy_positions = [
            {"ts_code": "300759.SZ", "quantity": 1900, "sellable_quantity": 0, "avg_price": 30.34, "last_price": 30.31, "market_value": 57589.0},
            {"ts_code": "600030.SH", "quantity": 2100, "sellable_quantity": 0, "avg_price": 28.03, "last_price": 28.00, "market_value": 58800.0},
        ]

        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["300418.SZ"],
                max_candidates=1,
                score_universe_limit=1,
                max_portfolio_positions=3,
                positions=strategy_positions,
                cash_available=83461.87,
                strategy_positions=strategy_positions,
                strategy_cash_available=83461.87,
                sample_adjustment={
                    "strategy_sample_valid_count": 2,
                    "min_strategy_samples": 5,
                },
            ),
            "20260709",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_sample_collection",
        )

        self.assertEqual(result["capital_plan"]["risk_mode"], "sample_collection")
        self.assertEqual(result["capital_plan"]["max_new_positions"], 1)
        self.assertEqual(result["capital_plan_decision"]["position_capacity"], 1)
        self.assertEqual(result["order_count"], 1)
        self.assertEqual(result["filled_count"], 1)
        self.assertEqual(self.executed_orders[0]["ts_code"], "300418.SZ")

    def test_stale_epoch_high_scores_cannot_enter_aggressive_capital_plan(self) -> None:
        from Ashare.evolution_controller import build_evolution_decision

        decision = build_evolution_decision(
            {
                "capital_epoch": 1,
                "capital_cny": 200_000.0,
                "epoch_cutover_timestamp": "2025-01-01T00:00:00+00:00",
                "trade_date": "20260711",
                "strategy_sample_count": 20,
                "pnl": {"total_pnl": 100.0, "realized_pnl": 100.0, "equity": 50_100.0},
                "evolution_evidence": {
                    "eligible_sample_count": 20,
                    "realized_round_trip_count": 10,
                    "forward_label_count": 20,
                },
            },
            epoch_authority={
                "current_epoch_id": 2,
                "capital_cny": 50_000.0,
                "cutover_timestamp": "2026-07-10T20:56:58+00:00",
            },
            target_trade_date="20260711",
            current_epoch_id=2,
        )

        with (
            patch("Ashare.evolution_controller.load_latest_decision", return_value=decision),
            patch("shared.execution.sim_account_epoch.read_epoch_state", return_value={"current_epoch_id": 2}),
        ):
            result = run_sim_loop(
                MultiCandidateSimAdapter(
                    ["000001.SZ", "600000.SH", "300418.SZ"],
                    max_candidates=3,
                    score_universe_limit=3,
                    max_portfolio_positions=3,
                ),
                "20260711",
                StubReader(),
                deps=self._high_score_deps(),
                signals_dir=self.tmp_path / "signals_stale_epoch_capital_plan",
            )

        self.assertEqual(result["capital_plan"]["risk_mode"], "cautious")
        self.assertLessEqual(result["capital_plan"]["max_new_positions"], 1)
        self.assertFalse(result["capital_plan"]["evolution_decision"]["evidence_usable"])
        self.assertEqual(
            result["capital_plan"]["evolution_decision"]["evidence_rejection_reason"],
            "capital_epoch_mismatch",
        )

    def test_zero_realized_pnl_high_scores_cannot_enter_aggressive_capital_plan(self) -> None:
        from Ashare.evolution_controller import build_evolution_decision

        decision = build_evolution_decision(
            {
                "capital_epoch": 2,
                "capital_cny": 50_000.0,
                "epoch_cutover_timestamp": "2026-07-10T20:56:58+00:00",
                "trade_date": "20260711",
                "strategy_sample_count": 20,
                "pnl": {"total_pnl": 100.0, "realized_pnl": 0.0, "equity": 50_100.0},
                "evolution_evidence": {
                    "eligible_sample_count": 20,
                    "realized_round_trip_count": 10,
                    "forward_label_count": 20,
                },
            },
            epoch_authority={
                "current_epoch_id": 2,
                "capital_cny": 50_000.0,
                "cutover_timestamp": "2026-07-10T20:56:58+00:00",
            },
            target_trade_date="20260711",
            current_epoch_id=2,
        )

        with (
            patch("Ashare.evolution_controller.load_latest_decision", return_value=decision),
            patch("shared.execution.sim_account_epoch.read_epoch_state", return_value={"current_epoch_id": 2}),
        ):
            result = run_sim_loop(
                MultiCandidateSimAdapter(
                    ["000001.SZ", "600000.SH", "300418.SZ"],
                    max_candidates=3,
                    score_universe_limit=3,
                    max_portfolio_positions=3,
                ),
                "20260711",
                StubReader(),
                deps=self._high_score_deps(),
                signals_dir=self.tmp_path / "signals_zero_realized_capital_plan",
            )

        self.assertEqual(result["capital_plan"]["risk_mode"], "cautious")
        self.assertLessEqual(result["capital_plan"]["max_new_positions"], 1)
        self.assertTrue(result["capital_plan"]["evolution_decision"]["evidence_usable"])
        self.assertEqual(
            result["capital_plan"]["evolution_decision"]["recommended_action"],
            "observe_and_label_candidates",
        )

    def test_epoch_validation_error_is_explicit_and_fails_closed(self) -> None:
        decision = {
            "capital_epoch": 2,
            "evidence_trade_date": "20260711",
            "state": "expansion_candidate",
            "recommended_action": "expand_risk_candidate",
            "reasons": ["positive_realized_pnl_after_all_gates"],
            "policy": {
                "strategy_sample_count": 20,
                "min_strategy_samples": 5,
                "sample_collection_min_score": 0.55,
            },
        }

        with (
            patch("Ashare.evolution_controller.load_latest_decision", return_value=decision),
            patch("shared.execution.sim_account_epoch.read_epoch_state", side_effect=RuntimeError("epoch unavailable")),
        ):
            result = run_sim_loop(
                MultiCandidateSimAdapter(
                    ["000001.SZ", "600000.SH", "300418.SZ"],
                    max_candidates=3,
                    score_universe_limit=3,
                    max_portfolio_positions=3,
                ),
                "20260711",
                StubReader(),
                deps=self._high_score_deps(),
                signals_dir=self.tmp_path / "signals_epoch_error_capital_plan",
            )

        self.assertEqual(result["capital_plan"]["risk_mode"], "cautious")
        self.assertLessEqual(result["capital_plan"]["max_new_positions"], 1)
        self.assertFalse(result["capital_plan"]["evolution_decision"]["evidence_usable"])
        self.assertEqual(
            result["capital_plan"]["evolution_decision"]["evidence_rejection_reason"],
            "evidence_unavailable",
        )

    def test_run_sim_loop_attaches_latest_5min_bar_to_ashare_order(self) -> None:
        class IntradayReader(StubReader):
            def get_bars_intraday(self, market, symbol, interval="5m", start=None, end=None):
                return [
                    {
                        "close": 10.6,
                        "bar_time": "2026-06-30 10:05:00",
                        "volume": 1800,
                        "provider": "sharedsignals_api_realtime_5min",
                    }
                ]

        deps = self._multi_candidate_deps()

        result = run_sim_loop(
            MultiCandidateSimAdapter(["600000.SH"], max_candidates=1, score_universe_limit=1, max_portfolio_positions=1),
            "20260630",
            IntradayReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_5min_evidence",
        )

        self.assertEqual(result["filled_count"], 1)
        snapshot = self.executed_orders[0]["market_snapshot"]
        self.assertEqual(snapshot["bar_time"], "2026-06-30 10:05:00")
        self.assertEqual(snapshot["bar_volume"], 1800)
        self.assertEqual(snapshot["provider"], "sharedsignals_api_realtime_5min")

    def test_run_sim_loop_uses_each_execution_symbol_for_ashare_5min_evidence(self) -> None:
        class IntradayReader(StubReader):
            def get_bars_intraday(self, market, symbol, interval="5m", start=None, end=None):
                if symbol == "000001.SZ":
                    return [{"close": 10.1, "bar_time": "2026-06-30 10:05:00", "volume": 1800}]
                if symbol == "600000.SH":
                    return [{"close": 20.2, "bar_time": "2026-06-30 10:10:00", "volume": 1900}]
                return []

        deps = self._multi_candidate_deps()

        def score_universe(date, universe, data_reader=None, market="ashare"):
            return [
                ("000001.SZ", {"combined": 0.80, "sector": "unit", "turnover_wan": 10000, "capital_layer": "simulated"}),
                ("600000.SH", {"combined": 0.70, "sector": "unit", "turnover_wan": 10000, "capital_layer": "simulated"}),
            ]

        deps.score_universe = score_universe
        result = run_sim_loop(
            MultiCandidateSimAdapter(["000001.SZ", "600000.SH"], max_candidates=2, score_universe_limit=2, max_portfolio_positions=1),
            "20260630",
            IntradayReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_5min_execution_symbol",
        )

        self.assertEqual(result["filled_count"], 1)
        self.assertEqual(self.executed_orders[0]["ts_code"], "000001.SZ")
        self.assertEqual(self.executed_orders[0]["market_snapshot"]["last_price"], 10.1)
        self.assertEqual(self.executed_orders[0]["market_snapshot"]["bar_time"], "2026-06-30 10:05:00")

    def test_execution_snapshot_rejects_stale_current_ashare_bar(self) -> None:
        from shared.orchestrator import _latest_execution_market_snapshot

        class IntradayReader:
            def get_bars_intraday(self, market, symbol, interval="5m", start=None, end=None):
                return [{"close": 10.1, "bar_time": "2026-07-10 10:10:00", "volume": 1800}]

        snapshot = _latest_execution_market_snapshot(
            IntradayReader(),
            "ashare",
            "000001.SZ",
            "20260710",
            "buy",
            now=datetime.fromisoformat("2026-07-10T10:54:00+08:00"),
        )

        self.assertEqual(snapshot, {})

    def test_run_sim_loop_does_not_let_retired_daily_sample_gate_override_capacity(self) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(date: str, universe: list[str], data_reader: object = None, market: str = "ashare") -> list[tuple[str, dict[str, object]]]:
            return [
                (symbol, {"combined": 0.60, "sector": "unit", "turnover_wan": 10000, "capital_layer": "simulated"})
                for symbol in universe
            ]

        deps.score_universe = score_universe
        strategy_positions = [
            {"ts_code": "300759.SZ", "quantity": 1900, "sellable_quantity": 0, "avg_price": 30.34, "last_price": 30.31, "market_value": 57589.0},
            {"ts_code": "600030.SH", "quantity": 2100, "sellable_quantity": 0, "avg_price": 28.03, "last_price": 28.00, "market_value": 58800.0},
        ]
        decision = {
            "recommended_action": "force_sample_collection",
            "policy": {
                "daily_sample_hard_gate": True,
                "daily_strategy_sample_target": 1,
                "today_strategy_sample_count": 0,
                "strategy_sample_count": 8,
                "min_strategy_samples": 5,
                "sample_collection_min_score": 0.55,
            },
        }

        with patch("Ashare.evolution_controller.load_latest_decision", return_value=decision):
            result = run_sim_loop(
                MultiCandidateSimAdapter(
                    ["300418.SZ"],
                    max_candidates=1,
                    score_universe_limit=1,
                    max_portfolio_positions=3,
                    positions=strategy_positions,
                    cash_available=83461.87,
                    strategy_positions=strategy_positions,
                    strategy_cash_available=83461.87,
                    sample_adjustment={
                        "strategy_sample_valid_count": 8,
                        "min_strategy_samples": 5,
                    },
                ),
                "20260710",
                StubReader(),
                deps=deps,
                signals_dir=self.tmp_path / "signals_daily_sample_gate",
            )

        self.assertNotEqual(result["capital_plan"]["risk_mode"], "sample_collection")
        self.assertNotIn("daily_strategy_sample_target_not_met", result["capital_plan"]["reasons"])
        self.assertEqual(result["order_count"], 0)
        self.assertEqual(result["filled_count"], 0)
        self.assertEqual(result["capital_plan"]["capacity_reason"], "target_positions_reached")
        self.assertEqual(result["candidate_decision_trace"][0]["drop_reason"], "target_positions_reached")
        self.assertEqual(self.executed_orders, [])

    def test_run_sim_loop_compresses_excess_ashare_positions_and_logs_capital_plan(self) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(date: str, universe: list[str], data_reader: object = None, market: str = "ashare") -> list[tuple[str, dict[str, object]]]:
            return [
                (symbol, {"combined": 0.86 - index * 0.03, "sector": "unit", "turnover_wan": 10000, "capital_layer": "simulated"})
                for index, symbol in enumerate(universe)
            ]

        deps.score_universe = score_universe
        positions = [
            {"ts_code": f"{i + 1:06d}.SZ", "quantity": 100, "sellable_quantity": 100, "avg_price": 10.0, "last_price": 10.0, "weight": 0.08}
            for i in range(5)
        ]

        with self._approved_expansion_evidence("20260630"):
            result = run_sim_loop(
                MultiCandidateSimAdapter(
                    ["AAA", "BBB", "CCC"],
                    max_candidates=3,
                    score_universe_limit=3,
                    max_portfolio_positions=3,
                    positions=positions,
                ),
                "20260630",
                StubReader(),
                deps=deps,
                signals_dir=self.tmp_path / "signals_rebalance",
            )

        sell_orders = [order for order in self.executed_orders if order["side"] == "sell"]
        self.assertEqual(result["rebalance"]["planned_sell_count"], 2)
        self.assertEqual(len(sell_orders), 2)
        self.assertEqual(result["order_count"], 2)
        self.assertEqual(result["capital_plan_log"]["status"], "written")
        self.assertEqual(result["post_execution_capital_plan_refresh"]["status"], "written")
        log_path = Path(result["capital_plan_log"]["path"])
        rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(rows[0]["rebalance"]["planned_sell_count"], 2)
        self.assertEqual(rows[0]["capital_plan"]["target_positions"], 3)
        self.assertEqual(rows[-1]["capital_plan"]["refresh_phase"], "post_execution")
        self.assertEqual(rows[-1]["capital_plan"]["max_new_positions"], 0)

    def test_run_sim_loop_sells_stop_loss_ashare_position_even_within_target_count(self) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(date: str, universe: list[str], data_reader: object = None, market: str = "ashare") -> list[tuple[str, dict[str, object]]]:
            return [
                (symbol, {"combined": 0.86, "sector": "unit", "turnover_wan": 10000, "capital_layer": "simulated"})
                for symbol in universe
            ]

        deps.score_universe = score_universe
        positions = [
            {"ts_code": "000010.SZ", "quantity": 100, "sellable_quantity": 100, "avg_price": 12.0, "last_price": 10.0, "weight": 0.08}
        ]

        result = run_sim_loop(
            MultiCandidateSimAdapter(["AAA", "BBB", "CCC"], max_candidates=3, score_universe_limit=3, max_portfolio_positions=3, positions=positions),
            "20260630",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_stop_loss",
        )

        sell_orders = [order for order in self.executed_orders if order["side"] == "sell"]
        self.assertEqual(result["rebalance"]["planned_sell_count"], 1)
        self.assertEqual(sell_orders[0]["ts_code"], "000010.SZ")
        self.assertIn("stop_loss", sell_orders[0]["note"])

    def test_run_sim_loop_merges_duplicate_lot_rows_into_one_sell_order(self) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(date: str, universe: list[str], data_reader: object = None, market: str = "ashare") -> list[tuple[str, dict[str, object]]]:
            return [
                (symbol, {"combined": 0.86, "sector": "unit", "turnover_wan": 10000, "capital_layer": "simulated"})
                for symbol in universe
            ]

        deps.score_universe = score_universe
        positions = [
            {"ts_code": "000010.SZ", "quantity": 3000, "sellable_quantity": 3000, "avg_price": 12.0, "last_price": 10.0, "weight": 0.15},
            {"ts_code": "000010.SZ", "quantity": 2000, "sellable_quantity": 2000, "avg_price": 12.0, "last_price": 10.0, "weight": 0.10},
        ]

        result = run_sim_loop(
            MultiCandidateSimAdapter(["AAA"], max_candidates=1, score_universe_limit=1, max_portfolio_positions=3, positions=positions),
            "20260630",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_duplicate_lots",
        )

        sell_orders = [order for order in self.executed_orders if order["side"] == "sell"]
        self.assertEqual(result["rebalance"]["planned_sell_count"], 1)
        self.assertEqual(len(sell_orders), 1)
        self.assertEqual(sell_orders[0]["ts_code"], "000010.SZ")
        self.assertEqual(sell_orders[0]["quantity"], 5000)

    def test_run_sim_loop_does_not_liquidate_normal_positions_when_capital_plan_is_defensive(self) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(date: str, universe: list[str], data_reader: object = None, market: str = "ashare") -> list[tuple[str, dict[str, object]]]:
            return [
                (symbol, {"combined": 0.50, "sector": "unit", "turnover_wan": 10000, "capital_layer": "simulated"})
                for symbol in universe
            ]

        deps.score_universe = score_universe
        positions = [
            {"ts_code": "000010.SZ", "quantity": 100, "sellable_quantity": 100, "avg_price": 10.0, "last_price": 10.0, "weight": 0.08},
            {"ts_code": "000011.SZ", "quantity": 100, "sellable_quantity": 100, "avg_price": 10.0, "last_price": 10.0, "weight": 0.08},
        ]

        result = run_sim_loop(
            MultiCandidateSimAdapter(["AAA", "BBB"], max_candidates=2, score_universe_limit=2, max_portfolio_positions=3, positions=positions),
            "20260630",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_defensive_hold",
        )

        self.assertEqual(result["capital_plan"]["risk_mode"], "defensive")
        self.assertEqual(result["rebalance"]["planned_sell_count"], 0)
        self.assertEqual(result["order_count"], 0)
        self.assertEqual(self.executed_orders, [])

    def test_run_sim_loop_does_not_buy_same_symbol_planned_for_rebalance_sell(self) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(date: str, universe: list[str], data_reader: object = None, market: str = "ashare") -> list[tuple[str, dict[str, object]]]:
            scores = {"000010.SZ": 0.92, "000011.SZ": 0.88, "000012.SZ": 0.84}
            return [
                (symbol, {"combined": scores[symbol], "sector": "unit", "turnover_wan": 10000, "capital_layer": "simulated"})
                for symbol in universe
            ]

        deps.score_universe = score_universe
        positions = [
            {"ts_code": "000010.SZ", "quantity": 100, "sellable_quantity": 100, "avg_price": 12.0, "last_price": 10.0, "weight": 0.08}
        ]

        with self._approved_expansion_evidence("20260630"):
            result = run_sim_loop(
                MultiCandidateSimAdapter(["000010.SZ", "000011.SZ", "000012.SZ"], max_candidates=3, score_universe_limit=3, max_portfolio_positions=3, positions=positions),
                "20260630",
                StubReader(),
                deps=deps,
                signals_dir=self.tmp_path / "signals_no_round_trip",
            )

        sell_orders = [order for order in self.executed_orders if order["side"] == "sell"]
        buy_orders = [order for order in self.executed_orders if order["side"] == "buy"]
        self.assertEqual(result["rebalance"]["planned_sell_count"], 1)
        self.assertEqual(sell_orders[0]["ts_code"], "000010.SZ")
        self.assertNotIn("000010.SZ", [order["ts_code"] for order in buy_orders])
        self.assertEqual([order["ts_code"] for order in buy_orders], ["000011.SZ", "000012.SZ"])

    def test_run_sim_loop_replaces_full_position_after_stop_loss_sell(self) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(date: str, universe: list[str], data_reader: object = None, market: str = "ashare") -> list[tuple[str, dict[str, object]]]:
            scores = {"000013.SZ": 0.91, "000014.SZ": 0.82}
            return [
                (symbol, {"combined": scores[symbol], "sector": "unit", "turnover_wan": 10000, "capital_layer": "simulated"})
                for symbol in universe
            ]

        deps.score_universe = score_universe
        positions = [
            {"ts_code": "000010.SZ", "quantity": 5000, "sellable_quantity": 5000, "avg_price": 12.0, "last_price": 10.0, "weight": 0.25},
            {"ts_code": "000011.SZ", "quantity": 5000, "sellable_quantity": 5000, "avg_price": 10.0, "last_price": 10.0, "weight": 0.25},
            {"ts_code": "000012.SZ", "quantity": 5000, "sellable_quantity": 5000, "avg_price": 10.0, "last_price": 10.0, "weight": 0.25},
        ]

        with self._approved_expansion_evidence("20260630"):
            result = run_sim_loop(
                MultiCandidateSimAdapter(["000013.SZ", "000014.SZ"], max_candidates=2, score_universe_limit=2, max_portfolio_positions=3, positions=positions),
                "20260630",
                StubReader(),
                deps=deps,
                signals_dir=self.tmp_path / "signals_replacement_after_sell",
            )

        sell_orders = [order for order in self.executed_orders if order["side"] == "sell"]
        buy_orders = [order for order in self.executed_orders if order["side"] == "buy"]
        self.assertEqual(result["rebalance"]["planned_sell_count"], 1)
        self.assertEqual(sell_orders[0]["ts_code"], "000010.SZ")
        self.assertEqual([order["ts_code"] for order in buy_orders], ["000013.SZ"])
        self.assertLessEqual(
            result["capital_plan"]["replacement_budget"]["allocated_cash"],
            result["capital_plan"]["replacement_budget"]["released_cash"],
        )
        self.assertEqual(result["capital_plan"]["replacement_budget"]["allocated_cash"], 17500.0)

    def test_run_sim_loop_replaces_full_position_for_opportunity_cost_gap(self) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(date: str, universe: list[str], data_reader: object = None, market: str = "ashare") -> list[tuple[str, dict[str, object]]]:
            scores = {"000010.SZ": 0.60, "000013.SZ": 0.84, "000014.SZ": 0.76}
            return [
                (symbol, {"combined": scores[symbol], "sector": "unit", "turnover_wan": 10000, "capital_layer": "simulated"})
                for symbol in universe
            ]

        deps.score_universe = score_universe
        positions = [
            {"ts_code": "000010.SZ", "quantity": 5000, "sellable_quantity": 5000, "avg_price": 10.0, "last_price": 10.0, "market_value": 200000.0}
        ]

        result = run_sim_loop(
            MultiCandidateSimAdapter(["000010.SZ", "000013.SZ", "000014.SZ"], max_candidates=3, score_universe_limit=3, max_portfolio_positions=1, positions=positions),
            "20260630",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_opportunity_cost",
        )

        sell_orders = [order for order in self.executed_orders if order["side"] == "sell"]
        buy_orders = [order for order in self.executed_orders if order["side"] == "buy"]
        self.assertEqual(result["rebalance"]["planned_sell_count"], 1)
        self.assertEqual(sell_orders[0]["ts_code"], "000010.SZ")
        self.assertIn("opportunity_cost", sell_orders[0]["note"])
        self.assertEqual([order["ts_code"] for order in buy_orders], ["000013.SZ"])
        self.assertEqual(result["capital_plan"]["replacement_budget"]["allocations"][0]["ts_code"], "000013.SZ")

    def test_run_sim_loop_keeps_full_position_when_opportunity_gap_is_small(self) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(date: str, universe: list[str], data_reader: object = None, market: str = "ashare") -> list[tuple[str, dict[str, object]]]:
            scores = {"000010.SZ": 0.70, "000013.SZ": 0.82}
            return [
                (symbol, {"combined": scores[symbol], "sector": "unit", "turnover_wan": 10000, "capital_layer": "simulated"})
                for symbol in universe
            ]

        deps.score_universe = score_universe
        positions = [
            {"ts_code": "000010.SZ", "quantity": 5000, "sellable_quantity": 5000, "avg_price": 10.0, "last_price": 10.0, "weight": 0.25}
        ]

        result = run_sim_loop(
            MultiCandidateSimAdapter(["000010.SZ", "000013.SZ"], max_candidates=2, score_universe_limit=2, max_portfolio_positions=1, positions=positions),
            "20260630",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_opportunity_gap_small",
        )

        self.assertEqual(result["rebalance"]["planned_sell_count"], 0)
        self.assertEqual(self.executed_orders, [])

    def test_run_sim_loop_persists_exclusions_even_when_some_orders_fill(self) -> None:
        class SelectiveReader:
            def get_bars_daily(self, market: str, symbol: str, start: object = None, end: object = None) -> list[dict[str, float]]:
                if symbol == "BAD":
                    return []
                return [{"close": 10.0}]

        deps = self._multi_candidate_deps()

        def score_universe(date: str, universe: list[str], data_reader: object = None, market: str = "ashare") -> list[tuple[str, dict[str, object]]]:
            return [
                (symbol, {"combined": 0.9 if symbol == "GOOD" else 0.8, "sector": "unit", "turnover_wan": 10000, "capital_layer": "simulated"})
                for symbol in universe
            ]

        deps.score_universe = score_universe

        result = run_sim_loop(
            MultiCandidateSimAdapter(["GOOD", "BAD"], max_candidates=2, score_universe_limit=2, max_portfolio_positions=2),
            "20260630",
            SelectiveReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_exclusions",
        )

        self.assertEqual(result["filled_count"], 1)
        self.assertEqual(result["skipped_candidate_count"], 1)
        self.assertEqual(result["execution_exclusion_log"]["rows"], 1)
        exclusion_path = Path(result["execution_exclusion_log"]["path"])
        rows = [json.loads(line) for line in exclusion_path.read_text(encoding="utf-8").splitlines()]
        self.assertIn(rows[0]["kind"], {"skipped_candidate", "risk_rejection", "execution_skip"})
        self.assertEqual(rows[0]["symbol"], "BAD")

    def test_strategy_cash_capped_to_account_cash_available(self) -> None:
        """Risk-2 regression: strategy_cash_available must not exceed real
        account cash_available for budget planning."""
        deps = self._multi_candidate_deps()

        def score_universe(date: str, universe: list[str], data_reader: object = None, market: str = "ashare") -> list[tuple[str, dict[str, object]]]:
            return [
                ("300418.SZ", {"combined": 0.80, "sector": "unit", "turnover_wan": 10000, "capital_layer": "simulated"})
            ]

        deps.score_universe = score_universe

        # Strategy reports 200,000 cash (clean strategy view), but the real
        # account only has 12,000 available cash after existing positions.
        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["300418.SZ"],
                max_candidates=1,
                score_universe_limit=1,
                max_portfolio_positions=3,
                positions=[
                    {"ts_code": "300759.SZ", "quantity": 1900, "sellable_quantity": 0, "avg_price": 30.34, "last_price": 30.31, "market_value": 57589.0},
                    {"ts_code": "600030.SH", "quantity": 2100, "sellable_quantity": 0, "avg_price": 28.03, "last_price": 28.00, "market_value": 58800.0},
                ],
                cash_available=12_000.0,
                strategy_positions=[],
                strategy_cash_available=200_000.0,
                sample_adjustment={
                    "view": "strategy_valid_samples_only",
                    "ignored_validation_sample_count": 0,
                    "reason": "validation_samples_do_not_consume_strategy_capital",
                },
            ),
            "20260630",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_strategy_cash_capped",
        )

        # The capital plan should see the capped cash (12,000), not 200,000
        self.assertEqual(result["capital_plan"]["available_cash"], 12000.0)
        self.assertEqual(result["capital_plan"]["cash_source"], "account_snapshot")
        self.assertEqual(result["capital_plan"]["max_new_positions"], 0)
        # Diagnostics should show the original uncapped value
        sample_adj = result["capital_plan"].get("sample_adjustment", {})
        self.assertEqual(sample_adj.get("original_strategy_cash_available"), 200000.0)
        self.assertTrue(sample_adj.get("strategy_cash_capped_to_account"))
        self.assertEqual(sample_adj.get("account_cash_available"), 12000.0)
        self.assertEqual(sample_adj.get("strategy_cash_available"), 12000.0)

    def test_ashare_sell_orders_use_ashare_rebalance_sell_candidate_pool_layer(self) -> None:
        """Risk-3 regression: A-share sell orders must carry
        candidate_pool_layer='ashare_rebalance_sell'."""
        deps = self._multi_candidate_deps()

        def score_universe(date: str, universe: list[str], data_reader: object = None, market: str = "ashare") -> list[tuple[str, dict[str, object]]]:
            scores = {"000010.SZ": 0.60, "000013.SZ": 0.84}
            return [
                (symbol, {"combined": scores[symbol], "sector": "unit", "turnover_wan": 10000, "capital_layer": "simulated"})
                for symbol in universe
            ]

        deps.score_universe = score_universe
        positions = [
            {"ts_code": "000010.SZ", "quantity": 5000, "sellable_quantity": 5000, "avg_price": 10.0, "last_price": 10.0, "market_value": 200000.0}
        ]

        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["000010.SZ", "000013.SZ"],
                max_candidates=2,
                score_universe_limit=2,
                max_portfolio_positions=1,
                positions=positions,
            ),
            "20260630",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_rebalance_naming",
        )

        sell_orders = [order for order in self.executed_orders if order["side"] == "sell"]
        self.assertEqual(len(sell_orders), 1)
        sell = sell_orders[0]
        self.assertEqual(sell["candidate_pool_layer"], "ashare_rebalance_sell")
        self.assertEqual(sell["execution_source"], "ashare_rebalance_sell")
        self.assertIn("opportunity_cost", sell["note"])


if __name__ == "__main__":
    unittest.main()
