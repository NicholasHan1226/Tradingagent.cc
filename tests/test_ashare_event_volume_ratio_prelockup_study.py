"""Offline tests for the pre-lockup volume-ratio (panel #18) study."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from Ashare.event_volume_ratio_prelockup_study import (
    _double_low,
    attach_volume_ratio_bucket,
    load_volume_ratio_book,
    volume_ratio_label,
)


def _book(values: list[float | None], start_day: int = 20200101) -> tuple[
        list[str], list[float | None]]:
    """Ascending synthetic book; values map 1:1 onto consecutive sessions."""
    days = [str(start_day + i) for i in range(len(values))]
    return days, values


class VolumeRatioLabelTest(unittest.TestCase):
    def test_low_edge_exclusive(self) -> None:
        days, ratios = _book([0.69])
        self.assertEqual(volume_ratio_label(days, ratios, "20200102"),
                         "low")

    def test_normal_band_includes_low_boundary(self) -> None:
        # 0.70 <= v < 1.20 -> normal; the low edge itself lands in normal.
        days, ratios = _book([0.70])
        self.assertEqual(volume_ratio_label(days, ratios, "20200102"),
                         "normal")

    def test_high_edge_inclusive(self) -> None:
        days, ratios = _book([1.20])
        self.assertEqual(volume_ratio_label(days, ratios, "20200102"),
                         "high")

    def test_just_below_high_edge_is_normal(self) -> None:
        days, ratios = _book([1.19])
        self.assertEqual(volume_ratio_label(days, ratios, "20200102"),
                         "normal")

    def test_entry_day_row_excluded(self) -> None:
        # The row dated entry_day itself must never be read as current:
        # the strictly-prior first row (10.0 -> high) wins over the
        # entry-day row's 999.0.
        days, ratios = _book([10.0, 999.0], start_day=20200101)
        self.assertEqual(
            volume_ratio_label(days, ratios, str(days[1])), "high")

    def test_entry_on_first_session_is_no_data(self) -> None:
        # Entry dated exactly on the shard's first session: zero
        # strictly-prior rows -> no_data reference bucket.
        days, ratios = _book([10.0], start_day=20200101)
        self.assertEqual(
            volume_ratio_label(days, ratios, str(days[0])), "no_data")

    def test_no_prior_row_is_no_data(self) -> None:
        days, ratios = _book([0.9, 1.0], start_day=20210101)
        self.assertEqual(
            volume_ratio_label(days, ratios, "20200101"), "no_data")

    def test_null_current_is_no_data(self) -> None:
        days, ratios = _book([None])
        self.assertEqual(volume_ratio_label(days, ratios, "20200102"),
                         "no_data")


class LoadVolumeRatioBookTest(unittest.TestCase):
    def test_descending_shard_defensively_resorted(self) -> None:
        cache = Path(tempfile.mkdtemp())
        rows = ["ts_code,trade_date,volume_ratio\n"]
        for day, ratio in (("20200103", "3.0"), ("20200102", "2.0"),
                           ("20200101", "1.0")):  # newest-first on disk
            rows.append(f"000001.SZ,{day},{ratio}\n")
        (cache / "dailybasic_000001SZ.csv").write_text("".join(rows),
                                                       encoding="utf-8")
        book = load_volume_ratio_book(cache, "000001.SZ")
        assert book is not None
        days, ratios = book
        self.assertEqual(days, ["20200101", "20200102", "20200103"])
        self.assertEqual(ratios, [1.0, 2.0, 3.0])

    def test_unparseable_value_preserved_as_none(self) -> None:
        cache = Path(tempfile.mkdtemp())
        (cache / "dailybasic_000001SZ.csv").write_text(
            "ts_code,trade_date,volume_ratio\n"
            "000001.SZ,20200101,\n"
            "000001.SZ,20200102,1.5\n",
            encoding="utf-8")
        book = load_volume_ratio_book(cache, "000001.SZ")
        assert book is not None
        self.assertEqual(book[1], [None, 1.5])

    def test_missing_shard_returns_none_not_raise(self) -> None:
        # Panel #18 prereg D3 diverges from panel #17 here: a missing
        # dailybasic shard is one of the three unified ``no_data``
        # reference cases, not a structural failure.
        self.assertIsNone(
            load_volume_ratio_book(Path(tempfile.mkdtemp()), "000002.SZ"))


class AttachVolumeRatioBucketTest(unittest.TestCase):
    def _write_shard(self, cache: Path, code: str,
                     days: list[str],
                     ratios: list[float | None]) -> None:
        lines = ["ts_code,trade_date,volume_ratio\n"]
        for day, ratio in reversed(list(zip(days, ratios))):
            value = "" if ratio is None else f"{ratio}"
            lines.append(f"{code.replace('.', '')},{day},{value}\n")
        (cache / f"dailybasic_{code.replace('.', '')}.csv").write_text(
            "".join(lines), encoding="utf-8")

    def test_missing_shard_lands_in_no_data_reference(self) -> None:
        # Prereg D3: three cases unify into no_data reference rows that
        # are counted separately and never enter the primary contrast.
        cache = Path(tempfile.mkdtemp())
        days, ratios = _book([0.5, 5.0])
        self._write_shard(cache, "000001.SZ", days, ratios)
        signals = [
            {"ts_code": "000001.SZ", "entry_day": "20200103"},
            {"ts_code": "000002.SZ", "entry_day": "20200103"},  # no shard
        ]
        stats = attach_volume_ratio_bucket(signals, cache)  # type: ignore[arg-type]
        self.assertEqual(signals[0]["volume_ratio_bucket"], "high")
        self.assertEqual(signals[1]["volume_ratio_bucket"], "no_data")
        self.assertEqual(stats["attached"], 2)
        self.assertEqual(stats["no_data"], 1)

    def test_second_signal_reuses_cached_book(self) -> None:
        cache = Path(tempfile.mkdtemp())
        flat = [0.9] * 10
        days = [str(20200101 + i) for i in range(len(flat))]
        self._write_shard(cache, "000001.SZ", days, flat)
        signals = [
            {"ts_code": "000001.SZ", "entry_day": "20200105"},
            {"ts_code": "000001.SZ", "entry_day": "20200106"},
        ]
        stats = attach_volume_ratio_bucket(signals, cache)  # type: ignore[arg-type]
        self.assertEqual(signals[0]["volume_ratio_bucket"], "normal")
        self.assertEqual(signals[1]["volume_ratio_bucket"], "normal")
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
