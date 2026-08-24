"""Offline tests for the pre-lockup valuation-percentile (panel #17) study."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from Ashare.event_valuation_prelockup_study import (
    PCT_MIN_HIST,
    ValuationStudyError,
    _double_low,
    attach_valuation_bucket,
    load_pe_book,
    valuation_label,
)


def _book(values: list[float | None], start_day: int = 20200101) -> tuple[
        list[str], list[float | None]]:
    """Ascending synthetic book; values map 1:1 onto consecutive sessions."""
    days = [str(start_day + i) for i in range(len(values))]
    return days, values


def _hist_book(cur: float | None) -> tuple[list[str], list[float | None]]:
    """250-row history window holding exactly 200 valid positive pe_ttm
    (25 None + 25 zero + range(1,201)), current row last; entry day sits
    strictly after the current row."""
    hist: list[float | None] = [None] * 25 + [0.0] * 25 + [
        float(v) for v in range(1, PCT_MIN_HIST + 1)
    ]
    assert len(hist) == 250
    return _book(hist + [cur], start_day=20200001)


class ValuationLabelTest(unittest.TestCase):
    def test_low_boundary_inclusive(self) -> None:
        # cur=50 -> 50/200 = 0.25 exactly (binary-representable) -> low_le25.
        days, pes = _hist_book(50.0)
        self.assertEqual(valuation_label(days, pes, "20210101"), "low_le25")

    def test_just_above_low_edge_is_mid(self) -> None:
        # cur=51 -> 51/200 = 0.255 > 0.25 -> mid (50.1 would still count 50).
        days, pes = _hist_book(51.0)
        self.assertEqual(valuation_label(days, pes, "20210101"), "mid")

    def test_high_boundary_inclusive(self) -> None:
        # cur=150 -> 150/200 = 0.75 exactly -> high_ge75.
        days, pes = _hist_book(150.0)
        self.assertEqual(valuation_label(days, pes, "20210101"), "high_ge75")

    def test_mid_between_edges(self) -> None:
        days, pes = _hist_book(100.0)
        self.assertEqual(valuation_label(days, pes, "20210101"), "mid")

    def test_entry_day_row_excluded(self) -> None:
        # The row dated entry_day itself must never be read as current.
        days, pes = _book([10.0, 999.0])
        self.assertEqual(valuation_label(days, pes, days[1]), "short_history")

    def test_no_prior_row_is_short_history(self) -> None:
        days, pes = _book([10.0, 20.0], start_day=20210101)
        self.assertEqual(
            valuation_label(days, pes, "20200101"), "short_history")

    def test_loss_or_missing_current(self) -> None:
        for cur in (None, 0.0, -5.0):
            with self.subTest(cur=cur):
                days, pes = _hist_book(cur)
                self.assertEqual(
                    valuation_label(days, pes, "20210101"), "loss_or_missing")

    def test_short_history_below_threshold(self) -> None:
        days, pes = _hist_book(100.0)
        # Drop one valid value from the window: 199 < 200 required.
        pes[pes.index(7.0)] = None
        self.assertEqual(valuation_label(days, pes, "20210101"), "short_history")


class LoadPeBookTest(unittest.TestCase):
    def test_descending_shard_defensively_resorted(self) -> None:
        cache = Path(tempfile.mkdtemp())
        rows = ["ts_code,trade_date,pe_ttm\n"]
        for day, pe in (("20200103", "30.0"), ("20200102", "20.0"),
                        ("20200101", "10.0")):  # newest-first on disk
            rows.append(f"000001.SZ,{day},{pe}\n")
        (cache / "dailybasic_000001SZ.csv").write_text("".join(rows),
                                                       encoding="utf-8")
        days, pes = load_pe_book(cache, "000001.SZ")
        self.assertEqual(days, ["20200101", "20200102", "20200103"])
        self.assertEqual(pes, [10.0, 20.0, 30.0])

    def test_missing_shard_fails_closed(self) -> None:
        with self.assertRaises(ValuationStudyError) as ctx:
            load_pe_book(Path(tempfile.mkdtemp()), "000002.SZ")
        self.assertEqual(str(ctx.exception),
                         "cache_missing:dailybasic_000002.SZ.csv")


class AttachValuationBucketTest(unittest.TestCase):
    def _write_shard(self, cache: Path, code: str,
                     days: list[str], pes: list[float | None]) -> None:
        lines = ["ts_code,trade_date,pe_ttm\n"]
        for day, pe in reversed(list(zip(days, pes))):  # newest-first on disk
            value = "" if pe is None else f"{pe}"
            lines.append(f"{code.replace('.', '')},{day},{value}\n")
        (cache / f"dailybasic_{code.replace('.', '')}.csv").write_text(
            "".join(lines), encoding="utf-8")

    def test_attach_fails_closed_on_missing_shard(self) -> None:
        # Prereg D3: a missing dailybasic shard is fail-closed (today's
        # coverage is 1035/1035; absence is structural surprise, not a label).
        cache = Path(tempfile.mkdtemp())
        days, pes = _hist_book(50.0)
        self._write_shard(cache, "000001.SZ", days, pes)
        signals = [
            {"ts_code": "000001.SZ", "entry_day": "20210101"},
            {"ts_code": "000002.SZ", "entry_day": "20210101"},  # no shard
        ]
        with self.assertRaises(ValuationStudyError):
            attach_valuation_bucket(signals, cache)  # type: ignore[arg-type]

    def test_second_signal_reuses_cached_book(self) -> None:
        cache = Path(tempfile.mkdtemp())
        flat = [100.0] * 300
        days = [str(20200101 + i) for i in range(len(flat))]
        self._write_shard(cache, "000001.SZ", days, flat)
        # All-tied window: every history value <= current -> pct 1.0 -> high.
        signals = [
            {"ts_code": "000001.SZ", "entry_day": "20210101"},
            {"ts_code": "000001.SZ", "entry_day": "20210102"},
        ]
        stats = attach_valuation_bucket(signals, cache)  # type: ignore[arg-type]
        self.assertEqual(signals[0]["valuation_bucket"], "high_ge75")
        self.assertEqual(signals[1]["valuation_bucket"], "high_ge75")
        self.assertEqual(stats["attached"], 2)
        self.assertEqual(stats["high_ge75"], 2)


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
