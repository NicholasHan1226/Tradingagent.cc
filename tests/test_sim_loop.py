from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.accounting import trade_audit_trail
from shared.execution import shadow_broker
from shared.execution.signal_state_machine import read_json
from shared.markets.base import MarketAdapter
from shared.orchestrator import OrchestratorDeps, run_sim_loop


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
        self.calls: list[str] = []
        self.risk_portfolios: list[dict[str, object]] = []
        self.review_requests: list[dict[str, object]] = []
        self.executed_orders: list[dict[str, object]] = []

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


if __name__ == "__main__":
    unittest.main()
