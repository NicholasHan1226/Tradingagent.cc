from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shared.execution.auto_pipeline import AutoPipeline


class AutoPipelineSmokeTest(unittest.TestCase):
    def test_import_init_and_review_stage_no_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = AutoPipeline(
                reader=object(),
                decision_engine=object(),
                fundamental_analyzer=object(),
                perspective_analyzer=object(),
                simulator_factory=lambda market: object(),
                evolution_fn=lambda market, review_root=None: {"state": "ok", "market": market},
                review_root=Path(tmp),
                max_candidates=1,
            )

            result = pipeline.run(trade_date="20260704", markets=["crypto"], stage="daily_review")

            self.assertEqual(result["capital_layer"], "simulated")
            self.assertEqual(result["markets"][0]["stages"]["daily_review"]["state"], "ok")


if __name__ == "__main__":
    unittest.main()
