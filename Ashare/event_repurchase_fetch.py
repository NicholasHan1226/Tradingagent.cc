"""All-market share ``repurchase`` announcement fetcher (company buybacks).

Data expansion bound to a mechanism hypothesis: the #453/#455 holdertrade
first read showed pre-unlock insider NET-SELLING marks a toxic population
(−131bps, win .435).  Company buyback announcements are the mirror
demand-side flow — management absorbing upcoming supply — so pre-event
buyback activity may condition sell_off outcomes positively.  One Tushare
call per CALENDAR MONTH returns that month's announcements (a few hundred
rows observed; well below row limits), and rows are grouped into one raw
CSV per ``ann_date`` under ``<cache>/repurchase_ann/`` — the ann-date-keyed
layout family shared with ``holdertrade_daily`` (announcement date is the
only clean key; end_date/exp_date are mixed-purpose fields kept raw for
the study layer to classify by ``proc`` state).

Idempotency mirrors the blocktrade/moneyflow fetchers: existing files are
never re-fetched (safe resume); per-month failures are recorded (printed
at the end, exit code 1 if any) without aborting the sweep.  Fail-closed
guards: aborts when the cache volume drops below ``MIN_FREE_BYTES`` free;
missing token aborts before any call.  research_only /
not_promotion_evidence.

Usage::

    python3 Ashare/event_repurchase_fetch.py [--cache DIR]
        [--start YYYYMMDD] [--end YYYYMMDD] [--delay SECONDS]
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

DEFAULT_START = "20180101"
DEFAULT_END = time.strftime("%Y%m%d")
# Run-time today: a frozen end date silently stops the
# window from ingesting new sessions.
REPURCHASE_DIRNAME = "repurchase_ann"
MIN_FREE_BYTES = 2 * 1024**3  # abort below 2 GiB free on the cache volume
FIELDS = [
    "ts_code",
    "ann_date",
    "end_date",
    "proc",
    "exp_date",
    "vol",
    "amount",
    "high_limit",
    "low_limit",
]


class RepurchaseFetchError(RuntimeError):
    """Fail-closed fetch failure with a stable reason code."""


def month_windows(start: str, end: str) -> list[tuple[str, str]]:
    """Inclusive [month_start, month_end] windows clipped to [start, end].

    Monthly calls instead of per-day sweeps: ~500 rows/month is far below
    row limits, so full history costs ~100 API calls instead of ~3200.
    """
    if len(start) != 8 or len(end) != 8 or start > end:
        raise RepurchaseFetchError("bad_range")
    windows: list[tuple[str, str]] = []
    year, month = int(start[:4]), int(start[4:6])
    while True:
        last_day = _last_day(year, month)
        win_start = f"{year:04d}{month:02d}01"
        win_end = f"{year:04d}{month:02d}{last_day:02d}"
        if win_end < start:
            pass  # whole month before the range
        elif win_start > end:
            break
        else:
            windows.append((max(win_start, start), min(win_end, end)))
        month += 1
        if month == 13:
            year, month = year + 1, 1
        if f"{year:04d}{month:02d}01" > end:
            break
    return windows


def _last_day(year: int, month: int) -> int:
    if month in (1, 3, 5, 7, 8, 10, 12):
        return 31
    if month in (4, 6, 9, 11):
        return 30
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    return 29 if leap else 28


def _token() -> str:
    import os

    token = os.environ.get("TUSHARE_MCP_TOKEN", "").strip().strip('"').strip("'")
    if not token:
        raise RepurchaseFetchError("token_missing")
    return token


def fetch_repurchase(
    cache: Path,
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    delay: float = 0.3,
    call=None,
) -> dict[str, object]:
    """Fetch announcements month by month into per-ann_date CSV files.

    ``call(start_date, end_date)`` returns the raw month payload as a list
    of row dicts (tests inject fakes; production wraps tushare).
    """
    folder = cache / REPURCHASE_DIRNAME
    if shutil.disk_usage(cache).free < MIN_FREE_BYTES:
        raise RepurchaseFetchError("disk_full_guard")
    folder.mkdir(parents=True, exist_ok=True)

    stats: dict[str, object] = {
        "months": 0,
        "files_written": 0,
        "files_skipped": 0,
        "rows_seen": 0,
        "bad_rows": 0,
        "errors": [],
    }
    if call is None:
        import tushare

        pro = tushare.pro_api(_token())

        def call(start_date: str, end_date: str):  # noqa: E306
            frame = pro.repurchase(start_date=start_date, end_date=end_date)
            return [] if frame is None else frame.to_dict("records")

    for win_start, win_end in month_windows(start, end):
        stats["months"] += 1
        try:
            rows = call(win_start, win_end)
        except Exception as exc:  # noqa: BLE001 - recorded, sweep continues
            stats["errors"].append(f"{win_start}: {exc}")
            time.sleep(max(delay, 0.1))
            continue
        time.sleep(max(delay, 0.0))
        by_day: dict[str, list[dict]] = {}
        for row in rows:
            stats["rows_seen"] += 1
            ann = str(row.get("ann_date") or "")
            if len(ann) != 8 or not ann.isdigit() or not row.get("ts_code"):
                stats["bad_rows"] += 1
                continue
            by_day.setdefault(ann, []).append(row)
        for ann in sorted(by_day):
            path = folder / f"{ann}.csv"
            if path.exists():
                stats["files_skipped"] += 1
                continue
            _write_csv(path, by_day[ann])
            stats["files_written"] = int(stats["files_written"]) + 1
    return stats


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--delay", type=float, default=0.3)
    args = parser.parse_args()
    from Ashare.event_calendar_fetch import CACHE_DIR

    cache = args.cache if args.cache is not None else CACHE_DIR
    stats = fetch_repurchase(
        cache, start=args.start, end=args.end, delay=args.delay
    )
    print(
        f"repurchase fetch: months={stats['months']} "
        f"written={stats['files_written']} skipped={stats['files_skipped']} "
        f"rows={stats['rows_seen']} bad={stats['bad_rows']} "
        f"errors={len(stats['errors'])}"  # type: ignore[arg-type]
    )
    for err in stats["errors"]:  # type: ignore[union-attr]
        print(f"  ERROR {err}", file=sys.stderr)
    return 1 if stats["errors"] else 0  # type: ignore[arg-type]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RepurchaseFetchError as exc:
        print(f"REPURCHASE_FETCH_FAILED {exc}", file=sys.stderr)
        sys.exit(2)
