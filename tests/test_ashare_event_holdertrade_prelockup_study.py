"""Offline tests for the holdertrade pre-lockup conditioning study."""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from Ashare import event_holdertrade_prelockup_study as study


def _sessions(count: int, start: date = date(2026, 1, 5)) -> list[str]:
    out: list[str] = []
    cursor = start
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return out


DAYS = _sessions(40)
ENTRY = DAYS[39]
CODE = "600000.SH"


def _idx(days: list[str], vols: list[float], code: str = CODE):
    return {code: (list(days), list(vols))}


def _signal(entry_day: str = ENTRY, code: str = CODE):
    return {"ts_code": code, "entry_day": entry_day}


class BucketClassificationTest(unittest.TestCase):
    def test_direction_buckets(self) -> None:
        # entry = 20260227; frozen window = ["20260128", "20260227")
        inside = "20260210"
        cases = [
            (_idx([inside], [5000.0]), "net_buy"),
            (_idx([inside], [-5000.0]), "net_sell"),
            (_idx([inside, inside], [3000.0, -3000.0]), "flat"),
        ]
        for book, want in cases:
            signals = [_signal()]
            stats = study.attach_holdertrade_states(signals, book)
            self.assertEqual(signals[0]["holder_bucket"], want, want)
        # no index entry at all -> no_records
        signals = [_signal()]
        study.attach_holdertrade_states(signals, {})
        self.assertEqual(signals[0]["holder_bucket"], "no_records")
        self.assertIsNone(signals[0]["holder_net_vol"])

    def test_empty_window_is_no_records(self) -> None:
        # sole record sits far outside the frozen 30-natural-day window
        far = "20251201"
        signals = [_signal()]
        stats = study.attach_holdertrade_states(
            signals, _idx([far], [9000.0])
        )
        self.assertEqual(signals[0]["holder_bucket"], "no_records")
        self.assertEqual(stats["no_records"], 1)

    def test_window_edges_are_inclusive_start_exclusive_entry(self) -> None:
        # ann_date == entry - 30 natural days stays INSIDE (frozen D1);
        # one day earlier falls OUTSIDE; ann_date == entry_day is
        # strictly excluded (frozen D1).
        self.assertEqual(ENTRY, "20260227")
        for ann, want in (("20260128", "net_buy"),
                          ("20260127", "no_records"),
                          ("20260227", "no_records")):
            signals = [_signal()]
            study.attach_holdertrade_states(
                signals, _idx([ann], [1000.0])
            )
            self.assertEqual(signals[0]["holder_bucket"], want, ann)

    def test_mixed_holders_aggregate_signed(self) -> None:
        inside = "20260210"
        # two buys and one bigger sell -> net negative
        book = _idx([inside, inside, inside],
                    [2000.0, 1500.0, -6000.0])
        signals = [_signal()]
        study.attach_holdertrade_states(signals, book)
        self.assertEqual(signals[0]["holder_bucket"], "net_sell")
        self.assertAlmostEqual(float(signals[0]["holder_net_vol"]), -2500.0)


class LoaderTest(unittest.TestCase):
    def _write_file(self, cache: Path, name: str,
                    rows: list[list[object]]) -> None:
        folder = cache / "holdertrade_daily"
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / f"{name}.csv").open("w", newline="",
                                           encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ts_code", "ann_date", "in_de", "change_vol"])
            for row in rows:
                writer.writerow(row)

    def test_missing_folder_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                study.HoldertradeStudyError, "holdertrade_cache_missing"
            ):
                study.load_holdertrade_index(Path(tmp))

    def test_reads_files_skips_malformed_keeps_ascending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._write_file(cache, "20260102",
                             [[CODE, "20260102", "IN", 1200.0]])
            self._write_file(cache, "20260105",
                             [[CODE, "20260105", "DE", 300.0]])
            # malformed rows: bad volume, unknown direction, missing code
            self._write_file(cache, "20260107",
                             [[CODE, "20260107", "IN", "bad"],
                              ["", "20260107", "IN", 50.0],
                              [CODE, "", "X", 50.0]])
            index = study.load_holdertrade_index(cache)
            days, vols = index[CODE]
            self.assertEqual(days, ["20260102", "20260105"])
            self.assertAlmostEqual(vols[0], 1200.0)
            self.assertAlmostEqual(vols[1], -300.0)

    def test_empty_cache_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            folder = cache / "holdertrade_daily"
            folder.mkdir(parents=True)
            (folder / "20260102.csv").write_text("", encoding="utf-8")
            with self.assertRaisesRegex(
                study.HoldertradeStudyError, "holdertrade_cache_empty"
            ):
                study.load_holdertrade_index(cache)


if __name__ == "__main__":
    unittest.main()
