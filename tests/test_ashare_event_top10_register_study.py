"""Offline tests for the top-ten register (panel #21) study."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from Ashare.event_top10_register_study import (
    RegisterStudyError,
    _double_low,
    attach_register_bucket,
    load_register_index,
    macro_class,
    register_bucket,
)

FIELDS = ["ts_code", "ann_date", "end_date", "holder_name",
          "hold_amount", "hold_ratio", "hold_float_ratio", "hold_change",
          "holder_type"]


def _row(code: str, ann: str, end: str, holder_type: str) -> list[str]:
    return [code, ann, end, "HOLDER", "100", "1.0", "", "", holder_type]


def _signal(code: str, float_date: str, entry_day: str | None = None) -> dict:
    return {
        "ts_code": code,
        "float_date": float_date,
        "entry_day": entry_day or float_date,
    }


def _write_file(cache: Path, stem: str, rows: list[list[str]]) -> None:
    with (cache / f"top10_{stem}.csv").open("w", newline="",
                                            encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(FIELDS)
        writer.writerows(rows)


class MacroClassTest(unittest.TestCase):
    def test_frozen_mapping(self) -> None:
        self.assertEqual(macro_class("自然人"), "natural")
        self.assertEqual(macro_class("开放式投资基金"), "fin_inst")
        self.assertEqual(macro_class("社保基金、社保机构"), "fin_inst")

    def test_unknown_and_blank_default_to_corp(self) -> None:
        self.assertEqual(macro_class("一般企业"), "corp_or_unknown")
        self.assertEqual(macro_class("风险投资公司"), "corp_or_unknown")
        self.assertEqual(macro_class(""), "corp_or_unknown")
        self.assertEqual(macro_class("未来新词表值"), "corp_or_unknown")


class RegisterBucketTest(unittest.TestCase):
    def _index(self, files: dict[str, list[list[str]]]) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            for stem, rows in files.items():
                _write_file(cache, stem, rows)
            return load_register_index(cache)

    def test_latest_qualifying_snapshot_wins(self) -> None:
        index = self._index({"000001SZ": [
            _row("000001.SZ", "20190401", "20181231", "自然人"),
            _row("000001.SZ", "20200401", "20191231", "开放式投资基金"),
        ]})
        # before the 2020 disclosure only the 2019 register qualifies
        self.assertEqual(register_bucket(index, "000001.SZ", "20200201"),
                         "natural_heavy")
        self.assertEqual(register_bucket(index, "000001.SZ", "20210101"),
                         "fin_inst_heavy")

    def test_same_period_revision_takes_latest_ann_date(self) -> None:
        index = self._index({"000001SZ": [
            _row("000001.SZ", "20190401", "20181231", "开放式投资基金"),
            _row("000001.SZ", "20190405", "20181231", "自然人"),
        ]})
        # both rows share end_date; the latest ann_date revision wins
        self.assertEqual(register_bucket(index, "000001.SZ", "20190501"),
                         "natural_heavy")

    def test_late_ann_row_does_not_block_earlier_valid_snapshot(self) -> None:
        index = self._index({"000001SZ": [
            _row("000001.SZ", "20250101", "20200101", "自然人"),
            _row("000001.SZ", "20200610", "20200601", "开放式投资基金"),
        ]})
        # full-scan semantics: the late-ann 2020 period row is not
        # disclosed by 20210101 but the mid-2020 snapshot is
        self.assertEqual(register_bucket(index, "000001.SZ", "20210101"),
                         "fin_inst_heavy")

    def test_share_thresholds_and_mixed(self) -> None:
        index = self._index({
            "000001SZ": [
                # 7 natural + 3 corp -> natural_heavy (share == 0.7)
                *[_row("000001.SZ", "20200601", "20200331", "自然人")
                  for _ in range(7)],
                _row("000001.SZ", "20200601", "20200331", "一般企业"),
                _row("000001.SZ", "20200601", "20200331", "投资公司"),
                _row("000001.SZ", "20200601", "20200331", ""),
            ],
            "600519SH": [
                # 5 fin + 5 corp -> mixed_other
                *[_row("600519.SH", "20200601", "20200331", "资产管理公司")
                  for _ in range(5)],
                *[_row("600519.SH", "20200601", "20200331", "国资局")
                  for _ in range(5)],
            ],
        })
        self.assertEqual(register_bucket(index, "000001.SZ", "20210101"),
                         "natural_heavy")
        self.assertEqual(register_bucket(index, "600519.SH", "20210101"),
                         "mixed_other")

    def test_no_match_reasons(self) -> None:
        index = self._index({
            "000001SZ": [
                _row("000001.SZ", "20260101", "20251231", "自然人"),
            ],
            "600519SH": [
                _row("600519.SH", "20200601", "20200331", "自然人"),
            ],
        })
        # symbol absent entirely
        self.assertEqual(register_bucket(index, "600000.SH", "20210101"),
                         "no_match")
        # file present but only post-expiry disclosures
        self.assertEqual(register_bucket(index, "000001.SZ", "20210101"),
                         "no_match")


class LoadRegisterIndexTest(unittest.TestCase):
    def test_aggregates_by_period_and_skips_bad_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_file(cache, "000001SZ", [
                _row("000001.SZ", "20200401", "20191231", "自然人"),
                _row("000001.SZ", "", "20191231", "自然人"),      # bad ann
                _row("000001.SZ", "20200401", "x", "自然人"),     # bad end
            ])
            index = load_register_index(cache)
            snaps = index["000001.SZ"]
            self.assertEqual(len(snaps), 1)
            (end_day, ann_day), counts = snaps[0]
            self.assertEqual((end_day, ann_day), ("20191231", "20200401"))
            self.assertEqual(counts, [1, 0, 0])

    def test_empty_cache_fails_closed(self) -> None:
        with self.assertRaises(RegisterStudyError) as ctx:
            load_register_index(Path(tempfile.mkdtemp()))
        self.assertEqual(str(ctx.exception), "cache_missing:top10_*.csv")


class AttachRegisterBucketTest(unittest.TestCase):
    def _index(self) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_file(cache, "000001SZ", [
                _row("000001.SZ", "20200401", "20191231", "自然人"),
            ])
            _write_file(cache, "000002SZ", [
                # only post-expiry disclosures for the 2021 event below
                _row("000002.SZ", "20250101", "20241231", "自然人"),
            ])
            return load_register_index(cache)

    def test_labels_key_on_float_date_not_entry_day(self) -> None:
        signals = [_signal("000001.SZ", "20210101", entry_day="20210104")]
        stats = attach_register_bucket(signals, self._index())
        self.assertEqual(signals[0]["reg_bucket"], "natural_heavy")
        self.assertEqual(stats["natural_heavy"], 1)
        self.assertEqual(signals[0]["reg_no_match_reason"], "")

    def test_no_match_split_records_reasons(self) -> None:
        signals = [_signal("600000.SH", "20210101"),
                   _signal("000002.SZ", "20210101")]
        stats = attach_register_bucket(signals, self._index())
        self.assertEqual(signals[0]["reg_no_match_reason"], "no_file")
        self.assertEqual(signals[1]["reg_no_match_reason"],
                         "no_early_snapshot")
        self.assertEqual(stats["no_match"], 2)


class DoubleLowGateTest(unittest.TestCase):
    def test_gate_legs_and_threshold(self) -> None:
        baseline = {"n": 300, "mean_net_bps": 100.0, "win_rate": 0.55}
        passing = {"n": 30, "mean_net_bps": 99.9, "win_rate": 0.549}
        thin_n = {"n": 29, "mean_net_bps": 99.9, "win_rate": 0.549}
        mean_high = {"n": 30, "mean_net_bps": 150.0, "win_rate": 0.500}
        self.assertTrue(_double_low(passing, baseline))
        self.assertFalse(_double_low(thin_n, baseline))
        self.assertFalse(_double_low(mean_high, baseline))


if __name__ == "__main__":
    unittest.main()
