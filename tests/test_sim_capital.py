from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from shared.markets.sim_capital import DEFAULT_SIM_CAPITAL_CNY, default_sim_capital, fx_to_cny


class SimCapitalTest(unittest.TestCase):
    def test_us_crypto_pm_default_to_10000_base_currency(self) -> None:
        for market in ("us", "crypto", "pm"):
            capital = default_sim_capital(market)
            self.assertAlmostEqual(capital, 10_000.0, places=2)
            self.assertAlmostEqual(capital * fx_to_cny(market), 10_000.0 * fx_to_cny(market), places=2)

    def test_a_share_and_cn_futures_default_to_200k_cny(self) -> None:
        self.assertEqual(default_sim_capital("ashare"), 200_000.0)
        self.assertEqual(default_sim_capital("cn_futures"), 200_000.0)

    def test_a_share_tier_env_selects_capital(self) -> None:
        with patch.dict(os.environ, {"ASHARE_SIM_CAPITAL_TIER": "100000"}, clear=False):
            self.assertEqual(default_sim_capital("ashare"), 100_000.0)

    def test_cn_futures_tier_env_selects_capital(self) -> None:
        with patch.dict(os.environ, {"CN_FUTURES_SIM_CAPITAL_TIER": "50000"}, clear=False):
            self.assertEqual(default_sim_capital("cn_futures"), 50_000.0)

    def test_invalid_tier_env_falls_back_to_default(self) -> None:
        with patch.dict(os.environ, {"ASHARE_SIM_CAPITAL_TIER": "12345"}, clear=False):
            self.assertEqual(default_sim_capital("ashare"), DEFAULT_SIM_CAPITAL_CNY)
        with patch.dict(os.environ, {"CN_FUTURES_SIM_CAPITAL_TIER": "999999"}, clear=False):
            self.assertEqual(default_sim_capital("cn_futures"), DEFAULT_SIM_CAPITAL_CNY)

    def test_capital_cny_parameter_selects_valid_tier(self) -> None:
        self.assertEqual(default_sim_capital("ashare", capital_cny=50_000.0), 50_000.0)
        self.assertEqual(default_sim_capital("cn_futures", capital_cny=200_000.0), 200_000.0)

    def test_invalid_capital_cny_parameter_falls_back_to_default(self) -> None:
        self.assertEqual(default_sim_capital("ashare", capital_cny=30_000.0), DEFAULT_SIM_CAPITAL_CNY)
        self.assertEqual(default_sim_capital("cn_futures", capital_cny=150_000.0), DEFAULT_SIM_CAPITAL_CNY)

    def test_tier_env_does_not_affect_us_crypto_pm(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ASHARE_SIM_CAPITAL_TIER": "50000",
                "CN_FUTURES_SIM_CAPITAL_TIER": "50000",
            },
            clear=False,
        ):
            for market in ("us", "crypto", "pm"):
                self.assertAlmostEqual(default_sim_capital(market), 10_000.0, places=2)


if __name__ == "__main__":
    unittest.main()
