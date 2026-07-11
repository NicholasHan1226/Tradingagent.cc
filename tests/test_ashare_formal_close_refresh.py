from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Ashare.formal_close_refresh import load_formal_close_prices, run_formal_close_refresh

EPOCH_STATE = {
    "current_epoch_id": 2,
    "capital_cny": 50_000.0,
    "cutover_timestamp": "2026-07-10T20:56:58+00:00",
}


class _Reader:
    def __init__(self, rows_by_symbol: dict[str, list[dict[str, object]]]) -> None:
        self.rows_by_symbol = rows_by_symbol

    def get_bars_daily(self, market: str, symbol: str, start: str, end: str) -> list[dict[str, object]]:
        return list(self.rows_by_symbol.get(symbol, []))


class AshareFormalCloseRefreshTest(unittest.TestCase):
    def setUp(self) -> None:
        self.epoch_patch = patch(
            "Ashare.formal_close_refresh.read_epoch_state", return_value=EPOCH_STATE
        )
        self.epoch_patch.start()
        self.addCleanup(self.epoch_patch.stop)

    def test_completed_trade_date_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            review_dir = Path(tmp)
            existing = {
                "report_type": "ashare_formal_close_refresh",
                "schema_version": 2,
                "trade_date": "20260710",
                "status": "pass",
                "reason": "formal_close_refresh_complete",
                "capital_epoch": 2,
                "capital_cny": 50_000.0,
                "epoch_cutover_timestamp": EPOCH_STATE["cutover_timestamp"],
                "generated_at": "2026-07-11T03:00:00+00:00",
            }
            (review_dir / "formal_close_latest.json").write_text(
                json.dumps(existing), encoding="utf-8"
            )
            with patch(
                "Ashare.formal_close_refresh.local_sim_ledger.get_local_sim_pnl"
            ) as pnl:
                report = run_formal_close_refresh(
                    trade_date="20260710", review_dir=review_dir, reader=_Reader({})
                )

            self.assertEqual(report["status"], "pass")
            self.assertTrue(report["idempotent_skip"])
            pnl.assert_not_called()
            self.assertFalse((review_dir / "formal_close_history.jsonl").exists())

    def test_load_requires_exact_trade_date_for_every_open_position(self) -> None:
        positions = {"600000.SH": {}, "000001.SZ": {}}
        reader = _Reader(
            {
                "600000.SH": [{"trade_date": "20260710", "close": 12.3}],
                "000001.SZ": [{"trade_date": "20260709", "close": 10.1}],
            }
        )

        result = load_formal_close_prices("20260710", positions, reader=reader)

        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["prices"], {"600000.SH": 12.3})
        self.assertEqual(result["missing_symbols"], ["000001.SZ"])
        self.assertEqual(result["price_semantics"], "formal_daily_close_exact_trade_date")

    def test_run_refreshes_all_review_layers_with_one_exact_close_map(self) -> None:
        positions = {"600000.SH": {"quantity": 100}, "000001.SZ": {"quantity": 200}}
        prices = {"600000.SH": 12.3, "000001.SZ": 10.1}
        reader = _Reader(
            {
                symbol: [{"trade_date": "20260710", "close": price}]
                for symbol, price in prices.items()
            }
        )
        captured: dict[str, object] = {}

        with tempfile.TemporaryDirectory() as tmp, patch(
            "Ashare.formal_close_refresh.local_sim_ledger.get_local_sim_pnl",
            side_effect=[
                {"positions": positions},
                {
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 123.4,
                    "total_pnl": 123.4,
                    "market_value": 150000.0,
                    "cash_available": 50000.0,
                },
            ],
        ), patch(
            "Ashare.formal_close_refresh.local_sim_ledger.refresh_local_sim_snapshot",
            side_effect=lambda *, mark_prices: captured.setdefault("snapshot_prices", mark_prices) or {"status": "ok"},
        ), patch(
            "Ashare.formal_close_refresh.write_portfolio_evolution",
            side_effect=lambda **kwargs: captured.setdefault("portfolio_prices", kwargs["mark_prices"]) or {"state": "observed"},
        ), patch(
            "Ashare.formal_close_refresh.build_forward_validation_report",
            return_value={"strategy_label_count": 2},
        ), patch(
            "Ashare.formal_close_refresh.run_daily_review",
            return_value={
                "session": "close",
                "capital_layer_reviews": {
                    "simulated": {
                        "market_reviews": {
                            "ashare": {
                                "stale": False,
                                "ledger_realized_pnl": 0.0,
                                "ledger_unrealized_pnl": 123.4,
                                "ledger_total_pnl": 123.4,
                                "ledger_market_value": 150000.0,
                                "ledger_pnl_source": "ashare_local_sim_mark_to_market",
                            }
                        }
                    }
                },
            },
        ):
            report = run_formal_close_refresh(
                trade_date="20260710",
                reader=reader,
                review_dir=Path(tmp),
            )

        self.assertEqual(report["status"], "pass")
        self.assertEqual(captured["snapshot_prices"], prices)
        self.assertEqual(captured["portfolio_prices"], prices)
        self.assertEqual(report["price_semantics"], "formal_daily_close_exact_trade_date")
        self.assertEqual(report["formal_pnl"]["total_pnl"], 123.4)
        self.assertEqual(report["daily_review"]["ledger_total_pnl"], 123.4)
        self.assertEqual(report["daily_review"]["status"], "current")

    def test_run_fails_without_overwriting_snapshots_when_a_close_is_missing(self) -> None:
        positions = {"600000.SH": {"quantity": 100}, "000001.SZ": {"quantity": 200}}
        reader = _Reader({"600000.SH": [{"trade_date": "20260710", "close": 12.3}]})

        with tempfile.TemporaryDirectory() as tmp, patch(
            "Ashare.formal_close_refresh.local_sim_ledger.get_local_sim_pnl",
            return_value={"positions": positions},
        ), patch(
            "Ashare.formal_close_refresh.local_sim_ledger.refresh_local_sim_snapshot",
        ) as snapshot_writer, patch(
            "Ashare.formal_close_refresh.write_portfolio_evolution",
        ) as evolution_writer:
            report = run_formal_close_refresh(
                trade_date="20260710",
                reader=reader,
                review_dir=Path(tmp),
            )

        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["reason"], "formal_close_incomplete")
        snapshot_writer.assert_not_called()
        evolution_writer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
