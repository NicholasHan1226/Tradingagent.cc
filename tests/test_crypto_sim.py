from __future__ import annotations

import unittest

from Crypto.adapter import CryptoAdapter
from Crypto.market_data import CryptoMarketData
from Crypto.sim_executor import crypto_sim_execute
from Crypto.simulator import CryptoSimulator
from shared.execution.sim_broker import SimResult, execute_sim_order
from shared.execution.sim_executor_registry import get_sim_executor




class EmptyCryptoMarketData:
    reader = None

    def get_latest_price(self, symbol: str, date: str):
        return None


class FixtureCryptoReader:
    def get_assets(self, market: str):
        assert market == "Crypto"
        return [{"symbol": "BTCUSDT", "status": "trading"}]

    def get_bars_daily(self, market: str, symbol: str, start: str, end: str):
        del start, end
        assert market == "Crypto"
        return [{"symbol": symbol, "close": 50_000}]


def _market_evidence(symbol: str, price: str) -> dict[str, object]:
    return {
        "transport": "fixture",
        "dataset_id": "fixture.crypto.spot_quote",
        "schema_major": 1,
        "symbol": symbol,
        "price": price,
        "metadata": {
            "state": "ready",
            "degraded": False,
            "freshness": "fresh",
            "quality": "pass",
            "lineage": {"fixture": "crypto-sim-v1"},
            "receipt_id": "fixture-receipt-001",
            "observed_at": "2026-07-20T01:00:00Z",
            "data_through": "2026-07-20T01:00:00Z",
            "reasons": [],
        },
        "rules": {
            "quantity_step": "0.000001",
            "min_quantity": "0.000001",
            "min_notional": "5",
            "base_asset": symbol.removesuffix("USDT"),
            "quote_asset": "USDT",
        },
    }


def _mock_market_evidence(symbol: str, price: str) -> dict[str, object]:
    evidence = _market_evidence(symbol, price)
    evidence["transport"] = "mock"
    evidence["dataset_id"] = "mock.crypto.spot_quote"
    return evidence


def _account(**balances: object) -> dict[str, object]:
    return {
        "account_id": "crypto_sim",
        "market": "crypto",
        "broker_contract": "tradingagent.crypto.paper_broker.v1",
        "authority_id": "crypto-shadow-sim-v1",
        "authority_generation": 1,
        "balances": {"USDT": "1000000", "BTC": "10", "ETH": "10", **balances},
    }


class CryptoSimExecutorTest(unittest.TestCase):
    def test_market_data_health_uses_tradingdatas_product_identity(self) -> None:
        health = CryptoMarketData(reader=FixtureCryptoReader()).health_check()

        self.assertTrue(health["ok"])
        self.assertEqual(health["tradingdatas_market_context"], "Crypto")
        self.assertNotIn("sharedsignals_market", health)

    def test_adapter_exposes_dedicated_sim_account(self) -> None:
        adapter = CryptoAdapter()

        self.assertEqual(adapter.get_shadow_account(), "crypto_shadow")
        self.assertEqual(adapter.get_sim_account(), "crypto_sim")

    def test_crypto_sim_execute_uses_provider_neutral_fixture(self) -> None:
        result = crypto_sim_execute(
            order={
                "order_id": "SIM-1",
                "symbol": "BTCUSDT",
                "side": "buy",
                "quantity": 2,
            },
            account=_account(),
            config={"market_evidence": _market_evidence("BTCUSDT", "50000.25")},
        )

        self.assertIsInstance(result, SimResult)
        self.assertEqual(result.status, "filled")
        self.assertEqual(result.filled_qty, 2)
        self.assertEqual(result.avg_price, 50000.25)
        self.assertAlmostEqual(result.fee, 100.0005, places=8)
        self.assertEqual(result.capital_layer, "simulated")
        self.assertEqual(result.account_type, "simulated")
        self.assertEqual(result.market, "crypto")
        self.assertEqual(result.raw_response["source"], "provider_neutral_market_evidence")

    def test_crypto_fractional_quantity_is_preserved_through_shared_dispatch(self) -> None:
        result = execute_sim_order(
            order={
                "order_id": "SIM-FRACTIONAL",
                "symbol": "BTCUSDT",
                "side": "buy",
                "quantity": 0.001,
                "authority_generation": 1,
            },
            market="crypto",
            account=_account(),
            config={"market_evidence": _market_evidence("BTCUSDT", "50000.00")},
        )

        self.assertEqual(result.status, "filled")
        self.assertEqual(result.filled_qty, 0.001)
        self.assertEqual(result.broker_contract, "tradingagent.crypto.paper_broker.v1")
        self.assertEqual(result.authority_id, "crypto-shadow-sim-v1")
        self.assertAlmostEqual(result.fee, 0.05, places=8)

    def test_crypto_sim_execute_accepts_explicit_mock_transport(self) -> None:
        result = crypto_sim_execute(
            order={
                "order_id": "SIM-MOCK",
                "symbol": "BTCUSDT",
                "side": "buy",
                "quantity": "0.001",
            },
            account=_account(),
            config={"market_evidence": _mock_market_evidence("BTCUSDT", "50000")},
        )

        self.assertEqual(result.status, "filled")
        self.assertEqual(result.raw_response["transport"], "mock")

    def test_crypto_sim_execute_rejects_tradingdatas_before_fresh_handoff(self) -> None:
        evidence = _market_evidence("BTCUSDT", "50000")
        evidence["transport"] = "tradingdatas_v1"
        evidence["dataset_id"] = "invented.crypto.spot_quote.v1"

        with self.assertRaisesRegex(
            ValueError, "TradingDatas dataset ID is not frozen for Crypto fills"
        ):
            crypto_sim_execute(
                order={
                    "order_id": "SIM-FORGED-TD",
                    "symbol": "BTCUSDT",
                    "side": "buy",
                    "quantity": "0.001",
                },
                account=_account(),
                config={"market_evidence": evidence},
            )

    def test_crypto_sim_execute_rejects_transport_dataset_prefix_mismatch(self) -> None:
        evidence = _market_evidence("BTCUSDT", "50000")
        evidence["dataset_id"] = "mock.crypto.spot_quote"

        with self.assertRaisesRegex(ValueError, "fixture dataset_id"):
            crypto_sim_execute(
                order={
                    "order_id": "SIM-BAD-FIXTURE-ID",
                    "symbol": "BTCUSDT",
                    "side": "buy",
                    "quantity": "0.001",
                },
                account=_account(),
                config={"market_evidence": evidence},
            )

    def test_crypto_sim_execute_rejects_non_timestamp_metadata(self) -> None:
        for field, value in (
            ("observed_at", "20260720"),
            ("data_through", "2026-07-20T01:00:00"),
        ):
            with self.subTest(field=field):
                evidence = _market_evidence("BTCUSDT", "50000")
                evidence["metadata"] = {**evidence["metadata"], field: value}
                with self.assertRaisesRegex(ValueError, f"metadata.{field}"):
                    crypto_sim_execute(
                        order={
                            "order_id": f"SIM-BAD-{field}",
                            "symbol": "BTCUSDT",
                            "side": "buy",
                            "quantity": "0.001",
                        },
                        account=_account(),
                        config={"market_evidence": evidence},
                    )

    def test_crypto_sim_execute_rejects_empty_lineage(self) -> None:
        for lineage in ({}, [], {"fixture": ""}, {"fixture": False}):
            with self.subTest(lineage=lineage):
                evidence = _market_evidence("BTCUSDT", "50000")
                evidence["metadata"] = {
                    **evidence["metadata"],
                    "lineage": lineage,
                }
                with self.assertRaisesRegex(ValueError, "metadata.lineage"):
                    crypto_sim_execute(
                        order={
                            "order_id": "SIM-EMPTY-LINEAGE",
                            "symbol": "BTCUSDT",
                            "side": "buy",
                            "quantity": "0.001",
                        },
                        account=_account(),
                        config={"market_evidence": evidence},
                    )

    def test_crypto_sim_execute_rejects_data_through_after_observation(self) -> None:
        evidence = _market_evidence("BTCUSDT", "50000")
        evidence["metadata"] = {
            **evidence["metadata"],
            "data_through": "2026-07-20T01:00:01Z",
        }

        with self.assertRaisesRegex(ValueError, "cannot exceed observed_at"):
            crypto_sim_execute(
                order={
                    "order_id": "SIM-FUTURE-DATA",
                    "symbol": "BTCUSDT",
                    "side": "buy",
                    "quantity": "0.001",
                },
                account=_account(),
                config={"market_evidence": evidence},
            )

    def test_registered_crypto_executor_dispatches_via_sim_broker(self) -> None:
        executor = get_sim_executor("crypto")
        self.assertIs(executor, crypto_sim_execute)

        result = execute_sim_order(
            order={
                "order_id": "SIM-2",
                "ts_code": "ETHUSDT",
                "side": "buy",
                "quantity": 3,
                "capital_layer": "real",
                "account_type": "real",
            },
            market="crypto",
            account={**_account(), "account_type": "real"},
            config={
                "market_evidence": _market_evidence("ETHUSDT", "42000.00"),
                "capital_layer": "real",
            },
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.filled_qty, 0)
        self.assertEqual(result.avg_price, 0.0)
        self.assertEqual(result.capital_layer, "simulated")
        self.assertEqual(result.account_type, "simulated")
        self.assertEqual(result.order_id, "SIM-2")
        self.assertIn("real/live execution is rejected", result.message)
    def test_legacy_crypto_simulator_does_not_fallback_to_order_price(self) -> None:
        simulator = CryptoSimulator(market_data=EmptyCryptoMarketData())

        with self.assertRaisesRegex(ValueError, "order-price fallback is retired"):
            simulator.simulate(
                {
                    "order_id": "SIM-CRYPTO-FALLBACK",
                    "symbol": "BTCUSDT",
                    "quantity": 2,
                    "price": 123.45,
                    "trade_date": "20260704",
                },
                {"account_id": "crypto_sim", "account_type": "simulated"},
            )

    def test_crypto_sim_executor_rejects_real_execution_payload_directly(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "real/live execution is rejected"):
            crypto_sim_execute(
                order={
                    "order_id": "SIM-REAL",
                    "symbol": "BTCUSDT",
                    "side": "buy",
                    "quantity": 1,
                    "capital_layer": "real",
                },
                account=_account(),
                config={"market_evidence": _market_evidence("BTCUSDT", "50000")},
            )


if __name__ == "__main__":
    unittest.main()
