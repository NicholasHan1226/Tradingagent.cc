from __future__ import annotations

import json
import unittest
import urllib.parse
from unittest.mock import patch

from shared.data.shared_signals_api import SharedSignalsAPIClient


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return json.dumps({"data": []}).encode("utf-8")


class SharedSignalsAPIClientTest(unittest.TestCase):
    def test_realtime_5min_sends_symbol_and_trade_date_aliases(self) -> None:
        captured_urls: list[str] = []

        def fake_urlopen(req, timeout=0):
            captured_urls.append(req.full_url)
            return FakeResponse()

        SharedSignalsAPIClient._cache.clear()
        client = SharedSignalsAPIClient(base_url="http://sharedsignals.test", max_retries=0)
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            client.get_realtime_5min("300759.SZ", date="20260709", market="ashare")

        parsed = urllib.parse.urlparse(captured_urls[0])
        params = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(params["ts_code"], ["300759.SZ"])
        self.assertEqual(params["symbol"], ["300759.SZ"])
        self.assertEqual(params["date"], ["20260709"])
        self.assertEqual(params["trade_date"], ["20260709"])
        self.assertEqual(params["market"], ["ashare"])


if __name__ == "__main__":
    unittest.main()
