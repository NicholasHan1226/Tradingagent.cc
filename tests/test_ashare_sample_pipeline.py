from __future__ import annotations

from copy import deepcopy
import pytest

from Ashare.sample_pipeline import (
    build_candidate_observation,
    execution_attribution,
    persist_candidate_observations,
    persist_simulation_outcomes,
    select_exploration_candidate,
)
from shared.review.sample_journal import SampleJournal


AUTHORITY = {
    "capital_authority_id": "ashare-capital-v1",
    "authority_generation": 1,
    "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
}
EXECUTION_SNAPSHOT_ID = "linked-execution-snapshot"


class ReliableReader:
    def get_bars_intraday(self, market, symbol, interval, start, end):
        return [
            {
                "close": 10.25,
                "bar_time": "2026-07-13T10:00:00+08:00",
                "volume": 88_000,
                "provider": "sharedsignals_api_realtime_5min",
            }
        ]

    def get_bars_daily(self, market, symbol, start, end):
        return []


class ProductionShapedIntradayReader:
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


class MissingPriceReader:
    def get_bars_intraday(self, market, symbol, interval, start, end):
        return []

    def get_bars_daily(self, market, symbol, start, end):
        return []


class LowPriceReader:
    """Returns 10 yuan price for cost model boundary testing."""

    def get_bars_intraday(self, market, symbol, interval, start, end):
        return [
            {
                "close": 10.0,
                "bar_time": "2026-07-13T10:00:00+08:00",
                "volume": 50_000,
                "provider": "sharedsignals_api_realtime_5min",
            }
        ]

    def get_bars_daily(self, market, symbol, start, end):
        return []


class HighPriceReader:
    """Returns 200 yuan price for cost model rate-based testing."""

    def get_bars_intraday(self, market, symbol, interval, start, end):
        return [
            {
                "close": 200.0,
                "bar_time": "2026-07-13T10:00:00+08:00",
                "volume": 100_000,
                "provider": "sharedsignals_api_realtime_5min",
            }
        ]

    def get_bars_daily(self, market, symbol, start, end):
        return []


def _score(value: float, **overrides) -> dict:
    payload = {
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
    payload.update(overrides)
    return payload


def _observation(symbol: str, score: float, *, reader=None, mg_enabled=False):
    return build_candidate_observation(
        symbol=symbol,
        trade_date="20260713",
        mapped_market="ashare",
        mapped_symbol=symbol,
        score=_score(score),
        reader=reader or ReliableReader(),
        prediction_at="2026-07-13T10:01:00+08:00",
        mg_enabled=mg_enabled,
    )


# -- v2 field tests -----------------------------------------------------------


def test_prediction_snapshots_use_v2_field_names_not_probability():
    observation = _observation("600000.SH", 0.43)

    for row in observation["prediction_snapshots"]:
        assert "probability" not in row
        assert "raw_style_score" in row
        assert row["rank_score"] == row["raw_style_score"]
        assert row["score_semantics"] == "uncalibrated_rank_score"
        assert row["primary_label_horizon"] in {"1d", "close"}
        assert row["primary_horizon_policy_version"] == "ashare-primary-horizon-v1"
        assert row["point_in_time_lineage_validation"]["complete"] is False
        assert row["forward_label_eligibility"] == "eligible"
        assert "expected_return_distribution" not in row
        assert "uncalibrated_return_prior" in row


def test_production_intraday_timestamp_and_receipt_form_complete_pit_lineage():
    observation = build_candidate_observation(
        symbol="000021.SZ",
        trade_date="20260713",
        mapped_market="ashare",
        mapped_symbol="000021.SZ",
        score=_score(0.68),
        reader=ProductionShapedIntradayReader(),
        prediction_at="2026-07-13T13:46:00+08:00",
        mg_enabled=False,
    )

    for row in observation["prediction_snapshots"]:
        assert row["trade_date"] == "20260713"
        assert row["data_quality"]["price_timestamp"] == (
            "2026-07-13T13:40:00+08:00"
        )
        assert row["event_time"] == "2026-07-13T13:40:00+08:00"
        assert row["available_at"] == "2026-07-13T05:45:02+00:00"
        assert row["ingested_at"] == "2026-07-13T05:45:02+00:00"
        assert row["point_in_time_lineage_validation"]["status"] == "valid"
        assert row["point_in_time_lineage_validation"]["complete"] is True
        assert row["forward_label_eligibility"] == "eligible"


def test_prediction_snapshots_have_embedded_conservative_costs():
    observation = _observation("600000.SH", 0.43)

    for row in observation["prediction_snapshots"]:
        assert row["cost_evidence_status"] == "embedded_conservative"
        costs = row["costs"]
        assert costs is not None
        assert costs["cost_model_version"] == "ashare-execution-reality-20260706-v1"
        assert "round_trip_fee_bps" in costs
        assert "round_trip_slippage_bps" in costs


def test_conservative_costs_at_10_yuan_embed_correct_bps():
    observation = build_candidate_observation(
        symbol="600000.SH",
        trade_date="20260713",
        mapped_market="ashare",
        mapped_symbol="600000.SH",
        score=_score(0.43),
        reader=LowPriceReader(),
        prediction_at="2026-07-13T10:01:00+08:00",
    )

    for row in observation["prediction_snapshots"]:
        costs = row["costs"]
        assert costs["round_trip_fee_bps"] == pytest.approx(105.2)
        assert costs["round_trip_slippage_bps"] == pytest.approx(10.0)
        assert costs["cost_basis_notional_cny"] == pytest.approx(1000.0)
        assert costs["buy_transfer_fee_cny"] == pytest.approx(0.01)
        assert costs["sell_transfer_fee_cny"] == pytest.approx(0.01)


def test_conservative_costs_at_high_price_uses_percentage():
    observation = build_candidate_observation(
        symbol="600000.SH",
        trade_date="20260713",
        mapped_market="ashare",
        mapped_symbol="600000.SH",
        score=_score(0.43),
        reader=HighPriceReader(),
        prediction_at="2026-07-13T10:01:00+08:00",
    )

    for row in observation["prediction_snapshots"]:
        costs = row["costs"]
        assert costs["cost_basis_notional_cny"] == pytest.approx(20000.0)
        assert costs["round_trip_fee_bps"] == pytest.approx(10.2)
        assert costs["round_trip_slippage_bps"] == pytest.approx(10.0)
        assert costs["buy_transfer_fee_cny"] == pytest.approx(0.2)
        assert costs["sell_transfer_fee_cny"] == pytest.approx(0.2)


def test_missing_price_sets_cost_evidence_rejected():
    observation = _observation("600001.SH", 0.80, reader=MissingPriceReader())

    for row in observation["prediction_snapshots"]:
        assert row["cost_evidence_status"] == "rejected_missing_cost_evidence"
        assert row["costs"] is None


# -- existing tests adapted for v2 --------------------------------------------


def test_reliable_scored_candidate_emits_four_style_snapshots_before_strategy_gates():
    score = _score(0.43)
    before = deepcopy(score)

    result = build_candidate_observation(
        symbol="600000.SH",
        trade_date="20260713",
        mapped_market="ashare",
        mapped_symbol="600000.SH",
        score=score,
        reader=ReliableReader(),
        prediction_at="2026-07-13T10:01:00+08:00",
        mg_enabled=True,
    )

    assert score == before
    assert result["status"] == "recordable"
    assert result["data_quality"]["qualified"] is True
    assert result["reference_price"] == 10.25
    assert result["reference_evidence"]["source"] == "sharedsignals_api_realtime_5min"
    assert len(result["prediction_snapshots"]) == 8
    assert all(
        row["sample_layer"] == "observation_counterfactual"
        for row in result["prediction_snapshots"]
    )
    assert all(
        row["forward_label_eligibility"] == "eligible"
        for row in result["prediction_snapshots"]
    )
    assert {
        row["marketgraph"]["ablation_group"] for row in result["prediction_snapshots"]
    } == {"mg_off", "mg_on"}
    assert all(
        row["mature_threshold_passed"] is False
        for row in result["prediction_snapshots"]
    )
    assert result["real_trading_enabled"] is False


def test_missing_real_price_keeps_predictions_but_never_becomes_exploration_eligible():
    observation = _observation("600001.SH", 0.80, reader=MissingPriceReader())

    assert observation["status"] == "recordable_data_quality_rejected"
    assert observation["data_quality"]["qualified"] is False
    assert len(observation["prediction_snapshots"]) == 4
    assert all(
        row["forward_label_eligibility"] == "rejected_data_quality"
        for row in observation["prediction_snapshots"]
    )

    selection = select_exploration_candidate(
        [observation],
        normal_candidate_symbols=[],
        sample_debt=True,
    )
    assert selection["status"] == "not_selected"
    assert selection["reason"] == "no_data_qualified_exploration_candidate"


def test_exploration_uses_relative_rank_and_selects_one_below_mature_absolute_threshold():
    observations = [
        _observation("600001.SH", 0.41),
        _observation("600002.SH", 0.44),
        _observation("600003.SH", 0.39),
    ]

    selection = select_exploration_candidate(
        observations,
        normal_candidate_symbols=[],
        sample_debt=True,
    )

    assert selection["status"] == "selected"
    assert selection["symbol"] == "600002.SH"
    assert selection["selection_method"] == "deterministic_top_k_epsilon_greedy"
    assert selection["opportunity_capture_scope"]["claim_scope"] == (
        "scanned_universe_only"
    )
    assert (
        selection["opportunity_capture_scope"]["full_eligible_universe_recall"] is None
    )
    assert selection["propensity"] == selection["selection_probability"]
    assert selection["absolute_mature_threshold_required"] is False
    assert selection["relative_rank"] == 1
    assert selection["selected_count"] == 1
    assert selection["sample_intent"] == "exploration"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"sample_debt": False}, "sample_debt_repaid"),
        (
            {"sample_debt": True, "existing_exploration_new_positions": 1},
            "exploration_daily_position_limit_reached",
        ),
        (
            {"sample_debt": True, "safety_blockers": ["master_drawdown_halt"]},
            "safety_gate_blocked",
        ),
    ],
)
def test_exploration_never_bypasses_sample_or_safety_limits(kwargs, reason):
    selection = select_exploration_candidate(
        [_observation("600001.SH", 0.80)],
        normal_candidate_symbols=[],
        **kwargs,
    )

    assert selection["status"] == "not_selected"
    assert selection["reason"] == reason
    assert selection["selected_count"] == 0


def test_normal_candidate_is_not_duplicated_by_exploration_and_attribution_is_single_portfolio():
    observation = _observation("600001.SH", 0.80)

    selection = select_exploration_candidate(
        [observation],
        normal_candidate_symbols=["600001.SH"],
        sample_debt=True,
    )
    assert selection["status"] == "not_selected"
    assert selection["reason"] == "no_data_qualified_exploration_candidate"

    attribution = execution_attribution(observation, sample_intent="exploration")
    assert attribution["sample_intent"] == "exploration"
    assert attribution["primary_style"]
    assert attribution["decision_policy_version"]
    assert set(attribution["style_scores"]) == set(attribution["style_versions"])
    assert attribution["capital_authority_id"] == "ashare-capital-v1"
    assert attribution["authority_generation"] == 1
    assert attribution["execution_lineage_id"] == "ashare-sim-fresh-20260712-v1"
    assert attribution["real_trading_enabled"] is False


def test_observations_are_append_only_per_style_and_idempotent(tmp_path):
    observations = [
        _observation("600001.SH", 0.41, mg_enabled=False),
        _observation("600002.SH", 0.44, mg_enabled=True),
    ]
    path = tmp_path / "ashare_samples.jsonl"

    first = persist_candidate_observations(observations, journal_path=path)
    second = persist_candidate_observations(observations, journal_path=path)

    assert first["prediction_count"] == 12
    assert first["appended_count"] == 12
    assert second["idempotent_count"] == 12
    assert len(SampleJournal(path).read_events()) == 12
    assert first["mg_ablation_counts"] == {"mg_off": 8, "mg_on": 4}
    assert first["real_trading_enabled"] is False


def test_simulation_outcomes_keep_exploration_fill_and_risk_reject_separate(tmp_path):
    observation = _observation("600001.SH", 0.44)
    path = tmp_path / "ashare_samples.jsonl"
    persist_candidate_observations([observation], journal_path=path)
    selection = select_exploration_candidate(
        [observation],
        normal_candidate_symbols=[],
        sample_debt=True,
        selection_seed="outcome-test",
    )
    attribution = execution_attribution(
        observation,
        sample_intent="exploration",
        selection=selection,
    )

    report = persist_simulation_outcomes(
        journal_path=path,
        trade_date="20260713",
        records=[
            {
                "symbol": "600001.SH",
                "order": {
                    "order_id": "SIM-1",
                    "side": "buy",
                    "sample_intent": "exploration",
                    **attribution,
                },
                "receipt": {
                    "status": "filled",
                    "execution_eligible": True,
                    "filled_quantity": 100,
                    "filled_price": 10.25,
                    "commission": 5.0,
                    "slippage_cny": 1.0,
                    "filled_at": "2026-07-13T10:05:00+08:00",
                },
            }
        ],
        risk_rejections=[
            {
                "symbol": "600002.SH",
                "reasons": ["liquidity_gate"],
                "sample_intent": "exploration",
                "primary_style": "event_catalyst_with_price_confirmation",
            }
        ],
    )

    assert report["exploration_fill_count"] == 1
    assert report["exploitation_fill_count"] == 0
    assert report["risk_reject_count"] == 1
    kpi = SampleJournal(path).build_kpi()
    assert kpi["sample_layer_totals"]["exploration_fill"] == 1
    assert kpi["sample_layer_totals"]["risk_reject"] == 1
    assert kpi["sample_layer_totals"]["exploitation_fill"] == 0


def test_unverified_fill_is_not_promoted_to_execution_sample(tmp_path):
    path = tmp_path / "ashare_samples.jsonl"

    report = persist_simulation_outcomes(
        journal_path=path,
        trade_date="20260713",
        records=[
            {
                "symbol": "600001.SH",
                "order": {
                    "order_id": "SIM-unsafe",
                    "sample_intent": "exploration",
                    "primary_style": "trend_breakout_strength_continuation",
                },
                "receipt": {
                    "status": "filled",
                    "execution_eligible": False,
                    "filled_quantity": 100,
                    "filled_price": 10.25,
                },
            }
        ],
        risk_rejections=[],
    )

    assert report["exploration_fill_count"] == 0
    assert report["skipped_outcome_count"] == 1
    assert report["skipped_outcomes"][0]["reason"] == "fill_not_execution_eligible"
    assert SampleJournal(path).read_events() == []


def _filled_execution(
    *,
    order_id: str,
    trade_id: str,
    side: str,
    quantity: int,
    price: float,
    sample_intent: str = "exploration",
    symbol: str = "600001.SH",
    account: str = "ashare_sim",
    primary_style: str = "trend_breakout_strength_continuation",
    fee_cny: float = 5.0,
    slippage_cny: float = 1.0,
    exit_reason: str = "",
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "account": account,
        "order": {
            "order_id": order_id,
            "strategy_name": account,
            "side": side,
            "sample_intent": sample_intent,
            "primary_style": primary_style,
            "supporting_styles": ["event_catalyst_with_price_confirmation"],
            "style_scores": {primary_style: 0.7},
            "style_versions": {primary_style: "style-v1"},
            "decision_policy_version": "portfolio-v1",
            "exit_reason": exit_reason,
            "prediction_snapshot_id": EXECUTION_SNAPSHOT_ID,
            **AUTHORITY,
            "prediction_source_snapshot_sha256": "a" * 64,
            "selection_probability": 1.0,
            "propensity": 1.0,
            "exploration_policy_version": "ashare-safe-top-k-epsilon-greedy-v1",
            "selection_seed_sha256": "b" * 64,
            "selection_method": "deterministic_top_k_epsilon_greedy",
        },
        "receipt": {
            "status": "filled",
            "execution_eligible": True,
            "trade_id": trade_id,
            "filled_quantity": quantity,
            "filled_price": price,
            "fee_cny": fee_cny,
            "slippage_cny": slippage_cny,
            "filled_at": "2026-07-14T14:30:00+08:00"
            if side == "sell"
            else "2026-07-13T10:00:00+08:00",
        },
    }


def _append_execution_prediction(path) -> None:
    SampleJournal(path).append_prediction(
        {
            "snapshot_id": EXECUTION_SNAPSHOT_ID,
            "market": "ashare",
            "symbol": "600001.SH",
            "style": "trend_breakout_strength_continuation",
            "strategy_version": "style-v1",
            "prediction_at": "2026-07-13T10:00:00+08:00",
            "reference_price": 10.0,
            "direction": "long",
            "raw_style_score": 0.7,
            "marketgraph": {"ablation_group": "mg_off"},
            **AUTHORITY,
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
    )


def test_sell_exit_pairs_to_immutable_buy_fill_and_builds_post_cost_round_trip(
    tmp_path,
):
    path = tmp_path / "ashare_samples.jsonl"
    _append_execution_prediction(path)
    buy = _filled_execution(
        order_id="BUY-1",
        trade_id="TRADE-BUY-1",
        side="buy",
        quantity=100,
        price=10.0,
    )
    sell = _filled_execution(
        order_id="SELL-1",
        trade_id="TRADE-SELL-1",
        side="sell",
        quantity=100,
        price=11.0,
        fee_cny=4.0,
        slippage_cny=1.0,
        exit_reason="stop_loss",
    )

    buy_report = persist_simulation_outcomes(
        journal_path=path,
        trade_date="20260713",
        records=[buy],
        risk_rejections=[],
    )
    sell_report = persist_simulation_outcomes(
        journal_path=path,
        trade_date="20260714",
        records=[sell],
        risk_rejections=[],
    )
    duplicate = persist_simulation_outcomes(
        journal_path=path,
        trade_date="20260714",
        records=[sell],
        risk_rejections=[],
    )

    events = SampleJournal(path).read_events()
    entry = next(row for row in events if row["record_type"] == "fill")
    exit_event = next(row for row in events if row["record_type"] == "stop")
    completed = next(
        row for row in events if row["record_type"] == "completed_round_trip"
    )
    assert buy_report["exploration_fill_count"] == 1
    assert sell_report["exit_stop_count"] == 1
    assert sell_report["completed_round_trip_count"] == 1
    assert duplicate["idempotent_execution_outcome_count"] == 1
    assert len(events) == 4
    assert exit_event["entry_fill_identity"] == entry["fill_identity"]
    assert completed["entry_fill_identity"] == entry["fill_identity"]
    assert completed["sample_intent"] == "exploration"
    assert completed["primary_style"] == "trend_breakout_strength_continuation"
    assert completed["style_versions"] == {
        "trend_breakout_strength_continuation": "style-v1"
    }
    assert completed["gross_pnl_cny"] == pytest.approx(100.0)
    assert completed["fee_cny"] == pytest.approx(9.0)
    assert completed["slippage_cny"] == pytest.approx(2.0)
    assert completed["net_pnl_cny"] == pytest.approx(89.0)

    performance = SampleJournal(path).build_kpi()["styles"][
        "trend_breakout_strength_continuation"
    ]["performance_by_sample_intent"]["exploration"]
    assert performance["completed_round_trip_count"] == 1
    assert performance["expectancy_cny"] == pytest.approx(89.0)
    assert performance["post_cost_pnl_cny"] == pytest.approx(89.0)


def test_partial_exit_stays_unfinished_until_later_sell_closes_buy_identity(tmp_path):
    path = tmp_path / "ashare_samples.jsonl"
    _append_execution_prediction(path)
    buy = _filled_execution(
        order_id="BUY-200",
        trade_id="TRADE-BUY-200",
        side="buy",
        quantity=200,
        price=10.0,
        fee_cny=8.0,
        slippage_cny=2.0,
    )
    first_sell = _filled_execution(
        order_id="SELL-100-A",
        trade_id="TRADE-SELL-100-A",
        side="sell",
        quantity=100,
        price=10.5,
        fee_cny=4.0,
        slippage_cny=1.0,
    )
    second_sell = _filled_execution(
        order_id="SELL-100-B",
        trade_id="TRADE-SELL-100-B",
        side="sell",
        quantity=100,
        price=11.0,
        fee_cny=4.0,
        slippage_cny=1.0,
    )

    persist_simulation_outcomes(
        journal_path=path,
        trade_date="20260713",
        records=[buy],
        risk_rejections=[],
    )
    partial = persist_simulation_outcomes(
        journal_path=path,
        trade_date="20260714",
        records=[first_sell],
        risk_rejections=[],
    )
    assert partial["unfinished_exit_count"] == 1
    assert partial["completed_round_trip_count"] == 0

    closed = persist_simulation_outcomes(
        journal_path=path,
        trade_date="20260715",
        records=[second_sell],
        risk_rejections=[],
    )
    events = SampleJournal(path).read_events()
    completed = [row for row in events if row["record_type"] == "completed_round_trip"]
    assert closed["completed_round_trip_count"] == 1
    assert len(completed) == 1
    assert [
        identity.rsplit("|", 1)[-1] for identity in completed[0]["exit_fill_identities"]
    ] == [
        "TRADE-SELL-100-A",
        "TRADE-SELL-100-B",
    ]
    assert completed[0]["gross_pnl_cny"] == pytest.approx(150.0)
    assert completed[0]["net_pnl_cny"] == pytest.approx(130.0)


def test_unmatched_sell_is_explicit_chain_rejection_not_fake_round_trip(tmp_path):
    path = tmp_path / "ashare_samples.jsonl"
    _append_execution_prediction(path)
    sell = _filled_execution(
        order_id="SELL-NO-ENTRY",
        trade_id="TRADE-SELL-NO-ENTRY",
        side="sell",
        quantity=100,
        price=11.0,
        account="other_sim",
    )

    report = persist_simulation_outcomes(
        journal_path=path,
        trade_date="20260714",
        records=[sell],
        risk_rejections=[],
    )

    events = SampleJournal(path).read_events()
    assert report["pairing_rejection_count"] == 1
    assert report["completed_round_trip_count"] == 0
    chain = next(row for row in events if row["record_type"] == "chain_validation")
    assert len(events) == 2
    assert chain["pairing_status"] == "rejected"
    assert chain["reason"] == "no_open_buy_fill_for_exact_lineage"
    assert (
        SampleJournal(path).build_kpi()["sample_layer_totals"]["completed_round_trip"]
        == 0
    )


def test_same_style_exploration_and_exploitation_round_trips_remain_separate(
    tmp_path,
):
    path = tmp_path / "ashare_samples.jsonl"
    _append_execution_prediction(path)
    records = [
        _filled_execution(
            order_id="BUY-E",
            trade_id="TRADE-BUY-E",
            side="buy",
            quantity=100,
            price=10.0,
            sample_intent="exploration",
        ),
        _filled_execution(
            order_id="BUY-X",
            trade_id="TRADE-BUY-X",
            side="buy",
            quantity=100,
            price=12.0,
            sample_intent="exploitation",
        ),
    ]
    persist_simulation_outcomes(
        journal_path=path,
        trade_date="20260713",
        records=records,
        risk_rejections=[],
    )
    persist_simulation_outcomes(
        journal_path=path,
        trade_date="20260714",
        records=[
            _filled_execution(
                order_id="SELL-BOTH",
                trade_id="TRADE-SELL-BOTH",
                side="sell",
                quantity=200,
                price=11.0,
                fee_cny=8.0,
                slippage_cny=2.0,
            )
        ],
        risk_rejections=[],
    )

    style = SampleJournal(path).build_kpi()["styles"][
        "trend_breakout_strength_continuation"
    ]
    assert style["completed_round_trip_count"] == 2
    assert style["performance_scope"] == "separated_by_sample_intent"
    assert style["expectancy_cny"] is None
    assert style["performance_by_sample_intent"]["exploration"]["post_cost_pnl_cny"] > 0
    assert (
        style["performance_by_sample_intent"]["exploitation"]["post_cost_pnl_cny"] < 0
    )
