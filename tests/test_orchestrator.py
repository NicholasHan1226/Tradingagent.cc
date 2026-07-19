from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.accounting import trade_audit_trail
from shared.execution import shadow_broker
from shared.markets.base import MarketAdapter
from shared.orchestrator import (
    OrchestratorDeps,
    _execution_quantity,
    _latest_price,
    _latest_volatility,
    run_shadow_loop,
)
from shared.portfolio.constructor import construct as construct_portfolio


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
    def get_bars_daily(
        self, market: str, symbol: str, start: object = None, end: object = None
    ) -> list[dict[str, float]]:
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

    def test_latest_price_prefers_intraday_and_daily_uses_lookback(self) -> None:
        class Reader:
            daily_calls: list[tuple[object, object]] = []

            def get_bars_intraday(
                self, market, symbol, interval="5m", start="", end=""
            ):
                return [{"close": 10.2}, {"close": 10.8}]

            def get_bars_daily(self, market, symbol, start=None, end=None):
                self.daily_calls.append((start, end))
                return [{"close": 9.8}]

        reader = Reader()

        self.assertEqual(
            _latest_price(reader, "ashare", "000001.SZ", "20260706", 0.0), 10.8
        )
        self.assertEqual(reader.daily_calls, [])

    def test_latest_price_and_volatility_fall_back_to_recent_daily_window(self) -> None:
        class Reader:
            daily_calls: list[tuple[object, object]] = []

            def get_bars_intraday(
                self, market, symbol, interval="5m", start="", end=""
            ):
                return []

            def get_bars_daily(self, market, symbol, start=None, end=None):
                self.daily_calls.append((start, end))
                return [{"close": 10.0}, {"close": 10.5}, {"close": 11.0}]

        reader = Reader()

        self.assertEqual(
            _latest_price(reader, "ashare", "000001.SZ", "20260706", 0.0), 11.0
        )
        self.assertGreater(
            _latest_volatility(reader, "ashare", "000001.SZ", "20260706", 0.2), 0.01
        )
        self.assertIn(("20260622", "20260706"), reader.daily_calls)
        self.assertIn(("20260522", "20260706"), reader.daily_calls)

    def test_ashare_execution_quantity_is_lot_aligned(self) -> None:
        self.assertEqual(_execution_quantity("ashare", "buy", 799), 700)
        self.assertEqual(_execution_quantity("ashare", "buy", 99), 0)
        self.assertEqual(_execution_quantity("us", "buy", 799), 799)

    def _deps(
        self, *, fail_debate: bool = False, use_batch_score: bool = False
    ) -> OrchestratorDeps:
        def score_stock(
            market: str,
            symbol: str,
            data_reader: object = None,
            date: str | None = None,
        ) -> dict[str, object]:
            self.calls.append("screening")
            self.score_requests.append(
                {
                    "market": market,
                    "symbol": symbol,
                    "date": date,
                    "reader": data_reader,
                }
            )
            return {"combined": 0.72, "sector": "unit", "capital_layer": "shadow"}

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "unit",
        ) -> list[tuple[str, dict[str, object]]]:
            self.calls.append("screening_batch")
            self.score_requests.append(
                {
                    "market": market,
                    "symbol": ",".join(universe),
                    "date": date,
                    "reader": data_reader,
                    "batch": True,
                }
            )
            return [
                (
                    symbol,
                    {"combined": 0.73, "sector": "unit", "capital_layer": "shadow"},
                )
                for symbol in universe
            ]

        def build_pool(
            date: str,
            universe: list[str],
            market: str | None = None,
            reader: object | None = None,
        ) -> dict[str, list[str]]:
            self.calls.append("candidate_pool")
            self.pool_requests.append(
                {
                    "market": market,
                    "date": date,
                    "universe": list(universe),
                    "reader": reader,
                }
            )
            return {
                "candidate": list(universe),
                "watch": [],
                "holdings": [],
                "universe": list(universe),
            }

        def debate(symbol: str, scores: dict[str, object]) -> dict[str, object]:
            self.calls.append("adversarial")
            if fail_debate:
                raise RuntimeError("debate failed")
            return {
                "ts_code": symbol,
                "belief_score": 0.70,
                "bull_case": "ok",
                "bear_case": "risk",
            }

        def risk_check(
            order: dict[str, object], portfolio: dict[str, object]
        ) -> dict[str, object]:
            self.calls.append("risk")
            return {
                "approved": True,
                "adjusted_weight": order["weight"],
                "adjustments": ["ok"],
                "reasons": [],
            }

        def construct(
            orders: list[dict[str, object]], capital: float, method: str, regime: str
        ) -> dict[str, object]:
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
            return {
                "session": session,
                "trade_date": date,
                "capital_layer_reviews": {"shadow": {}},
            }

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
            score_universe=score_universe if use_batch_score else None,
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
        for expected in (
            "screening",
            "candidate_pool",
            "adversarial",
            "risk",
            "position_sizer",
            "portfolio",
            "review",
        ):
            self.assertIn(expected, self.calls)
        self.assertEqual(result["recorded_count"], 2)
        self.assertEqual(
            len(list((self.tmp_path / "signals" / "pending").glob("*.json"))), 0
        )
        self.assertEqual(
            len(
                list((self.tmp_path / "signals" / "shadow" / "pending").glob("*.json"))
            ),
            2,
        )
        self.assertEqual(
            {request["market"] for request in self.score_requests}, {"unit"}
        )
        self.assertEqual(
            {request["market"] for request in self.pool_requests}, {"unit"}
        )

        trade_rows = [
            json.loads(line)
            for line in shadow_broker.SHADOW_TRADES.read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        self.assertEqual(len(trade_rows), 2)
        self.assertEqual({row["capital_layer"] for row in trade_rows}, {"shadow"})

        audit_rows = [
            json.loads(line)
            for line in trade_audit_trail.AUDIT_TRAIL.read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        self.assertEqual(
            {row["stage"] for row in audit_rows},
            {"signal", "decision", "risk", "execution", "result"},
        )
        self.assertTrue(
            all(
                row.get("metadata", {}).get("capital_layer") == "shadow"
                for row in audit_rows
            )
        )

    def test_llm_belief_cannot_change_weight_risk_portfolio_or_order(self) -> None:
        """LLM output is sidecar evidence, never a decision-authority input."""

        def run_with_debate(result: object, suffix: str) -> dict[str, object]:
            captured: dict[str, object] = {}
            audit_sequence = 0

            deps = self._deps()
            deps.debate = lambda symbol, scores: result

            def record_audit_event(**kwargs: object) -> dict[str, object]:
                nonlocal audit_sequence
                audit_sequence += 1
                stage = str(kwargs["stage"])
                return {
                    "audit_id": f"AUDIT-{suffix}-{audit_sequence}",
                    "stage": stage,
                    "ts_code": kwargs.get("ts_code", ""),
                    "parent_audit_id": kwargs.get("parent_audit_id", ""),
                    "payload": kwargs.get(f"{stage}_data", {}),
                }

            def size_position(
                decision_score: float, volatility: float, regime: str
            ) -> float:
                captured["position_score"] = decision_score
                return round(decision_score * 0.10, 6)

            def risk_check(
                order: dict[str, object], portfolio: dict[str, object]
            ) -> dict[str, object]:
                captured["risk_order"] = dict(order)
                return {
                    "approved": True,
                    "adjusted_weight": order["weight"],
                    "adjustments": [],
                    "reasons": [],
                }

            def construct(
                orders: list[dict[str, object]],
                capital: float,
                method: str,
                regime: str,
            ) -> dict[str, object]:
                captured["portfolio_orders"] = [
                    {
                        key: value
                        for key, value in order.items()
                        if key != "risk_audit_id"
                    }
                    for order in orders
                ]
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

            deps.size_position = size_position
            deps.risk_check = risk_check
            deps.construct = construct
            deps.record_audit_event = record_audit_event
            loop_result = run_shadow_loop(
                StubMarketAdapter(),
                "20260630",
                StubReader(),
                deps=deps,
                signals_dir=self.tmp_path / f"signals_{suffix}",
            )
            captured["execution_orders"] = [
                row["order"] for row in loop_result["records"]
            ]
            decision_payloads = [
                row["payload"]
                for row in loop_result["audit_events"]
                if row.get("stage") == "decision"
            ]
            captured["decision_payloads"] = decision_payloads
            return captured

        bearish = run_with_debate(
            {
                "belief_score": 0.0,
                "bull_case": "provider says no upside",
                "bear_case": "provider says maximum risk",
                "target_weight": 0.0,
            },
            "bearish",
        )
        bullish = run_with_debate(
            {
                "belief_score": 1.0,
                "bull_case": "provider says maximum upside",
                "bear_case": "provider says no risk",
                "target_weight": 1.0,
            },
            "bullish",
        )

        for snapshot in (bearish, bullish):
            self.assertAlmostEqual(float(snapshot["position_score"]), 0.72)
            self.assertEqual(snapshot["risk_order"], bearish["risk_order"])
            self.assertEqual(snapshot["portfolio_orders"], bearish["portfolio_orders"])
            self.assertEqual(snapshot["execution_orders"], bearish["execution_orders"])
            serialized_authority_paths = json.dumps(
                {
                    "risk_order": snapshot["risk_order"],
                    "portfolio_orders": snapshot["portfolio_orders"],
                    "execution_orders": snapshot["execution_orders"],
                },
                ensure_ascii=False,
            )
            self.assertNotIn("provider says", serialized_authority_paths)
            self.assertNotIn("target_weight", serialized_authority_paths)

            for payload in snapshot["decision_payloads"]:
                observation = payload["llm_evidence_observation"]
                self.assertEqual(observation["record_type"], "llm_evidence_observation")
                self.assertEqual(observation["status"], "invalid")
                self.assertNotIn("belief_score", observation)

    def test_run_shadow_loop_uses_batch_scoring_when_available(self) -> None:
        result = run_shadow_loop(
            StubMarketAdapter(),
            "20260630",
            StubReader(),
            deps=self._deps(use_batch_score=True),
            signals_dir=self.tmp_path / "signals_batch",
        )

        self.assertEqual(result["state"], "ok")
        self.assertIn("screening.six_dim_batch", result["stage_calls"])
        self.assertIn("screening_batch", self.calls)
        self.assertNotIn("screening", self.calls)
        self.assertEqual(self.score_requests[0]["symbol"], "AAA,BBB")

    def test_run_shadow_loop_deduplicates_same_day_shadow_pending(self) -> None:
        signals_dir = self.tmp_path / "signals_dedup"
        first = run_shadow_loop(
            StubMarketAdapter(),
            "20260630",
            StubReader(),
            deps=self._deps(),
            signals_dir=signals_dir,
        )
        second = run_shadow_loop(
            StubMarketAdapter(),
            "20260630",
            StubReader(),
            deps=self._deps(),
            signals_dir=signals_dir,
        )

        self.assertEqual(first["recorded_count"], 2)
        self.assertEqual(second["recorded_count"], 2)
        self.assertEqual(
            len(list((signals_dir / "shadow" / "pending").glob("*.json"))), 2
        )
        self.assertTrue(
            all(
                record["pending_signal"]["status"] == "duplicate"
                for record in second["records"]
            )
        )

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
                self.assertEqual(
                    {request["market"] for request in self.score_requests}, {market}
                )
                self.assertEqual(
                    {request["market"] for request in self.pool_requests}, {market}
                )

    def test_run_shadow_loop_records_fractional_crypto_quantity(self) -> None:
        class CryptoLikeAdapter(StubMarketAdapter):
            def __init__(self) -> None:
                super().__init__("crypto")

            def get_universe(self, date: str) -> list[str]:
                return ["BTCUSDT"]

            def get_strategy_config(self) -> dict[str, object]:
                return {
                    "shadow_capital": 10000.0,
                    "portfolio_method": "volatility_targeted",
                    "regime": "crypto_24_7",
                    "max_candidates": 1,
                    "default_price": 100000.0,
                    "default_volatility": 0.80,
                    "market_rules": {"lot_size": 0.0001},
                }

        class HighPriceReader:
            def get_bars_daily(
                self, market: str, symbol: str, start: object = None, end: object = None
            ) -> list[dict[str, float]]:
                return [{"close": 100000.0}, {"close": 100000.0}]

        deps = self._deps()
        deps.construct = construct_portfolio

        result = run_shadow_loop(
            CryptoLikeAdapter(),
            "20260630",
            HighPriceReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_crypto_fractional",
        )

        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["recorded_count"], 1)
        trade_rows = [
            json.loads(line)
            for line in shadow_broker.SHADOW_TRADES.read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        self.assertEqual(len(trade_rows), 1)
        self.assertEqual(trade_rows[0]["market"], "crypto")
        self.assertGreater(trade_rows[0]["quantity"], 0)
        self.assertLess(trade_rows[0]["quantity"], 1)


if __name__ == "__main__":
    unittest.main()
