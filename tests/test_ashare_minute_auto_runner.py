from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

import pytest

from Ashare.minute_auto_runner import (
    MinuteAutoRunnerError,
    expected_available_bar_end,
    run_current_delayed_minute_paper,
    session_bar_ends,
)
from Ashare.minute_data import SHANGHAI


REPO_ROOT = Path(__file__).resolve().parents[1]


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=SHANGHAI)


def _initialized_day(root: Path, *, last_bar: str | None) -> Path:
    day = root / "20260728"
    day.mkdir()
    for name in ("minute-manifest.json", "reference-facts.json", "universe.json"):
        (day / name).write_text("{}\n", encoding="utf-8")
    if last_bar is not None:
        (day / "state-bundle.json").write_text(
            json.dumps({"last_receipt": {"bar_end": last_bar}}),
            encoding="utf-8",
        )
    return day


def test_session_has_48_bars_and_excludes_lunch() -> None:
    slots = session_bar_ends(_at("2026-07-28T10:00:00").date())

    assert len(slots) == 48
    assert slots[0].strftime("%H:%M") == "09:35"
    assert slots[23].strftime("%H:%M") == "11:30"
    assert slots[24].strftime("%H:%M") == "13:05"
    assert slots[-1].strftime("%H:%M") == "15:00"


@pytest.mark.parametrize(
    ("now", "expected"),
    (
        ("2026-07-28T09:39:59", None),
        ("2026-07-28T09:40:40", "09:35"),
        ("2026-07-28T11:40:40", "11:30"),
        ("2026-07-28T13:05:40", "11:30"),
        ("2026-07-28T13:10:40", "13:05"),
        ("2026-07-28T15:05:40", "15:00"),
    ),
)
def test_expected_available_bar_respects_provider_lag_and_sessions(
    now: str,
    expected: str | None,
) -> None:
    result = expected_available_bar_end(_at(now))

    assert (None if result is None else result.strftime("%H:%M")) == expected


def test_missing_session_directory_is_safe_noop(tmp_path: Path) -> None:
    result = run_current_delayed_minute_paper(
        state_root=tmp_path,
        token_file=Path("/run/private/token"),
        now=_at("2026-07-28T09:40:40"),
    )

    assert result == {
        "status": "noop",
        "reason": "session_not_initialized",
        "trading_date": "2026-07-28",
        "real_trading_enabled": False,
    }


def test_already_processed_bar_is_safe_noop(tmp_path: Path) -> None:
    _initialized_day(tmp_path, last_bar="2026-07-28 13:45:00")

    result = run_current_delayed_minute_paper(
        state_root=tmp_path,
        token_file=Path("/run/private/token"),
        now=_at("2026-07-28T13:50:40"),
    )

    assert result["reason"] == "bar_already_processed"


def test_gap_fails_closed_without_calling_runner(tmp_path: Path) -> None:
    _initialized_day(tmp_path, last_bar="2026-07-28 13:35:00")
    called = False

    def fake_run_once(**_: object) -> dict[str, object]:
        nonlocal called
        called = True
        return {"status": "pass"}

    with pytest.raises(MinuteAutoRunnerError, match="minute_auto_bar_gap_detected"):
        run_current_delayed_minute_paper(
            state_root=tmp_path,
            token_file=Path("/run/private/token"),
            now=_at("2026-07-28T13:50:40"),
            run_once=fake_run_once,
        )

    assert called is False


def test_current_bar_delegates_exactly_once(tmp_path: Path) -> None:
    day = _initialized_day(tmp_path, last_bar="2026-07-28 13:40:00")
    calls: list[dict[str, object]] = []

    def fake_run_once(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"status": "pass", "bar_end": kwargs["bar_end"]}

    result = run_current_delayed_minute_paper(
        state_root=tmp_path,
        token_file=Path("/run/private/token"),
        now=_at("2026-07-28T13:50:40"),
        run_once=fake_run_once,
    )

    assert result == {"status": "pass", "bar_end": "2026-07-28 13:45:00"}
    assert len(calls) == 1
    assert calls[0]["state_bundle"] == day / "state-bundle.json"
    assert calls[0]["decision_time"] == _at("2026-07-28T13:50:40")
    assert calls[0]["trading_date"].isoformat() == "2026-07-28"
    assert (day / ".minute-auto.lock").stat().st_mode & 0o777 == 0o600


def test_first_bar_can_initialize_but_midday_cannot(tmp_path: Path) -> None:
    _initialized_day(tmp_path, last_bar=None)

    with pytest.raises(MinuteAutoRunnerError, match="minute_auto_initial_bar_missing"):
        run_current_delayed_minute_paper(
            state_root=tmp_path,
            token_file=Path("/run/private/token"),
            now=_at("2026-07-28T13:10:40"),
            run_once=lambda **_: {"status": "pass"},
        )


def test_real_trading_flag_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "true")

    with pytest.raises(MinuteAutoRunnerError, match="real_trading"):
        run_current_delayed_minute_paper(
            state_root=tmp_path,
            token_file=Path("/run/private/token"),
            now=_at("2026-07-28T09:40:40"),
        )


def test_minute_timer_has_exactly_the_48_delayed_session_triggers() -> None:
    timer = (
        REPO_ROOT
        / "deploy/systemd/tradingagent-ashare-minute-paper.timer"
    ).read_text(encoding="utf-8")

    calendar_lines = tuple(
        line for line in timer.splitlines() if line.startswith("OnCalendar=")
    )

    assert calendar_lines == (
        "OnCalendar=Mon..Fri *-*-* 09:44/5:00",
        "OnCalendar=Mon..Fri *-*-* 10:04/5:00",
        "OnCalendar=Mon..Fri *-*-* 11:04..39/5:00",
        "OnCalendar=Mon..Fri *-*-* 13:14/5:00",
        "OnCalendar=Mon..Fri *-*-* 14:04/5:00",
        "OnCalendar=Mon..Fri *-*-* 15:04:00",
        "OnCalendar=Mon..Fri *-*-* 15:09:00",
    )
    assert "09..11" not in timer
    assert "13..15" not in timer
    assert "Persistent=false" in timer
    assert "Unit=tradingagent-ashare-minute-paper.service" in timer
