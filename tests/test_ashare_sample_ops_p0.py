from __future__ import annotations

from datetime import timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil

import pytest

from Ashare.sample_pipeline import build_candidate_observation
from shared.review.projection_generation import (
    compute_generation_id,
    ProjectionGenerationError,
    load_current_projection_set,
    publish_projection_generation,
    record_projection_audit,
)
from shared.review.sample_journal import (
    JournalConflictError,
    JournalSafetyError,
    SampleJournal,
)
from shared.runtime_test.ashare_forward_label_ops import (
    run_ashare_forward_label_backlog as _run_ashare_forward_label_backlog,
)
from shared.runtime_test.ashare_sample_ops import (
    AshareSampleOpsSafetyError,
    _build_maturity,
    run_ashare_sample_ops as _run_ashare_sample_ops,
)
from tests._ashare_validation_plan_fixture import (
    build_non_production_ashare_validation_plan,
)


CN_TZ = timezone(timedelta(hours=8))
AUTHORITY = {
    "capital_authority_id": "ashare-capital-v1",
    "authority_generation": 1,
    "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
}


def run_ashare_forward_label_backlog(**kwargs):
    """Test-only adapter makes the non-production plan explicit."""

    kwargs.setdefault("validation_plan", build_non_production_ashare_validation_plan())
    return _run_ashare_forward_label_backlog(**kwargs)


def run_ashare_sample_ops(**kwargs):
    """Test-only adapter makes the non-production plan explicit."""

    kwargs.setdefault("validation_plan", build_non_production_ashare_validation_plan())
    return _run_ashare_sample_ops(**kwargs)


def _candidate(
    snapshot_id: str,
    *,
    symbol: str = "000001.SZ",
    prediction_at: str = "2026-07-13T09:30:00+08:00",
    retrieved_as_of: str = "2026-07-13T09:30:00+08:00",
    style: str = "trend_breakout",
    mg_arm: str = "mg_off",
) -> dict[str, object]:
    return {
        "snapshot_id": snapshot_id,
        "market": "Ashare",
        "symbol": symbol,
        "style": style,
        "strategy_version": "%s-v1" % style,
        "prediction_at": prediction_at,
        "event_time": prediction_at,
        "available_at": prediction_at,
        "ingested_at": prediction_at,
        "retrieved_as_of": retrieved_as_of,
        "point_in_time_lineage": {
            "timestamps": {
                "event_time": prediction_at,
                "available_at": prediction_at,
                "ingested_at": prediction_at,
                "retrieved_as_of": retrieved_as_of,
            }
        },
        "point_in_time_as_of": retrieved_as_of,
        "reference_price": 10.0,
        "direction": "long",
        "trade_date": "20260713",
        "base_snapshot_sha256": (snapshot_id.encode("utf-8").hex() + "0" * 64)[:64],
        "marketgraph": {
            "enabled": mg_arm == "mg_on",
            "ablation_group": mg_arm,
            "applied_features": {} if mg_arm == "mg_off" else {"regime": 0.1},
        },
        "costs": {
            "round_trip_fee_bps": 10.0,
            "round_trip_slippage_bps": 5.0,
            "cost_model_version": "ashare-execution-reality-20260706-v1",
        },
        "data_quality": {
            "reliable": True,
            "source": "SharedSignals/reference",
            "price_timestamp": prediction_at,
            "reference_timestamp_lineage": {
                "source_field": "bar_time",
                "raw_value": prediction_at,
                "normalized_value": prediction_at,
                "timezone_semantics": "ashare_exchange_event_time",
                "normalization_rule": "convert_aware_instant_to_asia_shanghai",
                "valid": True,
            },
        },
        "decision_timestamp_lineage": {
            field: {
                "source_field": field,
                "raw_value": prediction_at,
                "normalized_value": prediction_at,
                "timezone_semantics": "ashare_decision_time",
                "normalization_rule": "convert_aware_instant_to_asia_shanghai",
                "valid": True,
            }
            for field in ("prediction_at", "data_as_of")
        },
        "real_trading_enabled": False,
        **AUTHORITY,
    }


def _targets() -> dict[str, str]:
    return {
        "m30": "2026-07-13T10:00:00+08:00",
        "m60": "2026-07-13T10:30:00+08:00",
        "close": "2026-07-13T15:00:00+08:00",
        "1d": "2026-07-14T15:00:00+08:00",
        "3d": "2026-07-16T15:00:00+08:00",
        "5d": "2026-07-20T15:00:00+08:00",
    }


def _price_points() -> list[dict[str, object]]:
    points: list[dict[str, object]] = []
    for index, timestamp in enumerate(_targets().values(), start=1):
        points.append(
            {
                "timestamp": timestamp,
                "event_time": timestamp,
                "available_at": timestamp,
                "ingested_at": timestamp,
                "retrieved_as_of": "2026-07-20T16:00:00+08:00",
                "point_in_time_lineage": {
                    "timestamps": {
                        "event_time": timestamp,
                        "available_at": timestamp,
                        "ingested_at": timestamp,
                        "retrieved_as_of": "2026-07-20T16:00:00+08:00",
                    }
                },
                "price": 10.0 + index / 10.0,
                "source": "SharedSignals/test",
                "reliable": True,
            }
        )
    return points


def _label_request(snapshot_id: str) -> dict[str, object]:
    return {
        "snapshot_id": snapshot_id,
        "price_points": _price_points(),
        "as_of": "2026-07-20T16:00:00+08:00",
        "horizon_targets": _targets(),
        "costs": {
            "round_trip_fee_bps": 10.0,
            "round_trip_slippage_bps": 5.0,
            "cost_model_version": "ashare-execution-reality-20260706-v1",
        },
    }


class CountingReader:
    def __init__(self, *, timeout: bool = False) -> None:
        self.timeout = timeout
        self.calls: list[tuple[object, ...]] = []

    def get_bars_intraday(self, market, symbol, interval, start, end):
        self.calls.append(("intraday", market, symbol, interval, start, end))
        if self.timeout:
            raise TimeoutError("provider timeout")
        return [
            {
                "close": 10.1,
                "bar_time": "2026-07-13T10:00:00+08:00",
                "available_at": "2026-07-13T10:00:01+08:00",
                "ingested_at": "2026-07-13T10:00:02+08:00",
                "source": "SharedSignals/test",
            }
        ]

    def get_bars_daily(self, market, symbol, start, end):
        self.calls.append(("daily", market, symbol, start, end))
        if self.timeout:
            raise TimeoutError("provider timeout")
        dates = ("20260713", "20260714", "20260715", "20260716", "20260717", "20260720")
        return [
            {
                "close": 10.1 + index / 10.0,
                "trade_date": trade_date,
                "available_at": "%s-%s-%sT15:00:01+08:00"
                % (trade_date[:4], trade_date[4:6], trade_date[6:]),
                "ingested_at": "%s-%s-%sT15:00:02+08:00"
                % (trade_date[:4], trade_date[4:6], trade_date[6:]),
                "source": "SharedSignals/test",
            }
            for index, trade_date in enumerate(dates)
        ]


class ProductionShapedReferenceReader:
    """Sanitized shape matching the A-share reference/receipt time contract."""

    def get_bars_intraday(self, market, symbol, interval, start, end):
        return [
            {
                "close": 53.95,
                "bar_time": "2026-07-13 13:40:00",
                "collected_at": "2026-07-13T05:45:02+00:00",
                "volume": 2_920_166,
                "provider": "tushare_rt_min",
            }
        ]

    def get_bars_daily(self, market, symbol, start, end):
        return []


class ProductionShapedLabelReader:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def get_bars_intraday(self, market, symbol, interval, start, end):
        self.calls.append(("intraday", market, symbol, interval, start, end))
        return [
            {
                "close": 54.20,
                "bar_time": "2026-07-13 14:16:00",
                "available_at": "2026-07-13T06:16:01+00:00",
                "ingested_at": "2026-07-13T06:16:02+00:00",
                "source": "tushare_rt_min",
            }
        ]

    def get_bars_daily(self, market, symbol, start, end):
        self.calls.append(("daily", market, symbol, start, end))
        return []


def _terminal_update(snapshot_id: str) -> dict[str, object]:
    event = {
        "journal_event_type": "forward_label_update",
        "journal_event_id": "forward_label_update:terminal:%s" % snapshot_id,
        "record_type": "label_update",
        "snapshot_id": snapshot_id,
        "labels_as_of": "2026-07-13T15:59:00+08:00",
        "evidence_available_at": "2026-07-13T15:59:00+08:00",
        "labels": {
            horizon: {"status": "ready"}
            for horizon in ("m30", "m60", "close", "1d", "3d", "5d")
        },
        "real_trading_enabled": False,
        **AUTHORITY,
    }
    return SampleJournal._seal_event(event)


def test_frozen_cutoff_uses_receipt_not_early_prediction_time(tmp_path: Path) -> None:
    journal = SampleJournal(tmp_path / "journal.jsonl")
    journal.append_prediction(
        _candidate(
            "late-receipt",
            prediction_at="2026-07-13T14:00:00+08:00",
            retrieved_as_of="2026-07-13T14:21:00+08:00",
        )
    )

    before = journal.read_frozen(as_of="2026-07-13T14:19:00+08:00")
    after = journal.read_frozen(as_of="2026-07-13T14:21:00+08:00")

    assert before.journal_head_event_count == 0
    assert before.excluded_after_as_of_count == 1
    assert after.journal_head_event_count == 1
    assert after.max_evidence_available_at == "2026-07-13T14:21:00+08:00"
    assert len(after.journal_head_sha256) == 64


def test_frozen_cutoff_uses_latest_availability_or_receipt(tmp_path: Path) -> None:
    journal = SampleJournal(tmp_path / "journal.jsonl")
    candidate = _candidate(
        "available-early-retrieved-late",
        prediction_at="2026-07-13T14:00:00+08:00",
        retrieved_as_of="2026-07-13T14:21:00+08:00",
    )
    candidate["available_at"] = "2026-07-13T14:01:00+08:00"
    journal.append_prediction(candidate)

    before = journal.read_frozen(as_of="2026-07-13T14:19:00+08:00")
    after = journal.read_frozen(as_of="2026-07-13T14:21:00+08:00")

    assert before.journal_head_event_count == 0
    assert after.journal_head_event_count == 1
    assert after.max_evidence_available_at == "2026-07-13T14:21:00+08:00"


def test_frozen_cutoff_audits_nested_pit_availability_timestamps(
    tmp_path: Path,
) -> None:
    journal = SampleJournal(tmp_path / "nested.jsonl")
    candidate = _candidate(
        "nested-late",
        retrieved_as_of="2026-07-13T14:01:00+08:00",
    )
    candidate["point_in_time_lineage"] = {
        "timestamps": {"available_at": "2026-07-13T14:21:00+08:00"}
    }
    journal.append_prediction(candidate)

    before = journal.read_frozen(as_of="2026-07-13T14:19:00+08:00")
    after = journal.read_frozen(as_of="2026-07-13T14:21:00+08:00")
    assert before.journal_head_event_count == 0
    assert after.journal_head_event_count == 1
    assert after.max_evidence_available_at == "2026-07-13T14:21:00+08:00"

    invalid = SampleJournal(tmp_path / "nested-invalid.jsonl")
    invalid_candidate = _candidate("nested-invalid")
    invalid_candidate["point_in_time_lineage"] = {
        "timestamps": {"available_at": "2026-07-13T14:21:00"}
    }
    invalid.append_prediction(invalid_candidate)
    with pytest.raises(JournalSafetyError, match="timezone-naive"):
        invalid.read_frozen(as_of="2026-07-13T14:30:00+08:00")


def test_frozen_cutoff_missing_or_invalid_receipt_fails_closed(tmp_path: Path) -> None:
    journal = SampleJournal(tmp_path / "journal.jsonl")
    missing = _candidate("missing")
    missing.pop("retrieved_as_of")
    missing.pop("point_in_time_as_of")
    missing.pop("available_at")
    missing.pop("ingested_at")
    missing["point_in_time_lineage"]["timestamps"].pop("available_at")
    missing["point_in_time_lineage"]["timestamps"].pop("ingested_at")
    missing["point_in_time_lineage"]["timestamps"].pop("retrieved_as_of")
    journal.append_prediction(missing)
    with pytest.raises(JournalSafetyError, match="availability/receipt"):
        journal.read_frozen(as_of="2026-07-13T15:00:00+08:00")

    invalid_journal = SampleJournal(tmp_path / "invalid.jsonl")
    invalid_journal.append_prediction(
        _candidate("invalid", retrieved_as_of="2026-07-13T14:00:00")
    )
    with pytest.raises(JournalSafetyError, match="timezone-naive"):
        invalid_journal.read_frozen(as_of="2026-07-13T15:00:00+08:00")


def test_concurrent_4001_events_do_not_enter_h0_and_next_cutoff_sees_them(
    tmp_path: Path,
) -> None:
    journal = SampleJournal(tmp_path / "journal.jsonl")
    journal.append_prediction(_candidate("h0"))
    h0 = journal.read_frozen(as_of="2026-07-13T14:19:00+08:00")
    journal.append_predictions(
        [
            _candidate(
                "concurrent-%04d" % index,
                symbol="%06d.SZ" % (index % 3000),
                retrieved_as_of="2026-07-13T14:20:00+08:00",
            )
            for index in range(4001)
        ]
    )

    assert h0.journal_head_event_count == 1
    assert len(h0.copy_events()) == 1
    next_view = journal.read_frozen(as_of="2026-07-13T14:21:00+08:00")
    assert next_view.journal_head_event_count == 4002


def test_unknown_append_blocks_batch_but_task_owned_delta_advances_prefix(
    tmp_path: Path,
) -> None:
    blocked = SampleJournal(tmp_path / "blocked.jsonl")
    blocked.append_prediction(_candidate("base"))
    frozen = blocked.read_frozen(as_of="2026-07-20T16:00:00+08:00")
    blocked.append_prediction(_candidate("unknown", symbol="000002.SZ"))
    with pytest.raises(JournalConflictError, match="unknown journal append"):
        blocked.materialize_label_batch(
            frozen,
            [_label_request("base")],
            validation_plan=build_non_production_ashare_validation_plan(),
        )
    assert len(blocked.read_events()) == 2

    owned = SampleJournal(tmp_path / "owned.jsonl")
    owned.append_prediction(_candidate("base"))
    owned_frozen = owned.read_frozen(as_of="2026-07-20T16:00:00+08:00")
    report = owned.materialize_label_batch(
        owned_frozen,
        [_label_request("base")],
        batch_size=100,
        validation_plan=build_non_production_ashare_validation_plan(),
    )
    assert report["task_owned_delta_event_count"] == 1
    assert report["append_batch_count"] == report["fsync_count"] == 1
    assert len(owned.read_events()) == 2


def test_1999_terminal_plus_one_pending_selects_only_one_snapshot(
    tmp_path: Path,
) -> None:
    journal = SampleJournal(tmp_path / "journal.jsonl")
    candidates = [
        _candidate(
            "snapshot-%04d" % index,
            symbol="%06d.SZ" % (index + 1),
        )
        for index in range(2000)
    ]
    journal.append_predictions(candidates)
    terminal = [_terminal_update("snapshot-%04d" % index) for index in range(1999)]
    with journal._locked(exclusive=True, create_parent=True) as locked_paths:
        journal._append_many_unlocked(terminal, locked_paths)

    reader = CountingReader()
    report = run_ashare_forward_label_backlog(
        journal_path=journal.path,
        anchor_trade_date="20260713",
        as_of="2026-07-13T16:00:00+08:00",
        reader=reader,
        environ={},
    )

    assert report["backlog"]["terminal_snapshot_count"] == 1999
    assert report["backlog"]["pending_snapshot_count"] == 1
    assert report["counts"]["prediction_count"] == 1
    assert report["results"][0]["snapshot_id"] == "snapshot-1999"
    assert len(reader.calls) == 2


def test_2000_snapshots_250_symbol_dates_8_variants_have_bounded_calls(
    tmp_path: Path,
) -> None:
    journal = SampleJournal(tmp_path / "journal.jsonl")
    styles = ("trend", "pullback", "event", "defensive")
    candidates = []
    for symbol_index in range(250):
        symbol = "%06d.SZ" % (symbol_index + 1)
        for variant in range(8):
            candidates.append(
                _candidate(
                    "snapshot-%03d-%d" % (symbol_index, variant),
                    symbol=symbol,
                    style=styles[variant // 2],
                    mg_arm="mg_on" if variant % 2 else "mg_off",
                )
            )
    journal.append_predictions(candidates)
    reader = CountingReader()

    report = run_ashare_forward_label_backlog(
        journal_path=journal.path,
        anchor_trade_date="20260713",
        as_of="2026-07-13T16:00:00+08:00",
        reader=reader,
        environ={},
    )

    http = report["http_metrics"]
    assert report["counts"]["prediction_count"] == 2000
    assert http["logical_request_count"] == 4000
    assert http["physical_request_count"] == 500
    assert http["cache_hit_count"] == 3500
    assert len(reader.calls) == 500
    assert report["journal_append"]["append_batch_count"] == 10
    assert report["journal_append"]["fsync_count"] == 10


def test_provider_timeout_keeps_observation_retryable_and_nonterminal(
    tmp_path: Path,
) -> None:
    journal = SampleJournal(tmp_path / "journal.jsonl")
    journal.append_prediction(_candidate("timeout"))
    report = run_ashare_forward_label_backlog(
        journal_path=journal.path,
        anchor_trade_date="20260713",
        as_of="2026-07-13T16:00:00+08:00",
        reader=CountingReader(timeout=True),
        environ={},
    )

    assert report["http_metrics"]["timeout_count"] == 2
    assert report["results"][0]["retryable"] is True
    assert report["results"][0]["degraded"] is True
    events = journal.read_events()
    assert (
        sum(event["journal_event_type"] == "prediction_snapshot" for event in events)
        == 1
    )
    latest = journal.project_sample_records(events)[0]
    assert {label["status"] for label in latest["labels"].values()} <= {
        "missing_exit_evidence",
        "pending_not_due",
    }
    next_backlog = journal.read_frozen(as_of="2026-07-13T16:01:00+08:00")
    assert len(next_backlog.copy_events()) == 2


def test_batch_crash_before_and_after_append_replays_without_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = SampleJournal(tmp_path / "before.jsonl")
    before.append_prediction(_candidate("before"))
    frozen_before = before.read_frozen(as_of="2026-07-20T16:00:00+08:00")

    def fail_before(events, locked_paths):
        raise OSError("crash before append")

    monkeypatch.setattr(before, "_append_many_unlocked", fail_before)
    with pytest.raises(OSError, match="before append"):
        before.materialize_label_batch(
            frozen_before,
            [_label_request("before")],
            validation_plan=build_non_production_ashare_validation_plan(),
        )
    assert len(before.read_events()) == 1

    after = SampleJournal(tmp_path / "after.jsonl")
    after.append_prediction(_candidate("after"))
    frozen_after = after.read_frozen(as_of="2026-07-20T16:00:00+08:00")
    original_append = after._append_many_unlocked

    def fail_after(events, locked_paths):
        original_append(events, locked_paths)
        raise OSError("crash after append")

    monkeypatch.setattr(after, "_append_many_unlocked", fail_after)
    with pytest.raises(OSError, match="after append"):
        after.materialize_label_batch(
            frozen_after,
            [_label_request("after")],
            validation_plan=build_non_production_ashare_validation_plan(),
        )
    assert len(after.read_events()) == 2

    replay = SampleJournal(after.path)
    replay_view = replay.read_frozen(as_of="2026-07-20T16:00:00+08:00")
    replayed = replay.materialize_label_batch(
        replay_view,
        [_label_request("after")],
        validation_plan=build_non_production_ashare_validation_plan(),
    )
    assert replayed["results"][0]["status"] == "idempotent"
    assert replayed["task_owned_delta_event_count"] == 0
    assert len(replay.read_events()) == 2


def _projection_set(input_sha: str, marker: str) -> dict[str, dict[str, object]]:
    common = {
        "projection_input_sha256": input_sha,
        "data_as_of": "2026-07-13T16:00:00+08:00",
        "generated_at": "2026-07-13T08:00:01+00:00",
        "journal_head_event_count": 1,
        "journal_head_sha256": "f" * 64,
        "max_evidence_available_at": "2026-07-13T15:59:00+08:00",
        "excluded_after_as_of_count": 0,
        "run_id": "run-%s" % marker,
        "H0": {"event_count": 1, "sha256": "c" * 64},
        "H1": {
            "event_count": 2,
            "sha256": "d" * 64,
            "task_owned_delta_event_count": 1,
        },
        "real_trading_enabled": False,
        "live_execution_enabled": False,
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
        "marker": marker,
    }
    return {
        "sample_kpi_latest.json": {**common, "report_type": "sample_journal_kpi"},
        "evolution_decision_latest.json": {
            **common,
            "report_type": "ashare_evolution_decision_v2",
            "live_transition_authorized": False,
        },
        "market_maturity_latest.json": {
            **common,
            "report_type": "ashare_market_maturity_v1",
            "live_transition_authorized": False,
        },
    }


def test_projection_generation_id_matches_cross_language_golden_vector() -> None:
    assert compute_generation_id(
        "0" * 64,
        {
            "sample_kpi_latest.json": "1" * 64,
            "evolution_decision_latest.json": "2" * 64,
            "market_maturity_latest.json": "3" * 64,
        },
    ) == (
        "ashare-sample-projection-"
        "3d4cd18ef52c0b6cc3d7b34a2a3da8aeafb92a65fd0a54b8336017827cadcfdf"
    )


@pytest.mark.parametrize(
    "corruption",
    (
        "manifest_only",
        "missing_projection",
        "projection_sha_mismatch",
        "symlink_projection",
        "hardlink_projection",
        "extra_file",
    ),
)
def test_existing_corrupt_generation_cannot_replace_current(
    tmp_path: Path, corruption: str
) -> None:
    old_sha = "a" * 64
    old = publish_projection_generation(
        review_dir=tmp_path,
        projections=_projection_set(old_sha, "old-current"),
        projection_input_sha256=old_sha,
        run_id="run-old-current",
        generated_at="2026-07-13T08:00:01+00:00",
    )
    current_path = tmp_path / "projection_current.json"
    old_current_bytes = current_path.read_bytes()

    new_sha = "b" * 64
    source_root = tmp_path / "source-valid-generation"
    source = publish_projection_generation(
        review_dir=source_root,
        projections=_projection_set(new_sha, "candidate"),
        projection_input_sha256=new_sha,
        run_id="run-candidate",
        generated_at="2026-07-13T08:00:02+00:00",
    )
    source_generation = source_root / str(source["generation_path"])
    target_generation = (
        tmp_path / "projection_generations" / str(source["generation_id"])
    )
    if corruption == "manifest_only":
        target_generation.mkdir()
        shutil.copy2(
            source_generation / "generation_manifest.json",
            target_generation / "generation_manifest.json",
        )
    else:
        shutil.copytree(source_generation, target_generation)
        target_generation.chmod(0o755)
        if corruption == "missing_projection":
            (target_generation / "sample_kpi_latest.json").unlink()
        elif corruption == "projection_sha_mismatch":
            projection = target_generation / "sample_kpi_latest.json"
            projection.chmod(0o644)
            projection.write_text("{}\n", encoding="utf-8")
        elif corruption == "symlink_projection":
            projection = target_generation / "sample_kpi_latest.json"
            projection.unlink()
            projection.symlink_to(source_generation / "sample_kpi_latest.json")
        elif corruption == "hardlink_projection":
            projection = target_generation / "sample_kpi_latest.json"
            projection.unlink()
            os.link(source_generation / "sample_kpi_latest.json", projection)
        elif corruption == "extra_file":
            (target_generation / "unexpected.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ProjectionGenerationError):
        publish_projection_generation(
            review_dir=tmp_path,
            projections=_projection_set(new_sha, "candidate"),
            projection_input_sha256=new_sha,
            run_id="run-candidate",
            generated_at="2026-07-13T08:00:02+00:00",
        )
    assert current_path.read_bytes() == old_current_bytes
    assert (
        load_current_projection_set(tmp_path)["current_manifest"]["generation_id"]
        == old["generation_id"]
    )


def test_existing_complete_generation_is_idempotently_revalidated_before_reuse(
    tmp_path: Path,
) -> None:
    old_sha = "a" * 64
    publish_projection_generation(
        review_dir=tmp_path,
        projections=_projection_set(old_sha, "old-current"),
        projection_input_sha256=old_sha,
        run_id="run-old-current",
        generated_at="2026-07-13T08:00:01+00:00",
    )
    new_sha = "b" * 64
    source_root = tmp_path / "source-valid-generation"
    source = publish_projection_generation(
        review_dir=source_root,
        projections=_projection_set(new_sha, "candidate"),
        projection_input_sha256=new_sha,
        run_id="run-candidate",
        generated_at="2026-07-13T08:00:02+00:00",
    )
    source_generation = source_root / str(source["generation_path"])
    target_generation = (
        tmp_path / "projection_generations" / str(source["generation_id"])
    )
    shutil.copytree(source_generation, target_generation)

    reused = publish_projection_generation(
        review_dir=tmp_path,
        projections=_projection_set(new_sha, "candidate"),
        projection_input_sha256=new_sha,
        run_id="run-candidate",
        generated_at="2026-07-13T08:00:02+00:00",
    )
    assert reused["generation_id"] == source["generation_id"]
    loaded = load_current_projection_set(tmp_path)
    assert loaded["current_manifest"]["generation_id"] == source["generation_id"]


def test_reader_rejects_hash_consistent_copy_under_forged_generation_id(
    tmp_path: Path,
) -> None:
    input_sha = "a" * 64
    published = publish_projection_generation(
        review_dir=tmp_path,
        projections=_projection_set(input_sha, "canonical"),
        projection_input_sha256=input_sha,
        run_id="run-canonical",
        generated_at="2026-07-13T08:00:01+00:00",
    )
    original_id = str(published["generation_id"])
    forged_id = "ashare-sample-projection-" + "f" * 64
    assert forged_id != original_id
    original_dir = tmp_path / str(published["generation_path"])
    forged_dir = tmp_path / "projection_generations" / forged_id
    forged_dir.mkdir()
    for filename in (
        "sample_kpi_latest.json",
        "evolution_decision_latest.json",
        "market_maturity_latest.json",
    ):
        (forged_dir / filename).write_bytes((original_dir / filename).read_bytes())
        (forged_dir / filename).chmod(0o444)
    manifest = json.loads((original_dir / "generation_manifest.json").read_bytes())
    manifest["generation_id"] = forged_id
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    (forged_dir / "generation_manifest.json").write_bytes(manifest_bytes)
    (forged_dir / "generation_manifest.json").chmod(0o444)
    forged_dir.chmod(0o555)
    current_path = tmp_path / "projection_current.json"
    current = json.loads(current_path.read_bytes())
    current["generation_id"] = forged_id
    current["generation_path"] = "projection_generations/%s" % forged_id
    current["generation_manifest_sha256"] = sha256(manifest_bytes).hexdigest()
    current_path.write_text(json.dumps(current), encoding="utf-8")

    with pytest.raises(ProjectionGenerationError, match="generation_id_mismatch"):
        load_current_projection_set(tmp_path)


def test_projection_generation_is_atomic_same_hash_and_auditable(
    tmp_path: Path,
) -> None:
    old_sha = "a" * 64
    old = publish_projection_generation(
        review_dir=tmp_path,
        projections=_projection_set(old_sha, "old"),
        projection_input_sha256=old_sha,
        run_id="run-old",
        generated_at="2026-07-13T08:00:01+00:00",
    )
    assert old["generation_id"] == compute_generation_id(
        old_sha, old["projection_sha256"]
    )
    new_sha = "b" * 64
    with pytest.raises(ProjectionGenerationError, match="injected"):
        publish_projection_generation(
            review_dir=tmp_path,
            projections=_projection_set(new_sha, "new"),
            projection_input_sha256=new_sha,
            run_id="run-new",
            generated_at="2026-07-13T08:00:02+00:00",
            _fail_after_file_count=2,
        )
    still_old = load_current_projection_set(tmp_path)
    assert still_old["current_manifest"]["generation_id"] == old["generation_id"]
    assert still_old["generation_manifest"]["generation_id"] == compute_generation_id(
        old_sha, still_old["current_manifest"]["projection_sha256"]
    )
    assert {
        payload["projection_input_sha256"]
        for payload in still_old["projections"].values()
    } == {old_sha}

    new = publish_projection_generation(
        review_dir=tmp_path,
        projections=_projection_set(new_sha, "new"),
        projection_input_sha256=new_sha,
        run_id="run-new",
        generated_at="2026-07-13T08:00:02+00:00",
    )
    loaded = load_current_projection_set(tmp_path)
    assert loaded["current_manifest"]["generation_id"] == new["generation_id"]
    assert {
        payload["projection_input_sha256"] for payload in loaded["projections"].values()
    } == {new_sha}

    manifest_path = tmp_path / new["generation_path"] / str(new["generation_manifest"])
    manifest_bytes = manifest_path.read_bytes()
    tampered = json.loads(manifest_bytes)
    tampered["run_id"] = "tampered-run-id"
    manifest_path.chmod(0o600)
    manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
    manifest_path.chmod(0o444)
    with pytest.raises(ProjectionGenerationError, match="manifest_hash_mismatch"):
        load_current_projection_set(tmp_path)
    manifest_path.chmod(0o600)
    manifest_path.write_bytes(manifest_bytes)
    manifest_path.chmod(0o444)

    audit = record_projection_audit(
        review_dir=tmp_path,
        generation_id=old["generation_id"],
        status="superseded",
        reason="polluted_projection_replaced",
        superseded_by_generation_id=new["generation_id"],
    )
    repeated = record_projection_audit(
        review_dir=tmp_path,
        generation_id=old["generation_id"],
        status="superseded",
        reason="polluted_projection_replaced",
        superseded_by_generation_id=new["generation_id"],
    )
    assert audit["status"] == "appended"
    assert repeated["status"] == "idempotent"


@pytest.mark.parametrize("mutation_kind", ("in_place", "rename_replace"))
def test_generation_mutation_after_final_validation_cannot_swap_current(
    tmp_path: Path, mutation_kind: str
) -> None:
    old_sha = "a" * 64
    old = publish_projection_generation(
        review_dir=tmp_path,
        projections=_projection_set(old_sha, "old-current"),
        projection_input_sha256=old_sha,
        run_id="run-old-current",
        generated_at="2026-07-13T08:00:01+00:00",
    )
    current_path = tmp_path / "projection_current.json"
    old_current_bytes = current_path.read_bytes()

    def mutate_after_final_validation(generation_path: Path) -> None:
        target = generation_path / "sample_kpi_latest.json"
        if mutation_kind == "in_place":
            target.chmod(0o644)
            target.write_text("{}\n", encoding="utf-8")
            target.chmod(0o444)
            return
        generation_path.chmod(0o755)
        moved = tmp_path / "moved-sample-kpi.json"
        target.rename(moved)
        target.write_text("{}\n", encoding="utf-8")
        target.chmod(0o444)
        generation_path.chmod(0o555)

    new_sha = "b" * 64
    with pytest.raises(ProjectionGenerationError):
        publish_projection_generation(
            review_dir=tmp_path,
            projections=_projection_set(new_sha, "candidate"),
            projection_input_sha256=new_sha,
            run_id="run-candidate",
            generated_at="2026-07-13T08:00:02+00:00",
            _before_pointer_swap_hook=mutate_after_final_validation,
        )

    assert current_path.read_bytes() == old_current_bytes
    loaded = load_current_projection_set(tmp_path)
    assert loaded["current_manifest"]["generation_id"] == old["generation_id"]


def test_same_bytes_different_inode_after_final_validation_cannot_swap_current(
    tmp_path: Path,
) -> None:
    old_sha = "a" * 64
    old = publish_projection_generation(
        review_dir=tmp_path,
        projections=_projection_set(old_sha, "old-current"),
        projection_input_sha256=old_sha,
        run_id="run-old-current",
        generated_at="2026-07-13T08:00:01+00:00",
    )
    current_path = tmp_path / "projection_current.json"
    old_current_bytes = current_path.read_bytes()
    replacement_evidence: dict[str, object] = {}

    def replace_with_same_bytes_after_final_validation(generation_path: Path) -> None:
        target = generation_path / "sample_kpi_latest.json"
        before = os.lstat(target)
        raw = target.read_bytes()
        replacement = tmp_path / "same-bytes-sample-kpi.json"
        replacement.write_bytes(raw)
        replacement.chmod(before.st_mode & 0o7777)
        os.utime(
            replacement,
            ns=(before.st_atime_ns, before.st_mtime_ns),
        )
        generation_path.chmod(0o755)
        os.replace(replacement, target)
        generation_path.chmod(0o555)
        after = os.lstat(target)
        replacement_evidence.update(
            {
                "same_content": target.read_bytes() == raw,
                "same_mode": after.st_mode == before.st_mode,
                "different_inode": (after.st_dev, after.st_ino)
                != (before.st_dev, before.st_ino),
            }
        )

    new_sha = "b" * 64
    with pytest.raises(
        ProjectionGenerationError,
        match="projection_generation_identity_changed_before_pointer_swap",
    ):
        publish_projection_generation(
            review_dir=tmp_path,
            projections=_projection_set(new_sha, "candidate"),
            projection_input_sha256=new_sha,
            run_id="run-candidate",
            generated_at="2026-07-13T08:00:02+00:00",
            _before_pointer_swap_hook=replace_with_same_bytes_after_final_validation,
        )

    assert replacement_evidence == {
        "same_content": True,
        "same_mode": True,
        "different_inode": True,
    }
    assert current_path.read_bytes() == old_current_bytes
    loaded = load_current_projection_set(tmp_path)
    assert loaded["current_manifest"]["generation_id"] == old["generation_id"]


@pytest.mark.parametrize(
    ("compatibility_kind", "filename"),
    (
        ("mirror", "sample_kpi_latest.json"),
        ("log", "sample_kpi_log.jsonl"),
    ),
)
@pytest.mark.parametrize("mutation_kind", ("rename", "symlink", "hardlink"))
def test_compatibility_identity_change_after_final_validation_cannot_swap_current(
    tmp_path: Path,
    compatibility_kind: str,
    filename: str,
    mutation_kind: str,
) -> None:
    old_sha = "a" * 64
    old = publish_projection_generation(
        review_dir=tmp_path,
        projections=_projection_set(old_sha, "old-current"),
        projection_input_sha256=old_sha,
        run_id="run-old-current",
        generated_at="2026-07-13T08:00:01+00:00",
    )
    current_path = tmp_path / "projection_current.json"
    old_current_bytes = current_path.read_bytes()

    def mutate_after_final_validation(_generation_path: Path) -> None:
        target = tmp_path / filename
        backup = tmp_path / (".%s.%s.original" % (filename, mutation_kind))
        target.rename(backup)
        if mutation_kind == "rename":
            target.write_bytes(backup.read_bytes())
        elif mutation_kind == "symlink":
            target.symlink_to(backup)
        else:
            os.link(backup, target)

    new_sha = "b" * 64
    with pytest.raises(ProjectionGenerationError):
        publish_projection_generation(
            review_dir=tmp_path,
            projections=_projection_set(new_sha, "candidate"),
            projection_input_sha256=new_sha,
            run_id="run-candidate",
            generated_at="2026-07-13T08:00:02+00:00",
            _before_pointer_swap_hook=mutate_after_final_validation,
        )

    assert compatibility_kind in {"mirror", "log"}
    assert current_path.read_bytes() == old_current_bytes
    loaded = load_current_projection_set(tmp_path)
    assert loaded["current_manifest"]["generation_id"] == old["generation_id"]


def test_hardlinked_projection_log_cannot_mutate_target_or_swap_current(
    tmp_path: Path,
) -> None:
    old_sha = "a" * 64
    old = publish_projection_generation(
        review_dir=tmp_path,
        projections=_projection_set(old_sha, "old-current"),
        projection_input_sha256=old_sha,
        run_id="run-old-current",
        generated_at="2026-07-13T08:00:01+00:00",
    )
    current_path = tmp_path / "projection_current.json"
    old_current_bytes = current_path.read_bytes()
    log_path = tmp_path / "sample_kpi_log.jsonl"
    log_path.unlink()
    external = tmp_path / "external-log-target.jsonl"
    external.write_text('{"safe":true}\n', encoding="utf-8")
    os.link(external, log_path)
    external_before = external.read_bytes()

    new_sha = "b" * 64
    with pytest.raises(
        ProjectionGenerationError, match="projection_log_hardlink_not_allowed"
    ):
        publish_projection_generation(
            review_dir=tmp_path,
            projections=_projection_set(new_sha, "candidate"),
            projection_input_sha256=new_sha,
            run_id="run-candidate",
            generated_at="2026-07-13T08:00:02+00:00",
        )

    assert external.read_bytes() == external_before
    assert current_path.read_bytes() == old_current_bytes
    assert (
        load_current_projection_set(tmp_path)["current_manifest"]["generation_id"]
        == old["generation_id"]
    )


def test_new_batch_and_legacy_single_append_match_labels_kpi_and_maturity(
    tmp_path: Path,
) -> None:
    legacy = SampleJournal(tmp_path / "legacy.jsonl")
    modern = SampleJournal(tmp_path / "modern.jsonl")
    candidate = _candidate("equivalent")
    legacy.append_prediction(candidate)
    modern.append_prediction(candidate)
    legacy_result = legacy.materialize_labels(
        "equivalent",
        _price_points(),
        as_of="2026-07-20T16:00:00+08:00",
        horizon_targets=_targets(),
        costs=_label_request("equivalent")["costs"],
        validation_plan=build_non_production_ashare_validation_plan(),
    )
    modern_view = modern.read_frozen(as_of="2026-07-20T16:00:00+08:00")
    modern_result = modern.materialize_label_batch(
        modern_view,
        [_label_request("equivalent")],
        validation_plan=build_non_production_ashare_validation_plan(),
    )

    assert modern_result["results"][0]["record"] == legacy_result["record"]
    legacy_kpi = legacy.build_kpi()
    modern_events = modern_view.copy_events() + modern_result["appended_events"]
    modern_kpi = modern.build_kpi_from_events(modern_events)
    assert modern_kpi == legacy_kpi

    legacy_records = legacy.latest_sample_records()
    modern_records = modern.project_sample_records(modern_events)
    label_ops = {"counts": {}}
    legacy_maturity = _build_maturity(
        records=legacy_records,
        kpi=legacy_kpi,
        label_ops=label_ops,
        trade_date="20260713",
        generated_at="2026-07-20T08:00:00+00:00",
    )
    modern_maturity = _build_maturity(
        records=modern_records,
        kpi=modern_kpi,
        label_ops=label_ops,
        trade_date="20260713",
        generated_at="2026-07-20T08:00:00+00:00",
    )
    assert modern_maturity == legacy_maturity


def test_sample_ops_outputs_shared_input_hash_metrics_and_fail_closed_flags(
    tmp_path: Path,
) -> None:
    journal = SampleJournal(tmp_path / "journal.jsonl")
    journal.append_prediction(_candidate("sample-ops"))
    review_dir = tmp_path / "review"
    report = run_ashare_sample_ops(
        journal_path=journal.path,
        trade_date="20260713",
        as_of="2026-07-13T16:00:00+08:00",
        review_dir=review_dir,
        reader=CountingReader(),
        environ={"REAL_TRADING_ENABLED": "false"},
    )

    assert report["generated_at"] != report["data_as_of"]
    assert len(report["journal_head_sha256"]) == 64
    assert report["H0"] == {
        "event_count": report["journal_head_event_count"],
        "sha256": report["journal_head_sha256"],
    }
    assert report["H1"]["event_count"] == (
        report["H0"]["event_count"] + report["H1"]["task_owned_delta_event_count"]
    )
    assert len(report["H1"]["sha256"]) == 64
    assert report["performance"]["journal"]["journal_parse_count"] == 1
    assert report["performance"]["journal"]["journal_events_parsed"] == 1
    assert report["performance"]["stages"]["forward_labels"]["wall_seconds"] >= 0
    input_hashes = {
        report["sample_kpi"]["projection_input_sha256"],
        report["evolution_decision"]["projection_input_sha256"],
        report["market_maturity"]["projection_input_sha256"],
    }
    assert input_hashes == {report["projection_input_sha256"]}
    assert report["sample_kpi"]["H0"] == report["H0"]
    assert report["sample_kpi"]["H1"] == report["H1"]
    for payload in (
        report["sample_kpi"],
        report["evolution_decision"],
        report["market_maturity"],
    ):
        assert payload["automatic_promotion_enabled"] is False
        assert payload["automatic_risk_expansion_enabled"] is False
        assert payload["live_execution_enabled"] is False
        assert payload["real_trading_enabled"] is False
    assert report["live_transition_authorized"] is False

    blocked_dir = tmp_path / "blocked"
    with pytest.raises(AshareSampleOpsSafetyError):
        run_ashare_sample_ops(
            journal_path=journal.path,
            trade_date="20260713",
            as_of="2026-07-13T16:00:00+08:00",
            review_dir=blocked_dir,
            reader=CountingReader(),
            environ={"REAL_TRADING_ENABLED": "true"},
        )
    assert not blocked_dir.exists()

    with pytest.raises(JournalSafetyError, match="live trading marker"):
        journal.append_prediction(
            {
                **_candidate("live"),
                "account_type": "live",
                "real_trading_enabled": True,
            }
        )


def test_production_shaped_timestamp_reaches_same_cutoff_kpi_generation(
    tmp_path: Path,
) -> None:
    observation = build_candidate_observation(
        symbol="000021.SZ",
        trade_date="20260713",
        mapped_market="ashare",
        mapped_symbol="000021.SZ",
        score={
            "combined": 0.68,
            "macro": 0.68,
            "event": 0.68,
            "fundamental": 0.68,
            "capital": 0.68,
            "technical": 0.68,
            "sentiment": 0.68,
            "turnover_wan": 20_000,
            "evidence_coverage": 1.0,
            "missing_evidence_dimensions": [],
        },
        reader=ProductionShapedReferenceReader(),
        prediction_at="2026-07-13T13:46:00+08:00",
        mg_enabled=False,
    )
    journal = SampleJournal(tmp_path / "production-shape.jsonl")
    journal.append_predictions(observation["prediction_snapshots"])
    label_reader = ProductionShapedLabelReader()

    report = run_ashare_sample_ops(
        journal_path=journal.path,
        trade_date="20260713",
        as_of="2026-07-13T14:20:00+08:00",
        review_dir=tmp_path / "review",
        reader=label_reader,
        environ={"REAL_TRADING_ENABLED": "false"},
    )

    assert report["label_ops"]["counts"]["data_quality_rejected"] == 0
    assert report["label_ops"]["counts"]["ready_labels"] == 4
    assert report["H0"]["event_count"] == 4
    assert report["H1"]["event_count"] == 8
    assert report["H1"]["task_owned_delta_event_count"] == 4
    assert report["data_as_of"] == "2026-07-13T14:20:00+08:00"
    assert journal.canonical_head(journal.read_events()) == {
        "event_count": report["H1"]["event_count"],
        "sha256": report["H1"]["sha256"],
    }
    assert {
        report["sample_kpi"]["projection_input_sha256"],
        report["evolution_decision"]["projection_input_sha256"],
        report["market_maturity"]["projection_input_sha256"],
    } == {report["projection_input_sha256"]}
    assert len(label_reader.calls) == 2


def test_unknown_append_after_label_batch_blocks_projection_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shared.runtime_test.ashare_sample_ops as sample_ops_module

    journal = SampleJournal(tmp_path / "journal.jsonl")
    journal.append_prediction(_candidate("race"))
    review_dir = tmp_path / "review"
    real_backlog = sample_ops_module.run_ashare_forward_label_backlog

    def racing_backlog(**kwargs):
        result = real_backlog(**kwargs)
        SampleJournal(kwargs["journal_path"]).append_sample(
            {
                "journal_event_id": "unknown-concurrent-after-label-batch",
                "record_type": "risk_reject",
                "sample_layer": "risk_reject",
                "event_at": "2026-07-13T15:59:30+08:00",
                "reason": "independent_concurrent_writer",
                "real_trading_enabled": False,
                **AUTHORITY,
            }
        )
        return result

    monkeypatch.setattr(
        sample_ops_module, "run_ashare_forward_label_backlog", racing_backlog
    )
    with pytest.raises(JournalConflictError, match="unknown journal append"):
        run_ashare_sample_ops(
            journal_path=journal.path,
            trade_date="20260713",
            as_of="2026-07-13T16:00:00+08:00",
            review_dir=review_dir,
            reader=CountingReader(),
            environ={"REAL_TRADING_ENABLED": "false"},
        )

    assert (
        journal.read_frozen(
            as_of="2026-07-13T16:00:00+08:00"
        ).journal_source_event_count
        == 3
    )
    assert not (review_dir / "projection_current.json").exists()
