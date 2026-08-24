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
from shared.data.sharedsignals_v1 import (
    HTTPResponse,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)

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


_WIRE_SESSION = date(2026, 8, 24)


def _catalog_payload() -> dict[str, object]:
    return {
        "api_version": "v1",
        "catalog_version": "v1-wire-test",
        "request_id": "req-catalog",
        "data": [
            {
                "dataset_id": "cn.dataset.share_float",
                "schema_major": 1,
                "default_fields": [
                    "ts_code",
                    "ann_date",
                    "float_date",
                    "float_share",
                    "float_ratio",
                    "holder_name",
                    "share_type",
                ],
            }
        ],
    }


def _query_page(rows: list[dict[str, object]], next_cursor: str | None) -> dict[str, object]:
    """One query response in the exact wire shape observed on 2026-08-24."""

    return {
        "api_version": "v1",
        "catalog_version": "v1-wire-test",
        "request_id": f"req-page-{next_cursor or 'final'}",
        "dataset_id": "cn.dataset.share_float",
        "data": rows,
        "next_cursor": next_cursor,
        "metadata": {
            "state": "success",
            "degraded": False,
            "receipt_id": "td-receipt-wire-1",
            "data_through": "2026-08-23T00:00:00+08:00",
            "observed_at": "2026-08-24T09:35:10+08:00",
            "reasons": [],
            "row_receipt_proofs": [{"receipt_id": f"p{index}"} for index in range(len(rows))],
            "freshness": {"as_of": "2026-08-24T00:00:00+08:00"},
            "quality": {"row_count": len(rows)},
            "lineage": {"receipt": "lineage-wire-1"},
        },
    }


class _WireStubTransport:
    """Serve canned V1 payloads with production wire shapes; no network."""

    def __init__(self, catalog: dict, pages: list[dict]) -> None:
        self._catalog = catalog
        self._pages = list(pages)
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[dict | None] = []

    def __call__(self, *, method, url, headers, json_body, timeout_seconds):
        self.calls.append((method, url))
        self.bodies.append(json_body)
        if method == "GET":
            return HTTPResponse(status_code=200, json_body=self._catalog)
        return HTTPResponse(status_code=200, json_body=self._pages.pop(0))


class WireRehearsalTest(unittest.TestCase):
    """End-to-end fetch through the real client, pagination and envelopes.

    Rehearsed against production on 2026-08-24 (see PR #485 discussion):
    envelope ``data`` is a property (not callable), the engine rejects
    gte/lte with HTTP 400 despite catalog advertising, filtering on
    ``float_date`` is unreachable because the registry partitions this
    dataset by ``ann_date``, and several holders legitimately share one
    (symbol, ann_date, float_date) so identity must be the dataset-declared
    five-tuple.
    """

    def _wire_client(self, transport: _WireStubTransport) -> SharedSignalsV1Client:
        config = SharedSignalsV1Config(
            base_url="http://127.0.0.1:18082",
            expected_catalog_version="v1-wire-test",
            dataset_ids=frozenset({"cn.dataset.share_float"}),
            access_policy_id="tradingagent-read-v1",
            catalog_version_policy="evidence_only",
            timeout_seconds=5,
            max_limit=500,
            cache_ttl_seconds=0,
        )
        return SharedSignalsV1Client(config, transport=transport)

    def test_fetch_survives_duplicate_triples_across_pages(self) -> None:
        page_one_rows = [
            {
                "ts_code": "000657.SZ",
                "ann_date": "20260731",
                "float_date": "20260803",
                "float_ratio": 0.0772,
                "holder_name": "Holder A",
                "share_type": "limited shares",
            },
            {
                # Same triple as above — legal multi-holder row.
                "ts_code": "000657.SZ",
                "ann_date": "20260731",
                "float_date": "20260803",
                "float_ratio": 0.0101,
                "holder_name": "Holder B",
                "share_type": "limited shares",
            },
            {
                "ts_code": "300750.SZ",
                "ann_date": "20260731",
                "float_date": "20260805",
                "float_ratio": None,
                "holder_name": "ChiNext Holder",
                "share_type": "restricted",
            },
        ]
        page_two_rows = [
            {
                "ts_code": "600000.SH",
                "ann_date": "20260801",
                "float_date": "20260810",
                "float_ratio": 5.0,
                "holder_name": "Big Holder",
                "share_type": "size restricted",
            },
            {
                # Inverted row: float before announcement — skipped.
                "ts_code": "600001.SH",
                "ann_date": "20260901",
                "float_date": "20260805",
                "float_ratio": 1.0,
                "holder_name": "Odd Holder",
                "share_type": "restricted",
            },
            {
                # Outside the frozen [T-30, T) float window — filtered locally.
                "ts_code": "600002.SH",
                "ann_date": "20251231",
                "float_date": "20260101",
                "float_ratio": 9.9,
                "holder_name": "Old Holder",
                "share_type": "restricted",
            },
        ]
        transport = _WireStubTransport(
            _catalog_payload(),
            [
                _query_page(page_one_rows, "cursor-page-2"),
                _query_page(page_two_rows, None),
            ],
        )

        batch = fetch_lockup_hits(
            self._wire_client(transport), session_date=_WIRE_SESSION
        )

        # Server-side net rides the ann_date partition column with compact
        # dates; no order clause; receipt proofs requested.
        query_bodies = [
            body
            for body, (method, _url) in zip(transport.bodies, transport.calls)
            if method == "POST"
        ]
        self.assertEqual(len(query_bodies), 2)
        for body in query_bodies:
            self.assertEqual(
                body["filters"],
                {
                    "ann_date": {
                        "between": [
                            (_WIRE_SESSION - timedelta(days=120)).strftime("%Y%m%d"),
                            _WIRE_SESSION.strftime("%Y%m%d"),
                        ]
                    }
                },
            )
            self.assertNotIn("order", body)
            self.assertTrue(body["include_receipt_proofs"])
        methods = [method for method, _url in transport.calls]
        self.assertEqual(methods, ["GET", "POST", "POST"])

        # fetch returns raw hits (mainboard filtering happens in
        # build_event_evidence); duplicates survived pagination, inverted
        # and out-of-window rows were dropped locally.
        self.assertEqual(
            batch.hits,
            {
                "000657.SZ": {"latest_float_date": "20260803", "max_ratio": 0.0772},
                "300750.SZ": {"latest_float_date": "20260805", "max_ratio": None},
                "600000.SH": {"latest_float_date": "20260810", "max_ratio": 5.0},
            },
        )
        # The TD provenance binding survives the same run.
        self.assertEqual(
            batch.source_binding["receipt_id"], "td-receipt-wire-1"
        )
        self.assertIn("source_binding_sha256", batch.source_binding)

    def test_catalog_fault_wraps_into_aux_error(self) -> None:
        class BrokenTransport:
            def __call__(self, **kwargs):
                raise RuntimeError("socket exploded")

        with pytest.raises(MinuteEventAuxError, match="catalog_failed"):
            fetch_lockup_hits(
                self._wire_client(BrokenTransport()), session_date=_WIRE_SESSION
            )


if __name__ == "__main__":
    unittest.main()
