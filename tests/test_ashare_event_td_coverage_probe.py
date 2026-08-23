"""Offline tests for the read-only TradingDatas coverage probe.

The fixture transport emulates the shared V1 wire contract (catalog
envelope, ann_date-partitioned event pages, month-partitioned macro pages)
the same way the TD calendar tests do; everything stays offline.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from typing import Any

from Ashare.event_calendar_tradingdatas import (
    DATASET_SPECS,
    DISCLOSURE_DATE_DATASET_ID,
    SHARE_FLOAT_DATASET_ID,
)
from Ashare.event_td_coverage_probe import (
    FORWARD_DATASET_ID,
    MACRO_DATASETS,
    ProbeError,
    _publish_dates_at_or_after,
    run_probe,
)

AS_OF = datetime.fromisoformat("2026-08-23T10:00:00+08:00")
TODAY = AS_OF.astimezone().date()

MACRO_FIELDS = {
    "cn.dataset.cn_cpi": ("month", "nt_val", "nt_yoy"),
    "cn.dataset.cn_pmi": ("month", "pmi010000"),
    "cn.dataset.cn_gdp": ("month", "quarter", "gdp_yoy"),
    "cn.dataset.cn_m": ("month", "m0_yoy", "m1_yoy", "m2_yoy"),
    "cn.dataset.cn_schedule": ("month", "publish_date", "title"),
}


def _catalog_row(
    dataset_id: str,
    fields: list[str],
    *,
    filter_field: str,
    active: bool = True,
    queryable: bool = True,
    max_page_size: int = 2,
) -> dict[str, Any]:
    operators: dict[str, Any] = {
        name: ["eq", "in"] if queryable else ["in"] for name in fields
    }
    return {
        "dataset_id": dataset_id,
        "schema_major": 1,
        "default_fields": list(fields),
        "filter_operators": operators,
        "limits": {"max_page_size": max_page_size},
        "availability": {"activation_states": ["active" if active else "paused"]},
    }


def _default_catalog_rows(
    *,
    active_event: bool = True,
    queryable_macro: bool = True,
) -> list[dict[str, Any]]:
    rows = [
        _catalog_row(
            dataset_id,
            [spec["partition_field"], *spec["identity_fields"]],
            filter_field=spec["partition_field"],
            active=active_event,
        )
        for dataset_id, spec in (
            (DISCLOSURE_DATE_DATASET_ID, DATASET_SPECS[DISCLOSURE_DATE_DATASET_ID]),
            (SHARE_FLOAT_DATASET_ID, DATASET_SPECS[SHARE_FLOAT_DATASET_ID]),
        )
    ]
    rows += [
        _catalog_row(
            dataset_id,
            list(fields),
            filter_field="month",
            queryable=queryable_macro,
        )
        for dataset_id, fields in MACRO_FIELDS.items()
    ]
    return rows


class _Transport:
    """Fixture transport: catalog + partition/month-filtered pages."""

    def __init__(
        self,
        *,
        catalog_rows: list[dict[str, Any]] | None = None,
        macro_rows_by_month: dict[str, dict[str, list[dict[str, Any]]]] | None = None,
    ) -> None:
        self.catalog_rows = catalog_rows or _default_catalog_rows()
        self.macro = macro_rows_by_month or {}
        self.query_bodies: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any):
        if kwargs["method"] == "GET":
            return _HTTP(
                200,
                {
                    "api_version": "v1",
                    "catalog_version": "cat-probe-v1",
                    "request_id": "catalog-1",
                    "data": copy.deepcopy(self.catalog_rows),
                },
            )
        body = kwargs["json_body"]
        self.query_bodies.append(copy.deepcopy(body))
        dataset_id = body["dataset_id"]
        filters = body.get("filters") or {}
        value = next(iter(filters.values()), {}).get("eq")
        field = next(iter(filters))
        source = self.macro.get(dataset_id, {}).get(value, [])
        all_rows = [row for row in source if row.get(field) == value]
        cursor = body.get("cursor")
        index = int(cursor.split(":", 1)[1]) if cursor else 0
        page = copy.deepcopy(all_rows[index : index + int(body["limit"])])
        next_index = index + len(page)
        next_cursor = f"0:{next_index}" if next_index < len(all_rows) else None
        return _HTTP(
            200,
            {
                "api_version": "v1",
                "catalog_version": "cat-probe-v1",
                "request_id": f"query-{len(self.query_bodies)}",
                "dataset_id": dataset_id,
                "data": page,
                "next_cursor": next_cursor,
                "metadata": {
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
                    "receipt_id": "receipt-probe-1",
                    "data_through": "2026-08-23T09:00:00+08:00",
                    "observed_at": "2026-08-23T09:05:00+08:00",
                    "reasons": [],
                },
            },
        )


class _HTTP:
    def __init__(self, status_code: int, json_body: dict[str, Any]) -> None:
        self.status_code = status_code
        self.json_body = json_body


def _factory(transport: _Transport):
    def _build(_transport_id, *, token_file, base_url):
        return transport

    return _build


def _macro_fixture() -> dict[str, dict[str, list[dict[str, Any]]]]:
    return {
        "cn.dataset.cn_schedule": {
            "202608": [
                {"month": "202608", "publish_date": "20260809", "title": "7月CPI"},
            ],
            # Next month contains a publish date after the probe day:
            # the dataset is forward-capable.
            "202609": [
                {"month": "202609", "publish_date": "20260910", "title": "8月CPI"},
                {"month": "202609", "publish_date": "20260915", "title": "数据A"},
            ],
        },
        "cn.dataset.cn_cpi": {
            "202607": [{"month": "202607", "nt_val": 100.5, "nt_yoy": 0.2}],
            "202608": [],
        },
    }


def _run(out_dir: Path, transport: _Transport, *, lookback_days: int = 2) -> dict:
    return run_probe(
        token_file="/etc/tradingagent/tradingdatas-read.token",
        out_dir=out_dir,
        lookback_days=lookback_days,
        as_of=AS_OF,
        transport_factory=_factory(transport),
    )


class TestRunProbe(unittest.TestCase):
    def test_receipt_counts_and_forward_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "probe"
            receipt = _run(out_dir, _Transport(macro_rows_by_month=_macro_fixture()))

            self.assertEqual(receipt["probe_id"], "ashare-td-coverage-probe-v1")
            schedule = receipt["macro_datasets"][FORWARD_DATASET_ID]
            self.assertTrue(schedule["forward_capable"])
            self.assertEqual(schedule["next_month_rows"], 2)
            self.assertEqual(schedule["future_publish_dates"], ["20260910", "20260915"])

            share = receipt["event_datasets"][SHARE_FLOAT_DATASET_ID]
            self.assertEqual(share["days_scanned"], 2)
            self.assertGreaterEqual(share["empty_days"], 0)

            view = (out_dir / "coverage_view.md").read_text(encoding="utf-8")
            self.assertIn("forward=YES", view)
            doc = json.loads(
                (out_dir / "coverage_receipt.json").read_text(encoding="utf-8")
            )
            self.assertTrue(doc["research_only"])

    def test_inactive_event_dataset_fails_closed(self) -> None:
        transport = _Transport(catalog_rows=_default_catalog_rows(active_event=False))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                ProbeError, "td_probe_event_dataset_not_queryable"
            ):
                _run(Path(tmp) / "p", transport)

    def test_missing_dataset_in_catalog_fails_closed(self) -> None:
        rows = _default_catalog_rows()
        transport = _Transport(catalog_rows=[r for r in rows if r["dataset_id"] != "cn.dataset.cn_m"])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ProbeError, "td_probe_datasets_missing"):
                _run(Path(tmp) / "p", transport)

    def test_unqueryable_macro_dataset_is_recorded_not_raised(self) -> None:
        rows = _default_catalog_rows(queryable_macro=False)
        # Keep the forward dataset healthy so its absence isn't masked.
        for row in rows:
            if row["dataset_id"] == FORWARD_DATASET_ID:
                row["filter_operators"] = {
                    name: ["eq", "in"] for name in MACRO_FIELDS[FORWARD_DATASET_ID]
                }
        transport = _Transport(
            catalog_rows=rows, macro_rows_by_month=_macro_fixture()
        )
        with tempfile.TemporaryDirectory() as tmp:
            receipt = _run(Path(tmp) / "p", transport)
            cpi = receipt["macro_datasets"]["cn.dataset.cn_cpi"]
            # Active per catalog but lacking an eq operator on the month
            # filter: recorded as skipped, not raised.
            self.assertTrue(cpi["active"])
            self.assertNotIn("month_202608", cpi)
            self.assertEqual(cpi.get("skipped"), "not_queryable_per_catalog")


class TestPublishDateParsing(unittest.TestCase):
    def test_both_formats_parsed_garbage_ignored(self) -> None:
        rows = [
            {"publish_date": "20260910"},
            {"publish_date": "2026-09-11"},
            {"publish_date": ""},
            {"publish_date": "not-a-date"},
            {},
        ]
        hits = _publish_dates_at_or_after(rows, TODAY)
        self.assertEqual(hits, ["20260910", "2026-09-11"])
        past = _publish_dates_at_or_after([{"publish_date": "20260101"}], TODAY)
        self.assertEqual(past, [])


if __name__ == "__main__":
    unittest.main()
