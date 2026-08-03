from __future__ import annotations

from dataclasses import replace
import hashlib

import pytest

from CNFutures.fut_basic_contract_units import (
    FutBasicRawContractUnitFact,
    FutBasicRawContractUnitSnapshot,
)
from CNFutures.fut_settle_market_rules import (
    FutSettleRawMarketRuleFact,
    FutSettleRawMarketRuleSnapshot,
)
from CNFutures.fut_mapping_current_snapshot import (
    FutMappingCurrentSnapshot,
    FutMappingRawCurrentSnapshotFact,
)
from CNFutures.ft_limit_current_snapshot import (
    FutLimitCurrentSnapshot,
    FutLimitRawCurrentSnapshotFact,
)
from CNFutures.m_simulation_readiness import (
    DayNightFixtureAuthority,
    MSimulationReadinessProjectionError,
    project_m_simulation_readiness,
)


def _sha256(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _fut_basic(*, multiplier: int = 10, quote_unit_desc: str = "CNY/ton") -> FutBasicRawContractUnitSnapshot:
    facts = tuple(
        FutBasicRawContractUnitFact(
            ts_code=f"M{index:03d}.DCE",
            receipt_id="receipt:fut-basic",
            lineage_sha256=_sha256("fut-basic-lineage"),
            raw_values={
                "multiplier": multiplier,
                "trade_unit": "10 tons/lot",
                "per_unit": "ton",
                "quote_unit": "CNY",
                "quote_unit_desc": quote_unit_desc,
            },
        )
        for index in range(207)
    )
    return FutBasicRawContractUnitSnapshot(
        dataset_id="cn.dataset.fut_basic",
        schema_major=1,
        catalog_version="fixture-fut-basic-v1",
        receipt_id="receipt:fut-basic",
        lineage_sha256=_sha256("fut-basic-lineage"),
        page_count=3,
        row_count=207,
        terminal_pagination=True,
        replay_verified=True,
        semantic_sha256=_sha256("fut-basic-semantic"),
        pagination_trace_sha256=_sha256("fut-basic-pagination"),
        state="partial",
        degraded=True,
        coverage_complete=False,
        coverage_reason="response_completeness_unverified",
        facts=facts,
    )


def _fut_settle() -> FutSettleRawMarketRuleSnapshot:
    return FutSettleRawMarketRuleSnapshot(
        dataset_id="cn.dataset.fut_settle",
        schema_major=2,
        catalog_version="fixture-fut-settle-v2",
        trade_date="20260803",
        receipt_id="receipt:fut-settle",
        lineage_sha256=_sha256("fut-settle-lineage"),
        page_count=1,
        row_count=2,
        terminal_pagination=True,
        replay_verified=True,
        semantic_sha256=_sha256("fut-settle-semantic"),
        pagination_trace_sha256=_sha256("fut-settle-pagination"),
        facts=(
            FutSettleRawMarketRuleFact(
                trade_date="20260803",
                ts_code="M002.DCE",
                receipt_id="receipt:fut-settle",
                lineage_sha256=_sha256("fut-settle-lineage"),
                raw_values={"settle": 3000.0},
            ),
        ),
    )


def _ft_limit() -> FutLimitCurrentSnapshot:
    receipt_id = "receipt:a6b9755a6aef1da93f708b32c72e6487e2ed04a84dae9c3bc268a313e4e5c036"
    lineage_sha256 = "6a04306b8a014a46130e79edf3355c260e0e37a3c83822ce2b1bf6eabca632a2"
    return FutLimitCurrentSnapshot(
        dataset_id="cn.dataset.ft_limit",
        schema_major=1,
        catalog_version="v1-ae7d554642b6ae72",
        trade_date="20260803",
        receipt_id=receipt_id,
        lineage_sha256=lineage_sha256,
        page_count=9,
        row_count=868,
        terminal_pagination=True,
        replay_verified=True,
        semantic_sha256=_sha256("ft-limit-semantic"),
        pagination_trace_sha256=_sha256("ft-limit-pagination"),
        state="stale",
        degraded=True,
        reason="freshness_sla_exceeded",
        facts=tuple(
            FutLimitRawCurrentSnapshotFact(
                trade_date="20260803",
                ts_code=f"M{index:03d}.DCE",
                exchange="DCE",
                receipt_id=receipt_id,
                lineage_sha256=lineage_sha256,
                raw_values={"up_limit": 3000.0, "down_limit": 2500.0, "m_ratio": 12.0},
            )
            for index in range(1, 9)
        ),
    )


def _fut_mapping() -> FutMappingCurrentSnapshot:
    receipt_id = "receipt:358a36f5891f9b2a604c4942906da8e4c9170c714229a9a519944ef80ece1d06"
    lineage_sha256 = "df4e14bf0a16a28d8b6a030ff637588f5dc315d525282cbecd16011e40c1f172"
    return FutMappingCurrentSnapshot(
        dataset_id="cn.dataset.fut_mapping",
        schema_major=1,
        catalog_version="v1-ae7d554642b6ae72",
        trade_date="20260803",
        receipt_id=receipt_id,
        lineage_sha256=lineage_sha256,
        page_count=1,
        row_count=202,
        terminal_pagination=True,
        replay_verified=True,
        semantic_sha256=_sha256("fut-mapping-semantic"),
        pagination_trace_sha256=_sha256("fut-mapping-pagination"),
        facts=(
            FutMappingRawCurrentSnapshotFact(
                trade_date="20260803",
                ts_code="M.DCE",
                receipt_id=receipt_id,
                lineage_sha256=lineage_sha256,
                raw_values={"mapping_ts_code": "M2609.DCE"},
            ),
        ),
    )


def _fixture_authority(**overrides: object) -> DayNightFixtureAuthority:
    values: dict[str, object] = {
        "fixture_only": True,
        "numeric_tick_receipt_bound": False,
        "live_session_receipt_bound": False,
        "pit_rollover_receipt_bound": False,
        "fixture_session_windows": (
            ("2026-08-03T09:00:00+08:00", "2026-08-03T10:15:00+08:00"),
        ),
    }
    values.update(overrides)
    return DayNightFixtureAuthority(**values)


def _project(**overrides: object):
    values = {
        "fut_basic": _fut_basic(),
        "fut_settle": _fut_settle(),
        "fut_mapping": _fut_mapping(),
        "ft_limit": _ft_limit(),
        "day_night_fixture_authority": _fixture_authority(),
    }
    values.update(overrides)
    return project_m_simulation_readiness(**values)


def test_emits_deterministic_per_contract_fail_closed_coverage_ledger() -> None:
    result = _project()

    assert result.mode == "m_contract_simulation_readiness_projection"
    assert result.fixture_only is True
    assert result.fut_basic_receipt_id == "receipt:fut-basic"
    assert result.fut_settle_receipt_id == "receipt:fut-settle"
    assert result.fut_mapping_dataset_id == "cn.dataset.fut_mapping"
    assert result.fut_mapping_catalog_version == "v1-ae7d554642b6ae72"
    assert result.fut_mapping_trade_date == "20260803"
    assert result.fut_mapping_receipt_id == (
        "receipt:358a36f5891f9b2a604c4942906da8e4c9170c714229a9a519944ef80ece1d06"
    )
    assert result.fut_mapping_lineage_sha256 == (
        "df4e14bf0a16a28d8b6a030ff637588f5dc315d525282cbecd16011e40c1f172"
    )
    assert result.current_mapping_observed_non_pit is True
    assert result.ft_limit_dataset_id == "cn.dataset.ft_limit"
    assert result.ft_limit_catalog_version == "v1-ae7d554642b6ae72"
    assert result.ft_limit_trade_date == "20260803"
    assert result.ft_limit_receipt_id == (
        "receipt:a6b9755a6aef1da93f708b32c72e6487e2ed04a84dae9c3bc268a313e4e5c036"
    )
    assert len(result.contracts) == 207
    assert [item.ts_code for item in result.contracts] == sorted(
        item.ts_code for item in result.contracts
    )
    assert result.contracts[1].ts_code == "M001.DCE"
    assert result.contracts[1].fut_basic_raw_unit_bound is True
    assert result.contracts[1].fut_settle_raw_rule_bound is False
    assert result.contracts[1].ft_limit_raw_fact_bound is True
    assert result.contracts[2].fut_settle_raw_rule_bound is True
    assert result.contracts[2].ft_limit_raw_fact_bound is True
    assert result.contracts[0].reasons == (
        "fut_basic_coverage_incomplete",
        "ft_limit_stale_or_degraded",
        "numeric_tick_authority_missing",
        "receipt_bound_live_session_authority_missing",
        "pit_rollover_authority_missing",
    )
    assert result.fut_basic_coverage_reason == "response_completeness_unverified"
    assert result.ft_limit_state == "stale"
    assert result.ft_limit_degraded is True
    assert result.simulation_ready is False
    assert result.stable is False
    assert result.runtime_eligible is False
    assert result.execution_eligible is False
    assert result.trading_eligible is False
    assert all(
        item.simulation_ready is False
        and item.runtime_eligible is False
        and item.execution_eligible is False
        and item.trading_eligible is False
        for item in result.contracts
    )


def test_quote_text_multiplier_and_fixture_windows_cannot_create_missing_authority() -> None:
    baseline = _project()
    changed = _project(
        fut_basic=_fut_basic(multiplier=999, quote_unit_desc="tick=0.5 CNY/ton"),
        day_night_fixture_authority=_fixture_authority(
            fixture_session_windows=(
                ("2026-08-02T21:00:00+08:00", "2026-08-03T23:00:00+08:00"),
            )
        ),
    )

    assert changed.contracts[0].reasons == baseline.contracts[0].reasons
    assert changed.simulation_ready is False
    assert changed.runtime_eligible is False
    assert changed.execution_eligible is False
    assert changed.trading_eligible is False


@pytest.mark.parametrize(
    ("authority_update", "reason"),
    [
        ({"numeric_tick_receipt_bound": True}, "numeric_tick_authority_unexpected"),
        ({"live_session_receipt_bound": True}, "live_session_authority_unexpected"),
        ({"pit_rollover_receipt_bound": True}, "pit_rollover_authority_unexpected"),
    ],
)
def test_rejects_unproven_positive_authority_flags(
    authority_update: dict[str, bool], reason: str
) -> None:
    with pytest.raises(MSimulationReadinessProjectionError, match=reason):
        _project(day_night_fixture_authority=_fixture_authority(**authority_update))


@pytest.mark.parametrize(
    ("fact_update", "reason"),
    [
        ({"receipt_id": "receipt:other"}, "fut_basic_fact_receipt_mismatch"),
        ({"lineage_sha256": _sha256("other-lineage")}, "fut_basic_fact_lineage_mismatch"),
    ],
)
def test_rejects_fut_basic_fact_binding_drift(
    fact_update: dict[str, str], reason: str
) -> None:
    snapshot = _fut_basic()
    drifted = replace(
        snapshot,
        facts=(replace(snapshot.facts[0], **fact_update), *snapshot.facts[1:]),
    )

    with pytest.raises(MSimulationReadinessProjectionError, match=reason):
        _project(fut_basic=drifted)


@pytest.mark.parametrize(
    ("fact_update", "reason"),
    [
        ({"receipt_id": "receipt:other"}, "fut_settle_fact_receipt_mismatch"),
        ({"lineage_sha256": _sha256("other-lineage")}, "fut_settle_fact_lineage_mismatch"),
    ],
)
def test_rejects_fut_settle_fact_binding_drift(
    fact_update: dict[str, str], reason: str
) -> None:
    snapshot = _fut_settle()
    drifted = replace(
        snapshot,
        facts=(replace(snapshot.facts[0], **fact_update),),
    )

    with pytest.raises(MSimulationReadinessProjectionError, match=reason):
        _project(fut_settle=drifted)


def test_rejects_fut_settle_fact_trade_date_drift() -> None:
    snapshot = _fut_settle()
    drifted = replace(
        snapshot,
        facts=(replace(snapshot.facts[0], trade_date="20260804"),),
    )

    with pytest.raises(MSimulationReadinessProjectionError, match="fut_settle_fact_trade_date_mismatch"):
        _project(fut_settle=drifted)


@pytest.mark.parametrize(
    ("snapshot_update", "fact_update", "reason"),
    [
        ({"dataset_id": "cn.dataset.other"}, {}, "fut_mapping_dataset_invalid"),
        ({"trade_date": "20260804"}, {}, "fut_mapping_trade_date_invalid"),
        ({"receipt_id": "receipt:other"}, {}, "fut_mapping_fact_receipt_mismatch"),
        ({"lineage_sha256": _sha256("other-lineage")}, {}, "fut_mapping_fact_lineage_mismatch"),
        ({}, {"ts_code": "RB.DCE"}, "fut_mapping_fact_identity_invalid"),
    ],
)
def test_rejects_current_mapping_snapshot_binding_drift(
    snapshot_update: dict[str, object], fact_update: dict[str, object], reason: str
) -> None:
    snapshot = _fut_mapping()
    fact = replace(snapshot.facts[0], **fact_update)
    object.__setattr__(snapshot, "facts", (fact,))
    for field, value in snapshot_update.items():
        object.__setattr__(snapshot, field, value)

    with pytest.raises(MSimulationReadinessProjectionError, match=reason):
        _project(fut_mapping=snapshot)


@pytest.mark.parametrize(
    ("snapshot_update", "fact_update", "reason"),
    [
        ({"receipt_id": "receipt:other"}, {}, "ft_limit_fact_receipt_mismatch"),
        ({"lineage_sha256": _sha256("other-lineage")}, {}, "ft_limit_fact_lineage_mismatch"),
        ({"schema_major": 2}, {}, "ft_limit_catalog_invalid"),
        ({"trade_date": "20260804"}, {}, "ft_limit_trade_date_invalid"),
        ({"page_count": 8}, {}, "ft_limit_pagination_contract_invalid"),
        ({"terminal_pagination": False}, {}, "ft_limit_pagination_contract_invalid"),
        ({"replay_verified": False}, {}, "ft_limit_pagination_contract_invalid"),
        ({"state": "ready"}, {}, "ft_limit_metadata_contract_invalid"),
        ({"degraded": False}, {}, "ft_limit_metadata_contract_invalid"),
        ({"reason": "other_reason"}, {}, "ft_limit_metadata_contract_invalid"),
        ({}, {"receipt_id": "receipt:other"}, "ft_limit_fact_receipt_mismatch"),
        ({}, {"lineage_sha256": _sha256("other-lineage")}, "ft_limit_fact_lineage_mismatch"),
        ({}, {"trade_date": "20260804"}, "ft_limit_fact_identity_invalid"),
        ({}, {"raw_values": {"up_limit": 3000.0}}, "ft_limit_raw_fact_invalid"),
        ({"numeric_tick_authority": True}, {}, "ft_limit_authority_invalid"),
        ({}, {"numeric_tick_authority": True}, "ft_limit_fact_authority_invalid"),
    ],
)
def test_rejects_current_ft_limit_snapshot_binding_and_authority_drift(
    snapshot_update: dict[str, object], fact_update: dict[str, object], reason: str
) -> None:
    snapshot = _ft_limit()
    fact = snapshot.facts[0]
    for field, value in fact_update.items():
        object.__setattr__(fact, field, value)
    object.__setattr__(snapshot, "facts", (fact, *snapshot.facts[1:]))
    for field, value in snapshot_update.items():
        object.__setattr__(snapshot, field, value)

    with pytest.raises(MSimulationReadinessProjectionError, match=reason):
        _project(ft_limit=snapshot)


def test_rejects_non_snapshot_ft_limit_input() -> None:
    with pytest.raises(TypeError, match="ft_limit must be FutLimitCurrentSnapshot"):
        _project(ft_limit=object())
