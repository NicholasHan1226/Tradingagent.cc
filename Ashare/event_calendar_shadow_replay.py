"""Offline shadow replay of the event-catalyst factor on the study cache.

Research-only.  Replays the real ``Ashare/event_catalyst_shadow`` factor
over the historical event calendar built from the research cache: every
past earnings-disclosure appointment and lockup expiry for the sample
symbols is minted into a ``CatalystEntry``, the forward-adjusted sample
bars are injected, and ``build_catalyst_shadow_batch`` produces the
deterministic receipt-bound observations.

The output answers one question: do the factor's encoded anticipation
classes (front_run / sell_off / quiet, and front-run moderate vs extreme)
actually separate realised post-event returns in history?  This validates
or challenges the factor's positioning hypotheses with data; it grants no
authority and is not promotion evidence.

Usage::

    python3 Ashare/event_calendar_shadow_replay.py \
        [--cache /tmp/ashare_event_research]
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
from datetime import date, datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from Ashare.event_catalyst_adapter import (  # noqa: E402
    SHARE_FLOAT_DATASET_ID,
    EventCatalystAdapterError,
    catalyst_entry_from_lockup_row,
    catalyst_entries_from_calendar_document,
)
from Ashare.event_catalyst_shadow import (  # noqa: E402
    DailyBar,
    build_catalyst_shadow_batch,
)


class ReplayError(RuntimeError):
    """Fail-closed replay failure with a stable reason code."""


def _read_csv(cache: Path, name: str) -> list[dict[str, str]]:
    path = cache / f"{name}.csv"
    if not path.exists():
        raise ReplayError(f"cache_missing:{path.name}")
    with path.open(encoding="utf-8") as handle:
        reader = csv.reader(handle)
        fields = next(reader)
        return [dict(zip(fields, row)) for row in reader]


def _parse_day(raw: str) -> date:
    return datetime.strptime(raw.strip(), "%Y%m%d").date()


def _load_bars(cache: Path, samples: set[str]) -> dict[str, list[DailyBar]]:
    bars_by_symbol: dict[str, list[DailyBar]] = {}
    for code in sorted(samples):
        stem = code.replace(".", "")
        bar_path = cache / f"daily_{stem}.csv"
        adj_path = cache / f"adjfactor_{stem}.csv"
        if not bar_path.exists() or not adj_path.exists():
            continue
        with bar_path.open(encoding="utf-8") as handle:
            reader = csv.reader(handle)
            fields = next(reader)
            close_i = fields.index("close")
            date_i = fields.index("trade_date")
            rows = [(r[date_i], float(r[close_i])) for r in reader]
        with adj_path.open(encoding="utf-8") as handle:
            reader = csv.reader(handle)
            fields = next(reader)
            ai = fields.index("adj_factor")
            di = fields.index("trade_date")
            factors = {r[di]: float(r[ai]) for r in reader}
        latest = max(factors.values())
        series = [
            DailyBar(trade_date=_parse_day(d), close=c * factors[d] / latest)
            for d, c in rows
            if d in factors and c > 0
        ]
        if series:
            bars_by_symbol[code] = series
    return bars_by_symbol


_BAR_FIELDS = ("ts_code", "trade_date")


def _build_entries(cache: Path, samples: set[str], today: date) -> list[dict]:
    """Mint historical entries via the same adapters used at runtime."""
    entries: list[dict] = []

    # Disclosure appointments through the frozen-evidence snapshot shape.
    # The snapshot type requires evidence receipts we do not have offline,
    # so disclosure entries go through the calendar-document path instead,
    # which the adapter validates field-by-field.
    doc_rows = [
        {
            "event_id": f"disc:{r['ts_code']}:{r['end_date']}:{r['pre_date']}",
            "event_type": "earnings_disclosure",
            "scheduled_date": _iso(_parse_day(r["pre_date"])),
            "date_confidence": "hard_date",
            "impact_direction": "unclear",
            "source_ref": f"tushare:disclosure_date:{r['ann_date']}",
            "entity": r["end_date"],
            "symbol": r["ts_code"],
        }
        for r in _read_csv(cache, "disclosure")
        if r["ts_code"] in samples and _parse_day(r["pre_date"]) <= today
    ]
    entries.extend(doc_rows)

    # Lockup expiries through the dedicated validated-row path.
    lockup_rows = [
        r
        for r in _read_csv(cache, "share_float")
        if r["ts_code"] in samples
        and _parse_day(r["float_date"]) >= _parse_day(r["ann_date"])
        and _parse_day(r["float_date"]) <= today
    ]
    skipped_lockups = 0
    for idx, raw_row in enumerate(lockup_rows):
        # CSV cells are strings; the adapter requires numeric share counts.
        row = dict(raw_row)
        for field in ("float_share", "float_ratio"):
            try:
                row[field] = float(row[field])
            except (TypeError, ValueError):
                pass
        try:
            entry = catalyst_entry_from_lockup_row(
                row,
                dataset_id=SHARE_FLOAT_DATASET_ID,
                receipt_id=f"replay-{idx:06d}",
            )
        except EventCatalystAdapterError:
            # Real rows contain blank/zero float_share records; the adapter
            # fails closed on them and the replay skips and counts them.
            skipped_lockups += 1
            continue
        entries.append(
            {
                "event_id": entry.event_id,
                "event_type": entry.event_type,
                "scheduled_date": entry.scheduled_date.isoformat(),
                "date_confidence": entry.date_confidence,
                "impact_direction": entry.impact_direction,
                "source_ref": entry.source_ref,
                "entity": entry.entity,
                "symbol": entry.symbol,
            }
        )
    print(f"lockup_rows_skipped={skipped_lockups}", flush=True)

    document = {"calendar_id": "ashare-event-replay-v1", "entries": entries}
    return [
        {
            "event_id": e.event_id,
            "event_type": e.event_type,
            "scheduled_date": e.scheduled_date.isoformat(),
            "date_confidence": e.date_confidence,
            "impact_direction": e.impact_direction,
            "source_ref": e.source_ref,
            "entity": e.entity,
            "symbol": e.symbol,
        }
        for e in catalyst_entries_from_calendar_document(document)
    ]


def _iso(day: date) -> str:
    return day.isoformat()


def _group_stats(values: list[float]) -> dict:
    n = len(values)
    if n == 0:
        return {"n": 0}
    ordered = sorted(values)
    return {
        "n": n,
        "mean_bps": round(statistics.fmean(values) * 1e4, 1),
        "median_bps": round(statistics.median(values) * 1e4, 1),
        "win_rate": round(sum(1 for v in values if v > 0) / n, 3),
        "p25_bps": round(ordered[n // 4] * 1e4, 1),
        "p75_bps": round(ordered[(3 * n) // 4] * 1e4, 1),
    }


def main() -> int:
    cache = (
        Path(sys.argv[sys.argv.index("--cache") + 1])
        if "--cache" in sys.argv
        else Path("/tmp/ashare_event_research")
    )
    today = date.today()

    sym_rows = _read_csv(cache, "sample_symbols")
    samples = {r["ts_code"] for r in sym_rows}
    bars_by_symbol = _load_bars(cache, samples)
    print(f"symbols_with_bars={len(bars_by_symbol)}", flush=True)

    raw_entries = _build_entries(cache, samples, today)
    print(f"historical_entries={len(raw_entries)}", flush=True)

    # Convert back into CatalystEntry instances for the shadow factor.
    from Ashare.event_catalyst_adapter import catalyst_entries_from_calendar_document as _mint

    entries = _mint(
        {"calendar_id": "ashare-event-replay-v1", "entries": raw_entries}
    )

    batch = build_catalyst_shadow_batch(
        entries,
        bars_by_symbol,
        as_of=datetime.now(timezone.utc),
    )
    observations = batch.observations
    print(f"observations={len(observations)}", flush=True)

    groups: dict[tuple, list[float]] = {}
    status_counts: dict[str, int] = {}
    for obs in observations:
        status_counts[obs.observation_status] = (
            status_counts.get(obs.observation_status, 0) + 1
        )
        if obs.post_label_state != "labeled" or obs.post_return is None:
            continue
        key = (
            obs.event_type,
            obs.anticipation_class or "?",
            obs.anticipation_intensity or "-",
            obs.positioning_hypothesis or "?",
        )
        groups.setdefault(key, []).append(float(obs.post_return))

    summary = {
        "research_only": True,
        "batch_receipt_sha256": batch.batch_receipt_sha256,
        "observation_status_counts": status_counts,
        "groups": {
            "|".join(key): _group_stats(values)
            for key, values in sorted(groups.items())
        },
    }
    out_path = cache / "shadow_replay_summary.json"
    out_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("# Shadow-factor replay (research_only)\n")
    print(f"- observation statuses: {status_counts}")
    print("\n| event_type | class | intensity | hypothesis | n | mean_bps | median | win_rate |")
    print("|---|---|---|---|---|---|---|---|")
    for key, stat in summary["groups"].items():
        parts = key.split("|")
        if stat.get("n", 0) < 20:
            continue
        print(
            f"| {parts[0]} | {parts[1]} | {parts[2]} | {parts[3]} "
            f"| {stat['n']} | {stat['mean_bps']} | {stat['median_bps']} | {stat['win_rate']} |"
        )
    print(f"\nsaved -> {out_path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ReplayError as exc:
        print(f"REPLAY_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
