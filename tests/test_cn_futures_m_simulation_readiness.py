from __future__ import annotations

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
from CNFutures.m_simulation_readiness import (
    DayNightFixtureAuthority,
    FtLimitCoverageEvidence,
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


def _ft_limit() -> FtLimitCoverageEvidence:
    return FtLimitCoverageEvidence(
        dataset_id="cn.dataset.ft_limit",
        receipt_id="receipt:ft-limit",
        lineage_sha256=_sha256("ft-limit-lineage"),
        state="stale",
        degraded=True,
        raw_fact_ts_codes=("M001.DCE", "M002.DCE"),
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
    assert result.ft_limit_receipt_id == "receipt:ft-limit"
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


def test_rejects_non_m_or_duplicate_source_identity() -> None:
    invalid_limit = FtLimitCoverageEvidence(
        dataset_id="cn.dataset.ft_limit",
        receipt_id="receipt:ft-limit",
        lineage_sha256=_sha256("ft-limit-lineage"),
        state="stale",
        degraded=True,
        raw_fact_ts_codes=("RB2601.SHFE", "M001.DCE"),
    )

    with pytest.raises(MSimulationReadinessProjectionError, match="ft_limit_ts_code_invalid"):
        _project(ft_limit=invalid_limit)
