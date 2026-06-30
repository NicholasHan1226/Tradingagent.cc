from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shared.accounting import capital_ledger, position_ledger
from shared.execution import shadow_broker


def _patch_position_paths(testcase: unittest.TestCase, tmp_path: Path) -> None:
    ledger_dir = tmp_path / "position_logs"
    position_csv = ledger_dir / "position_ledger.csv"
    for name, value in (
        ("LEDGER_DIR", ledger_dir),
        ("POSITION_CSV", position_csv),
        ("POSITION_LOCK", position_csv.with_suffix(".csv.lock")),
    ):
        patcher = patch.object(position_ledger, name, value)
        patcher.start()
        testcase.addCleanup(patcher.stop)


def _patch_capital_paths(testcase: unittest.TestCase, tmp_path: Path) -> None:
    ledger_dir = tmp_path / "capital_logs"
    capital_csv = ledger_dir / "capital_ledger.csv"
    for name, value in (
        ("LEDGER_DIR", ledger_dir),
        ("CAPITAL_CSV", capital_csv),
        ("CAPITAL_LOCK", capital_csv.with_suffix(".csv.lock")),
    ):
        patcher = patch.object(capital_ledger, name, value)
        patcher.start()
        testcase.addCleanup(patcher.stop)


class CapitalLayerIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp_path = Path(self.tmpdir.name)

    def test_position_default_query_returns_only_real_layer(self) -> None:
        _patch_position_paths(self, self.tmp_path)

        position_ledger.open_position("600000.SH", 100, 10.0, capital_layer="real")
        position_ledger.open_position("600000.SH", 200, 11.0, capital_layer="shadow")
        position_ledger.open_position("600000.SH", 300, 12.0, capital_layer="simulated")

        default_positions = position_ledger.get_positions()
        self.assertEqual(len(default_positions), 1)
        self.assertEqual(default_positions[0]["capital_layer"], "real")
        self.assertEqual(default_positions[0]["is_real_money"], "Y")
        self.assertEqual(default_positions[0]["quantity"], 100)

        shadow_positions = position_ledger.get_positions(capital_layer="shadow")
        self.assertEqual(len(shadow_positions), 1)
        self.assertEqual(shadow_positions[0]["is_real_money"], "N")
        self.assertEqual(shadow_positions[0]["quantity"], 200)

        simulated_positions = position_ledger.get_positions(capital_layer="simulated")
        self.assertEqual(len(simulated_positions), 1)
        self.assertEqual(simulated_positions[0]["is_real_money"], "N")
        self.assertEqual(simulated_positions[0]["quantity"], 300)

        all_positions = position_ledger.get_positions(capital_layer="all")
        self.assertEqual({p["capital_layer"] for p in all_positions}, {"real", "shadow", "simulated"})
        self.assertTrue((self.tmp_path / "position_logs" / "position_ledger_real.csv").exists())
        self.assertTrue((self.tmp_path / "position_logs" / "position_ledger_shadow.csv").exists())
        self.assertTrue((self.tmp_path / "position_logs" / "position_ledger_simulated.csv").exists())
        self.assertFalse((self.tmp_path / "position_logs" / "position_ledger.csv").exists())

    def test_capital_cash_default_query_returns_only_real_layer(self) -> None:
        _patch_capital_paths(self, self.tmp_path)

        capital_ledger.record_deposit(1000.0, "2026-06-30T09:30:00", capital_layer="real")
        capital_ledger.record_deposit(2000.0, "2026-06-30T09:31:00", capital_layer="shadow")
        capital_ledger.record_deposit(3000.0, "2026-06-30T09:32:00", capital_layer="simulated")

        self.assertEqual(capital_ledger.get_capital_balance()["balance"], 1000.0)
        self.assertEqual(capital_ledger.get_cash_position(), 1000.0)
        self.assertEqual(capital_ledger.get_cash_position(capital_layer="real"), 1000.0)
        self.assertEqual(capital_ledger.get_cash_position(capital_layer="shadow"), 2000.0)
        self.assertEqual(capital_ledger.get_cash_position(capital_layer="simulated"), 3000.0)
        self.assertEqual(capital_ledger.get_cash_position(capital_layer="all"), 6000.0)
        self.assertTrue((self.tmp_path / "capital_logs" / "capital_ledger_real.csv").exists())
        self.assertTrue((self.tmp_path / "capital_logs" / "capital_ledger_shadow.csv").exists())
        self.assertTrue((self.tmp_path / "capital_logs" / "capital_ledger_simulated.csv").exists())
        self.assertFalse((self.tmp_path / "capital_logs" / "capital_ledger.csv").exists())

    def test_legacy_position_rows_default_to_shadow(self) -> None:
        _patch_position_paths(self, self.tmp_path)
        position_ledger.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        position_ledger.POSITION_CSV.write_text(
            "\n".join(
                [
                    "entry_id,timestamp,event_type,ts_code,quantity,price,amount,"
                    "running_quantity,running_cost,running_avg_price,realized_pnl,"
                    "order_id,audit_id,note",
                    "LEGACY-1,2026-06-30T09:30:00,open,600001.SH,100,10,1000,"
                    "100,1000,10,0,,,legacy",
                ]
            ),
            encoding="utf-8",
        )

        self.assertEqual(position_ledger.get_positions(), [])
        shadow_positions = position_ledger.get_positions(capital_layer="shadow")
        self.assertEqual(len(shadow_positions), 1)
        self.assertEqual(shadow_positions[0]["capital_layer"], "shadow")
        self.assertEqual(shadow_positions[0]["is_real_money"], "N")

    def test_shadow_broker_rejects_real_capital_layer(self) -> None:
        with self.assertRaises(ValueError):
            shadow_broker.record_shadow(
                {
                    "ts_code": "600000.SH",
                    "side": "buy",
                    "quantity": 100,
                    "price": 10.0,
                    "capital_layer": "real",
                },
                "capital-layer-test",
            )


if __name__ == "__main__":
    unittest.main()
