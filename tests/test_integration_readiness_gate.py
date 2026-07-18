from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest

from shared.runtime_test.integration_readiness_gate import (
    IntegrationReadinessError,
    VerifiedIntegrationReadiness,
    assert_readiness_matches_runtime,
    load_and_verify_integration_receipt,
)
from shared.runtime_test.integration_readiness_profile import (
    readiness_expectation_from_probe_config,
)
from shared.runtime_test.sharedsignals_v1_integration_probe import (
    run_sharedsignals_integration_probe,
    write_probe_receipt,
)
from tests.test_sharedsignals_v1_integration_probe import (
    DoubleRunTransport,
    _load_config,
    _receipt_sha256,
)


def _verified(tmp_path: Path):
    config = _load_config(tmp_path)
    receipt = run_sharedsignals_integration_probe(
        config,
        transport=DoubleRunTransport(),
    )
    path = (tmp_path / "readiness.json").resolve()
    write_probe_receipt(path, receipt)
    expectation = readiness_expectation_from_probe_config(config)
    return (
        config,
        expectation,
        path,
        load_and_verify_integration_receipt(path, expectation=expectation),
    )


def test_pass_receipt_becomes_only_a_process_local_non_authority_capability(
    tmp_path: Path,
) -> None:
    config, expectation, path, verified = _verified(tmp_path)

    assert type(verified) is VerifiedIntegrationReadiness
    assert verified.expectation_sha256 == expectation.expectation_sha256
    assert verified.receipt_sha256 == json.loads(path.read_text())["receipt_sha256"]
    assert dict(verified.binding_payload()) == {
        "authority": "non_authority",
        "verification_scope": "local_content_integrity_and_config_compatibility",
        "expectation_sha256": expectation.expectation_sha256,
        "receipt_sha256": verified.receipt_sha256,
        "semantic_snapshot_sha256": verified.semantic_snapshot_sha256,
    }
    assert config.as_of == expectation.as_of


def test_directly_constructed_or_forged_capability_is_rejected(tmp_path: Path) -> None:
    _, expectation, _, verified = _verified(tmp_path)

    forged = VerifiedIntegrationReadiness(
        expectation=expectation,
        receipt_sha256=verified.receipt_sha256,
        semantic_snapshot_sha256=verified.semantic_snapshot_sha256,
        attestation="0" * 64,
    )
    with pytest.raises(IntegrationReadinessError, match="attestation_invalid"):
        forged.binding_payload()


def test_check_is_read_only_and_creates_no_runtime_or_capital_artifact(
    tmp_path: Path,
) -> None:
    config = _load_config(tmp_path)
    receipt = run_sharedsignals_integration_probe(
        config,
        transport=DoubleRunTransport(),
    )
    path = (tmp_path / "readiness.json").resolve()
    write_probe_receipt(path, receipt)
    before = {
        item.name: item.read_bytes() for item in tmp_path.iterdir() if item.is_file()
    }

    load_and_verify_integration_receipt(
        path,
        expectation=readiness_expectation_from_probe_config(config),
    )

    after = {
        item.name: item.read_bytes() for item in tmp_path.iterdir() if item.is_file()
    }
    assert after == before


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("status", "fail", "integration_status_mismatch"),
        ("blocking", True, "blocking"),
        ("production_verified", True, "production_verified"),
        ("real_trading_enabled", True, "real_trading_enabled"),
        ("same_as_of_match", False, "same_as_of_match"),
    ],
)
def test_resigned_status_or_authority_mutation_still_fails_closed(
    tmp_path: Path,
    field: str,
    value: object,
    reason: str,
) -> None:
    config = _load_config(tmp_path)
    receipt = run_sharedsignals_integration_probe(
        config,
        transport=DoubleRunTransport(),
    )
    receipt[field] = value
    receipt["receipt_sha256"] = _receipt_sha256(receipt)
    path = (tmp_path / f"mutated-{field}.json").resolve()
    path.write_text(json.dumps(receipt), encoding="utf-8")
    os.chmod(path, 0o600)

    with pytest.raises(IntegrationReadinessError, match=reason):
        load_and_verify_integration_receipt(
            path,
            expectation=readiness_expectation_from_probe_config(config),
        )


def test_dataset_query_swap_unknown_key_and_incomplete_proof_are_rejected(
    tmp_path: Path,
) -> None:
    config = _load_config(tmp_path)
    baseline = run_sharedsignals_integration_probe(
        config,
        transport=DoubleRunTransport(),
    )
    expectation = readiness_expectation_from_probe_config(config)

    mutations = []
    swapped = deepcopy(baseline)
    swapped["datasets"].reverse()
    mutations.append(swapped)
    unknown = deepcopy(baseline)
    unknown["datasets"][0]["provider"] = "must-not-be-accepted"
    mutations.append(unknown)
    incomplete = deepcopy(baseline)
    incomplete["datasets"][0]["source_proof_complete"] = False
    mutations.append(incomplete)

    for index, receipt in enumerate(mutations):
        receipt["receipt_sha256"] = _receipt_sha256(receipt)
        path = (tmp_path / f"unsafe-{index}.json").resolve()
        path.write_text(json.dumps(receipt), encoding="utf-8")
        os.chmod(path, 0o600)
        with pytest.raises(IntegrationReadinessError):
            load_and_verify_integration_receipt(path, expectation=expectation)


def test_receipt_path_must_be_private_unique_regular_file(tmp_path: Path) -> None:
    _, expectation, path, _ = _verified(tmp_path)

    os.chmod(path, 0o644)
    with pytest.raises(IntegrationReadinessError, match="permissions"):
        load_and_verify_integration_receipt(path, expectation=expectation)
    os.chmod(path, 0o600)

    hardlink = (tmp_path / "hardlink.json").resolve()
    os.link(path, hardlink)
    with pytest.raises(IntegrationReadinessError, match="hardlink"):
        load_and_verify_integration_receipt(path, expectation=expectation)


def test_verified_capability_must_match_runtime_day_and_query_identity(
    tmp_path: Path,
) -> None:
    config, _, _, verified = _verified(tmp_path)
    profile = config.to_profile()
    requests = {
        spec.dataset_id: spec.query(as_of=config.as_of) for spec in config.datasets
    }
    policies = {spec.dataset_id: spec.policy() for spec in config.datasets}
    kwargs = {
        "readiness": verified,
        "decision_as_of": datetime.fromisoformat(config.as_of),
        "base_url": config.to_client_config().base_url,
        "access_policy_id": config.access_policy_id,
        "catalog_version": config.catalog_version,
        "dataset_profile": profile,
        "dataset_requests": requests,
        "evidence_policies": policies,
    }

    binding = assert_readiness_matches_runtime(
        trade_date="2026-07-17",
        **kwargs,
    )
    assert binding["receipt_sha256"] == verified.receipt_sha256

    with pytest.raises(IntegrationReadinessError, match="cross_day"):
        assert_readiness_matches_runtime(
            trade_date="2026-07-18",
            **kwargs,
        )

    changed_requests = dict(requests)
    first = config.datasets[0]
    changed_requests[first.dataset_id] = first.query(as_of="2026-07-17T09:24:59+08:00")
    with pytest.raises(IntegrationReadinessError, match="dataset_identity"):
        assert_readiness_matches_runtime(
            trade_date="2026-07-17",
            **{**kwargs, "dataset_requests": changed_requests},
        )
