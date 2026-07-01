from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from shared.adversarial import bull_bear_debate


class BullBearDebateFastModeTest(unittest.TestCase):
    def test_fast_mode_does_not_call_deepseek(self) -> None:
        scores = {"combined": 0.68, "macro": 0.7, "event": 0.3, "technical": 0.61}
        with patch.dict(os.environ, {"TRADINGS_DEBATE_MODE": "fast"}, clear=False):
            with patch.object(bull_bear_debate, "_call_deepseek", side_effect=AssertionError("should not call llm")):
                result = bull_bear_debate.debate("600519.SH", scores)
        self.assertEqual(result["source"], "fast_debate")
        self.assertAlmostEqual(result["belief_score"], 0.68)
        self.assertIn("macro", result["bull_case"])
        self.assertIn("event", result["bear_case"])


if __name__ == "__main__":
    unittest.main()
