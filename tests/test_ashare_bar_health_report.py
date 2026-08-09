from __future__ import annotations

from datetime import datetime

import pytest

from tools.ashare_bar_health_report import (
    classify,
    extract_failure_reason,
    parse_journal_line,
    session_bars,
)


def test_session_bars_are_the_48_session_slots() -> None:
    bars = session_bars(datetime(2026, 8, 7).date())
    assert len(bars) == 48
    assert bars[0].strftime("%H:%M") == "09:35"
    assert bars[23].strftime("%H:%M") == "11:30"
    assert bars[24].strftime("%H:%M") == "13:05"
    assert bars[-1].strftime("%H:%M") == "15:00"


def test_extract_failure_reason_reads_the_reason_code() -> None:
    line = (
        '{"contract":"tradingagent.ashare.minute_auto_runner_failure.v1",'
        '"failure_reason": "minute_metadata_not_ready","status":"failed_closed"}'
    )
    assert extract_failure_reason(line) == "minute_metadata_not_ready"
    assert extract_failure_reason("no reason here") == "fail"


def test_parse_journal_line_handles_pass_and_failure() -> None:
    passed = parse_journal_line(
        "2026-08-07T14:15:52+08:00 host systemd[1]: "
        "Finished tradingagent-ashare-minute-paper.service."
    )
    assert passed is not None
    when, state = passed
    assert state == "pass"
    assert when.hour == 14 and when.minute == 15

    failed = parse_journal_line(
        '2026-08-07T14:25:46+08:00 host python3[1]: '
        '{"failure_reason": "minute_metadata_not_ready","status":"failed_closed"}'
    )
    assert failed is not None
    assert failed[1] == "minute_metadata_not_ready"

    assert parse_journal_line("garbage") is None


def test_classify_maps_journal_events_to_the_bar_window() -> None:
    bar = datetime.fromisoformat("2026-08-07T14:10:00+08:00")
    events = (
        (datetime.fromisoformat("2026-08-07T14:15:52+08:00"), "pass"),
        (datetime.fromisoformat("2026-08-07T14:16:10+08:00"), "fail"),
    )
    assert classify(events, bar) == "pass"

    late = ((datetime.fromisoformat("2026-08-07T14:17:00+08:00"), "pass"),)
    assert classify(late, bar) == "no_run"

    failed = ((datetime.fromisoformat("2026-08-07T14:16:10+08:00"), "minute_x"),)
    assert classify(failed, bar) == "minute_x"
