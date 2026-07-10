#!/usr/bin/env python3
"""China futures session helpers for read-only validation and simulation health."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

CN_TZ = timezone(timedelta(hours=8))


def _minutes(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def parse_cn_datetime(value: Any) -> datetime | None:
    """Parse an exchange timestamp, treating naive values as China local time."""

    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def session_bar_age_minutes(bar_time: Any, now: datetime | None = None) -> float | None:
    """Return bar age in minutes using China exchange-time semantics."""

    bar_dt = parse_cn_datetime(bar_time)
    now_dt = parse_cn_datetime(now or datetime.now(timezone.utc))
    if bar_dt is None or now_dt is None:
        return None
    return (now_dt - bar_dt).total_seconds() / 60.0


def is_current_session_bar(
    bar_time: Any,
    *,
    session_start: Any = None,
    now: datetime | None = None,
    max_age_minutes: float = 10.0,
    max_future_minutes: float = 5.0,
) -> bool:
    """Return whether a bar is recent and belongs to the current session."""

    bar_dt = parse_cn_datetime(bar_time)
    now_dt = parse_cn_datetime(now or datetime.now(timezone.utc))
    start_dt = parse_cn_datetime(session_start)
    if bar_dt is None or now_dt is None:
        return False
    if start_dt is not None and bar_dt < start_dt:
        return False
    age = (now_dt - bar_dt).total_seconds() / 60.0
    return -max(0.0, float(max_future_minutes)) <= age <= max(0.0, float(max_age_minutes))


def cn_futures_session_state(now: datetime | None = None) -> dict[str, Any]:
    """Return a consistent China futures session state.

    The helper intentionally keeps product-specific night session differences
    out of the global health gate. Product/style checks still decide whether a
    concrete contract can trade at night.
    """

    raw_now = now or datetime.now(timezone.utc)
    current = raw_now.astimezone(CN_TZ) if raw_now.tzinfo is not None else raw_now.replace(tzinfo=CN_TZ)
    weekday = current.weekday()
    minutes = _minutes(current)
    windows = {
        "day_morning": (9 * 60, 11 * 60 + 30),
        "day_afternoon": (13 * 60, 15 * 60),
        "night": (21 * 60, 23 * 60 + 59),
        "night_early": (0, 2 * 60 + 30),
    }
    session = "closed"
    session_start: datetime | None = None
    in_session = False
    if weekday < 5 and windows["day_morning"][0] <= minutes <= windows["day_morning"][1]:
        session = "day_morning"
        session_start = current.replace(hour=9, minute=0, second=0, microsecond=0)
        in_session = True
    elif weekday < 5 and windows["day_afternoon"][0] <= minutes <= windows["day_afternoon"][1]:
        session = "day_afternoon"
        session_start = current.replace(hour=13, minute=0, second=0, microsecond=0)
        in_session = True
    elif weekday < 5 and windows["night"][0] <= minutes <= windows["night"][1]:
        session = "night"
        session_start = current.replace(hour=21, minute=0, second=0, microsecond=0)
        in_session = True
    elif 1 <= weekday <= 5 and windows["night_early"][0] <= minutes <= windows["night_early"][1]:
        session = "night_early"
        session_start = (current - timedelta(days=1)).replace(hour=21, minute=0, second=0, microsecond=0)
        in_session = True
    elif weekday < 5 and 11 * 60 + 30 < minutes < 13 * 60:
        session = "lunch_break"
    elif weekday < 5 and 15 * 60 < minutes < 21 * 60:
        session = "between_day_and_night"
    elif weekday < 5 and 2 * 60 + 30 < minutes < 9 * 60:
        session = "pre_day"

    samples_expected_today = (
        (weekday < 5 and minutes >= 9 * 60)
        or (1 <= weekday <= 5 and minutes <= 2 * 60 + 30)
    )
    return {
        "timezone": "Asia/Shanghai",
        "local_time": current.isoformat(timespec="seconds"),
        "session": session,
        "session_start": session_start.isoformat(timespec="seconds") if session_start else "",
        "active_trade_date": active_trade_date(current),
        "in_session": in_session,
        "samples_expected_today": samples_expected_today,
    }


def active_trade_date(now: datetime | None = None) -> str:
    """Return the China futures trading date for the current exchange session."""

    raw_now = now or datetime.now(timezone.utc)
    current = raw_now.astimezone(CN_TZ) if raw_now.tzinfo is not None else raw_now.replace(tzinfo=CN_TZ)
    minutes = _minutes(current)
    if current.weekday() < 5 and minutes >= 21 * 60:
        return (current + timedelta(days=1)).strftime("%Y%m%d")
    return current.strftime("%Y%m%d")


__all__ = [
    "active_trade_date",
    "cn_futures_session_state",
    "is_current_session_bar",
    "parse_cn_datetime",
    "session_bar_age_minutes",
]
