from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shared.markets.performance_tracker import compact_history, compare_styles, load_history, save_run


class PerformanceTrackerDedupeTest(unittest.TestCase):
    def test_save_run_upserts_same_market_style_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            review_root = Path(tmp) / "review"

            save_run(
                "mean_reversion",
                "crypto",
                {"date": "20260705", "pnl": 1.0, "win_rate": 0.5, "max_dd": 0.02, "sharpe": 1.0, "trades": 2},
                review_root=review_root,
            )
            save_run(
                "mean_reversion",
                "crypto",
                {"date": "20260705", "pnl": 3.0, "win_rate": 0.75, "max_dd": 0.01, "sharpe": 2.0, "trades": 4},
                review_root=review_root,
            )

            rows = load_history("crypto", review_root=review_root)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].pnl, 3.0)
            self.assertEqual(rows[0].trades, 4)

            rankings = compare_styles("crypto", review_root=review_root)
            self.assertEqual(rankings[0]["runs"], 1)
            self.assertEqual(rankings[0]["pnl"], 3.0)
            self.assertEqual(rankings[0]["trades"], 4)

    def test_compact_history_removes_existing_duplicate_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            review_root = Path(tmp) / "review"
            path = review_root / "crypto" / "style_performance.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(
                "\n".join(
                    [
                        '{"style_name":"grid","market":"crypto","date":"20260705","pnl":-1,"win_rate":0,"max_dd":0.1,"sharpe":-1,"trades":1}',
                        '{"style_name":"grid","market":"crypto","date":"20260705","pnl":2,"win_rate":1,"max_dd":0.01,"sharpe":2,"trades":3}',
                        '{"style_name":"grid","market":"crypto","date":"20260706","pnl":4,"win_rate":1,"max_dd":0.01,"sharpe":2,"trades":5}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            result = compact_history("crypto", review_root=review_root)

            self.assertEqual(result["before"], 3)
            self.assertEqual(result["after"], 2)
            self.assertEqual(result["removed"], 1)
            rankings = compare_styles("crypto", review_root=review_root)
            self.assertEqual(rankings[0]["runs"], 2)
            self.assertEqual(rankings[0]["pnl"], 6.0)
            self.assertEqual(rankings[0]["trades"], 8)


if __name__ == "__main__":
    unittest.main()
