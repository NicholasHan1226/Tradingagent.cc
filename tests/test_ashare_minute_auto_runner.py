from __future__ import annotations

from datetime import datetime
from datetime import timedelta
import json
from pathlib import Path

import pytest

import Ashare.minute_auto_runner as auto_runner_module
from Ashare.minute_auto_runner import (
    MinuteAutoRunnerError,
    expected_available_bar_end,
    main,
    run_current_delayed_minute_paper,
    session_bar_ends,
)
from Ashare.minute_data import MinuteDataContractError, SHANGHAI


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


def test_main_reports_fail_closed_provenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failing runner writes a secret-free reason code before exit 2."""

    def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise MinuteAutoRunnerError("minute_auto_continuity_invalid")

    monkeypatch.setattr(
        "Ashare.minute_auto_runner.run_current_delayed_minute_paper", fail
    )
    code = main(
        [
            "--state-root",
            str(tmp_path),
            "--token-file",
            str(tmp_path / "token"),
        ]
    )
    captured = capsys.readouterr()

    assert code == 2
    assert "automatic delayed minute paper runner failed closed" in captured.err
    assert "minute_auto_continuity_invalid" in captured.err
    assert "minute_auto_runner_failure.v1" in captured.err


def test_main_reports_data_contract_reason_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """MinuteDataContractError reason codes surface in the failure contract."""

    from Ashare.minute_data import MinuteDataContractError

    def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise MinuteDataContractError("minute_metadata_not_ready")

    monkeypatch.setattr(
        "Ashare.minute_auto_runner.run_current_delayed_minute_paper", fail
    )
    code = main(
        [
            "--state-root",
            str(tmp_path),
            "--token-file",
            str(tmp_path / "token"),
        ]
    )
    captured = capsys.readouterr()

    assert code == 2
    assert "minute_metadata_not_ready" in captured.err


@pytest.mark.parametrize(
    ("now", "expected"),
    (
        ("2026-07-28T09:41:59", None),
        ("2026-07-28T09:42:00", "09:35"),
        ("2026-07-28T09:42:30", "09:35"),
        ("2026-07-28T09:42:31", None),
        ("2026-07-28T09:47:00", "09:40"),
        ("2026-07-28T09:47:30", "09:40"),
        ("2026-07-28T09:47:31", None),
        ("2026-07-28T11:37:00", "11:30"),
        ("2026-07-28T11:37:30", "11:30"),
        ("2026-07-28T11:45:40", None),
        ("2026-07-28T13:12:00", "13:05"),
        ("2026-07-28T13:12:30", "13:05"),
        ("2026-07-28T13:12:10", "13:05"),
        ("2026-07-28T15:10:40", None),
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
        now=_at("2026-07-28T09:47:00"),
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
        now=_at("2026-07-28T13:52:00"),
    )

    assert result["reason"] == "bar_already_processed"


def test_gap_resumes_with_explicit_non_learning_segment_reset(tmp_path: Path) -> None:
    _initialized_day(tmp_path, last_bar="2026-07-28 13:35:00")
    calls: list[dict[str, object]] = []

    def fake_run_once(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"status": "pass", "bar_end": kwargs["bar_end"]}

    result = run_current_delayed_minute_paper(
        state_root=tmp_path,
        token_file=Path("/run/private/token"),
        now=_at("2026-07-28T13:57:00"),
        run_once=fake_run_once,
    )

    assert result == {
        "status": "pass",
        "bar_end": "2026-07-28 13:50:00",
        "gap_recovery": True,
        "gap_slots": ["2026-07-28 13:40:00", "2026-07-28 13:45:00"],
        "full_session_complete": False,
        "learning_eligible": False,
        "gap_recovery_reason": "minute_session_gap_detected",
    }
    assert len(calls) == 1
    assert calls[0]["gap_recovery"] == {
        "reason_code": "minute_session_gap_detected",
        "skipped_session_slots": ("2026-07-28 13:40:00", "2026-07-28 13:45:00"),
    }


def test_current_bar_delegates_exactly_once(tmp_path: Path) -> None:
    day = _initialized_day(tmp_path, last_bar="2026-07-28 13:45:00")
    calls: list[dict[str, object]] = []

    def fake_run_once(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"status": "pass", "bar_end": kwargs["bar_end"]}

    result = run_current_delayed_minute_paper(
        state_root=tmp_path,
        token_file=Path("/run/private/token"),
        now=_at("2026-07-28T13:57:00"),
        run_once=fake_run_once,
    )

    assert result == {"status": "pass", "bar_end": "2026-07-28 13:50:00"}
    assert len(calls) == 1
    assert calls[0]["state_bundle"] == day / "state-bundle.json"
    assert calls[0]["decision_time"] == _at("2026-07-28T13:57:00")
    assert calls[0]["trading_date"].isoformat() == "2026-07-28"
    assert (day / ".minute-auto.lock").stat().st_mode & 0o777 == 0o600


def test_decision_time_is_window_end_for_collector_commit(
    tmp_path: Path,
) -> None:
    """The delayed tier decides at the availability-window end."""

    _initialized_day(tmp_path, last_bar="2026-07-28 13:45:00")
    calls: list[dict[str, object]] = []

    def fake_run_once(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"status": "pass", "bar_end": kwargs["bar_end"]}

    run_current_delayed_minute_paper(
        state_root=tmp_path,
        token_file=Path("/run/private/token"),
        now=_at("2026-07-28T13:57:10"),
        run_once=fake_run_once,
    )

    assert len(calls) == 1
    assert calls[0]["bar_end"] == "2026-07-28 13:50:00"
    assert calls[0]["decision_time"] == _at("2026-07-28T13:57:00")


def test_readiness_failure_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A collector-latency readiness failure is retried with a refreshed
    decision time until the bar data becomes available."""

    _initialized_day(tmp_path, last_bar="2026-07-28 13:45:00")
    calls: list[dict[str, object]] = []

    def fake_run_once(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        if len(calls) == 1:
            raise MinuteDataContractError("minute_metadata_not_ready")
        return {"status": "pass", "bar_end": kwargs["bar_end"]}

    class _FixedNow(datetime):
        @staticmethod
        def now(tz: object = SHANGHAI) -> datetime:
            return _at("2026-07-28T13:57:40")

    monkeypatch.setattr(auto_runner_module, "datetime", _FixedNow)
    monkeypatch.setattr(
        auto_runner_module.time_module, "sleep", lambda _seconds: None
    )

    result = run_current_delayed_minute_paper(
        state_root=tmp_path,
        token_file=Path("/run/private/token"),
        now=_at("2026-07-28T13:57:10"),
        run_once=fake_run_once,
    )

    assert result == {"status": "pass", "bar_end": "2026-07-28 13:50:00"}
    assert len(calls) == 2
    assert calls[1]["decision_time"] == _at("2026-07-28T13:57:40")


def test_readiness_failure_exhausts_bounded_retries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A persistently-not-ready bar fails closed after the bounded retry
    budget instead of retrying forever."""

    _initialized_day(tmp_path, last_bar="2026-07-28 13:45:00")
    calls: list[dict[str, object]] = []

    def fake_run_once(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        raise MinuteDataContractError("minute_evidence_time_order_invalid")

    class _FixedNow(datetime):
        @staticmethod
        def now(tz: object = SHANGHAI) -> datetime:
            return _at("2026-07-28T13:57:40")

    monkeypatch.setattr(auto_runner_module, "datetime", _FixedNow)
    monkeypatch.setattr(
        auto_runner_module.time_module, "sleep", lambda _seconds: None
    )

    with pytest.raises(MinuteDataContractError):
        run_current_delayed_minute_paper(
        state_root=tmp_path,
        token_file=Path("/run/private/token"),
        now=_at("2026-07-28T13:57:10"),
        run_once=fake_run_once,
        )

    assert len(calls) == auto_runner_module.READINESS_RETRY_LIMIT


def test_non_readiness_failure_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A genuine data-gap failure (snapshot incomplete) fails immediately."""

    _initialized_day(tmp_path, last_bar="2026-07-28 13:45:00")
    calls: list[dict[str, object]] = []

    def fake_run_once(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        raise MinuteDataContractError("minute_paper_snapshot_universe_incomplete")

    monkeypatch.setattr(
        auto_runner_module.time_module, "sleep", lambda _seconds: None
    )

    with pytest.raises(MinuteDataContractError):
        run_current_delayed_minute_paper(
        state_root=tmp_path,
        token_file=Path("/run/private/token"),
        now=_at("2026-07-28T13:57:10"),
        run_once=fake_run_once,
        )

    assert len(calls) == 1


def test_partial_mode_contains_unsafe_data_failure_to_one_cohort(
    tmp_path: Path,
) -> None:
    _initialized_day(tmp_path, last_bar="2026-07-28 13:45:00")

    def fail(**_kwargs: object) -> dict[str, object]:
        raise MinuteDataContractError("minute_query_identity_mismatch")

    result = run_current_delayed_minute_paper(
        state_root=tmp_path,
        token_file=Path("/run/private/token"),
        now=_at("2026-07-28T13:57:10"),
        run_once=fail,
        partial_observation_minimum=495,
    )

    assert result["status"] == "partial_observation_failed_closed"
    assert result["reason_code"] == "minute_query_identity_mismatch"
    assert result["bar_end"] == "2026-07-28 13:50:00"
    assert result["execution_eligible"] is False


def test_first_bar_can_initialize_but_midday_cannot(tmp_path: Path) -> None:
    _initialized_day(tmp_path, last_bar=None)

    with pytest.raises(MinuteAutoRunnerError, match="minute_auto_initial_bar_missing"):
        run_current_delayed_minute_paper(
            state_root=tmp_path,
            token_file=Path("/run/private/token"),
            now=_at("2026-07-28T13:12:00"),
            run_once=lambda **_: {"status": "pass"},
        )


def test_manual_late_start_is_explicit_and_never_learning_eligible(
    tmp_path: Path,
) -> None:
    day = _initialized_day(tmp_path, last_bar=None)
    calls: list[dict[str, object]] = []

    def fake_run_once(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"status": "pass", "bar_end": kwargs["bar_end"]}

    result = run_current_delayed_minute_paper(
        state_root=tmp_path,
        token_file=Path("/run/private/token"),
        now=_at("2026-07-28T10:17:00"),
        run_once=fake_run_once,
        allow_late_start=True,
    )

    assert result == {
        "status": "pass",
        "bar_end": "2026-07-28 10:10:00",
        "gap_recovery": True,
        "gap_slots": [
            "2026-07-28 09:35:00",
            "2026-07-28 09:40:00",
            "2026-07-28 09:45:00",
            "2026-07-28 09:50:00",
            "2026-07-28 09:55:00",
            "2026-07-28 10:00:00",
            "2026-07-28 10:05:00",
        ],
        "gap_recovery_reason": "incident_recovery_no_historical_pit",
        "late_start": True,
        "skipped_session_slots": 7,
        "full_session_complete": False,
        "learning_eligible": False,
        "late_start_reason": "incident_recovery_no_historical_pit",
    }
    assert len(calls) == 1
    assert calls[0]["state_bundle"] == day / "state-bundle.json"
    assert calls[0]["decision_time"] == _at("2026-07-28T10:17:00")
    assert calls[0]["gap_recovery"] == {
        "reason_code": "incident_recovery_no_historical_pit",
        "skipped_session_slots": (
            "2026-07-28 09:35:00",
            "2026-07-28 09:40:00",
            "2026-07-28 09:45:00",
            "2026-07-28 09:50:00",
            "2026-07-28 09:55:00",
            "2026-07-28 10:00:00",
            "2026-07-28 10:05:00",
        ),
    }


def test_scheduled_late_start_recovers_after_opening_read_failure(tmp_path: Path) -> None:
    day = _initialized_day(tmp_path, last_bar=None)

    def fail(**_: object) -> dict[str, object]:
        raise MinuteDataContractError("minute_same_observation_mismatch")

    with pytest.raises(MinuteDataContractError, match="minute_same_observation_mismatch"):
        run_current_delayed_minute_paper(
            state_root=tmp_path, token_file=Path("/run/private/token"),
            now=_at("2026-07-28T09:42:00"), run_once=fail, allow_late_start=True,
        )
    assert not (day / "state-bundle.json").exists()
    calls = []

    def recover(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"status": "pass", "bar_end": kwargs["bar_end"]}

    result = run_current_delayed_minute_paper(
        state_root=tmp_path, token_file=Path("/run/private/token"),
        now=_at("2026-07-28T09:47:00"), run_once=recover, allow_late_start=True,
    )
    assert result["bar_end"] == "2026-07-28 09:40:00"
    assert result["gap_slots"] == ["2026-07-28 09:35:00"]
    assert result["learning_eligible"] is False
    assert result["full_session_complete"] is False
    assert len(calls) == 1


def test_real_trading_flag_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "true")

    with pytest.raises(MinuteAutoRunnerError, match="real_trading"):
        run_current_delayed_minute_paper(
            state_root=tmp_path,
            token_file=Path("/run/private/token"),
            now=_at("2026-07-28T09:42:10"),
        )


def test_minute_timer_has_exactly_the_48_delayed_session_triggers() -> None:
    timer = (
        REPO_ROOT / "deploy/systemd/tradingagent-ashare-minute-paper.timer"
    ).read_text(encoding="utf-8")

    calendar_lines = tuple(
        line for line in timer.splitlines() if line.startswith("OnCalendar=")
    )

    slots = session_bar_ends(_at("2026-07-28T10:00:00").date())
    expected_calendar = tuple(
        "OnCalendar=Mon..Fri *-*-* "
        f"{(slot + timedelta(minutes=7)).strftime('%H:%M:%S')}"
        for slot in slots
    )
    assert calendar_lines == expected_calendar
    assert "09..11" not in timer
    assert "13..15" not in timer
    assert "Persistent=false" in timer
    assert "Unit=tradingagent-ashare-minute-paper.service" in timer
    triggers = tuple(
        slot + timedelta(minutes=7) for slot in slots
    )
    assert len(triggers) == 48
    for trigger, slot in zip(triggers, slots, strict=True):
        assert expected_available_bar_end(trigger) == slot
        assert expected_available_bar_end(trigger + timedelta(seconds=10)) == slot
