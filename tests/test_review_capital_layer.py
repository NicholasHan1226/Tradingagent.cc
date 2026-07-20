import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shared.review import benchmark
from shared.review import daily_review, monthly_review, weekly_review


class ReviewCapitalLayerTest(unittest.TestCase):
    def test_daily_close_groups_pnl_by_capital_layer_and_normalizes_paper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            daily_review.DAILY_LOG = tmpdir / "daily_reviews.jsonl"
            benchmark.LAST_PERIOD_STORE = tmpdir / "last_period_return.json"
            benchmark.BENCHMARK_STORE = tmpdir / "benchmark_history.json"

            trades = [
                {
                    "ts_code": "BTCUSDT",
                    "market": "crypto",
                    "pnl": 100.0,
                    "capital_layer": "real",
                    "account_scope": "crypto-live",
                    "strategy": "trend",
                },
                {
                    "ts_code": "ETHUSDT",
                    "market": "crypto",
                    "pnl": 50.0,
                    "capital_layer": "paper",
                    "account_scope": "crypto-paper",
                    "strategy": "pullback",
                },
                {
                    "ts_code": "SOLUSDT",
                    "market": "crypto",
                    "pnl": -20.0,
                    "capital_layer": "sim",
                    "account_scope": "crypto-sim",
                    "strategy": "event",
                },
            ]
            positions = [
                {
                    "ts_code": "BTCUSDT",
                    "market": "crypto",
                    "weight": 0.5,
                    "pnl_pct": 0.02,
                    "capital_layer": "real",
                    "account_scope": "crypto-live",
                },
                {
                    "ts_code": "ETHUSDT",
                    "market": "crypto",
                    "weight": 0.2,
                    "pnl_pct": 0.05,
                    "capital_layer": "paper",
                    "account_scope": "crypto-paper",
                },
                {
                    "ts_code": "SOLUSDT",
                    "market": "crypto",
                    "weight": 0.1,
                    "pnl_pct": -0.10,
                    "capital_layer": "simulated",
                    "account_scope": "crypto-sim",
                },
            ]

            result = daily_review.review_close(trades, positions, benchmark_return=0.0)

            self.assertEqual(
                set(result["capital_layer_reviews"]), {"real", "shadow", "simulated"}
            )
            self.assertNotIn("pnl", result["capital_layer_reviews"]["real"])
            self.assertEqual(
                result["capital_layer_reviews"]["real"]["monetary_aggregation"],
                "forbidden",
            )
            self.assertAlmostEqual(
                result["capital_layer_reviews"]["real"]["market_reviews"]["crypto"][
                    "account_reviews"
                ]["crypto-live"]["pnl"],
                100.01,
            )
            self.assertEqual(
                result["capital_layer_reviews"]["real"]["market_reviews"]["crypto"][
                    "account_reviews"
                ]["crypto-live"]["account_scope"],
                "crypto-live",
            )
            self.assertAlmostEqual(
                result["capital_layer_reviews"]["shadow"]["market_reviews"]["crypto"][
                    "account_reviews"
                ]["crypto-paper"]["pnl"],
                50.01,
            )
            self.assertAlmostEqual(
                result["capital_layer_reviews"]["simulated"]["market_reviews"][
                    "crypto"
                ]["account_reviews"]["crypto-sim"]["pnl"],
                -20.01,
            )

            rows = [
                json.loads(line)
                for line in daily_review.DAILY_LOG.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(
                {row["capital_layer"] for row in rows}, {"real", "shadow", "simulated"}
            )
            self.assertTrue(all("capital_layer" in row for row in rows))

    def test_weekly_review_separates_strategy_stats_by_capital_layer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            weekly_review.WEEKLY_LOG = tmpdir / "weekly_reviews.jsonl"
            weekly_review.WEEKLY_STATE = tmpdir / "weekly_state.json"

            trades = [
                {
                    "market": "ashare",
                    "pnl": 10.0,
                    "strategy": "trend",
                    "capital_layer": "real",
                    "account_scope": "ashare-real",
                    "dimension": "technical",
                    "condition": "low_vol",
                },
                {
                    "market": "ashare",
                    "pnl": -5.0,
                    "strategy": "trend",
                    "capital_layer": "real",
                    "account_scope": "ashare-real",
                    "dimension": "technical",
                    "condition": "low_vol",
                },
                {
                    "market": "ashare",
                    "pnl": 7.0,
                    "strategy": "trend",
                    "capital_layer": "paper",
                    "account_scope": "ashare-paper",
                    "dimension": "macro",
                    "condition": "mid_vol",
                },
            ]

            result = weekly_review.review_week(trades, strategies=["trend"])

            ashare = result["market_reviews"]["ashare"]
            self.assertEqual(
                ashare["capital_layer_reviews"]["real"]["week_trade_count"], 2
            )
            self.assertEqual(
                ashare["capital_layer_reviews"]["shadow"]["week_trade_count"], 1
            )
            self.assertAlmostEqual(
                ashare["capital_layer_reviews"]["real"]["account_reviews"][
                    "ashare-real"
                ]["week_pnl"],
                5.0,
            )
            self.assertAlmostEqual(
                ashare["capital_layer_reviews"]["shadow"]["account_reviews"][
                    "ashare-paper"
                ]["week_pnl"],
                7.0,
            )
            self.assertEqual(ashare["currency"], "CNY")
            self.assertNotIn("week_pnl", result["all_markets"])

            rows = [
                json.loads(line)
                for line in weekly_review.WEEKLY_LOG.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual({row["capital_layer"] for row in rows}, {"real", "shadow"})
            self.assertEqual({row["market"] for row in rows}, {"ashare"})
            self.assertTrue(all(row["currency"] == "CNY" for row in rows))
            self.assertEqual(
                rows[0]["strategy_win_rates"]["trend"]["trades"]
                + rows[1]["strategy_win_rates"]["trend"]["trades"],
                3,
            )

    def test_weekly_review_excludes_after_hours_ashare_sim_strategy_sample(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            weekly_review.WEEKLY_LOG = tmpdir / "weekly_reviews.jsonl"
            weekly_review.WEEKLY_STATE = tmpdir / "weekly_state.json"

            trades = [
                {
                    "market": "ashare",
                    "capital_layer": "simulated",
                    "account_scope": "ashare_sim",
                    "side": "buy",
                    "strategy": "trend",
                    "pnl": 10.0,
                    "candidate_pool_layer": "candidate",
                    "execution_source": "ashare_candidate_layer",
                    "created_at": "2026-07-07T08:26:30+00:00",
                }
            ]

            result = weekly_review.review_week(trades, strategies=["trend"])

            simulated = result["market_reviews"]["ashare"]["capital_layer_reviews"][
                "simulated"
            ]
            self.assertEqual(simulated["week_trade_count"], 0)
            account = simulated["account_reviews"]["ashare_sim"]
            self.assertEqual(account["strategy_win_rates"]["trend"]["trades"], 0)
            self.assertEqual(account["week_pnl"], 0)

    def test_weekly_review_never_aggregates_cny_and_usdt_ledgers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            weekly_review.WEEKLY_LOG = tmpdir / "weekly_reviews.jsonl"
            weekly_review.WEEKLY_STATE = tmpdir / "weekly_state.json"

            ledger = {
                "ashare": {
                    "account_summaries": {
                        "ashare_sim": {
                            "realized_pnl": 100.0,
                            "unrealized_pnl": 10.0,
                            "total_pnl": 110.0,
                            "market_value": 1_000.0,
                            "open_position_count": 1,
                            "missing_mark_count": 0,
                            "pnl_source": "ashare_local_sim_mark_to_market",
                            "mark_authority": "fixture_ashare_mark",
                        }
                    },
                },
                "crypto": {
                    "account_summaries": {
                        "crypto:simulated:grid": {
                            "realized_pnl": 20.0,
                            "unrealized_pnl": 2.0,
                            "total_pnl": 22.0,
                            "market_value": 200.0,
                            "open_position_count": 2,
                            "missing_mark_count": 1,
                            "pnl_source": "crypto_sim_ledger",
                            "mark_authority": "fixture_crypto_mark",
                        }
                    },
                },
            }
            with mock.patch.object(
                weekly_review, "sim_ledger_pnl_summary", return_value=ledger
            ):
                result = weekly_review.review_week(
                    [
                        {
                            "market": "us",
                            "capital_layer": "simulated",
                            "pnl": 999.0,
                        }
                    ]
                )

            self.assertNotIn("ledger_total_pnl", result["all_markets"])
            self.assertNotIn("ledger_market_value", result["all_markets"])
            self.assertEqual(result["all_markets"]["week_trade_count"], 0)
            self.assertEqual(result["all_markets"]["open_position_count"], 3)
            self.assertEqual(result["all_markets"]["monetary_aggregation"], "forbidden")
            self.assertEqual(result["ledger_by_market"]["ashare"]["currency"], "CNY")
            self.assertEqual(result["ledger_by_market"]["crypto"]["currency"], "USDT")
            self.assertIsNone(result["ledger_by_market"]["crypto"]["account_scope"])
            self.assertNotIn("total_pnl", result["ledger_by_market"]["ashare"])
            self.assertEqual(
                result["ledger_by_market"]["ashare"]["account_summaries"]["ashare_sim"][
                    "total_pnl"
                ],
                110.0,
            )
            self.assertEqual(
                result["ledger_by_market"]["crypto"]["account_summaries"][
                    "crypto:simulated:grid"
                ]["total_pnl"],
                22.0,
            )
            self.assertEqual(
                result["ledger_by_market"]["ashare"]["account_summaries"]["ashare_sim"][
                    "mark_authority"
                ],
                "fixture_ashare_mark",
            )

    def test_monthly_review_keeps_real_and_shadow_reports_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            monthly_review.MONTHLY_LOG = tmpdir / "monthly_reviews.jsonl"

            month_data = {
                "month": "2026-06",
                "trades": [
                    {
                        "market": "ashare",
                        "pnl": 0.10,
                        "capital_layer": "real",
                        "account_scope": "ashare-real",
                        "dimension": "technical",
                        "strategy": "trend",
                        "condition": "breakout",
                    },
                    {
                        "market": "ashare",
                        "pnl": -0.03,
                        "capital_layer": "paper",
                        "account_scope": "ashare-paper",
                        "dimension": "event",
                        "strategy": "event_driven",
                        "condition": "high_vol",
                    },
                ],
                "pipeline": {
                    "screening": {"runs": 1, "errors": 0},
                    "adversarial": {"runs": 1, "errors": 0},
                    "risk": {"runs": 1, "errors": 0},
                    "portfolio": {"runs": 1, "errors": 0},
                    "execution": {"runs": 1, "errors": 0},
                    "review": {"runs": 1, "errors": 0},
                    "accounting": {"runs": 1, "errors": 0},
                },
            }

            result = monthly_review.review_month(month_data)

            ashare = result["market_reviews"]["ashare"]
            self.assertAlmostEqual(
                ashare["capital_layer_reviews"]["real"]["account_reviews"][
                    "ashare-real"
                ]["month_pnl"],
                0.10,
            )
            self.assertAlmostEqual(
                ashare["capital_layer_reviews"]["shadow"]["account_reviews"][
                    "ashare-paper"
                ]["month_pnl"],
                -0.03,
            )
            self.assertEqual(
                ashare["capital_layer_reviews"]["shadow"]["capital_layer"], "shadow"
            )
            self.assertEqual(ashare["currency"], "CNY")
            self.assertNotIn("month_pnl", result["all_markets"])
            self.assertNotIn("monthly_return", result["all_markets"])

            rows = [
                json.loads(line)
                for line in monthly_review.MONTHLY_LOG.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual({row["capital_layer"] for row in rows}, {"real", "shadow"})
            self.assertEqual({row["market"] for row in rows}, {"ashare"})
            self.assertTrue(all(row["currency"] == "CNY" for row in rows))
            self.assertTrue(all("capital_layer" in row for row in rows))

    def test_monthly_review_never_aggregates_cny_and_usdt(self) -> None:
        month_data = {
            "month": "2026-07",
            "trades": [
                {
                    "market": "ashare",
                    "capital_layer": "simulated",
                    "account_scope": "ashare_sim",
                    "pnl": 100.0,
                },
                {
                    "market": "crypto",
                    "capital_layer": "simulated",
                    "account_scope": "crypto:simulated:grid",
                    "pnl": 20.0,
                },
                {"market": "us", "capital_layer": "simulated", "pnl": 999.0},
            ],
            "pipeline": {},
            "monthly_return": 9.99,
        }

        with tempfile.TemporaryDirectory() as tmp:
            monthly_review.MONTHLY_LOG = Path(tmp) / "monthly_reviews.jsonl"
            result = monthly_review.review_month(month_data)

        self.assertEqual(result["all_markets"]["month_trade_count"], 2)
        self.assertEqual(result["all_markets"]["monetary_aggregation"], "forbidden")
        self.assertNotIn("month_pnl", result["all_markets"])
        self.assertNotIn("monthly_return", result["all_markets"])
        self.assertEqual(
            result["market_reviews"]["ashare"]["capital_layer_reviews"]["simulated"][
                "account_reviews"
            ]["ashare_sim"]["month_pnl"],
            100.0,
        )
        self.assertEqual(
            result["market_reviews"]["crypto"]["capital_layer_reviews"]["simulated"][
                "account_reviews"
            ]["crypto:simulated:grid"]["month_pnl"],
            20.0,
        )
        self.assertEqual(result["market_reviews"]["crypto"]["currency"], "USDT")
        self.assertEqual(
            result["market_reviews"]["crypto"]["capital_layer_reviews"]["simulated"][
                "account_reviews"
            ]["crypto:simulated:grid"]["account_scope"],
            "crypto:simulated:grid",
        )
        self.assertIsNone(
            result["market_reviews"]["ashare"]["capital_layer_reviews"]["simulated"][
                "account_reviews"
            ]["ashare_sim"]["monthly_return"]
        )

    def test_daily_same_market_layer_accounts_and_unscoped_never_mix_money(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daily_review.DAILY_LOG = Path(tmp) / "daily_reviews.jsonl"
            trades = [
                {
                    "market": "crypto",
                    "capital_layer": "simulated",
                    "account_scope": "crypto:simulated:grid",
                    "pnl": 10.0,
                    "strategy": "grid",
                },
                {
                    "market": "crypto",
                    "capital_layer": "simulated",
                    "account_scope": "crypto:simulated:momentum",
                    "pnl": -3.0,
                    "strategy": "momentum",
                },
                {
                    "market": "crypto",
                    "capital_layer": "simulated",
                    "pnl": 999.0,
                    "strategy": "unknown",
                },
            ]
            with mock.patch.object(
                daily_review,
                "sim_ledger_pnl_summary",
                return_value={"crypto": {"account_summaries": {}}},
            ):
                result = daily_review.review_close(trades, [], benchmark_return=None)

        market = result["capital_layer_reviews"]["simulated"]["market_reviews"][
            "crypto"
        ]
        self.assertNotIn("pnl", market)
        self.assertNotIn("attribution", market)
        self.assertNotIn("comparisons", market)
        self.assertEqual(
            market["account_reviews"]["crypto:simulated:grid"]["pnl"], 10.0
        )
        self.assertEqual(
            market["account_reviews"]["crypto:simulated:momentum"]["pnl"], -3.0
        )
        unscoped = market["account_reviews"][daily_review.UNSCOPED_ACCOUNT_KEY]
        self.assertEqual(unscoped["trades"], 1)
        self.assertEqual(unscoped["review_state"], "count_only")
        for field in ("pnl", "trades_summary", "attribution", "comparisons"):
            self.assertNotIn(field, unscoped)

    def test_weekly_same_market_layer_accounts_and_unscoped_never_mix_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            weekly_review.WEEKLY_LOG = root / "weekly_reviews.jsonl"
            weekly_review.WEEKLY_STATE = root / "weekly_state.json"
            with mock.patch.object(
                weekly_review,
                "sim_ledger_pnl_summary",
                return_value={"crypto": {"account_summaries": {}}},
            ):
                result = weekly_review.review_week(
                    [
                        {
                            "market": "crypto",
                            "capital_layer": "simulated",
                            "account_scope": "crypto:simulated:grid",
                            "strategy": "trend",
                            "pnl": 8.0,
                        },
                        {
                            "market": "crypto",
                            "capital_layer": "simulated",
                            "account_scope": "crypto:simulated:momentum",
                            "strategy": "trend",
                            "pnl": -2.0,
                        },
                        {
                            "market": "crypto",
                            "capital_layer": "simulated",
                            "strategy": "trend",
                            "pnl": 777.0,
                        },
                    ]
                )

            layer = result["market_reviews"]["crypto"]["capital_layer_reviews"][
                "simulated"
            ]
            self.assertNotIn("week_pnl", layer)
            self.assertEqual(
                layer["account_reviews"]["crypto:simulated:grid"]["week_pnl"],
                8.0,
            )
            self.assertEqual(
                layer["account_reviews"]["crypto:simulated:momentum"]["week_pnl"],
                -2.0,
            )
            unscoped = layer["account_reviews"][weekly_review.UNSCOPED_ACCOUNT_KEY]
            self.assertNotIn("week_pnl", unscoped)
            self.assertNotIn("dimension_effectiveness", unscoped)
            self.assertNotIn("wins", unscoped)
            self.assertNotIn("week_win_rate", unscoped)
            self.assertNotIn("wins", layer)
            self.assertNotIn("week_win_rate", layer)
            self.assertNotIn("wins", result["capital_layer_reviews"]["simulated"])
            self.assertNotIn(
                "week_win_rate", result["capital_layer_reviews"]["simulated"]
            )
            self.assertIn(
                "week_win_rate",
                layer["account_reviews"]["crypto:simulated:grid"],
            )
            state = json.loads(weekly_review.WEEKLY_STATE.read_text(encoding="utf-8"))
            strategies = state["strategies"]
            self.assertIn("crypto:simulated:crypto:simulated:grid:trend", strategies)
            self.assertIn(
                "crypto:simulated:crypto:simulated:momentum:trend", strategies
            )
            self.assertTrue(all("__unscoped__" not in key for key in strategies))

    def test_monthly_same_market_layer_accounts_require_account_metrics(
        self,
    ) -> None:
        month_data = {
            "month": "2026-07",
            "trades": [
                {
                    "market": "crypto",
                    "capital_layer": "simulated",
                    "account_scope": "crypto:simulated:grid",
                    "pnl": 4.0,
                },
                {
                    "market": "crypto",
                    "capital_layer": "simulated",
                    "account_scope": "crypto:simulated:momentum",
                    "pnl": -1.0,
                },
                {
                    "market": "crypto",
                    "capital_layer": "simulated",
                    "pnl": 888.0,
                },
            ],
            "market_metrics": {
                "crypto": {
                    "simulated": {
                        "crypto:simulated:grid": {"monthly_return": 0.04},
                        "crypto:simulated:momentum": {"monthly_return": -0.01},
                    }
                }
            },
            "pipeline": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            monthly_review.MONTHLY_LOG = Path(tmp) / "monthly_reviews.jsonl"
            result = monthly_review.review_month(month_data)

        layer = result["market_reviews"]["crypto"]["capital_layer_reviews"]["simulated"]
        self.assertNotIn("month_pnl", layer)
        self.assertNotIn("monthly_return", layer)
        self.assertEqual(
            layer["account_reviews"]["crypto:simulated:grid"]["month_pnl"], 4.0
        )
        self.assertEqual(
            layer["account_reviews"]["crypto:simulated:grid"]["monthly_return"],
            0.04,
        )
        self.assertEqual(
            layer["account_reviews"]["crypto:simulated:momentum"]["monthly_return"],
            -0.01,
        )
        unscoped = layer["account_reviews"][monthly_review.UNSCOPED_ACCOUNT_KEY]
        for field in (
            "month_pnl",
            "monthly_return",
            "memory_consolidation",
            "goal_achievement",
        ):
            self.assertNotIn(field, unscoped)


if __name__ == "__main__":
    unittest.main()
