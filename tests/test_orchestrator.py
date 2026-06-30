from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.accounting import trade_audit_trail
from shared.execution import shadow_broker
from shared.markets.base import MarketAdapter
from shared.orchestrator import OrchestratorDeps, run_shadow_loop


class StubMarketAdapter(MarketAdapter):
    def __init__(self, market: str = "unit") -> None:
        self.market = market

    def get_universe(self, date: str) -> list[str]:
        return ["AAA", "BBB"]

    def get_market(self) -> str:
        return self.market

    def map_symbol_to_reader(self, symbol: str) -> tuple[str, str]:
        return self.market, symbol

    def get_strategy_config(self) -> dict[str, object]:
        return {
            "shadow_capital": 10000.0,
            "portfolio_method": "conviction_weighted",
            "regime": "growth",
            "max_candidates": 2,
            "default_price": 10.0,
            "default_volatility": 0.20,
        }

    def get_shadow_account(self) -> str:
        return f"{self.market}_shadow"


class StubReader:
    def get_bars_daily(self, market: str, symbol: str, start: object = None, end: object = None) -> list[dict[str, float]]:
        return [{"close": 10.0}, {"close": 10.5}, {"close": 11.0}]


def _patch_shadow_paths(testcase: unittest.TestCase, tmp_path: Path) -> None:
    shadow_dir = tmp_path / "shadow"
    patches = (
        ("SHADOW_DIR", shadow_dir),
        ("SHADOW_TRADES", shadow_dir / "shadow_trades.jsonl"),
        ("SHADOW_POSITIONS", shadow_dir / "shadow_positions.json"),
        ("SHADOW_PNL", shadow_dir / "shadow_pnl.json"),
        ("SHADOW_LOCK", shadow_dir / ".shadow.lock"),
    )
    for name, value in patches:
        patcher = patch.object(shadow_broker, name, value)
        patcher.start()
        testcase.addCleanup(patcher.stop)


def _patch_audit_paths(testcase: unittest.TestCase, tmp_path: Path) -> None:
    ledger_dir = tmp_path / "logs"
    patches = (
        ("LEDGER_DIR", ledger_dir),
        ("AUDIT_TRAIL", ledger_dir / "trade_audit_trail.jsonl"),
    )
    for name, value in patches:
        patcher = patch.object(trade_audit_trail, name, value)
        patcher.start()
        testcase.addCleanup(patcher.stop)


class OrchestratorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp_path = Path(self.tmpdir.name)
        _patch_shadow_paths(self, self.tmp_path)
        _patch_audit_paths(self, self.tmp_path)
        self.calls: list[str] = []
        self.score_requests: list[dict[str, object]] = []
        self.pool_requests: list[dict[str, object]] = []

    def _deps(self, *, fail_debate: bool = False) -> OrchestratorDeps:
        def score_stock(market: str, symbol: str, data_reader: object = None, date: str | None = None) -> dict[str, object]:
            self.calls.append("screening")
            self.score_requests.append({
                "market": market,
                "symbol": symbol,
                "date": date,
                "reader": data_reader,
            })
            return {"combined": 0.72, "sector": "unit", "capital_layer": "shadow"}

        def build_pool(
            date: str,
            universe: list[str],
            market: str | None = None,
            reader: object | None = None,
        ) -> dict[str, list[str]]:
            self.calls.append("candidate_pool")
            self.pool_requests.append({
                "market": market,
                "date": date,
                "universe": list(universe),
                "reader": reader,
            })
            return {"candidate": list(universe), "watch": [], "holdings": [], "universe": list(universe)}

        def debate(symbol: str, scores: dict[str, object]) -> dict[str, object]:
            self.calls.append("adversarial")
            if fail_debate:
                raise RuntimeError("debate failed")
            return {"ts_code": symbol, "belief_score": 0.70, "bull_case": "ok", "bear_case": "risk"}

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
                        "shares": 10,
                        "amount": 100.0,
                        "sector": "unit",
                        "price": 10.0,
                    }
                    for order in orders
                ],
                "total_weight": sum(float(order["weight"]) for order in orders),
                "cash_weight": 0.90,
            }

        def size_position(belief_score: float, volatility: float, regime: str) -> float:
            self.calls.append("position_sizer")
            return 0.05

        def review(date: str, session: str = "close") -> dict[str, object]:
            self.calls.append("review")
            return {"session": session, "trade_date": date, "capital_layer_reviews": {"shadow": {}}}

        return OrchestratorDeps(
            score_stock=score_stock,
            build_pool=build_pool,
            debate=debate,
            risk_check=risk_check,
            construct=construct,
            size_position=size_position,
            record_shadow=shadow_broker.record_shadow,
            run_review=review,
            record_audit_event=trade_audit_trail.record_event,
        )

    def test_run_shadow_loop_records_full_shadow_chain(self) -> None:
        result = run_shadow_loop(
            StubMarketAdapter(),
            "20260630",
            StubReader(),
            deps=self._deps(),
            signals_dir=self.tmp_path / "signals",
        )

        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["capital_layer"], "shadow")
        for expected in ("screening", "candidate_pool", "adversarial", "risk", "position_sizer", "portfolio", "review"):
            self.assertIn(expected, self.calls)
        self.assertEqual(result["recorded_count"], 2)
        self.assertEqual(len(list((self.tmp_path / "signals" / "pending").glob("*.json"))), 2)
        self.assertEqual({request["market"] for request in self.score_requests}, {"unit"})
        self.assertEqual({request["market"] for request in self.pool_requests}, {"unit"})

        trade_rows = [
            json.loads(line)
            for line in shadow_broker.SHADOW_TRADES.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(trade_rows), 2)
        self.assertEqual({row["capital_layer"] for row in trade_rows}, {"shadow"})

        audit_rows = [
            json.loads(line)
            for line in trade_audit_trail.AUDIT_TRAIL.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            {row["stage"] for row in audit_rows},
            {"signal", "decision", "risk", "execution", "result"},
        )
        self.assertTrue(all(row.get("metadata", {}).get("capital_layer") == "shadow" for row in audit_rows))

    def test_run_shadow_loop_fail_safe_records_degraded_without_crashing(self) -> None:
        result = run_shadow_loop(
            StubMarketAdapter(),
            "20260630",
            StubReader(),
            deps=self._deps(fail_debate=True),
            signals_dir=self.tmp_path / "signals_fail",
        )

        self.assertEqual(result["state"], "degraded")
        self.assertTrue(result["errors"])
        self.assertIn("review", self.calls)
        self.assertGreaterEqual(result["recorded_count"], 1)

    def test_run_shadow_loop_uses_adapter_market_for_non_ashare_scoring(self) -> None:
        for market in ("crypto", "us"):
            with self.subTest(market=market):
                self.calls.clear()
                self.score_requests.clear()
                self.pool_requests.clear()

                result = run_shadow_loop(
                    StubMarketAdapter(market),
                    "20260630",
                    StubReader(),
                    deps=self._deps(),
                    signals_dir=self.tmp_path / f"signals_{market}",
                )

                self.assertEqual(result["state"], "ok")
                self.assertEqual(result["market"], market)
                self.assertEqual({request["market"] for request in self.score_requests}, {market})
                self.assertEqual({request["market"] for request in self.pool_requests}, {market})


if __name__ == "__main__":
    unittest.main()
