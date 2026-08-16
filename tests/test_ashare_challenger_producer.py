from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from Ashare.challenger_producer import (
    MAX_CHALLENGER_TRADE_PNL_DRAWDOWN_CNY,
    MIN_CHALLENGER_COMPLETED_ROUND_TRIPS,
    ChallengerProducerError,
    build_challenger_candidates,
)
from Ashare.evolution_controller import build_evolution_decision
from Ashare.promotion_executor import execute_automatic_promotion
from shared.models.champion_registry import ChampionSelectionRegistry
from tests._ashare_validation_plan_fixture import (
    build_non_production_ashare_validation_plan,
)


NOW = datetime(2026, 8, 15, 9, 30, tzinfo=timezone.utc)

AUTHORITY = {
    "capital_authority_id": "ashare-capital-v1",
    "authority_generation": 1,
    "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
}

STRONG_BUCKET = {
    "completed_round_trip_count": MIN_CHALLENGER_COMPLETED_ROUND_TRIPS + 1,
    "win_rate": 0.66,
    "expectancy_cny": 20.0,
    "post_cost_pnl_cny": 120.0,
    "trade_pnl_sequence_max_drawdown_cny": 45.0,
}


def _style_row(bucket: dict[str, object], *, intent: str = "exploitation"):
    return {
        "prediction_count": 24,
        "exploration_fill_count": 3,
        "exploitation_fill_count": 6,
        "completed_round_trip_count": 10,
        "post_cost_pnl_cny": 120.0,
        "forward_label_counts": {"5d": {"ready": 15}},
        "performance_by_sample_intent": {intent: dict(bucket)},
    }


def _kpi(styles: dict[str, object]) -> dict[str, object]:
    return {
        "report_type": "sample_journal_kpi",
        "evidence_source": "sample_journal_kpi",
        "market": "ashare",
        "trade_date": "20260815",
        "authority_scope": dict(AUTHORITY),
        "journal_event_count": 80,
        "journal_head_event_count": 80,
        "data_as_of": "2026-08-15T16:00:00+08:00",
        "projection_input_sha256": "e" * 64,
        "run_id": "ashare-sample-ops:test-run",
        "sample_size_evidence": {
            "raw_N": 24,
            "unique_decision_cluster_count": 20,
            "independent_trading_day_count": 10,
            "N_eff": 20.0,
        },
        "styles": styles,
        "scientific_evidence": {
            "point_in_time_lineage_complete": True,
            "costs_evidence_complete": True,
            "fill_evidence_revalidated": True,
            "duplicate_cluster_control_passed": True,
            "calibration_evidence_sufficient": True,
            "promotion_evidence_ready": True,
        },
    }


def _strong_kpi() -> dict[str, object]:
    return _kpi({"trend_breakout_strength_continuation": _style_row(STRONG_BUCKET)})


def _decision(kpi: dict[str, object]) -> dict[str, object]:
    return build_evolution_decision(
        kpi,
        authority_scope=AUTHORITY,
        target_trade_date="20260815",
    )


def _ready_decision() -> dict[str, object]:
    decision = _decision(_strong_kpi())
    assert decision["promotion_evidence_ready"] is True
    return decision


def _produce(
    kpi: dict[str, object], decision: dict[str, object]
) -> list[dict[str, object]]:
    return build_challenger_candidates(
        kpi,
        decision=decision,
        validation_plan=build_non_production_ashare_validation_plan(),
        recorded_at=NOW,
    )


def test_decision_without_ready_evidence_produces_no_candidates() -> None:
    kpi = _strong_kpi()
    kpi["styles"]["trend_breakout_strength_continuation"][  # type: ignore[index]
        "completed_round_trip_count"
    ] = 3
    decision = _decision(kpi)
    assert decision["promotion_evidence_ready"] is False

    assert _produce(kpi, decision) == []


@pytest.mark.parametrize(
    "bucket_override",
    [
        {"completed_round_trip_count": MIN_CHALLENGER_COMPLETED_ROUND_TRIPS - 1},
        {"expectancy_cny": 0.0},
        {"expectancy_cny": -5.0},
        {"win_rate": 0.49},
        {
            "trade_pnl_sequence_max_drawdown_cny": (
                MAX_CHALLENGER_TRADE_PNL_DRAWDOWN_CNY + 0.01
            )
        },
        {"win_rate": True},
        {"expectancy_cny": "20.0"},
    ],
)
def test_weak_style_evidence_produces_no_candidates(
    bucket_override: dict[str, object],
) -> None:
    bucket = {**STRONG_BUCKET, **bucket_override}
    kpi = _kpi({"trend_breakout_strength_continuation": _style_row(bucket)})
    decision = _decision(kpi)
    assert decision["promotion_evidence_ready"] is True

    assert _produce(kpi, decision) == []


def test_unclassified_bucket_never_qualifies() -> None:
    kpi = _kpi(
        {
            "trend_breakout_strength_continuation": _style_row(
                STRONG_BUCKET, intent="unclassified"
            )
        }
    )
    assert _produce(kpi, _ready_decision()) == []


def test_exploration_bucket_qualifies_when_no_exploitation_bucket() -> None:
    kpi = _kpi(
        {
            "trend_breakout_strength_continuation": _style_row(
                STRONG_BUCKET, intent="exploration"
            )
        }
    )
    candidates = _produce(kpi, _ready_decision())
    assert [candidate["challenger_id"] for candidate in candidates] == [
        "trend_breakout_strength_continuation"
    ]


def test_qualified_evidence_produces_executor_ready_candidate(
    tmp_path: Path,
) -> None:
    kpi = _strong_kpi()
    decision = _ready_decision()

    candidates = _produce(kpi, decision)

    assert [candidate["challenger_id"] for candidate in candidates] == [
        "trend_breakout_strength_continuation"
    ]
    candidate = candidates[0]
    assert candidate["challenger_version"] == "2.0.0"
    assert candidate["research_snapshot_sha256"] == kpi["projection_input_sha256"]
    assert candidate["created_by"] == "ashare-challenger-producer"

    result = execute_automatic_promotion(
        decision,
        registry_root=tmp_path / "champion_registry",
        challenger_candidates=candidates,
        recorded_at=NOW,
    )
    assert result["status"] == "promoted"
    assert result["selected_model_id"] == "trend_breakout_strength_continuation"
    assert result["selected_model_version"] == "2.0.0"
    assert result["real_trading_enabled"] is False
    assert result["live_transition_authorized"] is False
    assert result["automatic_risk_expansion_enabled"] is False

    current = ChampionSelectionRegistry(tmp_path / "champion_registry").load_current()
    assert current.receipt_sha256 == result["receipt_sha256"]
    assert current.real_trading_enabled is False
    assert current.simulation_only is True


def test_multiple_qualified_styles_are_deterministically_ordered() -> None:
    kpi = _kpi(
        {
            "pullback_or_short_reversal": _style_row(STRONG_BUCKET),
            "trend_breakout_strength_continuation": _style_row(STRONG_BUCKET),
            "event_catalyst_with_price_confirmation": _style_row(
                {**STRONG_BUCKET, "win_rate": 0.2}
            ),
        }
    )
    candidates = _produce(kpi, _ready_decision())
    assert [candidate["challenger_id"] for candidate in candidates] == [
        "pullback_or_short_reversal",
        "trend_breakout_strength_continuation",
    ]


def test_candidate_with_missing_field_is_rejected_by_executor(
    tmp_path: Path,
) -> None:
    decision = _ready_decision()
    candidates = _produce(_strong_kpi(), decision)
    broken = dict(candidates[0])
    broken.pop("artifact_sha256")

    result = execute_automatic_promotion(
        decision,
        registry_root=tmp_path / "champion_registry",
        challenger_candidates=[broken],
        recorded_at=NOW,
    )

    assert result["status"] == "no_op"
    assert result["reason"] == "no_qualified_challenger"
    assert result["challenger_rejections"] == ["challenger_artifact_sha256_invalid"]
    assert not (tmp_path / "champion_registry").exists()


def test_non_simulation_only_decision_fails_closed() -> None:
    decision = _ready_decision()
    decision["real_trading_enabled"] = True

    with pytest.raises(ChallengerProducerError, match="not_simulation_only"):
        _produce(_strong_kpi(), decision)


def test_unverified_plan_fails_closed() -> None:
    with pytest.raises(ChallengerProducerError, match="validation_plan_invalid"):
        build_challenger_candidates(
            _strong_kpi(),
            decision=_ready_decision(),
            validation_plan={"frozen": True},  # type: ignore[arg-type]
            recorded_at=NOW,
        )


class _EmptyReader:
    def get_bars_intraday(self, market, symbol, interval, start, end):
        return []

    def get_bars_daily(self, market, symbol, start, end):
        return []


def _run_sample_ops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs):
    import shared.runtime_test.ashare_sample_ops as sample_ops

    monkeypatch.setattr(
        sample_ops,
        "build_evolution_decision",
        lambda *args, **inner_kwargs: dict(_ready_decision()),
    )
    return sample_ops.run_ashare_sample_ops(
        journal_path=tmp_path / "sample_journal.jsonl",
        trade_date="20260815",
        as_of="2026-08-15T16:00:00+08:00",
        review_dir=tmp_path / "review",
        reader=_EmptyReader(),
        environ={},
        validation_plan=build_non_production_ashare_validation_plan(),
        **kwargs,
    )


def test_sample_ops_defaults_to_producer_when_no_candidates_given(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shared.runtime_test.ashare_sample_ops as sample_ops

    calls = []
    real_producer = sample_ops.build_challenger_candidates

    def spy(sample_kpi, *, decision, validation_plan, recorded_at):
        calls.append(
            {
                "decision_ready": decision.get("promotion_evidence_ready"),
                "plan_type": type(validation_plan).__name__,
                "recorded_at": recorded_at,
            }
        )
        return real_producer(
            sample_kpi,
            decision=decision,
            validation_plan=validation_plan,
            recorded_at=recorded_at,
        )

    monkeypatch.setattr(sample_ops, "build_challenger_candidates", spy)

    report = _run_sample_ops(tmp_path, monkeypatch)

    # The empty journal carries no style evidence, so the producer correctly
    # emits zero candidates and the promotion stays an explicit no-op.
    assert len(calls) == 1
    assert calls[0]["decision_ready"] is True
    assert calls[0]["plan_type"] == "ValidationPlan"
    execution = report["evolution_decision"]["promotion_execution"]
    assert execution["status"] == "no_op"
    assert execution["reason"] == "no_qualified_challenger"
    assert not (tmp_path / "review" / "champion_registry").exists()


def test_sample_ops_producer_output_promotes_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shared.runtime_test.ashare_sample_ops as sample_ops

    real_producer = sample_ops.build_challenger_candidates
    rich_kpi = _strong_kpi()

    def producer_with_evidence(sample_kpi, *, decision, validation_plan, recorded_at):
        return real_producer(
            rich_kpi,
            decision=decision,
            validation_plan=validation_plan,
            recorded_at=recorded_at,
        )

    monkeypatch.setattr(
        sample_ops, "build_challenger_candidates", producer_with_evidence
    )

    report = _run_sample_ops(tmp_path, monkeypatch)

    execution = report["evolution_decision"]["promotion_execution"]
    assert execution["status"] == "promoted"
    assert execution["selected_model_id"] == "trend_breakout_strength_continuation"
    assert report["real_trading_enabled"] is False
    assert report["live_execution_enabled"] is False

    registry_root = tmp_path / "review" / "champion_registry"
    current = ChampionSelectionRegistry(registry_root).load_current()
    assert current.receipt_sha256 == execution["receipt_sha256"]
    assert current.real_trading_enabled is False

    published = json.loads(
        (tmp_path / "review" / "evolution_decision_latest.json").read_text(
            encoding="utf-8"
        )
    )
    assert published["promotion_execution"]["status"] == "promoted"
    assert published["real_trading_enabled"] is False
    assert published["live_transition_authorized"] is False


def test_sample_ops_explicit_candidates_take_precedence_over_producer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shared.runtime_test.ashare_sample_ops as sample_ops

    def forbidden_producer(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("producer must not run for explicit candidates")

    monkeypatch.setattr(sample_ops, "build_challenger_candidates", forbidden_producer)

    report = _run_sample_ops(tmp_path, monkeypatch, challenger_candidates=[])

    execution = report["evolution_decision"]["promotion_execution"]
    assert execution["status"] == "no_op"
    assert execution["reason"] == "no_qualified_challenger"
    assert execution["challenger_rejections"] == []
