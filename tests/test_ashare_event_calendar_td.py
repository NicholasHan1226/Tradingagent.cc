"""Offline tests for the TradingDatas-sourced full-market event calendar.

The fixtures emulate the shared fail-closed V1 wire contract (catalog
envelope, partitioned query pages, metadata identity) the same way the
moneyflow evidence tests do, so every code path stays offline.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any

from shared.data.sharedsignals_v1 import HTTPResponse
from shared.data.tradingdatas_pagination import PaginationContractError

from Ashare.event_catalyst_adapter import (
    DISCLOSURE_DATE_DATASET_ID,
    SHARE_FLOAT_DATASET_ID,
    catalyst_entries_from_calendar_document,
)
from Ashare.event_calendar_tradingdatas import (
    CALENDAR_ID,
    DATASET_SPECS,
    TRANSPORT_ID,
    TradingDatasCalendarError,
    build_entries,
    fetch_validated_catalog,
    run,
)

CATALOG = "cat-fixture-v1"
AS_OF = date(2026, 8, 23)

DISCLOSURE_FIELDS = list(DATASET_SPECS[DISCLOSURE_DATE_DATASET_ID]["fields"])
SHARE_FLOAT_FIELDS = list(DATASET_SPECS[SHARE_FLOAT_DATASET_ID]["fields"])


def _catalog_row(
    dataset_id: str,
    fields: list[str],
    identity_fields: list[str],
    *,
    active: bool = True,
    max_page_size: int = 2,
    with_eq_operator: bool = True,
) -> dict[str, Any]:
    operators: dict[str, Any] = {
        name: ["eq", "in"] if with_eq_operator else ["in"]
        for name in fields
    }
    return {
        "dataset_id": dataset_id,
        "schema_major": 1,
        "default_fields": list(fields),
        "default_order": [f"{identity_fields[-1]}:asc"],
        "filter_operators": operators,
        "limits": {"max_page_size": max_page_size},
        "identity_fields": list(identity_fields),
        "availability": {
            "activation_states": ["active" if active else "paused"]
        },
    }


def _default_catalog_rows(
    *,
    max_page_size: int = 2,
    active_share_float: bool = True,
    with_eq_operator: bool = True,
) -> list[dict[str, Any]]:
    return [
        _catalog_row(
            DISCLOSURE_DATE_DATASET_ID,
            DISCLOSURE_FIELDS,
            list(DATASET_SPECS[DISCLOSURE_DATE_DATASET_ID]["identity_fields"]),
            max_page_size=max_page_size,
            with_eq_operator=with_eq_operator,
        ),
        _catalog_row(
            SHARE_FLOAT_DATASET_ID,
            SHARE_FLOAT_FIELDS,
            list(DATASET_SPECS[SHARE_FLOAT_DATASET_ID]["identity_fields"]),
            active=active_share_float,
            max_page_size=max_page_size,
            with_eq_operator=with_eq_operator,
        ),
    ]


def _metadata() -> dict[str, Any]:
    return {
        "state": "ready",
        "degraded": False,
        "freshness": {"state": "fresh", "stale": False},
        "quality": {"state": "valid"},
        "lineage": {
            "complete": True,
            "provider_neutral": True,
            "providers": ["fixture-provider"],
            "transport_service": "fixture-v1",
        },
        "receipt_id": "receipt-calendar-1",
        "data_through": "2026-08-23T10:20:00+08:00",
        "observed_at": "2026-08-23T10:25:00+08:00",
        "reasons": [],
    }


def _lockup_row(
    symbol: str,
    ann_date: str,
    float_date: str,
    *,
    holder: str = "全国社会保障基金理事会",
    share_type: str = "定向增发机构配售",
    float_ratio: float = 5.0,
) -> dict[str, Any]:
    return {
        "ts_code": symbol,
        "ann_date": ann_date,
        "float_date": float_date,
        "float_share": 10_000_000.0,
        "float_ratio": float_ratio,
        "holder_name": holder,
        "share_type": share_type,
    }


def _disclosure_row(
    symbol: str,
    ann_date: str,
    pre_date: str | None,
    *,
    end_date: str = "20260630",
) -> dict[str, Any]:
    return {
        "ts_code": symbol,
        "ann_date": ann_date,
        "end_date": end_date,
        "pre_date": pre_date,
        "actual_date": None,
    }


class _Transport:
    """Fixture transport serving catalog + ann_date-partitioned pages."""

    def __init__(
        self,
        *,
        catalog_rows: list[dict[str, Any]] | None = None,
        rows_by_dataset: dict[str, list[dict[str, Any]]] | None = None,
        catalog_status: int = 200,
        duplicate_identity_across_pages: bool = False,
    ) -> None:
        self.catalog_rows = (
            catalog_rows
            if catalog_rows is not None
            else _default_catalog_rows()
        )
        self.rows_by_dataset = rows_by_dataset or {}
        self.catalog_status = catalog_status
        self.duplicate_identity_across_pages = duplicate_identity_across_pages
        self.query_bodies: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> HTTPResponse:
        if kwargs["method"] == "GET":
            return HTTPResponse(
                self.catalog_status,
                {
                    "api_version": "v1",
                    "catalog_version": CATALOG,
                    "request_id": "catalog-1",
                    "data": copy.deepcopy(self.catalog_rows),
                },
            )
        body = kwargs["json_body"]
        assert body is not None
        self.query_bodies.append(copy.deepcopy(body))
        dataset_id = body["dataset_id"]
        filters = body.get("filters") or {}
        partition_value = next(iter(filters.values()), {}).get("eq")
        all_rows = [
            row
            for row in self.rows_by_dataset.get(dataset_id, [])
            if row.get("ann_date") == partition_value
        ]
        cursor = body.get("cursor")
        index = int(cursor.split(":", 1)[1]) if cursor else 0
        page = copy.deepcopy(all_rows[index : index + int(body["limit"])])
        if (
            self.duplicate_identity_across_pages
            and cursor
            and dataset_id == SHARE_FLOAT_DATASET_ID
            and page
        ):
            page[0] = copy.deepcopy(all_rows[index - 1])
        next_index = index + len(page)
        next_cursor = f"0:{next_index}" if next_index < len(all_rows) else None
        return HTTPResponse(
            200,
            {
                "api_version": "v1",
                "catalog_version": CATALOG,
                "request_id": f"query-{len(self.query_bodies)}",
                "dataset_id": dataset_id,
                "data": page,
                "next_cursor": next_cursor,
                "metadata": _metadata(),
            },
        )


def _factory(transport: _Transport):
    def _build(_transport_id, *, token_file, base_url):
        assert _transport_id == TRANSPORT_ID
        return transport

    return _build


def _run(transport: _Transport, out_dir: Path, **overrides: Any) -> dict[str, Any]:
    return run(
        token_file="/etc/tradingagent/tradingdatas-read.token",
        out_dir=out_dir,
        lookback_days=overrides.pop("lookback_days", 3),
        as_of=AS_OF,
        transport_factory=_factory(transport),
        **overrides,
    )


class TestBuildEntries(unittest.TestCase):
    def test_filters_future_mainboard_and_shapes_ids(self) -> None:
        rows_by_dataset = {
            SHARE_FLOAT_DATASET_ID: [
                _lockup_row("600000.SH", "20260801", "20260910"),
                _lockup_row("000001.SZ", "20260802", "20260915"),
                _lockup_row(
                    "002415.SZ",
                    "20260803",
                    "20260918",
                    holder="香港中央结算有限公司",
                ),
                _lockup_row("300750.SZ", "20260803", "20260920"),
                _lockup_row("688981.SH", "20260803", "20260921"),
                _lockup_row("830799.BJ", "20260803", "20260922"),
                _lockup_row("600000.SH", "20260731", "20260801"),  # not future
                _lockup_row("600000.SH", "20260805", "20260701"),  # float<ann
            ],
            DISCLOSURE_DATE_DATASET_ID: [
                _disclosure_row("600519.SH", "20260810", "20260828"),
                _disclosure_row("601398.SH", "20260811", "20260823"),  # today
                _disclosure_row("000002.SZ", "20260812", None),
            ],
        }
        entries, stats = build_entries(rows_by_dataset=rows_by_dataset, as_of=AS_OF)

        lockups = [e for e in entries if e["event_type"] == "lockup_expiry"]
        disclosures = [
            e for e in entries if e["event_type"] == "earnings_disclosure"
        ]
        self.assertEqual(
            [e["symbol"] for e in lockups],
            ["600000.SH", "000001.SZ", "002415.SZ"],
        )
        self.assertEqual(lockups[0]["event_id"], "lockup:600000.SH:2026-09-10:全国社会保障基金理事会:定向增发机构配售")
        self.assertEqual(lockups[0]["impact_direction"], "negative")
        self.assertEqual(
            lockups[0]["source_ref"], "td-v1:cn.dataset.share_float:ann=20260801"
        )
        self.assertEqual([e["symbol"] for e in disclosures], ["600519.SH"])
        self.assertEqual(disclosures[0]["entity"], "20260630")
        self.assertEqual(disclosures[0]["impact_direction"], "unclear")

        share_skips = stats["rows_skipped"][SHARE_FLOAT_DATASET_ID]
        self.assertEqual(stats["entries_emitted"][SHARE_FLOAT_DATASET_ID], 3)
        self.assertEqual(share_skips["non_mainboard"], 3)
        self.assertEqual(share_skips["not_future"], 1)
        self.assertEqual(share_skips["float_before_ann"], 1)
        disc_skips = stats["rows_skipped"][DISCLOSURE_DATE_DATASET_ID]
        self.assertEqual(disc_skips["empty_pre_date"], 1)
        self.assertEqual(disc_skips["not_future"], 1)

        dates = [e["scheduled_date"] for e in entries]
        self.assertEqual(dates, sorted(dates))

    def test_same_symbol_two_holders_both_kept(self) -> None:
        rows = {
            SHARE_FLOAT_DATASET_ID: [
                _lockup_row("600000.SH", "20260801", "20260910"),
                _lockup_row(
                    "600000.SH",
                    "20260801",
                    "20260910",
                    holder="中国证券金融股份有限公司",
                    share_type="股权激励限售股份",
                ),
            ],
            DISCLOSURE_DATE_DATASET_ID: [],
        }
        entries, stats = build_entries(rows_by_dataset=rows, as_of=AS_OF)
        self.assertEqual(len(entries), 2)
        self.assertEqual(
            stats["entries_emitted"][SHARE_FLOAT_DATASET_ID], 2
        )

    def test_reannouncement_duplicate_event_id_fails_closed(self) -> None:
        rows = {
            SHARE_FLOAT_DATASET_ID: [
                _lockup_row("600000.SH", "20260801", "20260910"),
                # Same symbol/date/holder/type announced again on a later
                # day: the registry PK differs but the calendar id collides.
                _lockup_row("600000.SH", "20260802", "20260910"),
            ],
            DISCLOSURE_DATE_DATASET_ID: [],
        }
        with self.assertRaisesRegex(
            TradingDatasCalendarError, "td_calendar_duplicate_event_id"
        ):
            build_entries(rows_by_dataset=rows, as_of=AS_OF)


class TestAdapterCompatibility(unittest.TestCase):
    def test_document_passes_real_adapter(self) -> None:
        rows = {
            SHARE_FLOAT_DATASET_ID: [
                _lockup_row("600000.SH", "20260801", "20260910"),
            ],
            DISCLOSURE_DATE_DATASET_ID: [
                _disclosure_row("600519.SH", "20260810", "20260828"),
            ],
        }
        entries, _ = build_entries(rows_by_dataset=rows, as_of=AS_OF)
        minted = catalyst_entries_from_calendar_document(
            {"calendar_id": CALENDAR_ID, "entries": entries}
        )
        self.assertEqual(len(minted), len(entries))
        for entry in minted:
            self.assertTrue(entry.event_id.startswith(f"{CALENDAR_ID}:"))


class TestFetchValidatedCatalog(unittest.TestCase):
    def test_happy_path_returns_contracts(self) -> None:
        transport = _Transport()
        version, contracts = fetch_validated_catalog(
            base_url="http://127.0.0.1:18082",
            timeout_seconds=5.0,
            transport=transport,
        )
        self.assertEqual(version, CATALOG)
        self.assertEqual(
            contracts[SHARE_FLOAT_DATASET_ID]["page_size"], 2
        )
        self.assertEqual(contracts[SHARE_FLOAT_DATASET_ID]["schema_major"], 1)

    def test_paused_dataset_fails_closed(self) -> None:
        transport = _Transport(
            catalog_rows=_default_catalog_rows(active_share_float=False)
        )
        with self.assertRaisesRegex(
            TradingDatasCalendarError, "td_calendar_dataset_inactive"
        ):
            fetch_validated_catalog(
                base_url="http://127.0.0.1:18082",
                timeout_seconds=5.0,
                transport=transport,
            )

    def test_missing_eq_operator_fails_closed(self) -> None:
        transport = _Transport(
            catalog_rows=_default_catalog_rows(with_eq_operator=False)
        )
        with self.assertRaisesRegex(
            TradingDatasCalendarError, "td_calendar_filters_invalid"
        ):
            fetch_validated_catalog(
                base_url="http://127.0.0.1:18082",
                timeout_seconds=5.0,
                transport=transport,
            )

    def test_http_failure_fails_closed(self) -> None:
        transport = _Transport(catalog_status=503)
        with self.assertRaisesRegex(
            TradingDatasCalendarError, "td_calendar_catalog_http_failed:503"
        ):
            fetch_validated_catalog(
                base_url="http://127.0.0.1:18082",
                timeout_seconds=5.0,
                transport=transport,
            )


class TestRunEndToEnd(unittest.TestCase):
    def test_full_pipeline_partitions_pagination_and_outputs(self) -> None:
        rows = {
            SHARE_FLOAT_DATASET_ID: [
                _lockup_row("600000.SH", "20260822", "20260910"),
                _lockup_row(
                    "000001.SZ", "20260822", "20260912", holder="汇金公司"
                ),
                _lockup_row("600519.SH", "20260823", "20260915"),
                _lockup_row("300750.SZ", "20260823", "20260920"),
            ],
            DISCLOSURE_DATE_DATASET_ID: [
                _disclosure_row("600519.SH", "20260823", "20260930"),
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "td-calendar"
            transport = _Transport(rows_by_dataset=rows)
            summary = _run(transport, out_dir)

            self.assertEqual(summary["research_only"], True)
            self.assertEqual(summary["calendar_id"], CALENDAR_ID)
            self.assertEqual(summary["lookback_days"], 3)
            self.assertEqual(summary["entries_total"], 4)
            self.assertEqual(
                summary["entries_by_type"]["lockup_expiry"], 3
            )
            self.assertEqual(
                summary["entries_by_type"]["earnings_disclosure"], 1
            )

            doc = json.loads(
                (out_dir / "calendar_doc.json").read_text(encoding="utf-8")
            )
            self.assertEqual(doc["calendar_id"], CALENDAR_ID)
            self.assertEqual(len(doc["entries"]), 4)
            view = (out_dir / "calendar_view.md").read_text(encoding="utf-8")
            self.assertIn("research_only", view)
            self.assertIn("## 2026-09", view)

            # Every partition query must target one exact ann_date.
            bodies = transport.query_bodies
            self.assertTrue(bodies)
            for body in bodies:
                (filters,) = body["filters"].values()
                self.assertIn(filters["eq"], {"20260821", "20260822", "20260823"})

    def test_duplicate_identity_across_pages_fails_closed(self) -> None:
        rows = {
            SHARE_FLOAT_DATASET_ID: [
                _lockup_row("600000.SH", "20260823", "20260910"),
                _lockup_row(
                    "000001.SZ", "20260823", "20260912", holder="汇金公司"
                ),
                _lockup_row("600519.SH", "20260823", "20260915"),
            ],
            DISCLOSURE_DATE_DATASET_ID: [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PaginationContractError):
                _run(
                    _Transport(
                        rows_by_dataset=rows,
                        duplicate_identity_across_pages=True,
                    ),
                    Path(tmp) / "td-calendar-dup",
                )

    def test_no_future_entries_fails_closed(self) -> None:
        rows = {
            SHARE_FLOAT_DATASET_ID: [
                _lockup_row("600000.SH", "20260821", "20260822"),  # past
            ],
            DISCLOSURE_DATE_DATASET_ID: [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                TradingDatasCalendarError, "td_calendar_no_future_entries"
            ):
                _run(_Transport(rows_by_dataset=rows), Path(tmp) / "td-empty")


if __name__ == "__main__":
    unittest.main()
