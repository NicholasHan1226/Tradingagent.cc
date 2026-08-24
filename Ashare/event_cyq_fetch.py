"""Per-symbol daily ``cyq_perf`` fetcher (chip distribution & win rate).

Data expansion bound to a mechanism hypothesis: chip concentration and
winner_rate quantify WHERE holders sit relative to price — around unlock
events they measure how much of the float is underwater when new supply
arrives, information neither order-flow absorption (#431), block discount
(#436) nor turnover (#441) can see.  Cost percentiles (5/15/50/85/95),
weight_avg and winner_rate land raw for study-layer factor math.

Fetched per SYMBOL (one call returns the symbol's full cyq_perf history ≈
2.1k rows, far below any row cap — no truncation guard needed; verified
live 2026-08-24 that history reaches back to 20180102), so files line up
with the existing per-symbol layout: ``cyqperf_<stem>.csv`` beside
``daily_<stem>.csv`` / ``dailybasic_<stem>.csv``.  The sweep is driven by
symbols ALREADY in the local cache (stems of ``daily_*.csv``); idempotent
skip-existing resume, one raw CSV per symbol, empty responses leave NO
file, per-symbol failures are recorded without aborting the sweep,
atomic ``.partial`` writes, fail-closed disk guard.  Rows stay raw.

research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_cyq_fetch.py [--cache DIR]
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


class CyqFetchError(RuntimeError):
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


def stem_to_code(stem: str) -> str:
    """``000001SZ`` -> ``000001.SZ`` (same mapping as the study loaders)."""
    return f"{stem[:6]}.{stem[6:]}"


def fetch_cyq(
    cache: Path,
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    delay_seconds: float = 0.12,
) -> dict[str, object]:
    """Fetch cyq_perf per cached symbol; idempotent and resumable."""
    from Ashare.event_calendar_fetch import call_api

    stems = _cached_stems(cache)
    if not stems:
        raise CyqFetchError("daily_cache_missing")
    fetched = 0
    skipped_existing = 0
    empty_symbols: list[str] = []
    failed_symbols: list[str] = []
    fields_out: list[str] | None = None
    for idx, stem in enumerate(stems):
        target = cache / f"cyqperf_{stem}.csv"
        if target.exists():
            skipped_existing += 1
            continue
        free = shutil.disk_usage(cache).free
        if free < MIN_FREE_BYTES:
            raise CyqFetchError(f"disk_low:{free}")
        try:
            fields, rows = call_api(
                "cyq_perf",
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
                f"cyq {idx + 1}/{len(stems)} fetched={fetched} "
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
        f"cyq done: range={start}-{end} fetched={fetched} "
        f"skipped_existing={skipped_existing} empty={len(empty_symbols)} "
        f"failed={len(failed_symbols)}"
    )
    if failed_symbols:
        print("failed symbols (rerun resumes past existing files): "
              + ",".join(failed_symbols))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--start", type=str, default=DEFAULT_START)
    parser.add_argument("--end", type=str, default=DEFAULT_END)
    parser.add_argument("--delay", type=float, default=0.12)
    args = parser.parse_args()
    from Ashare.event_calendar_fetch import CACHE_DIR

    cache = args.cache if args.cache is not None else CACHE_DIR
    summary = fetch_cyq(
        cache, start=args.start, end=args.end, delay_seconds=args.delay
    )
    return 1 if summary["failed_symbols"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CyqFetchError as exc:
        print(f"CYQ_FETCH_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
