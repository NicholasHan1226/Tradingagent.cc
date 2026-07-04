from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from CNFutures.adapter import CNFuturesAdapter
from CNFutures.evolution import evaluate_styles, style_weights_path
from shared.markets.performance_tracker import save_run


BASE_STYLE = {
    "name": "trend",
    "description": "unit test trend",
    "signal_threshold": 0.01,
    "risk_per_trade": 0.03,
    "max_margin_usage": 0.30,
}


class CNFuturesEvolutionTest(unittest.TestCase):
    def _write_style(self, styles_dir: Path, name: str, payload: dict[str, object] | None = None) -> Path:
        styles_dir.mkdir(parents=True, exist_ok=True)
        data = {**BASE_STYLE, "name": name, **(payload or {})}
        path = styles_dir / f"{name}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def test_evolution_writes_runtime_overlay_without_mutating_checked_in_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            styles_dir = root / "strategies"
            review_root = root / "review"
            style_path = self._write_style(styles_dir, "trend")
            original = style_path.read_text(encoding="utf-8")

            save_run("trend", "cn_futures", {"date": "20260703", "pnl": 2.0, "win_rate": 0.7, "max_dd": 0.01, "sharpe": 1.5, "trades": 2}, review_root=review_root)
            save_run("trend", "cn_futures", {"date": "20260704", "pnl": 3.0, "win_rate": 0.8, "max_dd": 0.01, "sharpe": 2.0, "trades": 3}, review_root=review_root)

            result = evaluate_styles(strategy_dir=styles_dir, review_root=review_root, min_trades=1)

            self.assertEqual(style_path.read_text(encoding="utf-8"), original)
            self.assertEqual(result["state"], "adjusted")
            self.assertFalse(result["real_execution"])
            self.assertTrue((review_root / "cn_futures/evolution_plan.json").exists())
            self.assertTrue((review_root / "cn_futures/evolution_log.jsonl").exists())
            self.assertTrue((review_root / "cn_futures/style_weights.json").exists())
            generated = sorted((review_root / "cn_futures/generated_styles").glob("trend_g2_*.json"))
            self.assertEqual(len(generated), 1)
            variant = json.loads(generated[0].read_text(encoding="utf-8"))
            self.assertEqual(variant["parent_style"], "trend")
            self.assertEqual(variant["capital_layer"], "simulated")
            self.assertFalse(variant["real_trading_enabled"])

    def test_adapter_loads_generated_styles_and_weight_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            styles_dir = root / "strategies"
            review_root = root / "review"
            self._write_style(styles_dir, "trend")
            generated_dir = review_root / "cn_futures/generated_styles"
            generated_dir.mkdir(parents=True)
            generated = {**BASE_STYLE, "name": "trend_g2_20260704", "parent_style": "trend", "weight": 0.05}
            (generated_dir / "trend_g2_20260704.json").write_text(json.dumps(generated), encoding="utf-8")
            weights = {
                "market": "cn_futures",
                "styles": {
                    "trend": {"status": "paused", "enabled": False, "weight": 0.05, "evolution_action": "pause"},
                    "trend_g2_20260704": {"status": "active", "enabled": True, "weight": 0.95, "evolution_action": "variant_generated"},
                },
            }
            style_weights_path(review_root).parent.mkdir(parents=True, exist_ok=True)
            style_weights_path(review_root).write_text(json.dumps(weights), encoding="utf-8")

            with patch.dict(os.environ, {"CN_FUTURES_REVIEW_ROOT": str(review_root)}):
                adapter = CNFuturesAdapter(reader=None, strategy_dir=styles_dir)
                styles = adapter.get_strategy_config()["styles"]

            self.assertEqual(styles["trend"]["status"], "paused")
            self.assertFalse(styles["trend"]["enabled"])
            self.assertEqual(styles["trend_g2_20260704"]["status"], "active")
            self.assertEqual(styles["trend_g2_20260704"]["weight"], 0.95)

    def test_blocked_style_is_paused_by_runtime_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            styles_dir = root / "strategies"
            review_root = root / "review"
            self._write_style(styles_dir, "trend")
            comparison = {
                "market": "cn_futures",
                "style_states": [
                    {"style_name": "trend", "status": "blocked", "suggested_action": "inspect_data_or_risk_gate"}
                ],
            }
            comparison_path = review_root / "cn_futures/style_comparison.json"
            comparison_path.parent.mkdir(parents=True)
            comparison_path.write_text(json.dumps(comparison), encoding="utf-8")

            result = evaluate_styles(strategy_dir=styles_dir, review_root=review_root, min_trades=20)

            self.assertEqual(result["weights"]["trend"]["status"], "paused")
            self.assertFalse(result["weights"]["trend"]["enabled"])
            self.assertEqual(result["weights"]["trend"]["evolution_action"], "pause")

    def test_index_intraday_directional_variant_preserves_style_framework(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            styles_dir = root / "strategies"
            review_root = root / "review"
            self._write_style(
                styles_dir,
                "index_intraday_directional",
                {
                    "style_family": "index_intraday_directional",
                    "products": ["if", "ih", "ic", "im"],
                    "signal_threshold": 0.0025,
                    "risk_per_trade": 0.01,
                    "max_margin_usage": 0.08,
                    "momentum_lookback_bars": 3,
                    "moving_average_bars": 6,
                    "prediction_horizon_bars": 3,
                    "no_overnight": True,
                    "day_session_only": True,
                    "flatten_before_session_close_minutes": 10,
                },
            )
            save_run("index_intraday_directional", "cn_futures", {"date": "20260703", "pnl": 2.0, "win_rate": 0.7, "max_dd": 0.01, "sharpe": 1.5, "trades": 2}, review_root=review_root)
            save_run("index_intraday_directional", "cn_futures", {"date": "20260704", "pnl": 3.0, "win_rate": 0.8, "max_dd": 0.01, "sharpe": 2.0, "trades": 3}, review_root=review_root)

            result = evaluate_styles(strategy_dir=styles_dir, review_root=review_root, min_trades=1)

            self.assertEqual(result["state"], "adjusted")
            generated = sorted((review_root / "cn_futures/generated_styles").glob("index_intraday_directional_g2_*.json"))
            self.assertEqual(len(generated), 1)
            variant = json.loads(generated[0].read_text(encoding="utf-8"))
            self.assertEqual(variant["style_family"], "index_intraday_directional")
            self.assertEqual(variant["products"], ["if", "ih", "ic", "im"])
            self.assertTrue(variant["no_overnight"])
            self.assertTrue(variant["day_session_only"])
            self.assertFalse(variant["real_trading_enabled"])


if __name__ == "__main__":
    unittest.main()
