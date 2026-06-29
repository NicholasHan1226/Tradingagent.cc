"""A-share T+1 settlement constraint helpers.

A-share market enforces T+1 settlement: shares bought today cannot be sold
until the next trading day. This module provides utilities to check whether
a position is sellable on a given date and to filter a portfolio down to
only T+1-compliant sellable positions.

Usage:
    from Ashare.t_plus_1 import can_sell, filter_sellable

    if can_sell(position_open_date, current_date):
        # safe to issue sell order
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable, Sequence


def _to_date(d: date | datetime | str) -> date:
    """Coerce a date / datetime / YYYYMMDD string into a date object."""
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    if isinstance(d, str):
        # support both "20260629" and "2026-06-29"
        cleaned = d.replace("-", "").replace("/", "")
        return datetime.strptime(cleaned, "%Y%m%d").date()
    raise TypeError(f"Unsupported date type: {type(d)}")


def can_sell(position_open_date: date | datetime | str,
             current_date: date | datetime | str) -> bool:
    """Return True if a position opened on *position_open_date* may be sold
    on *current_date* under A-share T+1 rules.

    A position bought on day T can only be sold on day T+1 or later.
    Same-day sell-back is not allowed.

    Parameters
    ----------
    position_open_date
        The date the position was acquired (buy fill date).
    current_date
        The date on which the sell would be attempted.

    Returns
    -------
    bool
        True if sellable (current_date strictly after open_date), False otherwise.
    """
    open_d = _to_date(position_open_date)
    curr_d = _to_date(current_date)
    return curr_d > open_d


def filter_sellable(
    positions: Sequence[dict],
    current_date: date | datetime | str,
    date_field: str = "open_date",
) -> list[dict]:
    """Filter *positions* to only those that satisfy T+1 (sellable today).

    Parameters
    ----------
    positions
        Iterable of position dicts. Each dict must contain a key matching
        *date_field* (default ``"open_date"``) with the acquisition date.
    current_date
        The date on which sells would be attempted.
    date_field
        Key name in each position dict holding the open/acquisition date.

    Returns
    -------
    list[dict]
        Positions whose open_date is strictly before current_date.
    """
    curr_d = _to_date(current_date)
    result = []
    for pos in positions:
        open_val = pos.get(date_field)
        if open_val is None:
            # If we cannot determine the open date, treat as NOT sellable
            # to be conservative (avoid T+0 violation).
            continue
        try:
            open_d = _to_date(open_val)
        except (TypeError, ValueError):
            continue
        if curr_d > open_d:
            result.append(pos)
    return result
