"""A-share T+1 settlement constraint helpers."""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Sequence

SHAREDSIGNALS_REFERENCE = Path("/opt/investment/SharedSignals/reference")
if SHAREDSIGNALS_REFERENCE.exists():
    ref_path = str(SHAREDSIGNALS_REFERENCE)
    if ref_path not in sys.path:
        sys.path.insert(0, ref_path)

try:
    from market_calendar import get_next_trading_day as _calendar_next_trading_day
except Exception:  # pragma: no cover - runtime fallback path
    _calendar_next_trading_day = None


def _to_date(d: date | datetime | str) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    if isinstance(d, str):
        cleaned = d.replace("-", "").replace("/", "")
        return datetime.strptime(cleaned, "%Y%m%d").date()
    raise TypeError(f"Unsupported date type: {type(d)}")


def _next_weekday(d: date) -> date:
    candidate = d + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def next_sellable_date(position_open_date: date | datetime | str) -> date:
    """Return the earliest sellable date under T+1."""
    open_d = _to_date(position_open_date)
    if _calendar_next_trading_day is not None:
        try:
            next_day = _calendar_next_trading_day(open_d)
            if next_day is not None:
                return next_day
        except Exception:
            pass
    return _next_weekday(open_d)


def can_sell(
    position_open_date: date | datetime | str,
    current_date: date | datetime | str,
) -> bool:
    open_d = _to_date(position_open_date)
    curr_d = _to_date(current_date)
    return curr_d >= next_sellable_date(open_d)


def filter_sellable(
    positions: Sequence[dict],
    current_date: date | datetime | str,
    date_field: str = "open_date",
) -> list[dict]:
    curr_d = _to_date(current_date)
    result = []
    for pos in positions:
        open_val = pos.get(date_field)
        if open_val is None:
            continue
        try:
            if can_sell(open_val, curr_d):
                result.append(pos)
        except (TypeError, ValueError):
            continue
    return result
