"""Tripwires for fetch-window bounds (#545).

The whole event-data family silently starves when a window bound is a
frozen literal: disclosure appointments, bar pagination, per-symbol
windows all stop at whatever date was written into the constant.  These
tests fail whenever someone reintroduces a hardcoded bound (they can only
pass by coincidence on that one literal date).
"""

from __future__ import annotations

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
