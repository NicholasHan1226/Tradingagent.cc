from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from shared.portfolio.small_account_optimizer import (
    AccountAuthoritySnapshot,
    AccountAuthorityVerification,
)
from tools import run_phase1_paper_fixture as fixture_cli


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "tools" / "run_phase1_paper_fixture.py"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "phase1_paper" / "paper_day.json"
FIXTURE_AUTHORITY_ID = "ashare-phase1-offline-fixture-capital-v1"
FIXTURE_EXECUTION_LINEAGE = "ashare-phase1-offline-fixture-lineage-v1"


def _fixture_runtime_root(output_root: Path) -> Path:
    return output_root / "shared" / "runtime_test" / "phase1_paper_fixture"


def _fixture_journal(output_root: Path) -> Path:
    return _fixture_runtime_root(output_root) / "ashare" / "sample_journal.jsonl"


def _file_state(path: Path) -> tuple[bool, bytes | None]:
    return path.exists(), path.read_bytes() if path.exists() else None


def _run_cli(
    output_root: Path,
    *,
    fixture: Path = FIXTURE,
    env: dict[str, str] | None = None,
):
    return subprocess.run(
        [
            sys.executable,
            str(CLI),
            "--fixture",
            str(fixture),
            "--output-root",
            str(output_root),
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _refresh_empty_fixture_account_proof(raw: dict[str, object]) -> None:
    optimizer = raw["small_account_optimizer"]
    assert isinstance(optimizer, dict)
    account = optimizer["account_snapshot"]
    assert isinstance(account, dict)
    assert account["positions"] == []
    snapshot = AccountAuthoritySnapshot(
        capital_authority_id=account["capital_authority_id"],
        authority_generation=account["authority_generation"],
        account_as_of=datetime.fromisoformat(account["account_as_of"]),
        available_cash_cny=account["available_cash_cny"],
        current_gross_cny=account["current_gross_cny"],
        positions=(),
        position_snapshot_receipt_id=account["position_snapshot_receipt_id"],
        position_snapshot_sha256=account["position_snapshot_sha256"],
        verification_receipt_sha256=account["verification_receipt_sha256"],
        authority_source_class=account["authority_source_class"],
    )
    proof = AccountAuthorityVerification.create(
        snapshot=snapshot,
        verifier_id="phase1-paper-fixture-account-authority",
        verifier_version="1",
        verified_at=snapshot.account_as_of,
        valid_until=datetime.fromisoformat(optimizer["decision_time"]),
        promotion_eligible=False,
    )
    account["verification_receipt_sha256"] = proof.verification_receipt_sha256


def test_cli_runs_complete_offline_fixture_to_validated_report(
    tmp_path: Path,
) -> None:
    result = _run_cli(tmp_path / "paper-run")

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary == {
        "artifact_path": summary["artifact_path"],
        "authority": "non_authority",
        "bundle_sha256": summary["bundle_sha256"],
        "environment": "local_candidate",
        "idempotent": False,
        "immutable_artifact_path": summary["immutable_artifact_path"],
        "mode": "offline_fixture",
        "production_verified": False,
        "real_trading_enabled": False,
        "run_id": summary["run_id"],
        "status": "completed",
        "transport_calls": [
            {"dataset_id": None, "method": "GET", "path": "/v1/catalog"},
            {
                "dataset_id": "fixture.cn.equity.daily.mainboard.v1",
                "method": "POST",
                "path": "/v1/query",
            },
            {
                "dataset_id": "fixture.cn.equity.sector.full-market-context.v1",
                "method": "POST",
                "path": "/v1/query",
            },
        ],
    }
    assert len(summary["run_id"]) > 32
    assert len(summary["bundle_sha256"]) == 64

    artifact = Path(summary["artifact_path"])
    immutable_artifact = Path(summary["immutable_artifact_path"])
    assert artifact.is_absolute()
    assert artifact.read_bytes() == immutable_artifact.read_bytes()
    projection = json.loads(artifact.read_text(encoding="utf-8"))
    assert projection["run_id"] == summary["run_id"]
    assert projection["status"] == "completed"
    assert projection["stage_receipts"][-1]["stage"] == "reported"
    assert projection["_projection"] == {
        "authority": "non_authority",
        "bundle_sha256": summary["bundle_sha256"],
        "environment": "local_candidate",
        "production_verified": False,
        "record_type": "run_bundle_projection",
        "schema_version": 1,
    }

    assert artifact == (
        _fixture_runtime_root(tmp_path / "paper-run") / "run_bundles" / "latest.json"
    )
    journal = _fixture_journal(tmp_path / "paper-run")
    assert journal.is_file()
    assert not (tmp_path / "paper-run" / "shared" / "review").exists()
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [event["journal_event_type"] for event in events] == [
        "prediction_snapshot",
        "sample_event",
        "sample_event",
        "sample_event",
        "sample_event",
    ]
    assert all(event["source_class"] == "fixture" for event in events)
    assert all(event["promotion_eligible"] is False for event in events)
    fill_event = next(event for event in events if event["record_type"] == "fill")
    prediction_event = next(
        event
        for event in events
        if event["journal_event_type"] == "prediction_snapshot"
    )
    disposition_event = next(
        event
        for event in events
        if event.get("audit_event_type") == "decision_exposure_disposition"
    )
    assert disposition_event["disposition"] == "paper_filled"
    assert prediction_event["source_class"] == "fixture"
    assert prediction_event["promotion_eligible"] is False
    assert prediction_event["capital_authority_id"] == FIXTURE_AUTHORITY_ID
    assert prediction_event["execution_lineage_id"] == FIXTURE_EXECUTION_LINEAGE
    assert fill_event["sample_layer"] == "exploitation_fill"
    assert fill_event["source_class"] == "fixture"
    assert fill_event["promotion_eligible"] is False
    assert fill_event["capital_authority_id"] == FIXTURE_AUTHORITY_ID
    assert fill_event["execution_lineage_id"] == FIXTURE_EXECUTION_LINEAGE
    assert fill_event["filled_quantity"] == 200
    assert fill_event["execution_eligible"] is True
    assert len(fill_event["receipt_sha256"]) == 64
    assert events[-1]["sample_layer"] == "chain_validation"

    assert projection["stage_receipts"][-2]["payload"]["canonical_outcome_count"] == 1
    reconciled = next(
        receipt["payload"]
        for receipt in projection["stage_receipts"]
        if receipt["stage"] == "reconciled"
    )
    assert reconciled["cash_cny"] == 47_995.0
    assert reconciled["account_equity_cny"] == 49_995.0
    order_receipt = next(
        receipt["payload"]["order_receipts"][0]
        for receipt in projection["stage_receipts"]
        if receipt["stage"] == "orders_simulated"
    )
    assert order_receipt["capital_commit_status"] == "committed"
    assert order_receipt["status"] == "filled"
    decision = next(
        receipt["payload"]
        for receipt in projection["stage_receipts"]
        if receipt["stage"] == "decision_ready"
    )
    optimizer_row = decision["small_account_plan"]["plan_decisions"][0]
    assert decision["small_account_plan"]["starting_available_cash_cny"] == 50_000.0
    assert optimizer_row["order_quantity"] == 200
    assert optimizer_row["order_quantity"] % 100 == 0
    assert optimizer_row["estimated_order_cost_cny"] == pytest.approx(5.02007)
    assert decision["small_account_plan"]["cash_after_orders_cny"] == pytest.approx(
        47_987.97993
    )
    risk = next(
        receipt["payload"]
        for receipt in projection["stage_receipts"]
        if receipt["stage"] == "risk_checked"
    )
    assert risk["drift_constraint"] == {
        "active_action_receipt_sha256": None,
        "quarantined": False,
        "reason_codes": [],
        "reduce_only": False,
        "review_required": False,
        "risk_multiplier_cap": 1.0,
        "schema_version": "tradingagent.drift_runtime_constraint.v1",
        "stop_new_orders": False,
    }
    assert len(risk["drift_constraint_sha256"]) == 64

    evidence = next(
        receipt["payload"]
        for receipt in projection["stage_receipts"]
        if receipt["stage"] == "evidence_ready"
    )
    assert evidence["historical_pit_eligible"] is False
    datasets = {item["dataset_id"]: item for item in evidence["datasets"]}
    daily = datasets["fixture.cn.equity.daily.mainboard.v1"]
    assert daily["observation_mode"] == "current_observation"
    assert daily["historical_pit_eligible"] is False
    assert daily["identity_fields"] == ["ts_code", "trade_date"]
    assert daily["row_event_time_field"] == "trade_date"
    assert daily["row_event_time_format"] == "yyyymmdd"
    assert daily["row_event_time_semantic"] == "session"
    assert daily["row_event_timezone"] == "Asia/Shanghai"
    assert daily["minimum_row_count"] == 1
    assert daily["max_pages"] == 2
    assert daily["max_rows"] == 1000
    assert daily["source_proof_complete"] is True
    assert len(daily["source_proof_sha256"]) == 64
    assert len(daily["row_observation_sha256"]) == 64


def test_fixture_uses_provider_native_current_observation_dataset_contract() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert raw["schema_version"] == 2
    assert ":8082" not in raw["config"]["tradingdatas_v1_base_url"]
    decision_as_of = datetime.fromisoformat(raw["config"]["decision_as_of"])
    dataset_configs = {
        item["dataset_id"]: item for item in raw["config"]["datasets"]
    }
    parsed = fixture_cli._parse_fixture(fixture_cli._load_fixture(FIXTURE))
    requirements = {
        item.dataset_id: item for item in parsed.config.dataset_profile.requirements
    }

    forbidden_row_evidence = {
        "event_time",
        "available_time",
        "revision_id",
        "receipt_id",
    }
    for dataset_id, dataset_config in dataset_configs.items():
        assert "as_of" not in dataset_config
        assert dataset_config["observation_mode"] == "current_observation"
        assert dataset_config["query_as_of_mode"] == "decision_as_of"
        requirement = requirements[dataset_id]
        assert requirement.identity_fields == tuple(dataset_config["identity_fields"])
        assert requirement.minimum_row_count == dataset_config["minimum_row_count"]
        assert requirement.max_pages == dataset_config["max_pages"]
        assert requirement.max_rows == dataset_config["max_rows"]
        assert datetime.fromisoformat(parsed.requests[dataset_id].as_of) == (
            decision_as_of
        )

        response = raw["transport_responses"]["queries"][dataset_id]["json_body"]
        assert all(
            not forbidden_row_evidence.intersection(row) for row in response["data"]
        )
        metadata = response["metadata"]
        assert metadata["receipt_id"]
        assert metadata["data_through"]
        assert metadata["observed_at"]
        assert metadata["lineage"]["complete"] is True
        assert metadata["lineage"]["provider_neutral"] is True


def test_cli_rejects_provider_native_row_missing_declared_identity(
    tmp_path: Path,
) -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    daily = raw["transport_responses"]["queries"][
        "fixture.cn.equity.daily.mainboard.v1"
    ]["json_body"]
    del daily["data"][0]["trade_date"]
    invalid_fixture = tmp_path / "missing-provider-identity.json"
    invalid_fixture.write_text(json.dumps(raw), encoding="utf-8")

    result = _run_cli(tmp_path / "paper-run", fixture=invalid_fixture)

    assert result.returncode == 2
    failure = json.loads(result.stderr)
    assert failure["status"] == "failed"
    assert failure["error"] == "pagination_row_identity_missing"
    assert failure["real_trading_enabled"] is False


def test_cli_repeat_is_byte_stable_and_does_not_replay_fixture_transport(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "paper-run"
    first = _run_cli(output_root)
    assert first.returncode == 0, first.stderr
    first_summary = json.loads(first.stdout)
    first_artifact = Path(first_summary["artifact_path"]).read_bytes()
    first_journal = _fixture_journal(output_root).read_bytes()

    second = _run_cli(output_root)

    assert second.returncode == 0, second.stderr
    second_summary = json.loads(second.stdout)
    assert second_summary["run_id"] == first_summary["run_id"]
    assert second_summary["bundle_sha256"] == first_summary["bundle_sha256"]
    assert second_summary["idempotent"] is True
    assert second_summary["transport_calls"] == []
    assert Path(second_summary["artifact_path"]).read_bytes() == first_artifact
    assert _fixture_journal(output_root).read_bytes() == first_journal
    assert not (output_root / "shared" / "review").exists()


def test_cli_business_bundle_is_output_root_independent_while_returned_paths_remain_actionable(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first-root"
    second_root = tmp_path / "second-root"

    first = _run_cli(first_root)
    second = _run_cli(second_root)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_summary = json.loads(first.stdout)
    second_summary = json.loads(second.stdout)
    assert first_summary["run_id"] == second_summary["run_id"]
    assert first_summary["bundle_sha256"] == second_summary["bundle_sha256"]

    first_artifact = Path(first_summary["artifact_path"])
    second_artifact = Path(second_summary["artifact_path"])
    assert first_artifact.is_absolute()
    assert second_artifact.is_absolute()
    assert first_artifact != second_artifact
    assert first_artifact.is_file()
    assert second_artifact.is_file()
    assert first_artifact.read_bytes() == second_artifact.read_bytes()

    projection = json.loads(first_artifact.read_text(encoding="utf-8"))
    learning = next(
        receipt["payload"]
        for receipt in projection["stage_receipts"]
        if receipt["stage"] == "learning_recorded"
    )
    reported = next(
        receipt["payload"]
        for receipt in projection["stage_receipts"]
        if receipt["stage"] == "reported"
    )
    assert "journal_path" not in learning["canonical_outcome_report"]
    assert Path(reported["artifact_path"]).is_absolute() is False
    assert Path(reported["latest_path"]).is_absolute() is False


@pytest.mark.parametrize(
    "unsafe_root",
    (
        REPO_ROOT,
        REPO_ROOT / "fixture-output-must-never-exist",
        REPO_ROOT / "shared" / "review",
        REPO_ROOT / "shared" / "review" / "ashare",
    ),
)
def test_output_root_validator_rejects_project_and_formal_review_paths(
    unsafe_root: Path,
) -> None:
    validator = getattr(fixture_cli, "_validated_fixture_output_root", None)
    assert callable(validator), "fixture output-root validator is missing"

    with pytest.raises(
        fixture_cli.FixtureCLIError,
        match="fixture_output_root_protected",
    ):
        validator(unsafe_root)


def test_output_root_validator_rejects_symlink_alias_into_project(
    tmp_path: Path,
) -> None:
    alias = tmp_path / "ta-project-alias"
    alias.symlink_to(REPO_ROOT, target_is_directory=True)
    validator = getattr(fixture_cli, "_validated_fixture_output_root", None)
    assert callable(validator), "fixture output-root validator is missing"

    with pytest.raises(
        fixture_cli.FixtureCLIError,
        match="fixture_output_root_protected",
    ):
        validator(alias / "shared" / "review" / "ashare")


def test_cli_rejects_project_root_with_structured_error_and_preserves_journal() -> None:
    canonical_journal = (
        REPO_ROOT / "shared" / "review" / "ashare" / "sample_journal.jsonl"
    )
    before = _file_state(canonical_journal)

    result = _run_cli(REPO_ROOT)

    assert result.returncode == 2
    failure = json.loads(result.stderr)
    assert failure["status"] == "failed"
    assert failure["error"] == "fixture_output_root_protected"
    assert failure["real_trading_enabled"] is False
    assert _file_state(canonical_journal) == before


def test_fixture_authority_and_lineage_are_not_production_authority_values() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    encoded = json.dumps(raw, sort_keys=True)
    optimizer = raw["small_account_optimizer"]
    account = optimizer["account_snapshot"]

    assert raw["config"]["capital_authority_id"] == FIXTURE_AUTHORITY_ID
    assert raw["config"]["execution_lineage"] == FIXTURE_EXECUTION_LINEAGE
    assert account["capital_authority_id"] == FIXTURE_AUTHORITY_ID
    assert account["authority_source_class"] == "offline_fixture"
    assert optimizer["runtime_environment"] == "local_candidate"
    assert optimizer["promotion_eligible"] is False
    assert '"capital_authority_id": "ashare-capital-v1"' not in encoded
    assert '"execution_lineage": "ashare-sim-fresh-20260712-v1"' not in encoded


@pytest.mark.parametrize(
    ("authority_id", "source_class"),
    (
        (FIXTURE_AUTHORITY_ID, "canonical_authority"),
        ("ashare-capital-v1", "offline_fixture"),
    ),
)
def test_cli_rejects_mixed_authority_id_and_source_class_before_writing(
    tmp_path: Path,
    authority_id: str,
    source_class: str,
) -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["config"]["capital_authority_id"] = authority_id
    account = raw["small_account_optimizer"]["account_snapshot"]
    account["capital_authority_id"] = authority_id
    account["authority_source_class"] = source_class
    invalid_fixture = tmp_path / "mixed-authority.json"
    invalid_fixture.write_text(json.dumps(raw), encoding="utf-8")
    output_root = tmp_path / "must-not-exist"

    result = _run_cli(output_root, fixture=invalid_fixture)

    assert result.returncode == 2
    failure = json.loads(result.stderr)
    assert failure["error"] == "capital_authority_id_mismatch"
    assert failure["real_trading_enabled"] is False
    assert not output_root.exists()


def test_cli_rejects_optimizer_promotion_eligibility_before_writing(
    tmp_path: Path,
) -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["small_account_optimizer"]["promotion_eligible"] = True
    invalid_fixture = tmp_path / "promotion-enabled.json"
    invalid_fixture.write_text(json.dumps(raw), encoding="utf-8")
    output_root = tmp_path / "must-not-exist"

    result = _run_cli(output_root, fixture=invalid_fixture)

    assert result.returncode == 2
    failure = json.loads(result.stderr)
    assert failure["error"] == (
        "small_account_optimizer.promotion_eligible_must_be_native_false"
    )
    assert failure["real_trading_enabled"] is False
    assert not output_root.exists()


def test_cli_accepts_matching_rotated_authority_generation(
    tmp_path: Path,
) -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["config"]["authority_generation"] = 2
    raw["small_account_optimizer"]["account_snapshot"]["authority_generation"] = 2
    _refresh_empty_fixture_account_proof(raw)
    rotated_fixture = tmp_path / "rotated-authority.json"
    rotated_fixture.write_text(json.dumps(raw), encoding="utf-8")

    result = _run_cli(tmp_path / "paper-run", fixture=rotated_fixture)

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    projection = json.loads(Path(summary["artifact_path"]).read_text(encoding="utf-8"))
    decision = next(
        receipt["payload"]
        for receipt in projection["stage_receipts"]
        if receipt["stage"] == "decision_ready"
    )
    assert decision["small_account_plan"]["authority_generation"] == 2


def test_cli_rejects_noncanonical_scope_identity_before_writing(
    tmp_path: Path,
) -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["scope_policy_identity"]["artifact_sha256"] = "f" * 64
    invalid_fixture = tmp_path / "forged-scope-policy.json"
    invalid_fixture.write_text(json.dumps(raw), encoding="utf-8")
    output_root = tmp_path / "must-not-exist"

    result = _run_cli(output_root, fixture=invalid_fixture)

    assert result.returncode == 2
    failure = json.loads(result.stderr)
    assert failure["error"] == "scope_policy_identity_not_canonical"
    assert failure["real_trading_enabled"] is False
    assert not output_root.exists()


def test_cli_rejects_a_fixture_not_marked_offline_before_writing(
    tmp_path: Path,
) -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["offline_fixture"] = False
    invalid_fixture = tmp_path / "not-offline.json"
    invalid_fixture.write_text(json.dumps(raw), encoding="utf-8")
    output_root = tmp_path / "must-not-exist"

    result = _run_cli(output_root, fixture=invalid_fixture)

    assert result.returncode == 2
    failure = json.loads(result.stderr)
    assert failure["status"] == "failed"
    assert failure["error"] == "offline_fixture_must_be_native_true"
    assert failure["real_trading_enabled"] is False
    assert not output_root.exists()


def test_runtime_contract_failure_remains_a_structured_cli_error(
    tmp_path: Path,
) -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    del raw["business_stage_payloads"]["decision_ready"]["candidate_set_receipt"]
    invalid_fixture = tmp_path / "missing-candidate-set.json"
    invalid_fixture.write_text(json.dumps(raw), encoding="utf-8")

    result = _run_cli(tmp_path / "paper-run", fixture=invalid_fixture)

    assert result.returncode == 2
    failure = json.loads(result.stderr)
    assert failure["status"] == "failed"
    assert failure["error"] == "candidate_set_receipt_missing"
    assert failure["real_trading_enabled"] is False


def test_cli_rejects_real_trading_environment_before_reading_fixture(
    tmp_path: Path,
) -> None:
    env = os.environ.copy()
    env["REAL_TRADING_ENABLED"] = "true"
    output_root = tmp_path / "must-not-exist"

    result = _run_cli(output_root, env=env)

    assert result.returncode == 2
    failure = json.loads(result.stderr)
    assert failure["status"] == "failed"
    assert failure["error"] == "REAL_TRADING_ENABLED_environment_must_be_false"
    assert failure["real_trading_enabled"] is False
    assert not output_root.exists()


def test_cli_reports_unsafe_symlink_output_root_as_structured_failure(
    tmp_path: Path,
) -> None:
    actual_root = tmp_path / "actual-root"
    actual_root.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(actual_root, target_is_directory=True)

    result = _run_cli(linked_root / "paper-run")

    assert result.returncode == 2
    failure = json.loads(result.stderr)
    assert failure["status"] == "failed"
    assert failure["error"] == "research_snapshot_store_symlink_forbidden"
    assert failure["real_trading_enabled"] is False
    assert not (actual_root / "paper-run").exists()


def test_cli_source_has_no_network_broker_or_legacy_data_fallback() -> None:
    source = CLI.read_text(encoding="utf-8").lower()

    for forbidden in (
        "import requests",
        "urllib.request",
        "http.client",
        "import socket",
        "sqlite",
        "tushare",
        "shared.data.reader",
        "/legacy",
    ):
        assert forbidden not in source
