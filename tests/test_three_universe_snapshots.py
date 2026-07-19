from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import datetime, timedelta, timezone

import pytest

from shared.universe.snapshots import (
    CoverageAuthorityVerification,
    UniverseContractError,
    build_account_tradable_snapshot,
    build_coverage_receipt,
    build_market_context_snapshot,
    build_small_capital_feasible_snapshot,
    canonical_source_sha256,
)


AS_OF = datetime(2026, 7, 16, 1, 0, tzinfo=timezone.utc)
CAPITAL_OBSERVED_AT = datetime(2026, 7, 16, 0, 58, tzinfo=timezone.utc)
CAPITAL_LINEAGE = "ashare-sim-fresh-20260712-v1"


class _FixtureCoverageAuthorityVerifier:
    """Explicit local-fixture trust boundary; never a production authority."""

    def verify(
        self,
        *,
        count_rows,
        source_generation,
        source_receipt_id,
        source_sha256,
        taxonomy_id,
        taxonomy_version,
        taxonomy_sector_count,
        assessed_as_of,
    ) -> CoverageAuthorityVerification:
        del count_rows
        return CoverageAuthorityVerification(
            accepted=True,
            verifier_id="fixture-coverage-authority-verifier-v1",
            proof_id=f"fixture-proof:{source_receipt_id}:{source_sha256}",
            source_generation=source_generation,
            source_receipt_id=source_receipt_id,
            source_sha256=source_sha256,
            taxonomy_id=taxonomy_id,
            taxonomy_version=taxonomy_version,
            taxonomy_sector_count=taxonomy_sector_count,
            assessed_as_of=assessed_as_of.isoformat(),
            verified_at=(assessed_as_of - timedelta(minutes=5)).isoformat(),
        )


FIXTURE_COVERAGE_VERIFIER = _FixtureCoverageAuthorityVerifier()


class _OffsetFixtureCoverageAuthorityVerifier(_FixtureCoverageAuthorityVerifier):
    """Returns the same instant with an explicit China Standard Time offset."""

    def verify(self, **kwargs) -> CoverageAuthorityVerification:
        verification = super().verify(**kwargs)
        verified_at = datetime.fromisoformat(verification.verified_at).astimezone(
            timezone(timedelta(hours=8))
        )
        return replace(verification, verified_at=verified_at.isoformat())


OFFSET_FIXTURE_COVERAGE_VERIFIER = _OffsetFixtureCoverageAuthorityVerifier()


def _coverage_counts(
    *,
    chinext_observed: int = 120,
    star_observed: int = 80,
    sector_observed: int = 200,
) -> list[dict]:
    return [
        {
            "dimension_type": "board",
            "dimension_id": "mainboard",
            "expected_count": 1_000,
            "observed_count": 1_000,
        },
        {
            "dimension_type": "board",
            "dimension_id": "chinext",
            "expected_count": 120,
            "observed_count": chinext_observed,
        },
        {
            "dimension_type": "board",
            "dimension_id": "star",
            "expected_count": 80,
            "observed_count": star_observed,
        },
        {
            "dimension_type": "board",
            "dimension_id": "beijing",
            "expected_count": 50,
            "observed_count": 50,
        },
        {
            "dimension_type": "sector",
            "dimension_id": "sector:sw801080",
            "expected_count": 200,
            "observed_count": sector_observed,
        },
    ]


def _build_coverage(
    counts: list[dict] | None = None,
    *,
    valid_until: datetime | None = None,
    taxonomy_sector_count: int = 1,
    source_lineage: object = (
        "catalog:fixture-catalog-v1",
        "dataset:fixture-industry-membership-v1",
    ),
    source_authority_verifier=FIXTURE_COVERAGE_VERIFIER,
):
    rows = counts if counts is not None else _coverage_counts()
    return build_coverage_receipt(
        rows,
        as_of=AS_OF,
        taxonomy_id="sw-level1",
        taxonomy_version="2026-07-16-v1",
        taxonomy_sector_count=taxonomy_sector_count,
        membership_effective_at=AS_OF - timedelta(days=1),
        membership_available_at=AS_OF - timedelta(minutes=10),
        valid_until=valid_until or AS_OF + timedelta(days=1),
        source_generation=7,
        source_receipt_id="ss-receipt-market-membership-20260716",
        source_lineage=source_lineage,
        source_sha256=canonical_source_sha256(rows),
        source_authority_verifier=source_authority_verifier,
    )


def _build_feasible(
    account,
    market_rows: list[dict],
    *,
    available_cash_cny: float = 40_000.0,
    simulated_equity_cny: float = 40_000.0,
):
    verified_rows = [
        {**row, "data_quality": row.get("data_quality", "ready")} for row in market_rows
    ]
    return build_small_capital_feasible_snapshot(
        account,
        verified_rows,
        source_sha256=canonical_source_sha256(verified_rows),
        capital_authority_id="ashare-capital-v1",
        capital_authority_generation=1,
        execution_lineage_id=CAPITAL_LINEAGE,
        capital_observed_at=CAPITAL_OBSERVED_AT,
        available_cash_cny=available_cash_cny,
        simulated_equity_cny=simulated_equity_cny,
        authorized_capital_ceiling_cny=50_000.0,
        cost_policy_id="ashare-research-cost-v1",
        execution_reality_model_version="ashare-execution-reality-20260706-v1",
    )


def test_missing_market_data_quality_never_defaults_to_ready() -> None:
    assets = [{"symbol": "600000.SH", "instrument_type": "common_stock"}]
    account = build_account_tradable_snapshot(
        assets,
        as_of=AS_OF,
        source_sha256=canonical_source_sha256(assets),
    )
    market_rows = [
        {
            "symbol": "600000.SH",
            "decision_reference_price": 20.0,
            "price_observed_at": "2026-07-16T00:59:00+00:00",
            "available_at": "2026-07-16T00:59:30+00:00",
            "revision_id": "r1",
            "receipt_id": "receipt-600000-r1",
            "listing_status": "listed",
            "risk_warning": False,
            "suspended": False,
            "adv20_cny": 1e8,
        }
    ]

    snapshot = build_small_capital_feasible_snapshot(
        account,
        market_rows,
        source_sha256=canonical_source_sha256(market_rows),
        capital_authority_id="ashare-capital-v1",
        capital_authority_generation=1,
        execution_lineage_id=CAPITAL_LINEAGE,
        capital_observed_at=CAPITAL_OBSERVED_AT,
        available_cash_cny=40_000.0,
        simulated_equity_cny=40_000.0,
        authorized_capital_ceiling_cny=50_000.0,
        cost_policy_id="ashare-research-cost-v1",
        execution_reality_model_version="ashare-execution-reality-20260706-v1",
    )

    assert snapshot.symbols == ()
    assert snapshot.exclusions[0].reason_code == "market_data_quality_missing"


def _dataclass_content_hash(value, *, hash_field: str) -> str:
    payload = asdict(value)
    payload.pop(hash_field)
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _dataclass_snapshot_hash(snapshot) -> str:
    return _dataclass_content_hash(snapshot, hash_field="snapshot_sha256")


def test_market_context_accepts_growth_indices_and_full_market_sector_aggregate() -> (
    None
):
    rows = [
        {"entity_id": "399006.SZ", "instrument_type": "index"},
        {"entity_id": "000688.SH", "instrument_type": "index"},
        {
            "entity_id": "sector:sw801080",
            "instrument_type": "sector_aggregate",
        },
    ]
    coverage = _build_coverage()
    snapshot = build_market_context_snapshot(
        rows,
        as_of=AS_OF,
        source_sha256=canonical_source_sha256(rows),
        coverage_receipt=coverage,
        coverage_authority_verifier=FIXTURE_COVERAGE_VERIFIER,
    )

    assert snapshot.contract_id == "tradingagent.universe_scope.v1"
    assert coverage.coverage_scope == "full_market"
    assert coverage.coverage_ratio == 1.0
    assert coverage.board_coverage_ratio == 1.0
    assert coverage.sector_coverage_ratio == 1.0
    assert snapshot.coverage_scope == "full_market"
    assert snapshot.coverage_receipt_sha256 == coverage.receipt_sha256
    assert snapshot.degraded is False
    assert snapshot.reason_codes == ()
    assert len(snapshot.observations) == 3
    assert all(row.context_only for row in snapshot.observations)
    assert all(row.order_identity is None for row in snapshot.observations)


def test_incomplete_membership_cannot_masquerade_as_full_market_width() -> None:
    rows = [
        {"entity_id": "399006.SZ", "instrument_type": "index"},
        {"entity_id": "000688.SH", "instrument_type": "index"},
        {
            "entity_id": "sector:sw801080",
            "instrument_type": "sector_aggregate",
        },
    ]
    coverage = _build_coverage(_coverage_counts(chinext_observed=100))
    snapshot = build_market_context_snapshot(
        rows,
        as_of=AS_OF,
        source_sha256=canonical_source_sha256(rows),
        coverage_receipt=coverage,
        coverage_authority_verifier=FIXTURE_COVERAGE_VERIFIER,
    )

    assert snapshot.degraded is True
    assert snapshot.coverage_scope == "partial_market"
    assert "coverage_count_incomplete" in snapshot.reason_codes
    assert "full_market_coverage_missing" in snapshot.reason_codes


def test_market_context_requires_coverage_receipt_instead_of_caller_scope_claim() -> (
    None
):
    rows = [{"entity_id": "399006.SZ", "instrument_type": "index"}]

    with pytest.raises(UniverseContractError, match="coverage_receipt_required"):
        build_market_context_snapshot(
            rows,
            as_of=AS_OF,
            source_sha256=canonical_source_sha256(rows),
        )


def test_unverified_coverage_source_cannot_claim_full_market() -> None:
    """Internal hashes alone do not prove the denominator or receipt authority."""

    coverage = _build_coverage(source_authority_verifier=None)

    assert coverage.coverage_scope == "partial_market"
    assert coverage.degraded is True
    assert "coverage_source_authority_unverified" in coverage.reason_codes


def test_verified_receipt_requires_authority_reverification_when_consumed() -> None:
    rows = [
        {"entity_id": "399006.SZ", "instrument_type": "index"},
        {"entity_id": "000688.SH", "instrument_type": "index"},
        {"entity_id": "sector:sw801080", "instrument_type": "sector_aggregate"},
    ]

    snapshot = build_market_context_snapshot(
        rows,
        as_of=AS_OF,
        source_sha256=canonical_source_sha256(rows),
        coverage_receipt=_build_coverage(),
    )

    assert snapshot.coverage_scope == "partial_market"
    assert snapshot.degraded is True
    assert "coverage_source_authority_unverified" in snapshot.reason_codes


def test_authority_time_comparison_uses_instants_not_iso_text_order() -> None:
    rows = [
        {"entity_id": "399006.SZ", "instrument_type": "index"},
        {"entity_id": "000688.SH", "instrument_type": "index"},
        {"entity_id": "sector:sw801080", "instrument_type": "sector_aggregate"},
    ]
    coverage = _build_coverage(
        source_authority_verifier=OFFSET_FIXTURE_COVERAGE_VERIFIER
    )

    snapshot = build_market_context_snapshot(
        rows,
        as_of=AS_OF,
        source_sha256=canonical_source_sha256(rows),
        coverage_receipt=coverage,
        coverage_authority_verifier=OFFSET_FIXTURE_COVERAGE_VERIFIER,
    )

    assert snapshot.coverage_scope == "full_market"
    assert snapshot.degraded is False


def test_stale_or_count_anomalous_coverage_is_degraded_fail_closed() -> None:
    stale = _build_coverage(valid_until=AS_OF - timedelta(seconds=1))
    anomalous = _build_coverage(_coverage_counts(star_observed=81))

    assert stale.coverage_scope == "partial_market"
    assert stale.degraded is True
    assert "coverage_receipt_stale" in stale.reason_codes
    assert anomalous.coverage_scope == "partial_market"
    assert anomalous.degraded is True
    assert anomalous.coverage_ratio == 0.0
    assert "coverage_count_anomaly" in anomalous.reason_codes


def test_missing_sector_membership_or_dual_innovation_context_is_degraded() -> None:
    board_only = [row for row in _coverage_counts() if row["dimension_type"] == "board"]
    missing_sector = _build_coverage(board_only, taxonomy_sector_count=1)
    assert missing_sector.coverage_scope == "partial_market"
    assert "taxonomy_sector_membership_gap" in missing_sector.reason_codes

    rows = [
        {"entity_id": "399006.SZ", "instrument_type": "index"},
        {
            "entity_id": "sector:sw801080",
            "instrument_type": "sector_aggregate",
        },
    ]
    snapshot = build_market_context_snapshot(
        rows,
        as_of=AS_OF,
        source_sha256=canonical_source_sha256(rows),
        coverage_receipt=_build_coverage(),
        coverage_authority_verifier=FIXTURE_COVERAGE_VERIFIER,
    )

    assert snapshot.coverage_scope == "partial_market"
    assert snapshot.degraded is True
    assert "star_aggregate_context_missing" in snapshot.reason_codes


def test_market_context_must_materialize_every_sector_bound_by_coverage_receipt() -> (
    None
):
    counts = _coverage_counts() + [
        {
            "dimension_type": "sector",
            "dimension_id": "sector:sw801150",
            "expected_count": 75,
            "observed_count": 75,
        }
    ]
    coverage = _build_coverage(counts, taxonomy_sector_count=2)
    rows = [
        {"entity_id": "399006.SZ", "instrument_type": "index"},
        {"entity_id": "000688.SH", "instrument_type": "index"},
        {
            "entity_id": "sector:sw801080",
            "instrument_type": "sector_aggregate",
        },
    ]

    snapshot = build_market_context_snapshot(
        rows,
        as_of=AS_OF,
        source_sha256=canonical_source_sha256(rows),
        coverage_receipt=coverage,
        coverage_authority_verifier=FIXTURE_COVERAGE_VERIFIER,
    )

    assert snapshot.coverage_scope == "partial_market"
    assert snapshot.degraded is True
    assert "taxonomy_sector_context_gap" in snapshot.reason_codes


def test_coverage_receipt_is_content_addressed_immutable_and_tamper_detected() -> None:
    coverage = _build_coverage()
    assert coverage.receipt_sha256 == _dataclass_content_hash(
        coverage,
        hash_field="receipt_sha256",
    )
    assert {row.dimension_id for row in coverage.board_counts} == {
        "mainboard",
        "chinext",
        "star",
        "beijing",
    }
    assert coverage.sector_counts[0].dimension_id == "sector:sw801080"
    with pytest.raises(FrozenInstanceError):
        coverage.coverage_scope = "partial_market"  # type: ignore[misc]

    rows = [
        {"entity_id": "399006.SZ", "instrument_type": "index"},
        {"entity_id": "000688.SH", "instrument_type": "index"},
        {
            "entity_id": "sector:sw801080",
            "instrument_type": "sector_aggregate",
        },
    ]
    tampered = replace(coverage, coverage_scope="partial_market")
    with pytest.raises(UniverseContractError, match="coverage_receipt_sha256_mismatch"):
        build_market_context_snapshot(
            rows,
            as_of=AS_OF,
            source_sha256=canonical_source_sha256(rows),
            coverage_receipt=tampered,
            coverage_authority_verifier=FIXTURE_COVERAGE_VERIFIER,
        )


def test_rehashed_authority_proof_tamper_still_fails_external_reverification() -> None:
    coverage = _build_coverage()
    tampered = replace(coverage, source_authority_proof_id="attacker-replaced-proof")
    tampered = replace(
        tampered,
        receipt_sha256=_dataclass_content_hash(
            tampered,
            hash_field="receipt_sha256",
        ),
    )
    rows = [
        {"entity_id": "399006.SZ", "instrument_type": "index"},
        {"entity_id": "000688.SH", "instrument_type": "index"},
        {"entity_id": "sector:sw801080", "instrument_type": "sector_aggregate"},
    ]

    snapshot = build_market_context_snapshot(
        rows,
        as_of=AS_OF,
        source_sha256=canonical_source_sha256(rows),
        coverage_receipt=tampered,
        coverage_authority_verifier=FIXTURE_COVERAGE_VERIFIER,
    )

    assert snapshot.coverage_scope == "partial_market"
    assert snapshot.degraded is True
    assert "coverage_source_authority_unverified" in snapshot.reason_codes


def test_coverage_lineage_requires_an_explicit_sequence_not_a_string() -> None:
    with pytest.raises(
        UniverseContractError,
        match="coverage_source_lineage_invalid",
    ):
        _build_coverage(source_lineage="abc")


@pytest.mark.parametrize(
    "row",
    [
        {"entity_id": "300750.SZ", "instrument_type": "common_stock"},
        {"entity_id": "688981.SH", "instrument_type": "common_stock"},
        {"entity_id": "", "instrument_type": "sector_aggregate"},
        {"entity_id": True, "instrument_type": "index"},
        {"entity_id": "not-an-index", "instrument_type": "index"},
    ],
)
def test_invalid_or_individual_growth_objects_cannot_enter_context(row: dict) -> None:
    with pytest.raises(UniverseContractError, match="context_object_not_allowed"):
        build_market_context_snapshot(
            [row],
            as_of=AS_OF,
            source_sha256=canonical_source_sha256([row]),
            coverage_receipt=_build_coverage(),
            coverage_authority_verifier=FIXTURE_COVERAGE_VERIFIER,
        )


def test_account_tradable_snapshot_is_mainboard_only_and_records_rejections() -> None:
    rows = [
        {"symbol": "600000.SH", "instrument_type": "common_stock"},
        {"symbol": "000001.SZ", "instrument_type": "common_stock"},
        {"symbol": "300750.SZ", "instrument_type": "common_stock"},
        {"symbol": "000688.SH", "instrument_type": "index"},
        {"symbol": "510300.SH", "instrument_type": "etf"},
    ]
    snapshot = build_account_tradable_snapshot(
        rows,
        as_of=AS_OF,
        source_sha256=canonical_source_sha256(rows),
    )

    assert snapshot.symbols == ("000001.SZ", "600000.SH")
    assert snapshot.access_policy_id == "ashare-simulated-mainboard-access-v1"
    assert snapshot.access_semantics == "policy_allowed_not_broker_verified"
    assert snapshot.broker_permission_status == "not_verified"
    assert snapshot.broker_permission_verified is False
    assert snapshot.real_trading_enabled is False
    assert snapshot.simulation_only is True
    assert {row.symbol for row in snapshot.exclusions} == {
        "300750.SZ",
        "000688.SH",
        "510300.SH",
    }
    assert all(row.reason_code for row in snapshot.exclusions)
    with pytest.raises(FrozenInstanceError):
        snapshot.contract_id = "changed"  # type: ignore[misc]


def test_snapshot_hash_binds_exact_public_contract_fields() -> None:
    context_rows = [
        {"entity_id": "399006.SZ", "instrument_type": "index"},
        {"entity_id": "sector:sw801080", "instrument_type": "sector_aggregate"},
    ]
    context = build_market_context_snapshot(
        context_rows,
        as_of=AS_OF,
        source_sha256=canonical_source_sha256(context_rows),
        coverage_receipt=_build_coverage(),
        coverage_authority_verifier=FIXTURE_COVERAGE_VERIFIER,
    )
    account_rows = [{"symbol": "600000.SH", "instrument_type": "common_stock"}]
    account = build_account_tradable_snapshot(
        account_rows,
        as_of=AS_OF,
        source_sha256=canonical_source_sha256(account_rows),
    )
    market_rows = [
        {
            "symbol": "600000.SH",
            "decision_reference_price": 20.0,
            "price_observed_at": "2026-07-16T00:59:00+00:00",
            "available_at": "2026-07-16T00:59:30+00:00",
            "revision_id": "r1",
            "receipt_id": "receipt-600000-r1",
            "listing_status": "listed",
            "risk_warning": False,
            "suspended": False,
            "adv20_cny": 1e8,
        }
    ]
    feasible = _build_feasible(account, market_rows)

    assert context.snapshot_sha256 == _dataclass_snapshot_hash(context)
    assert account.snapshot_sha256 == _dataclass_snapshot_hash(account)
    assert feasible.snapshot_sha256 == _dataclass_snapshot_hash(feasible)


def test_small_capital_feasible_snapshot_adds_only_constraints() -> None:
    assets = [
        {"symbol": "600000.SH", "instrument_type": "common_stock"},
        {"symbol": "600519.SH", "instrument_type": "common_stock"},
        {"symbol": "000001.SZ", "instrument_type": "common_stock"},
    ]
    account = build_account_tradable_snapshot(
        assets,
        as_of=AS_OF,
        source_sha256=canonical_source_sha256(assets),
    )
    market_rows = [
        {
            "symbol": "600000.SH",
            "decision_reference_price": 20.0,
            "price_observed_at": "2026-07-16T00:59:00+00:00",
            "available_at": "2026-07-16T00:59:30+00:00",
            "revision_id": "r1",
            "receipt_id": "receipt-600000-r1",
            "listing_status": "listed",
            "risk_warning": False,
            "suspended": False,
            "adv20_cny": 1e8,
        },
        {
            "symbol": "600519.SH",
            "decision_reference_price": 80.0,
            "price_observed_at": "2026-07-16T00:59:00+00:00",
            "available_at": "2026-07-16T00:59:30+00:00",
            "revision_id": "r1",
            "receipt_id": "receipt-600519-r1",
            "listing_status": "listed",
            "risk_warning": False,
            "suspended": False,
            "adv20_cny": 1e8,
        },
        {
            "symbol": "000001.SZ",
            "decision_reference_price": 5.0,
            "price_observed_at": "2026-07-16T00:59:00+00:00",
            "available_at": "2026-07-16T00:59:30+00:00",
            "revision_id": "r1",
            "receipt_id": "receipt-000001-r1",
            "listing_status": "listed",
            "risk_warning": False,
            "suspended": False,
            "adv20_cny": 1e8,
        },
    ]
    feasible = _build_feasible(account, market_rows)

    assert feasible.symbols == ("000001.SZ", "600000.SH")
    assert set(feasible.symbols).issubset(account.symbols)
    assert feasible.capital_authority_id == "ashare-capital-v1"
    assert feasible.capital_authority_generation == 1
    assert feasible.capital_observed_at == CAPITAL_OBSERVED_AT.isoformat()
    assert feasible.available_cash_cny == 40_000.0
    assert feasible.simulated_equity_cny == 40_000.0
    assert feasible.authorized_capital_ceiling_cny == 50_000.0
    assert feasible.risk_capital_base_cny == 40_000.0
    assert feasible.single_name_cap_cny == 6_000.0
    assert feasible.single_name_max_pct == 0.15
    assert feasible.minimum_economic_order_cny == 2_000.0
    assert feasible.max_adv_participation_pct == 0.01
    assert feasible.lot_size_shares == 100
    assert feasible.cost_policy_id == "ashare-research-cost-v1"
    assert (
        feasible.execution_reality_model_version
        == "ashare-execution-reality-20260706-v1"
    )
    assert feasible.real_trading_enabled is False
    assert feasible.broker_permission_status == "not_verified"
    assert feasible.position_state_applied is False
    assert feasible.entries[0].minimum_economic_shares % 100 == 0
    assert all(row.max_buyable_shares % 100 == 0 for row in feasible.entries)
    assert all(
        row.max_buyable_shares >= row.minimum_economic_shares
        for row in feasible.entries
    )
    assert all(row.one_lot_buy_fee_cny > 5.0 for row in feasible.entries)
    assert all(
        row.one_lot_round_trip_cost_cny > row.one_lot_buy_fee_cny
        for row in feasible.entries
    )
    assert all(
        row.minimum_economic_round_trip_cost_cny > row.minimum_economic_buy_fee_cny
        for row in feasible.entries
    )
    assert feasible.exclusions[0].symbol == "600519.SH"
    assert feasible.exclusions[0].reason_code == "lot_not_affordable"


def test_small_capital_rejects_tampered_or_self_signed_account_universe() -> None:
    assets = [{"symbol": "600000.SH", "instrument_type": "common_stock"}]
    account = build_account_tradable_snapshot(
        assets,
        as_of=AS_OF,
        source_sha256=canonical_source_sha256(assets),
    )
    growth_market_rows = [
        {
            "symbol": "300750.SZ",
            "decision_reference_price": 20.0,
            "price_observed_at": "2026-07-16T00:59:00+00:00",
            "available_at": "2026-07-16T00:59:30+00:00",
            "revision_id": "r1",
            "receipt_id": "receipt-300750-r1",
            "listing_status": "listed",
            "risk_warning": False,
            "suspended": False,
            "adv20_cny": 1e8,
        }
    ]
    tampered = replace(account, symbols=("300750.SZ",))

    with pytest.raises(UniverseContractError, match="account_snapshot_sha256_mismatch"):
        _build_feasible(tampered, growth_market_rows)

    self_signed = replace(
        tampered,
        snapshot_sha256=_dataclass_snapshot_hash(tampered),
    )
    with pytest.raises(
        UniverseContractError,
        match="simulated_access_snapshot_symbol_invalid",
    ):
        _build_feasible(self_signed, growth_market_rows)


def test_loss_shrinks_risk_base_and_profit_does_not_expand_authorized_ceiling() -> None:
    assets = [{"symbol": "600519.SH", "instrument_type": "common_stock"}]
    account = build_account_tradable_snapshot(
        assets,
        as_of=AS_OF,
        source_sha256=canonical_source_sha256(assets),
    )
    market_rows = [
        {
            "symbol": "600519.SH",
            "decision_reference_price": 70.0,
            "price_observed_at": "2026-07-16T00:59:00+00:00",
            "available_at": "2026-07-16T00:59:30+00:00",
            "revision_id": "r1",
            "receipt_id": "receipt-600519-r1",
            "listing_status": "listed",
            "risk_warning": False,
            "suspended": False,
            "adv20_cny": 1e8,
        }
    ]

    loss_snapshot = _build_feasible(
        account,
        market_rows,
        available_cash_cny=40_000.0,
        simulated_equity_cny=40_000.0,
    )
    profit_snapshot = _build_feasible(
        account,
        market_rows,
        available_cash_cny=60_000.0,
        simulated_equity_cny=60_000.0,
    )

    assert loss_snapshot.risk_capital_base_cny == 40_000.0
    assert loss_snapshot.single_name_cap_cny == 6_000.0
    assert loss_snapshot.symbols == ()
    assert loss_snapshot.exclusions[0].reason_code == "lot_not_affordable"
    assert profit_snapshot.risk_capital_base_cny == 50_000.0
    assert profit_snapshot.single_name_cap_cny == 7_500.0
    assert profit_snapshot.symbols == ("600519.SH",)
    assert profit_snapshot.entries[0].max_buyable_shares == 100


def test_max_buyable_shares_respects_cash_cap_and_buy_transfer_fee() -> None:
    assets = [{"symbol": "600000.SH", "instrument_type": "common_stock"}]
    account = build_account_tradable_snapshot(
        assets,
        as_of=AS_OF,
        source_sha256=canonical_source_sha256(assets),
    )
    market_rows = [
        {
            "symbol": "600000.SH",
            "decision_reference_price": 20.0,
            "price_observed_at": "2026-07-16T00:59:00+00:00",
            "available_at": "2026-07-16T00:59:30+00:00",
            "revision_id": "r1",
            "receipt_id": "receipt-600000-r1",
            "listing_status": "listed",
            "risk_warning": False,
            "suspended": False,
            "adv20_cny": 1e8,
        }
    ]

    snapshot = _build_feasible(
        account,
        market_rows,
        available_cash_cny=6_000.0,
        simulated_equity_cny=40_000.0,
    )

    entry = snapshot.entries[0]
    assert entry.max_buyable_shares == 200
    assert entry.max_buyable_notional_cny == 4_000.0
    assert entry.max_buy_cash_required_cny > 4_005.0
    assert entry.max_buy_cash_required_cny <= 6_000.0
    assert entry.one_lot_buy_fee_cny == pytest.approx(5.02)
    assert entry.one_lot_cash_required_cny == pytest.approx(2_005.02)
    assert snapshot.max_buyable_semantics == (
        "cash_and_single_name_upper_bound_before_position_check"
    )
    assert snapshot.position_state_applied is False


def test_small_capital_contract_rejects_implicit_or_mismatched_authority_and_cost() -> (
    None
):
    assets = [{"symbol": "600000.SH", "instrument_type": "common_stock"}]
    account = build_account_tradable_snapshot(
        assets,
        as_of=AS_OF,
        source_sha256=canonical_source_sha256(assets),
    )
    market_rows = [
        {
            "symbol": "600000.SH",
            "decision_reference_price": 20.0,
            "price_observed_at": "2026-07-16T00:59:00+00:00",
            "available_at": "2026-07-16T00:59:30+00:00",
            "revision_id": "r1",
            "receipt_id": "receipt-600000-r1",
            "listing_status": "listed",
            "risk_warning": False,
            "suspended": False,
            "adv20_cny": 1e8,
        }
    ]
    base = {
        "source_sha256": canonical_source_sha256(market_rows),
        "capital_authority_id": "ashare-capital-v1",
        "capital_authority_generation": 1,
        "execution_lineage_id": CAPITAL_LINEAGE,
        "capital_observed_at": CAPITAL_OBSERVED_AT,
        "available_cash_cny": 40_000.0,
        "simulated_equity_cny": 40_000.0,
        "authorized_capital_ceiling_cny": 50_000.0,
        "cost_policy_id": "ashare-research-cost-v1",
        "execution_reality_model_version": ("ashare-execution-reality-20260706-v1"),
    }

    with pytest.raises(UniverseContractError, match="capital_authority_id_invalid"):
        build_small_capital_feasible_snapshot(
            account,
            market_rows,
            **{**base, "capital_authority_id": ""},
        )
    with pytest.raises(UniverseContractError, match="capital_authority_id_mismatch"):
        build_small_capital_feasible_snapshot(
            account,
            market_rows,
            **{**base, "capital_authority_id": "cn-futures-capital-v1"},
        )
    with pytest.raises(
        UniverseContractError, match="capital_authority_generation_invalid"
    ):
        build_small_capital_feasible_snapshot(
            account,
            market_rows,
            **{**base, "capital_authority_generation": 0},
        )
    with pytest.raises(UniverseContractError, match="execution_lineage_id_invalid"):
        build_small_capital_feasible_snapshot(
            account,
            market_rows,
            **{**base, "execution_lineage_id": ""},
        )
    with pytest.raises(
        UniverseContractError, match="authorized_capital_ceiling_mismatch"
    ):
        build_small_capital_feasible_snapshot(
            account,
            market_rows,
            **{**base, "authorized_capital_ceiling_cny": 60_000.0},
        )
    with pytest.raises(UniverseContractError, match="cost_policy_id_mismatch"):
        build_small_capital_feasible_snapshot(
            account,
            market_rows,
            **{**base, "cost_policy_id": "broker-live-fee"},
        )
    with pytest.raises(
        UniverseContractError,
        match="execution_reality_model_version_mismatch",
    ):
        build_small_capital_feasible_snapshot(
            account,
            market_rows,
            **{**base, "execution_reality_model_version": "future-rules"},
        )
    with pytest.raises(
        UniverseContractError,
        match="minimum_economic_order_cny_invalid",
    ):
        build_small_capital_feasible_snapshot(
            account,
            market_rows,
            **base,
            minimum_economic_order_cny=0,
        )
    with pytest.raises(
        UniverseContractError,
        match="max_adv_participation_pct_invalid",
    ):
        build_small_capital_feasible_snapshot(
            account,
            market_rows,
            **base,
            max_adv_participation_pct=-0.01,
        )


def test_small_capital_scope_accepts_rotated_generation_and_lineage() -> None:
    assets = [{"symbol": "600000.SH", "instrument_type": "common_stock"}]
    account = build_account_tradable_snapshot(
        assets,
        as_of=AS_OF,
        source_sha256=canonical_source_sha256(assets),
    )
    market_rows = [
        {
            "symbol": "600000.SH",
            "decision_reference_price": 20.0,
            "price_observed_at": "2026-07-16T00:59:00+00:00",
            "available_at": "2026-07-16T00:59:30+00:00",
            "revision_id": "r1",
            "receipt_id": "receipt-600000-r1",
            "listing_status": "listed",
            "risk_warning": False,
            "suspended": False,
            "adv20_cny": 1e8,
            "data_quality": "ready",
        }
    ]
    rotated_lineage = "ashare-sim-rotated-generation-2"

    snapshot = build_small_capital_feasible_snapshot(
        account,
        market_rows,
        source_sha256=canonical_source_sha256(market_rows),
        capital_authority_id="ashare-capital-v1",
        capital_authority_generation=2,
        execution_lineage_id=rotated_lineage,
        capital_observed_at=CAPITAL_OBSERVED_AT,
        available_cash_cny=40_000.0,
        simulated_equity_cny=40_000.0,
        authorized_capital_ceiling_cny=50_000.0,
        cost_policy_id="ashare-research-cost-v1",
        execution_reality_model_version="ashare-execution-reality-20260706-v1",
    )

    assert snapshot.capital_authority_generation == 2
    assert snapshot.execution_lineage_id == rotated_lineage


def test_future_price_or_future_available_row_cannot_enter_decision_universe() -> None:
    assets = [{"symbol": "600000.SH", "instrument_type": "common_stock"}]
    account = build_account_tradable_snapshot(
        assets,
        as_of=AS_OF,
        source_sha256=canonical_source_sha256(assets),
    )
    base = {
        "symbol": "600000.SH",
        "decision_reference_price": 20.0,
        "price_observed_at": "2026-07-16T00:59:00+00:00",
        "available_at": "2026-07-16T00:59:30+00:00",
        "revision_id": "r1",
        "receipt_id": "receipt-600000-r1",
        "listing_status": "listed",
        "risk_warning": False,
        "suspended": False,
        "adv20_cny": 1e8,
    }

    with pytest.raises(UniverseContractError, match="future_price_field_forbidden"):
        row = {**base, "next_bar_open": 74.9}
        _build_feasible(account, [row])

    with pytest.raises(UniverseContractError, match="available_at_after_as_of"):
        row = {**base, "available_at": "2026-07-16T01:00:01+00:00"}
        _build_feasible(account, [row])


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("listing_status", "delisted", "listing_status_ineligible"),
        ("risk_warning", True, "risk_warning_ineligible"),
        ("suspended", True, "suspended_at_decision"),
    ],
)
def test_decision_time_dynamic_ineligibility_is_explicit(
    field: str,
    value: object,
    reason: str,
) -> None:
    assets = [{"symbol": "600000.SH", "instrument_type": "common_stock"}]
    account = build_account_tradable_snapshot(
        assets,
        as_of=AS_OF,
        source_sha256=canonical_source_sha256(assets),
    )
    row = {
        "symbol": "600000.SH",
        "decision_reference_price": 20.0,
        "price_observed_at": "2026-07-16T00:59:00+00:00",
        "available_at": "2026-07-16T00:59:30+00:00",
        "revision_id": "r1",
        "receipt_id": "receipt-600000-r1",
        "listing_status": "listed",
        "risk_warning": False,
        "suspended": False,
        "adv20_cny": 1e8,
        field: value,
    }
    snapshot = _build_feasible(account, [row])

    assert snapshot.symbols == ()
    assert snapshot.exclusions[0].reason_code == reason


def test_snapshot_hash_and_output_are_deterministic_across_input_order() -> None:
    rows = [
        {"symbol": "600000.SH", "instrument_type": "common_stock"},
        {"symbol": "000001.SZ", "instrument_type": "common_stock"},
    ]
    left = build_account_tradable_snapshot(
        rows,
        as_of=AS_OF,
        source_sha256=canonical_source_sha256(rows),
    )
    reversed_rows = list(reversed(rows))
    right = build_account_tradable_snapshot(
        reversed_rows,
        as_of=AS_OF,
        source_sha256=canonical_source_sha256(reversed_rows),
    )

    assert left.symbols == right.symbols
    assert left.snapshot_sha256 == right.snapshot_sha256
