from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from shared.review.sample_journal import SampleJournal


ROOT = Path(__file__).resolve().parents[1]
MODULE_NAME = "shared.runtime_test.ashare_sample_ops"


class FakeAshareReader:
    def get_bars_intraday(self, market, symbol, interval, start, end):
        return [
            {
                "close": 10.2,
                "bar_time": "2026-07-13T10:00:00+08:00",
                "source": "SharedSignals/realtime_5min",
            },
            {
                "close": 10.4,
                "bar_time": "2026-07-13T10:30:00+08:00",
                "source": "SharedSignals/realtime_5min",
            },
        ]

    def get_bars_daily(self, market, symbol, start, end):
        return [
            {
                "close": 10.5,
                "trade_date": "20260713",
                "source": "SharedSignals/market_data",
            },
            {
                "close": 10.6,
                "trade_date": "20260714",
                "source": "SharedSignals/market_data",
            },
        ]


class EmptyAshareReader:
    def get_bars_intraday(self, market, symbol, interval, start, end):
        return []

    def get_bars_daily(self, market, symbol, start, end):
        return []


def _module():
    return importlib.import_module(MODULE_NAME)


def _prediction() -> dict[str, object]:
    prediction_at = "2026-07-13T09:30:00+08:00"
    return {
        "market": "Ashare",
        "symbol": "000001.SZ",
        "style": "trend_breakout",
        "strategy_version": "trend-v1",
        "prediction_at": prediction_at,
        "reference_price": 10.0,
        "direction": "long",
        "trade_date": "20260713",
        "capital_authority_id": "ashare-capital-v1",
        "authority_generation": 1,
        "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
        "costs": {
            "round_trip_fee_bps": 105.0,
            "round_trip_slippage_bps": 10.0,
            "cost_model_version": "ashare-execution-reality-20260706-v1",
            "cost_basis_notional_cny": 1000.0,
        },
        "data_quality": {
            "reliable": True,
            "source": "SharedSignals/reference",
            "price_timestamp": prediction_at,
        },
        "real_trading_enabled": False,
        "live_execution_enabled": False,
    }


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_sample_ops_module_exists() -> None:
    assert (ROOT / "shared/runtime_test/ashare_sample_ops.py").is_file()


def test_empty_journal_is_an_explicit_warning_and_persists_manual_only_evidence(
    tmp_path: Path,
) -> None:
    module = _module()
    journal_path = tmp_path / "sample_journal.jsonl"
    review_dir = tmp_path / "review"

    report = module.run_ashare_sample_ops(
        journal_path=journal_path,
        trade_date="20260713",
        as_of="2026-07-13T10:20:00+08:00",
        review_dir=review_dir,
        reader=FakeAshareReader(),
        environ={},
    )

    assert (
        report["sample_kpi"]["scientific_evidence"]["point_in_time_lineage_complete"]
        is False
    )

    assert report["overall_status"] == "warn"
    assert report["reason"] == "no_current_trade_date_predictions"
    assert report["current_trade_date_prediction_count"] == 0
    assert report["orders_created"] == 0
    assert report["emails_sent"] == 0
    assert report["accounts_created"] == 0
    assert report["automatic_promotion_enabled"] is False
    assert report["automatic_risk_expansion_enabled"] is False
    assert report["real_trading_enabled"] is False
    assert report["live_execution_enabled"] is False

    kpi = _read_json(review_dir / "sample_kpi_latest.json")
    decision = _read_json(review_dir / "evolution_decision_latest.json")
    maturity = _read_json(review_dir / "market_maturity_latest.json")
    assert kpi["report_type"] == "sample_journal_kpi"
    assert kpi["trade_date"] == "20260713"
    assert kpi["journal_event_count"] == 0
    assert kpi["scientific_evidence"]["promotion_evidence_ready"] is False
    assert decision["state"] == "evidence_pending"
    assert decision["recommended_action"] == "observe_and_label_candidates"
    assert maturity["total_trading_days"] == 0
    assert maturity["live_transition_authorized"] is False
    assert maturity["broker_route_status"] == (
        "email_to_tonghuashun_design_only_not_implemented"
    )
    assert len((review_dir / "sample_kpi_log.jsonl").read_text().splitlines()) == 1
    assert len((review_dir / "market_maturity_log.jsonl").read_text().splitlines()) == 1


def test_sample_ops_materializes_labels_and_is_idempotent(tmp_path: Path) -> None:
    module = _module()
    journal_path = tmp_path / "sample_journal.jsonl"
    review_dir = tmp_path / "review"
    journal = SampleJournal(journal_path)
    journal.append_prediction(_prediction())

    first = module.run_ashare_sample_ops(
        journal_path=journal_path,
        trade_date="20260713",
        as_of="2026-07-14T16:00:00+08:00",
        review_dir=review_dir,
        reader=FakeAshareReader(),
        environ={},
    )
    second = module.run_ashare_sample_ops(
        journal_path=journal_path,
        trade_date="20260713",
        as_of="2026-07-14T16:00:00+08:00",
        review_dir=review_dir,
        reader=FakeAshareReader(),
        environ={},
    )

    assert first["overall_status"] == "pass"
    assert first["current_trade_date_prediction_count"] == 1
    assert first["label_ops"]["counts"]["new_label_updates"] == 1
    assert first["sample_kpi"]["styles"]["trend_breakout"]["prediction_count"] == 1
    assert first["market_maturity"]["total_trading_days"] == 1
    assert first["market_maturity"]["evidence_summary"]["simulation_trading_day"] == (
        "20260713"
    )
    assert second["label_ops"]["counts"]["new_label_updates"] == 0
    assert second["label_ops"]["counts"]["idempotent_label_updates"] == 1
    assert len(journal.read_events()) == 2
    assert len((review_dir / "sample_kpi_log.jsonl").read_text().splitlines()) == 2
    assert len((review_dir / "market_maturity_log.jsonl").read_text().splitlines()) == 2


def test_retired_authority_events_do_not_enter_fresh_start_counts(
    tmp_path: Path,
) -> None:
    module = _module()
    journal_path = tmp_path / "sample_journal.jsonl"
    review_dir = tmp_path / "review"
    legacy = {
        **_prediction(),
        "capital_authority_id": "retired-shared-master",
        "authority_generation": 2,
        "execution_lineage_id": "retired-epoch-2",
    }
    SampleJournal(journal_path).append_prediction(legacy)

    report = module.run_ashare_sample_ops(
        journal_path=journal_path,
        trade_date="20260713",
        as_of="2026-07-13T10:20:00+08:00",
        review_dir=review_dir,
        reader=FakeAshareReader(),
        environ={},
    )

    assert report["overall_status"] == "warn"
    assert report["current_trade_date_prediction_count"] == 0
    assert report["sample_kpi"]["journal_event_count"] == 0
    assert report["sample_kpi"]["journal_total_event_count"] == 1
    assert report["sample_kpi"]["excluded_legacy_event_count"] == 1
    assert report["market_maturity"]["total_trading_days"] == 0
    assert len(SampleJournal(journal_path).read_events()) == 1


def test_short_lineage_marker_cannot_claim_point_in_time_evidence(
    tmp_path: Path,
) -> None:
    module = _module()
    journal_path = tmp_path / "sample_journal.jsonl"
    prediction = {
        **_prediction(),
        "point_in_time_as_of": "2026-07-13T09:30:00+08:00",
        "source_snapshot_sha256": "not-a-sha256",
    }
    SampleJournal(journal_path).append_prediction(prediction)

    report = module.run_ashare_sample_ops(
        journal_path=journal_path,
        trade_date="20260713",
        as_of="2026-07-13T10:20:00+08:00",
        review_dir=tmp_path / "review",
        reader=FakeAshareReader(),
        environ={},
    )

    assert (
        report["sample_kpi"]["scientific_evidence"]["point_in_time_lineage_complete"]
        is False
    )


def test_scientific_evidence_recomputes_calibration_instead_of_trusting_bool() -> None:
    module = _module()
    records = [
        {
            "record_type": "chain_validation",
            "sample_layer": "chain_validation",
            "calibration_evidence_sufficient": True,
        }
    ]
    kpi = {
        "sample_layer_totals": {"completed_round_trip": 10},
        "styles": {},
        "maturity_duplicate_count": 0,
        "sample_size_evidence": {
            "unique_decision_cluster_count": 20,
            "N_eff": 20.0,
        },
        "calibration_evidence": {
            "status": "unavailable_no_calibrated_predictions",
            "sufficient": False,
        },
        "account_drawdown_evidence": {
            "status": "available",
            "max_drawdown_cny": 0.0,
        },
    }

    evidence = module._scientific_evidence(records, kpi)

    assert evidence["calibration_evidence_sufficient"] is False
    assert evidence["calibration_metrics"]["status"] == (
        "unavailable_no_calibrated_predictions"
    )
    assert evidence["promotion_evidence_ready"] is False


def test_scientific_evidence_excludes_immutable_data_quality_rejections() -> None:
    module = _module()
    rejected = {
        **_prediction(),
        "journal_event_type": "prediction_snapshot",
        "forward_label_eligibility": "rejected_data_quality",
    }
    eligible = {
        **_prediction(),
        "journal_event_type": "prediction_snapshot",
        "forward_label_eligibility": "eligible",
        "point_in_time_lineage": {
            "event_time": "2026-07-13T09:25:00+08:00",
            "available_at": "2026-07-13T09:30:00+08:00",
            "ingested_at": "2026-07-13T09:30:00+08:00",
            "retrieved_as_of": "2026-07-13T09:30:00+08:00",
        },
        "point_in_time_as_of": "2026-07-13T09:30:00+08:00",
        "source_snapshot_sha256": "a" * 64,
    }
    kpi = {
        "sample_layer_totals": {},
        "styles": {},
        "maturity_duplicate_count": 0,
        "sample_size_evidence": {},
        "calibration_evidence": {},
        "account_drawdown_evidence": {},
    }

    evidence = module._scientific_evidence([rejected, eligible], kpi)

    assert evidence["prediction_audit_total_count"] == 2
    assert evidence["prediction_data_quality_excluded_count"] == 1
    assert evidence["prediction_pit_total_count"] == 1
    assert evidence["prediction_pit_valid_count"] == 1


def test_due_labels_without_market_evidence_are_an_explicit_warning(
    tmp_path: Path,
) -> None:
    module = _module()
    journal_path = tmp_path / "sample_journal.jsonl"
    SampleJournal(journal_path).append_prediction(_prediction())

    report = module.run_ashare_sample_ops(
        journal_path=journal_path,
        trade_date="20260713",
        as_of="2026-07-13T15:30:00+08:00",
        review_dir=tmp_path / "review",
        reader=EmptyAshareReader(),
        environ={},
    )

    assert report["overall_status"] == "warn"
    assert report["reason"] == "forward_label_evidence_missing"
    assert report["label_ops"]["counts"]["missing_evidence"] == 3
    assert report["market_maturity"]["evidence_summary"]["degradation_events"] >= 3


def test_live_environment_blocks_before_any_output(tmp_path: Path) -> None:
    module = _module()
    journal_path = tmp_path / "sample_journal.jsonl"
    review_dir = tmp_path / "review"

    with pytest.raises(module.AshareSampleOpsSafetyError):
        module.run_ashare_sample_ops(
            journal_path=journal_path,
            trade_date="20260713",
            as_of="2026-07-13T10:20:00+08:00",
            review_dir=review_dir,
            reader=FakeAshareReader(),
            environ={"REAL_TRADING_ENABLED": "true"},
        )

    assert not journal_path.exists()
    assert not review_dir.exists()


def test_symlink_review_directory_is_rejected(tmp_path: Path) -> None:
    module = _module()
    target = tmp_path / "real-review"
    target.mkdir()
    review_dir = tmp_path / "review-link"
    review_dir.symlink_to(target, target_is_directory=True)

    with pytest.raises(module.AshareSampleOpsSafetyError):
        module.run_ashare_sample_ops(
            journal_path=tmp_path / "sample_journal.jsonl",
            trade_date="20260713",
            as_of="2026-07-13T10:20:00+08:00",
            review_dir=review_dir,
            reader=FakeAshareReader(),
            environ={},
        )

    assert list(target.iterdir()) == []


def test_sample_ops_has_no_email_broker_or_live_dispatch_path() -> None:
    source = (ROOT / "shared/runtime_test/ashare_sample_ops.py").read_text(
        encoding="utf-8"
    )
    forbidden = (
        "shared.notify",
        "send_email",
        "smtplib",
        "broker.submit",
        "place_order",
        "live_dispatch",
    )
    for marker in forbidden:
        assert marker not in source
    assert "write_evolution_decision" in source
    assert "assess_ashare_maturity" in source
    assert "run_ashare_forward_label_backlog" in source
