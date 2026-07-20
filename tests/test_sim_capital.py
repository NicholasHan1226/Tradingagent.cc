from __future__ import annotations

import unittest

from shared.markets.sim_capital import (
    ALLOWED_CNY_TIERS,
    DEFAULT_CRYPTO_SIM_CAPITAL_USDT,
    DEFAULT_SIM_CAPITAL_CNY,
    default_sim_capital,
)


class SimCapitalTest(unittest.TestCase):
    def test_owned_lane_defaults_are_native_and_independent(self) -> None:
        self.assertEqual(default_sim_capital("ashare"), 50_000.0)
        self.assertEqual(default_sim_capital("cn_futures"), 50_000.0)
        self.assertEqual(default_sim_capital("crypto"), 10_000.0)
        self.assertEqual(DEFAULT_CRYPTO_SIM_CAPITAL_USDT, 10_000.0)

    def test_domestic_legacy_tiers_cannot_create_parallel_authority(self) -> None:
        for market in ("ashare", "cn_futures"):
            with self.subTest(market=market):
                self.assertEqual(
                    default_sim_capital(market, capital_cny=30_000.0),
                    DEFAULT_SIM_CAPITAL_CNY,
                )
                self.assertEqual(
                    default_sim_capital(market, capital_cny=200_000.0),
                    DEFAULT_SIM_CAPITAL_CNY,
                )
                self.assertEqual(
                    default_sim_capital(market, tier="200000"),
                    DEFAULT_SIM_CAPITAL_CNY,
                )

    def test_crypto_rejects_cross_currency_or_tier_override(self) -> None:
        for kwargs in ({"capital_cny": 50_000.0}, {"tier": "50000"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ValueError, "USDT-native"):
                    default_sim_capital("crypto", **kwargs)

    def test_missing_unknown_and_retired_markets_fail_closed(self) -> None:
        for market in (None, "", "us", "pm", "hk", "martian"):
            with self.subTest(market=market):
                with self.assertRaisesRegex(ValueError, "unknown or retired"):
                    default_sim_capital(market)  # type: ignore[arg-type]

    def test_active_spelling_aliases_canonicalize_without_new_authority(self) -> None:
        self.assertEqual(default_sim_capital("a-share"), DEFAULT_SIM_CAPITAL_CNY)
        self.assertEqual(default_sim_capital("a_share"), DEFAULT_SIM_CAPITAL_CNY)
        self.assertEqual(default_sim_capital("cnfutures"), DEFAULT_SIM_CAPITAL_CNY)
        self.assertEqual(default_sim_capital("cn-futures"), DEFAULT_SIM_CAPITAL_CNY)

    def test_only_one_domestic_cny_tier_is_exported(self) -> None:
        self.assertEqual(ALLOWED_CNY_TIERS, (50_000.0,))


if __name__ == "__main__":
    unittest.main()
