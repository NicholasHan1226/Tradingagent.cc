"""All-market dividend/split announcement (``dividend``) daily fetcher.

Data expansion bound to the panel-#14 binding (task #42): pre-event
dividend/split announcements as a management-signal conditioning face —
cash dividend as discipline/deep-pocket signal, stock dividend/split as
ex-right dilution of the unlock overhang.  Live contract probes
(2026-08-25): the endpoint accepts a single ``ann_date`` OR ``ts_code``
but SILENTLY IGNORES ``start_date``/``end_date`` ranges (range probe
returned 0 rows against a 129-row single-day baseline), so the sweep is
one call per CALENDAR day — announcements land on non-trading days too.
Rows land one CSV per ann_date under ``<cache>/dividend_daily/``.
Idempotency mirrors the topinst fetcher: existing files are never
re-fetched (safe resume); per-day failures are recorded without
aborting the sweep; an EMPTY response writes no file and is counted in
``empty_days`` so re-runs retry it.  Fail-closed guards: aborts below
``MIN_FREE_BYTES`` free; missing token aborts before any call.
Lifecycle dedup across ``div_proc`` stages (预披露/预案/股东大会通过/
实施) is deliberately NOT done here — it is a frozen-preregistration
concern; this fetcher lands raw rows only.  research_only /
not_promotion_evidence.

Usage::

    python3 Ashare/event_dividend_fetch.py [--cache DIR]
        [--start YYYYMMDD] [--end YYYYMMDD] [--delay SECONDS]
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Sweep opens before the signal-stream origin (20180102) by more than
# one 30-natural-day conditioning window so earliest entries see full
# announcement coverage.
DEFAULT_START = "20171101"
DEFAULT_END = "20260825"
DIVIDEND_DIRNAME = "dividend_daily"
MIN_FREE_BYTES = 2 * 1024**3  # abort below 2 GiB free on the cache volume
FIELDS = [
    "ts_code",
    "end_date",
    "ann_date",
    "div_proc",
    "stk_div",
    "stk_bo_rate",
    "stk_co_rate",
    "cash_div",
    "cash_div_tax",
    "record_date",
    "ex_date",
    "pay_date",
    "div_listdate",
    "imp_ann_date",
]


class DividendFetchError(RuntimeError):
    """Fail-closed fetch failure with a stable reason code."""


def calendar_days(start: str, end: str) -> list[str]:
    """Every CALENDAR day in [start, end] ascending as YYYYMMDD strings.

    Announcement dates are not bounded by the trading calendar, so the
    sweep axis is plain natural days (no index series dependency).
    """
    if len(start) != 8 or len(end) != 8 or start > end:
        raise DividendFetchError("bad_range")
    try:
        first = date(int(start[:4]), int(start[4:6]), int(start[6:8]))
        last = date(int(end[:4]), int(end[4:6]), int(end[6:8]))
    except ValueError as exc:
        raise DividendFetchError(f"bad_date:{exc}") from exc
    days: list[str] = []
    cursor = first
    while cursor <= last:
        days.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return days


def _token() -> str:
    import os

    token = os.environ.get("TUSHARE_MCP_TOKEN", "").strip().strip('"').strip("'")
    if not token:
        raise DividendFetchError("token_missing")
    return token


def fetch_dividends(
    cache: Path,
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    delay: float = 0.12,
    call=None,
    days: list[str] | None = None,
) -> dict[str, object]:
    """Fetch dividend announcements day by day into per-ann_date files.

    ``call(ann_date)`` returns the raw day payload as a list of row
    dicts (tests inject fakes; production wraps the raw HTTP endpoint).
    ``days`` overrides the generated calendar (tests); production sweeps
    every natural day in [start, end].
    """
    folder = cache / DIVIDEND_DIRNAME
    if shutil.disk_usage(cache).free < MIN_FREE_BYTES:
        raise DividendFetchError("disk_full_guard")
    folder.mkdir(parents=True, exist_ok=True)

    sessions = list(days) if days is not None else calendar_days(start, end)

    stats: dict[str, object] = {
        "days": len(sessions),
        "files_written": 0,
        "files_skipped": 0,
        "rows_seen": 0,
        "bad_rows": 0,
        "empty_days": 0,
        "errors": [],
    }
    if call is None:
        _token()

        def call(ann_date: str):  # noqa: E306
            from Ashare.event_calendar_fetch import call_api

            fields, rows = call_api("dividend", {"ann_date": ann_date})
            return [dict(zip(fields, row)) for row in rows]

    for day in sessions:
        path = folder / f"{day}.csv"
        if path.exists():
            # A completed day is immutable cache evidence.  Do not consume a
            # provider call merely to rediscover its row count.
            stats["files_skipped"] = int(stats["files_skipped"]) + 1
            continue
        try:
            rows = call(day)
        except Exception as exc:  # noqa: BLE001 - recorded, sweep continues
            stats["errors"].append(f"{day}: {exc}")
            time.sleep(max(delay, 0.1))
            continue
        time.sleep(max(delay, 0.0))
        kept: list[dict] = []
        for row in rows:
            stats["rows_seen"] = int(stats["rows_seen"]) + 1
            ts_code = str(row.get("ts_code") or "")
            end_date = str(row.get("end_date") or "")
            # A row whose ann_date disagrees with the queried day breaks
            # the per-day shard invariant (holdertrade mixed-ann_date
            # lesson) — count and drop.
            if not ts_code or not end_date or str(row.get("ann_date") or "") != day:
                stats["bad_rows"] = int(stats["bad_rows"]) + 1
                continue
            kept.append(row)
        if not kept:
            # Calendar day with nothing announced: normal for sparse days
            # (most weekends) — write nothing so re-runs retry the day.
            stats["empty_days"] = int(stats["empty_days"]) + 1
            continue
        _write_csv(path, kept)
        stats["files_written"] = int(stats["files_written"]) + 1
    return stats


def _write_csv(path: Path, rows: list[dict]) -> None:
    handle = tempfile.NamedTemporaryFile(
        "w", newline="", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", suffix=".partial", delete=False,
    )
    try:
        with handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--delay", type=float, default=0.12)
    args = parser.parse_args()
    from Ashare.event_calendar_fetch import CACHE_DIR

    cache = args.cache if args.cache is not None else CACHE_DIR
    stats = fetch_dividends(cache, start=args.start, end=args.end, delay=args.delay)
    print(
        f"dividend fetch: days={stats['days']} "
        f"written={stats['files_written']} skipped={stats['files_skipped']} "
        f"rows={stats['rows_seen']} bad={stats['bad_rows']} "
        f"empty_days={stats['empty_days']} "
        f"errors={len(stats['errors'])}"  # type: ignore[arg-type]
    )
    for err in stats["errors"]:  # type: ignore[union-attr]
        print(f"  ERROR {err}", file=sys.stderr)
    return 1 if stats["errors"] else 0  # type: ignore[arg-type]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DividendFetchError as exc:
        print(f"DIVIDEND_FETCH_FAILED {exc}", file=sys.stderr)
        sys.exit(2)
