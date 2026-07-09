#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.review.opportunity_funnel import read_event_rows
from shared.runtime_test.sync_opportunity_funnel_events import sync_opportunity_funnel_events


class SyncOpportunityFunnelEventsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.signals = self.root / "signals"
        for state in ("pending", "filled", "failed", "expired", "cancelled", "partial"):
            (self.signals / state).mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_card(self, state: str, name: str, payload: dict[str, object]) -> None:
        path = self.signals / state / f"{name}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_sync_builds_stage_path_from_pending_and_filled_signals(self) -> None:
        self._write_card(
            "pending",
            "P1",
            {
                "order_id": "P1",
                "symbol": "0700.HK",
                "market": "HK",
                "strategy_name": "momentum",
                "timestamp": "2026-07-09T09:41:00+08:00",
                "scored_at": "2026-07-09T09:44:00+08:00",
                "risk_checked_at": "2026-07-09T09:49:00+08:00",
                "status": "pending",
            },
        )
        self._write_card(
            "filled",
            "F1",
            {
                "order_id": "F1",
                "ts_code": "600519.SH",
                "market": "A-share",
                "strategy_name": "candidate",
                "timestamp": "2026-07-09T10:01:00+08:00",
                "scored_at": "2026-07-09T10:03:00+08:00",
                "risk_check": {"passed": True, "checked_at": "2026-07-09T10:05:00+08:00"},
                "filled_at": "2026-07-09T10:08:00+08:00",
                "status": "filled",
            },
        )

        result = sync_opportunity_funnel_events(self.root, apply=True)

        self.assertEqual(result["applied"], True)
        self.assertEqual(result["cards_reviewed"], 2)
        self.assertEqual(result["events_written"], 8)
        rows = read_event_rows(self.root)
        self.assertEqual(len(rows), 8)
        p1_rows = [row for row in rows if row["opportunity_id"] == "P1"]
        f1_rows = [row for row in rows if row["opportunity_id"] == "F1"]
        self.assertEqual([row["stage"] for row in p1_rows], ["发现", "研判", "风控", "待确认"])
        self.assertEqual([row["stage"] for row in f1_rows], ["发现", "研判", "风控", "结果"])
        self.assertEqual(f1_rows[-1]["status"], "成交")
        self.assertTrue(f1_rows[-1]["terminal"])

    def test_sync_is_idempotent_and_dry_run_does_not_write(self) -> None:
        self._write_card(
            "failed",
            "R1",
            {
                "order_id": "R1",
                "symbol": "BTC-USD",
                "market": "Crypto",
                "timestamp": "2026-07-09T09:31:00+08:00",
                "risk_checked_at": "2026-07-09T09:36:00+08:00",
                "status": "failed",
                "failure_reason": "risk cap",
            },
        )

        dry = sync_opportunity_funnel_events(self.root, apply=False)
        self.assertEqual(dry["applied"], False)
        self.assertEqual(dry["events_written"], 0)
        self.assertEqual(dry["events_planned"], 3)
        self.assertEqual(read_event_rows(self.root), [])

        first = sync_opportunity_funnel_events(self.root, apply=True)
        second = sync_opportunity_funnel_events(self.root, apply=True)

        self.assertEqual(first["events_written"], 3)
        self.assertEqual(second["events_written"], 0)
        self.assertEqual(second["events_skipped_existing"], 3)
        rows = read_event_rows(self.root)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1]["status"], "拦截")


if __name__ == "__main__":
    unittest.main()
