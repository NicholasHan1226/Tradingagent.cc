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

from shared.review.opportunity_funnel import (
    append_opportunity_event,
    event_log_path,
    normalize_event_row,
    read_event_rows,
)


class OpportunityFunnelEventsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_append_normalizes_stage_status_and_writes_frontend_read_path(self) -> None:
        row = append_opportunity_event(
            self.root,
            opportunity_id="opp-0700-001",
            symbol="0700.HK",
            market="HK",
            stage="research",
            status="passed",
            label="理由成立",
            timestamp="2026-07-09T09:42:00+08:00",
            latency_minutes=4,
            metadata={"source": "unit"},
        )

        self.assertEqual(row["stage"], "研判")
        self.assertEqual(row["status"], "通过")
        self.assertEqual(row["source"], "opportunity_funnel_writer")
        self.assertEqual(row["sequence"], 2)
        self.assertEqual(row["latency_minutes"], 4)
        self.assertEqual(row["metadata"]["source"], "unit")

        path = event_log_path(self.root)
        self.assertEqual(path, self.root / "shared" / "review" / "opportunities" / "funnel_events.jsonl")
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["event_id"], row["event_id"])

    def test_normalize_terminal_defaults_for_result_and_blocked_status(self) -> None:
        result = normalize_event_row(
            {
                "opportunity_id": "opp-btc-001",
                "symbol": "BTC-USD",
                "market": "Crypto",
                "stage": "filled",
                "status": "filled",
                "timestamp": "2026-07-09T10:00:00+08:00",
            }
        )
        blocked = normalize_event_row(
            {
                "opportunity_id": "opp-btc-002",
                "symbol": "BTC-USD",
                "market": "Crypto",
                "stage": "risk",
                "status": "rejected",
                "timestamp": "2026-07-09T10:01:00+08:00",
            }
        )

        self.assertEqual(result["stage"], "结果")
        self.assertEqual(result["status"], "成交")
        self.assertTrue(result["terminal"])
        self.assertEqual(blocked["stage"], "风控")
        self.assertEqual(blocked["status"], "拦截")
        self.assertTrue(blocked["terminal"])

    def test_read_event_rows_skips_malformed_jsonl(self) -> None:
        path = event_log_path(self.root)
        path.parent.mkdir(parents=True)
        path.write_text(
            '{"event_id":"ok","symbol":"AAPL.US","market":"US","stage":"发现","status":"进入"}\n'
            'not-json\n'
            '{"symbol":"MSFT.US","market":"US","stage":"结果","status":"成交"}\n',
            encoding="utf-8",
        )

        rows = read_event_rows(self.root)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["event_id"], "ok")
        self.assertEqual(rows[1]["symbol"], "MSFT.US")


if __name__ == "__main__":
    unittest.main()
