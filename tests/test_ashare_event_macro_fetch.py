"""Offline tests for the macro release-indicator history fetcher."""

from __future__ import annotations

import csv
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from Ashare import event_macro_fetch as fetch

GDP_FIELDS = ["quarter", "gdp", "gdp_yoy", "pi", "pi_yoy"]
CPI_FIELDS = ["month", "nt_val", "nt_yoy"]


def _read_rows(path: Path) -> list[list]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.reader(handle))


class PlaceholderTest(unittest.TestCase):
    def test_all_value_fields_empty_is_placeholder(self) -> None:
        self.assertTrue(
            fetch._is_placeholder(GDP_FIELDS, ["2026Q3", None, None, None, None])
        )
        self.assertTrue(
            fetch._is_placeholder(CPI_FIELDS, ["202608", "", "", ""])
        )

    def test_any_value_present_is_not_placeholder(self) -> None:
        # Partial-null rows pass through: consumers pick columns themselves.
        self.assertFalse(fetch._is_placeholder(CPI_FIELDS, ["202607", 100.5, None]))
        self.assertFalse(
            fetch._is_placeholder(GDP_FIELDS,
                                  ["2026Q2", 695704.0, 4.7, 31521.8, 3.7])
        )


class FetchSweepTest(unittest.TestCase):
    def _responses(self) -> dict[str, tuple[list[str], list[list]]]:
        return {
            "cn_gdp": (
                GDP_FIELDS,
                [
                    ["2026Q2", 695704.0, 4.7, 31521.8, 3.7],
                    ["2026Q3", None, None, None, None],  # placeholder stays RAW
                ],
            ),
            "cn_cpi": (CPI_FIELDS, [["202607", 100.5, 0.5]]),
            "cn_ppi": (CPI_FIELDS, []),
            "cn_m": (CPI_FIELDS, [["202607", 100.5, 0.5]]),
        }

    def _fetch(self, cache: Path) -> dict[str, object]:
        responses = self._responses()

        def fake_call(api: str, params: dict):
            self.assertEqual(params, {})  # contract: full-history, no params
            return responses[api]

        with unittest.mock.patch(
            "Ashare.event_calendar_fetch.call_api", fake_call
        ):
            return fetch.fetch_macro(cache, delay_seconds=0.0)

    def test_writes_raw_files_and_counts_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            summary = self._fetch(cache)
            self.assertEqual(summary["fetched"], 3)
            self.assertEqual(summary["failed_endpoints"],
                             ["ppi:empty_response"])
            self.assertEqual(summary["placeholder_rows"],
                             {"gdp": 1, "cpi": 0, "money": 0})
            target = cache / "macro_gdp.csv"
            rows = _read_rows(target)
            self.assertEqual(len(rows), 3)  # header + 2 raw rows incl. placeholder
            self.assertEqual(rows[2][0], "2026Q3")

    def test_idempotent_skip_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._fetch(cache)
            summary = self._fetch(cache)  # rerun skips everything written
            self.assertEqual(summary["skipped_existing"], 3)
            self.assertEqual(summary["fetched"], 0)
            # the empty endpoint is re-attempted and fails again
            self.assertEqual(summary["failed_endpoints"],
                             ["ppi:empty_response"])

    def test_refresh_republishes_existing_files(self) -> None:
        # Macro series keep being published after the first sweep; a
        # skip-if-present-only pass would freeze the release calendar at
        # the week the cache was first built.
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            first = self._fetch(cache)
            self.assertEqual(first["fetched"], 3)

            responses = self._responses()
            responses["cn_cpi"] = (
                CPI_FIELDS,
                [["202607", 100.5, 0.5], ["202608", 100.4, 0.4]],
            )
            calls: list[str] = []

            def counting_call(api: str, params: dict):
                calls.append(api)
                return responses[api]

            with unittest.mock.patch(
                "Ashare.event_calendar_fetch.call_api", counting_call
            ):
                summary = fetch.fetch_macro(
                    cache, delay_seconds=0.0, refresh=True
                )
            self.assertEqual(summary["skipped_existing"], 0)
            self.assertEqual(summary["fetched"], 3)
            rows = _read_rows(cache / "macro_cpi.csv")
            self.assertEqual(rows[-1][0], "202608")  # new month landed

    def test_endpoint_exception_recorded_without_aborting(self) -> None:
        def flaky_call(api: str, params: dict):
            if api == "cn_cpi":
                raise TimeoutError("transport down")
            return CPI_FIELDS, [["202607", 100.5, 0.5]]

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            with unittest.mock.patch(
                "Ashare.event_calendar_fetch.call_api", flaky_call
            ):
                summary = fetch.fetch_macro(cache, delay_seconds=0.0)
            self.assertIn("cpi:TimeoutError", summary["failed_endpoints"])
            self.assertTrue((cache / "macro_gdp.csv").exists())

    def test_missing_cache_dir_surfaces_disk_error(self) -> None:
        # Same contract as the family fetchers: unconditional disk_usage.
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope"
            with self.assertRaises(OSError):
                self._fetch(missing)


if __name__ == "__main__":
    unittest.main()
