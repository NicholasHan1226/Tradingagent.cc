"""Per-symbol ``stk_holdernumber`` fetcher (shareholder-count history).

Data expansion bound to a mechanism hypothesis: shareholder-count
contraction ahead of a lockup-expiry entry marks a pre-concentrated chip
structure (strong hands accumulated while supply was locked), while
expansion marks thin retail absorption — the behavioural counterpart of
the price-side cyq_perf panel (#431/#441: 78% of unlocks land underwater,
winner-rate gradients monotone).  Neither the flow-side panels
(holdertrade #453, repurchase #457) nor the pledge stock panel (#462)
sees WHO currently holds the float.

Fetched per SYMBOL via ``stk_holdernumber`` with **ts_code ONLY** — one
call returns the symbol's full disclosure history (~40-150 rows; verified
live 2026-08-24: 000001.SZ -> 150 rows back to ann_date 1996).  Date
filtering happens to work on this endpoint, but ts_code-only already
returns everything and keeps the sweep contract uniform with #462/#465.
Files follow the existing per-symbol layout: ``holdernum_<stem>.csv``
beside ``daily_<stem>.csv``.  The sweep is driven by symbols ALREADY in
the local cache (stems of ``daily_*.csv``); idempotent skip-existing
resume, one raw CSV per symbol, empty responses leave NO file, per-symbol
failures are recorded without aborting the sweep, atomic ``.partial``
writes, fail-closed disk guard.  Rows stay raw.

research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_holdernumber_fetch.py [--cache DIR]
        [--delay SECONDS]
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

MIN_FREE_BYTES = 2 * 1024**3  # abort below 2 GiB free on the cache volume


class HolderNumFetchError(RuntimeError):
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


def fetch_holdernumber(
    cache: Path,
    delay_seconds: float = 0.12,
    call=None,
) -> dict[str, object]:
    """Fetch stk_holdernumber history per cached symbol; idempotent/resumable.

    The API contract used here is ``ts_code`` ONLY (module docstring).
    ``call(api_name, params) -> (fields, rows)`` is injectable for offline
    tests; the default resolves the shared authenticated helper lazily.
    """
    if call is None:
        from Ashare.event_calendar_fetch import call_api

        call = call_api

    stems = _cached_stems(cache)
    if not stems:
        raise HolderNumFetchError("daily_cache_missing")
    fetched = 0
    skipped_existing = 0
    empty_symbols: list[str] = []
    failed_symbols: list[str] = []
    fields_out: list[str] | None = None
    for idx, stem in enumerate(stems):
        target = cache / f"holdernum_{stem}.csv"
        if target.exists():
            skipped_existing += 1
            continue
        free = shutil.disk_usage(cache).free
        if free < MIN_FREE_BYTES:
            raise HolderNumFetchError(f"disk_low:{free}")
        try:
            fields, rows = call(
                "stk_holdernumber", {"ts_code": stem_to_code(stem)}
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
                f"holdernum {idx + 1}/{len(stems)} fetched={fetched} "
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
        f"holdernum done: fetched={fetched} "
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
    parser.add_argument("--delay", type=float, default=0.12)
    args = parser.parse_args()
    from Ashare.event_calendar_fetch import CACHE_DIR

    cache = args.cache if args.cache is not None else CACHE_DIR
    summary = fetch_holdernumber(cache, delay_seconds=args.delay)
    return 1 if summary["failed_symbols"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except HolderNumFetchError as exc:
        print(f"HOLDERNUM_FETCH_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
