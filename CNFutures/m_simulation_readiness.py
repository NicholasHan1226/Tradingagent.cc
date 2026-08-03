"""Offline, fail-closed coverage projection for receipt-bound DCE/M facts.

This module deliberately accepts already-mapped consumer snapshots only.  It
does not construct a TradingDatas client, inspect a fixture session window, or
derive market authority from raw unit or quote fields.  Its output is a
deterministic coverage ledger, not a simulation, runtime, or execution gate.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from CNFutures.fut_basic_contract_units import FutBasicRawContractUnitSnapshot
from CNFutures.fut_settle_market_rules import FutSettleRawMarketRuleSnapshot


_M_DCE_TS_CODE = re.compile(r"^M[0-9]{3,4}\.DCE$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class MSimulationReadinessProjectionError(ValueError):
    """Raised when an injected raw-fact snapshot cannot form a safe ledger."""


@dataclass(frozen=True)
class FtLimitCoverageEvidence:
    """Offline metadata plus the DCE/M identities retained from ``ft_limit``.

    Price-limit values remain with the upstream receipt-bound fact.  This
    projection only records whether that raw fact is present and whether the
    supplied evidence is stale or degraded.
    """

    dataset_id: str
    receipt_id: str
    lineage_sha256: str
    state: str
    degraded: bool
    raw_fact_ts_codes: tuple[str, ...]


@dataclass(frozen=True)
class DayNightFixtureAuthority:
    """Explicit authority gaps from the accepted fixture-only session result.

    ``fixture_session_windows`` is retained only for caller traceability.  It
    is intentionally never interpreted as an exchange schedule or live
    authority.
    """

    fixture_only: bool
    numeric_tick_receipt_bound: bool
    live_session_receipt_bound: bool
    pit_rollover_receipt_bound: bool
    fixture_session_windows: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class MContractCoverageLedger:
    """One deterministic M-contract coverage record with no eligibility lift."""

    ts_code: str
    fut_basic_raw_unit_bound: bool
    fut_settle_raw_rule_bound: bool
    ft_limit_raw_fact_bound: bool
    reasons: tuple[str, ...]
    simulation_ready: bool = False
    runtime_eligible: bool = False
    execution_eligible: bool = False
    trading_eligible: bool = False

    def __post_init__(self) -> None:
        if (
            not _M_DCE_TS_CODE.fullmatch(self.ts_code)
            or not self.fut_basic_raw_unit_bound
            or not self.reasons
            or self.simulation_ready
            or self.runtime_eligible
            or self.execution_eligible
            or self.trading_eligible
        ):
            raise MSimulationReadinessProjectionError("contract_ledger_authority_invalid")


@dataclass(frozen=True)
class MSimulationReadinessProjection:
    """Receipt-bound source summary and per-contract, fail-closed ledger."""

    mode: str
    fixture_only: bool
    fut_basic_receipt_id: str
    fut_basic_lineage_sha256: str
    fut_settle_receipt_id: str
    fut_settle_lineage_sha256: str
    ft_limit_receipt_id: str
    ft_limit_lineage_sha256: str
    fut_basic_coverage_reason: str
    ft_limit_state: str
    ft_limit_degraded: bool
    contracts: tuple[MContractCoverageLedger, ...]
    simulation_ready: bool = False
    runtime_eligible: bool = False
    execution_eligible: bool = False
    trading_eligible: bool = False

    def __post_init__(self) -> None:
        if (
            self.mode != "m_contract_simulation_readiness_projection"
            or not self.fixture_only
            or not self.contracts
            or self.simulation_ready
            or self.runtime_eligible
            or self.execution_eligible
            or self.trading_eligible
            or tuple(sorted(item.ts_code for item in self.contracts))
            != tuple(item.ts_code for item in self.contracts)
        ):
            raise MSimulationReadinessProjectionError("projection_authority_invalid")


def project_m_simulation_readiness(
    *,
    fut_basic: FutBasicRawContractUnitSnapshot,
    fut_settle: FutSettleRawMarketRuleSnapshot,
    ft_limit: FtLimitCoverageEvidence,
    day_night_fixture_authority: DayNightFixtureAuthority,
) -> MSimulationReadinessProjection:
    """Emit an offline M-contract ledger without inferring missing authority.

    The current accepted ``fut_basic`` cohort is explicitly partial and the
    supplied ``ft_limit`` evidence is stale/degraded.  Numeric tick, a live
    receipt-bound session, and PIT rollover must be supplied by later distinct
    authorities; a multiplier, quote text, fixture window, or current snapshot
    cannot satisfy any of those gaps here.
    """

    if not isinstance(fut_basic, FutBasicRawContractUnitSnapshot):
        raise TypeError("fut_basic must be FutBasicRawContractUnitSnapshot")
    if not isinstance(fut_settle, FutSettleRawMarketRuleSnapshot):
        raise TypeError("fut_settle must be FutSettleRawMarketRuleSnapshot")
    if not isinstance(ft_limit, FtLimitCoverageEvidence):
        raise TypeError("ft_limit must be FtLimitCoverageEvidence")
    if not isinstance(day_night_fixture_authority, DayNightFixtureAuthority):
        raise TypeError("day_night_fixture_authority must be DayNightFixtureAuthority")

    _validate_fut_basic(fut_basic)
    _validate_fut_settle(fut_settle)
    _validate_ft_limit(ft_limit)
    _validate_fixture_authority(day_night_fixture_authority)

    settle_codes = _unique_m_codes(
        (fact.ts_code for fact in fut_settle.facts), "fut_settle_ts_code_invalid"
    )
    limit_codes = _unique_m_codes(ft_limit.raw_fact_ts_codes, "ft_limit_ts_code_invalid")
    reasons = _coverage_reasons(fut_basic=fut_basic, ft_limit=ft_limit)
    contracts = tuple(
        MContractCoverageLedger(
            ts_code=fact.ts_code,
            fut_basic_raw_unit_bound=True,
            fut_settle_raw_rule_bound=fact.ts_code in settle_codes,
            ft_limit_raw_fact_bound=fact.ts_code in limit_codes,
            reasons=reasons,
        )
        for fact in sorted(fut_basic.facts, key=lambda item: item.ts_code)
    )
    return MSimulationReadinessProjection(
        mode="m_contract_simulation_readiness_projection",
        fixture_only=True,
        fut_basic_receipt_id=fut_basic.receipt_id,
        fut_basic_lineage_sha256=fut_basic.lineage_sha256,
        fut_settle_receipt_id=fut_settle.receipt_id,
        fut_settle_lineage_sha256=fut_settle.lineage_sha256,
        ft_limit_receipt_id=ft_limit.receipt_id,
        ft_limit_lineage_sha256=ft_limit.lineage_sha256,
        fut_basic_coverage_reason=fut_basic.coverage_reason,
        ft_limit_state=ft_limit.state,
        ft_limit_degraded=ft_limit.degraded,
        contracts=contracts,
    )


def _validate_fut_basic(snapshot: FutBasicRawContractUnitSnapshot) -> None:
    _receipt_binding(snapshot.receipt_id, snapshot.lineage_sha256, "fut_basic")
    if (
        snapshot.state != "partial"
        or not snapshot.degraded
        or snapshot.coverage_complete
        or snapshot.coverage_reason != "response_completeness_unverified"
        or snapshot.as_of is not None
        or snapshot.pit_authority
        or snapshot.runtime_eligible
        or snapshot.execution_eligible
        or snapshot.trading_eligible
    ):
        raise MSimulationReadinessProjectionError("fut_basic_coverage_contract_invalid")
    _unique_m_codes((fact.ts_code for fact in snapshot.facts), "fut_basic_ts_code_invalid")


def _validate_fut_settle(snapshot: FutSettleRawMarketRuleSnapshot) -> None:
    _receipt_binding(snapshot.receipt_id, snapshot.lineage_sha256, "fut_settle")
    if (
        snapshot.as_of is not None
        or snapshot.pit_authority
        or snapshot.execution_eligible
        or not snapshot.terminal_pagination
        or not snapshot.replay_verified
    ):
        raise MSimulationReadinessProjectionError("fut_settle_coverage_contract_invalid")


def _validate_ft_limit(evidence: FtLimitCoverageEvidence) -> None:
    if evidence.dataset_id != "cn.dataset.ft_limit":
        raise MSimulationReadinessProjectionError("ft_limit_dataset_invalid")
    _receipt_binding(evidence.receipt_id, evidence.lineage_sha256, "ft_limit")
    if not isinstance(evidence.state, str) or not evidence.state.strip():
        raise MSimulationReadinessProjectionError("ft_limit_state_invalid")
    if not isinstance(evidence.degraded, bool):
        raise MSimulationReadinessProjectionError("ft_limit_degraded_invalid")


def _validate_fixture_authority(authority: DayNightFixtureAuthority) -> None:
    if authority.fixture_only is not True:
        raise MSimulationReadinessProjectionError("fixture_only_required")
    if authority.numeric_tick_receipt_bound:
        raise MSimulationReadinessProjectionError("numeric_tick_authority_unexpected")
    if authority.live_session_receipt_bound:
        raise MSimulationReadinessProjectionError("live_session_authority_unexpected")
    if authority.pit_rollover_receipt_bound:
        raise MSimulationReadinessProjectionError("pit_rollover_authority_unexpected")


def _coverage_reasons(
    *, fut_basic: FutBasicRawContractUnitSnapshot, ft_limit: FtLimitCoverageEvidence
) -> tuple[str, ...]:
    reasons: list[str] = []
    if not fut_basic.coverage_complete:
        reasons.append("fut_basic_coverage_incomplete")
    if ft_limit.state.strip().lower() != "ready" or ft_limit.degraded:
        reasons.append("ft_limit_stale_or_degraded")
    reasons.extend(
        (
            "numeric_tick_authority_missing",
            "receipt_bound_live_session_authority_missing",
            "pit_rollover_authority_missing",
        )
    )
    return tuple(reasons)


def _receipt_binding(receipt_id: object, lineage_sha256: object, source: str) -> None:
    if not isinstance(receipt_id, str) or not receipt_id.strip():
        raise MSimulationReadinessProjectionError(f"{source}_receipt_invalid")
    if not isinstance(lineage_sha256, str) or not _SHA256.fullmatch(lineage_sha256):
        raise MSimulationReadinessProjectionError(f"{source}_lineage_invalid")


def _unique_m_codes(values: object, reason: str) -> frozenset[str]:
    try:
        codes = tuple(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise MSimulationReadinessProjectionError(reason) from exc
    if not codes or any(not isinstance(code, str) or not _M_DCE_TS_CODE.fullmatch(code) for code in codes):
        raise MSimulationReadinessProjectionError(reason)
    if len(set(codes)) != len(codes):
        raise MSimulationReadinessProjectionError(reason)
    return frozenset(codes)


__all__ = [
    "DayNightFixtureAuthority",
    "FtLimitCoverageEvidence",
    "MContractCoverageLedger",
    "MSimulationReadinessProjection",
    "MSimulationReadinessProjectionError",
    "project_m_simulation_readiness",
]
