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

from shared.runtime_test import archive_reviewed_signals


class ArchiveReviewedSignalsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.signals = self.root / "signals"
        for state in ["pending", "claimed", "running", "failed", "expired"]:
            (self.signals / state).mkdir(parents=True)
        self.old_root = archive_reviewed_signals.ROOT
        self.old_signals = archive_reviewed_signals.SIGNALS
        self.old_ops = archive_reviewed_signals.OPS_REVIEW
        archive_reviewed_signals.ROOT = self.root
        archive_reviewed_signals.SIGNALS = self.signals
        archive_reviewed_signals.OPS_REVIEW = self.root / "shared" / "review" / "ops"

    def tearDown(self) -> None:
        archive_reviewed_signals.ROOT = self.old_root
        archive_reviewed_signals.SIGNALS = self.old_signals
        archive_reviewed_signals.OPS_REVIEW = self.old_ops
        self.tmp.cleanup()

    def test_archive_moves_failed_and_expired_with_manifest(self) -> None:
        (self.signals / "failed" / "f.json").write_text(json.dumps({"order_id": "F1", "receipt": {"message": "unconfirmed"}}), encoding="utf-8")
        (self.signals / "expired" / "e.json").write_text(json.dumps({"order_id": "E1", "status": "expired"}), encoding="utf-8")

        result = archive_reviewed_signals.archive_reviewed("BATCH", "unit reviewed", True)

        self.assertTrue(result["applied"])
        self.assertEqual(result["record_count"], 2)
        self.assertFalse((self.signals / "failed" / "f.json").exists())
        self.assertFalse((self.signals / "expired" / "e.json").exists())
        self.assertTrue((self.signals / "reviewed" / "BATCH" / "failed" / "f.json").exists())
        self.assertTrue((self.signals / "reviewed" / "BATCH" / "expired" / "e.json").exists())
        self.assertTrue((self.signals / "reviewed" / "BATCH" / "manifest.json").exists())

    def test_archive_refuses_when_active_queue_exists(self) -> None:
        (self.signals / "pending" / "p.json").write_text("{}", encoding="utf-8")
        (self.signals / "failed" / "f.json").write_text("{}", encoding="utf-8")

        with self.assertRaises(SystemExit):
            archive_reviewed_signals.archive_reviewed("BATCH", "blocked", True)


if __name__ == "__main__":
    unittest.main()
