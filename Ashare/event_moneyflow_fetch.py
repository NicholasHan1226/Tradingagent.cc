"""All-market daily ``moneyflow`` fetcher (per-stock order-flow by size tier).

Data expansion bound to a mechanism hypothesis: sell_off relief quality may
depend on ORDER-FLOW absorption (who buys the unlocked supply — large vs
small tickets), a dimension distinct from the margin-balance lane (#426/#430).
One Tushare call per trading day returns every symbol's row (~5100 observed,
under the row limit) driven by the LOCAL SSE index calendar so weekends and
holidays never hit the network.

Layout & idempotency: one raw CSV per day under ``<cache>/moneyflow_daily/``;
existing files are never re-fetched (safe resume).  Empty responses leave NO
file so a later rerun retries them.  Rows stay raw — universe filtering and
size-tier aggregation are study-layer concerns.

Fail-closed guards: aborts when the cache volume drops below ``MIN_FREE_BYTES``
free; per-day failures are recorded (printed at the end, exit code 1 if any)
without aborting the sweep.  Days whose row count reaches
``TRUNCATION_WARN_ROWS`` are listed in the summary as potential silent
truncations.  research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_moneyflow_fetch.py [--cache DIR] [--start YYYYMMDD]
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
DEFAULT_END = time.strftime("%Y%m%d")
# Run-time today: a frozen end date silently stops the
# window from ingesting new sessions.
MONEYFLOW_DIRNAME = "moneyflow_daily"
MIN_FREE_BYTES = 2 * 1024**3  # abort below 2 GiB free on the cache volume
TRUNCATION_WARN_ROWS = 5500   # ~5100 observed on a full session; near-limit marker


class MoneyflowFetchError(RuntimeError):
    """Fail-closed fetch failure with a stable reason code."""


def _session_days(cache: Path, start: str, end: str) -> list[str]:
    """SSE sessions from the local index cache inside [start, end]."""
    from Ashare.event_calendar_lockup_strata import (
        StrataError,
        load_index_series,
    )

    try:
        pairs = load_index_series(cache)
    except StrataError as exc:
        raise MoneyflowFetchError(f"index_cache_missing:{exc}") from exc
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


def fetch_moneyflow_daily(
    cache: Path,
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    delay_seconds: float = 0.05,
) -> dict[str, object]:
    """Fetch all-market moneyflow per session; idempotent and resumable."""
    from Ashare.event_calendar_fetch import call_api

    out_dir = cache / MONEYFLOW_DIRNAME
    days = _session_days(cache, start, end)
    if not days:
        raise MoneyflowFetchError("no_sessions_in_range")
    fetched = 0
    skipped_existing = 0
    empty_days: list[str] = []
    failed_days: list[str] = []
    warn_rows_days: list[tuple[str, int]] = []
    fields_out: list[str] | None = None
    for idx, day in enumerate(days):
        target = out_dir / f"{day}.csv"
        if target.exists():
            skipped_existing += 1
            continue
        free = shutil.disk_usage(cache).free
        if free < MIN_FREE_BYTES:
            raise MoneyflowFetchError(f"disk_low:{free}")
        try:
            fields, rows = call_api("moneyflow", {"trade_date": day})
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
            if len(rows) >= TRUNCATION_WARN_ROWS:
                warn_rows_days.append((day, len(rows)))
        if (idx + 1) % 50 == 0:
            print(
                f"moneyflow {idx + 1}/{len(days)} fetched={fetched} "
                f"empty={len(empty_days)} failed={len(failed_days)}",
                flush=True,
            )
        time.sleep(delay_seconds)
    summary: dict[str, object] = {
        "sessions_in_range": len(days),
        "fetched": fetched,
        "skipped_existing": skipped_existing,
        "empty_days": empty_days,
        "failed_days": failed_days,
        "warn_rows_days": warn_rows_days,
        "fields": fields_out,
        "out_dir": str(out_dir),
    }
    print(
        f"moneyflow done: range={start}-{end} fetched={fetched} "
        f"skipped_existing={skipped_existing} empty={len(empty_days)} "
        f"failed={len(failed_days)} warn_rows={len(warn_rows_days)}"
    )
    if failed_days:
        print("failed days (rerun resumes past existing files): "
              + ",".join(failed_days))
    if warn_rows_days:
        print("near-row-limit days (verify no truncation): "
              + ",".join(f"{d}({n})" for d, n in warn_rows_days))
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
    summary = fetch_moneyflow_daily(
        cache, start=args.start, end=args.end, delay_seconds=args.delay
    )
    return 1 if summary["failed_days"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except MoneyflowFetchError as exc:
        print(f"MONEYFLOW_FETCH_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
