from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from shared.markets.evolution_engine import evaluate_and_adjust
from shared.markets.performance_tracker import save_run
from shared.markets.style_runner import StyleRunner


STYLE = {
    "name": "balanced",
    "position_pct": 0.1,
    "stop_loss_pct": -0.08,
    "take_profit_pct": 0.12,
    "max_hold_days": 5,
    "pyramid": False,
    "scale_in_steps": 1,
    "conviction_min": 0.3,
    "description": "unit test style",
    "status": "active",
    "weight": 1.0,
    "generation": 1,
}


class NoopSimulator:
    def simulate(self, order, account):
        return {
            "status": "filled",
            "market": order["market"],
            "symbol": order["symbol"],
            "side": order["side"],
            "filled_qty": order["quantity"],
            "avg_price": order["price"],
            "fee": 0.0,
            "order_id": order["order_id"],
            "capital_layer": "simulated",
            "account_type": "simulated",
            "real_execution": False,
        }


class EvolutionRuntimeStylesTest(unittest.TestCase):
    def test_evolution_keeps_checked_in_styles_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            styles_dir = root / "styles"
            review_root = root / "review"
            styles_dir.mkdir()
            style_path = styles_dir / "balanced.json"
            original = json.dumps(STYLE, ensure_ascii=False, indent=2) + "\n"
            style_path.write_text(original, encoding="utf-8")

            save_run(
                "balanced",
                "crypto",
                {
                    "date": "20260703",
                    "pnl": 1.0,
                    "win_rate": 0.7,
                    "max_dd": 0.02,
                    "sharpe": 1.1,
                    "trades": 2,
                    "avg_hold_hours": 10,
                },
                review_root=review_root,
            )
            save_run(
                "balanced",
                "crypto",
                {
                    "date": "20260704",
                    "pnl": 3.0,
                    "win_rate": 0.8,
                    "max_dd": 0.01,
                    "sharpe": 1.8,
                    "trades": 3,
                    "avg_hold_hours": 8,
                },
                review_root=review_root,
            )

            result = evaluate_and_adjust("crypto", review_root=review_root, styles_dir=styles_dir)

            self.assertEqual(style_path.read_text(encoding="utf-8"), original)
            self.assertEqual(result["state"], "adjusted")
            generated_dir = review_root / "crypto" / "generated_styles"
            generated = sorted(generated_dir.glob("balanced_g2_*.json"))
            self.assertEqual(len(generated), 1)

            weights = json.loads((review_root / "crypto" / "style_weights.json").read_text(encoding="utf-8"))
            self.assertIn("balanced", weights["styles"])
            self.assertIn(json.loads(generated[0].read_text(encoding="utf-8"))["name"], weights["styles"])

    def test_style_runner_loads_runtime_generated_styles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            styles_dir = root / "styles"
            review_root = root / "review"
            generated_dir = review_root / "crypto" / "generated_styles"
            styles_dir.mkdir()
            generated_dir.mkdir(parents=True)
            (styles_dir / "balanced.json").write_text(json.dumps(STYLE), encoding="utf-8")
            variant = {**STYLE, "name": "balanced_g2_20260704", "generation": 2, "weight": 0.02}
            (generated_dir / "balanced_g2_20260704.json").write_text(json.dumps(variant), encoding="utf-8")

            runner = StyleRunner("crypto", NoopSimulator(), styles_dir=styles_dir, review_root=review_root)
            styles = runner._load_weighted_styles()

            self.assertEqual({style.name for style in styles}, {"balanced", "balanced_g2_20260704"})


if __name__ == "__main__":
    unittest.main()
