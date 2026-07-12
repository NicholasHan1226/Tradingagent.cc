from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from CNFutures.execution_evidence import SCHEMA_VERSION as EXECUTION_EVIDENCE_SCHEMA
from CNFutures.review import append_review
from CNFutures.sample_maturity import (
    build_futures_maturity_projection,
    canonical_futures_maturity_projection_sha256,
)
from shared.runtime_test.cn_futures_sample_ops import (
    CNFuturesSampleOpsSafetyError,
    run_cn_futures_sample_ops,
)


CURRENT_AUTHORITY = {
    "source": "market_capital_ledger",
    "authority_id": "cn-futures-capital-v1",
    "authority_generation": 1,
    "execution_lineage_id": "cn-futures-sim-fresh-20260712-v1",
    "initial_equity_cny": 50_000.0,
    "margin_utilization_limit_cny": 25_000.0,
    "real_trading_enabled": False,
}


def test_projection_hash_has_cross_runtime_integer_float_canonical_vector() -> None:
    vector = {
        "pool_cny": 50_000.0,
        "margin": 25_000,
        "ratios": [1.0, 0.125, -0.0],
        "nested": {"b": 2.5, "a": 3.0},
        "enabled": False,
    }

    assert canonical_futures_maturity_projection_sha256(vector) == (
        "6092993cd591aa8dfda7c94f0134811cc4433afc53ccf7b4ab48728076c686f8"
    )


class _ForwardLabelReader:
    def get_bars_intraday(
        self,
        market: str,
        symbol: str,
        interval: str,
        start: str,
        end: str,
    ) -> list[dict[str, object]]:
        return [
            {
                "bar_time": "2026-07-13T10:05:00+08:00",
                "close": 3_510.0,
                "source": "sharedsignals_futures_bars",
            },
            {
                "bar_time": "2026-07-13T10:35:00+08:00",
                "close": 3_520.0,
                "source": "sharedsignals_futures_bars",
            },
            {
                "bar_time": "2026-07-13T15:00:00+08:00",
                "close": 3_530.0,
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
        return [
            {
                "trade_date": trade_date,
                "close": 3_540.0 + index * 10,
                "source": "sharedsignals_futures_daily",
            }
            for index, trade_date in enumerate(
                ["20260714", "20260715", "20260716", "20260717", "20260720"]
            )
        ]


def _label_evidence(
    source_sha: str,
    *,
    labeled: tuple[str, ...] = (),
    pending: tuple[str, ...] = (),
) -> dict[str, dict[str, object]]:
    labels: dict[str, dict[str, object]] = {}
    for horizon, status in [
        *((horizon, "ready") for horizon in labeled),
        *((horizon, "pending_not_due") for horizon in pending),
    ]:
        evidence: dict[str, object] = {
            "horizon": horizon,
            "status": status,
            "point_in_time_as_of": "2026-07-14T15:00:00+08:00",
            "source_snapshot_sha256": source_sha,
            "cost_model_version": "cn-futures-conservative-v1",
            "real_trading_enabled": False,
        }
        evidence["label_evidence_sha256"] = hashlib.sha256(
            json.dumps(
                evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
        ).hexdigest()
        labels[horizon] = evidence
    return labels


def _session_row(
    *,
    identity: str,
    trade_date: str,
    session: str,
    record_type: str,
    symbol: str,
    style: str = "trend",
    execution_eligible: bool = False,
    counterfactual_only: bool = False,
    reason: str = "",
    scenario_tags: dict[str, object] | None = None,
    size_decision: dict[str, object] | None = None,
    intent: str = "",
    round_trip_economics: dict[str, float] | None = None,
    labeled_horizons: tuple[str, ...] = (),
    pending_horizons: tuple[str, ...] = (),
) -> dict[str, object]:
    row: dict[str, object] = {
        "_row_type": "cn_futures_session_decision",
        "_identity": identity,
        "trade_date": trade_date,
        "session": session,
        "record_type": record_type,
        "style": style,
        "symbol": symbol,
        "execution_eligible": execution_eligible,
        "execution_class": (
            "execution_eligible"
            if execution_eligible
            else "counterfactual_only"
            if counterfactual_only
            else "observation_only"
        ),
        "counterfactual_only": counterfactual_only,
        "real_trading_enabled": False,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "bar_time": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]} 09:35:00",
        "decision": {
            "reason": reason,
            "scenario_tags": dict(scenario_tags or {}),
            "size_decision": dict(size_decision or {}),
            "intent": intent,
        },
        "lineage_status": "complete",
        "point_in_time_as_of": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}T09:35:00+08:00",
        "source_snapshot_id": f"snapshot-{identity}",
        "source_snapshot_sha256": hashlib.sha256(identity.encode()).hexdigest(),
        "authority": "SharedSignals",
        "capital_authority_id": "cn-futures-capital-v1",
        "authority_generation": 1,
        "execution_lineage_id": "cn-futures-sim-fresh-20260712-v1",
    }
    row["labels"] = _label_evidence(
        str(row["source_snapshot_sha256"]),
        labeled=labeled_horizons,
        pending=pending_horizons,
    )
    if execution_eligible:
        is_close = intent in {"reduce_only", "close", "flatten_no_overnight"}
        source_sha = str(row["source_snapshot_sha256"])
        execution_evidence: dict[str, object] = {
            "schema_version": EXECUTION_EVIDENCE_SCHEMA,
            "capital_authority_id": "cn-futures-capital-v1",
            "authority_generation": 1,
            "execution_lineage_id": "cn-futures-sim-fresh-20260712-v1",
            "order_id": f"order-{identity}",
            "symbol": symbol,
            "side": "sell" if is_close else "buy",
            "execution_fill_id": f"fill-{identity}",
            "filled_quantity": 1,
            "fill_price": 3_500.0,
            "requested_price": 3_499.3,
            "fee_cash_cny": 3.0,
            "slippage_bps": 2.0,
            "slippage_cny": 7.0,
            "fill_evidence_type": "bar_volume_participation",
            "evidence_timestamp": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}T09:35:00+08:00",
            "margin_required_cny": 4_500.0,
            "contract_multiplier": 10,
            "contract_spec_version": "cn-futures-executor-rule.v1",
            "contract_spec_sha256": hashlib.sha256(
                f"contract-{identity}".encode()
            ).hexdigest(),
            "receipt_sha256": hashlib.sha256(
                f"receipt-{identity}".encode()
            ).hexdigest(),
            "local_state_sha256": hashlib.sha256(
                f"position-{identity}".encode()
            ).hexdigest(),
            "capital_commit_action": "position_close_commit"
            if is_close
            else "fill_commit",
            "capital_commit_action_id": f"action-{identity}",
            "capital_commit_reference_id": f"reference-{identity}",
            "capital_commit_status": "committed",
            "capital_commit_event_id": f"MCAP-{identity}",
            "capital_commit_event_checksum": hashlib.sha256(
                f"commit-{identity}".encode()
            ).hexdigest(),
            "source_snapshot_sha256": source_sha,
            "real_trading_enabled": False,
        }
        execution_evidence["execution_evidence_sha256"] = hashlib.sha256(
            json.dumps(
                execution_evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
        ).hexdigest()
        row["execution_evidence"] = execution_evidence
        if is_close and round_trip_economics is not None:
            round_trip_evidence: dict[str, object] = {
                "schema_version": "cn_futures.round_trip_evidence.v1",
                "round_trip_complete": True,
                "costs_cover": "round_trip",
                "entry_fill_id": f"entry-{identity}",
                "exit_fill_id": f"fill-{identity}",
                "gross_pnl_cny": round_trip_economics["gross_pnl_cny"],
                "fee_cny": round_trip_economics["fee_cny"],
                "slippage_cny": round_trip_economics["slippage_cny"],
                "net_pnl_cny": round_trip_economics["net_pnl_cny"],
                "entry_evidence_sha256": hashlib.sha256(
                    f"entry-{identity}".encode()
                ).hexdigest(),
                "exit_evidence_sha256": hashlib.sha256(
                    f"exit-{identity}".encode()
                ).hexdigest(),
                "capital_authority_id": "cn-futures-capital-v1",
                "authority_generation": 1,
                "execution_lineage_id": "cn-futures-sim-fresh-20260712-v1",
                "real_trading_enabled": False,
            }
            round_trip_evidence["round_trip_evidence_sha256"] = hashlib.sha256(
                json.dumps(
                    round_trip_evidence,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode()
            ).hexdigest()
            row["round_trip_evidence"] = round_trip_evidence
    content = json.dumps(row, ensure_ascii=False, sort_keys=True)
    row["_checksum"] = hashlib.sha256(content.encode()).hexdigest()
    return row


def _summary(
    *,
    trade_date: str,
    session_rows: list[dict[str, object]],
    observations: list[dict[str, object]] | None = None,
    completed_round_trips: int = 0,
    wins: int = 0,
    losses: int = 0,
    gross_pnl: float = 0.0,
    net_pnl: float = 0.0,
    fee: float = 0.0,
    labeled: int = 0,
    pending: int = 0,
) -> dict[str, object]:
    row: dict[str, object] = {
        "date": trade_date,
        "market": "cn_futures",
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real_trading_enabled": False,
        "score_contract_version": "cn-futures-review-economics.v2",
        "authority_scope": {
            "capital_authority_id": "cn-futures-capital-v1",
            "authority_generation": 1,
            "execution_lineage_id": "cn-futures-sim-fresh-20260712-v1",
        },
        "generated_at": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}T08:00:00+00:00",
        "session_decisions": session_rows,
        "observation_samples": list(observations or []),
        "run_score_summary": {
            "style_scores": {
                "trend": {
                    "completed_round_trip_count": completed_round_trips,
                    "pnl_sample_count": completed_round_trips,
                    "wins": wins,
                    "losses": losses,
                    "gross_realized_pnl": gross_pnl,
                    "realized_pnl": net_pnl,
                    "fee": fee,
                    "max_drawdown": max(0.0, -net_pnl),
                }
            }
        },
        "forward_label_summary": {
            "styles": {
                "trend": {
                    "labeled": labeled,
                    "pending": pending,
                    "wins": min(labeled, wins),
                    "losses": max(0, labeled - wins),
                }
            }
        },
    }
    return row


def _observation(
    *,
    observation_id: str,
    trade_date: str,
    symbol: str,
    product: str,
    session: str,
    volatility_regime: str,
    counterfactual_only: bool = True,
    reason: str = "minimum_one_lot_exceeds_margin_budget",
    extra_tags: dict[str, object] | None = None,
    labeled_horizons: tuple[str, ...] = (),
    pending_horizons: tuple[str, ...] = (),
) -> dict[str, object]:
    tags: dict[str, object] = {
        "volatility_regime": volatility_regime,
        "product": product,
        **dict(extra_tags or {}),
    }
    row: dict[str, object] = {
        "observation_id": observation_id,
        "date": trade_date,
        "market": "cn_futures",
        "style": "trend",
        "symbol": symbol,
        "product": product,
        "session": session,
        "stage": "risk",
        "reason": reason,
        "sample_intent": "counterfactual" if counterfactual_only else "observe",
        "execution_class": "counterfactual_only"
        if counterfactual_only
        else "observation_only",
        "execution_eligible": False,
        "counterfactual_only": counterfactual_only,
        "label_eligible": True,
        "label_status": "pending",
        "scenario_tags": tags,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real_trading_enabled": False,
        "lineage_status": "complete",
        "source_snapshot_sha256": hashlib.sha256(observation_id.encode()).hexdigest(),
        "capital_authority_id": "cn-futures-capital-v1",
        "authority_generation": 1,
        "execution_lineage_id": "cn-futures-sim-fresh-20260712-v1",
    }
    row["labels"] = _label_evidence(
        str(row["source_snapshot_sha256"]),
        labeled=labeled_horizons,
        pending=pending_horizons,
    )
    row["_checksum"] = hashlib.sha256(
        json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
    return row


def _write_review(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _forward_label_update(
    target: dict[str, object],
    *,
    as_of: str,
    labeled_horizons: tuple[str, ...] = (),
    pending_horizons: tuple[str, ...] = (),
) -> dict[str, object]:
    source_sha = str(target["source_snapshot_sha256"])
    cost_model_version = "cn-futures-conservative-v1"
    labels = _label_evidence(
        source_sha,
        labeled=labeled_horizons,
        pending=pending_horizons,
    )
    for label in labels.values():
        label["point_in_time_as_of"] = as_of
        label["cost_model_version"] = cost_model_version
        label.pop("label_evidence_sha256")
        label["label_evidence_sha256"] = hashlib.sha256(
            json.dumps(
                label,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
        ).hexdigest()
    update: dict[str, object] = {
        "schema_version": "cn_futures.forward_label_update.v1",
        "record_type": "cn_futures_forward_label_update",
        "target_identity": target["_identity"],
        "target_record_type": target["record_type"],
        "trade_date": target["trade_date"],
        "style": target["style"],
        "symbol": target["symbol"],
        "source_snapshot_sha256": source_sha,
        "capital_authority_id": "cn-futures-capital-v1",
        "authority_generation": 1,
        "execution_lineage_id": "cn-futures-sim-fresh-20260712-v1",
        "point_in_time_as_of": as_of,
        "cost_model_version": cost_model_version,
        "labels": labels,
        "real_trading_enabled": False,
    }
    update["update_id"] = (
        "CNFLABEL-"
        + hashlib.sha256(
            json.dumps(
                update,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
        ).hexdigest()[:32]
    )
    update["journal_payload_sha256"] = hashlib.sha256(
        json.dumps(
            update,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
    return update


def test_empty_fresh_start_persists_one_manual_only_maturity_projection(
    tmp_path: Path,
) -> None:
    review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
    output_dir = tmp_path / "review" / "cn_futures"

    report = run_cn_futures_sample_ops(
        review_path=review_path,
        review_dir=output_dir,
        trade_date="20260713",
        as_of="2026-07-13T16:00:00+08:00",
        authority_state=CURRENT_AUTHORITY,
        environ={},
    )

    assert report["overall_status"] == "warn"
    assert report["orders_created"] == 0
    assert report["emails_sent"] == 0
    assert report["automatic_promotion_enabled"] is False
    assert report["automatic_risk_expansion_enabled"] is False
    assert report["live_transition_authorized"] is False
    assert report["real_trading_enabled"] is False
    assert sorted(path.name for path in output_dir.iterdir()) == [
        "market_maturity_latest.json"
    ]

    maturity = json.loads(
        (output_dir / "market_maturity_latest.json").read_text(encoding="utf-8")
    )
    assert maturity["report_type"] == "cn_futures_market_maturity_v1"
    assert maturity["authority_scope"] == {
        "capital_authority_id": "cn-futures-capital-v1",
        "authority_generation": 1,
        "execution_lineage_id": "cn-futures-sim-fresh-20260712-v1",
    }
    assert maturity["pool_cny"] == 50_000
    assert maturity["margin_utilization_limit_cny"] == 25_000
    assert maturity["total_simulation_trading_days"] == 0
    assert maturity["sample_counts"]["valid_sample_count"] == 0
    assert maturity["promotion_evidence_ready"] is False
    assert "insufficient_valid_samples_0_of_5" in maturity["blocking_reasons"]
    assert maturity["projection_sha256"] == (
        canonical_futures_maturity_projection_sha256(maturity)
    )


def test_sim_only_wrapper_and_session_close_cron_templates_are_declared() -> None:
    root = Path(__file__).resolve().parents[1]
    wrapper = root / "shared" / "wrappers" / "job_cn_futures_sample_ops.sh"
    assert wrapper.exists()
    wrapper_text = wrapper.read_text(encoding="utf-8")
    assert "REAL_TRADING_ENABLED=false" in wrapper_text
    assert "-m shared.runtime_test.cn_futures_sample_ops" in wrapper_text
    assert "job_cn_futures_sample_ops" in wrapper_text
    entries = (
        "40 11 * * 1-5 /opt/investment/tradingagent/shared/wrappers/job_cn_futures_sample_ops.sh",
        "10 15,23 * * 1-5 /opt/investment/tradingagent/shared/wrappers/job_cn_futures_sample_ops.sh",
        "40 2 * * 2-6 /opt/investment/tradingagent/shared/wrappers/job_cn_futures_sample_ops.sh",
    )
    for relative in ("shared/crontab.txt", "crontab.txt"):
        schedule = (root / relative).read_text(encoding="utf-8")
        for entry in entries:
            assert entry in schedule


def test_sample_ops_materializes_counterfactual_labels_before_maturity_and_is_idempotent(
    tmp_path: Path,
) -> None:
    review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
    output_dir = tmp_path / "review" / "cn_futures"
    source_sha = "a" * 64
    append_review(
        date="20260713",
        market="cn_futures",
        records=[
            {
                "record_type": "risk_reject",
                "session": "day_morning",
                "style": "trend",
                "style_version": "trend-v1",
                "symbol": "RB2610.SHF",
                "bar_time": "2026-07-13T09:35:00+08:00",
                "entry_price": 3_500.0,
                "direction": "buy",
                "reason": "minimum_one_lot_exceeds_margin_budget",
                "execution_eligible": False,
                "counterfactual_only": True,
                "execution_class": "counterfactual_only",
                "point_in_time_as_of": "2026-07-13T09:35:00+08:00",
                "source_event_time": "2026-07-13T09:35:00+08:00",
                "source_snapshot_id": "CNF-SNAP-" + source_sha[:16],
                "source_snapshot_sha256": source_sha,
                "authority": "market_capital_ledger",
                "lineage_status": "complete",
                "capital_authority_id": "cn-futures-capital-v1",
                "authority_generation": 1,
                "execution_lineage_id": "cn-futures-sim-fresh-20260712-v1",
                "cluster_id": "CNF-CLUST-RISK-LABEL-1",
                "real_trading_enabled": False,
            }
        ],
        errors=[],
        path=review_path,
        authority_scope={
            "capital_authority_id": "cn-futures-capital-v1",
            "authority_generation": 1,
            "execution_lineage_id": "cn-futures-sim-fresh-20260712-v1",
        },
    )

    first = run_cn_futures_sample_ops(
        review_path=review_path,
        review_dir=output_dir,
        trade_date="20260720",
        as_of="2026-07-20T15:05:00+08:00",
        authority_state=CURRENT_AUTHORITY,
        reader=_ForwardLabelReader(),
        environ={},
    )

    assert first["label_materialization"]["appended_update_count"] == 1
    assert first["market_maturity"]["sample_counts"]["valid_sample_count"] == 1
    assert first["market_maturity"]["sample_counts"]["counterfactual_only_count"] == 1
    assert first["market_maturity"]["sample_counts"]["forward_label_count"] == 6
    assert first["market_maturity"]["accepted_forward_label_update_count"] == 1

    repeated = run_cn_futures_sample_ops(
        review_path=review_path,
        review_dir=output_dir,
        trade_date="20260720",
        as_of="2026-07-20T15:05:00+08:00",
        authority_state=CURRENT_AUTHORITY,
        reader=_ForwardLabelReader(),
        environ={},
    )
    assert repeated["label_materialization"]["appended_update_count"] == 0
    assert repeated["label_materialization"]["idempotent_update_count"] == 1
    assert repeated["market_maturity"]["sample_counts"]["forward_label_count"] == 6
    updates = [
        json.loads(line)
        for line in review_path.read_text(encoding="utf-8").splitlines()
        if "cn_futures_forward_label_update" in line
    ]
    assert len(updates) == 1


def test_live_review_preflight_blocks_before_any_label_append_or_projection_write(
    tmp_path: Path,
) -> None:
    review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
    output_dir = tmp_path / "review" / "cn_futures"
    source_sha = "b" * 64
    append_review(
        date="20260713",
        market="cn_futures",
        records=[
            {
                "record_type": "prediction",
                "session": "day_morning",
                "style": "trend",
                "symbol": "RB2610.SHF",
                "bar_time": "2026-07-13T09:35:00+08:00",
                "entry_price": 3_500.0,
                "direction": "buy",
                "point_in_time_as_of": "2026-07-13T09:35:00+08:00",
                "source_event_time": "2026-07-13T09:35:00+08:00",
                "source_snapshot_id": "CNF-SNAP-" + source_sha[:16],
                "source_snapshot_sha256": source_sha,
                "authority": "market_capital_ledger",
                "lineage_status": "complete",
                "capital_authority_id": "cn-futures-capital-v1",
                "authority_generation": 1,
                "execution_lineage_id": "cn-futures-sim-fresh-20260712-v1",
                "cluster_id": "CNF-CLUST-UNSAFE-LIVE",
                "real_trading_enabled": False,
            }
        ],
        errors=[],
        path=review_path,
        authority_scope={
            "capital_authority_id": "cn-futures-capital-v1",
            "authority_generation": 1,
            "execution_lineage_id": "cn-futures-sim-fresh-20260712-v1",
        },
    )
    payload = json.loads(review_path.read_text(encoding="utf-8"))
    payload["real_trading_enabled"] = True
    review_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    before = review_path.read_bytes()

    with pytest.raises(CNFuturesSampleOpsSafetyError, match="live_review_marker"):
        run_cn_futures_sample_ops(
            review_path=review_path,
            review_dir=output_dir,
            trade_date="20260720",
            as_of="2026-07-20T15:05:00+08:00",
            authority_state=CURRENT_AUTHORITY,
            reader=_ForwardLabelReader(),
            environ={},
        )

    assert review_path.read_bytes() == before
    assert not output_dir.exists()


def test_projection_counts_current_authority_samples_and_coverage_without_mixing(
    tmp_path: Path,
) -> None:
    review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
    old = _summary(
        trade_date="20260710",
        session_rows=[
            _session_row(
                identity="old-fill",
                trade_date="20260710",
                session="day_morning",
                record_type="simulated_fill",
                symbol="IF2608.CFFEX",
                execution_eligible=True,
            )
        ],
        completed_round_trips=99,
        wins=99,
        gross_pnl=99_000,
        net_pnl=98_000,
        fee=1_000,
        labeled=99,
    )
    day_one_observations = [
        _observation(
            observation_id="obs-rb-high",
            trade_date="20260713",
            symbol="RB2610.SHF",
            product="rb",
            session="day_morning",
            volatility_regime="high",
            labeled_horizons=("m30", "m60"),
        ),
        _observation(
            observation_id="obs-au-high-night",
            trade_date="20260713",
            symbol="AU2612.SHF",
            product="au",
            session="night",
            volatility_regime="high",
            pending_horizons=("m30",),
        ),
    ]
    day_one = _summary(
        trade_date="20260713",
        session_rows=[
            _session_row(
                identity="fill-rb-close",
                trade_date="20260713",
                session="day_morning",
                record_type="simulated_fill",
                symbol="RB2610.SHF",
                execution_eligible=True,
                scenario_tags={"volatility_regime": "high", "product": "rb"},
                size_decision={
                    "margin_per_lot": 4_500,
                    "modeled_slippage_bps": 8,
                },
                intent="reduce_only",
                round_trip_economics={
                    "gross_pnl_cny": 105,
                    "fee_cny": 10,
                    "slippage_cny": 5,
                    "net_pnl_cny": 90,
                },
                labeled_horizons=("close",),
            ),
            _session_row(
                identity="risk-au-night",
                trade_date="20260713",
                session="night",
                record_type="risk_reject",
                symbol="AU2612.SHF",
                counterfactual_only=True,
                reason="minimum_one_lot_exceeds_margin_budget",
                scenario_tags={"volatility_regime": "high", "product": "au"},
                size_decision={"margin_per_lot": 28_000, "modeled_slippage_bps": 12},
            ),
        ],
        observations=day_one_observations,
        completed_round_trips=1,
        wins=1,
        gross_pnl=100,
        net_pnl=90,
        fee=10,
        labeled=2,
        pending=1,
    )
    day_two_observations = [
        _observation(
            observation_id="obs-cu-low-roll",
            trade_date="20260714",
            symbol="CU2609.SHF",
            product="cu",
            session="day_afternoon",
            volatility_regime="low",
            extra_tags={
                "contract_rollover_handled": True,
                "extreme_risk_scenario": "limit_gap",
            },
            labeled_horizons=("1d",),
        )
    ]
    day_two = _summary(
        trade_date="20260714",
        session_rows=[
            _session_row(
                identity="fill-cu-close",
                trade_date="20260714",
                session="day_afternoon",
                record_type="simulated_fill",
                symbol="CU2609.SHF",
                execution_eligible=True,
                scenario_tags={
                    "volatility_regime": "low",
                    "product": "cu",
                    "contract_rollover_handled": True,
                    "extreme_risk_scenario": "limit_gap",
                },
                size_decision={
                    "margin_per_lot": 6_000,
                    "modeled_slippage_bps": 9,
                },
                intent="reduce_only",
                round_trip_economics={
                    "gross_pnl_cny": 20,
                    "fee_cny": 3,
                    "slippage_cny": 2,
                    "net_pnl_cny": 15,
                },
                pending_horizons=("m60", "1d"),
            ),
            _session_row(
                identity="fill-au-close",
                trade_date="20260714",
                session="night",
                record_type="simulated_fill",
                symbol="AU2612.SHF",
                execution_eligible=True,
                scenario_tags={"volatility_regime": "low", "product": "au"},
                size_decision={
                    "margin_per_lot": 8_000,
                    "modeled_slippage_bps": 7,
                },
                intent="reduce_only",
                round_trip_economics={
                    "gross_pnl_cny": 10,
                    "fee_cny": 3,
                    "slippage_cny": 2,
                    "net_pnl_cny": 5,
                },
            ),
        ],
        observations=day_two_observations,
        completed_round_trips=2,
        wins=1,
        losses=1,
        gross_pnl=50,
        net_pnl=20,
        fee=30,
        labeled=2,
        pending=2,
    )
    _write_review(review_path, [old, day_one, day_two])

    projection = build_futures_maturity_projection(
        review_path=review_path,
        authority_state=CURRENT_AUTHORITY,
        fresh_start_trade_date="20260713",
        trade_date="20260714",
        generated_at="2026-07-14T16:00:00+08:00",
    )

    assert projection["simulation_trading_days"] == ["20260713", "20260714"]
    assert projection["total_simulation_trading_days"] == 2
    assert projection["excluded_pre_fresh_start_review_count"] == 1
    assert projection["sample_counts"] == {
        "valid_sample_count": 6,
        "observation_counterfactual_count": 3,
        "counterfactual_only_count": 3,
        "execution_eligible_sample_count": 3,
        "completed_round_trip_count": 3,
        "forward_label_count": 4,
        "pending_forward_label_count": 3,
        "risk_reject_count": 1,
        "exploration_fill_count": 0,
        "exploitation_fill_count": 0,
    }
    coverage = projection["coverage"]
    assert coverage["products"] == ["au", "cu", "rb"]
    assert coverage["volatility_regimes"] == ["high", "low"]
    # The risk-reject session row and its richer observation are two
    # representations of one decision, so coverage must not double count it.
    assert coverage["night_session_sample_count"] == 2
    assert coverage["rollover_sample_count"] == 2
    assert coverage["margin_evidence_sample_count"] == 3
    assert coverage["fee_evidence_sample_count"] == 3
    assert coverage["slippage_evidence_sample_count"] == 3
    assert coverage["extreme_risk_sample_count"] == 2
    assert projection["performance"]["post_cost_pnl_cny"] == pytest.approx(110)
    assert projection["performance"]["expectancy_cny"] == pytest.approx(110 / 3)
    assert projection["automatic_promotion_enabled"] is False
    assert projection["automatic_risk_expansion_enabled"] is False
    assert projection["live_transition_authorized"] is False
    assert projection["real_trading_enabled"] is False
    assert projection["promotion_evidence_ready"] is False
    assert "missing_independent_stability_evidence" in projection["blocking_reasons"]


def test_duplicate_session_and_observation_identities_do_not_inflate_counts(
    tmp_path: Path,
) -> None:
    review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
    decision = _session_row(
        identity="same-fill",
        trade_date="20260713",
        session="day_morning",
        record_type="simulated_fill",
        symbol="RB2610.SHF",
        execution_eligible=True,
        size_decision={"margin_per_lot": 4_500, "modeled_slippage_bps": 8},
        intent="reduce_only",
        round_trip_economics={
            "gross_pnl_cny": 12,
            "fee_cny": 3,
            "slippage_cny": 1,
            "net_pnl_cny": 8,
        },
    )
    observation = _observation(
        observation_id="same-observation",
        trade_date="20260713",
        symbol="RB2610.SHF",
        product="rb",
        session="day_morning",
        volatility_regime="high",
        labeled_horizons=("m30",),
    )
    first = _summary(
        trade_date="20260713",
        session_rows=[decision],
        observations=[observation],
        completed_round_trips=1,
        wins=1,
        gross_pnl=10,
        net_pnl=8,
        fee=2,
        labeled=1,
    )
    duplicate = json.loads(json.dumps(first))
    _write_review(review_path, [first, duplicate])

    projection = build_futures_maturity_projection(
        review_path=review_path,
        authority_state=CURRENT_AUTHORITY,
        fresh_start_trade_date="20260713",
        trade_date="20260713",
        generated_at="2026-07-13T16:00:00+08:00",
    )

    assert projection["sample_counts"]["valid_sample_count"] == 2
    assert projection["sample_counts"]["completed_round_trip_count"] == 1
    assert projection["sample_counts"]["forward_label_count"] == 1
    assert projection["duplicate_session_decision_count"] == 1
    assert projection["duplicate_observation_count"] == 1


def test_append_only_forward_label_updates_replace_embedded_pending_by_target_identity(
    tmp_path: Path,
) -> None:
    review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
    target = _session_row(
        identity="label-target",
        trade_date="20260713",
        session="day_morning",
        record_type="prediction",
        symbol="RB2610.SHF",
        pending_horizons=("m30", "m60"),
    )
    first = _forward_label_update(
        target,
        as_of="2026-07-13T10:10:00+08:00",
        pending_horizons=("m30", "m60"),
    )
    latest = _forward_label_update(
        target,
        as_of="2026-07-13T10:40:00+08:00",
        labeled_horizons=("m30",),
        pending_horizons=("m60",),
    )
    _write_review(
        review_path,
        [
            _summary(trade_date="20260713", session_rows=[target]),
            first,
            latest,
        ],
    )

    projection = build_futures_maturity_projection(
        review_path=review_path,
        authority_state=CURRENT_AUTHORITY,
        fresh_start_trade_date="20260713",
        trade_date="20260713",
        generated_at="2026-07-13T16:00:00+08:00",
    )

    assert projection["sample_counts"]["valid_sample_count"] == 1
    assert projection["sample_counts"]["forward_label_count"] == 1
    assert projection["sample_counts"]["pending_forward_label_count"] == 1
    assert projection["accepted_forward_label_update_count"] == 2
    assert projection["superseded_forward_label_update_count"] == 1
    assert projection["invalid_forward_label_update_count"] == 0


def test_duplicate_cluster_weight_zero_excludes_target_and_forward_labels(
    tmp_path: Path,
) -> None:
    review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
    duplicate = _session_row(
        identity="duplicate-label-target",
        trade_date="20260713",
        session="day_morning",
        record_type="prediction",
        symbol="RB2610.SHF",
        labeled_horizons=("m30",),
    )
    duplicate.update(
        {
            "cluster_id": "CNF-CLUST-DUPLICATE",
            "cluster_role": "duplicate",
            "occurrence_index": 1,
            "weight_multiplier": 0.0,
        }
    )
    duplicate.pop("_checksum")
    duplicate["_checksum"] = hashlib.sha256(
        json.dumps(duplicate, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    update = _forward_label_update(
        duplicate,
        as_of="2026-07-13T10:40:00+08:00",
        labeled_horizons=("m30",),
    )
    _write_review(
        review_path,
        [_summary(trade_date="20260713", session_rows=[duplicate]), update],
    )

    projection = build_futures_maturity_projection(
        review_path=review_path,
        authority_state=CURRENT_AUTHORITY,
        fresh_start_trade_date="20260713",
        trade_date="20260713",
        generated_at="2026-07-13T16:00:00+08:00",
    )

    assert projection["sample_counts"]["valid_sample_count"] == 0
    assert projection["sample_counts"]["forward_label_count"] == 0
    assert projection["zero_maturity_weight_sample_count"] == 1
    assert projection["zero_maturity_weight_forward_label_update_count"] == 1


def test_missing_or_mismatched_embedded_authority_is_fully_excluded(
    tmp_path: Path,
) -> None:
    review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
    missing_scope = _summary(
        trade_date="20260713",
        session_rows=[
            _session_row(
                identity="missing-scope-fill",
                trade_date="20260713",
                session="day_morning",
                record_type="simulated_fill",
                symbol="RB2610.SHF",
                execution_eligible=True,
                intent="reduce_only",
                round_trip_economics={
                    "gross_pnl_cny": 1_000,
                    "fee_cny": 10,
                    "slippage_cny": 10,
                    "net_pnl_cny": 980,
                },
            )
        ],
        completed_round_trips=999,
        net_pnl=999_999,
        fee=1,
        labeled=999,
    )
    missing_scope.pop("authority_scope")
    mismatched = _summary(
        trade_date="20260714",
        session_rows=[
            _session_row(
                identity="wrong-lineage",
                trade_date="20260714",
                session="night",
                record_type="risk_reject",
                symbol="AU2612.SHF",
                counterfactual_only=True,
                reason="margin_cap",
            )
        ],
        labeled=999,
    )
    mismatched["authority_scope"]["execution_lineage_id"] = "retired-epoch-2"  # type: ignore[index]
    _write_review(review_path, [missing_scope, mismatched])

    projection = build_futures_maturity_projection(
        review_path=review_path,
        authority_state=CURRENT_AUTHORITY,
        fresh_start_trade_date="20260713",
        trade_date="20260714",
        generated_at="2026-07-14T16:00:00+08:00",
    )

    assert projection["sample_counts"]["valid_sample_count"] == 0
    assert projection["sample_counts"]["completed_round_trip_count"] == 0
    assert projection["sample_counts"]["forward_label_count"] == 0
    assert projection["performance"]["post_cost_pnl_cny"] is None
    assert projection["excluded_missing_authority_scope_review_count"] == 1
    assert projection["excluded_authority_mismatch_review_count"] == 1


def test_unsigned_summary_economics_and_invalid_execution_evidence_never_count(
    tmp_path: Path,
) -> None:
    review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
    invalid_fill = _session_row(
        identity="invalid-fill-evidence",
        trade_date="20260713",
        session="day_morning",
        record_type="simulated_fill",
        symbol="RB2610.SHF",
        execution_eligible=True,
        intent="reduce_only",
        round_trip_economics={
            "gross_pnl_cny": 100,
            "fee_cny": 10,
            "slippage_cny": 5,
            "net_pnl_cny": 85,
        },
    )
    invalid_fill["execution_evidence"]["capital_commit_event_checksum"] = "0" * 64  # type: ignore[index]
    content = {key: value for key, value in invalid_fill.items() if key != "_checksum"}
    invalid_fill["_checksum"] = hashlib.sha256(
        json.dumps(content, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    observation = _observation(
        observation_id="only-valid-observation",
        trade_date="20260713",
        symbol="RB2610.SHF",
        product="rb",
        session="day_morning",
        volatility_regime="high",
        labeled_horizons=("m30",),
    )
    summary = _summary(
        trade_date="20260713",
        session_rows=[invalid_fill],
        observations=[observation],
        completed_round_trips=999,
        wins=999,
        gross_pnl=1_000_000,
        net_pnl=999_999,
        fee=1,
        labeled=999,
    )
    _write_review(review_path, [summary])

    projection = build_futures_maturity_projection(
        review_path=review_path,
        authority_state=CURRENT_AUTHORITY,
        fresh_start_trade_date="20260713",
        trade_date="20260713",
        generated_at="2026-07-13T16:00:00+08:00",
    )

    assert projection["sample_counts"]["valid_sample_count"] == 1
    assert projection["sample_counts"]["execution_eligible_sample_count"] == 0
    assert projection["sample_counts"]["completed_round_trip_count"] == 0
    assert projection["sample_counts"]["forward_label_count"] == 1
    assert projection["performance"]["post_cost_pnl_cny"] is None
    assert projection["invalid_execution_evidence_sample_count"] == 1
    assert (
        "invalid_execution_evidence_samples_excluded" in projection["blocking_reasons"]
    )


@pytest.mark.parametrize(
    "authority_override",
    [
        {"authority_id": "retired-shared-master"},
        {"authority_generation": 2},
        {"initial_equity_cny": 100_000},
        {"margin_utilization_limit_cny": 50_000},
        {"execution_lineage_id": ""},
        {"real_trading_enabled": True},
    ],
)
def test_wrong_or_unsafe_capital_authority_fails_before_write(
    tmp_path: Path,
    authority_override: dict[str, object],
) -> None:
    output_dir = tmp_path / "review" / "cn_futures"
    authority = {**CURRENT_AUTHORITY, **authority_override}

    with pytest.raises(CNFuturesSampleOpsSafetyError):
        run_cn_futures_sample_ops(
            review_path=tmp_path / "reviews.jsonl",
            review_dir=output_dir,
            trade_date="20260713",
            as_of="2026-07-13T16:00:00+08:00",
            authority_state=authority,
            environ={},
        )

    assert not output_dir.exists()


def test_live_review_marker_and_corrupt_session_checksum_fail_before_overwrite(
    tmp_path: Path,
) -> None:
    review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
    output_dir = tmp_path / "review" / "cn_futures"
    output_dir.mkdir(parents=True)
    latest = output_dir / "market_maturity_latest.json"
    latest.write_text('{"sentinel":true}\n', encoding="utf-8")

    unsafe = _summary(trade_date="20260713", session_rows=[])
    unsafe["real_trading_enabled"] = True
    _write_review(review_path, [unsafe])
    with pytest.raises(CNFuturesSampleOpsSafetyError):
        run_cn_futures_sample_ops(
            review_path=review_path,
            review_dir=output_dir,
            trade_date="20260713",
            as_of="2026-07-13T16:00:00+08:00",
            authority_state=CURRENT_AUTHORITY,
            environ={},
        )
    assert latest.read_text(encoding="utf-8") == '{"sentinel":true}\n'

    corrupt = _summary(
        trade_date="20260713",
        session_rows=[
            _session_row(
                identity="corrupt",
                trade_date="20260713",
                session="day_morning",
                record_type="hold",
                symbol="RB2610.SHF",
            )
        ],
    )
    corrupt["session_decisions"][0]["_checksum"] = "0" * 64  # type: ignore[index]
    _write_review(review_path, [corrupt])
    with pytest.raises(CNFuturesSampleOpsSafetyError):
        run_cn_futures_sample_ops(
            review_path=review_path,
            review_dir=output_dir,
            trade_date="20260713",
            as_of="2026-07-13T16:00:00+08:00",
            authority_state=CURRENT_AUTHORITY,
            environ={},
        )
    assert latest.read_text(encoding="utf-8") == '{"sentinel":true}\n'


def test_real_trading_environment_fails_before_write(tmp_path: Path) -> None:
    output_dir = tmp_path / "review" / "cn_futures"

    with pytest.raises(CNFuturesSampleOpsSafetyError):
        run_cn_futures_sample_ops(
            review_path=tmp_path / "reviews.jsonl",
            review_dir=output_dir,
            trade_date="20260713",
            as_of="2026-07-13T16:00:00+08:00",
            authority_state=CURRENT_AUTHORITY,
            environ={"REAL_TRADING_ENABLED": "true"},
        )

    assert not output_dir.exists()
