from __future__ import annotations

import json
from pathlib import Path

from CNFutures.forward_labels import (
    CONSERVATIVE_COST_MODEL_VERSION,
    materialize_cn_futures_forward_labels,
)
from CNFutures.review import append_review


SCOPE = {
    "capital_authority_id": "cn-futures-capital-v1",
    "authority_generation": 1,
    "execution_lineage_id": "cn-futures-sim-fresh-20260712-v1",
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
                "source": "sharedsignals_futures_bars",
            },
            {
                "bar_time": "2026-07-13T10:35:00+08:00",
                "close": 3520.0,
                "volume": 100,
                "source": "sharedsignals_futures_bars",
            },
            {
                "bar_time": "2026-07-13T15:00:00+08:00",
                "close": 3530.0,
                "volume": 100,
                "source": "sharedsignals_futures_bars",
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
            },
            {
                "trade_date": "20260715",
                "close": 3550.0,
                "source": "sharedsignals_futures_daily",
            },
            {
                "trade_date": "20260716",
                "close": 3560.0,
                "source": "sharedsignals_futures_daily",
            },
            {
                "trade_date": "20260717",
                "close": 3570.0,
                "source": "sharedsignals_futures_daily",
            },
            {
                "trade_date": "20260720",
                "close": 3580.0,
                "source": "sharedsignals_futures_daily",
            },
        ]


def _write_prediction(path: Path, *, lineage: str | None = None) -> None:
    scope = {**SCOPE, "execution_lineage_id": lineage or SCOPE["execution_lineage_id"]}
    append_review(
        date="20260713",
        market="cn_futures",
        records=[
            {
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
                "source_snapshot_id": "CNF-SNAP-" + "a" * 16,
                "source_snapshot_sha256": "a" * 64,
                "authority": "market_capital_ledger",
                "lineage_status": "complete",
                **scope,
                "cluster_id": "CNF-CLUST-LABEL-1",
                "real_trading_enabled": False,
            }
        ],
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
