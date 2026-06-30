from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Ashare.adapter import AshareAdapter
from Crypto.adapter import CryptoAdapter
from PM.adapter import PMAdapter
from PM.scoring import score_market
from US.adapter import USAdapter
from shared.accounting import position_ledger, trade_audit_trail
from shared.execution import shadow_broker
from shared.orchestrator import _default_deps, run_shadow_loop
from shared.review import benchmark, daily_review
from shared.screening.six_dimension_scorer import score_stock
from shared.wrappers.tradings_cron_entry import get_market_adapter


class Wave2MockReader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.assets: dict[str, list[dict[str, object]]] = {
            "ashare": [
                {
                    "symbol": "600519",
                    "name": "Kweichow Moutai",
                    "exchange": "SSE",
                    "list_date": "20010827",
                    "status": "active",
                }
            ],
            "crypto": [
                {
                    "symbol": "BTCUSDT",
                    "exchange": "BINANCE",
                    "quote_asset": "USDT",
                    "status": "TRADING",
                }
            ],
            "us": [
                {
                    "symbol": "AAPL",
                    "exchange": "NASDAQ",
                    "status": "active",
                    "index_memberships": ["S&P 500", "Nasdaq 100"],
                }
            ],
        }
        self.pm_markets = [
            {
                "market_id": "will-btc-hit-100k",
                "title": "Will BTC hit 100k?",
                "description": "Resolves from public BTC/USD price data.",
                "category": "crypto",
                "status": "ACTIVE",
                "volume": 25000,
                "liquidity": 18000,
                "end_date": "2026-07-15",
                "resolution_source": "Coinbase BTC/USD",
                "model_probability": 0.72,
                "sentiment_score": 0.65,
            }
        ]

    def get_assets(self, market: str) -> list[dict[str, object]]:
        return list(self.assets.get(str(market).lower(), []))

    def get_coverage(self, market: str, date: str) -> list[dict[str, object]]:
        if str(market).lower() != "ashare":
            return []
        return [{"symbol": "600519", "coverage_status": "normal"}]

    def get_regime(self) -> dict[str, object]:
        return {"regime": "growth", "regime_confidence": 1.0}

    def get_events(
        self,
        market: str,
        symbol: str,
        start: object = None,
        end: object = None,
    ) -> list[dict[str, object]]:
        self.calls.append(("events", str(market).lower(), str(symbol)))
        return [{"confidence": 0.8, "direction": "positive"}]

    def get_event_candidates(self) -> list[dict[str, object]]:
        return []

    def get_factors(self, market: str, symbol: str) -> list[dict[str, object]]:
        self.calls.append(("factors", str(market).lower(), str(symbol)))
        return [
            {"factor_name": "value", "event_time": "20260630", "value": 0.75},
            {"factor_name": "growth", "event_time": "20260630", "value": 0.70},
            {"factor_name": "quality", "event_time": "20260630", "value": 0.80},
            {"factor_name": "momentum", "event_time": "20260630", "value": 0.65},
            {"factor_name": "net_mf_amount", "event_time": "20260630", "value": 150000.0},
        ]

    def get_bars_daily(
        self,
        market: str,
        symbol: str,
        start: object = None,
        end: object = None,
    ) -> list[dict[str, object]]:
        market_key = str(market).lower()
        self.calls.append(("bars", market_key, str(symbol)))
        if market_key == "pm":
            return [{"trade_date": "20260630", "close": 0.60}]
        closes = [100.0 + idx for idx in range(25)]
        return [
            {
                "trade_date": f"202606{idx + 1:02d}",
                "close": close,
                "amount": 90_000_000,
            }
            for idx, close in enumerate(closes)
        ]

    def get_sentiment(self) -> list[dict[str, object]]:
        return []

    def get_pm_markets(self, active_only: bool = True) -> list[dict[str, object]]:
        if not active_only:
            return list(self.pm_markets)
        return [row for row in self.pm_markets if row.get("status") == "ACTIVE"]

    def get_pm_universe(self) -> list[str]:
        return [str(row["market_id"]) for row in self.get_pm_markets(active_only=True)]

    def get_pm_prices(
        self,
        market_id: str,
        start: object = None,
        end: object = None,
    ) -> list[dict[str, object]]:
        return [
            {
                "market_id": market_id,
                "timestamp": "2026-06-30T10:00:00",
                "last_price": 0.60,
                "bid_ask_spread": 0.015,
                "model_probability": 0.72,
                "sentiment_score": 0.65,
            }
        ]


def _patch_shadow_paths(testcase: unittest.TestCase, tmp_path: Path) -> None:
    shadow_dir = tmp_path / "shared" / "logs" / "shadow"
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
    patcher = patch.object(daily_review, "SHADOW_TRADES_LOG", shadow_dir / "shadow_trades.jsonl")
    patcher.start()
    testcase.addCleanup(patcher.stop)


def _patch_audit_paths(testcase: unittest.TestCase, tmp_path: Path) -> None:
    ledger_dir = tmp_path / "shared" / "logs"
    for name, value in (
        ("LEDGER_DIR", ledger_dir),
        ("AUDIT_TRAIL", ledger_dir / "trade_audit_trail.jsonl"),
    ):
        patcher = patch.object(trade_audit_trail, name, value)
        patcher.start()
        testcase.addCleanup(patcher.stop)


def _patch_review_paths(testcase: unittest.TestCase, tmp_path: Path) -> None:
    review_dir = tmp_path / "shared" / "review" / "data"
    ledger_dir = tmp_path / "shared" / "logs"
    filled_dir = tmp_path / "signals" / "filled"
    for module, name, value in (
        (daily_review, "DAILY_LOG", review_dir / "daily_reviews.jsonl"),
        (daily_review, "FILLED_SIGNALS_DIR", filled_dir),
        (benchmark, "LAST_PERIOD_STORE", review_dir / "last_period_return.json"),
        (benchmark, "BENCHMARK_STORE", review_dir / "benchmark_history.json"),
        (position_ledger, "LEDGER_DIR", ledger_dir),
        (position_ledger, "POSITION_CSV", ledger_dir / "position_ledger.csv"),
        (position_ledger, "POSITION_LOCK", ledger_dir / "position_ledger.csv.lock"),
    ):
        patcher = patch.object(module, name, value)
        patcher.start()
        testcase.addCleanup(patcher.stop)


class Wave2IntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp_path = Path(self.tmpdir.name)
        _patch_shadow_paths(self, self.tmp_path)
        _patch_audit_paths(self, self.tmp_path)
        _patch_review_paths(self, self.tmp_path)

    def _deps_for_market(self, market: str):
        deps = _default_deps()
        deps.build_pool = lambda date, universe: {
            "candidate": list(universe),
            "watch": [],
            "holdings": [],
            "universe": list(universe),
        }
        deps.debate = lambda symbol, scores: {
            "ts_code": symbol,
            "belief_score": 0.70,
            "bull_case": "mock bull",
            "bear_case": "mock bear",
            "key_risk": "mock risk",
        }
        if market == "pm":
            deps.score_stock = lambda symbol, date, data_reader=None: score_market(
                symbol,
                date,
                data_reader=data_reader,
            )
        else:
            deps.score_stock = lambda symbol, date, data_reader=None, market=market: score_stock(
                market,
                symbol,
                data_reader,
                date,
            )
        return deps

    def test_cron_registry_points_four_markets_to_real_adapters(self) -> None:
        expected = {
            "Ashare": "AshareAdapter",
            "Crypto": "CryptoAdapter",
            "US": "USAdapter",
            "PM": "PMAdapter",
        }
        for market, class_name in expected.items():
            adapter = get_market_adapter(market)
            self.assertEqual(type(adapter).__name__, class_name)
            self.assertNotIn("Stub", type(adapter).__name__)

    def test_ashare_us_real_adapters_complete_shadow_loop(self) -> None:
        reader = Wave2MockReader()
        cases = [
            ("ashare", AshareAdapter(reader=reader), "600519"),
            ("us", USAdapter(reader=reader), "AAPL"),
        ]

        for market, adapter, symbol in cases:
            with self.subTest(market=market):
                result = run_shadow_loop(
                    adapter,
                    "20260630",
                    reader,
                    deps=self._deps_for_market(market),
                    signals_dir=self.tmp_path / "signals",
                )
                self.assertEqual(result["state"], "ok")
                self.assertEqual(result["capital_layer"], "shadow")
                self.assertEqual(result["recorded_count"], 1)
                self.assertEqual(result["records"][0]["symbol"], symbol)
                self.assertEqual(result["review"]["capital_layer"], "shadow")
                self.assertIn("review.daily_review", result["stage_calls"])

        trades = [
            json.loads(line)
            for line in shadow_broker.SHADOW_TRADES.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(trades), 2)
        self.assertEqual({row["capital_layer"] for row in trades}, {"shadow"})
        self.assertEqual(
            {row["strategy_name"] for row in trades},
            {"ashare_shadow", "us_shadow"},
        )

        audit_rows = [
            json.loads(line)
            for line in trade_audit_trail.AUDIT_TRAIL.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertTrue(audit_rows)
        self.assertTrue(
            all(row.get("metadata", {}).get("capital_layer") == "shadow" for row in audit_rows)
        )
        self.assertTrue((self.tmp_path / "signals" / "pending").exists())
        self.assertGreaterEqual(len(list((self.tmp_path / "signals" / "pending").glob("*.json"))), 2)

    def test_crypto_pm_complete_shadow_loop_with_market_aware_scoring(self) -> None:
        cases = [
            ("crypto", CryptoAdapter, ("bars", "crypto", "BTCUSDT")),
            ("pm", PMAdapter, ("bars", "pm", "will-btc-hit-100k")),
        ]

        for market, adapter_cls, expected_call in cases:
            with self.subTest(market=market):
                reader = Wave2MockReader()
                result = run_shadow_loop(
                    adapter_cls(reader=reader),
                    "20260630",
                    reader,
                    deps=self._deps_for_market(market),
                    signals_dir=self.tmp_path / "signals",
                )

                self.assertEqual(result["market"], market)
                self.assertEqual(result["capital_layer"], "shadow")
                self.assertEqual(result["state"], "ok")
                self.assertEqual(result["recorded_count"], 1)
                self.assertIn(expected_call, reader.calls)
                self.assertEqual(result["records"][0]["symbol"], expected_call[2])
                self.assertEqual(result["review"]["capital_layer"], "shadow")

    def test_daily_review_reads_four_market_shadow_trades_and_keeps_market_breakdown(self) -> None:
        shadow_broker.SHADOW_DIR.mkdir(parents=True, exist_ok=True)
        for market, strategy, symbol in (
            ("ashare", "ashare_shadow", "600519"),
            ("crypto", "crypto_shadow", "BTCUSDT"),
            ("us", "us_shadow", "AAPL"),
            ("pm", "pm_shadow", "will-btc-hit-100k"),
        ):
            with shadow_broker.SHADOW_TRADES.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "trade_id": f"SHADOW-{market}",
                            "trade_date": "2026-06-30",
                            "market": market,
                            "strategy_name": strategy,
                            "ts_code": symbol,
                            "side": "buy",
                            "quantity": 1,
                            "price": 10.0,
                            "capital_layer": "shadow",
                            "created_at": "2026-06-30T10:30:00",
                            "pnl": 0.01,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

        review = daily_review.run_daily_review("20260630", session="close")

        self.assertEqual(review["capital_layer"], "shadow")
        self.assertFalse(review["stale"])
        self.assertEqual(review["capital_layer_reviews"]["shadow"]["trades_summary"]["count"], 4)
        self.assertIn("market_reviews", review)
        self.assertEqual(set(review["market_reviews"]), {"ashare", "crypto", "pm", "us"})


if __name__ == "__main__":
    unittest.main()
