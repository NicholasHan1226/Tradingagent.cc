from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pytest

from Ashare.sample_pipeline import (
    build_candidate_observation,
    execution_attribution,
    persist_candidate_observations,
    persist_simulation_outcomes,
    select_exploration_candidate,
)
from shared.review.sample_journal import (
    SampleJournal,
    build_strict_execution_evidence_index,
    prediction_source_payload_sha256,
    validate_strict_completed_round_trip_evidence,
)
from shared.orchestrator import _ashare_order_attribution


AUTHORITY = {
    "capital_authority_id": "ashare-capital-v1",
    "authority_generation": 1,
    "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
}


class ReliableReader:
    def get_bars_intraday(self, market, symbol, interval, start, end):
        return [
            {
                "close": 10.25,
                "bar_time": "2026-07-13T10:00:00+08:00",
                "volume": 88_000,
                "provider": "sharedsignals_api_realtime_5min",
                "available_at": "2026-07-13T10:00:00+08:00",
                "ingested_at": "2026-07-13T10:00:00+08:00",
                "retrieved_as_of": "2026-07-13T10:00:00+08:00",
            }
        ]

    def get_bars_daily(self, market, symbol, start, end):
        return []


def _score(value: float, **overrides) -> dict[str, object]:
    result: dict[str, object] = {
        "combined": value,
        "macro": value,
        "event": value,
        "fundamental": value,
        "capital": value,
        "technical": value,
        "sentiment": value,
        "turnover_wan": 20_000,
        "evidence_coverage": 1.0,
        "missing_evidence_dimensions": [],
    }
    result.update(overrides)
    return result


def _observation(
    symbol: str = "600001.SH",
    *,
    prediction_at: str = "2026-07-13T10:01:00+08:00",
    mg_enabled: bool = False,
    score: dict[str, object] | None = None,
) -> dict[str, object]:
    return build_candidate_observation(
        symbol=symbol,
        trade_date="20260713",
        mapped_market="ashare",
        mapped_symbol=symbol,
        score=score or _score(0.72),
        reader=ReliableReader(),
        prediction_at=prediction_at,
        mg_enabled=mg_enabled,
        authority_scope=AUTHORITY,
    )


def _selection(
    observation: dict[str, object], *, seed: str = "seed-1"
) -> dict[str, object]:
    return select_exploration_candidate(
        [observation],
        normal_candidate_symbols=[],
        sample_debt=True,
        epsilon=0.2,
        top_k=3,
        selection_seed=seed,
    )


def _execution_record(
    attribution: dict[str, object],
    *,
    order_id: str,
    trade_id: str,
    side: str,
    actual_quantity: int | None,
    requested_quantity: int = 100,
    price: float = 10.0,
    status: str = "filled",
    filled_at: str | None = None,
    receipt_quantity_only: int | None = None,
) -> dict[str, object]:
    receipt: dict[str, object] = {
        "status": status,
        "execution_eligible": True,
        "trade_id": trade_id,
        "filled_price": price,
        "fee_cny": 5.0,
        "slippage_cny": 1.0,
        "filled_at": filled_at
        or (
            "2026-07-14T14:30:00+08:00"
            if side == "sell"
            else "2026-07-13T10:05:00+08:00"
        ),
        "source_snapshot_sha256": "c" * 64,
    }
    if actual_quantity is not None:
        receipt["filled_quantity"] = actual_quantity
    if receipt_quantity_only is not None:
        receipt["quantity"] = receipt_quantity_only
    receipt["received_at"] = receipt["filled_at"]
    return {
        "symbol": "600001.SH",
        "account": "ashare_sim",
        "order": {
            "order_id": order_id,
            "strategy_name": "ashare_sim",
            "side": side,
            "quantity": requested_quantity,
            **attribution,
        },
        "receipt": receipt,
    }


def test_prediction_chain_has_current_authority_pit_and_content_hashes() -> None:
    observation = _observation()

    assert observation["capital_authority_id"] == AUTHORITY["capital_authority_id"]
    assert observation["authority_generation"] == AUTHORITY["authority_generation"]
    assert observation["execution_lineage_id"] == AUTHORITY["execution_lineage_id"]
    for snapshot in observation["prediction_snapshots"]:
        assert snapshot["capital_authority_id"] == AUTHORITY["capital_authority_id"]
        assert snapshot["authority_generation"] == AUTHORITY["authority_generation"]
        assert snapshot["execution_lineage_id"] == AUTHORITY["execution_lineage_id"]
        assert snapshot["as_of"] == "2026-07-13T10:01:00+08:00"
        assert snapshot["point_in_time_as_of"] == snapshot["as_of"]
        assert snapshot["source_snapshot_payload"]
        assert snapshot["source_snapshot_sha256"] == prediction_source_payload_sha256(
            snapshot["source_snapshot_payload"]
        )
        assert len(snapshot["source_snapshot_sha256"]) == 64
        assert len(snapshot["content_sha256"]) == 64


def test_mg_on_off_are_paired_from_one_base_and_off_physically_excludes_mg() -> None:
    observation = _observation(
        mg_enabled=True,
        score=_score(
            0.55,
            base_score=_score(0.55),
            marketgraph_features={
                "trend_strength": 0.99,
                "event_catalyst_score": 0.98,
            },
            mg_hidden_leak=0.97,
        ),
    )

    assert len(observation["prediction_snapshots"]) == 8
    by_style: dict[str, list[dict[str, object]]] = defaultdict(list)
    for snapshot in observation["prediction_snapshots"]:
        by_style[str(snapshot["style_id"])].append(snapshot)

    assert len(by_style) == 4
    for rows in by_style.values():
        assert {row["marketgraph"]["ablation_group"] for row in rows} == {
            "mg_off",
            "mg_on",
        }
        off = next(
            row for row in rows if row["marketgraph"]["ablation_group"] == "mg_off"
        )
        on = next(
            row for row in rows if row["marketgraph"]["ablation_group"] == "mg_on"
        )
        assert off["pair_id"] == on["pair_id"]
        assert off["base_snapshot_sha256"] == on["base_snapshot_sha256"]
        assert off["marketgraph"]["applied_features"] == {}
        assert off["marketgraph"]["features_physically_excluded"] is True
        assert all("mg" not in key.lower() for key in off["feature_snapshot"])
        assert on["marketgraph"]["applied_features"]["trend_strength"] == pytest.approx(
            0.99
        )


def test_mg_overlay_without_explicit_base_is_not_applied_or_claimed_causal() -> None:
    observation = _observation(
        mg_enabled=True,
        score=_score(
            0.55,
            marketgraph_features={"trend_strength": 0.99},
        ),
    )

    assert observation["mg_ablation_pairing"]["overlay_status"] == (
        "rejected_missing_explicit_base_score"
    )
    assert observation["mg_ablation_pairing"]["causal_pair_eligible"] is False
    on_rows = [
        row
        for row in observation["prediction_snapshots"]
        if row["marketgraph"]["ablation_group"] == "mg_on"
    ]
    assert on_rows
    assert all(row["marketgraph"]["applied_features"] == {} for row in on_rows)
    assert all(
        row["marketgraph"]["overlay_status"] == "rejected_missing_explicit_base_score"
        for row in on_rows
    )
    assert all(row["marketgraph"]["causal_pair_eligible"] is False for row in on_rows)


def test_mg_pair_can_derive_style_overlay_only_from_same_snapshot_enhanced_score() -> (
    None
):
    base = _score(0.55, technical=0.40, event=0.45)
    enhanced = _score(0.62, technical=0.90, event=0.80)
    observation = _observation(
        mg_enabled=True,
        score=_score(
            0.55,
            base_score=base,
            marketgraph_score=enhanced,
            marketgraph_pairing={
                "same_scoring_snapshot": True,
                "used_dimensions": ["event", "technical"],
                "pairing_version": "six-dimension-mg-pair-v1",
            },
        ),
    )

    assert observation["mg_ablation_pairing"]["overlay_status"] == (
        "derived_from_same_scoring_snapshot"
    )
    assert observation["mg_ablation_pairing"]["causal_pair_eligible"] is True
    off = next(
        row
        for row in observation["prediction_snapshots"]
        if row["style_id"] == "trend_breakout_strength_continuation"
        and row["marketgraph"]["ablation_group"] == "mg_off"
    )
    on = next(
        row
        for row in observation["prediction_snapshots"]
        if row["style_id"] == "trend_breakout_strength_continuation"
        and row["marketgraph"]["ablation_group"] == "mg_on"
    )
    assert off["feature_snapshot"]["trend_strength"] == pytest.approx(0.475)
    assert on["feature_snapshot"]["trend_strength"] == pytest.approx(0.76)
    assert off["base_snapshot_sha256"] == on["base_snapshot_sha256"]


def test_exploration_is_reproducible_top_k_epsilon_greedy_with_propensity() -> None:
    observations = [
        _observation("600001.SH", score=_score(0.81)),
        _observation("600002.SH", score=_score(0.79)),
        _observation("600003.SH", score=_score(0.77)),
        _observation("600004.SH", score=_score(0.30)),
    ]

    first = select_exploration_candidate(
        observations,
        normal_candidate_symbols=[],
        sample_debt=True,
        epsilon=1.0,
        top_k=3,
        selection_seed="reproducible-seed",
    )
    second = select_exploration_candidate(
        observations,
        normal_candidate_symbols=[],
        sample_debt=True,
        epsilon=1.0,
        top_k=3,
        selection_seed="reproducible-seed",
    )

    assert first == second
    assert first["symbol"] in {"600001.SH", "600002.SH", "600003.SH"}
    assert first["eligible_top_k_count"] == 3
    assert first["selection_probability"] == pytest.approx(1 / 3)
    assert first["propensity"] == first["selection_probability"]
    assert first["exploration_policy_version"] == "ashare-safe-top-k-epsilon-greedy-v1"
    assert len(first["selection_seed_sha256"]) == 64

    greedy = select_exploration_candidate(
        observations,
        normal_candidate_symbols=[],
        sample_debt=True,
        epsilon=0.0,
        top_k=3,
        selection_seed="any-seed",
    )
    assert greedy["symbol"] == "600001.SH"
    assert greedy["selection_probability"] == 1.0


def test_no_executable_primary_style_returns_bound_abstain_attribution_not_exception() -> (
    None
):
    observation = _observation(score=_score(0.01))

    attribution = execution_attribution(
        observation,
        sample_intent="exploitation",
    )

    assert attribution["primary_style"] is None
    assert attribution["attribution_status"] == "abstain_no_executable_primary_style"
    assert attribution["execution_allowed_by_style_attribution"] is False
    assert (
        attribution["prediction_snapshot_role"]
        == "observation_anchor_not_execution_thesis"
    )
    assert attribution["prediction_snapshot_id"] in {
        row["snapshot_id"] for row in observation["prediction_snapshots"]
    }
    assert attribution["capital_authority_id"] == AUTHORITY["capital_authority_id"]
    assert attribution["authority_generation"] == AUTHORITY["authority_generation"]
    assert attribution["execution_lineage_id"] == AUTHORITY["execution_lineage_id"]


def test_orchestrator_attribution_fallback_keeps_real_anchor_and_stays_non_executable() -> (
    None
):
    observation = _observation(score=_score(0.01))

    attribution = _ashare_order_attribution(
        observation,
        sample_intent="exploitation",
        account="ashare_sim",
    )

    assert attribution["primary_style"] == "legacy_six_dimension_champion"
    assert attribution["execution_allowed_by_style_attribution"] is False
    assert (
        attribution["prediction_snapshot_role"]
        == "observation_anchor_not_execution_thesis"
    )
    assert attribution["prediction_snapshot_id"] in {
        row["snapshot_id"] for row in observation["prediction_snapshots"]
    }
    assert attribution["capital_authority_id"] == AUTHORITY["capital_authority_id"]
    assert attribution["authority_generation"] == AUTHORITY["authority_generation"]
    assert attribution["execution_lineage_id"] == AUTHORITY["execution_lineage_id"]


def test_partial_fill_uses_only_actual_quantity_and_never_requested_fallback(
    tmp_path: Path,
) -> None:
    observation = _observation()
    path = tmp_path / "samples.jsonl"
    persist_candidate_observations([observation], journal_path=path)
    attribution = execution_attribution(
        observation,
        sample_intent="exploration",
        selection=_selection(observation),
    )

    report = persist_simulation_outcomes(
        journal_path=path,
        trade_date="20260713",
        records=[
            _execution_record(
                attribution,
                order_id="PARTIAL-1",
                trade_id="TRADE-PARTIAL-1",
                side="buy",
                actual_quantity=100,
                requested_quantity=300,
                status="partial",
            ),
            _execution_record(
                attribution,
                order_id="NO-ACTUAL-1",
                trade_id="TRADE-NO-ACTUAL-1",
                side="buy",
                actual_quantity=None,
                requested_quantity=300,
                receipt_quantity_only=300,
            ),
        ],
        risk_rejections=[],
        authority_scope=AUTHORITY,
    )

    fill = next(
        row for row in SampleJournal(path).read_events() if row["record_type"] == "fill"
    )
    assert fill["status"] == "partial"
    assert fill["filled_quantity"] == 100
    assert fill["requested_quantity"] == 300
    assert fill["unfilled_quantity"] == 200
    assert report["exploration_fill_count"] == 1
    assert report["skipped_outcomes"] == [
        {
            "symbol": "600001.SH",
            "order_id": "NO-ACTUAL-1",
            "reason": "actual_filled_quantity_missing",
        }
    ]


@pytest.mark.parametrize("claim_field", ["receipt_sha256", "local_trade_sha256"])
def test_execution_source_hash_claim_must_match_canonical_fill_payload(
    tmp_path: Path,
    claim_field: str,
) -> None:
    observation = _observation()
    path = tmp_path / ("%s.jsonl" % claim_field)
    persist_candidate_observations([observation], journal_path=path)
    attribution = execution_attribution(
        observation,
        sample_intent="exploration",
        selection=_selection(observation),
    )
    record = _execution_record(
        attribution,
        order_id="CLAIM-1",
        trade_id="TRADE-CLAIM-1",
        side="buy",
        actual_quantity=100,
    )
    if claim_field == "receipt_sha256":
        record["receipt"][claim_field] = "a" * 64
    else:
        record[claim_field] = "b" * 64

    report = persist_simulation_outcomes(
        journal_path=path,
        trade_date="20260713",
        records=[record],
        risk_rejections=[],
        authority_scope=AUTHORITY,
    )

    assert report["exploration_fill_count"] == 0
    assert report["skipped_outcomes"] == [
        {
            "symbol": "600001.SH",
            "order_id": "CLAIM-1",
            "reason": "%s_content_mismatch" % claim_field,
        }
    ]
    assert all(
        event.get("record_type") != "fill"
        for event in SampleJournal(path).read_events()
    )


def test_pairing_requires_exact_prediction_and_lineage_and_round_trip_cost_contract(
    tmp_path: Path,
) -> None:
    path = tmp_path / "samples.jsonl"
    entry_observation = _observation(prediction_at="2026-07-13T10:01:00+08:00")
    other_observation = _observation(prediction_at="2026-07-13T10:03:00+08:00")
    persist_candidate_observations(
        [entry_observation, other_observation], journal_path=path
    )
    entry_attribution = execution_attribution(
        entry_observation,
        sample_intent="exploration",
        selection=_selection(entry_observation, seed="entry"),
    )
    other_attribution = execution_attribution(
        other_observation,
        sample_intent="exploration",
        selection=_selection(other_observation, seed="other"),
    )

    persist_simulation_outcomes(
        journal_path=path,
        trade_date="20260713",
        records=[
            _execution_record(
                entry_attribution,
                order_id="BUY-1",
                trade_id="TRADE-BUY-1",
                side="buy",
                actual_quantity=100,
            )
        ],
        risk_rejections=[],
        authority_scope=AUTHORITY,
    )
    rejected = persist_simulation_outcomes(
        journal_path=path,
        trade_date="20260714",
        records=[
            _execution_record(
                other_attribution,
                order_id="SELL-WRONG-SNAPSHOT",
                trade_id="TRADE-SELL-WRONG-SNAPSHOT",
                side="sell",
                actual_quantity=100,
                price=11.0,
            )
        ],
        risk_rejections=[],
        authority_scope=AUTHORITY,
    )
    assert rejected["pairing_rejection_count"] == 1
    assert rejected["completed_round_trip_count"] == 0

    completed = persist_simulation_outcomes(
        journal_path=path,
        trade_date="20260714",
        records=[
            _execution_record(
                entry_attribution,
                order_id="SELL-1",
                trade_id="TRADE-SELL-1",
                side="sell",
                actual_quantity=100,
                price=11.0,
            )
        ],
        risk_rejections=[],
        authority_scope=AUTHORITY,
    )
    assert completed["completed_round_trip_count"] == 1
    events = SampleJournal(path).read_events()
    buy = next(row for row in events if row["record_type"] == "fill")
    round_trip = next(
        row for row in events if row["record_type"] == "completed_round_trip"
    )
    assert all(str(AUTHORITY[key]) in buy["fill_identity"] for key in AUTHORITY)
    assert entry_attribution["prediction_snapshot_id"] in buy["fill_identity"]
    assert (
        round_trip["prediction_snapshot_id"]
        == entry_attribution["prediction_snapshot_id"]
    )
    assert round_trip["capital_authority_id"] == AUTHORITY["capital_authority_id"]
    assert round_trip["authority_generation"] == AUTHORITY["authority_generation"]
    assert round_trip["execution_lineage_id"] == AUTHORITY["execution_lineage_id"]
    assert round_trip["round_trip_complete"] is True
    assert round_trip["execution_eligible"] is True
    assert round_trip["costs_cover"] == "round_trip"
    assert round_trip["cost_model_version"] == "actual_execution_costs_v1"
    assert round_trip["fee_cny"] == pytest.approx(10.0)
    assert round_trip["slippage_cny"] == pytest.approx(2.0)
    assert len(round_trip["source_snapshot_sha256"]) == 64
    assert len(round_trip["content_sha256"]) == 64
    assert len(round_trip["entry_receipt_sha256"]) == 64
    assert len(round_trip["entry_local_trade_sha256"]) == 64
    assert all(len(value) == 64 for value in round_trip["exit_receipt_sha256s"])
    assert all(len(value) == 64 for value in round_trip["exit_local_trade_sha256s"])
    assert round_trip["point_in_time_lineage"]["complete"] is True
    assert round_trip["evidence_envelope_validation"]["status"] == "valid"
    strict_validation = validate_strict_completed_round_trip_evidence(
        round_trip,
        boundary=datetime.fromisoformat("2026-07-14T16:00:00+08:00"),
        prediction_snapshot_id=entry_attribution["prediction_snapshot_id"],
        evidence_index=build_strict_execution_evidence_index(events),
    )
    assert strict_validation["valid"] is True, strict_validation


def _journal_prediction(
    *,
    snapshot_id: str,
    prediction_at: str,
    generation: int = 1,
) -> dict[str, object]:
    return {
        "snapshot_id": snapshot_id,
        "market": "ashare",
        "symbol": "600000.SH",
        "style": "trend_breakout",
        "strategy_version": "trend-v1",
        "prediction_at": prediction_at,
        "reference_price": 10.0,
        "direction": "long",
        "raw_style_score": 0.7,
        "marketgraph": {"ablation_group": "mg_off"},
        "capital_authority_id": "ashare-capital-v1",
        "authority_generation": generation,
        "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
        "data_quality": {
            "reliable": True,
            "source": "sharedsignals.5min",
            "price_timestamp": "2026-07-13T10:00:00+08:00",
        },
        "costs": {
            "round_trip_fee_bps": 105.0,
            "round_trip_slippage_bps": 10.0,
            "cost_model_version": "ashare-execution-reality-20260706-v1",
        },
        "real_trading_enabled": False,
    }


def test_journal_clusters_five_minute_duplicates_and_kpi_excludes_legacy_generation(
    tmp_path: Path,
) -> None:
    journal = SampleJournal(tmp_path / "samples.jsonl")
    journal.append_prediction(
        _journal_prediction(
            snapshot_id="current-origin",
            prediction_at="2026-07-13T10:01:00+08:00",
        )
    )
    journal.append_prediction(
        _journal_prediction(
            snapshot_id="current-duplicate",
            prediction_at="2026-07-13T10:04:00+08:00",
        )
    )
    journal.append_prediction(
        _journal_prediction(
            snapshot_id="legacy-generation",
            prediction_at="2026-07-13T10:06:00+08:00",
            generation=0,
        )
    )

    current = [
        event for event in journal.read_events() if event["authority_generation"] == 1
    ]
    assert [event["cluster_role"] for event in current] == ["origin", "duplicate"]
    assert [event["maturity_weight"] for event in current] == [1.0, 0.0]
    assert current[0]["sample_cluster_id"] == current[1]["sample_cluster_id"]

    kpi = journal.build_kpi(authority_scope=AUTHORITY)
    assert kpi["raw_current_authority_record_count"] == 2
    assert kpi["excluded_legacy_count"] == 1
    assert kpi["maturity_duplicate_count"] == 1
    assert kpi["maturity_effective_record_count"] == 1
    assert kpi["sample_layer_totals"]["observation_counterfactual"] == 1
    assert kpi["automatic_promotion_enabled"] is False
    assert kpi["promotion_state"] == "manual_review_only"


def test_kpi_excludes_current_authority_round_trip_without_strict_execution_evidence(
    tmp_path: Path,
) -> None:
    journal = SampleJournal(tmp_path / "samples.jsonl")
    journal.append_sample(
        {
            "event_id": "malformed-current-round-trip",
            "record_type": "completed_round_trip",
            "round_trip_complete": True,
            "sample_intent": "exploration",
            "primary_style": "trend_breakout",
            "gross_pnl_cny": 999.0,
            "net_pnl_cny": 999.0,
            **AUTHORITY,
            "real_trading_enabled": False,
        }
    )

    kpi = journal.build_kpi(authority_scope=AUTHORITY)

    assert kpi["sample_layer_totals"]["completed_round_trip"] == 0
    assert kpi["invalid_evolution_evidence_count"] == 1
    assert kpi["automatic_promotion_enabled"] is False
