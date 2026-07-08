#!/usr/bin/env python3
"""Tests for quarantine_legacy_usd_capital — isolate old simulated USD principal samples."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.runtime_test.quarantine_legacy_usd_capital import (
    OLD_CAPITAL_THRESHOLD_ORIGINAL,
    OLD_CAPITAL_THRESHOLD_CNY,
    QUARANTINE_FIELDS,
    TARGET_MARKETS,
    _detect_old_capital_row,
    quarantine_legacy_usd_capital,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_mtm_row(
    *,
    capital_base: float = 27777.78,
    capital_base_cny: float = 200000.0,
    market: str = "us",
    real_execution: bool = False,
    exclude_from_dashboard: bool = False,
    **extra,
) -> dict:
    row = {
        "date": "20260701",
        "capital_base": capital_base,
        "capital_base_cny": capital_base_cny,
        "equity": capital_base + 100.0,
        "total_equity": capital_base + 100.0,
        "pnl": 100.0,
        "total_pnl": 100.0,
        "realized_pnl": 0.0,
        "unrealized_pnl": 100.0,
        "cash": capital_base - 50.0,
        "market_value": 150.0,
        "return_pct": 0.36,
        "open_position_count": 1,
        "capital_layer": "simulated",
        "real_execution": real_execution,
        "exclude_from_dashboard": exclude_from_dashboard,
        **extra,
    }
    if market:
        row["market"] = market
    return row


def _make_style_row(
    *,
    market: str = "us",
    style_name: str = "trend_following",
    date: str = "20260701",
    capital_base: float | None = None,
    capital_base_cny: float | None = None,
    real_execution: bool = False,
    exclude_from_dashboard: bool = False,
) -> dict:
    row: dict = {
        "market": market,
        "style_name": style_name,
        "date": date,
        "pnl": 50.0,
        "win_rate": 0.55,
        "trades": 5,
        "capital_layer": "simulated",
        "real_execution": real_execution,
        "exclude_from_dashboard": exclude_from_dashboard,
    }
    if capital_base is not None:
        row["capital_base"] = capital_base
    if capital_base_cny is not None:
        row["capital_base_cny"] = capital_base_cny
    return row


def _make_trade_row(
    *,
    symbol: str = "BTCUSDT",
    side: str = "buy",
    fill_qty: float = 0.1,
    fill_price: float = 60000.0,
    capital_layer: str = "simulated",
    real_execution: bool = False,
    exclude_from_dashboard: bool = False,
) -> dict:
    return {
        "timestamp": "2026-07-01T10:00:00Z",
        "order_id": "ORD-001",
        "fill_id": "FILL-001",
        "symbol": symbol,
        "side": side,
        "fill_qty": fill_qty,
        "fill_price": fill_price,
        "notional": fill_qty * fill_price,
        "fees": {"total": 1.0},
        "realized_pnl": 0.0,
        "capital_layer": capital_layer,
        "real_execution": real_execution,
        "exclude_from_dashboard": exclude_from_dashboard,
    }


# ---------------------------------------------------------------------------
# unit tests: row-level detection
# ---------------------------------------------------------------------------


class DetectOldCapitalRowTest(unittest.TestCase):
    """Row-level detection logic for old capital_base."""

    def test_old_capital_mtm_usd_original_detected(self) -> None:
        row = _make_mtm_row(capital_base=27777.78, capital_base_cny=200000.0, market="us")
        self.assertTrue(_detect_old_capital_row(row, market="us"))

    def test_old_capital_mtm_cny_only_detected(self) -> None:
        # capital_base_cny = 166666 (old tier)
        row = _make_mtm_row(capital_base=23148.0, capital_base_cny=166666.0, market="crypto")
        self.assertTrue(_detect_old_capital_row(row, market="crypto"))

    def test_old_capital_mtm_original_only_detected(self) -> None:
        row = _make_mtm_row(capital_base=27777.78, capital_base_cny=None, market="pm")
        # Remove capital_base_cny
        row.pop("capital_base_cny", None)
        self.assertTrue(_detect_old_capital_row(row, market="pm"))

    def test_new_capital_not_detected(self) -> None:
        row = _make_mtm_row(capital_base=10000.0, capital_base_cny=72000.0, market="us")
        self.assertFalse(_detect_old_capital_row(row, market="us"))

    def test_below_threshold_not_detected(self) -> None:
        row = _make_mtm_row(capital_base=8000.0, capital_base_cny=57600.0, market="crypto")
        self.assertFalse(_detect_old_capital_row(row, market="crypto"))

    def test_real_execution_not_detected(self) -> None:
        row = _make_mtm_row(capital_base=27777.78, capital_base_cny=200000.0, market="us", real_execution=True)
        self.assertFalse(_detect_old_capital_row(row, market="us"))

    def test_already_quarantined_not_detected(self) -> None:
        row = _make_mtm_row(capital_base=27777.78, capital_base_cny=200000.0, market="us", exclude_from_dashboard=True)
        self.assertTrue(_detect_old_capital_row(row, market="us"))  # detection still true (data matches)
        # but quarantine should skip it — tested in integration

    def test_ashare_not_detected(self) -> None:
        row = _make_mtm_row(capital_base=200000.0, capital_base_cny=200000.0, market="ashare")
        self.assertFalse(_detect_old_capital_row(row, market="ashare"))

    def test_cn_futures_not_detected(self) -> None:
        row = _make_mtm_row(capital_base=200000.0, capital_base_cny=200000.0, market="cn_futures")
        self.assertFalse(_detect_old_capital_row(row, market="cn_futures"))

    def test_no_capital_base_fields_not_detected(self) -> None:
        row = {"date": "20260701", "pnl": 100.0, "market": "us"}
        self.assertFalse(_detect_old_capital_row(row, market="us"))

    def test_style_performance_old_capital_detected(self) -> None:
        row = _make_style_row(market="crypto", capital_base=27777.78, capital_base_cny=200000.0)
        self.assertTrue(_detect_old_capital_row(row, market="crypto"))

    def test_style_performance_new_capital_not_detected(self) -> None:
        row = _make_style_row(market="pm", capital_base=10000.0, capital_base_cny=72000.0)
        self.assertFalse(_detect_old_capital_row(row, market="pm"))

    def test_style_performance_no_capital_not_detected(self) -> None:
        row = _make_style_row(market="us")
        self.assertFalse(_detect_old_capital_row(row, market="us"))


# ---------------------------------------------------------------------------
# integration tests: full quarantine operation
# ---------------------------------------------------------------------------


class QuarantineLegacyUsdCapitalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger_root = self.root / "shared" / "logs" / "sim_ledger"
        self.review_root = self.root / "shared" / "review"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_jsonl(self, path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    def _read_jsonl(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    # -- daily_mark_to_market ------------------------------------------------

    def test_dry_run_flags_old_mtm_but_does_not_modify(self) -> None:
        mtm_path = self.ledger_root / "us" / "momentum" / "daily_mark_to_market.jsonl"
        rows = [
            _make_mtm_row(capital_base=27777.78, capital_base_cny=200000.0, market="us", date="20260701"),
            _make_mtm_row(capital_base=27777.78, capital_base_cny=200000.0, market="us", date="20260702"),
        ]
        self._write_jsonl(mtm_path, rows)

        result = quarantine_legacy_usd_capital(ledger_root=self.ledger_root, review_root=self.review_root, apply=False)

        self.assertEqual(result["quarantined_count"], 2)
        self.assertEqual(result["skipped_count"], 0)
        self.assertFalse(result["applied"])

        # file must be unchanged
        saved = self._read_jsonl(mtm_path)
        self.assertEqual(len(saved), 2)
        for row in saved:
            self.assertNotIn("run_context", row)
            self.assertNotEqual(row.get("exclude_from_dashboard"), True)

    def test_apply_quarantines_old_mtm_and_backs_up(self) -> None:
        mtm_path = self.ledger_root / "us" / "momentum" / "daily_mark_to_market.jsonl"
        rows = [
            _make_mtm_row(capital_base=27777.78, capital_base_cny=200000.0, market="us", date="20260701"),
        ]
        self._write_jsonl(mtm_path, rows)

        result = quarantine_legacy_usd_capital(ledger_root=self.ledger_root, review_root=self.review_root, apply=True)

        self.assertEqual(result["quarantined_count"], 1)
        self.assertTrue(result["applied"])

        # backup exists
        bak_files = list(mtm_path.parent.glob("*.bak"))
        self.assertEqual(len(bak_files), 1)

        # modified row has quarantine fields
        saved = self._read_jsonl(mtm_path)
        self.assertEqual(len(saved), 1)
        row = saved[0]
        self.assertTrue(row.get("exclude_from_dashboard"))
        self.assertEqual(row.get("run_context"), "legacy_usd_capital_quarantine")
        self.assertIn("quarantine_reason", row)

    def test_apply_skips_new_capital_rows(self) -> None:
        mtm_path = self.ledger_root / "crypto" / "grid" / "daily_mark_to_market.jsonl"
        rows = [
            _make_mtm_row(capital_base=10000.0, capital_base_cny=72000.0, market="crypto", date="20260701"),
            _make_mtm_row(capital_base=10000.0, capital_base_cny=72000.0, market="crypto", date="20260702"),
        ]
        self._write_jsonl(mtm_path, rows)

        result = quarantine_legacy_usd_capital(ledger_root=self.ledger_root, review_root=self.review_root, apply=True)

        self.assertEqual(result["quarantined_count"], 0)
        self.assertEqual(result["skipped_count"], 2)

    def test_skips_already_quarantined(self) -> None:
        mtm_path = self.ledger_root / "pm" / "arbitrage" / "daily_mark_to_market.jsonl"
        rows = [
            _make_mtm_row(capital_base=27777.78, capital_base_cny=200000.0, market="pm", date="20260701", exclude_from_dashboard=True, run_context="legacy_usd_capital_quarantine"),
        ]
        self._write_jsonl(mtm_path, rows)

        result = quarantine_legacy_usd_capital(ledger_root=self.ledger_root, review_root=self.review_root, apply=True)

        self.assertEqual(result["quarantined_count"], 0)
        self.assertEqual(result["already_quarantined_count"], 1)

    def test_skips_real_execution_rows(self) -> None:
        mtm_path = self.ledger_root / "us" / "live" / "daily_mark_to_market.jsonl"
        rows = [
            _make_mtm_row(capital_base=27777.78, capital_base_cny=200000.0, market="us", date="20260701", real_execution=True),
        ]
        self._write_jsonl(mtm_path, rows)

        result = quarantine_legacy_usd_capital(ledger_root=self.ledger_root, review_root=self.review_root, apply=True)

        self.assertEqual(result["quarantined_count"], 0)
        self.assertEqual(result["skipped_count"], 1)

    def test_ignores_ashare_directories(self) -> None:
        mtm_path = self.ledger_root / "ashare" / "ashare_sim" / "daily_mark_to_market.jsonl"
        rows = [
            _make_mtm_row(capital_base=200000.0, capital_base_cny=200000.0, market="ashare", date="20260701"),
        ]
        self._write_jsonl(mtm_path, rows)

        result = quarantine_legacy_usd_capital(ledger_root=self.ledger_root, review_root=self.review_root, apply=True)

        self.assertEqual(result["quarantined_count"], 0)
        self.assertEqual(result["skipped_count"], 0)  # ashare not even scanned

    def test_ignores_cn_futures_directories(self) -> None:
        mtm_path = self.ledger_root / "cn_futures" / "index" / "daily_mark_to_market.jsonl"
        rows = [
            _make_mtm_row(capital_base=200000.0, capital_base_cny=200000.0, market="cn_futures", date="20260701"),
        ]
        self._write_jsonl(mtm_path, rows)

        result = quarantine_legacy_usd_capital(ledger_root=self.ledger_root, review_root=self.review_root, apply=True)

        self.assertEqual(result["quarantined_count"], 0)

    def test_mixed_old_and_new_in_same_file(self) -> None:
        mtm_path = self.ledger_root / "us" / "swing" / "daily_mark_to_market.jsonl"
        rows = [
            _make_mtm_row(capital_base=27777.78, capital_base_cny=200000.0, market="us", date="20260701"),
            _make_mtm_row(capital_base=10000.0, capital_base_cny=72000.0, market="us", date="20260708"),
        ]
        self._write_jsonl(mtm_path, rows)

        result = quarantine_legacy_usd_capital(ledger_root=self.ledger_root, review_root=self.review_root, apply=True)

        self.assertEqual(result["quarantined_count"], 1)
        self.assertEqual(result["skipped_count"], 1)

        saved = self._read_jsonl(mtm_path)
        self.assertTrue(saved[0].get("exclude_from_dashboard"))
        self.assertNotEqual(saved[1].get("exclude_from_dashboard"), True)

    def test_before_cutoff_quarantines_pre_cutover_usd_rows_even_when_capital_is_below_threshold(self) -> None:
        mtm_path = self.ledger_root / "crypto" / "balanced" / "daily_mark_to_market.jsonl"
        rows = [
            _make_mtm_row(
                capital_base=4629.62963,
                capital_base_cny=33333.33,
                market="crypto",
                timestamp="2026-07-08T05:00:00+00:00",
            ),
            _make_mtm_row(
                capital_base=4629.62963,
                capital_base_cny=33333.33,
                market="crypto",
                timestamp="2026-07-08T05:20:00+00:00",
            ),
        ]
        self._write_jsonl(mtm_path, rows)

        result = quarantine_legacy_usd_capital(
            ledger_root=self.ledger_root,
            review_root=self.review_root,
            apply=True,
            before="2026-07-08T05:10:59Z",
        )

        self.assertEqual(result["quarantined_count"], 1)
        saved = self._read_jsonl(mtm_path)
        self.assertTrue(saved[0].get("exclude_from_dashboard"))
        self.assertIn("pre_cutover", saved[0].get("quarantine_reason", ""))
        self.assertNotEqual(saved[1].get("exclude_from_dashboard"), True)

    # -- style_performance ---------------------------------------------------

    def test_style_performance_quarantine(self) -> None:
        sp_path = self.review_root / "us" / "style_performance.jsonl"
        rows = [
            _make_style_row(market="us", capital_base=27777.78, capital_base_cny=200000.0, date="20260701"),
            _make_style_row(market="us", capital_base=10000.0, capital_base_cny=72000.0, date="20260708"),
        ]
        self._write_jsonl(sp_path, rows)

        result = quarantine_legacy_usd_capital(ledger_root=self.ledger_root, review_root=self.review_root, apply=True)

        self.assertEqual(result["quarantined_count"], 1)

        saved = self._read_jsonl(sp_path)
        self.assertTrue(saved[0].get("exclude_from_dashboard"))
        self.assertNotEqual(saved[1].get("exclude_from_dashboard"), True)

    def test_style_performance_skips_ashare(self) -> None:
        sp_path = self.review_root / "ashare" / "style_performance.jsonl"
        rows = [
            _make_style_row(market="ashare", capital_base=200000.0, capital_base_cny=200000.0, date="20260701"),
        ]
        self._write_jsonl(sp_path, rows)

        result = quarantine_legacy_usd_capital(ledger_root=self.ledger_root, review_root=self.review_root, apply=True)

        self.assertEqual(result["quarantined_count"], 0)

    # -- trade_journal -------------------------------------------------------

    def test_trade_journal_quarantine_in_old_capital_directory(self) -> None:
        # Create an MTM row with old capital to establish directory as old-capital
        mtm_path = self.ledger_root / "crypto" / "breakout" / "daily_mark_to_market.jsonl"
        self._write_jsonl(mtm_path, [
            _make_mtm_row(capital_base=27777.78, capital_base_cny=200000.0, market="crypto", date="20260701"),
        ])

        tj_path = self.ledger_root / "crypto" / "breakout" / "trade_journal.jsonl"
        trade_rows = [
            _make_trade_row(symbol="BTCUSDT"),
            _make_trade_row(symbol="ETHUSDT", side="sell"),
        ]
        self._write_jsonl(tj_path, trade_rows)

        result = quarantine_legacy_usd_capital(ledger_root=self.ledger_root, review_root=self.review_root, apply=True)

        self.assertGreaterEqual(result["quarantined_count"], 3)  # 1 mtm + 2 trades

        saved_trades = self._read_jsonl(tj_path)
        for row in saved_trades:
            self.assertTrue(row.get("exclude_from_dashboard"))
            self.assertEqual(row.get("run_context"), "legacy_usd_capital_quarantine")

    def test_trade_journal_quarantine_in_pre_cutover_directory(self) -> None:
        mtm_path = self.ledger_root / "us" / "balanced" / "daily_mark_to_market.jsonl"
        self._write_jsonl(mtm_path, [
            _make_mtm_row(
                capital_base=5411.100289,
                capital_base_cny=38959.92,
                market="us",
                timestamp="2026-07-08T05:00:00+00:00",
            ),
        ])

        tj_path = self.ledger_root / "us" / "balanced" / "trade_journal.jsonl"
        self._write_jsonl(tj_path, [
            _make_trade_row(symbol="AAPL"),
            _make_trade_row(symbol="MSFT", side="sell"),
        ])

        result = quarantine_legacy_usd_capital(
            ledger_root=self.ledger_root,
            review_root=self.review_root,
            apply=True,
            before="2026-07-08T05:10:59Z",
        )

        self.assertEqual(result["quarantined_count"], 3)
        saved_trades = self._read_jsonl(tj_path)
        for row in saved_trades:
            self.assertTrue(row.get("exclude_from_dashboard"))
            self.assertIn("pre_cutover", row.get("quarantine_reason", ""))

    def test_trade_journal_not_quarantined_in_new_capital_directory(self) -> None:
        mtm_path = self.ledger_root / "pm" / "scalp" / "daily_mark_to_market.jsonl"
        self._write_jsonl(mtm_path, [
            _make_mtm_row(capital_base=10000.0, capital_base_cny=72000.0, market="pm", date="20260708"),
        ])

        tj_path = self.ledger_root / "pm" / "scalp" / "trade_journal.jsonl"
        trade_rows = [_make_trade_row(symbol="YES-0x1234")]
        self._write_jsonl(tj_path, trade_rows)

        result = quarantine_legacy_usd_capital(ledger_root=self.ledger_root, review_root=self.review_root, apply=True)

        # Only MTM was scanned, not quarantined. Trade journal not flagged.
        saved_trades = self._read_jsonl(tj_path)
        for row in saved_trades:
            self.assertNotEqual(row.get("exclude_from_dashboard"), True)

    def test_positions_json_quarantine_in_pre_cutover_directory(self) -> None:
        mtm_path = self.ledger_root / "crypto" / "balanced" / "daily_mark_to_market.jsonl"
        self._write_jsonl(mtm_path, [
            _make_mtm_row(
                capital_base=4629.62963,
                capital_base_cny=33333.33,
                market="crypto",
                timestamp="2026-07-08T05:00:00+00:00",
            ),
        ])
        positions_path = self.ledger_root / "crypto" / "balanced" / "positions.json"
        positions_path.write_text(
            json.dumps({"cash": 3240.74, "positions": {"BTCUSDT": {"quantity": 1, "avg_cost": 100}}}, ensure_ascii=False),
            encoding="utf-8",
        )

        result = quarantine_legacy_usd_capital(
            ledger_root=self.ledger_root,
            review_root=self.review_root,
            apply=True,
            before="2026-07-08T05:10:59Z",
        )

        self.assertGreaterEqual(result["quarantined_count"], 2)
        payload = json.loads(positions_path.read_text(encoding="utf-8"))
        self.assertTrue(payload.get("exclude_from_dashboard"))
        self.assertEqual(payload.get("run_context"), "legacy_usd_capital_quarantine")
        self.assertIn("quarantine_reason", payload)

    def test_style_comparison_top_level_quarantine_before_cutoff(self) -> None:
        style_path = self.review_root / "us" / "style_comparison.json"
        style_path.parent.mkdir(parents=True, exist_ok=True)
        style_path.write_text(
            json.dumps(
                {
                    "market": "us",
                    "date": "2026-07-08",
                    "capital_layer": "simulated",
                    "account_type": "simulated",
                    "real_execution": False,
                    "styles_total": 6,
                    "filled_count": 34,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        result = quarantine_legacy_usd_capital(
            ledger_root=self.ledger_root,
            review_root=self.review_root,
            apply=True,
            before="2026-07-08T05:10:59Z",
        )

        self.assertEqual(result["quarantined_count"], 1)
        payload = json.loads(style_path.read_text(encoding="utf-8"))
        self.assertTrue(payload.get("exclude_from_dashboard"))
        self.assertEqual(payload.get("run_context"), "legacy_usd_capital_quarantine")

    # -- idempotency ---------------------------------------------------------

    def test_idempotent_double_apply(self) -> None:
        mtm_path = self.ledger_root / "us" / "value" / "daily_mark_to_market.jsonl"
        rows = [
            _make_mtm_row(capital_base=27777.78, capital_base_cny=200000.0, market="us", date="20260701"),
        ]
        self._write_jsonl(mtm_path, rows)

        r1 = quarantine_legacy_usd_capital(ledger_root=self.ledger_root, review_root=self.review_root, apply=True)
        self.assertEqual(r1["quarantined_count"], 1)

        r2 = quarantine_legacy_usd_capital(ledger_root=self.ledger_root, review_root=self.review_root, apply=True)
        self.assertEqual(r2["quarantined_count"], 0)
        self.assertEqual(r2["already_quarantined_count"], 1)

        # Only one backup should exist
        bak_files = list(mtm_path.parent.glob("*.bak"))
        self.assertEqual(len(bak_files), 1)

    # -- manifest output -----------------------------------------------------

    def test_manifest_includes_summary(self) -> None:
        mtm_path = self.ledger_root / "us" / "swing" / "daily_mark_to_market.jsonl"
        self._write_jsonl(mtm_path, [
            _make_mtm_row(capital_base=27777.78, capital_base_cny=200000.0, market="us", date="20260701"),
            _make_mtm_row(capital_base=27777.78, capital_base_cny=200000.0, market="us", date="20260702"),
        ])

        result = quarantine_legacy_usd_capital(ledger_root=self.ledger_root, review_root=self.review_root, apply=True)

        self.assertIn("files_modified", result)
        self.assertIn("files_scanned", result)
        self.assertGreater(len(result["files_modified"]), 0)

    def test_empty_directory_no_error(self) -> None:
        # no files at all
        result = quarantine_legacy_usd_capital(ledger_root=self.ledger_root, review_root=self.review_root, apply=True)

        self.assertEqual(result["quarantined_count"], 0)
        self.assertEqual(result["status"], "pass")


if __name__ == "__main__":
    unittest.main()
