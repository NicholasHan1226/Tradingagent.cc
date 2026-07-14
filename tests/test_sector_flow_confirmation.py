from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json

import pytest

from shared.screening.sector_flow_confirmation import (
    build_sector_flow_confirmation_pair,
)


BASE_SHA = "a" * 64

_INVALID_NATIVE_STRING_VALUES = [
    pytest.param(True, id="bool"),
    pytest.param(801780, id="int"),
    pytest.param(801780.0, id="float"),
    pytest.param(["801780.SI"], id="list"),
    pytest.param({"value": "801780.SI"}, id="mapping"),
    pytest.param(None, id="none"),
    pytest.param("", id="empty"),
    pytest.param("   ", id="blank"),
]

def _snapshot_sha(snapshot):
    payload = {
        key: snapshot[key]
        for key in (
            "scope",
            "sector_id",
            "sector_name",
            "taxonomy",
            "snapshot_id",
            "net_inflow_cny",
            "rank",
            "event_time",
            "available_at",
        )
    }
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _snapshot(**overrides):
    snapshot = {
        "scope": "sector",
        "sector_id": "801780.SI",
        "sector_name": "银行",
        "taxonomy": "SW2021",
        "snapshot_id": "sw-flow-20260714-0935",
        "net_inflow_cny": 320_000_000,
        "rank": 2,
        "event_time": "2026-07-14T09:35:00+08:00",
        "available_at": "2026-07-14T09:35:30+08:00",
    }
    snapshot.update(overrides)
    if "source_snapshot_sha256" not in overrides:
        snapshot["source_snapshot_sha256"] = _snapshot_sha(snapshot)
    return snapshot


def test_pair_is_shadow_only_and_reports_no_candidate_ranking_or_strategy_change():
    pair = build_sector_flow_confirmation_pair(
        base_snapshot_sha256=BASE_SHA,
        sector_id="801780.SI",
        decision_as_of="2026-07-14T09:36:00+08:00",
        sector_snapshot=_snapshot(),
    )

    assert pair["pairing"]["same_base_snapshot"] is True
    assert pair["off"]["status"] == "disabled"
    assert pair["on"]["status"] == "confirmed"
    assert pair["on"]["point_in_time_lineage"]["qualified"] is True
    assert pair["on"]["source_snapshot_sha256"] == _snapshot_sha(_snapshot())
    receipt = pair["on"]["consumption_receipt"]
    assert receipt["consumer"] == "shadow_observation_only"
    assert receipt["consumed"] is False
    assert receipt["changed_candidate_membership"] is False
    assert receipt["changed_ranking"] is False
    assert receipt["changed_playbook"] is False
    assert receipt["changed_strategy"] is False
    assert receipt["changed_execution_eligibility"] is False
    assert receipt["execution_gate_bypassed"] is False
    assert receipt["before_identity"] == receipt["after_identity"]
    assert receipt["before_identity"]["base_snapshot_sha256"] == BASE_SHA
    assert receipt["before_identity"]["decision_as_of"] == (
        "2026-07-14T09:36:00+08:00"
    )


def test_off_and_on_records_bind_the_same_base_and_decision_identity():
    pair = build_sector_flow_confirmation_pair(
        base_snapshot_sha256=BASE_SHA,
        sector_id="801780.SI",
        decision_as_of="2026-07-14T09:36:00+08:00",
        sector_snapshot=_snapshot(),
    )

    assert pair["pairing"]["pair_identity_sha256"]
    for record in (pair["off"], pair["on"]):
        assert record["base_snapshot_sha256"] == BASE_SHA
        assert record["decision_as_of"] == "2026-07-14T09:36:00+08:00"
        assert record["pair_identity_sha256"] == pair["pairing"][
            "pair_identity_sha256"
        ]
        receipt = record["consumption_receipt"]
        assert receipt["before_identity"] == receipt["after_identity"]


def test_missing_snapshot_degrades_without_changing_any_decision_surface():
    pair = build_sector_flow_confirmation_pair(
        base_snapshot_sha256=BASE_SHA,
        sector_id="801780.SI",
        decision_as_of="2026-07-14T09:36:00+08:00",
        sector_snapshot=None,
    )

    assert pair["on"]["status"] == "degraded"
    assert pair["on"]["reason"] == "missing_sector_flow_snapshot"
    assert pair["on"]["applied"] is False
    assert pair["on"]["consumption_receipt"]["changed_ranking"] is False
    assert pair["on"]["consumption_receipt"]["execution_gate_bypassed"] is False


def test_future_or_tampered_snapshot_fails_pit_lineage_closed():
    future = build_sector_flow_confirmation_pair(
        base_snapshot_sha256=BASE_SHA,
        sector_id="801780.SI",
        decision_as_of="2026-07-14T09:36:00+08:00",
        sector_snapshot=_snapshot(available_at="2026-07-14T09:37:00+08:00"),
    )
    tampered = build_sector_flow_confirmation_pair(
        base_snapshot_sha256=BASE_SHA,
        sector_id="801780.SI",
        decision_as_of="2026-07-14T09:36:00+08:00",
        sector_snapshot=_snapshot(source_snapshot_sha256="not-a-sha"),
    )

    assert future["on"]["status"] == "degraded"
    assert future["on"]["reason"] == "snapshot_available_after_decision"
    assert tampered["on"]["status"] == "degraded"
    assert tampered["on"]["reason"] == "invalid_source_snapshot_sha256"


def test_source_sha_must_bind_the_canonical_snapshot_payload():
    snapshot = _snapshot()
    original_sha = snapshot["source_snapshot_sha256"]
    snapshot["net_inflow_cny"] = snapshot["net_inflow_cny"] + 1

    pair = build_sector_flow_confirmation_pair(
        base_snapshot_sha256=BASE_SHA,
        sector_id="801780.SI",
        decision_as_of="2026-07-14T09:36:00+08:00",
        sector_snapshot=snapshot,
    )

    assert snapshot["source_snapshot_sha256"] == original_sha
    assert pair["on"]["status"] == "degraded"
    assert pair["on"]["reason"] == "source_snapshot_sha256_mismatch"


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_sector_flow_values_fail_closed_without_throwing(value):
    pair = build_sector_flow_confirmation_pair(
        base_snapshot_sha256=BASE_SHA,
        sector_id="801780.SI",
        decision_as_of="2026-07-14T09:36:00+08:00",
        sector_snapshot=_snapshot(net_inflow_cny=value, source_snapshot_sha256="b" * 64),
    )

    assert pair["on"]["status"] == "degraded"
    assert pair["on"]["reason"] == "invalid_sector_flow_value"


@pytest.mark.parametrize("value", [True, False, "1", "1.0"])
def test_sector_flow_value_rejects_bool_and_numeric_string_without_coercion(value):
    pair = build_sector_flow_confirmation_pair(
        base_snapshot_sha256=BASE_SHA,
        sector_id="801780.SI",
        decision_as_of="2026-07-14T09:36:00+08:00",
        sector_snapshot=_snapshot(net_inflow_cny=value),
    )

    assert pair["on"]["status"] == "degraded"
    assert pair["on"]["reason"] == "invalid_sector_flow_value_type"


@pytest.mark.parametrize(
    ("requested_sector_id", "snapshot_sector_id", "reason"),
    [
        ("", "", "missing_requested_sector_id"),
        ("801780.SI", "", "missing_snapshot_sector_id"),
    ],
)
def test_sector_identity_must_be_nonempty_before_comparison(
    requested_sector_id, snapshot_sector_id, reason
):
    pair = build_sector_flow_confirmation_pair(
        base_snapshot_sha256=BASE_SHA,
        sector_id=requested_sector_id,
        decision_as_of="2026-07-14T09:36:00+08:00",
        sector_snapshot=_snapshot(sector_id=snapshot_sector_id),
    )

    assert pair["on"]["status"] == "degraded"
    assert pair["on"]["reason"] == reason


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("snapshot_id", "", "missing_snapshot_id"),
        ("taxonomy", "", "missing_sector_taxonomy"),
        ("rank", 2.9, "invalid_sector_flow_rank"),
        ("rank", 2.0, "invalid_sector_flow_rank"),
        ("rank", "2", "invalid_sector_flow_rank"),
        ("rank", "2.0", "invalid_sector_flow_rank"),
        ("rank", True, "invalid_sector_flow_rank"),
    ],
)
def test_confirmation_requires_snapshot_identity_taxonomy_and_integer_rank(
    field, value, reason
):
    pair = build_sector_flow_confirmation_pair(
        base_snapshot_sha256=BASE_SHA,
        sector_id="801780.SI",
        decision_as_of="2026-07-14T09:36:00+08:00",
        sector_snapshot=_snapshot(**{field: value}),
    )

    assert pair["on"]["status"] == "degraded"
    assert pair["on"]["reason"] == reason


def test_pair_does_not_mutate_snapshot_and_rejects_individual_stock_flow():
    snapshot = _snapshot()
    original = deepcopy(snapshot)
    snapshot["scope"] = "individual_stock"

    pair = build_sector_flow_confirmation_pair(
        base_snapshot_sha256=BASE_SHA,
        sector_id="801780.SI",
        decision_as_of="2026-07-14T09:36:00+08:00",
        sector_snapshot=snapshot,
    )

    assert pair["on"]["status"] == "degraded"
    assert pair["on"]["reason"] == "flow_scope_is_not_sector"
    assert snapshot == {**original, "scope": "individual_stock"}


@pytest.mark.parametrize(
    "target",
    [
        "requested_sector_id",
        "snapshot_sector_id",
        "scope",
        "taxonomy",
        "snapshot_id",
    ],
)
@pytest.mark.parametrize("value", _INVALID_NATIVE_STRING_VALUES)
def test_pair_identity_fields_require_native_nonempty_strings(target, value):
    requested_sector_id = "801780.SI"
    overrides = {}

    if target == "requested_sector_id":
        requested_sector_id = value
        if type(value) is not str:
            overrides["sector_id"] = value
    elif target == "snapshot_sector_id":
        overrides["sector_id"] = value
        if type(value) is not str and value is not None:
            requested_sector_id = str(value)
    else:
        overrides[target] = value

    pair = build_sector_flow_confirmation_pair(
        base_snapshot_sha256=BASE_SHA,
        sector_id=requested_sector_id,
        decision_as_of="2026-07-14T09:36:00+08:00",
        sector_snapshot=_snapshot(**overrides),
    )

    type_reasons = {
        "requested_sector_id": "invalid_requested_sector_id_type",
        "snapshot_sector_id": "invalid_snapshot_sector_id_type",
        "scope": "invalid_flow_scope_type",
        "taxonomy": "invalid_sector_taxonomy_type",
        "snapshot_id": "invalid_snapshot_id_type",
    }
    empty_reasons = {
        "requested_sector_id": "missing_requested_sector_id",
        "snapshot_sector_id": "missing_snapshot_sector_id",
        "scope": "flow_scope_is_not_sector",
        "taxonomy": "missing_sector_taxonomy",
        "snapshot_id": "missing_snapshot_id",
    }
    expected_reason = (
        type_reasons[target] if type(value) is not str else empty_reasons[target]
    )
    if target == "scope" and value is None:
        expected_reason = "flow_scope_is_not_sector"

    assert pair["on"]["status"] == "degraded"
    assert pair["on"]["reason"] == expected_reason
    assert pair["pairing"]["pair_identity_valid"] is False
    assert pair["pairing"]["pair_identity_sha256"] is None
    for record in (pair["off"], pair["on"]):
        receipt = record["consumption_receipt"]
        assert receipt["consumed"] is False
        assert receipt["before_identity"] == receipt["after_identity"]
        assert receipt["before_identity"]["pair_identity_sha256"] is None
