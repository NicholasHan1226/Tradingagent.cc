"""Unit tests for the top-1000 universe refresh machinery (#540 follow-up).

Covers the two production-surface correctness fixes that keep the expanded
tracker universe fed on the scheduled CI surface: stale-series detection,
lockup-row merging into the single ``share_float.csv`` table, and the
fetch-side merge that stops weekly top-200 refetches from truncating
expansion-universe unlock history.  Also covers the call-volume rework that
keeps weekly backfills inside the run ceiling: date-driven whole-market
bar top-ups and the batched lockup merge.
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

    def test_prefix_targets_other_shard_families(self, tmp_path):
        # The dailybasic fetcher reuses these helpers for its own shard
        # family (#543): same stem layout, different file prefix.
        _write_csv(
            tmp_path / "dailybasic_600001SH.csv",
            ["ts_code", "trade_date", "pe_ttm"],
            [["600001.SH", "20260824", "9.5"]],
        )
        assert expand.series_max_day(
            tmp_path, "600001.SH", prefix="dailybasic"
        ) == "20260824"
        assert expand.series_is_fresh(
            tmp_path, "600001.SH", "20260825", max_age_days=6,
            prefix="dailybasic",
        )
        assert not expand.series_is_fresh(
            tmp_path, "600001.SH", "20260825", max_age_days=6, prefix="pe"
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


class TestSeriesTopup:
    """Date-driven whole-market top-up replaces per-symbol full re-pulls."""

    HEADER_DAILY = ["ts_code", "trade_date", "close"]
    HEADER_ADJ = ["ts_code", "trade_date", "adj_factor"]

    def _seed(self, cache):
        # Shard frozen at 20260810 — beyond the 6-day freshness ceiling,
        # inside the 30-day top-up window.
        _write_csv(cache / "daily_600001SH.csv", self.HEADER_DAILY, [
            ["600001.SH", "20260810", "10.0"],
            ["600001.SH", "20260807", "9.8"],
        ])
        _write_csv(cache / "adjfactor_600001SH.csv", self.HEADER_ADJ, [
            ["600001.SH", "20260810", "1.5"],
            ["600001.SH", "20260807", "1.4"],
        ])

    @staticmethod
    def _fake_call(daily_by_day, adj_by_day, calls):
        def fake(api_name, params):
            calls.append((api_name, dict(params)))
            if api_name == "trade_cal":
                return ["cal_date", "is_open"], [
                    ["20260811", "0"],  # Tuesday... whatever: closed
                    ["20260812", "1"],
                    ["20260813", "1"],
                    ["20260814", "0"],
                    ["20260815", "0"],
                    ["20260816", "1"],
                ]
            day = params["trade_date"]
            if api_name == "daily":
                return TestSeriesTopup.HEADER_DAILY, daily_by_day.get(day, [])
            return TestSeriesTopup.HEADER_ADJ, adj_by_day.get(day, [])
        return fake

    def test_topup_projects_new_sessions_newest_first(self, tmp_path, monkeypatch):
        self._seed(tmp_path)
        daily = {
            "20260812": [["600001.SH", "20260812", "10.2"]],
            "20260813": [["600001.SH", "20260813", "10.4"]],
            "20260816": [["600001.SH", "20260816", "10.6"]],
        }
        adj = {"20260812": [["600001.SH", "20260812", "1.52"]]}
        calls: list[tuple] = []
        monkeypatch.setattr(expand, "call_api",
                            self._fake_call(daily, adj, calls))
        extended = expand.fetch_series_topup(
            tmp_path, ["600001.SH"], today="20260816"
        )
        # One daily + one adjfactor shard extended.
        assert extended == 2
        _, rows = _read_rows(tmp_path / "daily_600001SH.csv")
        assert [r[1] for r in rows] == [
            "20260816", "20260813", "20260812", "20260810", "20260807",
        ]
        _, arows = _read_rows(tmp_path / "adjfactor_600001SH.csv")
        assert [r[1] for r in arows] == [
            "20260812", "20260810", "20260807",
        ]
        data_calls = [c for c in calls if c[0] != "trade_cal"]
        # Two endpoints x three open sessions — not two calls per symbol.
        assert len(data_calls) == 6
        assert all("ts_code" not in params for _api, params in data_calls)

    def test_symbols_absent_from_sessions_gain_nothing(
        self, tmp_path, monkeypatch,
    ):
        self._seed(tmp_path)
        # A second, less-stale shard whose symbol is suspended all window.
        _write_csv(tmp_path / "daily_000002SZ.csv", self.HEADER_DAILY,
                   [["000002.SZ", "20260812", "20.0"]])
        _write_csv(tmp_path / "adjfactor_000002SZ.csv", self.HEADER_ADJ,
                   [["000002.SZ", "20260812", "2.0"]])
        calls: list[tuple] = []
        monkeypatch.setattr(expand, "call_api",
                            self._fake_call({}, {}, calls))
        extended = expand.fetch_series_topup(
            tmp_path, ["600001.SH", "000002.SZ"], today="20260816"
        )
        assert extended == 0
        _, rows = _read_rows(tmp_path / "daily_000002SZ.csv")
        assert [r[1] for r in rows] == ["20260812"]  # untouched

    def test_capped_response_fails_closed(self, tmp_path, monkeypatch):
        self._seed(tmp_path)
        capped = [["999999.SH", "20260812", "1.0"]] * expand.PAGE_LIMIT

        def fake(api_name, params):
            if api_name == "trade_cal":
                return ["cal_date", "is_open"], [["20260812", "1"]]
            return TestSeriesTopup.HEADER_DAILY, capped

        monkeypatch.setattr(expand, "call_api", fake)
        with pytest.raises(expand.ExpandError, match="date_capped"):
            expand.fetch_series_topup(tmp_path, ["600001.SH"], "20260816")

    def test_idempotent_rerun_pulls_no_data(self, tmp_path, monkeypatch):
        self._seed(tmp_path)
        daily = {"20260812": [["600001.SH", "20260812", "10.2"]],
                 "20260816": [["600001.SH", "20260816", "10.6"]]}
        adj = {"20260812": [["600001.SH", "20260812", "1.52"]],
               "20260816": [["600001.SH", "20260816", "1.56"]]}
        calls: list[tuple] = []
        monkeypatch.setattr(expand, "call_api",
                            self._fake_call(daily, adj, calls))
        expand.fetch_series_topup(tmp_path, ["600001.SH"], "20260816")
        # Both shard families are now current through 20260816; rerunning
        # against the same end date short-circuits before any call.
        before = len(calls)
        assert expand.fetch_series_topup(
            tmp_path, ["600001.SH"], "20260816"
        ) == 0
        assert len(calls) == before


class TestBatchedLockupMerger:
    """The weekly sweep reads the table once and writes it back once."""

    LOCKUP_HEADER = [
        "ts_code", "ann_date", "float_date", "float_share",
        "float_ratio", "holder_name", "share_type",
    ]

    SEED = [["600001.SH", "20260701", "20260805", "1200000", "1.5",
             "Holder A", "IPO"]]
    RESP_X = [
        # Duplicate of the stored batch identity...
        ["600001.SH", "20260701", "20260805", "1200000", "1.5",
         "Holder A", "IPO"],
        # ...plus a new announcement.
        ["600001.SH", "20260820", "20260918", "800000", "0.9",
         "Holder B", "定增股份"],
    ]
    RESP_Y = [["000002.SZ", "20260702", "20260806", "500", "2.0",
               "Holder C", "IPO"]]

    def _seed_table(self, cache):
        _write_csv(cache / "share_float.csv", self.LOCKUP_HEADER, self.SEED)

    def _scripted_call(self):
        responses = {"600001.SH": self.RESP_X, "000002.SZ": self.RESP_Y}

        def fake(api_name, params):
            assert api_name == "share_float"
            return self.LOCKUP_HEADER, responses[params["ts_code"]]
        return fake

    def test_batched_matches_sequential_wrapper(
        self, tmp_path, monkeypatch,
    ):
        sequential_cache = tmp_path / "seq"
        batched_cache = tmp_path / "bat"
        sequential_cache.mkdir()
        batched_cache.mkdir()
        self._seed_table(sequential_cache)
        self._seed_table(batched_cache)

        monkeypatch.setattr(expand, "call_api", self._scripted_call())
        expand.fetch_symbol_lockups(sequential_cache, "600001.SH")
        expand.fetch_symbol_lockups(sequential_cache, "000002.SZ")

        merger = expand.ShareFloatMerger.load(batched_cache)
        merger.absorb(self.LOCKUP_HEADER, self.RESP_X)
        merger.absorb(self.LOCKUP_HEADER, self.RESP_Y)
        merger.save()

        seq_bytes = (sequential_cache / "share_float.csv").read_bytes()
        bat_bytes = (batched_cache / "share_float.csv").read_bytes()
        assert seq_bytes == bat_bytes

    def test_absorb_counts_all_rows_including_duplicates(
        self, tmp_path,
    ):
        merger = expand.ShareFloatMerger(
            tmp_path, list(self.LOCKUP_HEADER), [row[:] for row in self.SEED]
        )
        seen = merger.absorb(self.LOCKUP_HEADER, self.RESP_X)
        assert seen == 2  # rows_seen semantics count API rows, not merges
        assert len(merger.rows) == 2  # duplicate collapsed

    def test_missing_table_takes_schema_from_first_response(
        self, tmp_path,
    ):
        merger = expand.ShareFloatMerger.load(tmp_path)
        assert merger.absorb(self.LOCKUP_HEADER, self.RESP_Y) == 1
        merger.save()
        fields, rows = _read_rows(tmp_path / "share_float.csv")
        assert fields == self.LOCKUP_HEADER
        assert len(rows) == 1


class TestPartitionRefresh:
    """Work-list split: missing / beyond-window-or-broken-pair / topup."""

    def _shard(self, cache, prefix, code, last):
        _write_csv(
            cache / f"{prefix}_{code.replace('.', '')}.csv",
            ["ts_code", "trade_date", "x"],
            [[code, last, "1"]],
        )

    def test_buckets(self, tmp_path):
        todo = ["A.SH", "B.SH", "C.SH", "D.SH"]
        self._shard(tmp_path, "daily", "A.SH", "20260824")
        self._shard(tmp_path, "adjfactor", "A.SH", "20260824")  # recent
        # B: gap of 40 days -> fallback
        self._shard(tmp_path, "daily", "B.SH", "20260710")
        self._shard(tmp_path, "adjfactor", "B.SH", "20260710")
        # C: daily recent but adjfactor shard missing -> broken pair
        self._shard(tmp_path, "daily", "C.SH", "20260824")
        missing, fallback, topup = expand.partition_refresh(
            tmp_path, todo, cutoff="20260727"
        )
        assert missing == ["D.SH"]
        assert fallback == ["B.SH", "C.SH"]
        assert topup == ["A.SH"]


class TestTopupPrefixAlignment:
    """Each shard family filters against its OWN watermark.

    The shared research cache can be touched between runs, leaving the
    daily and adjfactor shards at different last sessions; filtering the
    adjfactor projection with the daily watermark appended duplicates.
    """

    def test_divergent_prefixes_gain_no_duplicates(self, tmp_path, monkeypatch):
        header_d = ["ts_code", "trade_date", "close"]
        header_a = ["ts_code", "trade_date", "adj_factor"]
        _write_csv(tmp_path / "daily_600001SH.csv", header_d,
                   [["600001.SH", "20260824", "10.0"]])
        # adjfactor already carries 20260825 (cache touched between runs).
        _write_csv(tmp_path / "adjfactor_600001SH.csv", header_a,
                   [["600001.SH", "20260825", "139.0"]])

        def fake(api_name, params):
            if api_name == "trade_cal":
                return ["cal_date", "is_open"], [["20260825", "1"]]
            if api_name == "daily":
                return header_d, [["600001.SH", "20260825", "10.2"]]
            return header_a, [["600001.SH", "20260825", "139.0"]]

        monkeypatch.setattr(expand, "call_api", fake)
        extended = expand.fetch_series_topup(
            tmp_path, ["600001.SH"], today="20260826"
        )
        # Daily gains 0825; adjfactor already has it and must not dup.
        assert extended == 1
        _, arows = _read_rows(tmp_path / "adjfactor_600001SH.csv")
        assert [r[1] for r in arows] == ["20260825"]
        _, drows = _read_rows(tmp_path / "daily_600001SH.csv")
        assert [r[1] for r in drows] == ["20260825", "20260824"]
