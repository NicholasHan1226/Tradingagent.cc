"""Tripwires for fetch-window bounds (#545).

The whole event-data family silently starves when a window bound is a
frozen literal: disclosure appointments, bar pagination, per-symbol
windows all stop at whatever date was written into the constant.  These
tests fail whenever someone reintroduces a hardcoded bound (they can only
pass by coincidence on that one literal date).
"""

from __future__ import annotations

import csv
import time

from Ashare import event_calendar_fetch as fetch_mod
from Ashare import event_calendar_expand_samples as expand


class TestStudyEndDynamic:
    def test_study_end_is_run_time_today(self):
        assert fetch_mod.STUDY_END == time.strftime("%Y%m%d")

    def test_study_start_stays_frozen(self):
        # The retrospective anchor is intentionally stable.
        assert fetch_mod.STUDY_START == "20180101"


class TestSeriesWindowEnd:
    def test_window_end_is_current_year_end(self):
        end = expand.series_window_end()
        today = time.strftime("%Y%m%d")
        assert end.startswith(today[:4])
        assert end.endswith("1231")
        assert end >= today


class TestDisclosureRefresh:
    """The calendar rebuild must sweep every report period, not just years.

    The endpoint matches the REPORT PERIOD itself: year-sliced range pulls
    silently capture only annual closes, so months of "successful" refreshes
    produced an annual-only calendar whose next inflow was a year away.
    force=True must query each quarter/year close exactly and upsert by
    (ts_code, end_date) so rescheduled appointments replace stale rows.
    """

    FIELDS = ["ts_code", "end_date", "pre_date", "ann_date"]
    TODAY = time.strftime("%Y%m%d")

    def _write_cached(self, cache_dir, rows):
        path = cache_dir / "disclosure.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(self.FIELDS)
            writer.writerows(rows)

    def _read_back(self, cache_dir):
        with (cache_dir / "disclosure.csv").open(encoding="utf-8") as handle:
            reader = csv.reader(handle)
            assert next(reader) == self.FIELDS
            return [list(r) for r in reader]

    def test_period_ends_enumerate_every_quarter_close(self):
        ends = fetch_mod._disclosure_period_ends("20181201", "20190601")
        assert ends == ["20181231", "20190331"]

    def test_cached_copy_reused_without_force(self, tmp_path, monkeypatch):
        self._write_cached(tmp_path, [["000001.SZ", "20251231", "20260430", "20260410"]])
        calls = []
        monkeypatch.setattr(fetch_mod, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(
            fetch_mod, "call_api",
            lambda api, params: calls.append(params) or (self.FIELDS, []),
        )
        fields, rows = fetch_mod.refresh_disclosure(force=False)
        assert calls == []  # no network when a copy exists and force is off
        assert rows == [["000001.SZ", "20251231", "20260430", "20260410"]]

    def test_force_sweeps_all_periods_and_upserts_reschedules(
        self, tmp_path, monkeypatch
    ):
        # Seed mirrors the historical defect: an annual-only table holding a
        # stale appointment, plus a pre-sweep row that must survive untouched.
        self._write_cached(
            tmp_path,
            [
                ["000001.SZ", "20260630", "20260715", "20260630"],
                ["999999.SZ", "20170331", "20170825", "20170801"],
            ],
        )
        served = {
            "20260630": [["000001.SZ", "20260630", "20260831", "20260823"]],
            "20260930": [["600000.SH", "20260930", "20261028", "20261015"]],
        }

        def fake_call(api, params):
            assert api == "disclosure_date"
            assert set(params) == {"end_date"}  # exact-period shape only
            return list(self.FIELDS), [list(r) for r in served.get(params["end_date"], [])]

        periods_asked = []
        orig_enum = fetch_mod._disclosure_period_ends

        def recording_enum(start, end):
            periods_asked.extend(orig_enum(start, end))
            return orig_enum(start, end)

        monkeypatch.setattr(fetch_mod, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(fetch_mod, "call_api", fake_call)
        monkeypatch.setattr(fetch_mod, "_disclosure_period_ends", recording_enum)

        fields, rows = fetch_mod.refresh_disclosure(force=True)

        # Sweep reaches every quarter/year close through the future horizon,
        # including periods beyond today (next season's appointments).
        assert periods_asked[0] == "20180331"
        assert periods_asked[-1] > self.TODAY
        assert len(periods_asked) > 30

        by_key = {(r[0], r[1]): r for r in rows}
        # Reschedule wins over the stale announcement...
        assert by_key[("000001.SZ", "20260630")] == [
            "000001.SZ", "20260630", "20260831", "20260823",
        ]
        # ...new symbols land, and pre-sweep history survives verbatim.
        assert by_key[("600000.SH", "20260930")] == [
            "600000.SH", "20260930", "20261028", "20261015",
        ]
        assert by_key[("999999.SZ", "20170331")] == [
            "999999.SZ", "20170331", "20170825", "20170801",
        ]

    def test_force_keeps_stored_row_when_announcement_is_older(
        self, tmp_path, monkeypatch
    ):
        self._write_cached(
            tmp_path, [["000001.SZ", "20260630", "20260831", "20260820"]]
        )
        served = {
            # Out-of-order delivery of an older revision must not regress.
            "20260630": [["000001.SZ", "20260630", "20260715", "20260630"]],
        }
        monkeypatch.setattr(fetch_mod, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(
            fetch_mod, "call_api",
            lambda api, params: (list(self.FIELDS), [list(r) for r in served.get(params["end_date"], [])]),
        )
        _fields, rows = fetch_mod.refresh_disclosure(force=True)
        assert rows == [["000001.SZ", "20260630", "20260831", "20260820"]]

    def test_schema_drift_fails_closed(self, tmp_path, monkeypatch):
        self._write_cached(tmp_path, [])
        state = {"n": 0}

        def drifting_call(api, params):
            state["n"] += 1
            fields = list(self.FIELDS) if state["n"] == 1 else self.FIELDS[:-1]
            return fields, []

        monkeypatch.setattr(fetch_mod, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(fetch_mod, "call_api", drifting_call)
        try:
            fetch_mod.refresh_disclosure(force=True)
        except fetch_mod.FetchError as exc:
            assert "schema_drift" in str(exc)
        else:
            raise AssertionError("expected FetchError on schema drift")
