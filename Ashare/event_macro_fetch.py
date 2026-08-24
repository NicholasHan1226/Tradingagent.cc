"""Macro release-indicator history fetcher (cn_gdp / cn_cpi / cn_ppi / cn_m).

Panel #13 data face (proposal ``2026-08-24-macro-calendar-pipeline-proposal.md``,
survey ``2026-08-24-macro-calendar-source-survey.md``): four discrete-release
macro indicators bound to the macro-release-window conditioning hypothesis
(lockup-expiry entries whose ±1td window straddles a CPI/PPI/M2/GDP
publication).  Continuous daily series (shibor, us_tycr) are deliberately
NOT collected here — no mechanism hypothesis is bound to them yet.

Engineering facts baked in (from the #505 survey, re-verified live
2026-08-24):

- The shared :func:`Ashare.event_calendar_fetch.call_api` already normalizes
  the list-row envelope to ``(fields, rows)``; the envelope ``count`` field
  is unreliable and row count is ``len(rows)``.
- Full-history calls need NO params for all four endpoints (verified live:
  cn_gdp 178 rows, cn_cpi 511, cn_ppi/cn_m same family); an empty response
  is treated as a per-endpoint FAILURE, not an empty truth.
- Placeholder rows exist TRANSIENTLY near publication boundaries: a month
  key can appear with every value field empty one day and be backfilled the
  next.  Rows therefore stay RAW in the cache; placeholders are only COUNTED
  in the sweep summary — consumers must re-discard defensively at read time
  (same double-guard lesson as blocktrade #23 / holdernum #497).

Files follow the shared-cache layout: ``macro_gdp.csv`` / ``macro_cpi.csv``
/ ``macro_ppi.csv`` / ``macro_money.csv`` beside ``daily_*.csv`` in
``/tmp/ashare_event_research``.  File-level idempotent skip-existing resume,
atomic ``.partial`` writes, fail-closed disk guard, per-endpoint failures
recorded without aborting the remaining endpoints.

research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_macro_fetch.py [--cache DIR] [--delay SECONDS]
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

#: (file stem, tushare api name) — full-history call, no params by contract.
MACRO_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("gdp", "cn_gdp"),
    ("cpi", "cn_cpi"),
    ("ppi", "cn_ppi"),
    ("money", "cn_m"),
)


class MacroFetchError(RuntimeError):
    """Fail-closed fetch failure with a stable reason code."""


def _is_placeholder(fields: list[str], row: list) -> bool:
    """True when every non-key field of the row is empty/None.

    The period key is field 0 (``quarter`` or ``month``); a placeholder row
    carries the key but none of the values yet.
    """
    return all(value is None or value == "" for value in row[1:])


def _write_day(path: Path, fields: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".partial")
    with tmp_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        writer.writerows(rows)
    tmp_path.replace(path)


def fetch_macro(
    cache: Path,
    delay_seconds: float = 0.12,
    call=None,
) -> dict[str, object]:
    """Fetch full macro-indicator history per endpoint; idempotent/resumable.

    ``call(api_name, params) -> (fields, rows)`` is injectable for offline
    tests; the default resolves the shared authenticated helper lazily.
    """
    if call is None:
        from Ashare.event_calendar_fetch import call_api

        call = call_api

    fetched = 0
    skipped_existing = 0
    failed_endpoints: list[str] = []
    placeholder_rows: dict[str, int] = {}
    fields_out: dict[str, list[str]] = {}
    for idx, (stem, api_name) in enumerate(MACRO_ENDPOINTS):
        target = cache / f"macro_{stem}.csv"
        if target.exists():
            skipped_existing += 1
            continue
        free = shutil.disk_usage(cache).free
        if free < MIN_FREE_BYTES:
            raise MacroFetchError(f"disk_low:{free}")
        try:
            # Contract: NO params — full history in one call (#505 fact 3,
            # re-verified 2026-08-24).  Empty response = endpoint failure.
            fields, rows = call(api_name, {})
        except Exception as exc:  # noqa: BLE001 - record it, keep going
            failed_endpoints.append(f"{stem}:{type(exc).__name__}")
            time.sleep(max(delay_seconds, 0.05))
            continue
        if not fields or not rows:
            failed_endpoints.append(f"{stem}:empty_response")
            time.sleep(max(delay_seconds, 0.05))
            continue
        placeholder_rows[stem] = sum(
            1 for row in rows if _is_placeholder(list(fields), row)
        )
        fields_out[stem] = [str(name) for name in fields]
        _write_day(target, [str(name) for name in fields], [list(r) for r in rows])
        fetched += 1
        print(
            f"macro {stem}({api_name}): {len(rows)} rows "
            f"placeholders={placeholder_rows[stem]}",
            flush=True,
        )
        if (idx + 1) % 200 == 0:
            print(
                f"macro {idx + 1}/{len(MACRO_ENDPOINTS)} fetched={fetched} "
                f"skipped={skipped_existing} failed={len(failed_endpoints)}",
                flush=True,
            )
        time.sleep(delay_seconds)
    summary: dict[str, object] = {
        "endpoints_in_sweep": len(MACRO_ENDPOINTS),
        "fetched": fetched,
        "skipped_existing": skipped_existing,
        "failed_endpoints": failed_endpoints,
        "placeholder_rows": placeholder_rows,
        "fields": fields_out,
    }
    print(
        f"macro done: fetched={fetched} skipped_existing={skipped_existing} "
        f"failed={len(failed_endpoints)}"
    )
    if failed_endpoints:
        print("failed endpoints (rerun resumes past existing files): "
              + ",".join(failed_endpoints))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--delay", type=float, default=0.12)
    args = parser.parse_args()
    from Ashare.event_calendar_fetch import CACHE_DIR

    cache = args.cache if args.cache is not None else CACHE_DIR
    summary = fetch_macro(cache, delay_seconds=args.delay)
    return 1 if summary["failed_endpoints"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except MacroFetchError as exc:
        print(f"MACRO_FETCH_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
