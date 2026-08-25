"""All-market daily ``margin_detail`` fetcher (stock-level margin balances).

Data expansion for task #19's phase two: does the STOCK'S OWN margin balance
add separation beyond the market-level crowding state (#426 first read)?
One Tushare call per trading day returns every symbol's row (~2000 < limit),
so the full 2018+ history costs one call per session — driven by the LOCAL
SSE index calendar so weekends/holidays never hit the network.

Layout & idempotency: one raw CSV per day under ``<cache>/margin_detail_daily/``;
existing files are never re-fetched (safe resume).  Empty responses (holidays,
pre-market days, publication gaps) leave NO file so a later rerun retries them.
Rows stay raw — ETF filtering is a study-layer concern.

Fail-closed guards: aborts when the cache volume drops below ``MIN_FREE_BYTES``
free, and records per-day failures (printed at the end, exit code 1 if any)
without aborting the sweep.  research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_margin_detail_fetch.py [--cache DIR] [--start YYYYMMDD]
        [--end YYYYMMDD] [--delay SECONDS]
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
DEFAULT_END = time.strftime("%Y%m%d")  # T-day balance publishes T+1 morning; end=today is harmless
# Run-time today: a frozen end date silently stops the
# window from ingesting new sessions.
DETAIL_DIRNAME = "margin_detail_daily"
MIN_FREE_BYTES = 2 * 1024**3  # abort below 2 GiB free on the cache volume


class MarginDetailFetchError(RuntimeError):
    """Fail-closed fetch failure with a stable reason code."""


def _session_days(
    cache: Path, start: str, end: str
) -> list[str]:
    """SSE sessions from the local index cache inside [start, end]."""
    from Ashare.event_calendar_lockup_strata import (
        StrataError,
        load_index_series,
    )

    try:
        pairs = load_index_series(cache)
    except StrataError as exc:
        raise MarginDetailFetchError(f"index_cache_missing:{exc}") from exc
    return [
        d.strftime("%Y%m%d")
        for d, _ in pairs
        if start <= d.strftime("%Y%m%d") <= end
    ]


def _write_day(path: Path, fields: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".partial")
    with tmp_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        writer.writerows(rows)
    tmp_path.replace(path)


def fetch_margin_detail_daily(
    cache: Path,
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    delay_seconds: float = 0.05,
) -> dict[str, object]:
    """Fetch all-market margin detail per session; idempotent and resumable."""
    from Ashare.event_calendar_fetch import call_api

    out_dir = cache / DETAIL_DIRNAME
    days = _session_days(cache, start, end)
    if not days:
        raise MarginDetailFetchError("no_sessions_in_range")
    fetched = 0
    skipped_existing = 0
    empty_days: list[str] = []
    failed_days: list[str] = []
    fields_out: list[str] | None = None
    for idx, day in enumerate(days):
        target = out_dir / f"{day}.csv"
        if target.exists():
            skipped_existing += 1
            continue
        free = shutil.disk_usage(cache).free
        if free < MIN_FREE_BYTES:
            raise MarginDetailFetchError(f"disk_low:{free}")
        try:
            fields, rows = call_api("margin_detail", {"trade_date": day})
        except Exception:  # noqa: BLE001 - record the day, keep the sweep going
            failed_days.append(day)
            time.sleep(max(delay_seconds, 0.05))
            continue
        if fields_out is None and fields:
            fields_out = list(fields)
        if not rows:
            empty_days.append(day)
        else:
            _write_day(target, list(fields), rows)
            fetched += 1
        if (idx + 1) % 50 == 0:
            print(
                f"margin_detail {idx + 1}/{len(days)} "
                f"fetched={fetched} empty={len(empty_days)} failed={len(failed_days)}",
                flush=True,
            )
        time.sleep(delay_seconds)
    summary: dict[str, object] = {
        "sessions_in_range": len(days),
        "fetched": fetched,
        "skipped_existing": skipped_existing,
        "empty_days": empty_days,
        "failed_days": failed_days,
        "fields": fields_out,
        "out_dir": str(out_dir),
    }
    print(
        f"margin_detail done: range={start}-{end} fetched={fetched} "
        f"skipped_existing={skipped_existing} empty={len(empty_days)} "
        f"failed={len(failed_days)}"
    )
    if failed_days:
        print("failed days (rerun resumes past existing files): "
              + ",".join(failed_days))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--start", type=str, default=DEFAULT_START)
    parser.add_argument("--end", type=str, default=DEFAULT_END)
    parser.add_argument("--delay", type=float, default=0.05)
    args = parser.parse_args()
    from Ashare.event_calendar_fetch import CACHE_DIR

    cache = args.cache if args.cache is not None else CACHE_DIR
    summary = fetch_margin_detail_daily(
        cache, start=args.start, end=args.end, delay_seconds=args.delay
    )
    return 1 if summary["failed_days"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except MarginDetailFetchError as exc:
        print(f"MARGIN_DETAIL_FETCH_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
