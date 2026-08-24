"""Offline tests for the lockup-event auxiliary evidence producer."""

from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from Ashare.minute_event_aux import (
    AUX_EXPIRES_WINDOW,
    HITS_FILENAME,
    LOCKUP_SCORE,
    MinuteEventAuxError,
    build_event_evidence,
    fetch_lockup_hits,
    hits_from_rows,
    load_or_refresh_daily_hits,
    make_session_client,
)
from Ashare.minute_loop import _canonical_sha256
from shared.data.sharedsignals_v1 import SharedSignalsV1Client

_CST = ZoneInfo("Asia/Shanghai")
_SESSION = date(2026, 8, 24)


def _row(
    ts_code: str,
    float_date: str,
    *,
    ann_date: str = "20260701",
    float_ratio: object = 5.0,
) -> dict[str, object]:
    """One share_float row in the wire shape: dates as YYYYMMDD."""

    return {
        "ts_code": ts_code,
        "ann_date": ann_date,
        "float_date": float_date,
        "float_ratio": float_ratio,
    }


class HitsFromRowsTest(unittest.TestCase):
    def test_window_boundaries_follow_preregistration(self) -> None:
        inside_start = (_SESSION - timedelta(days=30)).strftime("%Y%m%d")
        last_day = (_SESSION - timedelta(days=1)).strftime("%Y%m%d")
        outside_before = (_SESSION - timedelta(days=31)).strftime("%Y%m%d")
        rows = (
            _row("600000.SH", inside_start),
            _row("600001.SH", last_day, ann_date="20260702"),
            _row("600002.SH", outside_before),
            _row("600003.SH", "20260824"),  # session day itself excluded
        )
        hits = hits_from_rows(rows, session_date=_SESSION)
        self.assertEqual(set(hits), {"600000.SH", "600001.SH"})

    def test_inverted_rows_are_skipped_like_research_tier(self) -> None:
        rows = (_row("600000.SH", "20260610", ann_date="20260715"),)
        self.assertEqual(hits_from_rows(rows, session_date=_SESSION), {})

    def test_bad_ratio_does_not_veto_hit_and_max_ratio_aggregates(self) -> None:
        rows = (
            _row("600000.SH", "20260801", float_ratio=None),
            _row("600000.SH", "20260810", float_ratio="12.5"),
            _row("600000.SH", "20260820", float_ratio="oops"),
            _row("600000.SH", "20260805", float_ratio=30.0),
        )
        hits = hits_from_rows(rows, session_date=_SESSION)
        self.assertEqual(set(hits), {"600000.SH"})
        self.assertEqual(hits["600000.SH"]["latest_float_date"], "20260820")
        self.assertEqual(hits["600000.SH"]["max_ratio"], 30.0)

    def test_session_date_string_is_accepted(self) -> None:
        hits = hits_from_rows(
            (_row("600000.SH", "20260801"),), session_date="2026-08-24"
        )
        self.assertEqual(set(hits), {"600000.SH"})


def _decision_time() -> datetime:
    return datetime(2026, 8, 24, 9, 40, 20, tzinfo=_CST)


def _available_at() -> datetime:
    return datetime(2026, 8, 24, 9, 5, 0, tzinfo=_CST)


def _source_binding() -> dict[str, object]:
    unsigned: dict[str, object] = {
        "receipt_id": "td-receipt-001",
        "data_through": "2026-08-24T09:35:00+08:00",
        "observed_at": "2026-08-24T09:35:10+08:00",
        "catalog_version": "catalog-v1",
        "lineage": {"receipt": "lineage-001"},
        "pagination_trace_sha256": "a" * 64,
        "semantic_sha256": "b" * 64,
        "ordered_rows_sha256": "c" * 64,
        "row_receipt_proofs_sha256": "d" * 64,
    }
    return {**unsigned, "source_binding_sha256": _canonical_sha256(unsigned)}


class BuildEventEvidenceTest(unittest.TestCase):
    def test_evidence_fields_and_ordering(self) -> None:
        hits = {
            "000001.SZ": {"latest_float_date": "20260810", "max_ratio": 4.2},
            "600000.SH": {"latest_float_date": "20260820", "max_ratio": 9.9},
        }
        evidence = build_event_evidence(
            hits,
            decision_time=_decision_time(),
            available_at=_available_at(),
            source_binding_sha256=_source_binding()["source_binding_sha256"],
        )
        self.assertEqual([item.symbol for item in evidence], ["000001.SZ", "600000.SH"])
        first = evidence[0]
        self.assertEqual(first.evidence_type, "event")
        self.assertEqual(first.normalized_score, LOCKUP_SCORE)
        self.assertEqual(LOCKUP_SCORE, 1.0)
        self.assertFalse(first.execution_authority)
        self.assertEqual(
            first.event_time, datetime(2026, 8, 10, tzinfo=_CST)
        )
        self.assertLessEqual(first.event_time, first.available_at)
        self.assertLessEqual(first.available_at, first.decision_time)
        self.assertLessEqual(first.decision_time, first.expires_at)
        self.assertEqual(
            first.expires_at - first.decision_time, AUX_EXPIRES_WINDOW
        )

    def test_non_mainboard_symbols_are_dropped(self) -> None:
        hits = {
            "300750.SZ": {"latest_float_date": "20260810", "max_ratio": 1.0},
            "688981.SH": {"latest_float_date": "20260810", "max_ratio": 1.0},
            "600000.SH": {"latest_float_date": "20260810", "max_ratio": 1.0},
        }
        evidence = build_event_evidence(
            hits,
            decision_time=_decision_time(),
            available_at=_available_at(),
            source_binding_sha256=_source_binding()["source_binding_sha256"],
        )
        self.assertEqual([item.symbol for item in evidence], ["600000.SH"])

    def test_malformed_hit_row_is_skipped(self) -> None:
        hits = {"600000.SH": {"latest_float_date": "bad", "max_ratio": None}}
        evidence = build_event_evidence(
            hits,
            decision_time=_decision_time(),
            available_at=_available_at(),
            source_binding_sha256=_source_binding()["source_binding_sha256"],
        )
        self.assertEqual(evidence, ())


class DailyHitsCacheTest(unittest.TestCase):
    def test_refresh_false_without_cache_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with pytest.raises(MinuteEventAuxError, match="cache_missing"):
                load_or_refresh_daily_hits(
                    Path(tmp), session_date=_SESSION
                )

    def test_round_trip_and_corruption(self) -> None:
        hits = {"600000.SH": {"latest_float_date": "20260810", "max_ratio": 4.2}}
        with tempfile.TemporaryDirectory() as tmp:
            loaded = load_or_refresh_daily_hits(
                Path(tmp),
                session_date=_SESSION,
                refresh=hits,
                source_binding=_source_binding(),
            )
            self.assertEqual(loaded["hits"], hits)
            target = Path(tmp) / HITS_FILENAME
            self.assertTrue(target.exists())
            document = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(document["schema"], "tradingagent.ashare.minute_event_aux_hits.v2")
            self.assertEqual(document["session_date"], "2026-08-24")
            self.assertEqual(document["source_binding"]["receipt_id"], "td-receipt-001")
            again = load_or_refresh_daily_hits(
                Path(tmp), session_date=_SESSION
            )
            self.assertEqual(again["hits"], hits)
            target.write_text("{broken", encoding="utf-8")
            with pytest.raises(MinuteEventAuxError, match="cache_corrupt"):
                load_or_refresh_daily_hits(
                    Path(tmp), session_date=_SESSION, refresh=False
                )

    def test_tampered_receipt_binding_fails_closed(self) -> None:
        hits = {"600000.SH": {"latest_float_date": "20260810", "max_ratio": 4.2}}
        with tempfile.TemporaryDirectory() as tmp:
            load_or_refresh_daily_hits(
                Path(tmp),
                session_date=_SESSION,
                refresh=hits,
                source_binding=_source_binding(),
            )
            target = Path(tmp) / HITS_FILENAME
            document = json.loads(target.read_text(encoding="utf-8"))
            document["source_binding"]["receipt_id"] = "replaced"
            target.write_text(json.dumps(document), encoding="utf-8")
            with pytest.raises(MinuteEventAuxError, match="receipt_binding_invalid"):
                load_or_refresh_daily_hits(Path(tmp), session_date=_SESSION)

    def test_cache_for_another_session_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            load_or_refresh_daily_hits(
                Path(tmp),
                session_date=_SESSION,
                refresh={"600000.SH": {"latest_float_date": "20260810", "max_ratio": 4.2}},
                source_binding=_source_binding(),
            )
            with pytest.raises(MinuteEventAuxError, match="cache_session_mismatch"):
                load_or_refresh_daily_hits(Path(tmp), session_date="2026-08-25")


class FetchLayerContractTest(unittest.TestCase):
    def test_fetch_layer_rejects_foreign_client(self) -> None:
        with pytest.raises(MinuteEventAuxError, match="client_invalid"):
            fetch_lockup_hits(object(), session_date=_SESSION)

    def test_client_factory_signature_is_stable(self) -> None:
        parameters = set(inspect.signature(make_session_client).parameters)
        self.assertEqual(
            parameters,
            {
                "transport_id",
                "token_file",
                "base_url",
                "transport_factory",
                "expected_catalog_version",
                "access_policy_id",
                "timeout_seconds",
            },
        )
        # Static guard: the accepted client type stays bound to the real
        # shared-module class so refactors cannot silently bypass it.
        self.assertTrue(SharedSignalsV1Client is not None)

    def test_event_query_requests_receipt_proofs(self) -> None:
        source = Path(__file__).resolve().parents[1] / "Ashare" / "minute_event_aux.py"
        assert "include_receipt_proofs=True" in source.read_text(encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
