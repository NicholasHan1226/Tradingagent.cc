from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from CNFutures.review import (
    append_review,
    build_observation_samples,
    score_records,
    summarize_forward_outcomes,
    summarize_records,
)
from CNFutures.sim_runner import build_affordability_hold


@pytest.mark.parametrize(
    "counterfactual_marker",
    [
        {"counterfactual_only": True},
        {"execution_class": "counterfactual"},
        {"execution_class": "counterfactual_only"},
    ],
)
def test_counterfactual_fill_never_enters_execution_economics_or_promotion(
    counterfactual_marker: dict[str, object],
    tmp_path: Path,
) -> None:
    record = {
        "style": "trend",
        "session": "day_morning",
        **counterfactual_marker,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "receipt": {
            "status": "filled",
            "fee": 7.0,
            "raw_response": {
                "margin_required": 4_550.0,
                "notional": 35_000.0,
            },
        },
        "performance": {
            "gross_pnl": 100.0,
            "realized_pnl": 93.0,
            "closed_quantity": 1,
        },
        "forward_outcome": {
            "status": "labeled",
            "direction_correct": True,
        },
    }

    summary = summarize_records([record])
    score = score_records([record], min_sample_trades=1)["style_scores"]["trend"]
    labels = summarize_forward_outcomes([record])["styles"]["trend"]
    payload = append_review(
        date="20260712",
        market="cn_futures",
        records=[record],
        errors=[],
        path=tmp_path / "data" / "cn_futures_sim_reviews.jsonl",
    )
    style_output = json.loads(
        Path(payload["style_output_paths"]["style_comparison"]).read_text(
            encoding="utf-8"
        )
    )["style_comparison"][0]

    assert summary["filled_count"] == 0
    assert summary["styles"]["trend"]["filled_count"] == 0
    assert summary["styles"]["trend"]["fee"] == 0.0
    assert score["trade_count"] == 0
    assert score["filled_count"] == 0
    assert score["pnl_sample_count"] == 0
    assert score["completed_round_trip_count"] == 0
    assert score["realized_pnl"] == 0.0
    assert score["status"] == "sample_insufficient"
    assert style_output["performance_eligible"] is False
    assert style_output["filled_count"] == 0
    assert style_output["completed_round_trip_count"] == 0
    assert style_output["realized_pnl"] == 0.0
    assert not Path(payload["style_output_paths"]["style_performance"]).exists()
    assert labels["labeled"] == 1
    assert labels["wins"] == 1


def test_review_uses_each_leg_fee_once_and_reports_net_realized_pnl(
    tmp_path: Path,
) -> None:
    common_raw = {
        "margin_required": 4_550.0,
        "notional": 35_000.0,
        "open_fee": 3.0,
        "estimated_close_fee": 4.0,
        "total_estimated_fee": 7.0,
    }
    records = [
        {
            "style": "trend",
            "symbol": "RB2610.SHF",
            "order": {"intent": "open"},
            "receipt": {
                "status": "filled",
                "fee": 7.0,
                "raw_response": dict(common_raw),
            },
        },
        {
            "style": "trend",
            "symbol": "RB2610.SHF",
            "order": {"intent": "reduce_only"},
            "receipt": {
                "status": "filled",
                "fee": 7.0,
                "raw_response": dict(common_raw),
            },
            # The runner's position-close estimate has deducted only the close
            # leg. Review must also account for the earlier open leg exactly once.
            "performance": {
                "gross_pnl": 100.0,
                "realized_pnl": 96.0,
                "round_trip_fee": 4.0,
                "closed_quantity": 1,
            },
        },
    ]

    summary = summarize_records(records)
    style_score = score_records(records, min_sample_trades=1)["style_scores"]["trend"]
    payload = append_review(
        date="20260712",
        market="cn_futures",
        records=records,
        errors=[],
        path=tmp_path / "data" / "cn_futures_sim_reviews.jsonl",
    )
    style_output = json.loads(
        Path(payload["style_output_paths"]["style_comparison"]).read_text(
            encoding="utf-8"
        )
    )["style_comparison"][0]

    assert summary["filled_count"] == 2
    assert summary["styles"]["trend"]["fee"] == 7.0
    assert style_score["fee"] == 7.0
    assert style_score["gross_realized_pnl"] == 100.0
    assert style_score["realized_pnl"] == 93.0
    assert style_score["completed_round_trip_count"] == 1
    assert style_score["pnl_sample_count"] == 1
    assert style_score["score"] == 193.0
    assert style_output["fee"] == 7.0
    assert style_output["realized_pnl"] == 93.0
    assert style_output["total_pnl"] == 93.0


def test_review_does_not_mislabel_pnl_drawdown_fee_ratio_as_sharpe_or_dsr(
    tmp_path: Path,
) -> None:
    """A trade-level PnL diagnostic is not a same-frequency Sharpe series."""

    review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
    record = {
        "style": "trend",
        "symbol": "RB2610.SHF",
        "order": {"intent": "reduce_only"},
        "receipt": {
            "status": "filled",
            "raw_response": {"estimated_close_fee": 5.0},
        },
        "performance": {
            "gross_pnl": 100.0,
            "realized_pnl": 95.0,
            "closed_quantity": 1,
        },
    }

    payload = append_review(
        date="20260712",
        market="cn_futures",
        records=[record],
        errors=[],
        path=review_path,
    )
    style_row = json.loads(
        Path(payload["style_output_paths"]["style_comparison"]).read_text(
            encoding="utf-8"
        )
    )["style_comparison"][0]
    performance_row = json.loads(
        Path(payload["style_output_paths"]["style_performance"])
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )

    for row in (style_row, performance_row):
        assert row["sharpe"] is None
        assert row["sharpe_status"] == "unavailable_no_same_frequency_net_return_series"
        assert row["dsr_eligible"] is False
        assert row["dsr_status"] == "unavailable_sharpe_missing"
        assert row["promotion_metric_eligible"] is False
        assert row["net_pnl_to_drawdown_plus_fee_ratio"] == pytest.approx(19.0)
        assert row["diagnostic_ratio_only"] is True


def test_review_carries_open_fee_across_append_cycles_for_same_trade_date(
    tmp_path: Path,
) -> None:
    common_raw = {
        "margin_required": 4_550.0,
        "notional": 35_000.0,
        "open_fee": 3.0,
        "estimated_close_fee": 4.0,
        "total_estimated_fee": 7.0,
    }
    review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
    open_record = {
        "style": "trend",
        "symbol": "RB2610.SHF",
        "order": {"intent": "open"},
        "receipt": {
            "status": "filled",
            "fee": 7.0,
            "raw_response": dict(common_raw),
        },
    }
    close_record = {
        "style": "trend",
        "symbol": "RB2610.SHF",
        "order": {"intent": "reduce_only"},
        "receipt": {
            "status": "filled",
            "fee": 7.0,
            "raw_response": dict(common_raw),
        },
        "performance": {
            "gross_pnl": 100.0,
            "realized_pnl": 96.0,
            "round_trip_fee": 4.0,
            "closed_quantity": 1,
        },
    }

    append_review(
        date="20260712",
        market="cn_futures",
        records=[open_record],
        errors=[],
        path=review_path,
    )
    close_payload = append_review(
        date="20260712",
        market="cn_futures",
        records=[close_record],
        errors=[],
        path=review_path,
    )
    persisted_rows = [
        json.loads(line)
        for line in review_path.read_text(encoding="utf-8").splitlines()
    ]
    final_style = json.loads(
        Path(close_payload["style_output_paths"]["style_comparison"]).read_text(
            encoding="utf-8"
        )
    )["style_comparison"][0]
    cumulative = close_payload["score_summary"]["style_scores"]["trend"]
    current_run = close_payload["run_score_summary"]["style_scores"]["trend"]

    assert len(persisted_rows) == 2
    assert current_run["fee"] == 4.0
    assert current_run["realized_pnl"] == 96.0
    assert cumulative["fee"] == 7.0
    assert cumulative["gross_realized_pnl"] == 100.0
    assert cumulative["realized_pnl"] == 93.0
    assert cumulative["completed_round_trip_count"] == 1
    assert final_style["fee"] == 7.0
    assert final_style["realized_pnl"] == 93.0
    assert final_style["total_pnl"] == 93.0


def test_review_preserves_cumulative_drawdown_state_across_append_cycles(
    tmp_path: Path,
) -> None:
    review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"

    def closed_trade(gross_pnl: float) -> dict[str, object]:
        return {
            "style": "trend",
            "symbol": "RB2610.SHF",
            "order": {"intent": "reduce_only"},
            "receipt": {
                "status": "filled",
                "raw_response": {"estimated_close_fee": 0.0},
            },
            "performance": {
                "gross_pnl": gross_pnl,
                "realized_pnl": gross_pnl,
                "closed_quantity": 1,
            },
        }

    first = append_review(
        date="20260712",
        market="cn_futures",
        records=[closed_trade(100.0)],
        errors=[],
        path=review_path,
    )["score_summary"]["style_scores"]["trend"]
    second = append_review(
        date="20260712",
        market="cn_futures",
        records=[closed_trade(-50.0)],
        errors=[],
        path=review_path,
    )["score_summary"]["style_scores"]["trend"]
    third = append_review(
        date="20260712",
        market="cn_futures",
        records=[closed_trade(-25.0)],
        errors=[],
        path=review_path,
    )["score_summary"]["style_scores"]["trend"]

    assert first["ending_equity"] == 100.0
    assert first["high_water_equity"] == 100.0
    assert first["current_drawdown"] == 0.0
    assert first["max_drawdown"] == 0.0

    assert second["realized_pnl"] == 50.0
    assert second["ending_equity"] == 50.0
    assert second["high_water_equity"] == 100.0
    assert second["current_drawdown"] == 50.0
    assert second["max_drawdown"] == 50.0

    assert third["realized_pnl"] == 25.0
    assert third["ending_equity"] == 25.0
    assert third["high_water_equity"] == 100.0
    assert third["current_drawdown"] == 75.0
    assert third["max_drawdown"] == 75.0


def test_single_losing_review_starts_equity_curve_at_zero() -> None:
    record = {
        "style": "trend",
        "symbol": "RB2610.SHF",
        "order": {"intent": "reduce_only"},
        "receipt": {
            "status": "filled",
            "raw_response": {"estimated_close_fee": 0.0},
        },
        "performance": {
            "gross_pnl": -50.0,
            "realized_pnl": -50.0,
            "closed_quantity": 1,
        },
    }

    style_score = score_records([record], min_sample_trades=1)["style_scores"]["trend"]

    assert style_score["ending_equity"] == -50.0
    assert style_score["high_water_equity"] == 0.0
    assert style_score["current_drawdown"] == 50.0
    assert style_score["max_drawdown"] == 50.0


def test_real_affordability_hold_is_counterfactual_and_fails_closed_without_prediction() -> (
    None
):
    hold = build_affordability_hold(
        symbol="RB2610.SHF",
        style_name="trend",
        size_decision={
            "quantity": 0,
            "eligible": False,
            "reason": "account_state_unavailable",
            "counterfactual_only": True,
            "counterfactual_eligible": True,
        },
        cadence="5min",
        bar_time="2026-07-12 09:35:00",
        session="day",
    )

    observation = build_observation_samples(
        [hold], date="20260712", market="cn_futures"
    )[0]

    assert observation["execution_class"] == "counterfactual_only"
    assert observation["sample_intent"] == "counterfactual"
    assert observation["prediction"] == {}
    assert observation["direction"] == "unknown"
    assert observation["prediction_evidence_status"] == "incomplete"
    assert observation["label_eligible"] is False
    assert observation["label_status"] == "prediction_evidence_incomplete"
    assert observation["forward_outcome"]["status"] == "prediction_evidence_incomplete"


def test_hold_only_styles_persist_complete_observations_without_fake_trades(
    tmp_path: Path,
) -> None:
    holds = [
        {
            "style": "trend",
            "style_version": "trend.v2",
            "symbol": f"RB26{index:02d}.SHF",
            "stage": "risk",
            "reason": "account_state_unavailable",
            "side": "buy",
            "cadence": "5min",
            "bar_time": f"2026-07-12 09:{index:02d}:00",
            "session": "day",
            "sample_intent": "counterfactual",
            "execution_class": "counterfactual_only",
            "counterfactual_only": True,
            "label_eligible": True,
            "prediction": {"direction": "long", "probability": 0.61},
            "size_decision": {"quantity": 0, "reason": "account_state_unavailable"},
            "forward_outcome": {"status": "pending_future_bars"},
        }
        for index in range(15)
    ]
    holds.append(
        {
            "style": "defensive",
            "style_version": "defensive.v1",
            "symbol": "IF2609.CFFEX",
            "stage": "signal",
            "reason": "defensive_abstain",
            "action": "hold",
            "cadence": "5min",
            "bar_time": "2026-07-12 10:00:00",
            "session": "day",
            "sample_intent": "observe",
            "execution_class": "observation_only",
            "counterfactual_only": False,
            "label_eligible": True,
            "prediction": {"direction": "flat", "probability": 0.72},
            "forward_outcome": {"status": "pending_future_bars"},
        }
    )
    review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"

    payload = append_review(
        date="20260712",
        market="cn_futures",
        records=[],
        errors=[],
        holds=holds,
        path=review_path,
    )
    persisted = json.loads(review_path.read_text(encoding="utf-8").splitlines()[-1])
    style_output = json.loads(
        Path(payload["style_output_paths"]["style_comparison"]).read_text(
            encoding="utf-8"
        )
    )
    by_style = {row["style_name"]: row for row in style_output["style_comparison"]}

    assert payload["record_count"] == 0
    assert payload["filled_count"] == 0
    assert payload["observation_sample_count"] == 16
    assert len(payload["observation_samples"]) == 16
    assert len(persisted["observation_samples"]) == 16
    assert (
        len({row["observation_id"] for row in persisted["observation_samples"]}) == 16
    )
    last_trend = persisted["observation_samples"][14]
    assert last_trend["style_version"] == "trend.v2"
    assert last_trend["direction"] == "buy"
    assert last_trend["prediction"]["probability"] == pytest.approx(0.61)
    assert last_trend["reason"] == "account_state_unavailable"
    assert last_trend["execution_class"] == "counterfactual_only"
    assert last_trend["label_status"] == "pending"
    assert last_trend["real_trading_enabled"] is False
    assert payload["hold_reason_summary"]["by_stage"]["risk"] == 15
    assert payload["forward_label_summary"]["styles"]["trend"]["pending"] == 15
    assert payload["forward_label_summary"]["styles"]["defensive"]["pending"] == 1
    assert style_output["styles_loaded"] == 2
    assert set(by_style) == {"defensive", "trend"}
    assert by_style["trend"]["filled_count"] == 0
    assert by_style["trend"]["trades"] == 0
    assert by_style["trend"]["hold_count"] == 15
    assert by_style["trend"]["risk_rejection_count"] == 15
    assert by_style["trend"]["observation_count"] == 15
    assert by_style["trend"]["performance_eligible"] is False
    assert by_style["trend"]["status"] == "observe"
    assert by_style["trend"]["total_pnl"] == 0.0


# ---------------------------------------------------------------------------
# Per-session decision row contract
# ---------------------------------------------------------------------------


class TestPerSessionDecisionRows:
    """append_review MUST write per-session rows consumable by session acceptance."""

    @staticmethod
    def _fill_record(
        session: str,
        symbol: str = "IF2608.CFX",
        *,
        execution_eligible: bool = True,
        counterfactual_only: bool = False,
    ) -> dict[str, object]:
        source_sha = "a" * 64
        evidence: dict[str, object] = {
            "schema_version": "cn_futures.execution_evidence.v1",
            "capital_authority_id": "cn-futures-capital-v1",
            "authority_generation": 1,
            "execution_lineage_id": "test-lineage-20260712-0001",
            "order_id": f"SIM-{session}",
            "symbol": symbol,
            "side": "buy",
            "execution_fill_id": f"CNF-FILL-{session}",
            "filled_quantity": 1,
            "fill_price": 3500.0,
            "requested_price": 3499.3,
            "fee_cash_cny": 7.0,
            "slippage_bps": 2.0,
            "slippage_cny": 7.0,
            "fill_evidence_type": "bar_volume_participation",
            "evidence_timestamp": "2026-07-12T09:35:00+08:00",
            "margin_required_cny": 4550.0,
            "contract_multiplier": 10.0,
            "contract_spec_version": "cn-futures-contract-spec.v1",
            "contract_spec_sha256": "b" * 64,
            "receipt_sha256": "c" * 64,
            "local_state_sha256": "d" * 64,
            "capital_commit_action": "fill_commit",
            "capital_commit_action_id": f"MCAP-ACTION-{session}",
            "capital_commit_reference_id": f"MCAPFILL:1:lineage:{session}:fill",
            "capital_commit_status": "committed",
            "capital_commit_event_id": f"MCAP-EVENT-{session}",
            "capital_commit_event_checksum": "e" * 64,
            "source_snapshot_sha256": source_sha,
            "real_trading_enabled": False,
        }
        evidence["execution_evidence_sha256"] = hashlib.sha256(
            json.dumps(
                evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        record: dict[str, object] = {
            "style": "trend",
            "session": session,
            "symbol": symbol,
            "bar_time": "2026-07-12T09:35:00+08:00",
            "entry_price": 3500.0,
            "direction": "buy",
            "point_in_time_as_of": "2026-07-12T09:35:00+08:00",
            "source_event_time": "2026-07-12T09:35:00+08:00",
            "source_snapshot_id": "CNF-SNAP-" + source_sha[:16],
            "source_snapshot_sha256": source_sha,
            "authority": "market_capital_ledger",
            "lineage_status": "complete",
            "capital_authority_id": "cn-futures-capital-v1",
            "authority_generation": 1,
            "execution_lineage_id": "test-lineage-20260712-0001",
            "order": {"intent": "open"},
            "receipt": {
                "status": "filled",
                "filled_qty": 1,
                "execution_eligible": execution_eligible,
                "execution_class": (
                    "execution_eligible"
                    if execution_eligible
                    else "counterfactual_only"
                ),
                "counterfactual_only": counterfactual_only,
                "capital_layer": "simulated",
                "account_type": "simulated",
            },
        }
        if execution_eligible:
            record["execution_evidence"] = evidence
        return record

    @staticmethod
    def _hold_record(
        session: str,
        *,
        reason: str = "margin_cap",
        stage: str = "risk",
        counterfactual_only: bool = True,
    ) -> dict[str, object]:
        return {
            "style": "trend",
            "session": session,
            "symbol": "IH2608.CFX",
            "stage": stage,
            "reason": reason,
            "execution_eligible": False,
            "counterfactual_only": counterfactual_only,
            "execution_class": "counterfactual_only",
        }

    def test_per_session_rows_appear_in_jsonl(
        self,
        tmp_path: Path,
    ) -> None:
        """Each valid session produces a standalone decision row in session_decisions."""
        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
        records = [
            self._fill_record("day_morning"),
            self._fill_record("day_afternoon"),
        ]
        holds = [self._hold_record("night", reason="night_not_allowed")]

        payload = append_review(
            date="20260712",
            market="cn_futures",
            records=records,
            errors=[],
            holds=holds,
            path=review_path,
        )
        session_rows = payload.get("session_decisions", [])
        sessions_found = {r["session"] for r in session_rows}
        assert sessions_found >= {"day_morning", "day_afternoon", "night"}
        # Summary line still present as the only line
        lines = review_path.read_text("utf-8").splitlines()
        summary = json.loads(lines[-1])
        assert summary.get("score_contract_version") is not None

    def test_per_session_row_has_required_fields(
        self,
        tmp_path: Path,
    ) -> None:
        """Every session decision row carries trade_date, session, record_type,
        execution_eligible, counterfactual_only, real_trading_enabled, _checksum."""
        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
        payload = append_review(
            date="20260712",
            market="cn_futures",
            records=[self._fill_record("day_morning")],
            errors=[],
            holds=[self._hold_record("night", reason="night_not_allowed")],
            path=review_path,
        )
        session_rows = payload.get("session_decisions", [])
        assert len(session_rows) >= 2

        for row in session_rows:
            assert row["_row_type"] == "cn_futures_session_decision"
            assert row["trade_date"] == "20260712"
            assert row["session"] in {"day_morning", "night"}
            assert row["record_type"] in {
                "prediction",
                "candidate",
                "hold",
                "risk_reject",
                "simulated_fill",
            }
            assert "execution_eligible" in row
            assert "counterfactual_only" in row
            assert row["real_trading_enabled"] is False
            assert "_checksum" in row
            # Verify checksum matches content
            content = {k: v for k, v in row.items() if k != "_checksum"}
            expected = hashlib.sha256(
                json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            assert row["_checksum"] == expected

    def test_missing_session_in_record_skips_row_fail_closed(
        self,
        tmp_path: Path,
    ) -> None:
        """A record without a session field skips per-session row — no fabrication."""
        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
        payload = append_review(
            date="20260712",
            market="cn_futures",
            records=[{"style": "trend", "receipt": {"status": "filled"}}],
            errors=[],
            path=review_path,
        )
        # No session rows created
        session_rows = [
            json.loads(line)
            for line in review_path.read_text("utf-8").splitlines()
            if json.loads(line).get("_row_type") == "cn_futures_session_decision"
        ]
        assert len(session_rows) == 0
        # Summary still works
        assert payload["record_count"] == 1

    def test_unknown_session_value_fails_closed(
        self,
        tmp_path: Path,
    ) -> None:
        """A completely unknown session string must raise ValueError."""
        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
        with pytest.raises(ValueError, match="[Ss]ession"):
            append_review(
                date="20260712",
                market="cn_futures",
                records=[self._fill_record("bogus_session_xyz")],
                errors=[],
                path=review_path,
            )

    def test_empty_session_string_skips_row(
        self,
        tmp_path: Path,
    ) -> None:
        """Empty session string skips per-session row — no fabrication."""
        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
        payload = append_review(
            date="20260712",
            market="cn_futures",
            records=[self._fill_record("")],
            errors=[],
            path=review_path,
        )
        # No session rows for empty session
        session_rows = [
            json.loads(line)
            for line in review_path.read_text("utf-8").splitlines()
            if json.loads(line).get("_row_type") == "cn_futures_session_decision"
        ]
        assert len(session_rows) == 0
        # Summary still present
        assert payload["record_count"] == 1

    def test_legacy_day_session_mapped_via_bar_time_morning(
        self,
        tmp_path: Path,
    ) -> None:
        """Legacy 'day' session with morning bar_time maps to day_morning."""
        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
        record = {
            "style": "trend",
            "session": "day",
            "bar_time": "2026-07-12 09:35:00",
            "symbol": "IF2608.CFX",
            "order": {"intent": "open"},
            "receipt": {
                "status": "filled",
                "filled_qty": 1,
                "execution_eligible": True,
                "execution_class": "execution_eligible",
                "counterfactual_only": False,
                "capital_layer": "simulated",
                "account_type": "simulated",
            },
        }
        payload = append_review(
            date="20260712",
            market="cn_futures",
            records=[record],
            errors=[],
            path=review_path,
        )
        session_rows = payload.get("session_decisions", [])
        assert len(session_rows) >= 1
        assert session_rows[0]["session"] == "day_morning"

    def test_legacy_day_session_mapped_via_bar_time_afternoon(
        self,
        tmp_path: Path,
    ) -> None:
        """Legacy 'day' session with afternoon bar_time maps to day_afternoon."""
        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
        record = {
            "style": "trend",
            "session": "day",
            "bar_time": "2026-07-12 13:45:00",
            "symbol": "IF2608.CFX",
            "order": {"intent": "open"},
            "receipt": {
                "status": "filled",
                "filled_qty": 1,
                "execution_eligible": True,
                "execution_class": "execution_eligible",
                "counterfactual_only": False,
                "capital_layer": "simulated",
                "account_type": "simulated",
            },
        }
        payload = append_review(
            date="20260712",
            market="cn_futures",
            records=[record],
            errors=[],
            path=review_path,
        )
        session_rows = payload.get("session_decisions", [])
        assert len(session_rows) >= 1
        assert session_rows[0]["session"] == "day_afternoon"

    def test_legacy_day_session_fails_closed_without_bar_time(
        self,
        tmp_path: Path,
    ) -> None:
        """Legacy 'day' session without bar_time must raise ValueError."""
        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
        record = {
            "style": "trend",
            "session": "day",
            "symbol": "IF2608.CFX",
            "receipt": {"status": "filled", "filled_qty": 1},
        }
        with pytest.raises(ValueError, match="[Bb]ar_time"):
            append_review(
                date="20260712",
                market="cn_futures",
                records=[record],
                errors=[],
                path=review_path,
            )

    def test_idempotent_append_no_duplicate_rows(
        self,
        tmp_path: Path,
    ) -> None:
        """Same identity rows do not produce duplicates on re-append."""
        from CNFutures.review import load_review_rows

        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
        record = self._fill_record("day_morning", symbol="IF2608.CFX")

        payload1 = append_review(
            date="20260712",
            market="cn_futures",
            records=[record],
            errors=[],
            path=review_path,
        )
        # First call creates 1 session row
        assert len(payload1.get("session_decisions", [])) == 1

        # Second append with identical content – session row filtered by lock
        payload2 = append_review(
            date="20260712",
            market="cn_futures",
            records=[record],
            errors=[],
            path=review_path,
        )
        # Second payload has 0 session rows (cross-append dedup)
        assert len(payload2.get("session_decisions", [])) == 0

        # Each call writes exactly one summary line
        count_after = len(review_path.read_text("utf-8").splitlines())
        assert count_after == 2

        # load_review_rows must return the identity exactly once
        rows = load_review_rows(review_path)
        session_ids = [
            r["_identity"]
            for r in rows
            if r.get("_row_type") == "cn_futures_session_decision"
        ]
        assert len(set(session_ids)) == 1

    def test_corrupt_checksum_fails_reading(
        self,
        tmp_path: Path,
    ) -> None:
        """A row with invalid checksum must raise on read."""
        from CNFutures.review import load_review_rows

        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
        append_review(
            date="20260712",
            market="cn_futures",
            records=[self._fill_record("day_morning")],
            errors=[],
            path=review_path,
        )
        # Tamper with a session row inside the summary payload
        lines = review_path.read_text("utf-8").splitlines()
        for i, line in enumerate(lines):
            row = json.loads(line)
            session_rows = row.get("session_decisions", [])
            if session_rows:
                tampered = dict(session_rows[0])
                tampered["execution_eligible"] = not tampered.get("execution_eligible")
                row["session_decisions"] = [tampered] + session_rows[1:]
                lines[i] = json.dumps(row, ensure_ascii=False)
                break
        review_path.write_text("\n".join(lines) + "\n", "utf-8")

        with pytest.raises(ValueError, match="[Cc]hecksum"):
            load_review_rows(review_path)

    def test_real_trading_enabled_always_false_in_session_rows(
        self,
        tmp_path: Path,
    ) -> None:
        """Every session row must have real_trading_enabled=False regardless of input."""
        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
        payload = append_review(
            date="20260712",
            market="cn_futures",
            records=[
                self._fill_record("day_morning"),
            ],
            errors=[],
            holds=[
                self._hold_record("night", reason="night_not_allowed"),
            ],
            path=review_path,
        )
        session_rows = payload.get("session_decisions", [])
        for row in session_rows:
            assert row["real_trading_enabled"] is False, (
                f"Session row {row.get('session')} has real_trading_enabled={row.get('real_trading_enabled')}"
            )

    def test_session_rows_consumable_by_acceptance_evaluator(
        self,
        tmp_path: Path,
    ) -> None:
        """The review JSONL can be fed directly to session acceptance."""
        from shared.runtime_test.cn_futures_session_acceptance import (
            evaluate_session_acceptance,
            load_runtime_records,
        )

        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
        append_review(
            date="20260712",
            market="cn_futures",
            records=[
                self._fill_record("day_morning"),
                self._fill_record("day_afternoon"),
            ],
            errors=[],
            holds=[
                self._hold_record("night", reason="night_not_allowed"),
            ],
            path=review_path,
        )
        records = load_runtime_records(review_path, verify_checksums=True)
        report = evaluate_session_acceptance(
            records,
            trade_date="20260712",
            valid_sessions=["day_morning", "day_afternoon", "night"],
            real_trading_enabled=False,
        )
        assert report["status"] == "pass"
        assert report["ready"] is True
        assert report["summary"]["sessions_accepted"] == 3
        assert report["summary"]["execution_eligible_simulated_fill_count"] == 2

    def test_backward_compatible_summary_line_structure(
        self,
        tmp_path: Path,
    ) -> None:
        """Existing tests that depend on summary payload fields must still pass."""
        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
        record = {
            "style": "trend",
            "session": "day_morning",
            "symbol": "RB2610.SHF",
            "order": {"intent": "open"},
            "receipt": {
                "status": "filled",
                "fee": 7.0,
                "execution_eligible": True,
                "execution_class": "execution_eligible",
                "counterfactual_only": False,
                "capital_layer": "simulated",
                "account_type": "simulated",
                "raw_response": {
                    "margin_required": 4_550.0,
                    "notional": 35_000.0,
                    "open_fee": 3.0,
                },
            },
        }
        payload = append_review(
            date="20260712",
            market="cn_futures",
            records=[record],
            errors=[],
            path=review_path,
        )
        # Summary fields still present
        assert payload["date"] == "20260712"
        assert payload["market"] == "cn_futures"
        assert payload["record_count"] == 1
        assert payload["filled_count"] == 1
        assert payload["real_trading_enabled"] is False
        assert payload["score_contract_version"] is not None
        assert "score_summary" in payload
        assert "run_score_summary" in payload
        # The summary line is the last line
        last_line = json.loads(review_path.read_text("utf-8").splitlines()[-1])
        assert last_line.get("score_contract_version") is not None

    def test_session_row_includes_decision_evidence(
        self,
        tmp_path: Path,
    ) -> None:
        """Session rows carry decision snapshot and evidence fields."""
        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
        payload = append_review(
            date="20260712",
            market="cn_futures",
            records=[self._fill_record("day_morning")],
            errors=[],
            holds=[],
            path=review_path,
        )
        session_rows = payload.get("session_decisions", [])
        fill_row = [r for r in session_rows if r["record_type"] == "simulated_fill"]
        assert len(fill_row) == 1
        assert "decision" in fill_row[0]
        assert "style" in fill_row[0]
        assert "symbol" in fill_row[0]

    # ------------------------------------------------------------------
    # Item 1 – Cross‑append idempotency under lock
    # ------------------------------------------------------------------

    def test_cross_append_idempotency_flatten_unique_identities(
        self,
        tmp_path: Path,
    ) -> None:
        """Repeated append_review with identical records MUST NOT grow the set
        of unique _identity values visible through load_review_rows or
        load_runtime_records."""
        from CNFutures.review import load_review_rows
        from shared.runtime_test.cn_futures_session_acceptance import (
            load_runtime_records,
        )

        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
        record = self._fill_record("day_morning", symbol="IF2608.CFX")

        for _ in range(3):
            append_review(
                date="20260712",
                market="cn_futures",
                records=[record],
                errors=[],
                path=review_path,
            )

        # load_review_rows must return each session identity exactly once
        review_rows = load_review_rows(review_path)
        session_ids = [
            r["_identity"]
            for r in review_rows
            if r.get("_row_type") == "cn_futures_session_decision"
        ]
        assert len(session_ids) == 1, (
            f"Expected 1 unique session identity, got {len(session_ids)}: {session_ids}"
        )

        # load_runtime_records (acceptance path) must also be unique
        rt_records = load_runtime_records(review_path)
        rt_session_ids = [
            r.get("_identity")
            for r in rt_records
            if r.get("_row_type") == "cn_futures_session_decision"
        ]
        unique_rt = set(rt_session_ids)
        assert len(unique_rt) == 1, (
            f"load_runtime_records returned {len(rt_session_ids)} session rows, "
            f"unique={len(unique_rt)}"
        )

        # Sample counts must not grow beyond first write
        first_line = json.loads(review_path.read_text("utf-8").splitlines()[0])
        assert len(first_line.get("session_decisions", [])) == 1

    def test_cross_append_different_records_still_unique(
        self,
        tmp_path: Path,
    ) -> None:
        """Different records across appends must produce distinct identities."""
        from CNFutures.review import load_review_rows

        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"

        r1 = self._fill_record("day_morning", symbol="IF2608.CFX")
        r2 = self._fill_record("day_afternoon", symbol="IH2609.CFX")

        append_review(
            date="20260712",
            market="cn_futures",
            records=[r1],
            errors=[],
            path=review_path,
        )
        append_review(
            date="20260712",
            market="cn_futures",
            records=[r2],
            errors=[],
            path=review_path,
        )

        rows = load_review_rows(review_path)
        session_ids = [
            r["_identity"]
            for r in rows
            if r.get("_row_type") == "cn_futures_session_decision"
        ]
        assert len(set(session_ids)) == 2

    # ------------------------------------------------------------------
    # Item 2 – Missing session → summary rejection (not silent)
    # ------------------------------------------------------------------

    def test_missing_session_adds_contract_rejection_to_summary(
        self,
        tmp_path: Path,
    ) -> None:
        """Records/holds without session MUST appear in
        session_contract_rejections, not be silently dropped."""
        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
        payload = append_review(
            date="20260712",
            market="cn_futures",
            records=[
                {
                    "style": "trend",
                    "symbol": "RB2610.SHF",
                    "receipt": {"status": "filled", "filled_qty": 1},
                }
            ],
            errors=[],
            holds=[
                {"style": "defensive", "reason": "low_confidence", "stage": "signal"}
            ],
            path=review_path,
        )
        assert payload.get("session_contract_rejection_count", 0) >= 1
        rejections = payload.get("session_contract_rejections", [])
        assert isinstance(rejections, list)
        assert len(rejections) >= 1
        for rej in rejections:
            assert "reason" in rej
            assert rej["reason"] == "missing_session"
            assert "source_type" in rej  # "record" or "hold"
            assert "style" in rej or "symbol" in rej

    def test_session_contract_rejection_reported_in_acceptance(
        self,
        tmp_path: Path,
    ) -> None:
        """Acceptance module must surface session contract violations."""
        from shared.runtime_test.cn_futures_session_acceptance import (
            evaluate_session_acceptance,
            load_runtime_records,
        )

        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
        append_review(
            date="20260712",
            market="cn_futures",
            records=[self._fill_record("day_morning")],
            errors=[],
            holds=[{"style": "trend", "reason": "low_confidence", "stage": "signal"}],
            path=review_path,
        )
        records = load_runtime_records(review_path, verify_checksums=True)
        report = evaluate_session_acceptance(
            records,
            trade_date="20260712",
            valid_sessions=["day_morning"],
            real_trading_enabled=False,
        )
        # Must include contract violation info
        assert report["summary"].get("session_contract_violation_count", 0) >= 1

    # ------------------------------------------------------------------
    # Item 3 – Holds use nested session/bar_time extraction
    # ------------------------------------------------------------------

    def test_hold_session_extracted_from_nested_size_decision(
        self,
        tmp_path: Path,
    ) -> None:
        """A hold's session inside size_decision must be found."""
        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
        hold = {
            "style": "trend",
            "symbol": "IH2608.CFX",
            "stage": "risk",
            "reason": "margin_cap",
            "size_decision": {
                "session": "night",
                "bar_time": "2026-07-12 21:05:00",
            },
            "counterfactual_only": True,
        }
        payload = append_review(
            date="20260712",
            market="cn_futures",
            records=[],
            errors=[],
            holds=[hold],
            path=review_path,
        )
        session_rows = payload.get("session_decisions", [])
        assert len(session_rows) >= 1
        assert session_rows[0]["session"] == "night"

    def test_hold_bar_time_extracted_from_nested_receipt(
        self,
        tmp_path: Path,
    ) -> None:
        """A hold's bar_time inside receipt must map legacy 'day' correctly."""
        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
        hold = {
            "style": "trend",
            "session": "day",
            "symbol": "IH2608.CFX",
            "stage": "signal",
            "reason": "direction_score_below_threshold",
            "receipt": {"bar_time": "2026-07-12 14:30:00"},
        }
        payload = append_review(
            date="20260712",
            market="cn_futures",
            records=[],
            errors=[],
            holds=[hold],
            path=review_path,
        )
        session_rows = payload.get("session_decisions", [])
        assert len(session_rows) >= 1
        assert session_rows[0]["session"] == "day_afternoon"

    # ------------------------------------------------------------------
    # Item 4 – Checksum verify no pop mutation; context‑merge safety
    # ------------------------------------------------------------------

    def test_load_review_rows_does_not_mutate_original_session_dict(
        self,
        tmp_path: Path,
    ) -> None:
        """load_review_rows must not pop _checksum from the original dict."""
        from CNFutures.review import load_review_rows

        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
        append_review(
            date="20260712",
            market="cn_futures",
            records=[self._fill_record("day_morning")],
            errors=[],
            path=review_path,
        )

        # Read original bytes
        original_text = review_path.read_text("utf-8")
        original_line = json.loads(original_text.splitlines()[-1])
        original_srows = original_line.get("session_decisions", [])
        assert original_srows[0].get("_checksum") is not None

        # load_review_rows should not mutate the file
        _rows = load_review_rows(review_path)

        # Re-read – checksums must still be present
        after_text = review_path.read_text("utf-8")
        after_line = json.loads(after_text.splitlines()[-1])
        after_srows = after_line.get("session_decisions", [])
        assert after_srows[0].get("_checksum") is not None
        assert after_srows[0]["_checksum"] == original_srows[0]["_checksum"]

    def test_context_merge_does_not_alter_self_contained_row_checksum(
        self,
        tmp_path: Path,
    ) -> None:
        """When _flatten_payload extracts a self-contained session_decisions
        row, the checksum must still verify because no extra envelope fields
        are injected."""
        from shared.runtime_test.cn_futures_session_acceptance import (
            load_runtime_records,
        )

        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
        append_review(
            date="20260712",
            market="cn_futures",
            records=[self._fill_record("day_morning")],
            errors=[],
            path=review_path,
        )

        # Should NOT raise checksum error
        records = load_runtime_records(review_path, verify_checksums=True)
        session_rows = [
            r for r in records if r.get("_row_type") == "cn_futures_session_decision"
        ]
        assert len(session_rows) == 1
        # Verify the row content matches checksum
        row = session_rows[0]
        checksum = row.get("_checksum")
        assert checksum is not None
        content = {k: v for k, v in row.items() if k != "_checksum"}
        expected = hashlib.sha256(
            json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        assert checksum == expected

    # ------------------------------------------------------------------
    # Item 5 – observation/observation_only + nested counterfactual
    # ------------------------------------------------------------------

    def test_observation_only_execution_class_is_recognized(
        self,
        tmp_path: Path,
    ) -> None:
        """execution_class='observation_only' must classify as hold, not fill."""
        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
        hold = {
            "style": "trend",
            "session": "day_morning",
            "symbol": "IF2608.CFX",
            "stage": "signal",
            "reason": "observation_only",
            "execution_class": "observation_only",
            "counterfactual_only": False,
        }
        payload = append_review(
            date="20260712",
            market="cn_futures",
            records=[],
            errors=[],
            holds=[hold],
            path=review_path,
        )
        session_rows = payload.get("session_decisions", [])
        assert len(session_rows) >= 1
        obs_row = session_rows[0]
        assert obs_row["record_type"] == "hold"
        assert obs_row.get("counterfactual_only") is False
        assert obs_row.get("execution_class") == "observation_only"

    def test_nested_order_size_decision_counterfactual_marker(
        self,
        tmp_path: Path,
    ) -> None:
        """counterfactual_only inside order.size_decision must be detected
        and reflected in the session row."""
        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
        record = {
            "style": "trend",
            "session": "day_morning",
            "symbol": "IF2608.CFX",
            "order": {
                "intent": "open",
                "size_decision": {
                    "counterfactual_only": True,
                    "execution_eligible": False,
                    "execution_class": "counterfactual_only",
                },
            },
            "receipt": {
                "status": "filled",
                "filled_qty": 1,
                "capital_layer": "simulated",
                "account_type": "simulated",
            },
        }
        payload = append_review(
            date="20260712",
            market="cn_futures",
            records=[record],
            errors=[],
            path=review_path,
        )
        session_rows = payload.get("session_decisions", [])
        assert len(session_rows) >= 1
        row = session_rows[0]
        assert row["counterfactual_only"] is True
        assert row["execution_eligible"] is False
        assert row.get("execution_class") == "counterfactual_only"

    # ------------------------------------------------------------------
    # Item 8 – Every valid session produces at least one judgment row
    # ------------------------------------------------------------------

    def test_every_called_session_produces_at_least_one_judgment(
        self,
        tmp_path: Path,
    ) -> None:
        """For every session among records+holds, at least one session row
        of type candidate/hold/risk_reject/simulated_fill must exist."""
        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
        payload = append_review(
            date="20260712",
            market="cn_futures",
            records=[
                self._fill_record("day_morning"),
                self._fill_record("day_afternoon"),
            ],
            errors=[],
            holds=[
                self._hold_record("night", reason="night_not_allowed"),
                self._hold_record(
                    "day_morning", reason="volume_filter", counterfactual_only=False
                ),
            ],
            path=review_path,
        )
        session_rows = payload.get("session_decisions", [])
        sessions_found = {r["session"] for r in session_rows}
        assert sessions_found >= {"day_morning", "day_afternoon", "night"}

        record_types_by_session: dict[str, set[str]] = {}
        for r in session_rows:
            record_types_by_session.setdefault(r["session"], set()).add(
                r["record_type"]
            )

        for session_name in sessions_found:
            types = record_types_by_session[session_name]
            assert types & {"candidate", "hold", "risk_reject", "simulated_fill"}, (
                f"Session {session_name} has no actionable judgment type in {types}"
            )
        # sim-only enforcement
        for r in session_rows:
            assert r["real_trading_enabled"] is False


# ---------------------------------------------------------------------------
# P0: Point-in-time lineage / 5-minute cluster propagation – RED tests
# ---------------------------------------------------------------------------


class TestPITLineageAndClusterPropagation:
    """PIT lineage and cluster_id must flow through observation samples
    and session decision rows."""

    @staticmethod
    def _hold_with_pit(
        session: str = "day_morning",
        *,
        pit_as_of: str = "2026-07-12 09:35:00",
        authority: str = "master_capital_ledger",
        cluster_id: str = "",
    ) -> dict[str, object]:
        hold: dict[str, object] = {
            "style": "trend",
            "style_version": "trend-v9",
            "session": session,
            "symbol": "IF2608.CFX",
            "stage": "risk",
            "reason": "margin_cap",
            "side": "buy",
            "cadence": "5min",
            "bar_time": pit_as_of,
            "sample_intent": "counterfactual",
            "execution_class": "counterfactual_only",
            "counterfactual_only": True,
            "label_eligible": True,
            "prediction": {"direction": "long", "probability": 0.61},
            "size_decision": {"quantity": 0, "reason": "margin_cap"},
            "forward_outcome": {"status": "pending_future_bars"},
            "point_in_time_as_of": pit_as_of,
            "source_event_time": pit_as_of,
            "source_snapshot_id": "SNAP-test-001",
            "source_snapshot_sha256": "abc123",
            "authority": authority,
            "lineage_status": "complete" if authority else "incomplete",
        }
        if cluster_id:
            hold["cluster_id"] = cluster_id
            hold["cluster_role"] = "origin"
            hold["occurrence_index"] = 0
        return hold

    def test_observation_sample_propagates_pit_lineage(
        self,
        tmp_path: Path,
    ) -> None:
        """PIT lineage fields must appear in observation samples."""
        hold = self._hold_with_pit()
        observations = build_observation_samples(
            [hold],
            date="20260712",
            market="cn_futures",
        )
        assert len(observations) == 1
        obs = observations[0]
        assert obs["point_in_time_as_of"] == "2026-07-12 09:35:00"
        assert obs["source_event_time"] == "2026-07-12 09:35:00"
        assert obs["source_snapshot_id"] == "SNAP-test-001"
        assert obs["source_snapshot_sha256"] == "abc123"
        assert obs["authority"] == "master_capital_ledger"
        assert obs["lineage_status"] == "complete"

    def test_observation_lineage_incomplete_blocks_execution_eligible(
        self,
        tmp_path: Path,
    ) -> None:
        """When lineage_status is 'incomplete', execution_eligible must
        remain False."""
        hold = self._hold_with_pit(authority="")
        observations = build_observation_samples(
            [hold],
            date="20260712",
            market="cn_futures",
        )
        assert len(observations) == 1
        obs = observations[0]
        assert obs["lineage_status"] == "incomplete"
        assert obs["execution_eligible"] is False

    def test_observation_propagates_cluster_id(
        self,
        tmp_path: Path,
    ) -> None:
        """Cluster fields must appear in observation samples."""
        hold = self._hold_with_pit(cluster_id="CLUST-test-001")
        observations = build_observation_samples(
            [hold],
            date="20260712",
            market="cn_futures",
        )
        assert len(observations) == 1
        obs = observations[0]
        assert obs["cluster_id"] == "CLUST-test-001"
        assert obs["cluster_role"] == "origin"
        assert obs["occurrence_index"] == 0

    def test_session_row_propagates_pit_and_cluster_fields(
        self,
        tmp_path: Path,
    ) -> None:
        """Per-session decision rows must include PIT lineage and
        cluster fields."""
        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
        record = {
            "style": "trend",
            "session": "day_morning",
            "symbol": "IF2608.CFX",
            "order": {"intent": "open"},
            "receipt": {
                "status": "filled",
                "filled_qty": 1,
                "execution_eligible": True,
                "execution_class": "execution_eligible",
                "counterfactual_only": False,
                "capital_layer": "simulated",
                "account_type": "simulated",
            },
            "point_in_time_as_of": "2026-07-12 09:35:00",
            "source_event_time": "2026-07-12 09:35:00",
            "source_snapshot_id": "SNAP-002",
            "source_snapshot_sha256": "def456",
            "authority": "master_capital_ledger",
            "lineage_status": "complete",
            "cluster_id": "CLUST-002",
            "cluster_role": "origin",
            "occurrence_index": 0,
        }
        payload = append_review(
            date="20260712",
            market="cn_futures",
            records=[record],
            errors=[],
            path=review_path,
        )
        session_rows = payload.get("session_decisions", [])
        fill_rows = [r for r in session_rows if r["record_type"] == "simulated_fill"]
        assert len(fill_rows) >= 1
        row = fill_rows[0]
        assert row.get("point_in_time_as_of") == "2026-07-12 09:35:00"
        assert row.get("source_event_time") == "2026-07-12 09:35:00"
        assert row.get("authority") == "master_capital_ledger"
        assert row.get("lineage_status") == "complete"
        assert row.get("cluster_id") == "CLUST-002"

    def test_missing_pit_lineage_cannot_be_execution_eligible_in_session_row(
        self,
        tmp_path: Path,
    ) -> None:
        """Session rows without complete PIT lineage must have
        execution_eligible=False."""
        review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
        record = {
            "style": "trend",
            "session": "day_morning",
            "symbol": "IF2608.CFX",
            "order": {"intent": "open"},
            "receipt": {
                "status": "filled",
                "filled_qty": 1,
                "capital_layer": "simulated",
                "account_type": "simulated",
            },
            # No PIT lineage fields
        }
        payload = append_review(
            date="20260712",
            market="cn_futures",
            records=[record],
            errors=[],
            path=review_path,
        )
        session_rows = payload.get("session_decisions", [])
        fill_rows = [r for r in session_rows if r["record_type"] == "simulated_fill"]
        assert len(fill_rows) >= 1
        row = fill_rows[0]
        # Without PIT lineage, execution must not be eligible
        assert row.get("execution_eligible") is False
