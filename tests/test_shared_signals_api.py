from __future__ import annotations

import json
import unittest
import urllib.parse
from datetime import datetime
from unittest.mock import patch

from shared.data.shared_signals_api import SharedSignalsAPIClient
from shared.review.forward_labels import canonicalize_evidence_record


class FakeResponse:
    def __init__(self, data=None):
        self.data = [] if data is None else data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return json.dumps({"data": self.data}).encode("utf-8")


class SharedSignalsAPIClientTest(unittest.TestCase):
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
