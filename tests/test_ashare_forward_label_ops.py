from __future__ import annotations

import io
import json
import os
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pytest

from shared.review.sample_journal import (
    SampleJournal,
    build_strict_execution_evidence_index,
    prediction_source_payload_sha256,
    seal_strict_execution_event,
    strict_round_trip_content_sha256,
    strict_round_trip_source_sha256,
    validate_strict_completed_round_trip_evidence,
)
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
        rows = []
        for raw in self.intraday:
            row = dict(raw)
            event_time = row.get("bar_time") or row.get("trade_time")
            if event_time not in (None, ""):
                row.setdefault("available_at", event_time)
                row.setdefault("ingested_at", event_time)
                row.setdefault("retrieved_as_of", event_time)
            rows.append(row)
        return rows

    def get_bars_daily(self, market, symbol, start, end):
        self.calls.append(("daily", market, symbol, start, end))
        rows = []
        for raw in self.daily:
            row = dict(raw)
            trade_date = str(row.get("trade_date") or "").replace("-", "")
            if len(trade_date) == 8:
                close_time = "%s-%s-%sT15:00:00+08:00" % (
                    trade_date[:4],
                    trade_date[4:6],
                    trade_date[6:],
                )
                row.setdefault("available_at", close_time)
                row.setdefault("ingested_at", close_time)
                row.setdefault("retrieved_as_of", close_time)
            rows.append(row)
        return rows


def _prediction(
    *, prediction_at: str = "2026-07-13T09:30:00+08:00"
) -> dict[str, object]:
    receipt_at = (
        prediction_at
        if "+" in prediction_at[10:] or prediction_at.endswith("Z")
        else prediction_at + "+08:00"
    )
    return {
        "market": "Ashare",
        "symbol": "000001.SZ",
        "style": "trend_breakout",
        "strategy_version": "trend-v1",
        "prediction_at": prediction_at,
        "event_time": prediction_at,
        "available_at": receipt_at,
        "ingested_at": receipt_at,
        "retrieved_as_of": receipt_at,
        "point_in_time_lineage": {
            "timestamps": {
                "event_time": prediction_at,
                "available_at": receipt_at,
                "ingested_at": receipt_at,
                "retrieved_as_of": receipt_at,
            }
        },
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
            "reference_timestamp_lineage": {
                "source_field": "bar_time",
                "raw_value": prediction_at,
                "normalized_value": prediction_at,
                "timezone_semantics": "ashare_exchange_event_time",
                "normalization_rule": "convert_aware_instant_to_asia_shanghai",
                "valid": True,
            },
        },
        "decision_timestamp_lineage": {
            field: {
                "source_field": field,
                "raw_value": prediction_at,
                "normalized_value": prediction_at,
                "timezone_semantics": "ashare_decision_time",
                "normalization_rule": "convert_aware_instant_to_asia_shanghai",
                "valid": True,
            }
            for field in ("prediction_at", "data_as_of")
        },
    }


_RECEIPT_ALIAS_PATHS = (
    "receipt_at",
    "received_at",
    "retrieved_as_of",
    "collected_at",
    "ingested_at",
    "evidence_available_at",
    "available_at",
    "point_in_time_lineage.timestamps.receipt_at",
    "point_in_time_lineage.timestamps.received_at",
    "point_in_time_lineage.timestamps.retrieved_as_of",
    "point_in_time_lineage.timestamps.collected_at",
    "point_in_time_lineage.timestamps.ingested_at",
    "point_in_time_lineage.timestamps.evidence_available_at",
    "point_in_time_lineage.timestamps.available_at",
    "point_in_time_lineage.receipt_at",
    "point_in_time_lineage.received_at",
    "point_in_time_lineage.retrieved_as_of",
    "point_in_time_lineage.collected_at",
    "point_in_time_lineage.ingested_at",
    "point_in_time_lineage.evidence_available_at",
    "point_in_time_lineage.available_at",
)


def _raw_bar(price: float, event_time: str) -> dict[str, object]:
    return {
        "close": price,
        "bar_time": event_time,
        "available_at": event_time,
        "ingested_at": event_time,
        "retrieved_as_of": event_time,
        "point_in_time_lineage": {
            "timestamps": {
                "event_time": event_time,
                "available_at": event_time,
                "ingested_at": event_time,
                "retrieved_as_of": event_time,
            }
        },
        "source": "SharedSignals/realtime_5min",
    }


def _set_path(payload: dict[str, object], path: str, value: object) -> None:
    parts = path.split(".")
    current: dict[str, object] = payload
    for part in parts[:-1]:
        nested = current.setdefault(part, {})
        assert isinstance(nested, dict)
        current = nested
    current[parts[-1]] = value


def _run_adapter_bars(tmp_path: Path, bars: list[dict[str, object]]):
    journal = SampleJournal(tmp_path / "adapter-envelope.jsonl")
    journal.append_prediction(_prediction())
    report = run_ashare_forward_label_ops(
        journal_path=journal.path,
        trade_date="20260713",
        as_of="2026-07-13T11:30:00+08:00",
        reader=FakeAshareReader(intraday=bars),
        environ={},
    )
    return report, journal.latest_sample_records()[0]


@pytest.mark.parametrize("receipt_path", _RECEIPT_ALIAS_PATHS)
def test_adapter_each_present_future_receipt_alias_is_non_ready(
    tmp_path: Path, receipt_path: str
) -> None:
    bar = _raw_bar(99.0, "2026-07-13T10:00:00+08:00")
    _set_path(bar, receipt_path, "2026-07-13T12:00:00+08:00")

    _, latest = _run_adapter_bars(tmp_path, [bar])

    assert latest["labels"]["m30"]["status"] != "ready"
    assert latest["labels"]["m30"]["exit_price"] is None


def test_adapter_conflicting_high_price_event_alias_cannot_beat_valid_low_price(
    tmp_path: Path,
) -> None:
    invalid_high = _raw_bar(99.0, "2026-07-13T10:00:00+08:00")
    invalid_high["timestamp"] = "2026-07-13T11:00:00+08:00"
    valid_low = _raw_bar(10.1, "2026-07-13T10:01:00+08:00")

    report, latest = _run_adapter_bars(tmp_path, [invalid_high, valid_low])

    assert report["counts"]["bar_quality_rejections"] >= 1
    assert latest["labels"]["m30"]["status"] == "ready"
    assert latest["labels"]["m30"]["exit_price"] == 10.1


@pytest.mark.parametrize(
    "secondary_timestamp",
    ("2026-07-13T11:00:00+08:00", "2026-07-13T11:00:00"),
)
def test_adapter_only_conflicting_or_naive_secondary_event_is_non_ready(
    tmp_path: Path, secondary_timestamp: str
) -> None:
    bar = _raw_bar(99.0, "2026-07-13T10:00:00+08:00")
    bar["timestamp"] = secondary_timestamp

    _, latest = _run_adapter_bars(tmp_path, [bar])

    assert latest["labels"]["m30"]["status"] != "ready"
    assert latest["labels"]["m30"]["exit_price"] is None


def test_adapter_equivalent_shanghai_utc_event_aliases_are_ready(
    tmp_path: Path,
) -> None:
    bar = _raw_bar(10.2, "2026-07-13T10:00:00+08:00")
    bar["timestamp"] = "2026-07-13T02:00:00+00:00"
    bar["observed_at"] = "2026-07-13T02:00:00Z"

    _, latest = _run_adapter_bars(tmp_path, [bar])

    assert latest["labels"]["m30"]["status"] == "ready"
    assert latest["labels"]["m30"]["exit_price"] == 10.2


@pytest.mark.parametrize(
    "hidden_receipts",
    (
        {
            "published_at": "2026-07-13T12:00:00+08:00",
            "received_at": "2026-07-13T12:01:00+08:00",
        },
        {"retrieved_at": "2026-07-13T18:00:00+08:00"},
    ),
)
def test_adapter_hidden_future_receipts_cannot_win_selection(
    tmp_path: Path, hidden_receipts: dict[str, str]
) -> None:
    invalid_high = _raw_bar(98.0, "2026-07-13T10:00:00+08:00")
    invalid_high.update(hidden_receipts)
    valid_low = _raw_bar(10.1, "2026-07-13T10:01:00+08:00")

    _, latest = _run_adapter_bars(tmp_path, [invalid_high, valid_low])

    assert latest["labels"]["m30"]["status"] == "ready"
    assert latest["labels"]["m30"]["exit_price"] == 10.1


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

    def test_all_invalid_pit_points_remain_retryable_degraded_and_nonterminal(
        self,
    ) -> None:
        self.journal.append_prediction(_prediction())
        reader = FakeAshareReader(
            intraday=[
                {
                    "close": 12.0,
                    "bar_time": "2026-07-13T10:00:00+08:00",
                    "available_at": "2026-07-13T16:30:00+08:00",
                    "ingested_at": "2026-07-13T16:31:00+08:00",
                    "retrieved_as_of": "2026-07-13T16:00:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                }
            ]
        )

        report = run_ashare_forward_label_ops(
            journal_path=self.path,
            trade_date="20260713",
            as_of="2026-07-13T16:00:00+08:00",
            reader=reader,
            environ={},
        )

        self.assertEqual(report["counts"]["ready_labels"], 0)
        self.assertGreaterEqual(report["counts"]["missing_evidence"], 1)
        self.assertTrue(report["results"][0]["retryable"])
        self.assertTrue(report["results"][0]["degraded"])
        self.assertEqual(
            report["results"][0]["degraded_reason"], "no_exit_evidence_as_of"
        )
        self.assertEqual(len(report["results"][0]["bar_quality_rejections"]), 1)
        self.assertEqual(
            report["results"][0]["bar_quality_rejections"][0][
                "evidence_envelope_validation"
            ]["status"],
            "invalid_receipt_order",
        )
        latest = self.journal.latest_sample_records()[0]
        self.assertEqual(latest["labels"]["m30"]["status"], "missing_exit_evidence")
        self.assertIsNone(latest["labels"]["m30"]["exit_price"])
        self.assertFalse(latest["labels"]["m30"]["point_in_time_lineage"]["complete"])

    def test_conflicting_top_and_nested_event_clock_is_retryable_degraded(
        self,
    ) -> None:
        self.journal.append_prediction(_prediction())
        reader = FakeAshareReader(
            intraday=[
                {
                    "close": 99.0,
                    "bar_time": "2026-07-13T10:00:00+08:00",
                    "available_at": "2026-07-13T10:01:00+08:00",
                    "ingested_at": "2026-07-13T10:02:00+08:00",
                    "retrieved_as_of": "2026-07-13T10:03:00+08:00",
                    "point_in_time_lineage": {
                        "timestamps": {
                            "event_time": "2026-07-13T10:30:00+08:00",
                            "available_at": "2026-07-13T10:31:00+08:00",
                            "ingested_at": "2026-07-13T10:32:00+08:00",
                            "retrieved_as_of": "2026-07-13T10:33:00+08:00",
                        }
                    },
                    "source": "SharedSignals/realtime_5min",
                }
            ]
        )

        report = run_ashare_forward_label_ops(
            journal_path=self.path,
            trade_date="20260713",
            as_of="2026-07-13T11:00:00+08:00",
            reader=reader,
            environ={},
        )

        self.assertEqual(report["counts"]["ready_labels"], 0)
        self.assertTrue(report["results"][0]["retryable"])
        self.assertTrue(report["results"][0]["degraded"])
        self.assertEqual(
            report["results"][0]["degraded_reason"], "no_exit_evidence_as_of"
        )
        self.assertEqual(len(report["results"][0]["bar_quality_rejections"]), 1)
        self.assertEqual(
            report["results"][0]["bar_quality_rejections"][0][
                "evidence_envelope_validation"
            ]["status"],
            "event_time_conflict",
        )
        latest = self.journal.latest_sample_records()[0]
        self.assertEqual(latest["labels"]["m30"]["status"], "missing_exit_evidence")
        self.assertIsNone(latest["labels"]["m30"]["exit_price"])

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
        self.assertEqual(report["counts"]["filtered_predictions"], 0)
        self.assertEqual(report["frozen_head"]["excluded_after_as_of_count"], 1)

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

    def test_naive_prediction_timestamp_fails_closed_before_market_read(self) -> None:
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

        self.assertEqual(report["counts"]["data_quality_rejected"], 6)
        self.assertEqual(report["results"][0]["status"], "rejected_data_quality")
        self.assertEqual(report["results"][0]["reason"], "invalid_prediction_timestamp")
        self.assertEqual(reader.calls, [])
        self.assertEqual(len(self.journal.read_events()), 1)

    def test_missing_reference_price_is_retryable_degraded_without_fake_price(
        self,
    ) -> None:
        prediction = _prediction()
        prediction["reference_price"] = None
        prediction["data_quality"] = {
            "reliable": False,
            "source": None,
            "price_timestamp": None,
        }
        self.journal.append_prediction(prediction)

        report = run_ashare_forward_label_ops(
            journal_path=self.path,
            trade_date="20260713",
            as_of="2026-07-13T10:00:00+08:00",
            reader=FakeAshareReader(),
            environ={},
        )

        self.assertEqual(report["counts"]["data_quality_rejected"], 0)
        self.assertEqual(report["counts"]["missing_evidence"], 1)
        self.assertTrue(report["results"][0]["retryable"])
        self.assertTrue(report["results"][0]["degraded"])
        self.assertEqual(
            report["results"][0]["degraded_reason"], "missing_reference_price"
        )
        latest = self.journal.latest_sample_records()[0]
        self.assertEqual(latest["labels"]["m30"]["status"], "missing_exit_evidence")
        self.assertEqual(latest["labels"]["m30"]["reason"], "missing_reference_price")
        self.assertIsNone(latest["labels"]["m30"]["exit_price"])

    def test_missing_strict_reference_lineage_is_retryable_degraded_not_verified(
        self,
    ) -> None:
        prediction = _prediction()
        del prediction["data_quality"]["reference_timestamp_lineage"]
        self.journal.append_prediction(prediction)

        report = run_ashare_forward_label_ops(
            journal_path=self.path,
            trade_date="20260713",
            as_of="2026-07-13T10:00:00+08:00",
            reader=FakeAshareReader(),
            environ={},
        )

        self.assertEqual(report["counts"]["ready_labels"], 0)
        self.assertEqual(report["counts"]["missing_evidence"], 1)
        self.assertTrue(report["results"][0]["retryable"])
        self.assertTrue(report["results"][0]["degraded"])
        self.assertEqual(
            report["results"][0]["degraded_reason"],
            "missing_reference_timestamp_lineage",
        )
        latest = self.journal.latest_sample_records()[0]
        self.assertEqual(latest["labels"]["m30"]["status"], "missing_exit_evidence")
        self.assertEqual(
            latest["labels"]["m30"]["reason"],
            "missing_reference_timestamp_lineage",
        )

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

    def _add_prediction(
        self,
        symbol: str = "000001.SZ",
        *,
        source_payload_mode: str = "valid",
    ) -> dict[str, object]:
        source_snapshot_payload = {
            "source": "SharedSignals/reference",
            "symbol": symbol,
            "prediction_at": "2026-07-13T09:30:00+08:00",
            "reference_price": 10.0,
        }
        pred = {
            "market": "Ashare",
            "symbol": symbol,
            "style": "trend_breakout",
            "strategy_version": "trend-v1",
            "prediction_at": "2026-07-13T09:30:00+08:00",
            "event_time": "2026-07-13T09:30:00+08:00",
            "available_at": "2026-07-13T09:30:00+08:00",
            "ingested_at": "2026-07-13T09:30:00+08:00",
            "retrieved_as_of": "2026-07-13T09:30:00+08:00",
            "point_in_time_lineage": {
                "timestamps": {
                    "event_time": "2026-07-13T09:30:00+08:00",
                    "available_at": "2026-07-13T09:30:00+08:00",
                    "ingested_at": "2026-07-13T09:30:00+08:00",
                    "retrieved_as_of": "2026-07-13T09:30:00+08:00",
                }
            },
            "reference_price": 10.0,
            "direction": "long",
            "trade_date": "20260713",
            "capital_authority_id": "ashare-capital-v1",
            "authority_generation": 1,
            "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
            "source_snapshot_payload": source_snapshot_payload,
            "source_snapshot_sha256": prediction_source_payload_sha256(
                source_snapshot_payload
            ),
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
                "reference_timestamp_lineage": {
                    "source_field": "bar_time",
                    "raw_value": "2026-07-13T09:30:00+08:00",
                    "normalized_value": "2026-07-13T09:30:00+08:00",
                    "timezone_semantics": "ashare_exchange_event_time",
                    "normalization_rule": "convert_aware_instant_to_asia_shanghai",
                    "valid": True,
                },
            },
            "decision_timestamp_lineage": {
                field: {
                    "source_field": field,
                    "raw_value": "2026-07-13T09:30:00+08:00",
                    "normalized_value": "2026-07-13T09:30:00+08:00",
                    "timezone_semantics": "ashare_decision_time",
                    "normalization_rule": "convert_aware_instant_to_asia_shanghai",
                    "valid": True,
                }
                for field in ("prediction_at", "data_as_of")
            },
        }
        if source_payload_mode == "missing":
            pred.pop("source_snapshot_payload")
            pred["source_snapshot_sha256"] = "a" * 64
        elif source_payload_mode == "mismatch":
            pred["source_snapshot_payload"] = {
                **source_snapshot_payload,
                "reference_price": 99.0,
            }
        elif source_payload_mode != "valid":
            raise ValueError("unsupported source payload mode")
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
            "event_at": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}T10:00:00+08:00",
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
        mutate=None,
        entry_mutate=None,
        exit_mutate=None,
    ) -> None:
        closed_at = (
            f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}T15:00:00+08:00"
        )
        snapshot_id = str(prediction["snapshot_id"])
        canonical_timestamps = {
            "event_time": closed_at,
            "available_at": closed_at,
            "ingested_at": closed_at,
            "retrieved_as_of": closed_at,
        }
        evidence_envelope = {
            "event_time_fields": {"receipt.filled_at": closed_at},
            "availability_time_fields": {"receipt.available_at": closed_at},
            "ingestion_time_fields": {"receipt.received_at": closed_at},
            "retrieval_time_fields": {"receipt.retrieved_at": closed_at},
            "structure_errors": [],
        }
        entry_at = "2026-07-13T10:00:00+08:00"
        entry_timestamps = {
            field: entry_at
            for field in (
                "event_time",
                "available_at",
                "ingested_at",
                "retrieved_as_of",
            )
        }
        entry_envelope = {
            "event_time_fields": {"receipt.filled_at": entry_at},
            "availability_time_fields": {"receipt.available_at": entry_at},
            "ingestion_time_fields": {"receipt.received_at": entry_at},
            "retrieval_time_fields": {"receipt.retrieved_at": entry_at},
            "structure_errors": [],
        }
        entry_identity = "entry|%s|buy|BUY-1" % snapshot_id
        exit_identity = "exit|%s|sell|SELL-1" % snapshot_id
        prediction_source_sha256 = str(prediction["source_snapshot_sha256"])
        prediction_content_sha256 = str(prediction["prediction_content_sha256"])
        entry_fee = fee_cny / 2.0
        exit_fee = fee_cny - entry_fee
        entry_slippage = slippage_cny / 2.0
        exit_slippage = slippage_cny - entry_slippage
        entry_event = seal_strict_execution_event(
            {
                "journal_event_type": "sample_event",
                "journal_event_id": "ashare_fill:%s:BUY-1" % trade_date,
                "event_id": "ashare_fill:%s:BUY-1" % trade_date,
                "record_type": "fill",
                "status": "filled",
                "sample_intent": "exploration",
                "execution_eligible": True,
                "market": "ashare",
                "symbol": symbol,
                "trade_date": "20260713",
                "fill_identity": entry_identity,
                "filled_quantity": entry_qty,
                "filled_price": entry_price,
                "fee_cny": entry_fee,
                "slippage_cny": entry_slippage,
                "event_time": entry_at,
                "source_event_time": entry_at,
                "available_at": entry_at,
                "ingested_at": entry_at,
                "retrieved_as_of": entry_at,
                "evidence_envelope": entry_envelope,
                "evidence_envelope_validation": {
                    "status": "valid",
                    "complete": True,
                    "canonical_timestamps": entry_timestamps,
                },
                "point_in_time_lineage": {
                    "status": "valid",
                    "complete": True,
                    "timestamps": entry_timestamps,
                    "evidence_envelope": entry_envelope,
                },
                "capital_authority_id": prediction["capital_authority_id"],
                "authority_generation": prediction["authority_generation"],
                "execution_lineage_id": prediction["execution_lineage_id"],
                "prediction_snapshot_id": snapshot_id,
                "prediction_source_snapshot_sha256": prediction_source_sha256,
                "prediction_content_sha256": prediction_content_sha256,
                "real_trading_enabled": False,
            }
        )
        exit_price = entry_price + (100.0 / entry_qty)
        exit_event = seal_strict_execution_event(
            {
                "journal_event_type": "sample_event",
                "journal_event_id": "ashare_stop:%s:SELL-1" % trade_date,
                "event_id": "ashare_stop:%s:SELL-1" % trade_date,
                "record_type": "stop",
                "status": "filled",
                "sample_intent": "exploration",
                "execution_eligible": True,
                "market": "ashare",
                "symbol": symbol,
                "trade_date": trade_date,
                "fill_identity": exit_identity,
                "entry_fill_identity": entry_identity,
                "filled_quantity": entry_qty,
                "filled_price": exit_price,
                "fee_cny": exit_fee,
                "slippage_cny": exit_slippage,
                "event_time": closed_at,
                "source_event_time": closed_at,
                "available_at": closed_at,
                "ingested_at": closed_at,
                "retrieved_as_of": closed_at,
                "as_of": closed_at,
                "point_in_time_as_of": closed_at,
                "evidence_envelope": evidence_envelope,
                "evidence_envelope_validation": {
                    "status": "valid",
                    "complete": True,
                    "canonical_timestamps": canonical_timestamps,
                },
                "point_in_time_lineage": {
                    "status": "valid",
                    "complete": True,
                    "timestamps": canonical_timestamps,
                    "evidence_envelope": evidence_envelope,
                },
                "capital_authority_id": prediction["capital_authority_id"],
                "authority_generation": prediction["authority_generation"],
                "execution_lineage_id": prediction["execution_lineage_id"],
                "prediction_snapshot_id": snapshot_id,
                "prediction_source_snapshot_sha256": prediction_source_sha256,
                "prediction_content_sha256": prediction_content_sha256,
                "real_trading_enabled": False,
            }
        )
        if entry_mutate is not None:
            entry_mutate(entry_event)
        if exit_mutate is not None:
            exit_mutate(exit_event)
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
            "closed_at": closed_at,
            "event_time": closed_at,
            "available_at": closed_at,
            "ingested_at": closed_at,
            "retrieved_as_of": closed_at,
            "prediction_snapshot_id": snapshot_id,
            "capital_authority_id": prediction["capital_authority_id"],
            "authority_generation": prediction["authority_generation"],
            "execution_lineage_id": prediction["execution_lineage_id"],
            "prediction_source_snapshot_sha256": prediction_source_sha256,
            "prediction_content_sha256": prediction_content_sha256,
            "entry_fill_identity": entry_identity,
            "exit_fill_identities": [exit_identity],
            "entry_receipt_sha256": entry_event["receipt_sha256"],
            "entry_local_trade_sha256": entry_event["local_trade_sha256"],
            "exit_receipt_sha256s": [exit_event["receipt_sha256"]],
            "exit_local_trade_sha256s": [exit_event["local_trade_sha256"]],
            "entry_quantity": entry_qty,
            "entry_price": entry_price,
            "notional_cny": entry_qty * entry_price,
            "gross_pnl_cny": 100.0,
            "fee_cny": fee_cny,
            "slippage_cny": slippage_cny,
            "net_pnl_cny": 100.0 - fee_cny - slippage_cny,
            "sample_layer": "completed_round_trip",
            "evidence_envelope": evidence_envelope,
            "evidence_envelope_validation": {
                "status": "valid",
                "complete": True,
                "canonical_timestamps": dict(canonical_timestamps),
            },
            "point_in_time_lineage": {
                "status": "valid",
                "complete": True,
                "timestamps": dict(canonical_timestamps),
                "evidence_envelope": evidence_envelope,
            },
            "real_trading_enabled": False,
        }
        rt_event["source_snapshot_sha256"] = strict_round_trip_source_sha256(rt_event)
        rt_event["content_sha256"] = strict_round_trip_content_sha256(rt_event)
        if mutate is not None:
            mutate(rt_event)
        self.journal.append_samples([entry_event, exit_event, rt_event])

    def _add_unverified_fill(self, symbol: str = "000001.SZ") -> None:
        fill_event = {
            "journal_event_type": "sample_event",
            "journal_event_id": "ashare_fill:20260713:UNVERIFIED",
            "record_type": "fill",
            "market": "ashare",
            "symbol": symbol,
            "trade_date": "20260713",
            "event_at": "2026-07-13T10:00:00+08:00",
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
        events = self.journal.read_events()
        round_trip = next(
            event
            for event in events
            if event.get("record_type") == "completed_round_trip"
        )
        strict_validation = validate_strict_completed_round_trip_evidence(
            round_trip,
            boundary=datetime.fromisoformat("2026-07-14T16:00:00+08:00"),
            prediction_snapshot_id=str(prediction["snapshot_id"]),
            evidence_index=build_strict_execution_evidence_index(events),
        )
        self.assertTrue(strict_validation["valid"], strict_validation)

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

        self.assertEqual(report["counts"]["actual_execution_cost_used"], 1, report)
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

    def test_invalid_strict_round_trip_lineage_keeps_conservative_costs(self) -> None:
        def missing_receipts(event) -> None:
            for field_name in ("available_at", "ingested_at", "retrieved_as_of"):
                event.pop(field_name, None)
                event["point_in_time_lineage"]["timestamps"].pop(field_name, None)
            for group in (
                "availability_time_fields",
                "ingestion_time_fields",
                "retrieval_time_fields",
            ):
                event["evidence_envelope"][group] = {}

        def future_receipt(event) -> None:
            event["evidence_envelope"]["retrieval_time_fields"][
                "receipt.retrieved_at"
            ] = "2026-07-14T17:00:00+08:00"

        def reversed_receipt_stage(event) -> None:
            event["evidence_envelope"]["availability_time_fields"][
                "receipt.available_at"
            ] = "2026-07-14T15:05:00+08:00"
            event["evidence_envelope"]["ingestion_time_fields"][
                "receipt.received_at"
            ] = "2026-07-14T15:04:00+08:00"

        def naive_receipt(event) -> None:
            event["evidence_envelope"]["retrieval_time_fields"][
                "receipt.retrieved_at"
            ] = "2026-07-14T15:00:00"

        def conflicting_event_time(event) -> None:
            event["evidence_envelope"]["event_time_fields"]["receipt.timestamp"] = (
                "2026-07-14T15:01:00+08:00"
            )

        mutations = {
            "missing_pit_lineage": lambda event: event.pop("point_in_time_lineage"),
            "missing_source_hash": lambda event: event.pop("source_snapshot_sha256"),
            "invalid_content_hash": lambda event: event.__setitem__(
                "content_sha256", "invalid"
            ),
            "missing_execution_fingerprint": lambda event: event.pop(
                "entry_receipt_sha256"
            ),
            "missing_receipts": missing_receipts,
            "future_receipt": future_receipt,
            "reversed_receipt_stage": reversed_receipt_stage,
            "naive_receipt": naive_receipt,
            "conflicting_event_time": conflicting_event_time,
        }
        reader = FakeAshareReader(
            intraday=[
                {
                    "close": 10.2,
                    "bar_time": "2026-07-13T10:00:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                }
            ],
            daily=[
                {
                    "close": 10.5,
                    "trade_date": "20260713",
                    "source": "SharedSignals/market_data",
                }
            ],
        )

        for case_name, mutate in mutations.items():
            with self.subTest(case_name=case_name):
                self.path = Path(self.tmp.name) / (case_name + ".jsonl")
                self.journal = SampleJournal(self.path)
                prediction = self._add_prediction("000001.SZ")
                self._add_round_trip(prediction, mutate=mutate)

                report = run_ashare_forward_label_ops(
                    journal_path=self.path,
                    trade_date="20260713",
                    as_of="2026-07-14T16:00:00+08:00",
                    reader=reader,
                    environ={},
                )

                self.assertEqual(report["counts"]["actual_execution_cost_used"], 0)
                label = self.journal.latest_sample_records()[0]["labels"]["m30"]
                self.assertEqual(
                    label["cost_model_version"],
                    "ashare-execution-reality-20260706-v1",
                )

    def test_strict_execution_hashes_are_bound_to_authoritative_content(self) -> None:
        def empty_envelope_with_convenience_fields(event) -> None:
            event["evidence_envelope"] = {}

        cases = {
            "arbitrary_round_trip_64hex": {
                "mutate": lambda event: event.__setitem__(
                    "entry_receipt_sha256", "a" * 64
                )
            },
            "prediction_content_sha_mismatch": {
                "mutate": lambda event: event.__setitem__(
                    "prediction_content_sha256", "b" * 64
                )
            },
            "prediction_source_sha_mismatch": {
                "mutate": lambda event: event.__setitem__(
                    "prediction_source_snapshot_sha256", "9" * 64
                )
            },
            "empty_envelope_convenience_fields": {
                "mutate": empty_envelope_with_convenience_fields
            },
            "entry_receipt_payload_changed_hash_unchanged": {
                "entry_mutate": lambda event: event[
                    "execution_receipt_payload"
                ].__setitem__("filled_price", 999.0)
            },
            "exit_local_payload_changed_hash_unchanged": {
                "exit_mutate": lambda event: event[
                    "execution_local_trade_payload"
                ].__setitem__("symbol", "999999.SH")
            },
            "entry_receipt_hash_changed_content_unchanged": {
                "entry_mutate": lambda event: event.__setitem__(
                    "receipt_sha256", "c" * 64
                )
            },
            "exit_local_hash_changed_content_unchanged": {
                "exit_mutate": lambda event: event.__setitem__(
                    "local_trade_sha256", "d" * 64
                )
            },
            "entry_fingerprint_mismatch": {
                "mutate": lambda event: event.__setitem__(
                    "entry_local_trade_sha256", "e" * 64
                )
            },
            "exit_fingerprint_mismatch": {
                "mutate": lambda event: event.__setitem__(
                    "exit_receipt_sha256s", ["f" * 64]
                )
            },
        }
        reader = FakeAshareReader(
            intraday=[
                {
                    "close": 10.2,
                    "bar_time": "2026-07-13T10:00:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                }
            ],
            daily=[
                {
                    "close": 10.5,
                    "trade_date": "20260713",
                    "source": "SharedSignals/market_data",
                }
            ],
        )

        for case_name, mutations in cases.items():
            with self.subTest(case_name=case_name):
                self.path = Path(self.tmp.name) / ("strict_%s.jsonl" % case_name)
                self.journal = SampleJournal(self.path)
                prediction = self._add_prediction("000001.SZ")
                self._add_round_trip(prediction, **mutations)

                report = run_ashare_forward_label_ops(
                    journal_path=self.path,
                    trade_date="20260713",
                    as_of="2026-07-14T16:00:00+08:00",
                    reader=reader,
                    environ={},
                )

                self.assertEqual(
                    report["counts"]["actual_execution_cost_used"],
                    0,
                    report,
                )
                label = self.journal.latest_sample_records()[0]["labels"]["m30"]
                self.assertEqual(
                    label["cost_model_version"],
                    "ashare-execution-reality-20260706-v1",
                )

    def test_prediction_source_payload_is_required_and_content_bound(self) -> None:
        reader = FakeAshareReader(
            intraday=[
                {
                    "close": 10.2,
                    "bar_time": "2026-07-13T10:00:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                }
            ],
            daily=[
                {
                    "close": 10.5,
                    "trade_date": "20260713",
                    "source": "SharedSignals/market_data",
                }
            ],
        )
        for mode in ("missing", "mismatch"):
            with self.subTest(source_payload_mode=mode):
                self.path = Path(self.tmp.name) / ("prediction_source_%s.jsonl" % mode)
                self.journal = SampleJournal(self.path)
                prediction = self._add_prediction(
                    "000001.SZ",
                    source_payload_mode=mode,
                )
                self._add_round_trip(prediction)

                report = run_ashare_forward_label_ops(
                    journal_path=self.path,
                    trade_date="20260713",
                    as_of="2026-07-14T16:00:00+08:00",
                    reader=reader,
                    environ={},
                )

                self.assertEqual(
                    report["counts"]["actual_execution_cost_used"],
                    0,
                    report,
                )
                label = self.journal.latest_sample_records()[0]["labels"]["m30"]
                self.assertEqual(
                    label["cost_model_version"],
                    "ashare-execution-reality-20260706-v1",
                )


if __name__ == "__main__":
    unittest.main()
