#!/usr/bin/env python3
"""Tests for China futures simulated execution."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class CNFuturesSimTest(unittest.TestCase):
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
            receipt={"filled_qty": 1, "avg_price": 3490.0, "raw_response": {"estimated_close_fee": 6.0}},
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

    def test_index_intraday_directional_signal_buys_sells_and_respects_close_guard(self) -> None:
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
        close_guard_bars = [*up_bars[:-1], {"bar_time": "2026-07-06 14:55:00", "close": 3520, "volume": 1600}]

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

    def test_index_intraday_directional_signal_filters_open_gap_and_low_volatility(self) -> None:
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
            {"bar_time": "2026-07-06 09:00:00", "close": 3500, "volume": 1000, "previous_close": 3400},
            {"bar_time": "2026-07-06 09:05:00", "close": 3510, "volume": 1000, "previous_close": 3400},
            {"bar_time": "2026-07-06 09:10:00", "close": 3520, "volume": 1100, "previous_close": 3400},
            {"bar_time": "2026-07-06 09:15:00", "close": 3530, "volume": 1200, "previous_close": 3400},
            {"bar_time": "2026-07-06 09:20:00", "close": 3540, "volume": 1300, "previous_close": 3400},
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
        low_volatility = generate_style_signal("IF2601.CFFEX", low_volatility_bars, style)

        self.assertEqual(open_cooldown["action"], "hold")
        self.assertEqual(open_cooldown["reason"], "opening_cooldown")
        self.assertEqual(open_cooldown["minutes_since_open"], 14)
        self.assertEqual(gap["action"], "hold")
        self.assertEqual(gap["reason"], "opening_gap_cooldown")
        self.assertEqual(low_volatility["action"], "hold")
        self.assertEqual(low_volatility["reason"], "low_volatility_filter")

    def test_index_intraday_directional_signal_filters_choppy_reversal_and_noise(self) -> None:
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
            {"bar_time": "2026-07-06 14:25:00", "close": 3522, "high": 3535, "low": 3518, "volume": 1600},
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
        noisy = generate_style_signal("IF2601.CFFEX", noisy_bars, {**style, "min_directional_consistency": 0.0})

        self.assertEqual(choppy["action"], "hold")
        self.assertEqual(choppy["reason"], "directional_consistency_filter")
        self.assertEqual(reversal["action"], "hold")
        self.assertEqual(reversal["reason"], "intrabar_reversal_filter")
        self.assertEqual(noisy["action"], "hold")
        self.assertEqual(noisy["reason"], "signal_noise_filter")

    def test_index_intraday_directional_signal_filters_bar_quality_and_late_chase(self) -> None:
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
            {"bar_time": "2026-07-06 14:25:00", "open": 3519, "high": 3535, "low": 3515, "close": 3523, "volume": 1600},
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
            {"bar_time": "2026-07-06 14:25:00", "open": 3521, "high": 3545, "low": 3520, "close": 3544, "volume": 1800},
        ]

        gap = generate_style_signal("IF2601.CFFEX", gap_bars, style)
        long_wick = generate_style_signal("IF2601.CFFEX", long_wick_bars, style)
        not_consecutive = generate_style_signal("IF2601.CFFEX", not_consecutive_bars, {**style, "min_body_to_range_ratio": 0.0})
        chase = generate_style_signal(
            "IF2601.CFFEX",
            chase_bars,
            {**style, "min_body_to_range_ratio": 0.0, "min_consecutive_aligned_bars": 0},
        )

        self.assertEqual(gap["action"], "hold")
        self.assertEqual(gap["reason"], "bar_gap_filter")
        self.assertEqual(long_wick["action"], "hold")
        self.assertEqual(long_wick["reason"], "body_to_range_filter")
        self.assertEqual(not_consecutive["action"], "hold")
        self.assertEqual(not_consecutive["reason"], "consecutive_alignment_filter")
        self.assertEqual(chase["action"], "hold")
        self.assertEqual(chase["reason"], "late_chase_filter")

    def test_index_intraday_directional_signal_rejects_non_day_session_bars(self) -> None:
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
                "symbol": "rb2601",
                "side": "buy",
                "quantity": 2,
                "price": 3500.0,
            },
            market="cn_futures",
            account={"account": "simnow"},
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

    def test_sim_executor_models_partial_fill_and_price_limits(self) -> None:
        import CNFutures.sim_executor  # noqa: F401
        from shared.execution.sim_broker import execute_sim_order

        partial = execute_sim_order(
            order={
                "order_id": "SIM-CNF-PARTIAL",
                "symbol": "IF2601.CFFEX",
                "side": "buy",
                "quantity": 10,
                "price": 3500.0,
                "bar_volume": 20,
                "previous_close": 3500.0,
            },
            market="cn_futures",
            account={"account": "simnow"},
            config={"fee_mode": "round_trip_estimate", "volume_participation": 0.10, "slippage_bps": 1.0},
        )
        rejected = execute_sim_order(
            order={
                "order_id": "SIM-CNF-LIMIT",
                "symbol": "IF2601.CFFEX",
                "side": "buy",
                "quantity": 1,
                "price": 4000.0,
                "previous_close": 3500.0,
            },
            market="cn_futures",
            account={"account": "simnow"},
            config={},
        )

        self.assertEqual(partial.status, "partial")
        self.assertEqual(partial.filled_qty, 2)
        self.assertEqual(partial.raw_response["requested_quantity"], 10)
        self.assertEqual(partial.raw_response["fill_status"], "partial")
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(rejected.raw_response["limit_up"], 3850.0)

    def test_sim_executor_uses_order_book_quote_and_depth_quantity(self) -> None:
        import CNFutures.sim_executor  # noqa: F401
        from shared.execution.sim_broker import execute_sim_order

        result = execute_sim_order(
            order={
                "order_id": "SIM-CNF-DEPTH",
                "symbol": "rb2601",
                "side": "buy",
                "quantity": 5,
                "price": 3500.0,
                "ask_price": 3502.0,
                "ask_size": 2,
                "previous_close": 3500.0,
                "bar_volume": 1000,
            },
            market="cn_futures",
            account={"account": "simnow"},
            config={"fee_mode": "round_trip_estimate", "slippage_bps": 0.0},
        )

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.filled_qty, 2)
        self.assertEqual(result.avg_price, 3502.0)
        self.assertEqual(result.raw_response["execution_price_source"], "order_book_ask")
        self.assertEqual(result.raw_response["order_book_available_qty"], 2)
        self.assertEqual(result.raw_response["ask_price"], 3502.0)
        self.assertEqual(result.raw_response["ask_size"], 2)

    def test_sim_executor_rejects_expiring_contract_with_explicit_metadata(self) -> None:
        import CNFutures.sim_executor  # noqa: F401
        from shared.execution.sim_broker import execute_sim_order

        result = execute_sim_order(
            order={
                "order_id": "SIM-CNF-EXPIRY",
                "symbol": "rb2607",
                "side": "buy",
                "quantity": 1,
                "price": 3500.0,
                "trade_date": "20260703",
                "last_trade_date": "20260705",
                "previous_close": 3500.0,
            },
            market="cn_futures",
            account={"account": "simnow"},
            config={"rollover_min_days_to_expiry": 5},
        )

        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.raw_response["source"], "cn_futures_sim_executor_expiry_guard")
        self.assertEqual(result.raw_response["days_to_expiry"], 2)

    def test_reversal_pnl_uses_one_round_trip_fee_when_previous_fill_precharged_it(self) -> None:
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
                "raw_response": {"total_estimated_fee": 14.0, "estimated_close_fee": 7.0},
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
        self.assertEqual(health["trend"]["suggested_action"], "inspect_data_or_risk_gate")

    def test_append_review_writes_dashboard_style_outputs(self) -> None:
        from CNFutures.review import append_review

        with tempfile.TemporaryDirectory() as tmp:
            review_path = Path(tmp) / "shared" / "review" / "data" / "cn_futures_sim_reviews.jsonl"
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

            style_path = Path(payload["style_output_paths"]["style_comparison"])
            perf_path = Path(payload["style_output_paths"]["style_performance"])
            style_payload = json.loads(style_path.read_text(encoding="utf-8"))
            perf_rows = [json.loads(line) for line in perf_path.read_text(encoding="utf-8").splitlines() if line.strip()]

            self.assertTrue(style_path.exists())
            self.assertTrue(perf_path.exists())
            self.assertEqual(style_payload["market"], "cn_futures")
            self.assertEqual(style_payload["style_comparison"][0]["style_name"], "trend")
            self.assertEqual(style_payload["forward_label_summary"]["styles"]["trend"]["labeled"], 1)
            self.assertEqual(style_payload["forward_label_summary"]["styles"]["trend"]["win_rate"], 1.0)
            self.assertEqual(style_payload["style_comparison"][0]["forward_labeled_count"], 1)
            self.assertEqual(perf_rows[0]["market"], "cn_futures")
            self.assertFalse(perf_rows[0]["real_execution"])

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

        allowed_style = {"name": "trend", "signal_threshold": 0.001, "night_session_allowed": True}
        allowed_signal = generate_style_signal("rb2601", night_bars, allowed_style)
        self.assertEqual(allowed_signal["action"], "buy")
        self.assertEqual(allowed_signal["reason"], "trend_confirmed")

    def test_run_simulation_respects_kill_switch(self) -> None:
        import os
        import subprocess

        env = os.environ.copy()
        env["CN_FUTURES_SIM_DISABLED"] = "1"
        env["PYTHONPATH"] = str(ROOT)
        result = subprocess.run(
            [sys.executable, "-m", "CNFutures.run_simulation", "--json", "--date", "20260706"],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(output["state"], "paused")
        self.assertEqual(output["filled_count"], 0)
        self.assertFalse(output["real_trading_enabled"])


if __name__ == "__main__":
    unittest.main()
