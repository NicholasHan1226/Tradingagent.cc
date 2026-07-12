from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pytest

from shared.review.sample_journal import SampleJournal
from shared.runtime_test.ashare_forward_label_ops import (
    ForwardLabelOpsSafetyError,
    enumerate_ashare_forward_label_backlog,
    main,
    price_points_from_bars,
    run_ashare_forward_label_backlog,
    run_ashare_forward_label_ops,
)


class FakeAshareReader:
    def __init__(self, *, intraday=None, daily=None) -> None:
        self.intraday = list(intraday or [])
        self.daily = list(daily or [])
        self.calls: list[tuple[object, ...]] = []

    def get_bars_intraday(self, market, symbol, interval, start, end):
        self.calls.append(("intraday", market, symbol, interval, start, end))
        return list(self.intraday)

    def get_bars_daily(self, market, symbol, start, end):
        self.calls.append(("daily", market, symbol, start, end))
        return list(self.daily)


def _prediction(
    *, prediction_at: str = "2026-07-13T09:30:00+08:00"
) -> dict[str, object]:
    return {
        "market": "Ashare",
        "symbol": "000001.SZ",
        "style": "trend_breakout",
        "strategy_version": "trend-v1",
        "prediction_at": prediction_at,
        "reference_price": 10.0,
        "direction": "long",
        "trade_date": "20260713",
        "costs": {
            "round_trip_fee_bps": 105.0,
            "round_trip_slippage_bps": 10.0,
            "cost_model_version": "ashare-execution-reality-20260706-v1",
            "cost_basis_notional_cny": 1000.0,
        },
        "data_quality": {
            "reliable": True,
            "source": "SharedSignals/reference",
            "price_timestamp": prediction_at,
        },
    }


class PricePointTests(unittest.TestCase):
    def test_only_real_priced_bars_with_provenance_become_points(self) -> None:
        result = price_points_from_bars(
            intraday_rows=[
                {
                    "close": 10.5,
                    "bar_time": "2026-07-13T10:00:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                },
                {
                    "close": 99.0,
                    "bar_time": "2026-07-13T10:05:00+08:00",
                },
                {
                    "close": 0,
                    "bar_time": "2026-07-13T10:10:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                },
            ],
            daily_rows=[
                {
                    "close": 11.0,
                    "trade_date": "20260713",
                    "source": "SharedSignals/market_data",
                },
                {"close": 12.0, "trade_date": "20260714"},
            ],
        )

        self.assertEqual(
            [point["price"] for point in result["price_points"]], [10.5, 11.0]
        )
        self.assertEqual(
            result["price_points"][1]["timestamp"], "2026-07-13T15:00:00+08:00"
        )
        self.assertEqual(
            result["price_points"][1]["timestamp_semantics"], "ashare_daily_close"
        )
        self.assertEqual(
            sorted(rejection["reason"] for rejection in result["rejections"]),
            ["invalid_price", "missing_source", "missing_source"],
        )


class ForwardLabelOpsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=Path.cwd() / "tests")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "samples.jsonl"
        self.journal = SampleJournal(self.path)

    def test_due_labels_append_then_repeat_idempotently(self) -> None:
        self.journal.append_prediction(_prediction())
        reader = FakeAshareReader(
            intraday=[
                {
                    "close": 11.0,
                    "bar_time": "2026-07-13T10:00:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                },
                {
                    "close": 12.0,
                    "bar_time": "2026-07-13T10:30:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                },
            ],
            daily=[
                {
                    "close": 13.0,
                    "trade_date": "20260713",
                    "source": "SharedSignals/market_data",
                },
                {
                    "close": 14.0,
                    "trade_date": "20260714",
                    "source": "SharedSignals/market_data",
                },
            ],
        )

        first = run_ashare_forward_label_ops(
            journal_path=self.path,
            trade_date="20260713",
            as_of="2026-07-14T16:00:00+08:00",
            reader=reader,
            environ={},
        )
        second = run_ashare_forward_label_ops(
            journal_path=self.path,
            trade_date="20260713",
            as_of="2026-07-14T16:00:00+08:00",
            reader=reader,
            environ={},
        )

        self.assertEqual(first["status"], "pass")
        self.assertEqual(first["counts"]["prediction_count"], 1)
        self.assertEqual(first["counts"]["new_label_updates"], 1)
        self.assertEqual(first["counts"]["idempotent_label_updates"], 0)
        self.assertEqual(first["counts"]["ready_labels"], 4)
        self.assertEqual(first["counts"]["pending_not_due"], 2)
        self.assertEqual(first["counts"]["missing_evidence"], 0)
        self.assertEqual(second["counts"]["new_label_updates"], 0)
        self.assertEqual(second["counts"]["idempotent_label_updates"], 1)
        self.assertEqual(len(self.journal.read_events()), 2)
        latest = self.journal.latest_sample_records()[0]
        self.assertEqual(latest["labels"]["m30"]["exit_price"], 11.0)
        self.assertEqual(latest["labels"]["m60"]["exit_price"], 12.0)
        self.assertEqual(latest["labels"]["close"]["exit_price"], 13.0)
        self.assertEqual(latest["labels"]["1d"]["exit_price"], 14.0)
        # Verify cost model version in labels
        self.assertEqual(
            latest["labels"]["m30"]["cost_model_version"],
            "ashare-execution-reality-20260706-v1",
        )
        self.assertFalse(first["real_trading_enabled"])

    def test_missing_cost_evidence_is_tracked(self) -> None:
        """Snapshot without costs should produce cost_evidence_rejected labels."""
        pred = _prediction()
        del pred["costs"]
        self.journal.append_prediction(pred)
        reader = FakeAshareReader(
            intraday=[
                {
                    "close": 11.0,
                    "bar_time": "2026-07-13T10:00:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                },
            ],
            daily=[
                {
                    "close": 12.0,
                    "trade_date": "20260713",
                    "source": "SharedSignals/market_data",
                },
                {
                    "close": 13.0,
                    "trade_date": "20260714",
                    "source": "SharedSignals/market_data",
                },
            ],
        )

        report = run_ashare_forward_label_ops(
            journal_path=self.path,
            trade_date="20260713",
            as_of="2026-07-14T16:00:00+08:00",
            reader=reader,
            environ={},
        )

        self.assertEqual(report["counts"]["cost_evidence_rejected"], 4)
        self.assertEqual(report["counts"]["ready_labels"], 0)

    def test_missing_or_unproven_prices_are_counted_without_fabrication(self) -> None:
        self.journal.append_prediction(_prediction())
        reader = FakeAshareReader(
            intraday=[
                {
                    "close": 15.0,
                    "bar_time": "2026-07-13T10:00:00+08:00",
                    # No source: never eligible as label evidence.
                }
            ]
        )

        report = run_ashare_forward_label_ops(
            journal_path=self.path,
            trade_date="20260713",
            as_of="2026-07-13T15:30:00+08:00",
            reader=reader,
            environ={},
        )

        self.assertEqual(report["counts"]["missing_evidence"], 3)
        self.assertEqual(report["counts"]["pending_not_due"], 3)
        self.assertEqual(report["counts"]["bar_quality_rejections"], 1)
        latest = self.journal.latest_sample_records()[0]
        self.assertIsNone(latest["labels"]["m30"]["exit_price"])
        self.assertEqual(latest["labels"]["m30"]["status"], "missing_exit_evidence")

    def test_daily_horizons_count_trading_days_and_do_not_fake_intraday_labels(
        self,
    ) -> None:
        prediction = _prediction(prediction_at="2026-07-10T09:30:00+08:00")
        prediction["trade_date"] = "20260710"
        self.journal.append_prediction(prediction)
        reader = FakeAshareReader(
            daily=[
                {
                    "close": 10.1,
                    "trade_date": "20260710",
                    "source": "SharedSignals/market_data",
                },
                {
                    "close": 10.2,
                    "trade_date": "20260713",
                    "source": "SharedSignals/market_data",
                },
            ]
        )

        report = run_ashare_forward_label_ops(
            journal_path=self.path,
            trade_date="20260710",
            as_of="2026-07-13T16:00:00+08:00",
            reader=reader,
            environ={},
        )

        latest = self.journal.latest_sample_records()[0]
        self.assertEqual(latest["labels"]["m30"]["status"], "missing_exit_evidence")
        self.assertEqual(latest["labels"]["m60"]["status"], "missing_exit_evidence")
        self.assertEqual(latest["labels"]["close"]["exit_price"], 10.1)
        self.assertEqual(latest["labels"]["1d"]["exit_price"], 10.2)
        self.assertEqual(latest["labels"]["3d"]["status"], "pending_not_due")
        self.assertEqual(latest["labels"]["5d"]["status"], "pending_not_due")
        self.assertEqual(report["counts"]["ready_labels"], 2)

    def test_trade_date_filters_predictions(self) -> None:
        self.journal.append_prediction(_prediction())
        self.journal.append_prediction(
            _prediction(prediction_at="2026-07-14T09:30:00+08:00")
            | {"trade_date": "20260714"}
        )

        report = run_ashare_forward_label_ops(
            journal_path=self.path,
            trade_date="20260713",
            as_of="2026-07-13T09:45:00+08:00",
            reader=FakeAshareReader(),
            environ={},
        )

        self.assertEqual(report["counts"]["prediction_count"], 1)
        self.assertEqual(report["counts"]["filtered_predictions"], 1)

    def test_live_environment_fails_before_market_reads_or_journal_writes(self) -> None:
        self.journal.append_prediction(_prediction())
        reader = FakeAshareReader()

        with self.assertRaises(ForwardLabelOpsSafetyError):
            run_ashare_forward_label_ops(
                journal_path=self.path,
                trade_date="20260713",
                as_of="2026-07-13T15:30:00+08:00",
                reader=reader,
                environ={"REAL_TRADING_ENABLED": "true"},
            )

        self.assertEqual(reader.calls, [])
        self.assertEqual(len(self.journal.read_events()), 1)

    def test_explicit_live_flag_fails_closed(self) -> None:
        self.journal.append_prediction(_prediction())
        with self.assertRaises(ForwardLabelOpsSafetyError):
            run_ashare_forward_label_ops(
                journal_path=self.path,
                trade_date="20260713",
                as_of="2026-07-13T15:30:00+08:00",
                reader=FakeAshareReader(),
                environ={},
                safety_flags={"live_execution_enabled": True},
            )

    def test_unreliable_reference_is_materialized_as_data_quality_rejection(
        self,
    ) -> None:
        candidate = _prediction()
        candidate["data_quality"] = {
            "reliable": False,
            "source": "SharedSignals/reference",
            "price_timestamp": candidate["prediction_at"],
        }
        self.journal.append_prediction(candidate)

        report = run_ashare_forward_label_ops(
            journal_path=self.path,
            trade_date="20260713",
            as_of="2026-07-13T10:00:00+08:00",
            reader=FakeAshareReader(),
            environ={},
        )

        self.assertEqual(report["counts"]["data_quality_rejected"], 1)
        self.assertEqual(report["counts"]["pending_not_due"], 5)
        latest = self.journal.latest_sample_records()[0]
        self.assertEqual(latest["labels"]["m30"]["status"], "rejected_data_quality")

    def test_naive_prediction_timestamp_uses_compatible_naive_bar_timestamps(
        self,
    ) -> None:
        prediction = _prediction(prediction_at="2026-07-13T09:30:00")
        prediction["data_quality"]["price_timestamp"] = "2026-07-13T09:30:00"
        self.journal.append_prediction(prediction)
        reader = FakeAshareReader(
            intraday=[
                {
                    "close": 10.5,
                    "bar_time": "2026-07-13T10:00:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                }
            ]
        )

        report = run_ashare_forward_label_ops(
            journal_path=self.path,
            trade_date="20260713",
            as_of="2026-07-13T10:00:00+08:00",
            reader=reader,
            environ={},
        )

        self.assertEqual(report["counts"]["ready_labels"], 1)
        latest = self.journal.latest_sample_records()[0]
        self.assertEqual(latest["labels"]["m30"]["exit_price"], 10.5)

    def test_live_marker_inside_injected_journal_fails_before_market_read(self) -> None:
        class LiveMarkedJournal:
            def read_events(self):
                return [
                    {
                        "journal_event_type": "prediction_snapshot",
                        "market": "Ashare",
                        "trade_date": "20260713",
                        "live_trading_enabled": True,
                    }
                ]

        reader = FakeAshareReader()
        with self.assertRaises(ForwardLabelOpsSafetyError):
            run_ashare_forward_label_ops(
                journal_path=self.path,
                trade_date="20260713",
                as_of="2026-07-13T15:30:00+08:00",
                reader=reader,
                environ={},
                journal=LiveMarkedJournal(),
            )
        self.assertEqual(reader.calls, [])

    def test_cli_writes_only_label_update_and_prints_json(self) -> None:
        self.journal.append_prediction(_prediction())
        fake = FakeAshareReader()
        stdout = io.StringIO()
        with (
            patch(
                "shared.runtime_test.ashare_forward_label_ops.TradingagentDataReader",
                return_value=fake,
            ),
            patch.dict(os.environ, {"REAL_TRADING_ENABLED": "false"}, clear=True),
            patch("sys.stdout", stdout),
        ):
            exit_code = main(
                [
                    "--journal-path",
                    str(self.path),
                    "--trade-date",
                    "20260713",
                    "--as-of",
                    "2026-07-13T15:30:00+08:00",
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["operation"], "ashare_forward_label_ops")
        self.assertEqual(len(self.journal.read_events()), 2)


class ForwardLabelBacklogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=Path.cwd() / "tests")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "samples.jsonl"
        self.journal = SampleJournal(self.path)

    @staticmethod
    def _event(
        snapshot_id: str,
        trade_date: str,
        *,
        prediction_at: str | None = None,
    ) -> dict[str, object]:
        iso_date = "%s-%s-%s" % (
            trade_date[:4],
            trade_date[4:6],
            trade_date[6:],
        )
        return {
            "journal_event_type": "prediction_snapshot",
            "snapshot_id": snapshot_id,
            "market": "Ashare",
            "trade_date": trade_date,
            "prediction_at": prediction_at or "%sT09:30:00+08:00" % iso_date,
            "forward_label_eligibility": "eligible",
        }

    def test_enumerator_finds_old_pending_dates_within_bounded_window(self) -> None:
        events = [
            self._event("outside", "20260630"),
            self._event("old-pending", "20260710"),
            self._event("terminal", "20260711"),
            self._event("today-pending", "20260713"),
            self._event("future", "20260714"),
            {
                "journal_event_type": "forward_label_update",
                "snapshot_id": "terminal",
                "labels_as_of": "2026-07-13T16:00:00+08:00",
                "labels": {
                    horizon: {"status": "ready"}
                    for horizon in ("m30", "m60", "close", "1d", "3d", "5d")
                },
            },
        ]

        backlog = enumerate_ashare_forward_label_backlog(
            events,
            anchor_trade_date="20260713",
            as_of="2026-07-13T16:00:00+08:00",
            window_days=7,
        )

        self.assertEqual(backlog["pending_trade_dates"], ["20260710", "20260713"])
        self.assertEqual(backlog["pending_snapshot_count"], 2)
        self.assertEqual(backlog["terminal_snapshot_count"], 1)
        self.assertEqual(backlog["outside_window_prediction_count"], 1)
        self.assertEqual(backlog["future_prediction_count"], 1)
        self.assertEqual(backlog["window_start_trade_date"], "20260707")
        self.assertEqual(backlog["window_end_trade_date"], "20260713")

    def test_enumerator_accepts_rejected_missing_cost_evidence_as_terminal(
        self,
    ) -> None:
        """Labels with rejected_missing_cost_evidence should also be terminal."""
        events = [
            self._event("snap-cost", "20260713"),
            {
                "journal_event_type": "forward_label_update",
                "snapshot_id": "snap-cost",
                "labels_as_of": "2026-07-13T16:00:00+08:00",
                "labels": {
                    "m30": {"status": "rejected_missing_cost_evidence"},
                    "m60": {"status": "rejected_missing_cost_evidence"},
                    "close": {"status": "rejected_missing_cost_evidence"},
                    "1d": {"status": "rejected_missing_cost_evidence"},
                    "3d": {"status": "rejected_missing_cost_evidence"},
                    "5d": {"status": "rejected_missing_cost_evidence"},
                },
            },
        ]

        backlog = enumerate_ashare_forward_label_backlog(
            events,
            anchor_trade_date="20260713",
            as_of="2026-07-13T16:00:00+08:00",
            window_days=7,
        )

        self.assertEqual(backlog["terminal_snapshot_count"], 1)
        self.assertEqual(backlog["pending_snapshot_count"], 0)

    def test_enumerator_rejects_unbounded_or_semantically_invalid_input(self) -> None:
        with self.assertRaisesRegex(ValueError, "window_days"):
            enumerate_ashare_forward_label_backlog(
                [],
                anchor_trade_date="20260713",
                as_of="2026-07-13T16:00:00+08:00",
                window_days=32,
            )

        mismatch = self._event(
            "mismatch",
            "20260712",
            prediction_at="2026-07-13T09:30:00+08:00",
        )
        with self.assertRaisesRegex(ValueError, "prediction_trade_date_mismatch"):
            enumerate_ashare_forward_label_backlog(
                [mismatch],
                anchor_trade_date="20260713",
                as_of="2026-07-13T16:00:00+08:00",
                window_days=7,
            )

    def test_backlog_processes_old_and_anchor_dates_then_repeats_idempotently(
        self,
    ) -> None:
        old = _prediction(prediction_at="2026-07-10T09:30:00+08:00")
        old["trade_date"] = "20260710"
        old["symbol"] = "000001.SZ"
        current = _prediction(prediction_at="2026-07-13T09:30:00+08:00")
        current["trade_date"] = "20260713"
        current["symbol"] = "000002.SZ"
        self.journal.append_prediction(old)
        self.journal.append_prediction(current)
        reader = FakeAshareReader(
            intraday=[
                {
                    "close": 10.2,
                    "bar_time": "2026-07-10T10:00:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                },
                {
                    "close": 10.3,
                    "bar_time": "2026-07-10T10:30:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                },
                {
                    "close": 10.4,
                    "bar_time": "2026-07-13T10:00:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                },
                {
                    "close": 10.5,
                    "bar_time": "2026-07-13T10:30:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                },
            ],
            daily=[
                {
                    "close": 10.6,
                    "trade_date": "20260710",
                    "source": "SharedSignals/market_data",
                },
                {
                    "close": 10.7,
                    "trade_date": "20260713",
                    "source": "SharedSignals/market_data",
                },
                {
                    "close": 10.8,
                    "trade_date": "20260714",
                    "source": "SharedSignals/market_data",
                },
            ],
        )

        first = run_ashare_forward_label_backlog(
            journal_path=self.path,
            anchor_trade_date="20260714",
            as_of="2026-07-14T16:00:00+08:00",
            window_days=7,
            reader=reader,
            environ={},
        )
        second = run_ashare_forward_label_backlog(
            journal_path=self.path,
            anchor_trade_date="20260714",
            as_of="2026-07-14T16:00:00+08:00",
            window_days=7,
            reader=reader,
            environ={},
        )

        self.assertEqual(first["processed_trade_dates"], ["20260710", "20260713"])
        self.assertEqual(first["counts"]["prediction_count"], 2)
        self.assertEqual(first["counts"]["new_label_updates"], 2)
        self.assertEqual(second["counts"]["new_label_updates"], 0)
        self.assertEqual(second["counts"]["idempotent_label_updates"], 2)
        self.assertEqual(len(self.journal.read_events()), 4)

    def test_backlog_journal_semantic_error_fails_before_market_read(self) -> None:
        class InvalidJournal:
            def read_events(self):
                return [
                    {
                        "journal_event_type": "prediction_snapshot",
                        "snapshot_id": "",
                        "market": "Ashare",
                        "trade_date": "20260710",
                        "prediction_at": "2026-07-10T09:30:00+08:00",
                    }
                ]

        reader = FakeAshareReader()
        with self.assertRaisesRegex(ValueError, "missing_snapshot_id"):
            run_ashare_forward_label_backlog(
                journal_path=self.path,
                anchor_trade_date="20260713",
                as_of="2026-07-13T16:00:00+08:00",
                window_days=7,
                reader=reader,
                environ={},
                journal=InvalidJournal(),
            )
        self.assertEqual(reader.calls, [])

    def test_cli_anchor_run_discovers_old_prediction_date_by_default(self) -> None:
        old = _prediction(prediction_at="2026-07-10T09:30:00+08:00")
        old["trade_date"] = "20260710"
        self.journal.append_prediction(old)
        stdout = io.StringIO()
        with (
            patch(
                "shared.runtime_test.ashare_forward_label_ops.TradingagentDataReader",
                return_value=FakeAshareReader(),
            ),
            patch.dict(os.environ, {"REAL_TRADING_ENABLED": "false"}, clear=True),
            patch("sys.stdout", stdout),
        ):
            exit_code = main(
                [
                    "--journal-path",
                    str(self.path),
                    "--trade-date",
                    "20260713",
                    "--as-of",
                    "2026-07-13T16:00:00+08:00",
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["mode"], "bounded_backlog")
        self.assertEqual(payload["processed_trade_dates"], ["20260710"])
        self.assertEqual(payload["counts"]["prediction_count"], 1)
        self.assertEqual(payload["backlog"]["window_days"], 31)


class ActualExecutionCostTests(unittest.TestCase):
    """Tests for _build_actual_execution_costs and actual-cost priority."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=Path.cwd() / "tests")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "samples.jsonl"
        self.journal = SampleJournal(self.path)

    def _add_prediction(self, symbol: str = "000001.SZ") -> dict[str, object]:
        pred = {
            "market": "Ashare",
            "symbol": symbol,
            "style": "trend_breakout",
            "strategy_version": "trend-v1",
            "prediction_at": "2026-07-13T09:30:00+08:00",
            "reference_price": 10.0,
            "direction": "long",
            "trade_date": "20260713",
            "capital_authority_id": "ashare-capital-v1",
            "authority_generation": 1,
            "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
            "costs": {
                "round_trip_fee_bps": 105.0,
                "round_trip_slippage_bps": 10.0,
                "cost_model_version": "ashare-execution-reality-20260706-v1",
                "cost_basis_notional_cny": 1000.0,
            },
            "data_quality": {
                "reliable": True,
                "source": "SharedSignals/reference",
                "price_timestamp": "2026-07-13T09:30:00+08:00",
            },
        }
        return self.journal.append_prediction(pred)["record"]

    def _add_verified_fill(
        self,
        symbol: str = "000001.SZ",
        filled_price: float = 10.0,
        filled_qty: float = 100,
        fee_cny: float = 5.0,
        slippage_cny: float = 0.5,
        trade_date: str = "20260713",
    ) -> None:
        fill_event = {
            "journal_event_type": "sample_event",
            "journal_event_id": "ashare_fill:%s:BUY-1" % trade_date,
            "record_type": "fill",
            "market": "ashare",
            "symbol": symbol,
            "trade_date": trade_date,
            "execution_eligible": True,
            "filled_quantity": filled_qty,
            "filled_price": filled_price,
            "fee_cny": fee_cny,
            "slippage_cny": slippage_cny,
            "sample_layer": "exploration_fill",
            "real_trading_enabled": False,
        }
        self.journal.append_sample(fill_event)

    def _add_round_trip(
        self,
        prediction: dict[str, object],
        symbol: str = "000001.SZ",
        entry_price: float = 10.0,
        entry_qty: float = 100,
        fee_cny: float = 10.0,
        slippage_cny: float = 1.0,
        trade_date: str = "20260714",
    ) -> None:
        rt_event = {
            "journal_event_type": "sample_event",
            "journal_event_id": "ashare_round_trip:%s:RT-1" % trade_date,
            "record_type": "completed_round_trip",
            "round_trip_complete": True,
            "execution_eligible": True,
            "costs_cover": "round_trip",
            "market": "ashare",
            "symbol": symbol,
            "trade_date": trade_date,
            "closed_at": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}T15:00:00+08:00",
            "prediction_snapshot_id": prediction["snapshot_id"],
            "capital_authority_id": prediction["capital_authority_id"],
            "authority_generation": prediction["authority_generation"],
            "execution_lineage_id": prediction["execution_lineage_id"],
            "entry_quantity": entry_qty,
            "entry_price": entry_price,
            "gross_pnl_cny": 100.0,
            "fee_cny": fee_cny,
            "slippage_cny": slippage_cny,
            "net_pnl_cny": 89.0,
            "sample_layer": "completed_round_trip",
            "real_trading_enabled": False,
        }
        self.journal.append_sample(rt_event)

    def _add_unverified_fill(self, symbol: str = "000001.SZ") -> None:
        fill_event = {
            "journal_event_type": "sample_event",
            "journal_event_id": "ashare_fill:20260713:UNVERIFIED",
            "record_type": "fill",
            "market": "ashare",
            "symbol": symbol,
            "trade_date": "20260713",
            "execution_eligible": False,
            "filled_quantity": 100,
            "filled_price": 10.0,
            "fee_cny": 5.0,
            "slippage_cny": 0.5,
            "sample_layer": "chain_validation",
            "real_trading_enabled": False,
        }
        self.journal.append_sample(fill_event)

    # -- actual execution cost priority tests ----------------------------------

    def test_one_sided_buy_fill_never_fabricates_actual_round_trip_costs(self) -> None:
        self._add_prediction("000001.SZ")
        # A buy fill contains no actual exit cost and must not be doubled.
        self._add_verified_fill(
            "000001.SZ", filled_price=10.0, fee_cny=5.0, slippage_cny=0.5
        )

        reader = FakeAshareReader(
            intraday=[
                {
                    "close": 10.2,
                    "bar_time": "2026-07-13T10:00:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                },
            ],
            daily=[
                {
                    "close": 10.5,
                    "trade_date": "20260713",
                    "source": "SharedSignals/market_data",
                },
                {
                    "close": 10.8,
                    "trade_date": "20260714",
                    "source": "SharedSignals/market_data",
                },
            ],
        )

        report = run_ashare_forward_label_ops(
            journal_path=self.path,
            trade_date="20260713",
            as_of="2026-07-14T16:00:00+08:00",
            reader=reader,
            environ={},
        )

        self.assertEqual(report["counts"]["actual_execution_cost_used"], 0)
        latest = self.journal.latest_sample_records()[0]
        label = latest["labels"]["m30"]
        self.assertEqual(
            label["cost_model_version"], "ashare-execution-reality-20260706-v1"
        )
        self.assertIsNone(label["cost_evidence_event_id"])
        self.assertEqual(label["fee_bps"], pytest.approx(105.0))
        self.assertEqual(label["slippage_bps"], pytest.approx(10.0))
        self.assertEqual(label["net_return_after_costs"], pytest.approx(0.0085))

    def test_completed_round_trip_preferred_over_buy_fill(self) -> None:
        prediction = self._add_prediction("000001.SZ")
        # Both a fill and a round trip exist. Round trip has total costs.
        self._add_verified_fill("000001.SZ", fee_cny=5.0, slippage_cny=0.5)
        # Round trip: entry 10*100=1000, fee=10.5, slip=1.0
        # fee_bps = 10.5/1000*10000 = 105, slip_bps = 1.0/1000*10000 = 10
        self._add_round_trip(
            prediction,
            "000001.SZ",
            entry_price=10.0,
            entry_qty=100,
            fee_cny=10.5,
            slippage_cny=1.0,
            trade_date="20260714",
        )

        reader = FakeAshareReader(
            intraday=[
                {
                    "close": 10.2,
                    "bar_time": "2026-07-13T10:00:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                },
            ],
            daily=[
                {
                    "close": 10.5,
                    "trade_date": "20260713",
                    "source": "SharedSignals/market_data",
                },
            ],
        )

        report = run_ashare_forward_label_ops(
            journal_path=self.path,
            trade_date="20260713",
            as_of="2026-07-14T16:00:00+08:00",
            reader=reader,
            environ={},
        )

        self.assertEqual(report["counts"]["actual_execution_cost_used"], 1)
        latest = self.journal.latest_sample_records()[0]
        label = latest["labels"]["m30"]
        self.assertEqual(label["cost_model_version"], "actual_execution_costs_v1")
        # Should use round trip costs (total is already round-trip)
        self.assertEqual(label["fee_bps"], pytest.approx(105.0))
        self.assertEqual(label["slippage_bps"], pytest.approx(10.0))

    def test_unverified_fill_does_not_override_conservative(self) -> None:
        self._add_prediction("000001.SZ")
        self._add_unverified_fill("000001.SZ")

        reader = FakeAshareReader(
            intraday=[
                {
                    "close": 10.2,
                    "bar_time": "2026-07-13T10:00:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                },
            ],
            daily=[
                {
                    "close": 10.5,
                    "trade_date": "20260713",
                    "source": "SharedSignals/market_data",
                },
            ],
        )

        report = run_ashare_forward_label_ops(
            journal_path=self.path,
            trade_date="20260713",
            as_of="2026-07-14T16:00:00+08:00",
            reader=reader,
            environ={},
        )

        self.assertEqual(report["counts"]["actual_execution_cost_used"], 0)
        latest = self.journal.latest_sample_records()[0]
        label = latest["labels"]["m30"]
        self.assertEqual(
            label["cost_model_version"], "ashare-execution-reality-20260706-v1"
        )

    def test_different_symbol_fill_does_not_override(self) -> None:
        self._add_prediction("000001.SZ")
        self._add_verified_fill("000002.SZ")  # Different symbol

        reader = FakeAshareReader(
            intraday=[
                {
                    "close": 10.2,
                    "bar_time": "2026-07-13T10:00:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                },
            ],
            daily=[
                {
                    "close": 10.5,
                    "trade_date": "20260713",
                    "source": "SharedSignals/market_data",
                },
            ],
        )

        report = run_ashare_forward_label_ops(
            journal_path=self.path,
            trade_date="20260713",
            as_of="2026-07-14T16:00:00+08:00",
            reader=reader,
            environ={},
        )

        self.assertEqual(report["counts"]["actual_execution_cost_used"], 0)
        latest = self.journal.latest_sample_records()[0]
        label = latest["labels"]["m30"]
        self.assertEqual(
            label["cost_model_version"], "ashare-execution-reality-20260706-v1"
        )

    def test_future_fill_does_not_override_point_in_time(self) -> None:
        """Fill with trade_date after as_of must not be used."""
        self._add_prediction("000001.SZ")
        # Fill dated 20260715 but as_of is 20260714
        self._add_verified_fill("000001.SZ", trade_date="20260715")

        reader = FakeAshareReader(
            intraday=[
                {
                    "close": 10.2,
                    "bar_time": "2026-07-13T10:00:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                },
            ],
            daily=[
                {
                    "close": 10.5,
                    "trade_date": "20260713",
                    "source": "SharedSignals/market_data",
                },
            ],
        )

        report = run_ashare_forward_label_ops(
            journal_path=self.path,
            trade_date="20260713",
            as_of="2026-07-14T16:00:00+08:00",
            reader=reader,
            environ={},
        )

        self.assertEqual(report["counts"]["actual_execution_cost_used"], 0)
        latest = self.journal.latest_sample_records()[0]
        label = latest["labels"]["m30"]
        self.assertEqual(
            label["cost_model_version"], "ashare-execution-reality-20260706-v1"
        )

    def test_observation_without_fill_uses_conservative_model(self) -> None:
        """A prediction without any fill still uses embedded conservative costs."""
        self._add_prediction("000001.SZ")
        # No fill added

        reader = FakeAshareReader(
            intraday=[
                {
                    "close": 10.2,
                    "bar_time": "2026-07-13T10:00:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                },
            ],
            daily=[
                {
                    "close": 10.5,
                    "trade_date": "20260713",
                    "source": "SharedSignals/market_data",
                },
            ],
        )

        report = run_ashare_forward_label_ops(
            journal_path=self.path,
            trade_date="20260713",
            as_of="2026-07-14T16:00:00+08:00",
            reader=reader,
            environ={},
        )

        self.assertEqual(report["counts"]["actual_execution_cost_used"], 0)
        latest = self.journal.latest_sample_records()[0]
        label = latest["labels"]["m30"]
        self.assertEqual(
            label["cost_model_version"], "ashare-execution-reality-20260706-v1"
        )

    def test_actual_costs_with_idempotent_repeat_respect_same_evidence(self) -> None:
        """Same evidence id = idempotent; different evidence id = new append."""
        self._add_prediction("000001.SZ")
        self._add_verified_fill("000001.SZ")

        reader = FakeAshareReader(
            intraday=[
                {
                    "close": 10.2,
                    "bar_time": "2026-07-13T10:00:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                },
            ],
            daily=[
                {
                    "close": 10.5,
                    "trade_date": "20260713",
                    "source": "SharedSignals/market_data",
                },
            ],
        )
        kwargs = {
            "journal_path": self.path,
            "trade_date": "20260713",
            "as_of": "2026-07-14T16:00:00+08:00",
            "reader": reader,
            "environ": {},
        }

        first = run_ashare_forward_label_ops(**kwargs)
        second = run_ashare_forward_label_ops(**kwargs)

        self.assertEqual(first["counts"]["new_label_updates"], 1)
        self.assertEqual(second["counts"]["new_label_updates"], 0)
        self.assertEqual(second["counts"]["idempotent_label_updates"], 1)

    def test_label_preserves_gross_and_net_separately_with_actual_costs(self) -> None:
        """Gross return, fee_bps, slippage_bps, and net_return are separate fields."""
        prediction = self._add_prediction("000001.SZ")
        self._add_round_trip(
            prediction,
            "000001.SZ",
            entry_price=10.0,
            entry_qty=100,
            fee_cny=10.5,
            slippage_cny=1.0,
            trade_date="20260714",
        )

        reader = FakeAshareReader(
            intraday=[
                {
                    "close": 10.15,  # +1.5% move
                    "bar_time": "2026-07-13T10:00:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                },
            ],
            daily=[
                {
                    "close": 10.5,
                    "trade_date": "20260713",
                    "source": "SharedSignals/market_data",
                },
            ],
        )

        run_ashare_forward_label_ops(
            journal_path=self.path,
            trade_date="20260713",
            as_of="2026-07-14T16:00:00+08:00",
            reader=reader,
            environ={},
        )

        latest = self.journal.latest_sample_records()[0]
        label = latest["labels"]["m30"]
        # gross = (10.15-10.0)/10.0 = 0.015
        self.assertEqual(label["market_return"], pytest.approx(0.015))
        self.assertEqual(label["gross_return_after_direction"], pytest.approx(0.015))
        self.assertEqual(label["fee_bps"], pytest.approx(105.0))
        self.assertEqual(label["slippage_bps"], pytest.approx(10.0))
        self.assertEqual(label["total_cost_bps"], pytest.approx(115.0))
        self.assertEqual(label["net_return_after_costs"], pytest.approx(0.0035))
        self.assertEqual(label["cost_model_version"], "actual_execution_costs_v1")
        self.assertIsNotNone(label["cost_evidence_event_id"])


if __name__ == "__main__":
    unittest.main()
