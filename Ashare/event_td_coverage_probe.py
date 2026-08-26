"""Read-only TradingDatas production coverage probe (server-side).

One diagnostic entry point for the maintenance window: validates the V1
catalog, then probes the datasets the event-research program consumes and
writes a JSON receipt plus a human-readable summary.  Nothing outside
``--out-dir`` is written and no journal or ledger is touched.

Probes (all bounded, read-only):

* ``cn.dataset.share_float`` / ``cn.dataset.disclosure_date`` — the two
  active event datasets behind ``event_calendar_tradingdatas.py``: count
  rows over the most recent ``--lookback-days`` ``ann_date`` partitions so
  real coverage can be compared with the Tushare baseline before the
  full-market calendar is trusted.
* the five active macro datasets (``cn_cpi`` / ``cn_pmi`` / ``cn_gdp`` /
  ``cn_m`` / ``cn_schedule``) — read whole recent months through the same
  fail-closed collector and report row counts plus raw first rows.  For
  ``cn_schedule`` it also reads the following month and flags whether any
  ``publish_date`` lies at/after today: that answers whether the dataset
  can serve as a *forward* macro calendar before any consumer is built.

The probe records data anomalies instead of raising them; only catalog or
authentication failures abort, because nothing downstream means anything
without them.  research_only / not_promotion_evidence.

Usage (on the release host — the TD read API is loopback-only)::

    python3 Ashare/event_td_coverage_probe.py \
        --token-file /etc/tradingagent/tradingdatas-read.token \
        --out-dir /tmp/td_coverage_probe [--lookback-days 30]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from shared.data.sharedsignals_v1 import (  # noqa: E402
    CatalogEnvelope,
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
    SharedSignalsV1Error,
    parse_catalog_envelope,
)
from shared.data.tradingdatas_pagination import collect_query_pages  # noqa: E402

from Ashare.event_calendar_tradingdatas import (  # noqa: E402
    ACCESS_POLICY_ID,
    CATALOG_PATH,
    DATASET_SPECS,
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    TRANSPORT_ID,
    TradingDatasCalendarError,
    load_transport,
)

PROBE_ID = "ashare-td-coverage-probe-v1"

MACRO_DATASETS = (
    "cn.dataset.cn_cpi",
    "cn.dataset.cn_pmi",
    "cn.dataset.cn_gdp",
    "cn.dataset.cn_m",
    "cn.dataset.cn_schedule",
)
FORWARD_DATASET_ID = "cn.dataset.cn_schedule"
MAX_PAGES_PER_MONTH_QUERY = 50
MAX_ROWS_PER_MONTH_QUERY = 10_000


class ProbeError(RuntimeError):
    """Fail-fast probe setup failure with a stable reason code."""


def _month_starts(today: date, count: int) -> list[str]:
    """Most recent ``count`` months including the current one, as yyyymm."""

    months: list[str] = []
    year, month = today.year, today.month
    for _ in range(count):
        months.append(f"{year:04d}{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return months


def _next_month_start(today: date) -> str:
    year, month = (
        (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    )
    return f"{year:04d}{month:02d}"


def fetch_catalog(
    *,
    base_url: str,
    timeout_seconds: float,
    transport: Any,
) -> tuple[str, CatalogEnvelope]:
    response = transport(
        method="GET",
        url=f"{base_url}{CATALOG_PATH}",
        headers={"Accept": "application/json"},
        json_body=None,
        timeout_seconds=timeout_seconds,
    )
    if response.status_code != 200:
        raise ProbeError(f"td_probe_catalog_http_failed:{response.status_code}")
    try:
        catalog = parse_catalog_envelope(response.json_body)
    except SharedSignalsV1Error as exc:
        raise ProbeError("td_probe_catalog_invalid") from exc
    return catalog.catalog_version, catalog


def validate_dataset_row(row: Mapping[str, Any], *, filter_field: str) -> dict[str, Any]:
    """Light per-dataset contract needed to issue one bounded query."""

    availability = row.get("availability")
    active = (
        isinstance(availability, Mapping)
        and availability.get("activation_states") == ["active"]
    )
    schema_major = row.get("schema_major")
    operators = row.get("filter_operators") if isinstance(row.get("filter_operators"), Mapping) else {}
    values = operators.get(filter_field)
    limits = row.get("limits")
    page_size = limits.get("max_page_size") if isinstance(limits, Mapping) else None
    default_fields = row.get("default_fields")
    return {
        "active": bool(active),
        "queryable": (
            type(schema_major) is int
            and schema_major > 0
            and isinstance(values, list)
            and "eq" in values
            and type(page_size) is int
            and page_size > 0
            and isinstance(default_fields, list)
            and filter_field in default_fields
        ),
        "schema_major": schema_major if type(schema_major) is int else None,
        "page_size": page_size if type(page_size) is int else None,
        "fields": tuple(default_fields) if isinstance(default_fields, list) else (),
    }


def make_pinned_client(
    *,
    transport: Any,
    base_url: str,
    catalog_version: str,
    dataset_ids: Sequence[str],
    timeout_seconds: float,
) -> SharedSignalsV1Client:
    return SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url=base_url,
            expected_catalog_version=catalog_version,
            dataset_ids=frozenset(dataset_ids),
            access_policy_id=ACCESS_POLICY_ID,
            catalog_version_policy="strict",
            timeout_seconds=timeout_seconds,
            cache_ttl_seconds=0,
        ),
        transport=transport,
    )


def _read_whole_month(
    client: SharedSignalsV1Client,
    *,
    dataset_id: str,
    schema_major: int,
    fields: Sequence[str],
    page_size: int,
    month: str,
) -> list[dict[str, Any]]:
    request = QueryRequest(
        dataset_id=dataset_id,
        schema_major=schema_major,
        fields=tuple(fields),
        filters={"month": {"eq": month}},
        limit=min(page_size, MAX_ROWS_PER_MONTH_QUERY),
    )
    paged = collect_query_pages(
        client=client,
        request=request,
        # Full-projection identity: duplicate detection then means truly
        # identical rows, which is the right semantics without a registry PK.
        identity_fields=tuple(fields),
        max_pages=MAX_PAGES_PER_MONTH_QUERY,
        max_rows=MAX_ROWS_PER_MONTH_QUERY,
    )
    return [dict(row) for row in paged.envelope.data]


def probe_event_dataset(
    client: SharedSignalsV1Client,
    *,
    dataset_id: str,
    spec: Mapping[str, Any],
    schema_major: int,
    page_size: int,
    days: Sequence[date],
) -> dict[str, Any]:
    """Row counts over recent announcement-day partitions."""

    daily_counts: dict[str, int] = {}
    total = 0
    # Registry primary key as both projection and pagination identity:
    # every row inside one ann_date partition shares the partition field
    # value, so a partition-only projection would collide on the very
    # first multi-row page (pagination_duplicate_row_identity).
    identity = tuple(spec["identity_fields"])
    for day in days:
        request = QueryRequest(
            dataset_id=dataset_id,
            schema_major=schema_major,
            fields=identity,
            filters={spec["partition_field"]: {"eq": f"{day:%Y%m%d}"}},
            limit=min(page_size, 1000),
        )
        paged = collect_query_pages(
            client=client,
            request=request,
            identity_fields=identity,
            max_pages=200,
            max_rows=200_000,
        )
        count = len(paged.envelope.data)
        daily_counts[day.isoformat()] = count
        total += count
    return {
        "partition_field": spec["partition_field"],
        "days_scanned": len(days),
        "rows_total": total,
        "empty_days": sum(1 for value in daily_counts.values() if value == 0),
        "daily_counts": daily_counts,
    }


def _publish_dates_at_or_after(rows: Sequence[Mapping[str, Any]], today: date) -> list[str]:
    hits: list[str] = []
    for row in rows:
        raw = str(row.get("publish_date") or "").strip()
        for fmt in ("%Y%m%d", "%Y-%m-%d"):
            try:
                if datetime.strptime(raw, fmt).date() >= today:
                    hits.append(raw)
                break
            except ValueError:
                continue
    return hits


def run_probe(
    *,
    token_file: str,
    out_dir: Path,
    lookback_days: int,
    base_url: str = DEFAULT_BASE_URL,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    as_of: datetime | None = None,
    transport_factory: Callable | None = None,
) -> dict[str, Any]:
    as_of = as_of or datetime.now(timezone.utc)
    today = as_of.astimezone().date()
    out_dir.mkdir(parents=True, exist_ok=True)

    if transport_factory is None:
        transport = load_transport(
            transport_id=TRANSPORT_ID, token_file=token_file, base_url=base_url
        )
    else:
        transport = transport_factory(TRANSPORT_ID, token_file=token_file, base_url=base_url)

    wanted = (*DATASET_SPECS, *MACRO_DATASETS)
    catalog_version, catalog = fetch_catalog(
        base_url=base_url, timeout_seconds=timeout_seconds, transport=transport
    )
    rows_by_id: dict[str, Mapping[str, Any]] = {
        row["dataset_id"]: row
        for row in catalog.data
        if isinstance(row, Mapping) and row.get("dataset_id") in wanted
    }
    missing = [dataset_id for dataset_id in wanted if dataset_id not in rows_by_id]
    if missing:
        raise ProbeError(f"td_probe_datasets_missing:{','.join(missing)}")

    contracts = {
        dataset_id: validate_dataset_row(
            rows_by_id[dataset_id],
            filter_field="ann_date"
            if dataset_id in DATASET_SPECS
            else "month",
        )
        for dataset_id in wanted
    }
    dead_event = [
        dataset_id
        for dataset_id in DATASET_SPECS
        if not (contracts[dataset_id]["active"] and contracts[dataset_id]["queryable"])
    ]
    if dead_event:
        raise ProbeError(f"td_probe_event_dataset_not_queryable:{','.join(dead_event)}")

    client = make_pinned_client(
        transport=transport,
        base_url=base_url,
        catalog_version=catalog_version,
        dataset_ids=wanted,
        timeout_seconds=timeout_seconds,
    )

    receipt: dict[str, Any] = {
        "research_only": True,
        "probe_id": PROBE_ID,
        "catalog_version": catalog_version,
        "as_of": as_of.isoformat(),
        "event_datasets": {},
        "macro_datasets": {},
    }
    event_days = [today - timedelta(days=offset) for offset in range(lookback_days)]
    for dataset_id, spec in DATASET_SPECS.items():
        contract = contracts[dataset_id]
        receipt["event_datasets"][dataset_id] = probe_event_dataset(
            client,
            dataset_id=dataset_id,
            spec=spec,
            schema_major=int(contract["schema_major"]),
            page_size=int(contract["page_size"]),
            days=event_days,
        )

    months = _month_starts(today, 2)
    for dataset_id in MACRO_DATASETS:
        contract = contracts[dataset_id]
        entry: dict[str, Any] = {"active": contract["active"]}
        if not contract["queryable"]:
            entry["skipped"] = "not_queryable_per_catalog"
            receipt["macro_datasets"][dataset_id] = entry
            continue
        entry["schema_major"] = contract["schema_major"]
        for month in months:
            rows = _read_whole_month(
                client,
                dataset_id=dataset_id,
                schema_major=int(contract["schema_major"]),
                fields=contract["fields"],
                page_size=int(contract["page_size"]),
                month=month,
            )
            entry[f"month_{month}"] = {"rows": len(rows), "first_rows": rows[:3]}
        if dataset_id == FORWARD_DATASET_ID:
            upcoming = _read_whole_month(
                client,
                dataset_id=dataset_id,
                schema_major=int(contract["schema_major"]),
                fields=contract["fields"],
                page_size=int(contract["page_size"]),
                month=_next_month_start(today),
            )
            future = _publish_dates_at_or_after(upcoming, today)
            entry["next_month_rows"] = len(upcoming)
            entry["future_publish_dates"] = sorted(set(future))
            entry["forward_capable"] = bool(future)
        receipt["macro_datasets"][dataset_id] = entry

    receipt_path = out_dir / "coverage_receipt.json"
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# TD coverage probe (research_only)",
        "",
        f"- as_of: {as_of.isoformat()}",
        f"- catalog_version: {catalog_version}",
        "",
    ]
    for dataset_id, stats in receipt["event_datasets"].items():
        lines.append(
            f"- {dataset_id}: rows={stats['rows_total']} over "
            f"{stats['days_scanned']}d ({stats['empty_days']} empty)"
        )
    for dataset_id, stats in receipt["macro_datasets"].items():
        parts = [
            f"{value['rows']} rows"
            for key, value in stats.items()
            if key.startswith("month_")
        ]
        if "forward_capable" in stats:
            parts.append(
                f"forward={'YES' if stats['forward_capable'] else 'no'} "
                f"(next-month rows={stats['next_month_rows']})"
            )
        if stats.get("skipped"):
            parts.append(f"skipped ({stats['skipped']})")
        lines.append(f"- {dataset_id}: " + ", ".join(parts))
    (out_dir / "coverage_view.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = parser.parse_args()
    receipt = run_probe(
        token_file=args.token_file,
        out_dir=args.out_dir,
        lookback_days=args.lookback_days,
        base_url=args.base_url,
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ProbeError as exc:
        print(f"PROBE_FAILED {exc}", file=sys.stderr)
        sys.exit(1)


# Re-exported for reuse; kept at the bottom to keep the top import block flat.
EVENT_DATASETS = tuple(DATASET_SPECS)
