from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from CNFutures.calibration import build_calibration_report, label_signal_card


class FakeCalibrationReader:
    rows: list[dict[str, object]] = []

    def get_bars_intraday(
        self,
        market: str,
        symbol: str,
        interval: str = "5min",
        start: object = None,
        end: object = None,
    ) -> list[dict[str, object]]:
        if market == "Futures" and symbol == "IF2601.CFFEX" and interval == "5min":
            return list(self.rows)
        return []


class CNFuturesCalibrationTest(unittest.TestCase):
    def _card(self) -> dict[str, object]:
        return {
            "order_id": "SIM-CNF-index_intraday_directional-IF2601.CFFEX-202607061430",
            "market": "cn_futures",
            "symbol": "IF2601.CFFEX",
            "strategy_name": "index_intraday_directional",
            "side": "buy",
            "price": 3520.0,
            "filled_price": 3520.0,
            "bar_time": "2026-07-06 14:30:00",
            "valid_until": "20260706",
            "signal": {
                "prediction_horizon_bars": 3,
                "scenario_tags": {
                    "session": "day",
                    "time_bucket": "day_late",
                    "direction": "buy",
                    "volatility_bucket": "normal",
                    "volume_bucket": "strong",
                    "signal_strength_bucket": "confirmed",
                },
                "exit_plan": {
                    "prediction_horizon_bars": 3,
                    "time_stop_bars": 2,
                    "stop_loss_pct": 0.004,
                    "take_profit_pct": 0.006,
                },
            },
        }

    def test_label_signal_card_scores_future_bars(self) -> None:
        rows = [
            {"bar_time": "2026-07-06 14:30:00", "close": 3520.0},
            {"bar_time": "2026-07-06 14:35:00", "close": 3530.0},
            {"bar_time": "2026-07-06 14:40:00", "close": 3543.0},
            {"bar_time": "2026-07-06 14:45:00", "close": 3545.0},
        ]

        label = label_signal_card(self._card(), rows)

        outcome = label["forward_outcome"]
        self.assertEqual(outcome["status"], "labeled")
        self.assertTrue(outcome["direction_correct"])
        self.assertTrue(outcome["time_stop_positive"])
        self.assertTrue(outcome["take_profit_hit"])
        self.assertFalse(outcome["stop_loss_hit"])
        self.assertEqual(label["scenario_tags"]["product"], "if")

    def test_build_calibration_report_writes_labels_and_pending_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            signals = root / "signals"
            filled = signals / "filled"
            filled.mkdir(parents=True)
            card = self._card()
            (filled / f"{card['order_id']}.json").write_text(json.dumps(card), encoding="utf-8")
            labels_path = root / "review/cn_futures/forward_labels.jsonl"
            report = build_calibration_report(
                date="20260706",
                reader=FakeCalibrationReader(),
                signals_dir=signals,
                review_path=root / "review/data/cn_futures_sim_reviews.jsonl",
                labels_path=labels_path,
            )

            self.assertEqual(report["signal_card_count"], 1)
            self.assertEqual(report["pending_count"], 1)
            self.assertEqual(report["labeled_count"], 0)
            rows = [json.loads(line) for line in labels_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(rows[0]["forward_outcome"]["status"], "pending_future_bars")
            self.assertFalse(report["real_trading_enabled"])


if __name__ == "__main__":
    unittest.main()
