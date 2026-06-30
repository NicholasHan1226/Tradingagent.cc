from __future__ import annotations

import unittest

from Crypto.adapter import CryptoAdapter
from shared.screening.six_dimension_scorer import score_stock
from shared.wrappers.tradings_cron_entry import _crypto_orchestrator_deps, get_market_adapter


class FakeCryptoReader:
    def __init__(self) -> None:
        self.assets = [
            {
                "symbol": "BTCUSDT",
                "exchange": "BINANCE",
                "quote_asset": "USDT",
                "status": "TRADING",
            },
            {
                "symbol": "ETHUSDT",
                "exchange": "BINANCE",
                "quote_asset": "USDT",
                "status": "active",
            },
            {
                "symbol": "SOLUSDT",
                "exchange": "BINANCE",
                "quote_asset": "USDT",
                "status": "inactive",
            },
            {
                "symbol": "BTCBUSD",
                "exchange": "BINANCE",
                "quote_asset": "BUSD",
                "status": "TRADING",
            },
            {
                "symbol": "DOGEUSDT",
                "exchange": "COINBASE",
                "quote_asset": "USDT",
                "status": "TRADING",
            },
        ]

    def get_assets(self, market: str) -> list[dict[str, object]]:
        return self.assets if market in {"crypto", "Crypto"} else []


class FakeScoringReader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def get_regime(self) -> dict[str, object]:
        return {"regime": "risk_on", "regime_confidence": 1.0}

    def get_events(
        self,
        market: str,
        symbol: str,
        start: object = None,
        end: object = None,
    ) -> list[dict[str, object]]:
        self.calls.append(("events", market, symbol))
        return [{"confidence": 0.7, "direction": "positive"}]

    def get_event_candidates(self) -> list[dict[str, object]]:
        return []

    def get_factors(self, market: str, symbol: str) -> list[dict[str, object]]:
        self.calls.append(("factors", market, symbol))
        return [
            {"factor_name": "value", "event_time": "20260630", "value": 0.5},
            {"factor_name": "growth", "event_time": "20260630", "value": 0.5},
            {"factor_name": "quality", "event_time": "20260630", "value": 0.5},
            {"factor_name": "momentum", "event_time": "20260630", "value": 0.9},
            {"factor_name": "net_mf_amount", "event_time": "20260630", "value": 150000.0},
        ]

    def get_bars_daily(
        self,
        market: str,
        symbol: str,
        start: object = None,
        end: object = None,
    ) -> list[dict[str, object]]:
        self.calls.append(("bars", market, symbol))
        closes = [100.0 + idx * 2 for idx in range(20)]
        return [{"trade_date": f"202606{idx + 1:02d}", "close": close} for idx, close in enumerate(closes)]

    def get_sentiment(self) -> list[dict[str, object]]:
        return []


class CryptoAdapterTest(unittest.TestCase):
    def test_universe_keeps_active_binance_usdt_pairs(self) -> None:
        adapter = CryptoAdapter(reader=FakeCryptoReader())

        universe = adapter.get_universe("20260630")

        self.assertEqual(universe, ["BTCUSDT", "ETHUSDT"])

    def test_symbol_mapping_preserves_crypto_pair(self) -> None:
        adapter = CryptoAdapter(reader=FakeCryptoReader())

        self.assertEqual(adapter.get_market(), "crypto")
        self.assertEqual(adapter.map_symbol_to_reader("btcusdt"), ("crypto", "BTCUSDT"))
        self.assertEqual(adapter.map_symbol_to_reader("ETHUSDT"), ("crypto", "ETHUSDT"))
        self.assertEqual(adapter.get_shadow_account(), "crypto_shadow")

    def test_strategy_config_loads_five_crypto_strategies_and_24_7_rules(self) -> None:
        config = CryptoAdapter(reader=FakeCryptoReader()).get_strategy_config()

        self.assertEqual(config["market"], "crypto")
        self.assertEqual(config["volatility_baseline"], 0.80)
        self.assertEqual(config["market_rules"]["settlement"], "T+0")
        self.assertEqual(config["market_rules"]["trading_hours"], "24/7")
        self.assertEqual(len(config["strategies"]), 5)
        self.assertEqual(
            set(config["strategies"]),
            {"momentum", "trend", "mean_reversion", "volatility", "intraday"},
        )

    def test_six_dimension_score_uses_crypto_market_for_reader_queries(self) -> None:
        reader = FakeScoringReader()

        scores = score_stock("crypto", "BTCUSDT", reader, "20260630")

        self.assertGreater(scores["combined"], 0.5)
        self.assertIn(("events", "crypto", "BTCUSDT"), reader.calls)
        self.assertIn(("factors", "crypto", "BTCUSDT"), reader.calls)
        self.assertIn(("bars", "crypto", "BTCUSDT"), reader.calls)

    def test_cron_entry_registers_crypto_adapter(self) -> None:
        adapter = get_market_adapter("Crypto")

        self.assertEqual(adapter.get_market(), "crypto")
        self.assertEqual(adapter.get_shadow_account(), "crypto_shadow")

    def test_crypto_wrapper_injects_market_aware_scoring(self) -> None:
        reader = FakeScoringReader()

        scores = _crypto_orchestrator_deps().score_stock("BTCUSDT", "20260630", data_reader=reader)

        self.assertGreater(scores["combined"], 0.5)
        self.assertIn(("events", "crypto", "BTCUSDT"), reader.calls)
        self.assertIn(("factors", "crypto", "BTCUSDT"), reader.calls)
        self.assertIn(("bars", "crypto", "BTCUSDT"), reader.calls)


if __name__ == "__main__":
    unittest.main()
