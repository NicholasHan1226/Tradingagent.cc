"""Per-symbol daily ``daily_basic`` fetcher (valuation & turnover panel).

Data expansion bound to two mechanism hypotheses:

- H1 (turnover normalization): pre-event turnover-rate anomalies carry
  demand-side information complementary to the three supply-side views
  already in place (margin support #430, order-flow outflow #432, block
  discount #436) — intensity measures so far use raw amounts only.
- H2 (float cross-validation): the stratification variable ``float_ratio``
  currently derives from share_float announcements; ``daily_basic`` float
  shares / circ_mv give an independent measure to validate those buckets.

Fetched per SYMBOL (one call returns the symbol's full daily_basic history
≈ 2.1k rows, far below any row cap — no truncation guard needed), so files
line up with the existing per-symbol layout: ``dailybasic_<stem>.csv``
beside ``daily_<stem>.csv`` / ``adjfactor_<stem>.csv``.  The sweep is driven
by symbols ALREADY in the local cache (stems of ``daily_*.csv``) and by the
local SSE index calendar for the date window; idempotent skip-existing
resume, one raw CSV per symbol, empty responses leave NO file, per-symbol
failures are recorded without aborting the sweep, atomic ``.partial``
writes, fail-closed disk guard.  Rows stay raw — bucketing and factor math
are study-layer concerns.

research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_dailybasic_fetch.py [--cache DIR]
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
DEFAULT_END = "20260824"
MIN_FREE_BYTES = 2 * 1024**3  # abort below 2 GiB free on the cache volume


class DailybasicFetchError(RuntimeError):
    """Fail-closed fetch failure with a stable reason code."""


def _cached_stems(cache: Path) -> list[str]:
    """Stems of symbols already present as ``daily_*.csv``."""
    return sorted(path.stem.removeprefix("daily_")
                  for path in cache.glob("daily_*.csv"))


def _write_day(path: Path, fields: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".partial")
    with tmp_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        writer.writerows(rows)
    tmp_path.replace(path)


def fetch_dailybasic(
    cache: Path,
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    delay_seconds: float = 0.12,
) -> dict[str, object]:
    """Fetch daily_basic per cached symbol; idempotent and resumable."""
    from Ashare.event_calendar_fetch import call_api

    stems = _cached_stems(cache)
    if not stems:
        raise DailybasicFetchError("daily_cache_missing")
    fetched = 0
    skipped_existing = 0
    empty_symbols: list[str] = []
    failed_symbols: list[str] = []
    fields_out: list[str] | None = None
    for idx, stem in enumerate(stems):
        target = cache / f"dailybasic_{stem}.csv"
        if target.exists():
            skipped_existing += 1
            continue
        free = shutil.disk_usage(cache).free
        if free < MIN_FREE_BYTES:
            raise DailybasicFetchError(f"disk_low:{free}")
        try:
            fields, rows = call_api(
                "daily_basic",
                {"ts_code": stem_to_code(stem),
                 "start_date": start,
                 "end_date": end},
            )
        except Exception:  # noqa: BLE001 - record the symbol, keep going
            failed_symbols.append(stem)
            time.sleep(max(delay_seconds, 0.05))
            continue
        if fields_out is None and fields:
            fields_out = list(fields)
        if not rows:
            empty_symbols.append(stem)
        else:
            _write_day(target, list(fields), rows)
            fetched += 1
        if (idx + 1) % 200 == 0:
            print(
                f"dailybasic {idx + 1}/{len(stems)} fetched={fetched} "
                f"empty={len(empty_symbols)} failed={len(failed_symbols)}",
                flush=True,
            )
        time.sleep(delay_seconds)
    summary: dict[str, object] = {
        "symbols_in_cache": len(stems),
        "fetched": fetched,
        "skipped_existing": skipped_existing,
        "empty_symbols": empty_symbols,
        "failed_symbols": failed_symbols,
        "fields": fields_out,
    }
    print(
        f"dailybasic done: range={start}-{end} fetched={fetched} "
        f"skipped_existing={skipped_existing} empty={len(empty_symbols)} "
        f"failed={len(failed_symbols)}"
    )
    if failed_symbols:
        print("failed symbols (rerun resumes past existing files): "
              + ",".join(failed_symbols))
    return summary


def stem_to_code(stem: str) -> str:
    """``000001SZ`` -> ``000001.SZ`` (same mapping as the study loaders)."""
    return f"{stem[:6]}.{stem[6:]}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--start", type=str, default=DEFAULT_START)
    parser.add_argument("--end", type=str, default=DEFAULT_END)
    parser.add_argument("--delay", type=float, default=0.12)
    args = parser.parse_args()
    from Ashare.event_calendar_fetch import CACHE_DIR

    cache = args.cache if args.cache is not None else CACHE_DIR
    summary = fetch_dailybasic(
        cache, start=args.start, end=args.end, delay_seconds=args.delay
    )
    return 1 if summary["failed_symbols"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DailybasicFetchError as exc:
        print(f"DAILYBASIC_FETCH_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
