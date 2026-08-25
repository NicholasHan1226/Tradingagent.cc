"""Unit tests for the top-1000 universe refresh machinery (#540 follow-up).

Covers the two production-surface correctness fixes that keep the expanded
tracker universe fed on the scheduled CI surface: stale-series detection,
lockup-row merging into the single ``share_float.csv`` table, and the
fetch-side merge that stops weekly top-200 refetches from truncating
expansion-universe unlock history.
"""

from __future__ import annotations

import csv

import pytest

from Ashare import event_calendar_expand_samples as expand
from Ashare import event_calendar_fetch as fetch_mod


def _write_csv(path, header, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def _read_rows(path):
    with path.open(encoding="utf-8") as handle:
        reader = csv.reader(handle)
        return next(reader), [row for row in reader]


class TestSeriesFreshness:
    def test_missing_shard_is_never_fresh(self, tmp_path):
        assert expand.series_max_day(tmp_path, "600001.SH") is None
        assert not expand.series_is_fresh(
            tmp_path, "600001.SH", "20260825", max_age_days=6
        )

    def test_stale_and_fresh_shards(self, tmp_path):
        code = "600001.SH"
        _write_csv(
            tmp_path / f"daily_{code.replace('.', '')}.csv",
            ["ts_code", "trade_date", "close"],
            [[code, "20260810", "10.0"]],
        )
        last = expand.series_max_day(tmp_path, code)
        assert last == "20260810"
        # Gap of 15 days trips the 6-day ceiling; gap of 2 does not.
        assert not expand.series_is_fresh(
            tmp_path, "600001.SH", "20260825", max_age_days=6
        )
        assert expand.series_is_fresh(
            tmp_path, "600001.SH", "20260812", max_age_days=6
        )


class TestLockupMergeIntoSingleTable:
    def test_merges_into_share_float_csv_without_duplicates(
        self, tmp_path, monkeypatch
    ):
        header = [
            "ts_code", "ann_date", "float_date", "float_share",
            "float_ratio", "holder_name", "share_type",
        ]
        _write_csv(
            tmp_path / "share_float.csv",
            header,
            [["600001.SH", "20260701", "20260805", "1200000", "1.5",
              "Holder A", "IPO"]],
        )

        def fake_call(api_name, params):
            assert api_name == "share_float"
            return header, [
                # Duplicate of the stored batch identity -> dropped.
                ["600001.SH", "20260701", "20260805", "1200000", "1.5",
                 "Holder A", "IPO"],
                # New announcement -> appended.
                ["600001.SH", "20260820", "20260918", "800000", "0.9",
                 "Holder B", "定增股份"],
            ]

        monkeypatch.setattr(expand, "call_api", fake_call)
        seen = expand.fetch_symbol_lockups(tmp_path, "600001.SH")
        assert seen == 2

        _, rows = _read_rows(tmp_path / "share_float.csv")
        assert len(rows) == 2
        float_days = sorted(r[2] for r in rows)
        assert float_days == ["20260805", "20260918"]

    def test_creates_table_when_absent(self, tmp_path, monkeypatch):
        header = [
            "ts_code", "ann_date", "float_date", "float_share",
            "float_ratio", "holder_name", "share_type",
        ]

        def fake_call(api_name, params):
            return header, [
                ["600777.SH", "20260701", "20260805", "10", "4.0",
                 "Holder C", "IPO"],
            ]

        monkeypatch.setattr(expand, "call_api", fake_call)
        assert expand.fetch_symbol_lockups(tmp_path, "600777.SH") == 1
        _, rows = _read_rows(tmp_path / "share_float.csv")
        assert len(rows) == 1


class TestFetchShareFloatMerge:
    def test_top200_refetch_never_truncates_expansion_rows(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(fetch_mod, "CACHE_DIR", tmp_path)
        header = [
            "ts_code", "ann_date", "float_date", "float_share",
            "float_ratio", "holder_name", "share_type",
        ]
        expansion_row = [
            "600777.SH", "20260601", "20260706", "5", "2.2",
            "Expansion Holder", "IPO",
        ]
        _write_csv(tmp_path / "share_float.csv", header, [expansion_row])

        base_sample_row_new = [
            "600001.SH", "20260701", "20260805", "1200000", "1.5",
            "Holder A", "IPO",
        ]
        base_sample_row_dup = expansion_row[:]  # same identity, refetched

        out_fields, merged = fetch_mod.merge_share_float_rows(
            header, [base_sample_row_new, base_sample_row_dup]
        )
        assert out_fields == header
        codes = {r[0] for r in merged}
        # The expansion symbol survives the top-200 refetch...
        assert codes == {"600777.SH", "600001.SH"}
        # ...and the duplicate batch collapses to one row.
        keys = [(r[0], r[1], r[2], r[5]) for r in merged]
        assert len(keys) == len(set(keys)) == 2
