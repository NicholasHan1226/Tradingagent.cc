"""A-share T+1 settlement constraint helpers."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Sequence

try:
    from shared.data import reader as shared_data_reader
except Exception:  # pragma: no cover - optional SharedSignals integration
    shared_data_reader = None  # type: ignore[assignment]

TRADE_CALENDAR_SEARCH_ROOTS = (
    Path("/opt/investment/SharedSignals"),
    Path("/opt/investment/MarketGraph/data"),
)
TRADE_CALENDAR_PATTERNS = (
    "trade_cal.csv",
    "trade_cal.json",
    "trade_cal.jsonl",
    "trade_cal.txt",
    "*trade_cal*.csv",
    "*trade_cal*.json",
    "*trade_cal*.jsonl",
    "*trade_cal*.txt",
)

# Conservative built-in fallback for major A-share market holidays in 2026.
KNOWN_A_SHARE_HOLIDAYS_2026 = frozenset(
    {
        date(2026, 1, 1),
        date(2026, 1, 2),
        date(2026, 2, 16),
        date(2026, 2, 17),
        date(2026, 2, 18),
        date(2026, 2, 19),
        date(2026, 2, 20),
        date(2026, 2, 23),
        date(2026, 4, 6),
        date(2026, 5, 1),
        date(2026, 5, 4),
        date(2026, 5, 5),
        date(2026, 6, 19),
        date(2026, 9, 25),
        date(2026, 10, 1),
        date(2026, 10, 2),
        date(2026, 10, 5),
        date(2026, 10, 6),
        date(2026, 10, 7),
    }
)


@dataclass(frozen=True)
class _TradeCalendarData:
    source_path: Path | None
    open_days: frozenset[date]
    coverage_start: date | None
    coverage_end: date | None

    def covers(self, trading_day: date) -> bool:
        if self.coverage_start is None or self.coverage_end is None:
            return False
        return self.coverage_start <= trading_day <= self.coverage_end


def _to_date(d: date | datetime | str) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    if isinstance(d, str):
        value = d.strip()
        if not value:
            raise ValueError("Unsupported date string: empty value")
        if value.isdigit() and len(value) == 8:
            return datetime.strptime(value, "%Y%m%d").date()
        for parser in (date.fromisoformat,):
            try:
                return parser(value)
            except ValueError:
                continue
        iso_candidate = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(iso_candidate).date()
        except ValueError as exc:
            raise ValueError(
                "Unsupported date string format: "
                f"{d!r}. Expected YYYYMMDD, YYYY-MM-DD, or ISO timestamp."
            ) from exc
    raise TypeError(
        "Unsupported date type: "
        f"{type(d)!r}. Expected date, datetime, or str."
    )


def _iter_trade_calendar_paths() -> Iterable[Path]:
    yielded: set[Path] = set()
    for root in TRADE_CALENDAR_SEARCH_ROOTS:
        if not root.exists():
            continue
        for pattern in TRADE_CALENDAR_PATTERNS:
            for candidate in sorted(root.rglob(pattern)):
                resolved = candidate.resolve()
                if resolved in yielded or not resolved.is_file():
                    continue
                yielded.add(resolved)
                yield resolved


def _parse_is_open(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "t", "y", "yes", "open"}:
            return True
        if normalized in {"0", "false", "f", "n", "no", "closed"}:
            return False
    raise ValueError(f"Unsupported trade calendar is_open value: {value!r}")


def _parse_trade_calendar_record(record: object) -> tuple[date, bool] | None:
    if isinstance(record, str):
        stripped = record.strip()
        if not stripped:
            return None
        return _to_date(stripped), True

    if not isinstance(record, dict):
        return None

    raw_date = (
        record.get("cal_date")
        or record.get("trade_date")
        or record.get("date")
        or record.get("calendar_date")
    )
    if raw_date is None:
        return None

    raw_is_open = record.get("is_open")
    if raw_is_open is None:
        raw_is_open = record.get("open")
    if raw_is_open is None:
        raw_is_open = record.get("is_trading_day")
    if raw_is_open is None:
        raw_is_open = record.get("trading")

    return _to_date(raw_date), _parse_is_open(raw_is_open)


def _load_trade_calendar_records(path: Path) -> tuple[set[date], set[date]]:
    if path.suffix == ".csv":
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = csv.DictReader(handle)
            return _collect_calendar_days(rows)

    if path.suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload = (
                payload.get("data")
                or payload.get("items")
                or payload.get("trade_cal")
                or payload.get("calendar")
                or []
            )
        if not isinstance(payload, list):
            raise ValueError(f"Unsupported trade calendar JSON payload in {path}")
        return _collect_calendar_days(payload)

    if path.suffix == ".jsonl":
        with path.open("r", encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
        return _collect_calendar_days(rows)

    with path.open("r", encoding="utf-8") as handle:
        return _collect_calendar_days(handle)


def _collect_calendar_days(
    rows: Iterable[object],
) -> tuple[set[date], set[date]]:
    seen_days: set[date] = set()
    open_days: set[date] = set()
    for row in rows:
        parsed = _parse_trade_calendar_record(row)
        if parsed is None:
            continue
        trading_day, is_open = parsed
        seen_days.add(trading_day)
        if is_open:
            open_days.add(trading_day)
    return seen_days, open_days


@lru_cache(maxsize=1)
def _load_trade_calendar_data() -> _TradeCalendarData:
    for candidate in _iter_trade_calendar_paths():
        try:
            seen_days, open_days = _load_trade_calendar_records(candidate)
        except Exception:
            continue
        if not seen_days and not open_days:
            continue
        coverage_days = seen_days or open_days
        return _TradeCalendarData(
            source_path=candidate,
            open_days=frozenset(open_days),
            coverage_start=min(coverage_days),
            coverage_end=max(coverage_days),
        )
    return _TradeCalendarData(
        source_path=None,
        open_days=frozenset(),
        coverage_start=None,
        coverage_end=None,
    )


def _fallback_is_trading_day(trading_day: date) -> bool:
    return (
        trading_day.weekday() < 5
        and trading_day not in KNOWN_A_SHARE_HOLIDAYS_2026
    )


def _load_shared_calendar_module():
    if shared_data_reader is None:
        return None
    try:
        return shared_data_reader._import_shared_calendar()
    except Exception:
        return None


def _shared_calendar_is_trading_day(trading_day: date) -> bool | None:
    module = _load_shared_calendar_module()
    if module is None or not hasattr(module, "is_trading_day"):
        return None
    try:
        return bool(module.is_trading_day(trading_day))
    except Exception:
        return None


def _shared_calendar_trading_days(start_d: date, end_d: date) -> list[date] | None:
    module = _load_shared_calendar_module()
    if module is None:
        return None
    try:
        if hasattr(module, "get_trading_days"):
            return [_to_date(day) for day in module.get_trading_days(start_d, end_d)]
        if hasattr(module, "get_trading_calendar"):
            return [_to_date(day) for day in module.get_trading_calendar(start_d, end_d)]
    except Exception:
        return None
    return None


def _shared_calendar_next_trading_day(trading_day: date) -> date | None:
    module = _load_shared_calendar_module()
    if module is None:
        return None
    try:
        if hasattr(module, "get_next_trading_day"):
            next_day = module.get_next_trading_day(trading_day)
            return _to_date(next_day) if next_day is not None else None
        if hasattr(module, "next_trading_day"):
            return _to_date(module.next_trading_day(trading_day))
    except Exception:
        return None
    return None


def get_trading_calendar(
    start: date | datetime | str,
    end: date | datetime | str,
) -> list[date]:
    start_d = _to_date(start)
    end_d = _to_date(end)
    if start_d > end_d:
        raise ValueError(
            f"Invalid calendar range: start {start_d.isoformat()} is after end {end_d.isoformat()}"
        )

    shared_days = _shared_calendar_trading_days(start_d, end_d)
    if shared_days is not None:
        return shared_days

    trading_days: list[date] = []
    current = start_d
    while current <= end_d:
        if is_trading_day(current):
            trading_days.append(current)
        current += timedelta(days=1)
    return trading_days


def is_trading_day(d: date | datetime | str) -> bool:
    trading_day = _to_date(d)
    shared_result = _shared_calendar_is_trading_day(trading_day)
    if shared_result is not None:
        return shared_result
    calendar = _load_trade_calendar_data()
    if calendar.covers(trading_day):
        return trading_day in calendar.open_days
    return _fallback_is_trading_day(trading_day)


def next_trading_day(d: date | datetime | str) -> date:
    trading_day = _to_date(d)
    shared_next = _shared_calendar_next_trading_day(trading_day)
    if shared_next is not None:
        return shared_next
    current = trading_day + timedelta(days=1)
    while not is_trading_day(current):
        current += timedelta(days=1)
    return current


def next_sellable_date(position_open_date: date | datetime | str) -> date:
    """Return the earliest sellable date under T+1."""
    return next_trading_day(position_open_date)


def can_sell(
    position_open_date: date | datetime | str | None,
    current_date: date | datetime | str,
) -> bool:
    if position_open_date is None:
        return False
    try:
        open_d = _to_date(position_open_date)
    except (TypeError, ValueError):
        return False
    curr_d = _to_date(current_date)
    return curr_d >= next_trading_day(open_d)


def filter_sellable(
    positions: Sequence[dict],
    current_date: date | datetime | str,
    date_field: str = "open_date",
) -> list[dict]:
    curr_d = _to_date(current_date)
    result = []
    for pos in positions:
        if can_sell(pos.get(date_field), curr_d):
            result.append(pos)
    return result
