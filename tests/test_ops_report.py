#!/usr/bin/env python3
"""Tests for Tradings operations report."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.runtime_test import ops_report
from shared.notify.email_templates import daily_report, weekly_report


class OpsReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.signals = self.root / "signals"
        for state in ("pending", "claimed", "running", "filled", "failed", "expired", "cancelled"):
            (self.signals / state).mkdir(parents=True)
        self.old_root = ops_report.ROOT
        self.old_signals = ops_report.SIGNALS
        self.old_out = ops_report.OUT_DIR
        self.old_latest = ops_report.LATEST
        self.old_history = ops_report.HISTORY
        ops_report.ROOT = self.root
        ops_report.SIGNALS = self.signals
        ops_report.OUT_DIR = self.root / "shared" / "review" / "ops"
        ops_report.LATEST = ops_report.OUT_DIR / "tradings_ops_latest.json"
        ops_report.HISTORY = ops_report.OUT_DIR / "tradings_ops_history.jsonl"

    def tearDown(self) -> None:
        ops_report.ROOT = self.old_root
        ops_report.SIGNALS = self.old_signals
        ops_report.OUT_DIR = self.old_out
        ops_report.LATEST = self.old_latest
        ops_report.HISTORY = self.old_history
        self.tmp.cleanup()

    def _write_card(self, state: str, name: str, payload: dict[str, object]) -> None:
        (self.signals / state / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_queue_summary_groups_by_market_and_state(self) -> None:
        self._write_card("pending", "a.json", {"ts_code": "600000.SH"})
        self._write_card("pending", "p.json", {"market": "pm", "symbol": "event-1"})
        self._write_card("filled", "u.json", {"market": "US", "symbol": "AAPL"})

        summary = ops_report.queue_summary()

        self.assertEqual(summary["totals"]["pending"], 2)
        self.assertEqual(summary["totals"]["filled"], 1)
        self.assertEqual(summary["by_market"]["ashare"]["pending"], 1)
        self.assertEqual(summary["by_market"]["pm"]["pending"], 1)
        self.assertEqual(summary["by_market"]["us"]["filled"], 1)

    def test_shadow_queue_summary_groups_shadow_cards(self) -> None:
        (self.signals / "shadow" / "pending").mkdir(parents=True)
        (self.signals / "shadow" / "failed").mkdir(parents=True)
        (self.signals / "shadow" / "pending" / "s1.json").write_text(json.dumps({"market": "pm"}), encoding="utf-8")
        (self.signals / "shadow" / "failed" / "s2.json").write_text(json.dumps({"ts_code": "600000.SH"}), encoding="utf-8")

        summary = ops_report.shadow_queue_summary()

        self.assertEqual(summary["totals"]["pending"], 1)
        self.assertEqual(summary["totals"]["failed"], 1)
        self.assertEqual(summary["by_market"]["pm"]["pending"], 1)
        self.assertEqual(summary["by_market"]["ashare"]["failed"], 1)

    def test_failure_review_classifies_existing_failed_cards(self) -> None:
        self._write_card("failed", "bad_code.json", {"market": "ashare", "receipt": {"message": "不支持的A股代码段"}})
        self._write_card("failed", "unconfirmed.json", {"market": "ashare", "receipt": {"confirmation_status": "unconfirmed"}})
        self._write_card("expired", "old.json", {"market": "crypto", "status": "expired"})

        review = ops_report.failure_review()

        self.assertEqual(review["by_category"]["code_unsupported"], 1)
        self.assertEqual(review["by_category"]["confirmation_unverified"], 1)
        self.assertEqual(review["by_category"]["expired"], 1)
        self.assertEqual(review["by_market"]["ashare"]["code_unsupported"], 1)

    def test_receipt_integrity_counts_signed_unsigned_and_invalid(self) -> None:
        path = self.root / "receipts.jsonl"
        signed = {"order_id": "1", "status": "filled"}
        signed["payload_sha256"] = ops_report.payload_sha256(signed, drop_checksums=True)
        invalid = {"order_id": "2", "status": "filled", "payload_sha256": "bad"}
        unsigned = {"order_id": "3", "status": "failed"}
        path.write_text("\n".join(json.dumps(x) for x in [signed, invalid, unsigned]) + "\n", encoding="utf-8")

        report = ops_report.receipt_integrity([path])

        self.assertEqual(report["total"], 3)
        self.assertEqual(report["signed"], 1)
        self.assertEqual(report["invalid"], 1)
        self.assertEqual(report["unsigned"], 1)

    def test_email_templates_render_optional_ops_section(self) -> None:
        ops = {
            "ops_status": "warn",
            "ops_queue_summary": {"pending": 1, "running": 0, "failed": 2, "expired": 0},
            "ops_receipt_integrity": {"total": 3, "unsigned": 1, "invalid": 0},
            "ops_shadow_queue_summary": {"pending": 4, "running": 0, "failed": 0, "expired": 0},
            "ops_failure_summary": {"timeout": 2},
        }
        daily_html = daily_report.render({"date": "2026-07-01", **ops})
        weekly_html = weekly_report.render({"week_range": "2026-06-29 ~ 2026-07-03", **ops})

        self.assertIn("运行状态", daily_html)
        self.assertIn("timeout:2", daily_html)
        self.assertIn("影子队列", daily_html)
        self.assertIn("运行状态", weekly_html)


if __name__ == "__main__":
    unittest.main()
