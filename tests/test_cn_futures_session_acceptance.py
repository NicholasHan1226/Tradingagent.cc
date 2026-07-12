from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
from pathlib import Path

from shared.runtime_test.cn_futures_session_acceptance import (
    evaluate_session_acceptance,
    load_runtime_records,
)


TRADE_DATE = "20260713"


def _complete_lineage(fill: str = "a") -> dict[str, object]:
    digest = fill * 64
    lineage: dict[str, object] = {
        "point_in_time_as_of": "2026-07-13T09:35:00+08:00",
        "source_event_time": "2026-07-13T09:35:00+08:00",
        "source_snapshot_id": f"CNF-SNAP-{digest[:16]}",
        "source_snapshot_sha256": digest,
        "authority": "market_capital_ledger",
        "lineage_status": "complete",
    }
    evidence: dict[str, object] = {
        "schema_version": "cn_futures.execution_evidence.v1",
        "capital_authority_id": "cn-futures-capital-v1",
        "authority_generation": 1,
        "execution_lineage_id": "cn-futures-sim-fresh-20260712-v1",
        "order_id": "SIM-CNF-ACCEPT-1",
        "symbol": "RB2610.SHF",
        "side": "buy",
        "execution_fill_id": "CNF-FILL-ACCEPT-1",
        "filled_quantity": 1,
        "fill_price": 3500.0,
        "requested_price": 3499.3,
        "fee_cash_cny": 7.0,
        "slippage_bps": 2.0,
        "fill_evidence_type": "bar_volume_participation",
        "evidence_timestamp": "2026-07-13T09:35:00+08:00",
        "margin_required_cny": 4550.0,
        "contract_multiplier": 10.0,
        "slippage_cny": 7.0,
        "contract_spec_version": "cn-futures-contract-spec.v1",
        "contract_spec_sha256": "c" * 64,
        "receipt_sha256": "d" * 64,
        "local_state_sha256": "e" * 64,
        "capital_commit_action": "fill_commit",
        "capital_commit_action_id": "MCAP-ACTION-ACCEPT-1",
        "capital_commit_reference_id": "MCAPFILL:1:lineage:reservation:fill",
        "capital_commit_status": "committed",
        "capital_commit_event_id": "MCAP-EVENT-ACCEPT-1",
        "capital_commit_event_checksum": "f" * 64,
        "source_snapshot_sha256": digest,
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
    lineage["execution_evidence"] = evidence
    return lineage


def _with_execution_fill_id(
    lineage: dict[str, object], fill_id: str
) -> dict[str, object]:
    evidence = dict(lineage["execution_evidence"])
    evidence["execution_fill_id"] = fill_id
    evidence.pop("execution_evidence_sha256", None)
    evidence["execution_evidence_sha256"] = hashlib.sha256(
        json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return {**lineage, "execution_evidence": evidence}


def _record(session: str, record_type: str, **extra: object) -> dict[str, object]:
    record = {
        "trade_date": TRADE_DATE,
        "session": session,
        "record_type": record_type,
        "real_trading_enabled": False,
        **extra,
    }
    if record_type == "simulated_fill" and extra.get("execution_eligible") is True:
        for key, value in _complete_lineage().items():
            record.setdefault(key, value)
    return record


def test_accepts_every_valid_session_and_separates_fill_classes() -> None:
    records = [
        _record("day_morning", "prediction", symbol="IF2608.CFX"),
        _record(
            "day_morning",
            "simulated_fill",
            execution_eligible=True,
            counterfactual_only=False,
        ),
        _record(
            "day_afternoon",
            "risk_reject",
            execution_eligible=False,
            counterfactual_only=True,
            reason="minimum_one_lot_exceeds_margin_budget",
        ),
        _record("night", "hold", reason="night_session_not_allowed_for_style"),
    ]

    report = evaluate_session_acceptance(
        records,
        trade_date=TRADE_DATE,
        valid_sessions=["day_morning", "day_afternoon", "night"],
        real_trading_enabled=False,
    )

    assert report["status"] == "pass"
    assert report["ready"] is True
    assert report["real_trading_enabled"] is False
    assert report["summary"]["sessions_expected"] == 3
    assert report["summary"]["sessions_accepted"] == 3
    assert report["summary"]["execution_eligible_simulated_fill_count"] == 1
    assert report["summary"]["counterfactual_only_count"] == 1
    assert report["sessions"]["day_afternoon"]["record_type_counts"] == {
        "risk_reject": 1
    }
    assert report["sessions"]["day_morning"]["record_type_counts"] == {
        "prediction": 1,
        "simulated_fill": 1,
    }


def test_fails_when_an_expected_session_has_no_decision_record() -> None:
    report = evaluate_session_acceptance(
        [_record("day_morning", "candidate")],
        trade_date=TRADE_DATE,
        valid_sessions=["day_morning", "day_afternoon"],
    )

    assert report["status"] == "fail"
    assert report["ready"] is False
    assert report["sessions"]["day_afternoon"]["reasons"] == [
        "missing_session_decision_record"
    ]
    assert "missing_session:day_afternoon" in report["failure_reasons"]


def test_sample_insufficiency_alone_is_not_a_valid_hold_reason() -> None:
    report = evaluate_session_acceptance(
        [_record("day_morning", "hold", reason="sample_insufficient")],
        trade_date=TRADE_DATE,
        valid_sessions=["day_morning"],
    )

    assert report["status"] == "fail"
    assert report["sessions"]["day_morning"]["reasons"] == [
        "sample_insufficiency_without_concrete_reason"
    ]


def test_sample_debt_can_coexist_with_a_specific_data_or_safety_reason() -> None:
    report = evaluate_session_acceptance(
        [
            _record(
                "day_morning",
                "risk_reject",
                reasons=["sample_insufficient", "stale_intraday_bar"],
            )
        ],
        trade_date=TRADE_DATE,
        valid_sessions=["day_morning"],
    )

    assert report["status"] == "pass"
    assert report["sessions"]["day_morning"]["concrete_reasons"] == [
        "stale_intraday_bar"
    ]


def test_fails_closed_when_real_trading_is_enabled_anywhere() -> None:
    records = [
        _record("day_morning", "prediction"),
        _record("day_morning", "candidate", real_trading_enabled=True),
    ]

    report = evaluate_session_acceptance(
        records,
        trade_date=TRADE_DATE,
        valid_sessions=["day_morning"],
        real_trading_enabled=False,
    )

    assert report["status"] == "fail"
    assert report["real_trading_enabled"] is False
    assert report["summary"]["real_trading_violation_count"] == 1
    assert "real_trading_enabled_in_runtime_record" in report["failure_reasons"]


def test_fails_when_simulated_fill_has_ambiguous_execution_class() -> None:
    report = evaluate_session_acceptance(
        [
            _record(
                "day_morning",
                "simulated_fill",
                execution_eligible=True,
                counterfactual_only=True,
            )
        ],
        trade_date=TRADE_DATE,
        valid_sessions=["day_morning"],
    )

    assert report["status"] == "fail"
    assert report["sessions"]["day_morning"]["ambiguous_fill_count"] == 1
    assert "ambiguous_simulated_fill_class:day_morning" in report["failure_reasons"]


def test_counterfactual_risk_reject_requires_a_concrete_reason() -> None:
    report = evaluate_session_acceptance(
        [
            _record(
                "day_morning",
                "risk_reject",
                execution_eligible=False,
                counterfactual_only=True,
                reason="sample_insufficient",
            )
        ],
        trade_date=TRADE_DATE,
        valid_sessions=["day_morning"],
    )

    assert report["status"] == "fail"
    assert report["sessions"]["day_morning"]["counterfactual_only_count"] == 1
    assert "counterfactual_without_reason:day_morning" in report["failure_reasons"]


def test_loads_json_and_jsonl_without_mutating_input(tmp_path: Path) -> None:
    rows = [
        _record("day_morning", "prediction"),
        _record("day_afternoon", "hold", reason="insufficient_consecutive_5min_bars"),
    ]
    json_path = tmp_path / "runtime.json"
    jsonl_path = tmp_path / "runtime.jsonl"
    json_path.write_text(json.dumps({"records": rows}), encoding="utf-8")
    jsonl_path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    before_json = json_path.read_bytes()
    before_jsonl = jsonl_path.read_bytes()

    assert load_runtime_records(json_path) == rows
    assert load_runtime_records(jsonl_path) == rows
    assert json_path.read_bytes() == before_json
    assert jsonl_path.read_bytes() == before_jsonl


def test_loads_nested_session_envelope_with_inherited_trade_date(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.json"
    path.write_text(
        json.dumps(
            {
                "trade_date": TRADE_DATE,
                "real_trading_enabled": False,
                "sessions": [
                    {
                        "session": "day_morning",
                        "records": [
                            {
                                "record_type": "hold",
                                "reason": "insufficient_consecutive_5min_bars",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert load_runtime_records(path) == [
        {
            "trade_date": TRADE_DATE,
            "real_trading_enabled": False,
            "session": "day_morning",
            "record_type": "hold",
            "reason": "insufficient_consecutive_5min_bars",
        }
    ]


def test_accepts_native_sim_runner_records_and_holds_shape(tmp_path: Path) -> None:
    path = tmp_path / "cn_futures_run.json"
    path.write_text(
        json.dumps(
            {
                "date": TRADE_DATE,
                "session": "day_morning",
                "real_trading_enabled": False,
                "records": [
                    {
                        **_complete_lineage(),
                        "order": {
                            "symbol": "IF2608.CFX",
                            "capital_layer": "simulated",
                            "account_type": "simulated",
                        },
                        "receipt": {
                            "status": "filled",
                            "filled_qty": 1,
                            "capital_layer": "simulated",
                            "account_type": "simulated",
                            "execution_eligible": True,
                            "execution_class": "execution_eligible",
                            "counterfactual_only": False,
                        },
                    }
                ],
                "holds": [
                    {
                        "stage": "risk",
                        "symbol": "IH2608.CFX",
                        "reason": "minimum_contract_exceeds_risk_budget",
                        "execution_eligible": False,
                        "counterfactual_only": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = evaluate_session_acceptance(
        load_runtime_records(path),
        trade_date=TRADE_DATE,
        valid_sessions=["day_morning"],
    )

    assert report["status"] == "pass"
    assert report["summary"]["record_type_counts"] == {
        "risk_reject": 1,
        "simulated_fill": 1,
    }
    assert report["summary"]["execution_eligible_simulated_fill_count"] == 1
    assert report["summary"]["counterfactual_only_count"] == 1


def test_native_fill_without_explicit_evidence_classification_fails_acceptance() -> (
    None
):
    report = evaluate_session_acceptance(
        [
            _record(
                "day_morning",
                "simulated_fill",
                receipt={
                    "status": "filled",
                    "filled_qty": 1,
                    "capital_layer": "simulated",
                    "account_type": "simulated",
                },
            )
        ],
        trade_date=TRADE_DATE,
        valid_sessions=["day_morning"],
    )

    assert report["status"] == "fail"
    assert report["sessions"]["day_morning"]["ambiguous_fill_count"] == 1
    assert "ambiguous_simulated_fill_class:day_morning" in report["failure_reasons"]


def test_cli_reads_jsonl_and_returns_nonzero_for_missing_session(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.jsonl"
    path.write_text(
        json.dumps(_record("day_morning", "prediction")) + "\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "shared.runtime_test.cn_futures_session_acceptance",
            "--input",
            str(path),
            "--trade-date",
            TRADE_DATE,
            "--sessions",
            "day_morning,day_afternoon",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    report = json.loads(completed.stdout)
    assert report["status"] == "fail"
    assert report["read_only"] is True
    assert report["real_trading_enabled"] is False


def test_cli_fails_closed_when_real_trading_environment_is_enabled(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.json"
    path.write_text(
        json.dumps([_record("day_morning", "prediction")]),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["REAL_TRADING_ENABLED"] = "true"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "shared.runtime_test.cn_futures_session_acceptance",
            "--input",
            str(path),
            "--trade-date",
            TRADE_DATE,
            "--sessions",
            "day_morning",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 2
    report = json.loads(completed.stdout)
    assert report["status"] == "fail"
    assert report["real_trading_enabled"] is True
    assert "real_trading_enabled" in report["failure_reasons"]


def test_consumes_review_jsonl_directly(tmp_path: Path) -> None:
    """The append_review JSONL is directly consumable by session acceptance."""
    from CNFutures.review import append_review

    review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
    records = [
        _record(
            "day_morning",
            "simulated_fill",
            execution_eligible=True,
            counterfactual_only=False,
        ),
        _record(
            "day_afternoon",
            "simulated_fill",
            execution_eligible=True,
            counterfactual_only=False,
        ),
    ]
    holds = [
        _record(
            "night",
            "risk_reject",
            reason="night_not_allowed",
            execution_eligible=False,
            counterfactual_only=True,
        ),
    ]

    append_review(
        date=TRADE_DATE,
        market="cn_futures",
        records=records,
        errors=[],
        holds=holds,
        path=review_path,
    )

    runtime_records = load_runtime_records(review_path, verify_checksums=True)
    report = evaluate_session_acceptance(
        runtime_records,
        trade_date=TRADE_DATE,
        valid_sessions=["day_morning", "day_afternoon", "night"],
        real_trading_enabled=False,
    )

    assert report["status"] == "pass"
    assert report["ready"] is True
    assert report["summary"]["sessions_accepted"] == 3
    assert report["summary"]["execution_eligible_simulated_fill_count"] == 2
    assert report["summary"]["counterfactual_only_count"] == 1


def test_review_jsonl_corrupt_checksum_fails_acceptance_cli(
    tmp_path: Path,
) -> None:
    """Corrupt checksum in review JSONL causes acceptance CLI to fail."""
    from CNFutures.review import append_review

    review_path = tmp_path / "data" / "cn_futures_sim_reviews.jsonl"
    append_review(
        date=TRADE_DATE,
        market="cn_futures",
        records=[
            _record(
                "day_morning",
                "simulated_fill",
                execution_eligible=True,
                counterfactual_only=False,
            )
        ],
        errors=[],
        path=review_path,
    )

    # Tamper with the session row inside session_decisions
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

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "shared.runtime_test.cn_futures_session_acceptance",
            "--input",
            str(review_path),
            "--trade-date",
            TRADE_DATE,
            "--sessions",
            "day_morning",
            "--verify-checksums",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    report = json.loads(completed.stdout)
    assert report["status"] == "fail"
    assert "checksum" in str(report).lower() or "Checksum" in str(report)


# ---------------------------------------------------------------------------
# P0: PIT lineage validation in session acceptance – RED tests
# ---------------------------------------------------------------------------


def test_execution_eligible_fill_without_pit_lineage_rejected() -> None:
    """Simulated fills with explicitly incomplete PIT lineage must be flagged
    as not execution-eligible by the acceptance evaluator."""
    # Fill with PIT lineage explicitly marked incomplete
    records = [
        {
            "trade_date": TRADE_DATE,
            "session": "day_morning",
            "record_type": "simulated_fill",
            "real_trading_enabled": False,
            "execution_eligible": True,
            "counterfactual_only": False,
            "lineage_status": "incomplete",
            "authority": "master_capital_ledger",
            "point_in_time_as_of": "2026-07-13 09:35:00",
            "receipt": {
                "status": "filled",
                "filled_qty": 1,
                "capital_layer": "simulated",
                "account_type": "simulated",
            },
        }
    ]
    report = evaluate_session_acceptance(
        records,
        trade_date=TRADE_DATE,
        valid_sessions=["day_morning"],
        real_trading_enabled=False,
    )
    # PIT lineage incomplete → execution_eligible should be overridden to False
    assert report["summary"]["execution_eligible_simulated_fill_count"] == 0


def test_execution_eligible_fill_with_no_pit_fields_is_rejected() -> None:
    record = {
        "trade_date": TRADE_DATE,
        "session": "day_morning",
        "record_type": "simulated_fill",
        "real_trading_enabled": False,
        "execution_eligible": True,
        "execution_class": "execution_eligible",
        "counterfactual_only": False,
        "receipt": {
            "status": "filled",
            "filled_qty": 1,
            "capital_layer": "simulated",
            "account_type": "simulated",
        },
    }
    report = evaluate_session_acceptance(
        [record],
        trade_date=TRADE_DATE,
        valid_sessions=["day_morning"],
        real_trading_enabled=False,
    )
    assert report["summary"]["execution_eligible_simulated_fill_count"] == 0
    assert report["sessions"]["day_morning"]["lineage_incomplete_fill_count"] == 1
    assert "execution_fill_lineage_incomplete:day_morning" in report["failure_reasons"]


def test_execution_eligible_fill_with_lineage_but_no_execution_evidence_is_rejected() -> (
    None
):
    lineage = _complete_lineage("9")
    lineage.pop("execution_evidence")
    record = {
        "trade_date": TRADE_DATE,
        "session": "day_morning",
        "record_type": "simulated_fill",
        "real_trading_enabled": False,
        "execution_eligible": True,
        "execution_class": "execution_eligible",
        "counterfactual_only": False,
        **lineage,
    }
    report = evaluate_session_acceptance(
        [record], trade_date=TRADE_DATE, valid_sessions=["day_morning"]
    )
    assert report["summary"]["execution_eligible_simulated_fill_count"] == 0
    assert (
        report["sessions"]["day_morning"]["execution_evidence_invalid_fill_count"] == 1
    )
    assert "execution_fill_evidence_invalid:day_morning" in report["failure_reasons"]


def test_execution_eligible_fill_with_complete_pit_lineage_accepted() -> None:
    """Simulated fills with complete PIT lineage must be counted as
    execution-eligible."""
    records = [
        {
            "trade_date": TRADE_DATE,
            "session": "day_morning",
            "record_type": "simulated_fill",
            "real_trading_enabled": False,
            "execution_eligible": True,
            "execution_class": "execution_eligible",
            "counterfactual_only": False,
            **_complete_lineage("b"),
            "receipt": {
                "status": "filled",
                "filled_qty": 1,
                "capital_layer": "simulated",
                "account_type": "simulated",
            },
        }
    ]
    report = evaluate_session_acceptance(
        records,
        trade_date=TRADE_DATE,
        valid_sessions=["day_morning"],
        real_trading_enabled=False,
    )
    assert report["status"] == "pass"
    assert report["summary"]["execution_eligible_simulated_fill_count"] == 1


def test_cluster_duplicate_marker_reduces_weight_in_acceptance() -> None:
    """Cluster duplicate records should still be accepted but must
    carry cluster metadata."""
    records = [
        {
            "trade_date": TRADE_DATE,
            "session": "day_morning",
            "record_type": "simulated_fill",
            "real_trading_enabled": False,
            "execution_eligible": True,
            "execution_class": "execution_eligible",
            "counterfactual_only": False,
            **_complete_lineage("c"),
            "cluster_id": "CLUST-001",
            "cluster_role": "duplicate",
            "occurrence_index": 1,
            "receipt": {
                "status": "filled",
                "filled_qty": 1,
                "capital_layer": "simulated",
                "account_type": "simulated",
            },
        }
    ]
    report = evaluate_session_acceptance(
        records,
        trade_date=TRADE_DATE,
        valid_sessions=["day_morning"],
        real_trading_enabled=False,
    )
    assert report["status"] == "pass"


def test_cluster_occurrence_is_persistent_across_independent_appends(
    tmp_path: Path,
) -> None:
    from CNFutures.review import append_review, load_review_rows

    path = tmp_path / "cn_reviews.jsonl"
    record = _record(
        "day_morning",
        "prediction",
        symbol="RB2610.SHF",
        style="trend",
        cluster_id="CNF-CLUST-PERSIST-1",
        source_snapshot_id="CNF-SNAP-PREDICT-1",
    )
    append_review(
        date=TRADE_DATE,
        market="cn_futures",
        records=[record],
        errors=[],
        path=path,
    )
    append_review(
        date=TRADE_DATE,
        market="cn_futures",
        records=[record],
        errors=[],
        path=path,
    )

    rows = [
        row
        for row in load_review_rows(path, include_summaries=False)
        if row.get("cluster_id") == "CNF-CLUST-PERSIST-1"
    ]
    assert [
        (row["cluster_role"], row["occurrence_index"], row["weight_multiplier"])
        for row in rows
    ] == [
        ("origin", 0, 1.0),
        ("duplicate", 1, 0.0),
    ]


def test_distinct_partial_fill_facts_in_one_cluster_are_both_retained(
    tmp_path: Path,
) -> None:
    from CNFutures.review import append_review, load_review_rows

    path = tmp_path / "cn_partial_reviews.jsonl"
    first_lineage = _with_execution_fill_id(_complete_lineage("4"), "CNF-FILL-1")
    second_lineage = _with_execution_fill_id(_complete_lineage("5"), "CNF-FILL-2")
    common = {
        "symbol": "RB2610.SHF",
        "style": "trend",
        "cluster_id": "CNF-CLUST-PARTIAL-1",
        "execution_eligible": True,
        "execution_class": "execution_eligible",
        "counterfactual_only": False,
    }
    append_review(
        date=TRADE_DATE,
        market="cn_futures",
        records=[_record("day_morning", "simulated_fill", **common, **first_lineage)],
        errors=[],
        path=path,
    )
    append_review(
        date=TRADE_DATE,
        market="cn_futures",
        records=[_record("day_morning", "simulated_fill", **common, **second_lineage)],
        errors=[],
        path=path,
    )

    rows = [
        row
        for row in load_review_rows(path, include_summaries=False)
        if row.get("cluster_id") == "CNF-CLUST-PARTIAL-1"
    ]
    assert len(rows) == 2
    assert {row["execution_evidence"]["execution_fill_id"] for row in rows} == {
        "CNF-FILL-1",
        "CNF-FILL-2",
    }
    assert [row["weight_multiplier"] for row in rows] == [1.0, 0.0]
