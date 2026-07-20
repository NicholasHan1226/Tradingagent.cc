from __future__ import annotations

import unittest

from Crypto.adapter import CryptoAdapter
from Crypto.capital_policy import CRYPTO_CAPITAL_AUTHORITY_ID
from Crypto.market_data import CryptoMarketData
from Crypto.sim_executor import (
    PAPER_BROKER_CONTRACT,
    CryptoLegacyExecutionRetired,
    crypto_sim_execute,
)
from Crypto.simulator import CryptoSimulator
from shared.execution import sim_executor_registry
from shared.execution.sim_broker import SimResult, execute_sim_order, simulate_order


class EmptyCryptoMarketData:
    reader = None

    def get_latest_price(self, symbol: str, date: str):
        del symbol, date
        return None


class FixtureCryptoReader:
    def get_assets(self, market: str):
        assert market == "Crypto"
        return [{"symbol": "BTCUSDT", "status": "trading"}]

    def get_bars_daily(self, market: str, symbol: str, start: str, end: str):
        del start, end
        assert market == "Crypto"
        return [{"symbol": symbol, "close": 50_000}]


def _account() -> dict[str, object]:
    return {
        "account_id": "crypto_sim",
        "market": "crypto",
        "broker_contract": PAPER_BROKER_CONTRACT,
        "authority_id": CRYPTO_CAPITAL_AUTHORITY_ID,
        "authority_generation": 1,
    }


class CryptoSimExecutorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._old_registry = dict(sim_executor_registry._SIM_EXECUTORS)
        sim_executor_registry._SIM_EXECUTORS.clear()

    def tearDown(self) -> None:
        sim_executor_registry._SIM_EXECUTORS.clear()
        sim_executor_registry._SIM_EXECUTORS.update(self._old_registry)

    def test_market_data_health_uses_tradingdatas_product_identity(self) -> None:
        health = CryptoMarketData(reader=FixtureCryptoReader()).health_check()

        self.assertTrue(health["ok"])
        self.assertEqual(health["tradingdatas_market_context"], "Crypto")
        self.assertNotIn("sharedsignals_market", health)

    def test_adapter_exposes_dedicated_sim_account(self) -> None:
        adapter = CryptoAdapter()

        self.assertEqual(adapter.get_shadow_account(), "crypto_shadow")
        self.assertEqual(adapter.get_sim_account(), "crypto_sim")

    def test_legacy_crypto_direct_executor_always_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            CryptoLegacyExecutionRetired, CRYPTO_CAPITAL_AUTHORITY_ID
        ):
            crypto_sim_execute(
                order={
                    "order_id": "RETIRED-DIRECT",
                    "symbol": "BTCUSDT",
                    "side": "buy",
                    "quantity": "0.001",
                },
                account=_account(),
                config={"market_evidence": {"transport": "fixture"}},
            )

    def test_legacy_crypto_direct_executor_rejects_real_markers_first(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "real/live execution is rejected"):
            crypto_sim_execute(
                order={
                    "order_id": "RETIRED-REAL",
                    "symbol": "BTCUSDT",
                    "capital_layer": "real",
                },
                account=_account(),
                config={},
            )

    def test_importing_legacy_executor_does_not_register_crypto(self) -> None:
        self.assertIsNone(sim_executor_registry.get_sim_executor("crypto"))
        self.assertIsNone(sim_executor_registry.get_sim_executor_binding("crypto"))

    def test_registry_rejects_generic_crypto_executor_registration(self) -> None:
        calls: list[object] = []

        def unsafe_stub(order, account, config) -> SimResult:
            calls.append((order, account, config))
            return SimResult(
                status="filled",
                filled_qty=1,
                avg_price=1,
                market="crypto",
                broker_contract=PAPER_BROKER_CONTRACT,
                authority_id=CRYPTO_CAPITAL_AUTHORITY_ID,
            )

        with self.assertRaisesRegex(ValueError, "registration disabled"):
            sim_executor_registry.register_sim_executor(
                "crypto",
                unsafe_stub,
                simulation_contract=PAPER_BROKER_CONTRACT,
                authority_id=CRYPTO_CAPITAL_AUTHORITY_ID,
            )

        result = execute_sim_order(
            order={"order_id": "NO-GENERAL-FILL", "authority_generation": 1},
            market="crypto",
            account=_account(),
            config={},
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.filled_qty, 0)
        self.assertEqual(
            result.raw_response["reason"], "crypto_general_executor_retired"
        )
        self.assertEqual(calls, [])

    def test_non_crypto_asset_pair_fields_do_not_change_domestic_dispatch(self) -> None:
        for market in ("ashare", "cn_futures"):
            with self.subTest(market=market):
                result = simulate_order(
                    {
                        "order_id": f"NON-CRYPTO-{market}",
                        "market": market,
                        "base_asset": "LOCAL_SECURITY",
                        "quote_asset": "CNY",
                        "quantity": 1,
                    }
                )
                self.assertEqual(result["status"], "rejected")
                self.assertEqual(result["message"], "Missing mid_price and limit_price")
                self.assertNotIn("crypto", str(result.get("reason", "")))

    def test_shared_dispatch_rejects_crypto_instruments_mislabeled_as_other_markets(
        self,
    ) -> None:
        cases = (
            ("ashare", {"order_id": "MISLABEL-A", "ts_code": "BTCUSDT"}),
            ("cn_futures", {"order_id": "MISLABEL-F", "symbol": "ETHUSDT"}),
        )

        for market, order in cases:
            with self.subTest(market=market):
                result = execute_sim_order(order=order, market=market)
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.filled_qty, 0)
                self.assertEqual(
                    result.raw_response["reason"],
                    "crypto_market_binding_conflict",
                )

    def test_generic_slippage_simulator_rejects_crypto_without_fill(self) -> None:
        orders = (
            (
                {
                    "order_id": "NO-LEGACY-SLIPPAGE-MARKET",
                    "market": "crypto",
                    "mid_price": 50_000,
                    "quantity": 1,
                },
                "crypto_general_executor_retired",
            ),
            (
                {
                    "order_id": "NO-LEGACY-SLIPPAGE-SYMBOL",
                    "symbol": "BTCUSDT",
                    "mid_price": 50_000,
                    "quantity": 1,
                },
                "crypto_general_executor_retired",
            ),
            (
                {
                    "order_id": "NO-LEGACY-SLIPPAGE-ASHARE-MISLABEL",
                    "market": "ashare",
                    "ts_code": "BTCUSDT",
                    "mid_price": 50_000,
                    "quantity": 1,
                },
                "crypto_market_binding_conflict",
            ),
            (
                {
                    "order_id": "NO-LEGACY-SLIPPAGE-CNFUTURES-MISLABEL",
                    "market": "cn_futures",
                    "symbol": "ETHUSDT",
                    "mid_price": 50_000,
                    "quantity": 1,
                },
                "crypto_market_binding_conflict",
            ),
        )

        for order, expected_reason in orders:
            with self.subTest(order_id=order["order_id"]):
                result = simulate_order(order)
                self.assertEqual(result["status"], "rejected")
                self.assertEqual(result["filled_quantity"], 0)
                self.assertEqual(result["reason"], expected_reason)

    def test_legacy_crypto_simulator_never_uses_market_or_order_price(self) -> None:
        simulator = CryptoSimulator(market_data=EmptyCryptoMarketData())

        with self.assertRaisesRegex(
            CryptoLegacyExecutionRetired, CRYPTO_CAPITAL_AUTHORITY_ID
        ):
            simulator.simulate(
                {
                    "order_id": "NO-LEGACY-SIMULATOR",
                    "symbol": "BTCUSDT",
                    "quantity": 2,
                    "price": 123.45,
                    "trade_date": "20260704",
                },
                {"account_id": "crypto_sim", "account_type": "simulated"},
            )


if __name__ == "__main__":
    unittest.main()
