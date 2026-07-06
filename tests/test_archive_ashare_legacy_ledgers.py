from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from shared.runtime_test.archive_ashare_legacy_ledgers import archive_legacy_ashare_ledgers


class ArchiveAshareLegacyLedgersTest(unittest.TestCase):
    def test_archive_moves_legacy_styles_but_keeps_canonical_ashare_sim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_root = root / "sim_ledger" / "ashare"
            archive_root = root / "archive"
            legacy = ledger_root / "aggressive"
            canonical = ledger_root / "ashare_sim"
            legacy.mkdir(parents=True)
            canonical.mkdir(parents=True)
            (legacy / "positions.json").write_text("{}", encoding="utf-8")
            (canonical / "positions.json").write_text("{}", encoding="utf-8")

            dry_run = archive_legacy_ashare_ledgers(
                ledger_root=ledger_root,
                archive_root=archive_root,
                batch_id="B1",
                apply=False,
            )
            self.assertEqual(dry_run["candidate_count"], 1)
            self.assertTrue(legacy.exists())

            result = archive_legacy_ashare_ledgers(
                ledger_root=ledger_root,
                archive_root=archive_root,
                batch_id="B1",
                apply=True,
            )

            self.assertEqual(result["moved_count"], 1)
            self.assertFalse(legacy.exists())
            self.assertTrue(canonical.exists())
            archived = archive_root / "B1" / "aggressive" / "positions.json"
            self.assertTrue(archived.exists())
            manifest = json.loads((archive_root / "B1" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["canonical_style"], "ashare_sim")


if __name__ == "__main__":
    unittest.main()
