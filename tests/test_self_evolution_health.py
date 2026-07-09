from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from shared.runtime_test.self_evolution_health import evaluate_self_evolution_health


class SelfEvolutionHealthTest(unittest.TestCase):
    def _write_jsonl(self, path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def test_flags_strategy_samples_missing_from_evolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_jsonl(
                root / "ashare" / "evolution_log.jsonl",
                {
                    "generated_at": "2026-07-09T01:00:00+00:00",
                    "state": "observed",
                    "actions": [],
                    "rankings": [{"style_name": "balanced", "trades": 0, "pnl": 0}],
                    "weights": {"balanced": {"status": "active", "weight": 1.0}},
                },
            )

            report = evaluate_self_evolution_health(
                review_root=root,
                markets=["ashare"],
                pnl_summary={
                    "ashare": {
                        "total_pnl": -149.13,
                        "sample_quality": {"strategy_sample_valid_count": 2},
                    }
                },
            )

            self.assertEqual(report["overall_status"], "warn")
            self.assertEqual(report["issues"], [{"market": "ashare", "issue": "strategy_samples_not_seen_by_evolution"}])

    def test_flags_cn_futures_action_after_weight_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_jsonl(
                root / "cn_futures" / "evolution_log.jsonl",
                {
                    "generated_at": "2026-07-09T07:46:00+00:00",
                    "state": "observed",
                    "actions": [
                        {"style_name": "trend", "action": "observe", "after": {"status": "active", "weight": 0.05}}
                    ],
                    "rankings": [{"style_name": "trend", "trades": 0, "pnl": 0}],
                    "weights": {"trend": {"status": "active", "weight": 1.0}},
                },
            )

            report = evaluate_self_evolution_health(
                review_root=root,
                markets=["cn_futures"],
                pnl_summary={"cn_futures": {"total_pnl": 0.0}},
            )

            self.assertEqual(report["overall_status"], "warn")
            self.assertEqual(report["markets"][0]["issues"], ["action_after_weight_mismatch"])
            self.assertEqual(report["markets"][0]["weight_mismatches"][0]["style_name"], "trend")

    def test_passes_when_samples_and_evolution_are_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_jsonl(
                root / "us" / "evolution_log.jsonl",
                {
                    "generated_at": "2026-07-09T01:00:00+00:00",
                    "state": "adjusted",
                    "actions": [{"style_name": "trend", "action": "promote", "after": {"weight": 1.0}}],
                    "rankings": [{"style_name": "trend", "trades": 3, "pnl": 1.5}],
                    "weights": {"trend": {"status": "active", "weight": 1.0}},
                },
            )

            report = evaluate_self_evolution_health(
                review_root=root,
                markets=["us"],
                pnl_summary={"us": {"total_pnl": 1.5}},
            )

            self.assertEqual(report["overall_status"], "pass")
            self.assertTrue(report["markets"][0]["positive_evolution_proven"])


if __name__ == "__main__":
    unittest.main()
