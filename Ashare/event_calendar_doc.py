"""Build the forward-looking A-share event calendar from the research cache.

Research-only.  Reads the scratch cache fetched by ``event_calendar_fetch``
and emits two artefacts under the cache directory:

* ``calendar_doc.json`` -- one caller-maintained calendar document in the
  exact shape ``Ashare/event_catalyst_adapter.catalyst_entries_from_calendar_document``
  validates (calendar_id + entries with event_type / scheduled_date /
  date_confidence / impact_direction / source_ref), so a later integration
  step can feed it to the shadow factor unchanged.
* ``calendar_view.md``  -- a human-readable month view: per-day disclosure
  and lockup counts, top lockups by share size, and upcoming LPR dates.

Future LPR dates are generated from the monthly-on-the-20th rule, rolled
forward to the next trading day using the cached index calendar; they carry
``expected_window`` confidence, not ``hard_date``.

Usage::

    python3 Ashare/event_calendar_doc.py [--cache /tmp/ashare_event_research]
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

CALENDAR_ID = "ashare-event-calendar-research-v1"
INDEX_CACHE_STEM = "index_000001SH"
LPR_FORWARD_MONTHS = 4


class DocError(RuntimeError):
    """Fail-closed calendar build failure with a stable reason code."""


def _read_csv(cache: Path, name: str) -> list[dict[str, str]]:
    path = cache / f"{name}.csv"
    if not path.exists():
        raise DocError(f"cache_missing:{path.name}")
    with path.open(encoding="utf-8") as handle:
        reader = csv.reader(handle)
        fields = next(reader)
        return [dict(zip(fields, row)) for row in reader]


def _parse_day(raw: str) -> date:
    try:
        return datetime.strptime(raw.strip(), "%Y%m%d").date()
    except ValueError as exc:
        raise DocError(f"bad_date:{raw}") from exc


def _iso(day: date) -> str:
    return day.isoformat()


def _entry(
    event_id: str,
    event_type: str,
    scheduled: date,
    date_confidence: str,
    impact_direction: str,
    source_ref: str,
    entity: str | None = None,
    symbol: str | None = None,
) -> dict:
    entry = {
        "event_id": event_id,
        "event_type": event_type,
        "scheduled_date": _iso(scheduled),
        "date_confidence": date_confidence,
        "impact_direction": impact_direction,
        "source_ref": source_ref,
    }
    if entity is not None:
        entry["entity"] = entity
    if symbol is not None:
        entry["symbol"] = symbol
    return entry


def build_entries(cache: Path, today: date) -> tuple[list[dict], dict[str, list[dict]]]:
    entries: list[dict] = []
    view_rows: dict[str, list[dict]] = {"lpr": [], "disclosure": [], "lockup": []}

    # --- LPR: rule-based forward dates ------------------------------------
    # The cached index calendar only covers the fetched past, so future
    # roll-forward uses a weekend rule only; mainland holidays can shift
    # these by another day or two, which is why they stay expected_window.
    lpr_rows = _read_csv(cache, "lpr")
    if lpr_rows:
        last = max(_parse_day(r["date"]) for r in lpr_rows)
        cursor_year, cursor_month = last.year, last.month
        for _ in range(LPR_FORWARD_MONTHS):
            cursor_month += 1
            if cursor_month > 12:
                cursor_year, cursor_month = cursor_year + 1, 1
            scheduled = date(cursor_year, cursor_month, 20)
            while scheduled.weekday() >= 5:
                scheduled += timedelta(days=1)
            entries.append(
                _entry(
                    f"lpr:{_iso(scheduled)}",
                    "macro_release",
                    scheduled,
                    "expected_window",
                    "unclear",
                    "rule:lpr_monthly_20th_rolled_to_trading_day",
                    entity="PBOC_LPR",
                )
            )

    # --- Earnings disclosure appointments ---------------------------------
    for row in _read_csv(cache, "disclosure"):
        pre = _parse_day(row["pre_date"])
        if pre <= today:
            continue
        symbol = row["ts_code"]
        report_period = row.get("end_date", "")
        entries.append(
            _entry(
                f"disc:{symbol}:{report_period}:{row['pre_date']}",
                "earnings_disclosure",
                pre,
                "hard_date",
                "unclear",
                f"tushare:disclosure_date:{row['ann_date']}",
                entity=report_period,
                symbol=symbol,
            )
        )
        view_rows["disclosure"].append({"date": pre, "symbol": symbol})

    # --- Lockup expiries ---------------------------------------------------
    for row in _read_csv(cache, "share_float"):
        float_day = _parse_day(row["float_date"])
        ann = _parse_day(row["ann_date"])
        if float_day < ann or float_day <= today:
            continue
        symbol = row["ts_code"]
        holder = row.get("holder_name", "")
        entries.append(
            _entry(
                f"lockup:{symbol}:{row['float_date']}:{holder}",
                "lockup_expiry",
                float_day,
                "hard_date",
                "negative",
                f"tushare:share_float:{row['ann_date']}",
                entity=holder,
                symbol=symbol,
            )
        )
        view_rows["lockup"].append(
            {"date": float_day, "symbol": symbol, "float_share": float(row["float_share"]), "holder": holder}
        )

    # Adapter contract requires unique raw ids; enforce before emitting.
    seen: set[str] = set()
    unique: list[dict] = []
    for entry in entries:
        if entry["event_id"] in seen:
            raise DocError(f"duplicate_event_id:{entry['event_id']}")
        seen.add(entry["event_id"])
        unique.append(entry)
    unique.sort(key=lambda e: e["scheduled_date"])
    return unique, view_rows


def write_view(path: Path, view_rows: dict[str, list[dict]], today: date) -> None:
    lines = [f"# A-share forward event calendar (generated {today.isoformat()}, research_only)", ""]
    by_month: dict[str, dict[str, dict[str, int]]] = {}
    for item in view_rows["disclosure"]:
        month = item["date"].strftime("%Y-%m")
        by_month.setdefault(month, {}).setdefault(item["date"].isoformat(), {})
        day_map = by_month[month][item["date"].isoformat()]
        day_map["disclosures"] = day_map.get("disclosures", 0) + 1
    for item in view_rows["lockup"]:
        month = item["date"].strftime("%Y-%m")
        by_month.setdefault(month, {}).setdefault(item["date"].isoformat(), {})
        day_map = by_month[month][item["date"].isoformat()]
        day_map["lockups"] = day_map.get("lockups", 0) + 1

    for month in sorted(by_month):
        lines.append(f"## {month}")
        lines.append("")
        lines.append("| 日期 | 财报披露家数 | 解禁笔数 |")
        lines.append("|---|---|---|")
        for day in sorted(by_month[month]):
            counts = by_month[month][day]
            lines.append(
                f"| {day} | {counts.get('disclosures', 0)} | {counts.get('lockups', 0)} |"
            )
        lines.append("")

    lines.append("## LPR 预计发布日")
    lines.append("")
    lpr_days = sorted({e["scheduled_date"] for e in view_rows["lpr"]})
    lines.extend(f"- {d}" for d in lpr_days) if lpr_days else lines.append("- （缓存中无 LPR 历史）")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    cache = (
        Path(sys.argv[sys.argv.index("--cache") + 1])
        if "--cache" in sys.argv
        else Path("/tmp/ashare_event_research")
    )
    today = date.today()

    # Keep the LPR forward entries visible in the human view as well.
    entries, view_rows = build_entries(cache, today)
    view_rows["lpr"] = [e for e in entries if e["event_type"] == "macro_release"]

    doc_path = cache / "calendar_doc.json"
    doc_path.write_text(
        json.dumps({"calendar_id": CALENDAR_ID, "entries": entries}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    view_path = cache / "calendar_view.md"
    write_view(view_path, view_rows, today)

    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry["event_type"]] = counts.get(entry["event_type"], 0) + 1
    print(f"entries_total={len(entries)} {counts}")
    print(f"saved -> {doc_path}")
    print(f"saved -> {view_path}")

    # Self-check against the real adapter so contract drift fails loudly.
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))
    from Ashare.event_catalyst_adapter import (
        catalyst_entries_from_calendar_document,
    )

    minted = catalyst_entries_from_calendar_document(json.loads(doc_path.read_text(encoding="utf-8")))
    print(f"adapter_validation_ok entries={len(minted)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DocError as exc:
        print(f"DOC_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
