from __future__ import annotations

import hashlib
import json
from copy import copy
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone

import pytest

from shared.data.research_snapshot import (
    ResearchDataSnapshot,
    ResearchDatasetSnapshot,
)
from shared.runtime.ashare_observation_history import (
    PROSPECTIVE_OBSERVATION_HISTORY,
    build_ashare_observation_history_readiness,
)
from shared.runtime.ashare_observation_ledger import (
    LABEL_HORIZONS,
    OBSERVED_REASON_CODE,
    AshareObservationMembershipArtifact,
    AshareObservationMembershipRecord,
    build_ashare_observation_membership_artifact,
)
from shared.runtime.ashare_runtime_ports import (
    AshareRuntimeAuthorityBundle,
    _promote_verified_committed_bundle,
)


TARGET_SYMBOL = "600000.SH"


def _unit_verified(
    bundle: AshareRuntimeAuthorityBundle,
) -> AshareRuntimeAuthorityBundle:
    """Mark a pure unit fixture; durable minting is tested in runtime_ports."""

    return _promote_verified_committed_bundle(bundle)


def _verified_replace(
    bundle: AshareRuntimeAuthorityBundle,
    **changes: object,
) -> AshareRuntimeAuthorityBundle:
    mutant = copy(bundle)
    for field_name, value in changes.items():
        object.__setattr__(mutant, field_name, value)
    return mutant


PROFILE_ID = "ashare-phase1-current-observation-v1"
CATALOG_VERSION = "catalog-ashare-history-fixture-v1"
SCHEMA_MAJOR = 2


def _sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _session_date(index: int) -> date:
    return date(2026, 1, 1) + timedelta(days=index)


def _bundle(
    index: int,
    *,
    session: date | None = None,
    decision_at: datetime | None = None,
    target_rows: tuple[dict[str, object], ...] | None = None,
    profile_id: str = PROFILE_ID,
    catalog_version: str = CATALOG_VERSION,
    schema_major: int = SCHEMA_MAJOR,
) -> AshareRuntimeAuthorityBundle:
    session = session or _session_date(index)
    trade_date = session.strftime("%Y%m%d")
    decision_at = decision_at or datetime.combine(
        session,
        time(15, 30),
        tzinfo=timezone(timedelta(hours=8)),
    )
    if target_rows is None:
        target_rows = (
            {
                "ts_code": TARGET_SYMBOL,
                "trade_date": trade_date,
                "close": 10.0 + index,
                "amount": 1_000_000.0 + index,
            },
        )
    rows = [dict(row) for row in target_rows]
    daily = ResearchDatasetSnapshot(
        dataset_id="cn.equity.daily",
        role="required_execution",
        api_version="v1",
        catalog_version=catalog_version,
        request_id=f"daily-request-{trade_date}-{index}",
        receipt_id=f"daily-receipt-{trade_date}-{index}",
        evidence_state="ready",
        evidence_action="accept",
        eligible=True,
        weight=1.0,
        reasons=(),
        source_proof_complete=True,
        lineage_sha256=_sha256(["lineage", trade_date, index]),
        source_proof_sha256=_sha256(["source-proof", trade_date, index]),
        data_through=decision_at.isoformat(),
        observed_at=decision_at.isoformat(),
        next_cursor=None,
        row_count=len(rows),
        observation_mode="current_observation",
        historical_pit_eligible=False,
        query_as_of_mode="decision_as_of",
        minimum_row_count=1,
        max_pages=20,
        max_rows=100_000,
        identity_fields=("ts_code", "trade_date"),
        row_event_time_field="trade_date",
        row_event_time_format="yyyymmdd",
        row_event_timezone="Asia/Shanghai",
        row_event_time_semantic="session",
        identity_sha256=_sha256(
            sorted((row.get("ts_code"), row.get("trade_date")) for row in rows)
        ),
        row_observation_sha256=_sha256(rows),
        max_row_observed_at=decision_at.isoformat(),
        max_row_event_value=trade_date,
        page_count=1,
        pagination_trace_sha256=_sha256(["page-trace", trade_date, index]),
        pagination_semantic_sha256=_sha256(["page-semantic", trade_date, index]),
        page_request_set_sha256=_sha256(["page-requests", trade_date, index]),
        page_response_set_sha256=_sha256(["page-responses", trade_date, index]),
        cursor_chain_sha256=_sha256(["cursor-chain", trade_date, index]),
        response_sha256=_sha256(["response", trade_date, index, rows]),
        _rows_json=json.dumps(rows, separators=(",", ":"), sort_keys=True),
    )
    snapshot_sha256 = _sha256(["snapshot", trade_date, index])
    snapshot = ResearchDataSnapshot(
        profile_id=profile_id,
        profile_contract_sha256=_sha256(["profile", profile_id]),
        catalog_version=catalog_version,
        decision_as_of=decision_at.isoformat(),
        datasets=(daily,),
        execution_eligible=True,
        historical_pit_eligible=False,
        blocking_reasons=(),
        snapshot_sha256=snapshot_sha256,
    )
    probe_receipt_sha256 = _sha256(["probe", trade_date, index])
    observation_receipt_sha256 = _sha256(["observation", trade_date, index])
    membership_records = tuple(
        AshareObservationMembershipRecord(
            symbol=str(row["ts_code"]),
            disposition="observed",
            reason_code=OBSERVED_REASON_CODE,
        )
        for row in rows
    )
    membership_unsigned = {
        "schema_id": "tradingagent.ashare.observation-membership-ledger.v1",
        "observation_session": trade_date,
        "decision_as_of": decision_at.isoformat(),
        "profile_id": profile_id,
        "profile_contract_sha256": snapshot.profile_contract_sha256,
        "catalog_version": catalog_version,
        "catalog_version_sha256": _sha256(catalog_version),
        "snapshot_sha256": snapshot_sha256,
        "probe_receipt_sha256": probe_receipt_sha256,
        "observation_receipt_sha256": observation_receipt_sha256,
        "universe_sha256": _sha256([item.symbol for item in membership_records]),
        "records": [item.to_dict() for item in membership_records],
        "label_horizons": list(LABEL_HORIZONS),
        "historical_pit_eligible": False,
        "learning_eligible": False,
        "performance_eligible": False,
        "promotion_eligible": False,
        "real_trading_enabled": False,
    }
    bundle = AshareRuntimeAuthorityBundle(
        research_snapshot=snapshot,
        profile_id=profile_id,
        catalog_version=catalog_version,
        decision_as_of=decision_at.isoformat(),
        schema_major=schema_major,
        snapshot_sha256=snapshot_sha256,
        probe_receipt_sha256=probe_receipt_sha256,
        observation_receipt_sha256=observation_receipt_sha256,
        observation_membership_sha256=_sha256(membership_unsigned),
        observation_transaction_complete_sha256=_sha256(
            ["transaction-complete", trade_date, index]
        ),
        historical_pit_eligible=False,
        historical_feature_claims=(),
        observation_eligible=False,
        ranking_eligible=False,
        planning_eligible=False,
        execution_evidence_eligible=False,
        blockers=(
            "champion_numeric_features_unavailable",
            "minute_execution_evidence_unavailable",
        ),
    )
    membership = _membership(bundle)
    return _unit_verified(
        replace(
            bundle,
            observation_membership_sha256=membership.content_sha256,
            observation_membership=membership,
        )
    )


def _history(count: int) -> tuple[AshareRuntimeAuthorityBundle, ...]:
    return tuple(_bundle(index) for index in range(count))


def _membership(
    bundle: AshareRuntimeAuthorityBundle,
    *,
    excluded_target: bool = False,
) -> AshareObservationMembershipArtifact:
    assert bundle.research_snapshot is not None
    daily = bundle.research_snapshot.datasets[0]
    session = str(daily.max_row_event_value)
    unique_symbols = sorted(
        {
            str(row["ts_code"])
            for row in daily.decoded_rows()
            if isinstance(row, dict) and isinstance(row.get("ts_code"), str)
        }
    )
    records = tuple(
        AshareObservationMembershipRecord(
            symbol=symbol,
            disposition=(
                "excluded"
                if excluded_target and symbol == TARGET_SYMBOL
                else "observed"
            ),
            reason_code=(
                "target_not_in_observation_universe"
                if excluded_target and symbol == TARGET_SYMBOL
                else OBSERVED_REASON_CODE
            ),
        )
        for symbol in unique_symbols
    )
    observed_symbols = [
        item.symbol for item in records if item.disposition == "observed"
    ]
    universe_sha256 = _sha256(observed_symbols)
    unsigned = {
        "schema_id": "tradingagent.ashare.observation-membership-ledger.v1",
        "observation_session": session,
        "decision_as_of": bundle.decision_as_of,
        "profile_id": bundle.profile_id,
        "profile_contract_sha256": bundle.research_snapshot.profile_contract_sha256,
        "catalog_version": bundle.catalog_version,
        "catalog_version_sha256": _sha256(bundle.catalog_version),
        "snapshot_sha256": bundle.snapshot_sha256,
        "probe_receipt_sha256": bundle.probe_receipt_sha256,
        "observation_receipt_sha256": bundle.observation_receipt_sha256,
        "universe_sha256": universe_sha256,
        "records": [item.to_dict() for item in records],
        "label_horizons": list(LABEL_HORIZONS),
        "historical_pit_eligible": False,
        "learning_eligible": False,
        "performance_eligible": False,
        "promotion_eligible": False,
        "real_trading_enabled": False,
    }
    return AshareObservationMembershipArtifact(
        observation_session=session,
        decision_as_of=str(bundle.decision_as_of),
        profile_id=str(bundle.profile_id),
        profile_contract_sha256=bundle.research_snapshot.profile_contract_sha256,
        catalog_version=str(bundle.catalog_version),
        catalog_version_sha256=_sha256(bundle.catalog_version),
        snapshot_sha256=str(bundle.snapshot_sha256),
        probe_receipt_sha256=str(bundle.probe_receipt_sha256),
        observation_receipt_sha256=str(bundle.observation_receipt_sha256),
        universe_sha256=universe_sha256,
        records=records,
        label_horizons=LABEL_HORIZONS,
        historical_pit_eligible=False,
        learning_eligible=False,
        performance_eligible=False,
        promotion_eligible=False,
        real_trading_enabled=False,
        content_sha256=_sha256(unsigned),
    )


def _memberships(
    bundles: tuple[AshareRuntimeAuthorityBundle, ...],
) -> tuple[AshareObservationMembershipArtifact, ...]:
    return tuple(_membership(bundle) for bundle in bundles)


def _observation_receipt(bundle: AshareRuntimeAuthorityBundle) -> dict[str, object]:
    observed = [TARGET_SYMBOL]
    unsigned: dict[str, object] = {
        "schema_id": "tradingagent.ashare.observation-receipt.v1",
        "profile_id": bundle.profile_id,
        "catalog_version": bundle.catalog_version,
        "decision_as_of": bundle.decision_as_of,
        "manifest_sha256": _sha256(["manifest", bundle.decision_as_of]),
        "snapshot_sha256": bundle.snapshot_sha256,
        "probe_receipt_sha256": bundle.probe_receipt_sha256,
        "tradable_universe_count": 1,
        "tradable_universe_sha256": _sha256(observed),
        "excluded_reason_counts": {},
        "context_probe_roles": [],
        "mode": "observation_only",
        "marketgraph_mode": "mg_off",
        "real_trading_enabled": False,
        "historical_pit_eligible": False,
        "execution_authority": False,
    }
    return {**unsigned, "receipt_sha256": _sha256(unsigned)}


def _readiness(
    bundles: tuple[AshareRuntimeAuthorityBundle, ...],
    *,
    target_symbol: object = TARGET_SYMBOL,
    min_required_sessions: object = 21,
    membership_artifacts: tuple[AshareObservationMembershipArtifact, ...] | None = None,
):
    return build_ashare_observation_history_readiness(
        bundles,
        membership_artifacts=(
            _memberships(bundles)
            if membership_artifacts is None
            else membership_artifacts
        ),
        target_symbol=target_symbol,
        min_required_sessions=min_required_sessions,
    )


def _feature_map(result: object) -> dict[str, object]:
    return {
        item.feature_id: item
        for item in result.feature_readiness  # type: ignore[attr-defined]
    }


def test_twenty_prospective_sessions_remain_explicitly_insufficient() -> None:
    bundles = _history(20)

    result = _readiness(
        bundles,
        target_symbol=TARGET_SYMBOL,
    )

    assert result.history_mode == PROSPECTIVE_OBSERVATION_HISTORY
    assert result.history_mode == "prospective_observation_history"
    assert result.session_count == 20
    assert result.min_required_sessions == 21
    assert isinstance(result.history_identity_sha256, str)
    assert len(result.history_identity_sha256) == 64
    assert (
        result.history_identity_sha256
        == _readiness(
            bundles,
            target_symbol=TARGET_SYMBOL,
        ).history_identity_sha256
    )
    assert result.prospective_history_eligible is False
    assert result.blockers == (
        "trading_session_continuity_authority_unavailable",
        "corporate_action_adjustment_authority_unavailable",
        "insufficient_prospective_sessions",
    )
    assert result.coverage.target_symbol == TARGET_SYMBOL
    assert result.coverage.expected_session_count == 20
    assert result.coverage.complete_session_count == 20
    assert result.coverage.coverage_ratio == 1.0
    assert result.coverage.incomplete_sessions == ()
    features = _feature_map(result)
    assert tuple(features) == (
        "momentum_20d",
        "low_volatility_20d",
        "adv_20d",
    )
    assert all(item.eligible is False for item in features.values())
    assert all(item.blockers == result.blockers for item in features.values())
    assert all(bundle.historical_pit_eligible is False for bundle in bundles)
    assert all(
        bundle.research_snapshot is not None
        and bundle.research_snapshot.historical_pit_eligible is False
        and bundle.research_snapshot.datasets[0].historical_pit_eligible is False
        for bundle in bundles
    )
    assert not hasattr(result, "champion_score")
    assert not hasattr(result, "probability")
    assert not hasattr(result, "orders")


def test_authority_subclass_cannot_override_loader_capability() -> None:
    class ForgedAuthority(AshareRuntimeAuthorityBundle):
        @property
        def committed_state_verified(self) -> bool:
            return True

    legitimate = _bundle(0)
    forged = object.__new__(ForgedAuthority)
    forged.__dict__.update(legitimate.__dict__)
    object.__setattr__(forged, "observation_eligible", True)

    result = build_ashare_observation_history_readiness(
        (forged,),
        membership_artifacts=(_membership(legitimate),),
        target_symbol=TARGET_SYMBOL,
    )

    assert "observation_authority_invalid" in result.blockers
    assert result.coverage.complete_session_count == 0
    assert result.coverage.coverage_ratio == 0.0


def test_twenty_one_complete_sessions_still_require_calendar_and_adjustment_authority() -> (
    None
):
    bundles = _history(21)
    result = _readiness(
        bundles,
        target_symbol=TARGET_SYMBOL,
    )

    assert result.session_count == 21
    assert isinstance(result.history_identity_sha256, str)
    assert len(result.history_identity_sha256) == 64
    assert result.prospective_history_eligible is False
    assert result.blockers == (
        "trading_session_continuity_authority_unavailable",
        "corporate_action_adjustment_authority_unavailable",
    )
    assert result.coverage.complete_session_count == 21
    assert result.coverage.coverage_ratio == 1.0
    features = _feature_map(result)
    assert all(item.eligible is False for item in features.values())
    assert all(
        item.history_mode == PROSPECTIVE_OBSERVATION_HISTORY
        for item in features.values()
    )
    assert all(item.required_sessions == 21 for item in features.values())
    assert all(item.observed_sessions == 21 for item in features.values())
    assert all(item.blockers == result.blockers for item in features.values())


def test_public_membership_builder_binds_into_history_without_manual_artifact() -> None:
    raw_bundle = _bundle(0)
    assert raw_bundle.research_snapshot is not None
    snapshot = raw_bundle.research_snapshot
    strict_snapshot_sha256 = _sha256(
        {
            "profile_id": snapshot.profile_id,
            "profile_contract_sha256": snapshot.profile_contract_sha256,
            "catalog_version": snapshot.catalog_version,
            "decision_as_of": snapshot.decision_as_of,
            "datasets": [
                {
                    "dataset_id": dataset.dataset_id,
                    "role": dataset.role,
                    "response_sha256": dataset.response_sha256,
                }
                for dataset in snapshot.datasets
            ],
            "blocking_reasons": [],
        }
    )
    bundle = _verified_replace(
        raw_bundle,
        research_snapshot=replace(snapshot, snapshot_sha256=strict_snapshot_sha256),
        snapshot_sha256=strict_snapshot_sha256,
    )
    receipt = _observation_receipt(bundle)
    bundle = _verified_replace(
        bundle,
        observation_receipt_sha256=receipt["receipt_sha256"],
    )
    assert bundle.research_snapshot is not None
    membership = build_ashare_observation_membership_artifact(
        observation_session="20260101",
        research_snapshot=bundle.research_snapshot,
        observation_receipt=receipt,
        records=(
            AshareObservationMembershipRecord(
                symbol=TARGET_SYMBOL,
                disposition="observed",
                reason_code=OBSERVED_REASON_CODE,
            ),
        ),
    )
    bundle = _verified_replace(
        bundle,
        observation_membership_sha256=membership.content_sha256,
    )

    result = _readiness((bundle,), membership_artifacts=(membership,))

    assert result.history_identity_sha256 is not None
    assert "membership_artifact_invalid" not in result.blockers
    assert "membership_artifact_identity_mismatch" not in result.blockers
    assert result.coverage.complete_session_count == 1


@pytest.mark.parametrize(
    ("target_rows", "expected_kind"),
    (
        (
            (
                {
                    "ts_code": "000001.SZ",
                    "trade_date": "20260120",
                    "close": 12.0,
                    "amount": 300.0,
                },
            ),
            "missing",
        ),
        (
            (
                {
                    "ts_code": TARGET_SYMBOL,
                    "trade_date": "20260120",
                    "close": 10.0,
                    "amount": 100.0,
                },
                {
                    "ts_code": TARGET_SYMBOL,
                    "trade_date": "20260120",
                    "close": 11.0,
                    "amount": 200.0,
                },
            ),
            "duplicate",
        ),
        (
            (
                {
                    "ts_code": TARGET_SYMBOL,
                    "trade_date": "20260120",
                    "close": 0.0,
                    "amount": 100.0,
                },
            ),
            "invalid",
        ),
        (
            (
                {
                    "ts_code": TARGET_SYMBOL,
                    "trade_date": "20260120",
                    "close": 10.0,
                    "amount": 0.0,
                },
            ),
            "invalid",
        ),
    ),
)
def test_symbol_requires_exactly_one_positive_close_and_amount_per_session(
    target_rows: tuple[dict[str, object], ...],
    expected_kind: str,
) -> None:
    bundles = list(_history(19))
    bundles.append(_bundle(19, target_rows=target_rows))

    frozen_bundles = tuple(bundles)
    result = _readiness(
        frozen_bundles,
        target_symbol=TARGET_SYMBOL,
    )

    assert result.session_count == 20
    assert result.prospective_history_eligible is False
    if expected_kind == "missing":
        assert result.blockers == (
            "membership_target_not_observed",
            "trading_session_continuity_authority_unavailable",
            "corporate_action_adjustment_authority_unavailable",
            "insufficient_prospective_sessions",
            "incomplete_symbol_history",
        )
    elif expected_kind == "duplicate":
        assert result.blockers == (
            "membership_artifact_identity_mismatch",
            "trading_session_continuity_authority_unavailable",
            "corporate_action_adjustment_authority_unavailable",
            "insufficient_prospective_sessions",
            "incomplete_symbol_history",
        )
    else:
        assert result.blockers == (
            "trading_session_continuity_authority_unavailable",
            "corporate_action_adjustment_authority_unavailable",
            "insufficient_prospective_sessions",
            "incomplete_symbol_history",
        )
    assert result.coverage.complete_session_count == 19
    assert result.coverage.coverage_ratio == 0.95
    assert result.coverage.incomplete_sessions == ("20260120",)
    if expected_kind == "missing":
        assert result.coverage.missing_sessions == ("20260120",)
    elif expected_kind == "duplicate":
        assert result.coverage.duplicate_row_sessions == ("20260120",)
    else:
        assert result.coverage.invalid_value_sessions == ("20260120",)
    assert all(
        item.blockers == result.blockers for item in _feature_map(result).values()
    )


@pytest.mark.parametrize(
    ("mutate", "reason"),
    (
        ("ineligible", "observation_authority_ineligible"),
        ("missing_daily_receipt", "source_identity_incomplete"),
        ("profile_mismatch", "authority_contract_mismatch"),
        ("catalog_mismatch", "authority_contract_mismatch"),
        ("schema_mismatch", "authority_contract_mismatch"),
    ),
)
def test_every_session_must_keep_one_consistent_observation_authority(
    mutate: str,
    reason: str,
) -> None:
    bundles = list(_history(20))
    memberships = _memberships(tuple(bundles))
    last = bundles[-1]
    if mutate == "ineligible":
        last = replace(last, observation_eligible=False)
    elif mutate == "missing_daily_receipt":
        assert last.research_snapshot is not None
        daily = replace(last.research_snapshot.datasets[0], receipt_id=None)
        snapshot = replace(last.research_snapshot, datasets=(daily,))
        last = _verified_replace(
            last,
            research_snapshot=snapshot,
        )
    elif mutate == "profile_mismatch":
        last = _bundle(19, profile_id="other-profile")
    elif mutate == "catalog_mismatch":
        last = _bundle(19, catalog_version="other-catalog")
    elif mutate == "schema_mismatch":
        last = _bundle(19, schema_major=3)
    bundles[-1] = last

    frozen_bundles = tuple(bundles)
    result = _readiness(
        frozen_bundles,
        membership_artifacts=memberships,
        target_symbol=TARGET_SYMBOL,
    )

    assert result.prospective_history_eligible is False
    assert reason in result.blockers
    assert result.history_identity_sha256 is None
    assert all(item.eligible is False for item in _feature_map(result).values())


@pytest.mark.parametrize(
    "mutate",
    (
        "missing_snapshot",
        "missing_bundle_receipt",
        "missing_transaction_complete",
        "historical_claim",
    ),
)
def test_observation_eligible_bundle_rejects_missing_or_historical_authority(
    mutate: str,
) -> None:
    bundle = _bundle(0)

    with pytest.raises(
        ValueError,
        match="observation_eligible_requires_verified_committed_state",
    ):
        if mutate == "missing_snapshot":
            replace(bundle, research_snapshot=None)
        elif mutate == "missing_bundle_receipt":
            replace(bundle, observation_receipt_sha256=None)
        elif mutate == "missing_transaction_complete":
            replace(bundle, observation_transaction_complete_sha256=None)
        else:
            replace(bundle, historical_pit_eligible=True)


def test_decision_as_of_must_be_aware_and_strictly_increasing() -> None:
    naive = list(_history(20))
    naive_memberships = _memberships(tuple(naive))
    naive_decision = datetime.combine(_session_date(19), time(15, 30)).isoformat()
    assert naive[-1].research_snapshot is not None
    naive[-1] = _verified_replace(
        naive[-1],
        decision_as_of=naive_decision,
        research_snapshot=replace(
            naive[-1].research_snapshot,
            decision_as_of=naive_decision,
        ),
    )
    frozen_naive = tuple(naive)
    naive_result = _readiness(
        frozen_naive,
        membership_artifacts=naive_memberships,
        target_symbol=TARGET_SYMBOL,
    )
    assert "decision_as_of_invalid" in naive_result.blockers

    decreasing = list(_history(20))
    decreasing_memberships = _memberships(tuple(decreasing))
    decreasing_decision = datetime.combine(
        _session_date(18),
        time(14, 30),
        tzinfo=timezone(timedelta(hours=8)),
    ).isoformat()
    assert decreasing[-1].research_snapshot is not None
    decreasing[-1] = _verified_replace(
        decreasing[-1],
        decision_as_of=decreasing_decision,
        research_snapshot=replace(
            decreasing[-1].research_snapshot,
            decision_as_of=decreasing_decision,
        ),
    )
    frozen_decreasing = tuple(decreasing)
    decreasing_result = _readiness(
        frozen_decreasing,
        membership_artifacts=decreasing_memberships,
        target_symbol=TARGET_SYMBOL,
    )
    assert "decision_as_of_not_strictly_increasing" in decreasing_result.blockers


def test_daily_trade_date_must_be_unique_and_strictly_increasing() -> None:
    duplicate = list(_history(20))
    duplicate[-1] = _bundle(19, session=_session_date(18))

    frozen_duplicate = tuple(duplicate)
    duplicate_result = _readiness(
        frozen_duplicate,
        target_symbol=TARGET_SYMBOL,
    )

    assert duplicate_result.session_count == 19
    assert "duplicate_session" in duplicate_result.blockers
    assert "daily_trade_date_not_strictly_increasing" in duplicate_result.blockers

    decreasing = list(_history(20))
    decreasing[-1] = _bundle(19, session=_session_date(17))
    frozen_decreasing = tuple(decreasing)
    decreasing_result = _readiness(
        frozen_decreasing,
        target_symbol=TARGET_SYMBOL,
    )
    assert "daily_trade_date_not_strictly_increasing" in decreasing_result.blockers


@pytest.mark.parametrize(
    ("identity", "reason"),
    (
        ("snapshot", "duplicate_snapshot_identity"),
        ("probe", "duplicate_receipt_identity"),
        ("observation", "duplicate_receipt_identity"),
        ("daily", "duplicate_receipt_identity"),
    ),
)
def test_snapshot_and_receipt_identities_cannot_repeat_across_sessions(
    identity: str,
    reason: str,
) -> None:
    bundles = list(_history(20))
    first = bundles[0]
    last = bundles[-1]
    if identity == "snapshot":
        assert last.research_snapshot is not None
        snapshot = replace(
            last.research_snapshot,
            snapshot_sha256=first.snapshot_sha256,
        )
        last = _verified_replace(
            last,
            research_snapshot=snapshot,
            snapshot_sha256=first.snapshot_sha256,
        )
    elif identity == "probe":
        last = _verified_replace(
            last,
            probe_receipt_sha256=first.probe_receipt_sha256,
        )
    elif identity == "observation":
        last = _verified_replace(
            last,
            observation_receipt_sha256=first.observation_receipt_sha256,
        )
    else:
        assert first.research_snapshot is not None
        assert last.research_snapshot is not None
        daily = replace(
            last.research_snapshot.datasets[0],
            receipt_id=first.research_snapshot.datasets[0].receipt_id,
        )
        snapshot = replace(last.research_snapshot, datasets=(daily,))
        last = _verified_replace(
            last,
            research_snapshot=snapshot,
        )
    bundles[-1] = last

    frozen_bundles = tuple(bundles)
    result = _readiness(
        frozen_bundles,
        target_symbol=TARGET_SYMBOL,
    )

    assert result.prospective_history_eligible is False
    assert reason in result.blockers


def test_malformed_frozen_daily_rows_fail_closed_without_escaping() -> None:
    bundles = list(_history(20))
    memberships = _memberships(tuple(bundles))
    last = bundles[-1]
    assert last.research_snapshot is not None
    daily = replace(last.research_snapshot.datasets[0], _rows_json="{")
    snapshot = replace(last.research_snapshot, datasets=(daily,))
    bundles[-1] = _verified_replace(
        last,
        research_snapshot=snapshot,
    )

    frozen_bundles = tuple(bundles)
    result = _readiness(
        frozen_bundles,
        membership_artifacts=memberships,
        target_symbol=TARGET_SYMBOL,
    )

    assert result.prospective_history_eligible is False
    assert "daily_dataset_contract_invalid" in result.blockers


def test_membership_artifact_is_required_for_every_observation_session() -> None:
    bundles = _history(20)

    result = build_ashare_observation_history_readiness(
        bundles,
        membership_artifacts=(),
        target_symbol=TARGET_SYMBOL,
    )

    assert result.prospective_history_eligible is False
    assert "membership_artifact_missing" in result.blockers
    assert result.history_identity_sha256 is None


def test_target_must_be_observed_in_every_membership_artifact() -> None:
    bundles = list(_history(19))
    target_rows = (
        {
            "ts_code": TARGET_SYMBOL,
            "trade_date": "20260120",
            "close": 10.0,
            "amount": 100.0,
        },
        {
            "ts_code": "000001.SZ",
            "trade_date": "20260120",
            "close": 12.0,
            "amount": 200.0,
        },
    )
    bundles.append(_bundle(19, target_rows=target_rows))
    frozen_bundles = tuple(bundles)
    memberships = list(_memberships(frozen_bundles))
    memberships[-1] = _membership(frozen_bundles[-1], excluded_target=True)
    bundles[-1] = _verified_replace(
        bundles[-1],
        observation_membership_sha256=memberships[-1].content_sha256,
    )
    frozen_bundles = tuple(bundles)

    result = _readiness(
        frozen_bundles,
        membership_artifacts=tuple(memberships),
    )

    assert result.prospective_history_eligible is False
    assert "membership_target_not_observed" in result.blockers
    assert result.history_identity_sha256 is None


def test_membership_content_identity_is_part_of_history_identity() -> None:
    bundles = _history(19)
    memberships = _memberships(bundles)
    baseline = _readiness(bundles, membership_artifacts=memberships)
    tampered = copy(memberships[-1])
    object.__setattr__(tampered, "content_sha256", "f" * 64)

    invalid = _readiness(
        bundles,
        membership_artifacts=(*memberships[:-1], tampered),
    )

    assert baseline.history_identity_sha256 is not None
    assert invalid.history_identity_sha256 is None
    assert "membership_artifact_invalid" in invalid.blockers


@pytest.mark.parametrize("min_required_sessions", (True, 0, 20, 21.0, "21"))
def test_minimum_session_configuration_cannot_weaken_twenty_day_features(
    min_required_sessions: object,
) -> None:
    with pytest.raises(
        ValueError,
        match="min_required_sessions_must_be_integer_at_least_21",
    ):
        _readiness(
            _history(20),
            target_symbol=TARGET_SYMBOL,
            min_required_sessions=min_required_sessions,
        )


@pytest.mark.parametrize("target_symbol", ("", " 600000.SH", "600000.sh", 600000))
def test_target_symbol_must_be_one_canonical_ashare_symbol(
    target_symbol: object,
) -> None:
    with pytest.raises(ValueError, match="target_symbol_must_be_canonical"):
        _readiness(
            _history(20),
            target_symbol=target_symbol,
        )
