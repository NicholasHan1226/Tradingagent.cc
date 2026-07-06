from __future__ import annotations

import unittest

from shared.markets.sim_capital import DEFAULT_SIM_CAPITAL_CNY, default_sim_capital, fx_to_cny


class SimCapitalTest(unittest.TestCase):
    def test_all_markets_default_to_200k_cny_equivalent(self) -> None:
        for market in ("ashare", "cn_futures", "crypto", "us", "pm"):
            capital = default_sim_capital(market)
            self.assertAlmostEqual(capital * fx_to_cny(market), DEFAULT_SIM_CAPITAL_CNY, places=2)

    def test_a_share_and_cn_futures_keep_cny_native_capital(self) -> None:
        self.assertEqual(default_sim_capital("ashare"), 200_000.0)
        self.assertEqual(default_sim_capital("cn_futures"), 200_000.0)
