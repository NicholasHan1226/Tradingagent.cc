"""Offline tests for the pre-lockup price-to-book (panel #19) study."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from Ashare.event_pb_prelockup_study import (
    _double_low,
    attach_pb_bucket,
    load_pb_book,
    pb_label,
)


def _book(values: list[float | None], start_day: int = 20200101) -> tuple[
        list[str], list[float | None]]:
    """Ascending synthetic book; values map 1:1 onto consecutive sessions."""
    days = [str(start_day + i) for i in range(len(values))]
    return days, values


class PbLabelTest(unittest.TestCase):
    def test_low_edge_exclusive(self) -> None:
        days, values = _book([1.49])
        self.assertEqual(pb_label(days, values, "20200102"), "low")

    def test_normal_band_includes_low_boundary(self) -> None:
        # 1.50 <= pb < 4.00 -> normal; the low edge itself lands in normal.
        days, values = _book([1.50])
        self.assertEqual(pb_label(days, values, "20200102"), "normal")

    def test_high_edge_inclusive(self) -> None:
        days, values = _book([4.00])
        self.assertEqual(pb_label(days, values, "20200102"), "high")

    def test_just_below_high_edge_is_normal(self) -> None:
        days, values = _book([3.99])
        self.assertEqual(pb_label(days, values, "20200102"), "normal")

    def test_entry_day_row_excluded(self) -> None:
        # The row dated entry_day itself must never be read as current:
        # the strictly-prior first row (2.5 -> normal) wins over the
        # entry-day row's 999.0.
        days, values = _book([2.5, 999.0], start_day=20200101)
        self.assertEqual(pb_label(days, values, str(days[1])), "normal")

    def test_entry_on_first_session_is_no_data(self) -> None:
        days, values = _book([2.5], start_day=20200101)
        self.assertEqual(pb_label(days, values, str(days[0])), "no_data")

    def test_no_prior_row_is_no_data(self) -> None:
        days, values = _book([2.0, 3.0], start_day=20210101)
        self.assertEqual(pb_label(days, values, "20200101"), "no_data")

    def test_null_current_is_no_data(self) -> None:
        days, values = _book([None])
        self.assertEqual(pb_label(days, values, "20200102"), "no_data")


class LoadPbBookTest(unittest.TestCase):
    def test_descending_shard_defensively_resorted(self) -> None:
        cache = Path(tempfile.mkdtemp())
        rows = ["ts_code,trade_date,pb\n"]
        for day, pb in (("20200103", "3.0"), ("20200102", "2.0"),
                        ("20200101", "1.0")):  # newest-first on disk
            rows.append(f"000001.SZ,{day},{pb}\n")
        (cache / "dailybasic_000001SZ.csv").write_text("".join(rows),
                                                       encoding="utf-8")
        book = load_pb_book(cache, "000001.SZ")
        assert book is not None
        days, values = book
        self.assertEqual(days, ["20200101", "20200102", "20200103"])
        self.assertEqual(values, [1.0, 2.0, 3.0])

    def test_unparseable_value_preserved_as_none(self) -> None:
        cache = Path(tempfile.mkdtemp())
        (cache / "dailybasic_000001SZ.csv").write_text(
            "ts_code,trade_date,pb\n"
            "000001.SZ,20200101,\n"
            "000001.SZ,20200102,2.04\n",
            encoding="utf-8")
        book = load_pb_book(cache, "000001.SZ")
        assert book is not None
        self.assertEqual(book[1], [None, 2.04])

    def test_missing_shard_returns_none_not_raise(self) -> None:
        # Panel #19 prereg D3: a missing dailybasic shard is one of the
        # three unified ``no_data`` reference cases, not a structural
        # failure (diverges from panel #17's fail-closed shard load).
        self.assertIsNone(
            load_pb_book(Path(tempfile.mkdtemp()), "000002.SZ"))


class AttachPbBucketTest(unittest.TestCase):
    def _write_shard(self, cache: Path, code: str,
                     days: list[str],
                     values: list[float | None]) -> None:
        lines = ["ts_code,trade_date,pb\n"]
        for day, pb in reversed(list(zip(days, values))):
            value = "" if pb is None else f"{pb}"
            lines.append(f"{code.replace('.', '')},{day},{value}\n")
        (cache / f"dailybasic_{code.replace('.', '')}.csv").write_text(
            "".join(lines), encoding="utf-8")

    def test_missing_shard_lands_in_no_data_reference(self) -> None:
        cache = Path(tempfile.mkdtemp())
        days, values = _book([6.0, 1.2])
        self._write_shard(cache, "000001.SZ", days, values)
        signals = [
            {"ts_code": "000001.SZ", "entry_day": "20200103"},
            {"ts_code": "000002.SZ", "entry_day": "20200103"},  # no shard
        ]
        stats = attach_pb_bucket(signals, cache)  # type: ignore[arg-type]
        self.assertEqual(signals[0]["pb_bucket"], "low")
        self.assertEqual(signals[1]["pb_bucket"], "no_data")
        self.assertEqual(stats["attached"], 2)
        self.assertEqual(stats["no_data"], 1)

    def test_second_signal_reuses_cached_book(self) -> None:
        cache = Path(tempfile.mkdtemp())
        flat = [2.5] * 10
        days = [str(20200101 + i) for i in range(len(flat))]
        self._write_shard(cache, "000001.SZ", days, flat)
        signals = [
            {"ts_code": "000001.SZ", "entry_day": "20200105"},
            {"ts_code": "000001.SZ", "entry_day": "20200106"},
        ]
        stats = attach_pb_bucket(signals, cache)  # type: ignore[arg-type]
        self.assertEqual(signals[0]["pb_bucket"], "normal")
        self.assertEqual(signals[1]["pb_bucket"], "normal")
        self.assertEqual(stats["attached"], 2)
        self.assertEqual(stats["normal"], 2)


class DoubleLowGateTest(unittest.TestCase):
    def test_gate_legs_and_threshold(self) -> None:
        baseline = {"n": 300, "mean_net_bps": 100.0, "win_rate": 0.55}
        passing = {"n": 30, "mean_net_bps": 99.9, "win_rate": 0.549}
        thin_n = {"n": 29, "mean_net_bps": 99.9, "win_rate": 0.549}
        mean_high = {"n": 30, "mean_net_bps": 150.0, "win_rate": 0.500}
        win_high = {"n": 30, "mean_net_bps": 90.0, "win_rate": 0.560}
        self.assertTrue(_double_low(passing, baseline))
        self.assertFalse(_double_low(thin_n, baseline))
        self.assertFalse(_double_low(mean_high, baseline))
        self.assertFalse(_double_low(win_high, baseline))


if __name__ == "__main__":
    unittest.main()
