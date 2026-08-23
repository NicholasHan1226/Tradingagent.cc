"""Full-market A-share forward event calendar sourced from TradingDatas.

Research-only.  Server-side counterpart of ``event_calendar_doc``: instead
of the per-sample Tushare cache it reads two activated provider-native
datasets (``cn.dataset.share_float`` / ``cn.dataset.disclosure_date``)
through the shared fail-closed V1 client, so the forward calendar covers
the whole mainboard market without per-symbol Tushare loops or row caps.

The output document is byte-compatible with
``Ashare/event_catalyst_adapter.catalyst_entries_from_calendar_document``
(the same contract ``event_calendar_doc`` emits), so downstream shadow
tooling can consume either interchangeably.  Coverage notes:

* disclosure appointments only appear once the exchange publishes them
  (historically roughly one month ahead of the report period);
* lockup expiries are announced weeks-to-months ahead; both are scanned by
  ``ann_date`` partitions over a bounded lookback window and filtered to
  strictly-future scheduled dates;
* LPR forward dates stay in the Tushare-cache variant; they are not part
  of this dataset-backed document.

Requires line-of-sight to the TradingDatas read API (on the release host
this is ``http://127.0.0.1:18082``) and a separately provisioned 0600
bearer token file; it cannot run on GitHub-hosted runners and never reads
or writes any trading surface.

Usage::

    python3 Ashare/event_calendar_tradingdatas.py \
        --token-file /etc/tradingagent/tradingdatas-read.token \
        --out-dir /tmp/ashare_event_research_td \
        [--base-url http://127.0.0.1:18082] [--lookback-days 270]
        [--as-of YYYY-MM-DD] [--timeout-seconds 20]
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from shared.data.sharedsignals_v1 import (
    CatalogContractError,
    CatalogEnvelope,
    HTTPStatusError,
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
    SharedSignalsV1Error,
    TransportNotConfigured,
    parse_catalog_envelope,
)
from shared.data.tradingdatas_pagination import (
    PaginationContractError,
    collect_query_pages,
)
from shared.data.tradingdatas_transport import (
    RuntimeGateConfigurationError,
    TradingDatasAuthenticationError,
    build_runtime_transport,
)
from shared.universe.policy import is_mainboard_tradable

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from Ashare.event_catalyst_adapter import (  # noqa: E402
    DISCLOSURE_DATE_DATASET_ID,
    SHARE_FLOAT_DATASET_ID,
)

CALENDAR_ID = "ashare-event-calendar-td-v1"
DEFAULT_BASE_URL = "http://127.0.0.1:18082"
ACCESS_POLICY_ID = "tradingagent-read-v1"
TRANSPORT_ID = "http-json-v1"
DEFAULT_LOOKBACK_DAYS = 270
DEFAULT_TIMEOUT_SECONDS = 20.0
CATALOG_PATH = "/v1/catalog"

# Per-partition budgets.  One partition is a single ann_date; the collector
# contract allows up to 10k rows per attempt, so these bounds are generous
# against observed full-market volumes while staying far below the shared
# hard ceilings (1000 pages / 5M rows).
MAX_PAGES_PER_PARTITION = 200
MAX_ROWS_PER_PARTITION = 200_000

DATASET_SPECS: Mapping[str, Mapping[str, Any]] = {
    DISCLOSURE_DATE_DATASET_ID: {
        "fields": ("ts_code", "ann_date", "end_date", "pre_date", "actual_date"),
        # Registry primary key: ann_date is constant inside one partition.
        "identity_fields": ("ann_date", "end_date", "ts_code"),
        "partition_field": "ann_date",
    },
    SHARE_FLOAT_DATASET_ID: {
        "fields": (
            "ts_code",
            "ann_date",
            "float_date",
            "float_share",
            "float_ratio",
            "holder_name",
            "share_type",
        ),
        "identity_fields": (
            "ann_date",
            "float_date",
            "ts_code",
            "holder_name",
            "share_type",
        ),
        "partition_field": "ann_date",
    },
}


class TradingDatasCalendarError(ValueError):
    """Fail-closed calendar source failure with a stable reason code."""


def _compact(day: date) -> str:
    return day.strftime("%Y%m%d")


def _parse_day(raw: str) -> date:
    return datetime.strptime(raw.strip(), "%Y%m%d").date()


def load_transport(
    *,
    transport_id: str,
    token_file: Path | str,
    base_url: str,
) -> Any:
    if not Path(token_file).is_absolute():
        raise TradingDatasCalendarError("td_calendar_token_path_not_absolute")
    try:
        return build_runtime_transport(
            transport_id,
            token_file=token_file,
            base_url=base_url,
        )
    except RuntimeGateConfigurationError as exc:
        raise TradingDatasCalendarError(f"td_calendar_token_invalid:{exc}") from exc


def fetch_validated_catalog(
    *,
    base_url: str,
    timeout_seconds: float,
    transport: Any,
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Read the catalog and validate both datasets are actively queryable."""

    response = transport(
        method="GET",
        url=f"{base_url}{CATALOG_PATH}",
        headers={"Accept": "application/json"},
        json_body=None,
        timeout_seconds=timeout_seconds,
    )
    if response.status_code != 200:
        raise TradingDatasCalendarError(
            f"td_calendar_catalog_http_failed:{response.status_code}"
        )
    try:
        catalog: CatalogEnvelope = parse_catalog_envelope(response.json_body)
    except SharedSignalsV1Error as exc:
        raise TradingDatasCalendarError("td_calendar_catalog_invalid") from exc

    contracts: dict[str, dict[str, Any]] = {}
    for dataset_id, spec in DATASET_SPECS.items():
        matches = [
            row
            for row in catalog.data
            if isinstance(row, Mapping) and row.get("dataset_id") == dataset_id
        ]
        if len(matches) != 1:
            raise TradingDatasCalendarError(
                f"td_calendar_dataset_missing:{dataset_id}"
            )
        row = matches[0]
        availability = row.get("availability")
        if (
            not isinstance(availability, Mapping)
            or availability.get("activation_states") != ["active"]
        ):
            raise TradingDatasCalendarError(
                f"td_calendar_dataset_inactive:{dataset_id}"
            )
        schema_major = row.get("schema_major")
        if type(schema_major) is not int or schema_major <= 0:
            raise TradingDatasCalendarError(
                f"td_calendar_schema_invalid:{dataset_id}"
            )
        default_fields = row.get("default_fields")
        if (
            not isinstance(default_fields, list)
            or not set(spec["fields"]).issubset(default_fields)
        ):
            raise TradingDatasCalendarError(
                f"td_calendar_fields_invalid:{dataset_id}"
            )
        operators = row.get("filter_operators")
        if not isinstance(operators, Mapping):
            raise TradingDatasCalendarError(
                f"td_calendar_filters_invalid:{dataset_id}"
            )
        values = operators.get(spec["partition_field"])
        if not isinstance(values, list) or "eq" not in values:
            raise TradingDatasCalendarError(
                f"td_calendar_filters_invalid:{dataset_id}"
            )
        limits = row.get("limits")
        page_size = (
            limits.get("max_page_size") if isinstance(limits, Mapping) else None
        )
        if type(page_size) is not int or page_size <= 0:
            raise TradingDatasCalendarError(
                f"td_calendar_limit_invalid:{dataset_id}"
            )
        contracts[dataset_id] = {
            "schema_major": schema_major,
            "page_size": page_size,
        }
    return catalog.catalog_version, contracts


def make_client(
    *,
    transport: Any,
    base_url: str,
    catalog_version: str,
    timeout_seconds: float,
) -> SharedSignalsV1Client:
    """Pin the client to the exact catalog version validated above."""

    return SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url=base_url,
            expected_catalog_version=catalog_version,
            dataset_ids=frozenset(DATASET_SPECS),
            access_policy_id=ACCESS_POLICY_ID,
            catalog_version_policy="strict",
            timeout_seconds=timeout_seconds,
            cache_ttl_seconds=0,
        ),
        transport=transport,
    )


def scan_partition_days(
    *,
    client: SharedSignalsV1Client,
    dataset_id: str,
    spec: Mapping[str, Any],
    days: Sequence[date],
    schema_major: int,
    page_size: int,
) -> list[dict[str, Any]]:
    """Collect every row announced on each partition day, fail-closed."""

    rows: list[dict[str, Any]] = []
    for day in days:
        request = QueryRequest(
            dataset_id=dataset_id,
            schema_major=schema_major,
            fields=tuple(spec["fields"]),
            filters={spec["partition_field"]: {"eq": _compact(day)}},
            limit=page_size,
        )
        paged = collect_query_pages(
            client=client,
            request=request,
            identity_fields=tuple(spec["identity_fields"]),
            max_pages=MAX_PAGES_PER_PARTITION,
            max_rows=MAX_ROWS_PER_PARTITION,
        )
        rows.extend(dict(row) for row in paged.envelope.data)
    return rows


def _entry(
    event_id: str,
    event_type: str,
    scheduled: date,
    impact_direction: str,
    source_ref: str,
    entity: str | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "event_id": event_id,
        "event_type": event_type,
        "scheduled_date": scheduled.isoformat(),
        "date_confidence": "hard_date",
        "impact_direction": impact_direction,
        "source_ref": source_ref,
    }
    if entity is not None:
        entry["entity"] = entity
    if symbol is not None:
        entry["symbol"] = symbol
    return entry


def build_entries(
    *,
    rows_by_dataset: Mapping[str, Sequence[Mapping[str, Any]]],
    as_of: date,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Map raw provider rows into adapter-shaped future mainboard entries."""

    entries: list[dict[str, Any]] = []
    stats: dict[str, Any] = {
        "rows_fetched": {},
        "rows_skipped": {},
        "entries_emitted": {},
    }
    for dataset_id, rows in sorted(rows_by_dataset.items()):
        kept = skipped = 0
        skip_reasons: dict[str, int] = {}
        for raw in rows:
            symbol = raw.get("ts_code")
            ann_raw = raw.get("ann_date")
            if (
                not isinstance(symbol, str)
                or not isinstance(ann_raw, str)
                or not symbol
                or not ann_raw.strip()
            ):
                skipped += 1
                skip_reasons["bad_identity"] = (
                    skip_reasons.get("bad_identity", 0) + 1
                )
                continue
            if not is_mainboard_tradable(symbol):
                skipped += 1
                skip_reasons["non_mainboard"] = (
                    skip_reasons.get("non_mainboard", 0) + 1
                )
                continue
            try:
                ann_day = _parse_day(ann_raw)
            except ValueError:
                skipped += 1
                skip_reasons["bad_ann_date"] = (
                    skip_reasons.get("bad_ann_date", 0) + 1
                )
                continue
            source_ref = f"td-v1:{dataset_id}:ann={ann_raw.strip()}"
            if dataset_id == DISCLOSURE_DATE_DATASET_ID:
                pre_raw = raw.get("pre_date")
                end_date = raw.get("end_date") or ""
                if not isinstance(pre_raw, str) or len(pre_raw.strip()) < 8:
                    skipped += 1
                    skip_reasons["empty_pre_date"] = (
                        skip_reasons.get("empty_pre_date", 0) + 1
                    )
                    continue
                try:
                    pre_day = _parse_day(pre_raw)
                except ValueError:
                    skipped += 1
                    skip_reasons["bad_pre_date"] = (
                        skip_reasons.get("bad_pre_date", 0) + 1
                    )
                    continue
                if pre_day <= as_of:
                    skipped += 1
                    skip_reasons["not_future"] = (
                        skip_reasons.get("not_future", 0) + 1
                    )
                    continue
                entries.append(
                    _entry(
                        f"disc:{symbol}:{end_date}:{pre_day.isoformat()}:{ann_day.isoformat()}",
                        "earnings_disclosure",
                        pre_day,
                        "unclear",
                        source_ref,
                        entity=str(end_date),
                        symbol=symbol,
                    )
                )
            elif dataset_id == SHARE_FLOAT_DATASET_ID:
                float_raw = raw.get("float_date")
                if not isinstance(float_raw, str):
                    skipped += 1
                    skip_reasons["bad_float_date"] = (
                        skip_reasons.get("bad_float_date", 0) + 1
                    )
                    continue
                try:
                    float_day = _parse_day(float_raw)
                except ValueError:
                    skipped += 1
                    skip_reasons["bad_float_date"] = (
                        skip_reasons.get("bad_float_date", 0) + 1
                    )
                    continue
                if float_day < ann_day:
                    skipped += 1
                    skip_reasons["float_before_ann"] = (
                        skip_reasons.get("float_before_ann", 0) + 1
                    )
                    continue
                if float_day <= as_of:
                    skipped += 1
                    skip_reasons["not_future"] = (
                        skip_reasons.get("not_future", 0) + 1
                    )
                    continue
                holder = str(raw.get("holder_name") or "")
                share_type = str(raw.get("share_type") or "")
                entries.append(
                    _entry(
                        f"lockup:{symbol}:{float_day.isoformat()}:"
                        f"{holder}:{share_type}",
                        "lockup_expiry",
                        float_day,
                        "negative",
                        source_ref,
                        entity=holder,
                        symbol=symbol,
                    )
                )
            else:  # pragma: no cover - DATASET_SPECS is closed
                raise TradingDatasCalendarError(
                    f"td_calendar_dataset_unknown:{dataset_id}"
                )
            kept += 1
        stats["rows_fetched"][dataset_id] = len(rows)
        stats["rows_skipped"][dataset_id] = {
            "total": skipped,
            **skip_reasons,
        }
        stats["entries_emitted"][dataset_id] = kept

    seen: set[str] = set()
    for entry in entries:
        if entry["event_id"] in seen:
            raise TradingDatasCalendarError(
                f"td_calendar_duplicate_event_id:{entry['event_id']}"
            )
        seen.add(entry["event_id"])
    entries.sort(key=lambda e: (e["scheduled_date"], e["event_id"]))
    return entries, stats


def write_outputs(
    *,
    out_dir: Path,
    entries: Sequence[Mapping[str, Any]],
    stats: Mapping[str, Any],
    as_of: date,
) -> tuple[Path, Path]:
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    doc_path = out_dir / "calendar_doc.json"
    doc_path.write_text(
        json.dumps(
            {"calendar_id": CALENDAR_ID, "entries": list(entries)},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )

    by_month: dict[str, dict[str, int]] = {}
    for entry in entries:
        month = entry["scheduled_date"][:7]
        bucket = by_month.setdefault(month, {})
        kind = "disclosures" if entry["event_type"] == "earnings_disclosure" else "lockups"
        bucket[kind] = bucket.get(kind, 0) + 1

    lines = [
        f"# A-share forward event calendar, full mainboard market "
        f"(as_of {as_of.isoformat()}, research_only)",
        "",
        "- Source: TradingDatas provider-native datasets "
        f"({DISCLOSURE_DATE_DATASET_ID}, {SHARE_FLOAT_DATASET_ID}).",
        "- 财报预约由交易所提前约一个月公布；解禁公告一般提前数周至数月。",
        "- 本文件为研究产物，不构成任何预测概率或投资建议。",
        "",
    ]
    for month in sorted(by_month):
        lines.append(f"## {month}")
        lines.append("")
        lines.append("| 类型 | 财报披露家数 | 解禁笔数 |")
        lines.append("|---|---|---|")
        counts = by_month[month]
        lines.append(
            f"| 合计 | {counts.get('disclosures', 0)} | {counts.get('lockups', 0)} |"
        )
        lines.append("")
    view_path = out_dir / "calendar_view.md"
    view_path.write_text("\n".join(lines), encoding="utf-8")
    return doc_path, view_path


def run(
    *,
    token_file: Path | str,
    out_dir: Path,
    base_url: str = DEFAULT_BASE_URL,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    as_of: date | None = None,
    transport_factory: Any | None = None,
) -> dict[str, Any]:
    if as_of is None:
        now = datetime.now(timezone.utc).astimezone()
        as_of = now.date()
    if type(lookback_days) is not int or lookback_days <= 0:
        raise TradingDatasCalendarError("td_calendar_lookback_invalid")

    factory = transport_factory or load_transport
    transport = factory(
        TRANSPORT_ID,
        token_file=token_file,
        base_url=base_url,
    )
    catalog_version, contracts = fetch_validated_catalog(
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        transport=transport,
    )
    client = make_client(
        transport=transport,
        base_url=base_url,
        catalog_version=catalog_version,
        timeout_seconds=timeout_seconds,
    )

    days = [as_of - timedelta(days=offset) for offset in range(lookback_days)]
    rows_by_dataset: dict[str, list[dict[str, Any]]] = {}
    for dataset_id, spec in DATASET_SPECS.items():
        contract = contracts[dataset_id]
        rows_by_dataset[dataset_id] = scan_partition_days(
            client=client,
            dataset_id=dataset_id,
            spec=spec,
            days=days,
            schema_major=contract["schema_major"],
            page_size=contract["page_size"],
        )

    entries, stats = build_entries(rows_by_dataset=rows_by_dataset, as_of=as_of)
    if not entries:
        raise TradingDatasCalendarError("td_calendar_no_future_entries")

    doc_path, view_path = write_outputs(
        out_dir=out_dir,
        entries=entries,
        stats=stats,
        as_of=as_of,
    )

    from Ashare.event_catalyst_adapter import (
        catalyst_entries_from_calendar_document,
    )

    minted = catalyst_entries_from_calendar_document(
        json.loads(doc_path.read_text(encoding="utf-8"))
    )
    if len(minted) != len(entries):
        raise TradingDatasCalendarError("td_calendar_adapter_roundtrip_mismatch")

    type_counts: dict[str, int] = {}
    for entry in entries:
        type_counts[entry["event_type"]] = type_counts.get(entry["event_type"], 0) + 1
    summary = {
        "research_only": True,
        "calendar_id": CALENDAR_ID,
        "catalog_version": catalog_version,
        "as_of": as_of.isoformat(),
        "lookback_days": lookback_days,
        "scanned_partitions_per_dataset": lookback_days,
        "entries_total": len(entries),
        "entries_by_type": type_counts,
        **stats,
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    print(f"saved -> {doc_path}")
    print(f"saved -> {view_path}")
    print(f"adapter_validation_ok entries={len(minted)}")
    return summary


def _arg_value(flag: str) -> str | None:
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else None


def main() -> int:
    token_file = _arg_value("--token-file")
    out_dir = _arg_value("--out-dir")
    if not token_file or not out_dir:
        raise TradingDatasCalendarError("td_calendar_usage_missing_args")
    as_of_raw = _arg_value("--as-of")
    as_of = (
        datetime.strptime(as_of_raw, "%Y-%m-%d").date() if as_of_raw else None
    )
    lookback_raw = _arg_value("--lookback-days")
    timeout_raw = _arg_value("--timeout-seconds")
    run(
        token_file=token_file,
        out_dir=Path(out_dir),
        base_url=_arg_value("--base-url") or DEFAULT_BASE_URL,
        lookback_days=int(lookback_raw) if lookback_raw else DEFAULT_LOOKBACK_DAYS,
        timeout_seconds=(
            float(timeout_raw) if timeout_raw else DEFAULT_TIMEOUT_SECONDS
        ),
        as_of=as_of,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (
        TradingDatasCalendarError,
        CatalogContractError,
        PaginationContractError,
        SharedSignalsV1Error,
        TradingDatasAuthenticationError,
        RuntimeGateConfigurationError,
        TransportNotConfigured,
        HTTPStatusError,
        OSError,
        ValueError,
    ) as exc:
        print(f"EVENT_CALENDAR_TD_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
