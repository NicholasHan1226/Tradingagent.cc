"""Minimal current-bar scheduler for delayed A-share paper research.

This module does not discover a provider, broker, or trading account.  It only
selects the one five-minute bar that should already be available from the
frozen TradingDatas delayed-paper contract, verifies continuity with the
existing fixture bundle, and delegates to ``minute_paper_runner``.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
import fcntl
import json
import os
from pathlib import Path
import sys
from typing import Callable, Iterator, Mapping

from .minute_data import (
    MAX_DELAYED_PAPER_LATENCY,
    SHANGHAI,
    MinuteDataContractError,
)
from .minute_paper_runner import MinutePaperRunnerError, run_delayed_minute_paper_once


FIVE_MINUTES = timedelta(minutes=5)
# A delayed decision may consume exactly one completed cadence plus the shared
# jitter budget. This remains observation-only and never changes the 30-second
# low-latency execution gate in ``minute_data``.
PROVIDER_AVAILABILITY_LAG = MAX_DELAYED_PAPER_LATENCY
STATE_BUNDLE_NAME = "state-bundle.json"
MANIFEST_NAME = "minute-manifest.json"
REFERENCE_FACTS_NAME = "reference-facts.json"
UNIVERSE_NAME = "universe.json"


class MinuteAutoRunnerError(ValueError):
    """Fail-closed automatic delayed-paper configuration or continuity error."""


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MinuteAutoRunnerError("minute_auto_now_must_be_timezone_aware")
    return value


def session_bar_ends(trading_date: date) -> tuple[datetime, ...]:
    """Return the 48 completed five-minute bar ends for one A-share session."""

    morning_start = datetime.combine(trading_date, time(9, 35), tzinfo=SHANGHAI)
    morning_end = datetime.combine(trading_date, time(11, 30), tzinfo=SHANGHAI)
    afternoon_start = datetime.combine(trading_date, time(13, 5), tzinfo=SHANGHAI)
    afternoon_end = datetime.combine(trading_date, time(15, 0), tzinfo=SHANGHAI)
    values: list[datetime] = []
    current = morning_start
    while current <= morning_end:
        values.append(current)
        current += FIVE_MINUTES
    current = afternoon_start
    while current <= afternoon_end:
        values.append(current)
        current += FIVE_MINUTES
    return tuple(values)


def expected_available_bar_end(now: datetime) -> datetime | None:
    """Select only the immediately eligible delayed-observation bar.

    A bar may be used after one completed five-minute cadence and no later than
    the shared 30-second jitter allowance.  Returning an older bar would make
    the decision stale even when its original receipt was timely.
    """

    local = _aware(now).astimezone(SHANGHAI)
    slots = session_bar_ends(local.date())
    eligible = [
        value
        for value in slots
        if FIVE_MINUTES <= local - value <= PROVIDER_AVAILABILITY_LAG
    ]
    return max(eligible, default=None)


def _load_mapping(path: Path, reason: str) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MinuteAutoRunnerError(reason) from exc
    if not isinstance(value, Mapping):
        raise MinuteAutoRunnerError(reason)
    return value


def _last_processed_bar_end(state_bundle: Path) -> datetime | None:
    if not state_bundle.exists():
        return None
    raw = _load_mapping(state_bundle, "minute_auto_state_invalid")
    receipt = raw.get("last_receipt")
    if not isinstance(receipt, Mapping):
        raise MinuteAutoRunnerError("minute_auto_state_invalid")
    raw_bar_end = receipt.get("bar_end")
    if not isinstance(raw_bar_end, str):
        raise MinuteAutoRunnerError("minute_auto_state_invalid")
    try:
        parsed = datetime.strptime(raw_bar_end, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise MinuteAutoRunnerError("minute_auto_state_invalid") from exc
    return parsed.replace(tzinfo=SHANGHAI)


def _assert_continuity(
    *,
    target: datetime,
    last_processed: datetime | None,
) -> None:
    slots = session_bar_ends(target.astimezone(SHANGHAI).date())
    if target not in slots:
        raise MinuteAutoRunnerError("minute_auto_target_not_session_bar")
    target_index = slots.index(target)
    if last_processed is None:
        if target_index != 0:
            raise MinuteAutoRunnerError("minute_auto_initial_bar_missing")
        return
    local_last = last_processed.astimezone(SHANGHAI)
    if local_last.date() != target.date():
        raise MinuteAutoRunnerError("minute_auto_state_session_mismatch")
    if local_last not in slots:
        raise MinuteAutoRunnerError("minute_auto_state_bar_invalid")
    if slots.index(local_last) + 1 != target_index:
        raise MinuteAutoRunnerError("minute_auto_bar_gap_detected")


def _skipped_session_slots(
    *,
    target: datetime,
    last_processed: datetime,
) -> tuple[str, ...]:
    slots = session_bar_ends(target.astimezone(SHANGHAI).date())
    local_last = last_processed.astimezone(SHANGHAI)
    if (
        target not in slots
        or local_last.date() != target.date()
        or local_last not in slots
    ):
        _assert_continuity(target=target, last_processed=last_processed)
        raise MinuteAutoRunnerError("minute_auto_gap_state_invalid")
    last_index = slots.index(local_last)
    target_index = slots.index(target)
    if target_index <= last_index:
        raise MinuteAutoRunnerError("minute_auto_gap_state_invalid")
    return tuple(
        value.strftime("%Y-%m-%d %H:%M:%S")
        for value in slots[last_index + 1 : target_index]
    )


@contextmanager
def _exclusive_lock(day_root: Path) -> Iterator[None]:
    lock_path = day_root / ".minute-auto.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MinuteAutoRunnerError("minute_auto_already_running") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def run_current_delayed_minute_paper(
    *,
    state_root: Path | str,
    token_file: Path | str,
    now: datetime,
    run_once: Callable[..., dict[str, object]] = run_delayed_minute_paper_once,
    allow_late_start: bool = False,
) -> dict[str, object]:
    """Process exactly one expected current delayed bar or return a safe no-op."""

    if os.environ.get("REAL_TRADING_ENABLED", "false").strip().lower() != "false":
        raise MinuteAutoRunnerError("real_trading_must_remain_disabled")
    root = Path(state_root)
    token = Path(token_file)
    if not root.is_absolute() or not token.is_absolute():
        raise MinuteAutoRunnerError("minute_auto_paths_must_be_absolute")
    decision_time = _aware(now).astimezone(SHANGHAI)
    target = expected_available_bar_end(decision_time)
    if target is None:
        return {
            "status": "noop",
            "reason": "outside_delayed_session_window",
            "real_trading_enabled": False,
        }
    day_root = root / target.strftime("%Y%m%d")
    if not day_root.is_dir():
        return {
            "status": "noop",
            "reason": "session_not_initialized",
            "trading_date": target.date().isoformat(),
            "real_trading_enabled": False,
        }
    with _exclusive_lock(day_root):
        state_bundle = day_root / STATE_BUNDLE_NAME
        last_processed = _last_processed_bar_end(state_bundle)
        if last_processed is not None and last_processed >= target:
            return {
                "status": "noop",
                "reason": "bar_already_processed",
                "bar_end": target.strftime("%Y-%m-%d %H:%M:%S"),
                "real_trading_enabled": False,
            }
        slots = session_bar_ends(target.date())
        target_index = slots.index(target)
        late_start = last_processed is None and target_index > 0 and allow_late_start
        skipped_slots: tuple[str, ...] = ()
        if last_processed is not None:
            skipped_slots = _skipped_session_slots(
                target=target,
                last_processed=last_processed,
            )
        elif late_start:
            skipped_slots = tuple(
                value.strftime("%Y-%m-%d %H:%M:%S") for value in slots[:target_index]
            )
        else:
            _assert_continuity(target=target, last_processed=last_processed)
        required = {
            "manifest": day_root / MANIFEST_NAME,
            "reference_facts_path": day_root / REFERENCE_FACTS_NAME,
            "universe_path": day_root / UNIVERSE_NAME,
        }
        if any(not path.is_file() for path in required.values()):
            raise MinuteAutoRunnerError("minute_auto_session_inputs_missing")
        run_kwargs: dict[str, object] = {
            **required,
            "token_file": token,
            "state_bundle": state_bundle,
            "decision_time": decision_time,
            "trading_date": target.date(),
            "bar_end": target.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if skipped_slots:
            recovery_reason = (
                "incident_recovery_no_historical_pit"
                if late_start
                else "minute_session_gap_detected"
            )
            run_kwargs["gap_recovery"] = {
                "reason_code": recovery_reason,
                "skipped_session_slots": skipped_slots,
            }
        # Decide at the end of the bar's availability window so the TD
        # collector's commit watermark (bar end + ~5m + collection latency) is
        # already observed before the decision; wall-now at timer fire is too
        # early and fails the PIT ordering check.
        run_kwargs["decision_time"] = target + timedelta(minutes=5, seconds=30)
        receipt = run_once(
            **run_kwargs,
        )
        result = dict(receipt)
        if skipped_slots:
            result.update(
                {
                    "gap_recovery": True,
                    "gap_slots": list(skipped_slots),
                    "full_session_complete": False,
                    "learning_eligible": False,
                    "gap_recovery_reason": recovery_reason,
                }
            )
        if late_start:
            result.update(
                {
                    "late_start": True,
                    "skipped_session_slots": target_index,
                    "full_session_complete": False,
                    "learning_eligible": False,
                    "late_start_reason": "incident_recovery_no_historical_pit",
                }
            )
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one current delayed A-share five-minute paper step"
    )
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument(
        "--now",
        help="Explicit aware ISO timestamp for controlled replay; omit in service",
    )
    parser.add_argument(
        "--allow-late-start",
        action="store_true",
        help=(
            "Manually start an initialized sim-only day after 09:35 without "
            "claiming full-session or learning eligibility"
        ),
    )
    args = parser.parse_args(argv)
    try:
        now = (
            datetime.now(tz=SHANGHAI)
            if args.now is None
            else datetime.fromisoformat(args.now)
        )
        receipt = run_current_delayed_minute_paper(
            state_root=args.state_root,
            token_file=args.token_file,
            now=now,
            allow_late_start=args.allow_late_start,
        )
    except (MinuteAutoRunnerError, OSError, ValueError) as exc:
        print("automatic delayed minute paper runner failed closed", file=sys.stderr)
        print(
            json.dumps(
                {
                    "contract": "tradingagent.ashare.minute_auto_runner_failure.v1",
                    "status": "failed_closed",
                    "failure_type": type(exc).__name__,
                    "failure_reason": (
                        str(exc)
                        if isinstance(
                            exc,
                            (
                                MinuteAutoRunnerError,
                                MinutePaperRunnerError,
                                MinuteDataContractError,
                            ),
                        )
                        else type(exc).__name__.lower()
                    ),
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MinuteAutoRunnerError",
    "expected_available_bar_end",
    "main",
    "run_current_delayed_minute_paper",
    "session_bar_ends",
]
