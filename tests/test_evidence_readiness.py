from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from shared.governance.evidence_readiness import (
    dataset_contract_fingerprint,
    dataset_contract_material,
    load_evidence_readiness_contract,
)


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "shared" / "governance" / "evidence_readiness.yaml"


def _write_mutation(tmp_path: Path, mutate) -> Path:
    payload = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    mutate(payload)
    target = tmp_path / "evidence_readiness.yaml"
    target.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return target


def _observation_proofs() -> dict[str, bool]:
    return {
        "api_envelope_bound": True,
        "dataset_contract_bound": True,
        "identity_valid": True,
        "receipt_bound": True,
        "lineage_complete": True,
        "quality_valid": True,
    }


def _catalog_row() -> dict[str, object]:
    return {
        "dataset_id": "cn.equity.daily",
        "schema_major": 2,
        "default_fields": ["ts_code", "trade_date", "close"],
        "filter_operators": {
            "trade_date": ["between", "eq"],
            "ts_code": ["in", "eq"],
        },
        "default_order": ["ts_code:asc", "trade_date:asc"],
        "limits": {"max_page_size": 500, "max_lookback_days": 36500},
        "identity_fields": ["ts_code", "trade_date"],
        "state": "ready",
        "degraded": False,
        "runtime_state": "success",
    }


def test_tracked_contract_separates_roles_freshness_and_maturity() -> None:
    contract = load_evidence_readiness_contract()

    assert tuple(role.role_id for role in contract.roles) == (
        "observation_ready",
        "historical_pit_ready",
        "delayed_paper_ready",
        "execution_ready",
    )
    assert contract.freshness("execution_equivalent").maximum_lag_seconds == 30
    assert contract.freshness("delayed_observation").maximum_bar_cadence_multiple == 1
    assert contract.freshness("historical_pit").wall_clock_freshness_required is False
    assert (
        contract.contract_binding.unrelated_catalog_change_blocks_consumption is False
    )
    assert contract.replay_policy.routine_cycle == "receipt_bound_single_traversal"
    assert (
        contract.market_policies["ashare"]["operational_maturity"][
            "expected_session_slots"
        ]
        == 48
    )
    assert (
        contract.market_policies["ashare"]["cohort_shadow"]["minimum_coverage_ratio"]
        == 0.99
    )
    assert (
        contract.market_policies["crypto"]["operational_maturity"][
            "minimum_continuous_slots"
        ]
        == 288
    )


def test_observation_does_not_grant_learning_paper_or_execution() -> None:
    contract = load_evidence_readiness_contract()
    result = contract.assess(_observation_proofs())

    assert result.granted_roles == ("observation_ready",)
    assert result.grants("historical_pit_ready") is False
    assert result.grants("delayed_paper_ready") is False
    assert result.grants("execution_ready") is False


def test_historical_and_delayed_paper_are_independent_grants() -> None:
    contract = load_evidence_readiness_contract()
    historical = contract.assess(
        {
            **_observation_proofs(),
            "as_of_bounded": True,
            "first_available_time_proven": True,
            "revision_or_immutable_vintage_proven": True,
            "no_future_information": True,
            "label_window_complete": True,
        }
    )
    assert historical.grants("historical_pit_ready") is True
    assert historical.grants("delayed_paper_ready") is False

    paper = contract.assess(
        {
            **_observation_proofs(),
            "completed_market_event": True,
            "delayed_freshness_policy_satisfied": True,
            "next_event_execution_only": True,
            "capital_authority_bound": True,
            "idempotency_and_reconcile_valid": True,
        }
    )
    assert paper.grants("historical_pit_ready") is False
    assert paper.grants("delayed_paper_ready") is True
    assert paper.grants("execution_ready") is False


def test_execution_cannot_be_enabled_by_proofs_in_current_phase() -> None:
    contract = load_evidence_readiness_contract()
    proofs = {
        requirement: True for role in contract.roles for requirement in role.requires
    }
    result = contract.assess(proofs)

    assert result.grants("delayed_paper_ready") is True
    assert result.grants("execution_ready") is False
    assert result.blocked_reasons["execution_ready"][0] == (
        "role_disabled_in_current_phase"
    )


def test_contract_rejects_execution_enablement(tmp_path: Path) -> None:
    path = _write_mutation(
        tmp_path,
        lambda payload: payload["roles"]["execution_ready"].__setitem__(
            "enabled_in_current_phase", True
        ),
    )
    with pytest.raises(ValueError, match="execution_ready must remain disabled"):
        load_evidence_readiness_contract(path)


def test_contract_rejects_global_catalog_pin_and_same_bar_execution(
    tmp_path: Path,
) -> None:
    def mutate(payload: dict) -> None:
        payload["contract_binding"]["unrelated_catalog_change_blocks_consumption"] = (
            True
        )
        payload["freshness_policies"]["delayed_observation"][
            "same_event_execution_allowed"
        ] = True

    path = _write_mutation(tmp_path, mutate)
    with pytest.raises(ValueError, match="freshness policy safety invariants"):
        load_evidence_readiness_contract(path)


def test_contract_rejects_partial_shadow_notional_or_silent_replacement(
    tmp_path: Path,
) -> None:
    def mutate(payload: dict) -> None:
        shadow = payload["market_policies"]["ashare"]["cohort_shadow"]
        shadow["simulated_notional_allowed"] = True
        shadow["silent_replacement_allowed"] = True

    path = _write_mutation(tmp_path, mutate)
    with pytest.raises(ValueError, match="partial cohort must remain zero-notional"):
        load_evidence_readiness_contract(path)


def test_dataset_fingerprint_ignores_runtime_metadata_and_operator_order() -> None:
    row = _catalog_row()
    changed_runtime = {
        **row,
        "state": "stale",
        "degraded": True,
        "runtime_state": "failed",
        "catalog_version": "unrelated-global-version",
        "filter_operators": {
            "ts_code": ["eq", "in"],
            "trade_date": ["eq", "between"],
        },
    }

    assert dataset_contract_fingerprint(row) == dataset_contract_fingerprint(
        changed_runtime
    )
    assert dataset_contract_material(row)["dataset_id"] == "cn.equity.daily"


def test_dataset_fingerprint_golden_vector_is_cross_repository_stable() -> None:
    assert dataset_contract_fingerprint(_catalog_row()) == (
        "2a64eade6402119d492ae339213af96865ad5125358ac45de576b5a71f1d9e07"
    )


@pytest.mark.parametrize(
    "field, value",
    [
        ("dataset_id", "cn.equity.daily.v2"),
        ("schema_major", 3),
        ("default_fields", ["ts_code", "trade_date", "open", "close"]),
        ("filter_operators", {"trade_date": ["eq"]}),
        ("default_order", ["trade_date:asc", "ts_code:asc"]),
        ("limits", {"max_page_size": 100, "max_lookback_days": 36500}),
        ("identity_fields", ["trade_date", "ts_code"]),
    ],
)
def test_each_dataset_contract_field_changes_fingerprint(
    field: str, value: object
) -> None:
    row = _catalog_row()
    changed = {**row, field: value}

    assert dataset_contract_fingerprint(row) != dataset_contract_fingerprint(changed)


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (
            lambda row: row.update({"identity_fields": ["missing"]}),
            "identity_fields must be default fields",
        ),
        (
            lambda row: row.update({"filter_operators": {"missing": ["eq"]}}),
            "filter_operators field must be a default field",
        ),
        (
            lambda row: row.update({"limits": {"max_page_size": 0}}),
            "limits values must be positive integers",
        ),
    ],
)
def test_dataset_fingerprint_rejects_malformed_contract(mutate, reason: str) -> None:
    row = _catalog_row()
    mutate(row)

    with pytest.raises(ValueError, match=reason):
        dataset_contract_fingerprint(row)
