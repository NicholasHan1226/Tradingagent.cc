from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.accounting import capital_ledger, position_ledger


def _patch_capital_paths(testcase: unittest.TestCase, root: Path) -> None:
    ledger_dir = root / "capital"
    capital_csv = ledger_dir / "capital_ledger.csv"
    for name, value in (
        ("LEDGER_DIR", ledger_dir),
        ("CAPITAL_CSV", capital_csv),
        ("CAPITAL_LOCK", capital_csv.with_suffix(".csv.lock")),
    ):
        patcher = patch.object(capital_ledger, name, value)
        patcher.start()
        testcase.addCleanup(patcher.stop)


def _patch_position_paths(testcase: unittest.TestCase, root: Path) -> None:
    ledger_dir = root / "position"
    position_csv = ledger_dir / "position_ledger.csv"
    for name, value in (
        ("LEDGER_DIR", ledger_dir),
        ("POSITION_CSV", position_csv),
        ("POSITION_LOCK", position_csv.with_suffix(".csv.lock")),
    ):
        patcher = patch.object(position_ledger, name, value)
        patcher.start()
        testcase.addCleanup(patcher.stop)


def _table_count(db_path: Path, table_name: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])
    finally:
        conn.close()


class LedgerIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_capital_ledger_uses_three_physical_sqlite_tables(self) -> None:
        _patch_capital_paths(self, self.root)

        capital_ledger.record_deposit(100.0, "2026-06-30T09:30:00", capital_layer="real")
        capital_ledger.record_deposit(200.0, "2026-06-30T09:31:00", capital_layer="simulated")
        capital_ledger.record_deposit(300.0, "2026-06-30T09:32:00", capital_layer="shadow")

        db_path = self.root / "capital" / "capital_ledger.sqlite3"
        self.assertEqual(_table_count(db_path, "capital_ledger_real"), 1)
        self.assertEqual(_table_count(db_path, "capital_ledger_simulated"), 1)
        self.assertEqual(_table_count(db_path, "capital_ledger_shadow"), 1)
        self.assertEqual(capital_ledger.get_cash_position(), 100.0)
        self.assertEqual(capital_ledger.get_capital_balance()["capital_layer"], "real")
        self.assertEqual(capital_ledger.get_cash_position(capital_layer="all"), 600.0)

    def test_position_ledger_uses_three_physical_sqlite_tables(self) -> None:
        _patch_position_paths(self, self.root)

        position_ledger.open_position("600000.SH", 100, 10.0, capital_layer="real")
        position_ledger.open_position("600000.SH", 200, 10.0, capital_layer="simulated")
        position_ledger.open_position("600000.SH", 300, 10.0, capital_layer="shadow")

        db_path = self.root / "position" / "position_ledger.sqlite3"
        self.assertEqual(_table_count(db_path, "position_ledger_real"), 1)
        self.assertEqual(_table_count(db_path, "position_ledger_simulated"), 1)
        self.assertEqual(_table_count(db_path, "position_ledger_shadow"), 1)

        default_positions = position_ledger.get_positions()
        self.assertEqual(len(default_positions), 1)
        self.assertEqual(default_positions[0]["capital_layer"], "real")
        self.assertEqual(default_positions[0]["quantity"], 100)

        all_positions = position_ledger.get_positions(capital_layer="all")
        self.assertEqual({row["capital_layer"] for row in all_positions}, {"real", "simulated", "shadow"})

    def test_legacy_single_csv_rows_migrate_to_layer_tables(self) -> None:
        _patch_capital_paths(self, self.root)
        capital_ledger.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        capital_ledger.CAPITAL_CSV.write_text(
            "\n".join(
                [
                    ",".join(capital_ledger.CSV_HEADERS),
                    "REAL-1,2026-06-30T09:30:00,deposit,real,CASH,0,0,100,0,,,",
                    "SIM-1,2026-06-30T09:31:00,deposit,simulated,CASH,0,0,200,0,,,",
                    "SHADOW-1,2026-06-30T09:32:00,deposit,shadow,CASH,0,0,300,0,,,",
                ]
            ),
            encoding="utf-8",
        )

        self.assertEqual(capital_ledger.get_cash_position(), 100.0)
        self.assertEqual(capital_ledger.get_cash_position(capital_layer="simulated"), 200.0)
        self.assertEqual(capital_ledger.get_cash_position(capital_layer="shadow"), 300.0)

        db_path = self.root / "capital" / "capital_ledger.sqlite3"
        self.assertEqual(_table_count(db_path, "capital_ledger_real"), 1)
        self.assertEqual(_table_count(db_path, "capital_ledger_simulated"), 1)
        self.assertEqual(_table_count(db_path, "capital_ledger_shadow"), 1)


if __name__ == "__main__":
    unittest.main()
