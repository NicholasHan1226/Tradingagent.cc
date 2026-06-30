from __future__ import annotations

import unittest

from shared.screening.condition_generator import _dedup_queue, _sweep_expired
from shared.screening.condition_monitor import trigger_replay


class ConditionLifecycleTest(unittest.TestCase):
    def test_sweep_expired_removes_stale_conditions(self) -> None:
        conditions = [
            {"ts_code": "AAA", "type": "breakout", "valid_until": "20260629"},
            {"ts_code": "BBB", "type": "pullback", "valid_until": "20260702"},
        ]

        swept = _sweep_expired(conditions, "20260630")

        self.assertEqual(swept, [{"ts_code": "BBB", "type": "pullback", "valid_until": "20260702"}])

    def test_dedup_queue_keeps_latest_symbol_type_condition(self) -> None:
        conditions = [
            {"ts_code": "AAA", "type": "breakout", "date": "20260629", "valid_until": "20260701", "trigger_price": 10.0},
            {"ts_code": "AAA", "type": "breakout", "date": "20260630", "valid_until": "20260703", "trigger_price": 10.5},
            {"ts_code": "AAA", "type": "pullback", "date": "20260630", "valid_until": "20260703", "trigger_price": 9.8},
        ]

        deduped = _dedup_queue(conditions)

        self.assertEqual(len(deduped), 2)
        breakout = next(item for item in deduped if item["type"] == "breakout")
        self.assertEqual(breakout["trigger_price"], 10.5)

    def test_trigger_replay_marks_breakout_fillable_when_trigger_price_inside_bar(self) -> None:
        conditions = [
            {
                "ts_code": "AAA",
                "type": "breakout",
                "trigger_price": 10.0,
                "valid_until": "20260703",
                "scores": {"combined": 0.8},
                "description": "突破10元",
            }
        ]
        bars_map = {
            "AAA": [
                {"trade_time": "2026-06-30T09:35:00", "open": 9.9, "high": 10.1, "low": 9.95, "close": 10.05},
            ]
        }

        replay = trigger_replay(conditions, date="20260630", bars_map=bars_map)

        self.assertEqual(len(replay), 1)
        self.assertTrue(replay[0]["replay_fillable"])
        self.assertEqual(replay[0]["replay_status"], "filled")
        self.assertEqual(replay[0]["replay_fill_price"], 10.0)


if __name__ == "__main__":
    unittest.main()
