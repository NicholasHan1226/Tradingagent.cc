#!/usr/bin/env python3
"""Tests for China futures simulated execution."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _close_account(
    *, symbol: str = "rb2601", position_side: str = "long", total_qty: int = 2
) -> dict[str, object]:
    return {
        **_cnf_account(),
        "position_snapshot": {
            "snapshot_id": "fixture-cnf-position-001",
            "as_of": "2026-07-06T14:30:00+08:00",
            "authority_id": "cn-futures-capital-v1",
            "broker_contract": "tradingagent.cnfutures.paper_broker.v1",
            "positions": [
                {
                    "symbol": symbol,
                    "position_side": position_side,
                    "total_qty": total_qty,
                }
            ],
        },
    }


def _cnf_account() -> dict[str, object]:
    return {
        "account_id": "cn_futures_sim",
        "market": "cn_futures",
        "broker_contract": "tradingagent.cnfutures.paper_broker.v1",
        "authority_id": "cn-futures-capital-v1",
        "authority_generation": 1,
    }


class CNFuturesSimTest(unittest.TestCase):
    def test_direct_executor_rejects_real_account_before_ignoring_it(self) -> None:
        from CNFutures.sim_executor import cn_futures_sim_execute

        with self.assertRaisesRegex(RuntimeError, "real/live execution is rejected"):
            cn_futures_sim_execute(
                order={
                    "order_id": "SIM-CNF-REAL",
                    "symbol": "rb2601",
                    "side": "buy",
                    "quantity": 1,
                    "price": 3500.0,
                    "previous_close": 3500.0,
                },
                account={"account_type": "real"},
                config={},
            )

    def test_force_flatten_position_close_records_realized_pnl(self) -> None:
        from CNFutures.sim_runner import _realized_pnl_from_position_close

        long_perf = _realized_pnl_from_position_close(
            position={"net_qty": 2, "avg_price": 3500.0},
            side="sell",
            receipt={"filled_qty": 2, "avg_price": 3510.0, "fee": 12.0},
            rule_multiplier=300,
        )
        short_perf = _realized_pnl_from_position_close(
            position={"net_qty": -1, "avg_price": 3500.0},
            side="buy",
            receipt={
                "filled_qty": 1,
                "avg_price": 3490.0,
                "raw_response": {"estimated_close_fee": 6.0},
            },
            rule_multiplier=300,
        )

        self.assertEqual(long_perf["method"], "force_flatten_position_close")
        self.assertEqual(long_perf["realized_pnl"], 5988.0)
        self.assertEqual(short_perf["realized_pnl"], 2994.0)

    def test_contract_rules_calculate_margin_and_round_trip_fee(self) -> None:
        from CNFutures.contract_rules import get_contract_rule
        from CNFutures.margin_model import estimate_order_cost
        from CNFutures.contract_rules import normalize_product

        rule = get_contract_rule("rb2601")
        suffixed_rule = get_contract_rule("RB2601.SHF")
        cost = estimate_order_cost(
            symbol="rb2601",
            side="buy",
            quantity=2,
            price=3500.0,
        )

        self.assertEqual(rule.exchange, "SHFE")
        self.assertEqual(suffixed_rule.product, "rb")
        self.assertEqual(normalize_product("I2509.DCE"), "i")
        self.assertEqual(normalize_product("IF2601.CFFEX"), "if")
        self.assertEqual(rule.open_fee_type, "rate")
        self.assertEqual(rule.contract_multiplier, 10)
        self.assertEqual(cost.notional, 70000.0)
        self.assertEqual(cost.margin_required, 9100.0)
        self.assertEqual(cost.open_fee, 7.0)
        self.assertEqual(cost.estimated_close_fee, 7.0)
        self.assertEqual(cost.total_estimated_fee, 14.0)
        fixed_fee_cost = estimate_order_cost(
            symbol="m2601.DCE",
            side="buy",
            quantity=2,
            price=3000.0,
        )
        self.assertEqual(fixed_fee_cost.rule.open_fee_type, "fixed_per_lot")
        self.assertEqual(fixed_fee_cost.open_fee, 3.0)
        self.assertEqual(fixed_fee_cost.estimated_close_fee, 3.0)
        index_rule = get_contract_rule("IF2601.CFFEX")
        self.assertEqual(index_rule.exchange, "CFFEX")
        self.assertEqual(index_rule.contract_multiplier, 300)
        self.assertFalse(index_rule.night_session)

    def test_index_intraday_directional_signal_buys_sells_and_respects_close_guard(
        self,
    ) -> None:
        from CNFutures.signal_engine import generate_style_signal

        style = {
            "name": "index_intraday_directional",
            "style_family": "index_intraday_directional",
            "signal_threshold": 0.001,
            "momentum_lookback_bars": 3,
            "moving_average_bars": 4,
            "prediction_horizon_bars": 3,
            "no_overnight": True,
            "min_volume_ratio": 1.05,
            "flatten_before_session_close_minutes": 10,
        }
        up_bars = [
            {"bar_time": "2026-07-06 14:10:00", "close": 3500, "volume": 1000},
            {"bar_time": "2026-07-06 14:15:00", "close": 3502, "volume": 1000},
            {"bar_time": "2026-07-06 14:20:00", "close": 3505, "volume": 1100},
            {"bar_time": "2026-07-06 14:25:00", "close": 3512, "volume": 1400},
            {"bar_time": "2026-07-06 14:30:00", "close": 3520, "volume": 1600},
        ]
        down_bars = [
            {"bar_time": "2026-07-06 14:10:00", "close": 3500, "volume": 1000},
            {"bar_time": "2026-07-06 14:15:00", "close": 3498, "volume": 1000},
            {"bar_time": "2026-07-06 14:20:00", "close": 3490, "volume": 1100},
            {"bar_time": "2026-07-06 14:25:00", "close": 3482, "volume": 1400},
            {"bar_time": "2026-07-06 14:30:00", "close": 3475, "volume": 1600},
        ]
        close_guard_bars = [
            *up_bars[:-1],
            {"bar_time": "2026-07-06 14:55:00", "close": 3520, "volume": 1600},
        ]

        buy = generate_style_signal("IF2601.CFFEX", up_bars, style)
        sell = generate_style_signal("IF2601.CFFEX", down_bars, style)
        guarded = generate_style_signal("IF2601.CFFEX", close_guard_bars, style)

        self.assertEqual(buy["action"], "buy")
        self.assertEqual(buy["style_family"], "index_intraday_directional")
        self.assertEqual(buy["scenario_tags"]["time_bucket"], "day_late")
        self.assertEqual(buy["scenario_tags"]["direction"], "buy")
        self.assertEqual(buy["exit_plan"]["prediction_horizon_bars"], 3)
        self.assertEqual(buy["exit_plan"]["time_stop_bars"], 3)
        self.assertEqual(sell["action"], "sell")
        self.assertEqual(guarded["action"], "hold")
        self.assertEqual(guarded["reason"], "session_close_guard")

    def test_index_intraday_directional_signal_accepts_timezone_aware_bar_times(
        self,
    ) -> None:
        from CNFutures.signal_engine import generate_style_signal

        style = {
            "name": "index_intraday_directional",
            "style_family": "index_intraday_directional",
            "signal_threshold": 0.001,
            "momentum_lookback_bars": 3,
            "moving_average_bars": 4,
            "prediction_horizon_bars": 3,
            "no_overnight": True,
            "min_volume_ratio": 1.05,
            "flatten_before_session_close_minutes": 10,
        }
        bars = [
            {"bar_time": "2026-07-06T14:10:00+08:00", "close": 3500, "volume": 1000},
            {"bar_time": "2026-07-06T14:15:00+08:00", "close": 3502, "volume": 1000},
            {"bar_time": "2026-07-06T14:20:00+08:00", "close": 3505, "volume": 1100},
            {"bar_time": "2026-07-06T14:25:00+08:00", "close": 3512, "volume": 1400},
            {"bar_time": "2026-07-06T14:30:00+08:00", "close": 3520, "volume": 1600},
        ]

        signal = generate_style_signal("IF2601.CFFEX", bars, style)

        self.assertEqual(signal["action"], "buy")
        self.assertEqual(signal["scenario_tags"]["time_bucket"], "day_late")

    def test_rollover_guard_only_blocks_before_contract_month_start(self) -> None:
        from CNFutures.sim_runner import _contract_inside_rollover_guard

        style = {"rollover_min_days_to_contract_month_start": 5}

        self.assertEqual(
            _contract_inside_rollover_guard("IF2607.CFFEX", "20260628", style),
            (True, 3),
        )
        self.assertEqual(
            _contract_inside_rollover_guard("IF2607.CFFEX", "20260708", style),
            (False, -7),
        )

    def test_index_intraday_directional_signal_filters_weak_confirmation(self) -> None:
        from CNFutures.signal_engine import generate_style_signal

        style = {
            "name": "index_intraday_directional",
            "style_family": "index_intraday_directional",
            "signal_threshold": 0.001,
            "momentum_lookback_bars": 3,
            "moving_average_bars": 4,
            "no_overnight": True,
            "day_session_only": True,
            "trend_alignment_required": True,
            "min_volume_ratio": 1.05,
        }
        weak_volume_bars = [
            {"bar_time": "2026-07-06 14:10:00", "close": 3500, "volume": 1000},
            {"bar_time": "2026-07-06 14:15:00", "close": 3502, "volume": 1000},
            {"bar_time": "2026-07-06 14:20:00", "close": 3505, "volume": 1000},
            {"bar_time": "2026-07-06 14:25:00", "close": 3512, "volume": 1000},
            {"bar_time": "2026-07-06 14:30:00", "close": 3520, "volume": 1000},
        ]
        misaligned_bars = [
            {"bar_time": "2026-07-06 14:10:00", "close": 3700, "volume": 1000},
            {"bar_time": "2026-07-06 14:15:00", "close": 3500, "volume": 1000},
            {"bar_time": "2026-07-06 14:20:00", "close": 3800, "volume": 1000},
            {"bar_time": "2026-07-06 14:25:00", "close": 3800, "volume": 1000},
            {"bar_time": "2026-07-06 14:30:00", "close": 3600, "volume": 1500},
        ]

        weak_volume = generate_style_signal("IF2601.CFFEX", weak_volume_bars, style)
        misaligned = generate_style_signal("IF2601.CFFEX", misaligned_bars, style)

        self.assertEqual(weak_volume["action"], "hold")
        self.assertEqual(weak_volume["reason"], "volume_confirmation_filter")
        self.assertEqual(misaligned["action"], "hold")
        self.assertEqual(misaligned["reason"], "trend_alignment_filter")

    def test_index_intraday_directional_signal_filters_open_gap_and_low_volatility(
        self,
    ) -> None:
        from CNFutures.signal_engine import generate_style_signal

        style = {
            "name": "index_intraday_directional",
            "style_family": "index_intraday_directional",
            "signal_threshold": 0.0005,
            "momentum_lookback_bars": 3,
            "moving_average_bars": 4,
            "no_overnight": True,
            "day_session_only": True,
            "trend_alignment_required": True,
            "min_volume_ratio": 1.0,
            "open_cooldown_minutes": 15,
            "gap_cooldown_minutes": 30,
            "max_open_gap_pct": 0.01,
            "min_recent_range_pct": 0.001,
        }
        open_cooldown_bars = [
            {"bar_time": "2026-07-06 09:00:00", "close": 3500, "volume": 1000},
            {"bar_time": "2026-07-06 09:05:00", "close": 3505, "volume": 1000},
            {"bar_time": "2026-07-06 09:10:00", "close": 3510, "volume": 1100},
            {"bar_time": "2026-07-06 09:12:00", "close": 3515, "volume": 1200},
            {"bar_time": "2026-07-06 09:14:00", "close": 3520, "volume": 1300},
        ]
        gap_bars = [
            {
                "bar_time": "2026-07-06 09:00:00",
                "close": 3500,
                "volume": 1000,
                "previous_close": 3400,
            },
            {
                "bar_time": "2026-07-06 09:05:00",
                "close": 3510,
                "volume": 1000,
                "previous_close": 3400,
            },
            {
                "bar_time": "2026-07-06 09:10:00",
                "close": 3520,
                "volume": 1100,
                "previous_close": 3400,
            },
            {
                "bar_time": "2026-07-06 09:15:00",
                "close": 3530,
                "volume": 1200,
                "previous_close": 3400,
            },
            {
                "bar_time": "2026-07-06 09:20:00",
                "close": 3540,
                "volume": 1300,
                "previous_close": 3400,
            },
        ]
        low_volatility_bars = [
            {"bar_time": "2026-07-06 14:10:00", "close": 3500.0, "volume": 1000},
            {"bar_time": "2026-07-06 14:15:00", "close": 3500.4, "volume": 1000},
            {"bar_time": "2026-07-06 14:20:00", "close": 3500.8, "volume": 1100},
            {"bar_time": "2026-07-06 14:25:00", "close": 3501.0, "volume": 1200},
            {"bar_time": "2026-07-06 14:30:00", "close": 3501.2, "volume": 1300},
        ]

        open_cooldown = generate_style_signal("IF2601.CFFEX", open_cooldown_bars, style)
        gap = generate_style_signal("IF2601.CFFEX", gap_bars, style)
        low_volatility = generate_style_signal(
            "IF2601.CFFEX", low_volatility_bars, style
        )

        self.assertEqual(open_cooldown["action"], "hold")
        self.assertEqual(open_cooldown["reason"], "opening_cooldown")
        self.assertEqual(open_cooldown["minutes_since_open"], 14)
        self.assertEqual(gap["action"], "hold")
        self.assertEqual(gap["reason"], "opening_gap_cooldown")
        self.assertEqual(low_volatility["action"], "hold")
        self.assertEqual(low_volatility["reason"], "low_volatility_filter")

    def test_index_intraday_directional_signal_filters_choppy_reversal_and_noise(
        self,
    ) -> None:
        from CNFutures.signal_engine import generate_style_signal

        style = {
            "name": "index_intraday_directional",
            "style_family": "index_intraday_directional",
            "signal_threshold": 0.0005,
            "momentum_lookback_bars": 4,
            "moving_average_bars": 5,
            "no_overnight": True,
            "day_session_only": True,
            "trend_alignment_required": True,
            "min_volume_ratio": 1.0,
            "min_recent_range_pct": 0.0001,
            "min_directional_consistency": 0.75,
            "max_intrabar_reversal_pct": 0.001,
            "min_signal_to_range_ratio": 0.55,
        }
        choppy_bars = [
            {"bar_time": "2026-07-06 14:00:00", "close": 3500, "volume": 1000},
            {"bar_time": "2026-07-06 14:05:00", "close": 3510, "volume": 1000},
            {"bar_time": "2026-07-06 14:10:00", "close": 3504, "volume": 1100},
            {"bar_time": "2026-07-06 14:15:00", "close": 3514, "volume": 1200},
            {"bar_time": "2026-07-06 14:20:00", "close": 3508, "volume": 1300},
            {"bar_time": "2026-07-06 14:25:00", "close": 3520, "volume": 1600},
        ]
        reversal_bars = [
            {"bar_time": "2026-07-06 14:00:00", "close": 3500, "volume": 1000},
            {"bar_time": "2026-07-06 14:05:00", "close": 3505, "volume": 1000},
            {"bar_time": "2026-07-06 14:10:00", "close": 3510, "volume": 1100},
            {"bar_time": "2026-07-06 14:15:00", "close": 3515, "volume": 1200},
            {"bar_time": "2026-07-06 14:20:00", "close": 3520, "volume": 1300},
            {
                "bar_time": "2026-07-06 14:25:00",
                "close": 3522,
                "high": 3535,
                "low": 3518,
                "volume": 1600,
            },
        ]
        noisy_bars = [
            {"bar_time": "2026-07-06 14:00:00", "close": 3500, "volume": 1000},
            {"bar_time": "2026-07-06 14:05:00", "close": 3520, "volume": 1000},
            {"bar_time": "2026-07-06 14:10:00", "close": 3510, "volume": 1100},
            {"bar_time": "2026-07-06 14:15:00", "close": 3525, "volume": 1200},
            {"bar_time": "2026-07-06 14:20:00", "close": 3515, "volume": 1300},
            {"bar_time": "2026-07-06 14:25:00", "close": 3526, "volume": 1600},
        ]

        choppy = generate_style_signal("IF2601.CFFEX", choppy_bars, style)
        reversal = generate_style_signal("IF2601.CFFEX", reversal_bars, style)
        noisy = generate_style_signal(
            "IF2601.CFFEX", noisy_bars, {**style, "min_directional_consistency": 0.0}
        )

        self.assertEqual(choppy["action"], "hold")
        self.assertEqual(choppy["reason"], "directional_consistency_filter")
        self.assertEqual(reversal["action"], "hold")
        self.assertEqual(reversal["reason"], "intrabar_reversal_filter")
        self.assertEqual(noisy["action"], "hold")
        self.assertEqual(noisy["reason"], "signal_noise_filter")

    def test_index_intraday_directional_signal_filters_bar_quality_and_late_chase(
        self,
    ) -> None:
        from CNFutures.signal_engine import generate_style_signal

        style = {
            "name": "index_intraday_directional",
            "style_family": "index_intraday_directional",
            "signal_threshold": 0.0005,
            "momentum_lookback_bars": 4,
            "moving_average_bars": 5,
            "no_overnight": True,
            "day_session_only": True,
            "trend_alignment_required": True,
            "min_volume_ratio": 1.0,
            "min_recent_range_pct": 0.0001,
            "min_directional_consistency": 0.0,
            "max_intrabar_reversal_pct": 0.0,
            "min_signal_to_range_ratio": 0.0,
            "max_bar_gap_minutes": 7,
            "min_body_to_range_ratio": 0.45,
            "min_consecutive_aligned_bars": 3,
            "max_late_chase_pct": 0.006,
        }
        gap_bars = [
            {"bar_time": "2026-07-06 14:00:00", "close": 3500, "volume": 1000},
            {"bar_time": "2026-07-06 14:05:00", "close": 3505, "volume": 1000},
            {"bar_time": "2026-07-06 14:20:00", "close": 3510, "volume": 1100},
            {"bar_time": "2026-07-06 14:25:00", "close": 3515, "volume": 1200},
            {"bar_time": "2026-07-06 14:30:00", "close": 3520, "volume": 1300},
            {"bar_time": "2026-07-06 14:35:00", "close": 3526, "volume": 1600},
        ]
        long_wick_bars = [
            {"bar_time": "2026-07-06 14:00:00", "close": 3500, "volume": 1000},
            {"bar_time": "2026-07-06 14:05:00", "close": 3505, "volume": 1000},
            {"bar_time": "2026-07-06 14:10:00", "close": 3510, "volume": 1100},
            {"bar_time": "2026-07-06 14:15:00", "close": 3515, "volume": 1200},
            {"bar_time": "2026-07-06 14:20:00", "close": 3520, "volume": 1300},
            {
                "bar_time": "2026-07-06 14:25:00",
                "open": 3519,
                "high": 3535,
                "low": 3515,
                "close": 3523,
                "volume": 1600,
            },
        ]
        not_consecutive_bars = [
            {"bar_time": "2026-07-06 14:00:00", "close": 3500, "volume": 1000},
            {"bar_time": "2026-07-06 14:05:00", "close": 3510, "volume": 1000},
            {"bar_time": "2026-07-06 14:10:00", "close": 3518, "volume": 1100},
            {"bar_time": "2026-07-06 14:15:00", "close": 3512, "volume": 1200},
            {"bar_time": "2026-07-06 14:20:00", "close": 3520, "volume": 1300},
            {"bar_time": "2026-07-06 14:25:00", "close": 3528, "volume": 1600},
        ]
        chase_bars = [
            {"bar_time": "2026-07-06 14:00:00", "close": 3500, "volume": 1000},
            {"bar_time": "2026-07-06 14:05:00", "close": 3505, "volume": 1000},
            {"bar_time": "2026-07-06 14:10:00", "close": 3510, "volume": 1100},
            {"bar_time": "2026-07-06 14:15:00", "close": 3515, "volume": 1200},
            {"bar_time": "2026-07-06 14:20:00", "close": 3520, "volume": 1300},
            {
                "bar_time": "2026-07-06 14:25:00",
                "open": 3521,
                "high": 3545,
                "low": 3520,
                "close": 3544,
                "volume": 1800,
            },
        ]

        gap = generate_style_signal("IF2601.CFFEX", gap_bars, style)
        long_wick = generate_style_signal("IF2601.CFFEX", long_wick_bars, style)
        not_consecutive = generate_style_signal(
            "IF2601.CFFEX",
            not_consecutive_bars,
            {**style, "min_body_to_range_ratio": 0.0},
        )
        chase = generate_style_signal(
            "IF2601.CFFEX",
            chase_bars,
            {
                **style,
                "min_body_to_range_ratio": 0.0,
                "min_consecutive_aligned_bars": 0,
            },
        )

        self.assertEqual(gap["action"], "hold")
        self.assertEqual(gap["reason"], "bar_gap_filter")
        self.assertEqual(long_wick["action"], "hold")
        self.assertEqual(long_wick["reason"], "body_to_range_filter")
        self.assertEqual(not_consecutive["action"], "hold")
        self.assertEqual(
            not_consecutive["reason"], "insufficient_consecutive_5min_bars"
        )
        self.assertEqual(chase["action"], "hold")
        self.assertEqual(chase["reason"], "late_chase_filter")

    def test_index_intraday_directional_signal_rejects_non_day_session_bars(
        self,
    ) -> None:
        from CNFutures.signal_engine import generate_style_signal

        style = {
            "name": "index_intraday_directional",
            "style_family": "index_intraday_directional",
            "signal_threshold": 0.001,
            "momentum_lookback_bars": 3,
            "moving_average_bars": 4,
            "no_overnight": True,
            "day_session_only": True,
        }
        night_bars = [
            {"bar_time": "2026-07-06 21:00:00", "close": 3500, "volume": 1000},
            {"bar_time": "2026-07-06 21:05:00", "close": 3502, "volume": 1000},
            {"bar_time": "2026-07-06 21:10:00", "close": 3505, "volume": 1100},
            {"bar_time": "2026-07-06 21:15:00", "close": 3512, "volume": 1400},
            {"bar_time": "2026-07-06 21:20:00", "close": 3520, "volume": 1600},
        ]
        lunch_break_bars = [
            {"bar_time": "2026-07-06 11:05:00", "close": 3500, "volume": 1000},
            {"bar_time": "2026-07-06 11:10:00", "close": 3502, "volume": 1000},
            {"bar_time": "2026-07-06 11:15:00", "close": 3505, "volume": 1100},
            {"bar_time": "2026-07-06 11:20:00", "close": 3512, "volume": 1400},
            {"bar_time": "2026-07-06 11:35:00", "close": 3520, "volume": 1600},
        ]

        night = generate_style_signal("IF2601.CFFEX", night_bars, style)
        lunch_break = generate_style_signal("IF2601.CFFEX", lunch_break_bars, style)

        self.assertEqual(night["action"], "hold")
        self.assertEqual(night["reason"], "outside_day_session")
        self.assertEqual(lunch_break["action"], "hold")
        self.assertEqual(lunch_break["reason"], "outside_day_session")

    def test_sim_executor_registers_cn_futures_as_simulated_only(self) -> None:
        import CNFutures.sim_executor  # noqa: F401
        from shared.execution.sim_broker import execute_sim_order

        result = execute_sim_order(
            order={
                "order_id": "SIM-CNF-1",
                "authority_generation": 1,
                "symbol": "rb2601",
                "side": "buy",
                "position_effect": "open",
                "quantity": 2,
                "price": 3500.0,
                "previous_close": 3500.0,
                "bar_time": "2026-07-06 14:30:00",
                "bar_volume": 1000,
                "trade_date": "20260706",
                "decision_time": "2026-07-06T14:30:10+08:00",
            },
            market="cn_futures",
            account=_cnf_account(),
            config={"fee_mode": "round_trip_estimate"},
        )

        self.assertEqual(result.status, "filled")
        self.assertEqual(result.market, "cn_futures")
        self.assertEqual(result.capital_layer, "simulated")
        self.assertEqual(result.account_type, "simulated")
        self.assertEqual(result.filled_qty, 2)
        self.assertEqual(result.avg_price, 3501.0)
        self.assertEqual(result.fee, 14.0)
        self.assertEqual(result.raw_response["symbol"], "rb2601")
        self.assertEqual(result.raw_response["requested_price"], 3500.0)
        self.assertEqual(result.raw_response["slippage_bps"], 2.0)
        self.assertEqual(result.raw_response["contract_multiplier"], 10)
        self.assertEqual(result.raw_response["margin_required"], 9102.6)
        self.assertFalse(result.raw_response["real_trading_enabled"])

    def test_close_requires_matching_authority_bound_position_snapshot(self) -> None:
        from shared.execution.sim_broker import execute_sim_order

        order = {
            "order_id": "SIM-CNF-CLOSE",
            "authority_generation": 1,
            "symbol": "rb2601",
            "side": "sell",
            "position_effect": "close",
            "quantity": 2,
            "price": 3500.0,
            "previous_close": 3500.0,
            "bar_time": "2026-07-06 14:30:00",
            "bar_volume": 1000,
            "trade_date": "20260706",
            "decision_time": "2026-07-06T14:30:10+08:00",
        }
        missing = execute_sim_order(
            order=order,
            market="cn_futures",
            account=_cnf_account(),
            config={"fee_mode": "round_trip_estimate"},
        )
        accepted = execute_sim_order(
            order=order,
            market="cn_futures",
            account=_close_account(),
            config={"fee_mode": "round_trip_estimate"},
        )

        self.assertEqual(missing.status, "rejected")
        self.assertEqual(missing.raw_response["reason"], "position_snapshot_required")
        self.assertEqual(accepted.status, "filled")
        self.assertEqual(accepted.filled_qty, 2)

    def test_close_cannot_exceed_or_reverse_snapshot_side(self) -> None:
        from shared.execution.sim_broker import execute_sim_order

        base = {
            "authority_generation": 1,
            "symbol": "rb2601",
            "side": "sell",
            "position_effect": "close",
            "quantity": 2,
            "price": 3500.0,
            "previous_close": 3500.0,
            "bar_time": "2026-07-06 14:30:00",
            "bar_volume": 1000,
            "trade_date": "20260706",
            "decision_time": "2026-07-06T14:30:10+08:00",
        }
        insufficient = execute_sim_order(
            order={**base, "order_id": "SIM-CNF-CLOSE-OVER"},
            market="cn_futures",
            account=_close_account(total_qty=1),
            config={},
        )
        wrong_side = execute_sim_order(
            order={**base, "order_id": "SIM-CNF-CLOSE-WRONG"},
            market="cn_futures",
            account=_close_account(position_side="short"),
            config={},
        )

        self.assertEqual(
            insufficient.raw_response["reason"], "insufficient_close_position"
        )
        self.assertEqual(
            wrong_side.raw_response["reason"], "position_snapshot_match_not_unique"
        )

    def test_sim_executor_models_partial_fill_and_price_limits(self) -> None:
        import CNFutures.sim_executor  # noqa: F401
        from shared.execution.sim_broker import execute_sim_order

        partial = execute_sim_order(
            order={
                "order_id": "SIM-CNF-PARTIAL",
                "authority_generation": 1,
                "symbol": "IF2601.CFFEX",
                "side": "buy",
                "position_effect": "open",
                "quantity": 10,
                "price": 3500.0,
                "bar_volume": 20,
                "bar_time": "2026-07-06 14:30:00",
                "previous_close": 3500.0,
                "trade_date": "20260706",
                "decision_time": "2026-07-06T14:30:10+08:00",
            },
            market="cn_futures",
            account=_cnf_account(),
            config={
                "fee_mode": "round_trip_estimate",
                "volume_participation": 0.10,
                "slippage_bps": 1.0,
            },
        )
        rejected = execute_sim_order(
            order={
                "order_id": "SIM-CNF-LIMIT",
                "authority_generation": 1,
                "symbol": "IF2601.CFFEX",
                "side": "buy",
                "position_effect": "open",
                "quantity": 1,
                "price": 4000.0,
                "previous_close": 3500.0,
            },
            market="cn_futures",
            account=_cnf_account(),
            config={},
        )

        self.assertEqual(partial.status, "partial")
        self.assertEqual(partial.filled_qty, 2)
        self.assertEqual(partial.raw_response["requested_quantity"], 10)
        self.assertEqual(partial.raw_response["fill_status"], "partial")
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(rejected.raw_response["limit_up"], 3850.0)
        self.assertEqual(rejected.raw_response["reason"], "price_limit_guard")

    def test_sim_executor_uses_order_book_quote_and_depth_quantity(self) -> None:
        import CNFutures.sim_executor  # noqa: F401
        from shared.execution.sim_broker import execute_sim_order

        result = execute_sim_order(
            order={
                "order_id": "SIM-CNF-DEPTH",
                "authority_generation": 1,
                "symbol": "rb2601",
                "side": "buy",
                "position_effect": "open",
                "quantity": 5,
                "price": 3500.0,
                "ask_price": 3502.0,
                "ask_size": 2,
                "previous_close": 3500.0,
                "bar_volume": 1000,
                "quote_time": "2026-07-06 14:30:00",
                "trade_date": "20260706",
                "decision_time": "2026-07-06T14:30:10+08:00",
            },
            market="cn_futures",
            account=_cnf_account(),
            config={"fee_mode": "round_trip_estimate", "slippage_bps": 0.0},
        )

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.filled_qty, 2)
        self.assertEqual(result.avg_price, 3502.0)
        self.assertEqual(
            result.raw_response["execution_price_source"], "order_book_ask"
        )
        self.assertEqual(result.raw_response["order_book_available_qty"], 2)
        self.assertEqual(result.raw_response["ask_price"], 3502.0)
        self.assertEqual(result.raw_response["ask_size"], 2)

    def test_sim_executor_rejects_expiring_contract_with_explicit_metadata(
        self,
    ) -> None:
        import CNFutures.sim_executor  # noqa: F401
        from shared.execution.sim_broker import execute_sim_order

        result = execute_sim_order(
            order={
                "order_id": "SIM-CNF-EXPIRY",
                "authority_generation": 1,
                "symbol": "rb2607",
                "side": "buy",
                "position_effect": "open",
                "quantity": 1,
                "price": 3500.0,
                "trade_date": "20260703",
                "last_trade_date": "20260705",
                "previous_close": 3500.0,
            },
            market="cn_futures",
            account=_cnf_account(),
            config={"rollover_min_days_to_expiry": 5},
        )

        self.assertEqual(result.status, "rejected")
        self.assertEqual(
            result.raw_response["source"], "cn_futures_sim_executor_expiry_guard"
        )
        self.assertEqual(result.raw_response["days_to_expiry"], 2)
        self.assertEqual(result.raw_response["reason"], "contract_expiry_guard")

    def test_sim_executor_requires_position_effect_and_exact_integer_lots(self) -> None:
        from shared.execution.sim_broker import execute_sim_order

        missing_effect = execute_sim_order(
            order={
                "order_id": "SIM-CNF-NO-OFFSET",
                "authority_generation": 1,
                "symbol": "rb2601",
                "side": "buy",
                "quantity": 1,
                "price": 3500.0,
                "previous_close": 3500.0,
            },
            market="cn_futures",
            account=_cnf_account(),
        )
        fractional = execute_sim_order(
            order={
                "order_id": "SIM-CNF-FRACTIONAL",
                "authority_generation": 1,
                "symbol": "rb2601",
                "side": "buy",
                "position_effect": "open",
                "quantity": 1.5,
                "price": 3500.0,
                "previous_close": 3500.0,
            },
            market="cn_futures",
            account=_cnf_account(),
        )

        self.assertEqual(missing_effect.status, "rejected")
        self.assertEqual(
            missing_effect.raw_response["reason"], "position_effect_required"
        )
        self.assertEqual(fractional.status, "rejected")
        self.assertEqual(fractional.raw_response["reason"], "non_positive_quantity")

    def test_reversal_pnl_uses_one_round_trip_fee_when_previous_fill_precharged_it(
        self,
    ) -> None:
        from CNFutures.sim_runner import _realized_pnl_from_reversal

        performance = _realized_pnl_from_reversal(
            previous={
                "side": "buy",
                "filled_price": 3500.0,
                "filled_qty": 2,
                "fee": 14.0,
                "raw_response": {"total_estimated_fee": 14.0},
            },
            side="sell",
            receipt={
                "avg_price": 3510.0,
                "filled_qty": 2,
                "fee": 14.0,
                "raw_response": {
                    "total_estimated_fee": 14.0,
                    "estimated_close_fee": 7.0,
                },
            },
            rule_multiplier=10,
        )

        self.assertEqual(performance["gross_pnl"], 200.0)
        self.assertEqual(performance["round_trip_fee"], 14.0)
        self.assertEqual(performance["realized_pnl"], 186.0)

    def test_review_summarizes_errors_and_style_health(self) -> None:
        from CNFutures.review import summarize_errors, style_health

        errors = [
            {
                "stage": "data",
                "style": "trend",
                "symbol": "RB2601.SHF",
                "error": "stale_intraday_bar",
            },
            {
                "stage": "risk",
                "style": "breakout",
                "symbol": "RB2601.SHF",
                "error": "repeated_same_side_exposure",
            },
        ]
        records = [
            {
                "style": "breakout",
                "receipt": {"status": "filled"},
            }
        ]

        summary = summarize_errors(errors)
        health = style_health(records, errors)

        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["by_error"]["stale_intraday_bar"], 1)
        self.assertEqual(summary["by_stage"]["risk"], 1)
        self.assertEqual(health["trend"]["status"], "blocked")
        self.assertEqual(health["breakout"]["status"], "degraded")
        self.assertEqual(
            health["trend"]["suggested_action"], "inspect_data_or_risk_gate"
        )

    def test_append_review_keeps_current_review_and_retires_style_outputs(self) -> None:
        from CNFutures.review import append_review

        with tempfile.TemporaryDirectory() as tmp:
            review_path = (
                Path(tmp)
                / "shared"
                / "review"
                / "data"
                / "cn_futures_sim_reviews.jsonl"
            )
            record = {
                "style": "trend",
                "receipt": {
                    "status": "filled",
                    "fee": 2.0,
                    "raw_response": {"margin_required": 100.0, "notional": 1000.0},
                },
                "performance": {"realized_pnl": 5.0},
                "scenario_tags": {
                    "session": "day",
                    "time_bucket": "day_afternoon",
                    "product": "if",
                    "direction": "buy",
                    "volatility_bucket": "normal",
                    "volume_bucket": "strong",
                },
                "forward_outcome": {
                    "status": "labeled",
                    "direction_correct": True,
                    "time_stop_positive": True,
                    "take_profit_hit": True,
                    "stop_loss_hit": False,
                    "horizon_return_pct": 0.003,
                    "time_stop_return_pct": 0.002,
                },
            }

            payload = append_review(
                date="20260706",
                market="cn_futures",
                records=[record],
                errors=[],
                path=review_path,
            )

            persisted = json.loads(
                review_path.read_text(encoding="utf-8").splitlines()[-1]
            )

            self.assertNotIn("style_output_paths", payload)
            self.assertNotIn("style_output_paths", persisted)
            self.assertFalse(
                (
                    review_path.parent.parent / "cn_futures" / "style_comparison.json"
                ).exists()
            )
            self.assertFalse(
                (
                    review_path.parent.parent / "cn_futures" / "style_performance.jsonl"
                ).exists()
            )
            self.assertEqual(payload["market"], "cn_futures")
            self.assertEqual(
                payload["forward_label_summary"]["styles"]["trend"]["labeled"], 1
            )
            self.assertEqual(
                payload["forward_label_summary"]["styles"]["trend"]["win_rate"],
                1.0,
            )

    def test_non_index_styles_hold_in_night_session_by_default(self) -> None:
        from CNFutures.signal_engine import generate_style_signal

        style = {"name": "trend", "signal_threshold": 0.001}
        night_bars = [
            {"bar_time": "2026-07-06 21:00:00", "close": 3500, "volume": 1000},
            {"bar_time": "2026-07-06 21:05:00", "close": 3540, "volume": 1000},
        ]
        signal = generate_style_signal("rb2601", night_bars, style)
        self.assertEqual(signal["action"], "hold")
        self.assertEqual(signal["reason"], "night_session_not_allowed")

        allowed_style = {
            "name": "trend",
            "signal_threshold": 0.001,
            "night_session_allowed": True,
        }
        allowed_signal = generate_style_signal("rb2601", night_bars, allowed_style)
        self.assertEqual(allowed_signal["action"], "buy")
        self.assertEqual(allowed_signal["reason"], "trend_confirmed")

    def test_signal_hold_reason_explicitly_names_insufficient_consecutive_bars(
        self,
    ) -> None:
        from CNFutures.signal_engine import generate_style_signal

        style = {
            "name": "index_intraday_directional",
            "style_family": "index_intraday_directional",
            "signal_threshold": 0.0005,
            "momentum_lookback_bars": 4,
            "moving_average_bars": 5,
            "no_overnight": True,
            "day_session_only": True,
            "trend_alignment_required": True,
            "min_volume_ratio": 1.0,
            "min_recent_range_pct": 0.0001,
            "min_directional_consistency": 0.0,
            "max_intrabar_reversal_pct": 0.0,
            "min_signal_to_range_ratio": 0.0,
            "min_body_to_range_ratio": 0.0,
            "min_consecutive_aligned_bars": 3,
            "max_late_chase_pct": 0.0,
        }
        not_consecutive_bars = [
            {"bar_time": "2026-07-06 14:00:00", "close": 3500, "volume": 1000},
            {"bar_time": "2026-07-06 14:05:00", "close": 3510, "volume": 1000},
            {"bar_time": "2026-07-06 14:10:00", "close": 3518, "volume": 1100},
            {"bar_time": "2026-07-06 14:15:00", "close": 3512, "volume": 1200},
            {"bar_time": "2026-07-06 14:20:00", "close": 3520, "volume": 1300},
            {"bar_time": "2026-07-06 14:25:00", "close": 3528, "volume": 1600},
        ]

        signal = generate_style_signal("IF2601.CFFEX", not_consecutive_bars, style)

        self.assertEqual(signal["action"], "hold")
        self.assertEqual(signal["reason"], "insufficient_consecutive_5min_bars")
        self.assertIn("consecutive_aligned_bars", signal)
        self.assertIn("min_consecutive_aligned_bars", signal)
        self.assertLess(
            signal["consecutive_aligned_bars"], signal["min_consecutive_aligned_bars"]
        )

    def test_summarize_holds_breaks_down_by_product(self) -> None:
        from CNFutures.review import summarize_holds

        holds = [
            {
                "style": "index_intraday_directional",
                "symbol": "IF2601.CFFEX",
                "reason": "insufficient_consecutive_5min_bars",
                "session": "day",
            },
            {
                "style": "index_intraday_directional",
                "symbol": "IH2601.CFFEX",
                "reason": "insufficient_consecutive_5min_bars",
                "session": "day",
            },
            {
                "style": "trend",
                "symbol": "RB2601.SHF",
                "reason": "insufficient_consecutive_5min_bars",
                "session": "day",
            },
            {
                "style": "trend",
                "symbol": "RB2605.SHF",
                "reason": "volume_confirmation_filter",
                "session": "day",
            },
            {
                "style": "breakout",
                "symbol": "CU2607.SHF",
                "reason": "session_close_guard",
                "session": "day",
            },
        ]

        summary = summarize_holds(holds)

        self.assertEqual(summary["by_reason"]["insufficient_consecutive_5min_bars"], 3)
        self.assertEqual(summary["by_product"]["if"], 1)
        self.assertEqual(summary["by_product"]["ih"], 1)
        self.assertEqual(summary["by_product"]["rb"], 2)
        self.assertEqual(summary["by_product"]["cu"], 1)
        self.assertEqual(
            summary["by_product_by_reason"]["rb"]["insufficient_consecutive_5min_bars"],
            1,
        )
        self.assertEqual(
            summary["by_product_by_reason"]["rb"]["volume_confirmation_filter"], 1
        )
        self.assertEqual(
            summary["by_product_by_reason"]["if"]["insufficient_consecutive_5min_bars"],
            1,
        )

    def test_run_simulation_cli_is_retired_before_kill_switch_or_data_access(
        self,
    ) -> None:
        import os
        import subprocess

        env = os.environ.copy()
        env["CN_FUTURES_SIM_DISABLED"] = "1"
        env["PYTHONPATH"] = str(ROOT)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "CNFutures.run_simulation",
                "--json",
                "--date",
                "20260706",
            ],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 78)
        self.assertEqual(result.stdout, "")
        output = json.loads(result.stderr.strip().splitlines()[-1])
        self.assertEqual(output["state"], "retired")
        self.assertFalse(output["real_trading_enabled"])

    def test_night_session_22xx_reads_bars_with_natural_date(self) -> None:
        """Night session at 22:xx: _read_intraday_bars receives natural calendar date, not active_trade_date."""
        from CNFutures.sim_runner import _read_intraday_bars

        class Recorder:
            def __init__(self) -> None:
                self.calls: list[tuple[object, ...]] = []

            def get_bars_intraday(
                self, *args: object, **kwargs: object
            ) -> list[dict[str, object]]:
                self.calls.append(args)
                start = args[3]
                # Return bars only when queried with natural date "20260710"
                if start == "20260710":
                    return [
                        {
                            "bar_time": "2026-07-10 22:30:00",
                            "close": 3500.0,
                            "volume": 100,
                            "trade_date": "20260710",
                        },
                    ]
                return []

        reader = Recorder()
        # natural_date = "20260710" (night at 22:xx on Jul 10)
        bars = _read_intraday_bars(reader, "IF2609.CFX", "20260710")
        self.assertEqual(len(bars), 1, "Natural date should return night bars")
        self.assertEqual(bars[0]["close"], 3500.0)

        # active_trade_date = "20260711" (next trading day) would return nothing
        bars_wrong = _read_intraday_bars(reader, "IF2609.CFX", "20260711")
        self.assertEqual(
            len(bars_wrong), 0, "Active trade date should return empty at night"
        )

    def test_night_early_session_01xx_reads_bars_with_natural_date(self) -> None:
        """Night-early at 01:xx: natural calendar date is the next day, active_trade_date is the same day."""
        from CNFutures.sim_runner import _read_intraday_bars

        class Recorder:
            def __init__(self) -> None:
                self.calls: list[tuple[object, ...]] = []

            def get_bars_intraday(
                self, *args: object, **kwargs: object
            ) -> list[dict[str, object]]:
                self.calls.append(args)
                start = args[3]
                if start == "20260711":
                    return [
                        {
                            "bar_time": "2026-07-11 01:00:00",
                            "close": 3510.0,
                            "volume": 100,
                            "trade_date": "20260711",
                        },
                    ]
                return []

        reader = Recorder()
        # natural_date = "20260711" (01:xx on Jul 11)
        bars = _read_intraday_bars(reader, "CU2609.SHF", "20260711")
        self.assertEqual(len(bars), 1, "Natural date should return early-morning bars")
        self.assertEqual(bars[0]["close"], 3510.0)

        # active_trade_date would also be "20260711" in this case, so both work
        # but the key is that sim_runner derives market_data_date from now's natural date

    def test_stale_bars_rejected_by_freshness_gate(self) -> None:
        """Bars older than max_intraday_bar_age_minutes cause a hold, not a fill."""
        from datetime import datetime, timezone
        from CNFutures.sim_runner import _is_intraday_bar_fresh

        # now at 22:35 CN (= 14:35 UTC)
        now = datetime(2026, 7, 10, 14, 35, tzinfo=timezone.utc)
        old_bar_time = "2026-07-10 14:55:00"
        fresh, age = _is_intraday_bar_fresh(old_bar_time, now=now, max_age_minutes=10.0)
        self.assertFalse(fresh, "7+ hour old bar should NOT be fresh")
        self.assertIsNotNone(age)
        self.assertGreater(age, 10.0)

        fresh_bar_time = "2026-07-10 22:25:00"
        fresh2, age2 = _is_intraday_bar_fresh(
            fresh_bar_time, now=now, max_age_minutes=10.0
        )
        self.assertTrue(fresh2, "10-minute old bar should be fresh")
        self.assertIsNotNone(age2)
        self.assertLessEqual(age2, 10.0)

        future, future_age = _is_intraday_bar_fresh(
            "2026-07-10 22:50:00",
            now=now,
            max_age_minutes=10.0,
        )
        self.assertFalse(future)
        self.assertLess(future_age, -5.0)

    def test_run_simulation_output_date_is_active_trade_date(self) -> None:
        """run_multi_style_simulation output.date is the passed active_trade_date, not natural date."""
        import tempfile
        from datetime import datetime, timezone
        from CNFutures.sim_runner import run_multi_style_simulation

        class MockReader:
            def __init__(self) -> None:
                self.intraday_dates: list[str] = []

            def get_assets(self, market: str) -> list[dict[str, object]]:
                return [
                    {"symbol": "RB2610.SHF"},
                    {"symbol": "CU2610.SHF"},
                    {"symbol": "AL2610.SHF"},
                ]

            def get_bars_intraday(
                self, *args: object, **kwargs: object
            ) -> list[dict[str, object]]:
                self.intraday_dates.append(str(args[3]))
                return [
                    {
                        "bar_time": "2026-07-10 22:30:00",
                        "close": 3500.0,
                        "volume": 100,
                        "trade_date": "20260710",
                    },
                ]

            def get_realtime_5min_batch(
                self, market: str, date: object, **kwargs: object
            ) -> list[dict[str, object]]:
                return [
                    {"symbol": "RB2610.SHF", "interval": "5min"},
                    {"symbol": "CU2610.SHF", "interval": "5min"},
                    {"symbol": "AL2610.SHF", "interval": "5min"},
                ]

        reader = MockReader()

        class MockAdapter:
            universe_filter: dict[str, object] = {}

            def __init__(self, reader: object) -> None:
                self.reader = reader
                self.universe_dates: list[str] = []

            def get_strategy_config(self) -> dict[str, object]:
                return {
                    "styles": {
                        "trend": {
                            "name": "trend",
                            "signal_threshold": 0.001,
                            "risk_per_trade": 0.03,
                            "max_margin_usage": 0.30,
                            "products": ("rb", "cu", "al"),
                            "night_session_allowed": True,
                        },
                    }
                }

            def get_sim_account(self) -> dict[str, object]:
                return {"sim_capital": 50_000.0}

            def get_intraday_universe(self, date: str, **kwargs: object) -> list[str]:
                self.universe_dates.append(date)
                return ["RB2610.SHF", "CU2610.SHF", "AL2610.SHF"]

            def get_universe(self, date: str) -> list[str]:
                return ["RB2610.SHF", "CU2610.SHF", "AL2610.SHF"]

        adapter = MockAdapter(reader)
        now = datetime(2026, 7, 10, 14, 35, tzinfo=timezone.utc)  # 22:35 CN
        active_date = "20260713"  # Friday night belongs to Monday trade date

        tmp_dir = Path(tempfile.mkdtemp())
        signals_dir = tmp_dir / "signals"
        signals_dir.mkdir(parents=True, exist_ok=True)
        review_path = tmp_dir / "review.jsonl"

        result = run_multi_style_simulation(
            adapter,
            active_date,
            reader,
            signals_dir=signals_dir,
            review_path=review_path,
            cadence="5min",
            now=now,
            max_intraday_bar_age_minutes=120.0,
        )

        self.assertEqual(
            result["date"],
            active_date,
            "Output date must be active_trade_date, not natural date",
        )
        self.assertEqual(adapter.universe_dates, ["20260710"])
        self.assertTrue(reader.intraday_dates)
        self.assertEqual(set(reader.intraday_dates), {"20260710"})

    def test_stale_bars_do_not_produce_fills_in_simulation(self) -> None:
        """When all bars are stale (beyond max_age), simulation produces no filled records."""
        import tempfile
        from datetime import datetime, timezone
        from CNFutures.sim_runner import run_multi_style_simulation

        class MockReader:
            def get_assets(self, market: str) -> list[dict[str, object]]:
                return [
                    {"symbol": "RB2610.SHF"},
                    {"symbol": "CU2610.SHF"},
                    {"symbol": "AL2610.SHF"},
                ]

            def get_bars_intraday(
                self, *args: object, **kwargs: object
            ) -> list[dict[str, object]]:
                return [
                    {
                        "bar_time": "2026-07-10 14:55:00",
                        "close": 3500.0,
                        "volume": 100,
                        "trade_date": "20260710",
                    },
                ]

            def get_realtime_5min_batch(
                self, market: str, date: object, **kwargs: object
            ) -> list[dict[str, object]]:
                return [
                    {"symbol": "RB2610.SHF", "interval": "5min"},
                    {"symbol": "CU2610.SHF", "interval": "5min"},
                    {"symbol": "AL2610.SHF", "interval": "5min"},
                ]

        reader = MockReader()

        class MockAdapter:
            universe_filter: dict[str, object] = {}

            def __init__(self, reader: object) -> None:
                self.reader = reader

            def get_strategy_config(self) -> dict[str, object]:
                return {
                    "styles": {
                        "trend": {
                            "name": "trend",
                            "signal_threshold": 0.001,
                            "risk_per_trade": 0.03,
                            "max_margin_usage": 0.30,
                            "products": ("rb", "cu", "al"),
                            "night_session_allowed": True,
                        },
                    }
                }

            def get_sim_account(self) -> dict[str, object]:
                return {"sim_capital": 50_000.0}

            def get_intraday_universe(self, date: str, **kwargs: object) -> list[str]:
                return ["RB2610.SHF", "CU2610.SHF", "AL2610.SHF"]

            def get_universe(self, date: str) -> list[str]:
                return ["RB2610.SHF", "CU2610.SHF", "AL2610.SHF"]

        adapter = MockAdapter(reader)
        now = datetime(2026, 7, 10, 14, 35, tzinfo=timezone.utc)  # 22:35 CN
        active_date = "20260713"

        tmp_dir = Path(tempfile.mkdtemp())
        signals_dir = tmp_dir / "signals"
        signals_dir.mkdir(parents=True, exist_ok=True)
        review_path = tmp_dir / "review.jsonl"

        provider_state = {
            "source": "market_capital_ledger",
            "reconciled": True,
            "fresh": True,
            "market": "cn_futures",
            "authority_id": "cn-futures-capital-v1",
            "authority_generation": 1,
            "execution_lineage_id": "cn-futures-sim-fresh-20260712-v1",
            "trade_date": active_date,
            "initial_equity_cny": 50_000.0,
            "equity_cny": 50_000.0,
            "available_margin": 25_000.0,
            "margin_utilization_limit_cny": 25_000.0,
            "margin_used_cny": 0.0,
            "unrealized_pnl_cny": 0.0,
            "event_id": "MCAP-CNF-RECONCILED",
            "event_checksum": "a" * 64,
            "cumulative_pnl": 0.0,
            "daily_realized_pnl": 0.0,
            "max_daily_loss": 1_500.0,
            "consecutive_losses": 0,
            "max_consecutive_losses": 3,
            "high_water_equity": 50_000.0,
            "max_drawdown": 3_500.0,
            "real_trading_enabled": False,
        }
        with patch(
            "CNFutures.sim_runner.get_cn_futures_capital_provider_state",
            return_value=provider_state,
        ):
            result = run_multi_style_simulation(
                adapter,
                active_date,
                reader,
                signals_dir=signals_dir,
                review_path=review_path,
                cadence="5min",
                now=now,
                max_intraday_bar_age_minutes=10.0,  # strict: only 10 min window
            )

        # Stale bars should produce 0 filled records
        self.assertEqual(
            result["filled_count"],
            0,
            "Stale bars from day session must not produce fills at night",
        )
        # Should have stale_intraday_bar errors
        stale_errors = [
            e
            for e in result.get("errors", [])
            if e.get("error") == "stale_intraday_bar"
        ]
        self.assertTrue(len(stale_errors) > 0, "Should have stale bar errors")


if __name__ == "__main__":
    unittest.main()
