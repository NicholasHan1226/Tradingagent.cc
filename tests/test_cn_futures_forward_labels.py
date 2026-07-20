from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from CNFutures.forward_labels import (
    CONSERVATIVE_COST_MODEL_VERSION,
    _price_evidence,
    materialize_cn_futures_forward_labels,
)
from CNFutures.review import append_review


SCOPE = {
    "capital_authority_id": "cn-futures-capital-v1",
    "authority_generation": 1,
    "execution_lineage_id": "cn-futures-sim-fresh-20260712-v1",
}


def _receipts(event_time: str) -> dict[str, str]:
    return {
        "available_at": event_time,
        "ingested_at": event_time,
        "retrieved_as_of": event_time,
    }


class _Reader:
    def get_bars_intraday(
        self,
        market: str,
        symbol: str,
        interval: str,
        start: str,
        end: str,
    ) -> list[dict[str, object]]:
        assert market == "Futures"
        return [
            {
                "bar_time": "2026-07-13T10:05:00+08:00",
                "close": 3510.0,
                "volume": 100,
                "source": "fixture.cnfutures.intraday-bars.v1",
                **_receipts("2026-07-13T10:05:00+08:00"),
            },
            {
                "bar_time": "2026-07-13T10:35:00+08:00",
                "close": 3520.0,
                "volume": 100,
                "source": "fixture.cnfutures.intraday-bars.v1",
                **_receipts("2026-07-13T10:35:00+08:00"),
            },
            {
                "bar_time": "2026-07-13T15:00:00+08:00",
                "close": 3530.0,
                "volume": 100,
                "source": "fixture.cnfutures.intraday-bars.v1",
                **_receipts("2026-07-13T15:00:00+08:00"),
            },
        ]

    def get_bars_daily(
        self,
        market: str,
        symbol: str,
        start: str,
        end: str,
    ) -> list[dict[str, object]]:
        assert market == "Futures"
        return [
            {
                "trade_date": "20260714",
                "close": 3540.0,
                "source": "sharedsignals_futures_daily",
                **_receipts("2026-07-14T15:00:00+08:00"),
            },
            {
                "trade_date": "20260715",
                "close": 3550.0,
                "source": "sharedsignals_futures_daily",
                **_receipts("2026-07-15T15:00:00+08:00"),
            },
            {
                "trade_date": "20260716",
                "close": 3560.0,
                "source": "sharedsignals_futures_daily",
                **_receipts("2026-07-16T15:00:00+08:00"),
            },
            {
                "trade_date": "20260717",
                "close": 3570.0,
                "source": "sharedsignals_futures_daily",
                **_receipts("2026-07-17T15:00:00+08:00"),
            },
            {
                "trade_date": "20260720",
                "close": 3580.0,
                "source": "sharedsignals_futures_daily",
                **_receipts("2026-07-20T15:00:00+08:00"),
            },
        ]


def _write_prediction(
    path: Path,
    *,
    lineage: str | None = None,
    evidence_overrides: dict[str, object] | None = None,
) -> None:
    scope = {**SCOPE, "execution_lineage_id": lineage or SCOPE["execution_lineage_id"]}
    prediction = {
        "record_type": "prediction",
        "session": "day_morning",
        "style": "trend",
        "style_version": "trend-v1",
        "symbol": "RB2610.SHF",
        "bar_time": "2026-07-13T09:35:00+08:00",
        "entry_price": 3500.0,
        "direction": "buy",
        "side": "buy",
        "point_in_time_as_of": "2026-07-13T09:35:00+08:00",
        "source_event_time": "2026-07-13T09:35:00+08:00",
        **_receipts("2026-07-13T09:35:00+08:00"),
        "source_snapshot_id": "CNF-SNAP-" + "a" * 16,
        "source_snapshot_sha256": "a" * 64,
        "authority": "market_capital_ledger",
        "lineage_status": "complete",
        **scope,
        "cluster_id": "CNF-CLUST-LABEL-1",
        "real_trading_enabled": False,
    }
    for key, value in (evidence_overrides or {}).items():
        if value is None:
            prediction.pop(key, None)
        else:
            prediction[key] = value
    append_review(
        date="20260713",
        market="cn_futures",
        records=[prediction],
        errors=[],
        path=path,
        authority_scope=scope,
    )


def test_materializes_all_due_horizons_append_only_with_versioned_costs(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reviews.jsonl"
    _write_prediction(path)

    result = materialize_cn_futures_forward_labels(
        review_path=path,
        reader=_Reader(),
        authority_scope=SCOPE,
        as_of="2026-07-20T15:05:00+08:00",
    )

    assert result["eligible_target_count"] == 1
    assert result["appended_update_count"] == 1
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    update = lines[-1]
    assert update["record_type"] == "cn_futures_forward_label_update"
    assert set(update["labels"]) == {"m30", "m60", "close", "1d", "3d", "5d"}
    assert {label["status"] for label in update["labels"].values()} == {"ready"}
    for label in update["labels"].values():
        assert label["cost_model_version"] == CONSERVATIVE_COST_MODEL_VERSION
        assert label["net_return_after_costs"] < label["gross_return_after_direction"]
        assert len(label["label_evidence_sha256"]) == 64
    assert len(update["journal_payload_sha256"]) == 64
    assert update["real_trading_enabled"] is False

    repeated = materialize_cn_futures_forward_labels(
        review_path=path,
        reader=_Reader(),
        authority_scope=SCOPE,
        as_of="2026-07-20T15:05:00+08:00",
    )
    assert repeated["appended_update_count"] == 0
    assert repeated["idempotent_update_count"] == 1


def test_wrong_lineage_prediction_is_excluded(tmp_path: Path) -> None:
    path = tmp_path / "reviews.jsonl"
    _write_prediction(path, lineage="retired-lineage")

    result = materialize_cn_futures_forward_labels(
        review_path=path,
        reader=_Reader(),
        authority_scope=SCOPE,
        as_of="2026-07-20T15:05:00+08:00",
    )

    assert result["eligible_target_count"] == 0
    assert result["excluded_authority_count"] == 1
    assert result["appended_update_count"] == 0


class _SinglePointReader:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def get_bars_intraday(self, *args, **kwargs):
        return list(self.rows)

    def get_bars_daily(self, *args, **kwargs):
        return []


def _m30_label(path: Path, reader: object) -> dict[str, object]:
    materialize_cn_futures_forward_labels(
        review_path=path,
        reader=reader,
        authority_scope=SCOPE,
        as_of="2026-07-13T10:10:00+08:00",
    )
    update = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    return update["labels"]["m30"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"available_at": None, "ingested_at": None, "retrieved_as_of": None},
        {"retrieved_as_of": "not-a-timestamp"},
        {"retrieved_as_of": "2026-07-13T09:35:00"},
        {"retrieved_as_of": "2026-07-13T09:36:00+08:00"},
        {
            "available_at": "2026-07-13T09:36:00+08:00",
            "ingested_at": "2026-07-13T09:35:00+08:00",
        },
    ],
)
def test_reference_receipt_failures_never_become_ready(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    path = tmp_path / "reviews.jsonl"
    _write_prediction(path, evidence_overrides=overrides)
    label = _m30_label(
        path,
        _SinglePointReader(
            [
                {
                    "bar_time": "2026-07-13T10:05:00+08:00",
                    "close": 3510.0,
                    "source": "fixture.cnfutures.intraday-bars.v1",
                    **_receipts("2026-07-13T10:05:00+08:00"),
                }
            ]
        ),
    )
    assert label["status"] != "ready"


@pytest.mark.parametrize(
    "receipt_fields",
    [
        {},
        {"retrieved_as_of": "invalid"},
        {"retrieved_as_of": "2026-07-13T10:05:00"},
        {"retrieved_as_of": "2026-07-13T10:11:00+08:00"},
        {
            "available_at": "2026-07-13T10:06:00+08:00",
            "ingested_at": "2026-07-13T10:05:00+08:00",
            "retrieved_as_of": "2026-07-13T10:06:00+08:00",
        },
    ],
)
def test_exit_receipt_failures_never_become_ready(
    tmp_path: Path, receipt_fields: dict[str, str]
) -> None:
    path = tmp_path / "reviews.jsonl"
    _write_prediction(path)
    label = _m30_label(
        path,
        _SinglePointReader(
            [
                {
                    "bar_time": "2026-07-13T10:05:00+08:00",
                    "close": 99.0,
                    "source": "fixture.cnfutures.intraday-bars.v1",
                    **receipt_fields,
                }
            ]
        ),
    )
    assert label["status"] != "ready"


@pytest.mark.parametrize("reverse", [False, True])
def test_invalid_high_price_cannot_control_selection_order(
    tmp_path: Path, reverse: bool
) -> None:
    path = tmp_path / "reviews.jsonl"
    _write_prediction(path)
    valid = {
        "bar_time": "2026-07-13T10:05:00+08:00",
        "close": 3510.0,
        "source": "fixture.cnfutures.intraday-bars.v1",
        **_receipts("2026-07-13T10:05:00+08:00"),
    }
    invalid = {
        "bar_time": "2026-07-13T10:04:00+08:00",
        "timestamp": "2026-07-13T11:04:00+08:00",
        "close": 99_000.0,
        "source": "fixture.cnfutures.intraday-bars.v1",
        **_receipts("2026-07-13T10:04:00+08:00"),
    }
    rows = [valid, invalid]
    if reverse:
        rows.reverse()
    label = _m30_label(path, _SinglePointReader(rows))
    assert label["status"] == "ready"
    assert label["exit_price"] == 3510.0


def test_missing_receipt_is_not_fabricated_from_as_of() -> None:
    row = {
        "point_in_time_as_of": "2026-07-13T09:35:00+08:00",
        "symbol": "RB2610.SHF",
        "trade_date": "20260713",
    }
    points, _ = _price_evidence(
        _SinglePointReader(
            [
                {
                    "bar_time": "2026-07-13T10:05:00+08:00",
                    "close": 3510.0,
                    "source": "fixture.cnfutures.intraday-bars.v1",
                }
            ]
        ),
        row,
        as_of=datetime.fromisoformat("2026-07-13T10:10:00+08:00"),
    )
    assert points[0]["evidence_envelope_validation"]["status"] == (
        "missing_receipt_timestamps"
    )
    assert "point_in_time_lineage" not in points[0]
