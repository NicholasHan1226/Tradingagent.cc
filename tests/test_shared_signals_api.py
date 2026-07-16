from __future__ import annotations

import json
import unittest
import urllib.parse
from datetime import datetime
from unittest.mock import patch

from shared.data.shared_signals_api import SharedSignalsAPIClient
from shared.review.forward_labels import canonicalize_evidence_record


class FakeResponse:
    def __init__(self, data=None, *, payload=None):
        self.data = [] if data is None else data
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        payload = {"data": self.data} if self.payload is None else self.payload
        return json.dumps(payload).encode("utf-8")


def _healthy_v1_metadata(dataset_id: str) -> dict[str, object]:
    return {
        "state": "ready",
        "runtime_state": "success",
        "degraded": False,
        "freshness": {"state": "fresh", "stale": False, "sla_seconds": 259200},
        "quality": {"state": "valid", "valid": True, "evidence": []},
        "lineage": {
            "state": "complete",
            "complete": True,
            "provider_neutral": True,
            "authority": "sqlite_ingest_receipts",
            "dataset_id": dataset_id,
            "providers": ["tushare"],
            "receipt_watermark": "receipt-watermark-1",
        },
        "receipt_id": "receipt-1",
        "data_through": "2026-07-16T00:00:00+08:00",
        "observed_at": "2026-07-16T15:35:00+08:00",
        "requested_as_of": None,
        "resolved_as_of": None,
        "reasons": [],
    }


def _v1_query_page(
    rows: list[dict[str, object]],
    *,
    dataset_id: str = "cn.equity.daily",
    schema_version: str = "1.0.0",
    request_id: str = "request-1",
    next_cursor: object = None,
    metadata: object | None = None,
) -> dict[str, object]:
    return {
        "api_version": "v1",
        "catalog_version": "v1-contract-1",
        "request_id": request_id,
        "dataset_id": dataset_id,
        "schema_version": schema_version,
        "data": rows,
        "next_cursor": next_cursor,
        "metadata": (
            _healthy_v1_metadata(dataset_id) if metadata is None else metadata
        ),
    }


def _canonical_json_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


class SharedSignalsAPIClientTest(unittest.TestCase):
    def test_v1_query_all_posts_canonical_pages_and_retains_full_page_evidence(
        self,
    ) -> None:
        captured_requests = []
        first_metadata = _healthy_v1_metadata("cn.equity.daily")
        first_metadata["future_metadata_extension"] = {
            "nested": [1, {"preserve": "exactly"}]
        }
        pages = [
            _v1_query_page(
                [{"symbol": "600000.SH", "close": 10.1}],
                request_id="request-page-1",
                next_cursor="signed.cursor.page-2",
                metadata=first_metadata,
            ),
            _v1_query_page(
                [{"symbol": "000001.SZ", "close": 11.2}],
                request_id="request-page-2",
            ),
        ]

        def fake_urlopen(req, timeout=0):
            captured_requests.append(req)
            return FakeResponse(payload=pages[len(captured_requests) - 1])

        SharedSignalsAPIClient._cache.clear()
        client = SharedSignalsAPIClient(
            base_url="http://sharedsignals.test",
            api_key="tenant-secret",
            max_retries=0,
        )
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            rows = client.query_v1_all(
                "cn.equity.daily",
                schema_major=1,
                total_limit=3000,
            )

        self.assertEqual([row["symbol"] for row in rows], ["600000.SH", "000001.SZ"])
        self.assertEqual(len(captured_requests), 2)
        first_body = {
            "dataset_id": "cn.equity.daily",
            "schema_major": 1,
            "limit": 500,
        }
        second_body = {
            **first_body,
            "cursor": "signed.cursor.page-2",
        }
        for request, expected_body in zip(
            captured_requests, (first_body, second_body)
        ):
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.full_url, "http://sharedsignals.test/v1/query")
            self.assertEqual(request.data, _canonical_json_bytes(expected_body))
            headers = {key.lower(): value for key, value in request.header_items()}
            self.assertEqual(headers["authorization"], "Bearer tenant-secret")
            self.assertEqual(headers["accept"], "application/json")
            self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
            self.assertEqual(headers["content-length"], str(len(request.data)))

        self.assertTrue(
            all("/tushare" not in request.full_url for request in captured_requests)
        )
        self.assertEqual(
            rows[0]["sharedsignals_v1_page_evidence"],
            {
                "api_version": "v1",
                "catalog_version": "v1-contract-1",
                "request_id": "request-page-1",
                "dataset_id": "cn.equity.daily",
                "schema_version": "1.0.0",
                "next_cursor": "signed.cursor.page-2",
                "metadata": first_metadata,
            },
        )
        self.assertEqual(
            rows[1]["sharedsignals_v1_page_evidence"]["request_id"],
            "request-page-2",
        )
        receipt = datetime.fromisoformat(
            rows[0]["sharedsignals_response_lineage"]["received_at"]
        )
        self.assertIsNotNone(receipt.tzinfo)

    def test_v1_query_all_exhausts_security_master_cursor_without_reference(self) -> None:
        captured_requests = []
        pages = [
            _v1_query_page(
                [{"symbol": "600000.SH"}],
                dataset_id="cn.equity.security_master",
                request_id="security-1",
                next_cursor="signed.security.page-2",
            ),
            _v1_query_page(
                [{"symbol": "000001.SZ"}],
                dataset_id="cn.equity.security_master",
                request_id="security-2",
            ),
        ]

        def fake_urlopen(req, timeout=0):
            captured_requests.append(req)
            return FakeResponse(payload=pages[len(captured_requests) - 1])

        client = SharedSignalsAPIClient(
            base_url="http://sharedsignals.test", max_retries=0
        )
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            rows = client.query_v1_all(
                "cn.equity.security_master",
                schema_major=1,
            )

        self.assertEqual([row["symbol"] for row in rows], ["600000.SH", "000001.SZ"])
        self.assertEqual(len(captured_requests), 2)
        bodies = [json.loads(request.data) for request in captured_requests]
        self.assertEqual(
            bodies,
            [
                {
                    "dataset_id": "cn.equity.security_master",
                    "schema_major": 1,
                    "limit": 500,
                },
                {
                    "cursor": "signed.security.page-2",
                    "dataset_id": "cn.equity.security_master",
                    "schema_major": 1,
                    "limit": 500,
                },
            ],
        )
        self.assertTrue(
            all("/reference" not in request.full_url for request in captured_requests)
        )

    def test_v1_query_total_limit_keeps_one_limit_for_the_signed_cursor_chain(
        self,
    ) -> None:
        cases = (
            (750, (500, 500), (500, 500), 750),
            (300, (200, 200), (300, 300), 300),
        )
        for total_limit, page_sizes, expected_limits, expected_rows in cases:
            with self.subTest(total_limit=total_limit):
                captured_requests = []
                pages = [
                    _v1_query_page(
                        [
                            {"symbol": f"{index:06d}.SZ"}
                            for index in range(page_sizes[0])
                        ],
                        request_id="fixed-limit-page-1",
                        next_cursor="signed.fixed-limit.page-2",
                    ),
                    _v1_query_page(
                        [
                            {"symbol": f"{index + page_sizes[0]:06d}.SZ"}
                            for index in range(page_sizes[1])
                        ],
                        request_id="fixed-limit-page-2",
                    ),
                ]

                def fake_urlopen(req, timeout=0):
                    captured_requests.append(req)
                    return FakeResponse(payload=pages.pop(0))

                client = SharedSignalsAPIClient(
                    base_url="http://sharedsignals.test", max_retries=0
                )
                with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    rows = client.query_v1_all(
                        "cn.equity.daily",
                        total_limit=total_limit,
                    )

                bodies = [json.loads(request.data) for request in captured_requests]
                self.assertEqual(
                    [body["limit"] for body in bodies],
                    list(expected_limits),
                )
                self.assertNotIn("cursor", bodies[0])
                self.assertEqual(
                    bodies[1]["cursor"], "signed.fixed-limit.page-2"
                )
                self.assertEqual(len(rows), expected_rows)
                self.assertEqual(client.errors, [])

    def test_v1_query_all_rejects_unhealthy_runtime_or_degraded_metadata(self) -> None:
        cases: list[tuple[str, dict[str, object]]] = []
        for state in ("empty", "unobserved", "paused", "failed", "stale"):
            metadata = _healthy_v1_metadata("cn.equity.daily")
            metadata["state"] = state
            metadata["runtime_state"] = state
            metadata["degraded"] = state not in {"success", "empty"}
            cases.append((state, metadata))
        degraded = _healthy_v1_metadata("cn.equity.daily")
        degraded["degraded"] = True
        cases.append(("degraded", degraded))

        for name, metadata in cases:
            with self.subTest(name=name):
                client = SharedSignalsAPIClient(
                    base_url="http://sharedsignals.test", max_retries=0
                )
                page = _v1_query_page(
                    [{"symbol": "600000.SH"}], metadata=metadata
                )
                with patch(
                    "urllib.request.urlopen",
                    return_value=FakeResponse(payload=page),
                ):
                    rows = client.query_v1_all("cn.equity.daily")
                self.assertEqual(rows, [])
                self.assertTrue(client.errors)
                self.assertIn("metadata", client.errors[-1])

    def test_v1_query_all_rejects_missing_or_malformed_metadata(self) -> None:
        missing_receipt = _healthy_v1_metadata("cn.equity.daily")
        del missing_receipt["receipt_id"]
        empty_freshness = _healthy_v1_metadata("cn.equity.daily")
        empty_freshness["freshness"] = {}
        cases = {
            "missing_metadata": None,
            "non_object_metadata": ["invalid"],
            "missing_receipt": missing_receipt,
            "empty_freshness": empty_freshness,
        }

        for name, metadata in cases.items():
            with self.subTest(name=name):
                page = _v1_query_page(
                    [{"symbol": "600000.SH"}],
                    metadata=metadata if metadata is not None else {},
                )
                if name == "missing_metadata":
                    del page["metadata"]
                client = SharedSignalsAPIClient(
                    base_url="http://sharedsignals.test", max_retries=0
                )
                with patch(
                    "urllib.request.urlopen",
                    return_value=FakeResponse(payload=page),
                ):
                    rows = client.query_v1_all("cn.equity.daily")
                self.assertEqual(rows, [])
                self.assertTrue(client.errors)
                self.assertIn("metadata", client.errors[-1])

    def test_v1_query_all_rejects_incomplete_page_and_lineage_envelopes(self) -> None:
        missing_cursor = _v1_query_page([{"symbol": "600000.SH"}])
        del missing_cursor["next_cursor"]
        missing_watermark = _v1_query_page([{"symbol": "600000.SH"}])
        del missing_watermark["metadata"]["lineage"]["receipt_watermark"]
        empty_providers = _v1_query_page([{"symbol": "600000.SH"}])
        empty_providers["metadata"]["lineage"]["providers"] = []

        for name, page in {
            "missing_next_cursor": missing_cursor,
            "missing_receipt_watermark": missing_watermark,
            "empty_providers": empty_providers,
        }.items():
            with self.subTest(name=name):
                client = SharedSignalsAPIClient(
                    base_url="http://sharedsignals.test", max_retries=0
                )
                with patch(
                    "urllib.request.urlopen",
                    return_value=FakeResponse(payload=page),
                ):
                    rows = client.query_v1_all("cn.equity.daily")
                self.assertEqual(rows, [])
                self.assertTrue(client.errors)

    def test_v1_query_all_rejects_partial_pagination_failures(self) -> None:
        cases = {
            "empty page with cursor": [
                _v1_query_page([], next_cursor="signed.more")
            ],
            "repeated cursor": [
                _v1_query_page(
                    [{"symbol": "600000.SH"}], next_cursor="signed.repeat"
                ),
                _v1_query_page(
                    [{"symbol": "000001.SZ"}], next_cursor="signed.repeat"
                ),
            ],
            "dataset drift": [
                _v1_query_page(
                    [{"symbol": "600000.SH"}], next_cursor="signed.next"
                ),
                _v1_query_page(
                    [{"symbol": "000001.SZ"}],
                    dataset_id="cn.equity.security_master",
                ),
            ],
            "schema drift": [
                _v1_query_page(
                    [{"symbol": "600000.SH"}], next_cursor="signed.next"
                ),
                _v1_query_page(
                    [{"symbol": "000001.SZ"}], schema_version="1.1.0"
                ),
            ],
            "metadata": [
                _v1_query_page(
                    [{"symbol": "600000.SH"}], next_cursor="signed.next"
                ),
                _v1_query_page(
                    [{"symbol": "000001.SZ"}],
                    metadata={
                        **_healthy_v1_metadata("cn.equity.daily"),
                        "degraded": True,
                    },
                ),
            ],
            "malformed cursor": [
                _v1_query_page(
                    [{"symbol": "600000.SH"}], next_cursor=123
                )
            ],
        }

        for expected_error, case_pages in cases.items():
            with self.subTest(expected_error=expected_error):
                pages = list(case_pages)
                client = SharedSignalsAPIClient(
                    base_url="http://sharedsignals.test", max_retries=0
                )
                with patch(
                    "urllib.request.urlopen",
                    side_effect=lambda req, timeout=0: FakeResponse(
                        payload=pages.pop(0)
                    ),
                ):
                    rows = client.query_v1_all("cn.equity.daily")
                self.assertEqual(rows, [])
                self.assertTrue(client.errors)
                self.assertIn(expected_error, client.errors[-1])

    def test_v1_query_all_enforces_bounded_page_cap(self) -> None:
        pages = [
            _v1_query_page(
                [{"symbol": "600000.SH"}], next_cursor="signed.page-2"
            ),
            _v1_query_page(
                [{"symbol": "000001.SZ"}],
                request_id="request-2",
                next_cursor="signed.page-3",
            ),
        ]
        client = SharedSignalsAPIClient(
            base_url="http://sharedsignals.test", max_retries=0
        )
        client._v1_max_pages = 2
        with patch(
            "urllib.request.urlopen",
            side_effect=lambda req, timeout=0: FakeResponse(payload=pages.pop(0)),
        ):
            rows = client.query_v1_all("cn.equity.daily")

        self.assertEqual(rows, [])
        self.assertTrue(client.errors)
        self.assertIn("page cap", client.errors[-1])

    def test_v1_query_bypasses_legacy_get_cache_and_isolates_credentials(self) -> None:
        captured_requests = []

        def fake_urlopen(req, timeout=0):
            captured_requests.append(req)
            return FakeResponse(
                payload=_v1_query_page([{"symbol": "600000.SH"}])
            )

        SharedSignalsAPIClient._cache.clear()
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            for api_key in ("tenant-a", "tenant-b"):
                client = SharedSignalsAPIClient(
                    base_url="http://sharedsignals.test",
                    api_key=api_key,
                    max_retries=0,
                )
                self.assertEqual(
                    len(client.query_v1_all("cn.equity.daily", total_limit=1)),
                    1,
                )

        self.assertEqual(len(captured_requests), 2)
        self.assertEqual(
            [
                {key.lower(): value for key, value in request.header_items()}[
                    "authorization"
                ]
                for request in captured_requests
            ],
            ["Bearer tenant-a", "Bearer tenant-b"],
        )

    def test_realtime_5min_sends_symbol_and_trade_date_aliases(self) -> None:
        captured_urls: list[str] = []

        def fake_urlopen(req, timeout=0):
            captured_urls.append(req.full_url)
            return FakeResponse()

        SharedSignalsAPIClient._cache.clear()
        client = SharedSignalsAPIClient(
            base_url="http://sharedsignals.test", max_retries=0
        )
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            client.get_realtime_5min("300759.SZ", date="20260709", market="ashare")

        parsed = urllib.parse.urlparse(captured_urls[0])
        params = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(params["ts_code"], ["300759.SZ"])
        self.assertEqual(params["symbol"], ["300759.SZ"])
        self.assertEqual(params["date"], ["20260709"])
        self.assertEqual(params["trade_date"], ["20260709"])
        self.assertEqual(params["market"], ["ashare"])

    def test_http_response_receipt_is_persisted_before_cache(self) -> None:
        calls = 0

        def fake_urlopen(req, timeout=0):
            nonlocal calls
            calls += 1
            return FakeResponse(
                [
                    {
                        "bar_time": "2026-07-13T10:00:00+08:00",
                        "close": 10.0,
                    }
                ]
            )

        SharedSignalsAPIClient._cache.clear()
        client = SharedSignalsAPIClient(
            base_url="http://sharedsignals.test", max_retries=0
        )
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            first = client.get_realtime_5min(
                "300759.SZ", date="20260713", market="ashare"
            )
            repeated = client.get_realtime_5min(
                "300759.SZ", date="20260713", market="ashare"
            )

        self.assertEqual(calls, 1)
        receipt = first[0]["sharedsignals_response_lineage"]["received_at"]
        parsed = datetime.fromisoformat(receipt)
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(
            first[0]["evidence_envelope"]["retrieval_time_fields"][
                "sharedsignals_http_response.received_at"
            ],
            receipt,
        )
        self.assertEqual(repeated, first)

    def _assert_invalid_provider_envelope_keeps_transport_receipt(
        self, provider_envelope: object
    ) -> None:
        calls = 0

        def fake_urlopen(req, timeout=0):
            nonlocal calls
            calls += 1
            return FakeResponse(
                [
                    {
                        "bar_time": "2026-07-13T10:00:00+08:00",
                        "available_at": "2026-07-13T10:00:01+08:00",
                        "ingested_at": "2026-07-13T10:00:02+08:00",
                        "retrieved_as_of": "2026-07-13T10:00:03+08:00",
                        "close": 10.0,
                        "evidence_envelope": provider_envelope,
                    }
                ]
            )

        SharedSignalsAPIClient._cache.clear()
        client = SharedSignalsAPIClient(
            base_url="http://sharedsignals.test", max_retries=0
        )
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            first = client.get_realtime_5min(
                "300759.SZ", date="20260713", market="ashare"
            )
            repeated = client.get_realtime_5min(
                "300759.SZ", date="20260713", market="ashare"
            )

        self.assertEqual(calls, 1)
        self.assertEqual(first[0]["evidence_envelope"], provider_envelope)
        lineage = first[0]["sharedsignals_response_lineage"]
        self.assertEqual(lineage["transport"], "http_response")
        self.assertEqual(lineage["endpoint"], "/realtime_5min")
        receipt = datetime.fromisoformat(lineage["received_at"])
        self.assertIsNotNone(receipt.tzinfo)
        self.assertEqual(repeated, first)
        validation = canonicalize_evidence_record(first[0])[
            "evidence_envelope_validation"
        ]
        self.assertEqual(validation["status"], "invalid_envelope_structure")
        self.assertIs(validation["complete"], False)

    def test_invalid_provider_envelope_keeps_transport_receipt_and_cache(self) -> None:
        self._assert_invalid_provider_envelope_keeps_transport_receipt(
            "provider-invalid-envelope"
        )

    def test_invalid_provider_retrieval_group_keeps_transport_receipt_and_cache(
        self,
    ) -> None:
        self._assert_invalid_provider_envelope_keeps_transport_receipt(
            {"retrieval_time_fields": "provider-invalid-retrieval-group"}
        )


if __name__ == "__main__":
    unittest.main()
