"""Immutable market-context, simulated-access and small-capital snapshots.

The historical ``AccountTradable`` type name is retained as an API bridge, but
its contract is deliberately simulation-only.  It describes instruments
allowed by a local mainboard policy; it is not evidence of broker permission.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Protocol

from shared.execution.cost_policy import (
    ASHARE_RESEARCH_COST_POLICY_V1,
    commission,
    estimate_round_trip_cost,
    transfer_fee,
)

from .policy import POLICY_ID, classify_instrument


class UniverseContractError(ValueError):
    """Raised when an input could blur a universe or authority boundary."""


_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTEXT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SIMULATED_ACCESS_POLICY_ID = "ashare-simulated-mainboard-access-v1"
_CAPITAL_AUTHORITY_ID = "ashare-capital-v1"
_AUTHORIZED_CAPITAL_CEILING_CNY = 50_000.0
_COVERAGE_RECEIPT_CONTRACT_ID = "tradingagent.market_context_coverage.v1"
_REQUIRED_COVERAGE_BOARDS = frozenset({"mainboard", "chinext", "star", "beijing"})


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise UniverseContractError("source_rows_not_canonical_json") from exc


def canonical_source_sha256(rows: Iterable[Mapping[str, Any]]) -> str:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise UniverseContractError("source_row_must_be_mapping")
        normalized.append(dict(row))
    serialized_rows = sorted(_canonical_json(row) for row in normalized)
    return hashlib.sha256(_canonical_json(serialized_rows).encode("utf-8")).hexdigest()


def _source_hash(rows: tuple[Mapping[str, Any], ...], declared: object) -> str:
    if not isinstance(declared, str) or not _SHA_RE.fullmatch(declared):
        raise UniverseContractError("source_sha256_invalid")
    actual = canonical_source_sha256(rows)
    if actual != declared:
        raise UniverseContractError("source_sha256_mismatch")
    return actual


def _as_of(value: object) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise UniverseContractError("as_of_timezone_required")
    if value.utcoffset() is None:
        raise UniverseContractError("as_of_timezone_required")
    return value.isoformat()


def _text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise UniverseContractError(f"{field_name}_invalid")
    return value


def _snapshot_sha(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(dict(payload)).encode("utf-8")).hexdigest()


def _verify_snapshot_sha(snapshot: object, *, error_code: str) -> None:
    payload = asdict(snapshot)
    declared = payload.pop("snapshot_sha256", None)
    if not isinstance(declared, str) or not _SHA_RE.fullmatch(declared):
        raise UniverseContractError(error_code)
    if _snapshot_sha(payload) != declared:
        raise UniverseContractError(error_code)


def _aware_datetime(value: object, *, field_name: str) -> datetime:
    if isinstance(value, datetime):
        instant = value
    elif isinstance(value, str) and value and value == value.strip():
        try:
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise UniverseContractError(f"{field_name}_invalid") from exc
    else:
        raise UniverseContractError(f"{field_name}_invalid")
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise UniverseContractError(f"{field_name}_timezone_required")
    return instant


@dataclass(frozen=True)
class ContextObservation:
    entity_id: str
    role: str
    board: str
    context_only: bool = True
    order_identity: None = None


@dataclass(frozen=True)
class CoverageDimensionCount:
    dimension_type: str
    dimension_id: str
    expected_count: int
    observed_count: int
    coverage_ratio: float


@dataclass(frozen=True)
class CoverageAuthorityVerification:
    """Verifier-owned proof that coverage denominators came from an authority."""

    accepted: bool
    verifier_id: str
    proof_id: str
    source_generation: int
    source_receipt_id: str
    source_sha256: str
    taxonomy_id: str
    taxonomy_version: str
    taxonomy_sector_count: int
    assessed_as_of: str
    verified_at: str


class CoverageAuthorityVerifier(Protocol):
    """Injected trust boundary; TradingAgent deliberately provides no default."""

    def verify(
        self,
        *,
        count_rows: tuple[Mapping[str, Any], ...],
        source_generation: int,
        source_receipt_id: str,
        source_sha256: str,
        taxonomy_id: str,
        taxonomy_version: str,
        taxonomy_sector_count: int,
        assessed_as_of: datetime,
    ) -> CoverageAuthorityVerification: ...


@dataclass(frozen=True)
class CoverageReceipt:
    contract_id: str
    as_of: str
    taxonomy_id: str
    taxonomy_version: str
    taxonomy_sector_count: int
    membership_effective_at: str
    membership_available_at: str
    valid_until: str
    source_generation: int
    source_receipt_id: str
    source_lineage: tuple[str, ...]
    source_sha256: str
    source_authority_status: str
    source_authority_verifier_id: str | None
    source_authority_proof_id: str | None
    source_authority_verified_at: str | None
    board_counts: tuple[CoverageDimensionCount, ...]
    sector_counts: tuple[CoverageDimensionCount, ...]
    board_coverage_ratio: float
    sector_coverage_ratio: float
    coverage_ratio: float
    coverage_scope: str
    degraded: bool
    reason_codes: tuple[str, ...]
    receipt_sha256: str


@dataclass(frozen=True)
class MarketContextUniverseSnapshot:
    contract_id: str
    as_of: str
    source_sha256: str
    coverage_receipt_sha256: str
    coverage_scope: str
    degraded: bool
    reason_codes: tuple[str, ...]
    observations: tuple[ContextObservation, ...]
    snapshot_sha256: str


def _count_ratio(rows: tuple[CoverageDimensionCount, ...]) -> float:
    if any(row.observed_count > row.expected_count for row in rows):
        return 0.0
    expected = sum(row.expected_count for row in rows)
    if expected <= 0:
        return 0.0
    observed = sum(min(row.observed_count, row.expected_count) for row in rows)
    return round(observed / expected, 12)


def _derive_coverage_status(
    *,
    as_of: datetime,
    valid_until: datetime,
    taxonomy_sector_count: int,
    board_counts: tuple[CoverageDimensionCount, ...],
    sector_counts: tuple[CoverageDimensionCount, ...],
) -> tuple[float, float, float, str, tuple[str, ...]]:
    reasons: set[str] = set()
    board_by_id = {row.dimension_id: row for row in board_counts}
    if set(board_by_id) != _REQUIRED_COVERAGE_BOARDS:
        reasons.add("coverage_board_membership_gap")
    if len(sector_counts) != taxonomy_sector_count:
        reasons.add("taxonomy_sector_membership_gap")
    if taxonomy_sector_count <= 0 or not sector_counts:
        reasons.add("coverage_sector_missing")
    all_counts = board_counts + sector_counts
    if any(row.observed_count > row.expected_count for row in all_counts):
        reasons.add("coverage_count_anomaly")
    if any(row.observed_count < row.expected_count for row in all_counts):
        reasons.add("coverage_count_incomplete")
    for board, reason in (
        ("chinext", "chinext_aggregate_coverage_missing"),
        ("star", "star_aggregate_coverage_missing"),
    ):
        count = board_by_id.get(board)
        if count is None or count.observed_count != count.expected_count:
            reasons.add(reason)
    if valid_until < as_of:
        reasons.add("coverage_receipt_stale")
    board_ratio = _count_ratio(board_counts)
    sector_ratio = _count_ratio(sector_counts)
    overall_ratio = round(min(board_ratio, sector_ratio), 12)
    reason_codes = tuple(sorted(reasons))
    scope = "full_market" if not reason_codes else "partial_market"
    return board_ratio, sector_ratio, overall_ratio, scope, reason_codes


def _parse_coverage_counts(
    rows: tuple[Mapping[str, Any], ...],
) -> tuple[tuple[CoverageDimensionCount, ...], tuple[CoverageDimensionCount, ...]]:
    parsed: list[CoverageDimensionCount] = []
    seen: set[tuple[str, str]] = set()
    required_keys = {
        "dimension_type",
        "dimension_id",
        "expected_count",
        "observed_count",
    }
    for row in rows:
        if not isinstance(row, Mapping):
            raise UniverseContractError("coverage_count_row_must_be_mapping")
        if set(row) != required_keys:
            raise UniverseContractError("coverage_count_row_schema_invalid")
        dimension_type = row.get("dimension_type")
        if dimension_type not in {"board", "sector"}:
            raise UniverseContractError("coverage_dimension_type_invalid")
        dimension_id = _text(
            row.get("dimension_id"),
            field_name="coverage_dimension_id",
        )
        if not _CONTEXT_ID_RE.fullmatch(dimension_id):
            raise UniverseContractError("coverage_dimension_id_invalid")
        key = (dimension_type, dimension_id)
        if key in seen:
            raise UniverseContractError("duplicate_coverage_dimension")
        seen.add(key)
        expected = row.get("expected_count")
        observed = row.get("observed_count")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected <= 0:
            raise UniverseContractError("coverage_expected_count_invalid")
        if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
            raise UniverseContractError("coverage_observed_count_invalid")
        ratio = 0.0 if observed > expected else round(observed / expected, 12)
        parsed.append(
            CoverageDimensionCount(
                dimension_type=dimension_type,
                dimension_id=dimension_id,
                expected_count=expected,
                observed_count=observed,
                coverage_ratio=ratio,
            )
        )
    parsed.sort(key=lambda item: (item.dimension_type, item.dimension_id))
    return (
        tuple(row for row in parsed if row.dimension_type == "board"),
        tuple(row for row in parsed if row.dimension_type == "sector"),
    )


def build_coverage_receipt(
    count_rows: Iterable[Mapping[str, Any]],
    *,
    as_of: datetime,
    taxonomy_id: str,
    taxonomy_version: str,
    taxonomy_sector_count: int,
    membership_effective_at: datetime,
    membership_available_at: datetime,
    valid_until: datetime,
    source_generation: int,
    source_receipt_id: str,
    source_lineage: Iterable[str],
    source_sha256: str,
    source_authority_verifier: CoverageAuthorityVerifier | None = None,
) -> CoverageReceipt:
    raw_rows = tuple(count_rows)
    source_hash = _source_hash(raw_rows, source_sha256)
    as_of_text = _as_of(as_of)
    taxonomy_id_text = _text(taxonomy_id, field_name="taxonomy_id")
    taxonomy_version_text = _text(
        taxonomy_version,
        field_name="taxonomy_version",
    )
    if (
        isinstance(taxonomy_sector_count, bool)
        or not isinstance(taxonomy_sector_count, int)
        or taxonomy_sector_count <= 0
    ):
        raise UniverseContractError("taxonomy_sector_count_invalid")
    effective = _aware_datetime(
        membership_effective_at,
        field_name="membership_effective_at",
    )
    available = _aware_datetime(
        membership_available_at,
        field_name="membership_available_at",
    )
    valid_through = _aware_datetime(valid_until, field_name="valid_until")
    if effective > as_of:
        raise UniverseContractError("membership_effective_at_after_as_of")
    if available > as_of:
        raise UniverseContractError("membership_available_at_after_as_of")
    if valid_through < effective:
        raise UniverseContractError("valid_until_before_membership_effective_at")
    if (
        isinstance(source_generation, bool)
        or not isinstance(source_generation, int)
        or source_generation <= 0
    ):
        raise UniverseContractError("coverage_source_generation_invalid")
    source_receipt = _text(
        source_receipt_id,
        field_name="coverage_source_receipt_id",
    )
    if isinstance(source_lineage, (str, bytes)):
        raise UniverseContractError("coverage_source_lineage_invalid")
    try:
        lineage = tuple(
            _text(value, field_name="coverage_source_lineage")
            for value in source_lineage
        )
    except TypeError as exc:
        raise UniverseContractError("coverage_source_lineage_invalid") from exc
    if not lineage or len(lineage) != len(set(lineage)):
        raise UniverseContractError("coverage_source_lineage_invalid")
    board_counts, sector_counts = _parse_coverage_counts(raw_rows)

    authority_status = "external_verification_required"
    authority_verifier_id: str | None = None
    authority_proof_id: str | None = None
    authority_verified_at: str | None = None
    authority_reason: str | None = "coverage_source_authority_unverified"
    if source_authority_verifier is not None:
        callback = getattr(source_authority_verifier, "verify", None)
        if callback is None:
            raise UniverseContractError("coverage_source_authority_verifier_invalid")
        try:
            verification = callback(
                count_rows=raw_rows,
                source_generation=source_generation,
                source_receipt_id=source_receipt,
                source_sha256=source_hash,
                taxonomy_id=taxonomy_id_text,
                taxonomy_version=taxonomy_version_text,
                taxonomy_sector_count=taxonomy_sector_count,
                assessed_as_of=as_of,
            )
        except Exception as exc:
            raise UniverseContractError("coverage_source_authority_rejected") from exc
        if not isinstance(verification, CoverageAuthorityVerification):
            raise UniverseContractError(
                "coverage_source_authority_verification_invalid"
            )
        verified_at = _aware_datetime(
            verification.verified_at,
            field_name="coverage_source_authority_verified_at",
        )
        expected_verification = {
            "accepted": True,
            "source_generation": source_generation,
            "source_receipt_id": source_receipt,
            "source_sha256": source_hash,
            "taxonomy_id": taxonomy_id_text,
            "taxonomy_version": taxonomy_version_text,
            "taxonomy_sector_count": taxonomy_sector_count,
            "assessed_as_of": as_of_text,
        }
        actual_verification = {
            "accepted": verification.accepted,
            "source_generation": verification.source_generation,
            "source_receipt_id": verification.source_receipt_id,
            "source_sha256": verification.source_sha256,
            "taxonomy_id": verification.taxonomy_id,
            "taxonomy_version": verification.taxonomy_version,
            "taxonomy_sector_count": verification.taxonomy_sector_count,
            "assessed_as_of": verification.assessed_as_of,
        }
        if actual_verification != expected_verification:
            raise UniverseContractError("coverage_source_authority_rejected")
        if verified_at < available or verified_at > as_of:
            raise UniverseContractError("coverage_source_authority_time_invalid")
        authority_verifier_id = _text(
            verification.verifier_id,
            field_name="coverage_source_authority_verifier_id",
        )
        authority_proof_id = _text(
            verification.proof_id,
            field_name="coverage_source_authority_proof_id",
        )
        authority_verified_at = verified_at.isoformat()
        authority_status = "external_verified"
        authority_reason = None
    (
        board_ratio,
        sector_ratio,
        coverage_ratio,
        coverage_scope,
        reason_codes,
    ) = _derive_coverage_status(
        as_of=as_of,
        valid_until=valid_through,
        taxonomy_sector_count=taxonomy_sector_count,
        board_counts=board_counts,
        sector_counts=sector_counts,
    )
    if authority_reason is not None:
        reason_codes = tuple(sorted(set(reason_codes) | {authority_reason}))
        coverage_scope = "partial_market"
    payload = {
        "contract_id": _COVERAGE_RECEIPT_CONTRACT_ID,
        "as_of": as_of_text,
        "taxonomy_id": taxonomy_id_text,
        "taxonomy_version": taxonomy_version_text,
        "taxonomy_sector_count": taxonomy_sector_count,
        "membership_effective_at": effective.isoformat(),
        "membership_available_at": available.isoformat(),
        "valid_until": valid_through.isoformat(),
        "source_generation": source_generation,
        "source_receipt_id": source_receipt,
        "source_lineage": list(lineage),
        "source_sha256": source_hash,
        "source_authority_status": authority_status,
        "source_authority_verifier_id": authority_verifier_id,
        "source_authority_proof_id": authority_proof_id,
        "source_authority_verified_at": authority_verified_at,
        "board_counts": [asdict(item) for item in board_counts],
        "sector_counts": [asdict(item) for item in sector_counts],
        "board_coverage_ratio": board_ratio,
        "sector_coverage_ratio": sector_ratio,
        "coverage_ratio": coverage_ratio,
        "coverage_scope": coverage_scope,
        "degraded": bool(reason_codes),
        "reason_codes": list(reason_codes),
    }
    return CoverageReceipt(
        contract_id=_COVERAGE_RECEIPT_CONTRACT_ID,
        as_of=as_of_text,
        taxonomy_id=taxonomy_id_text,
        taxonomy_version=taxonomy_version_text,
        taxonomy_sector_count=taxonomy_sector_count,
        membership_effective_at=effective.isoformat(),
        membership_available_at=available.isoformat(),
        valid_until=valid_through.isoformat(),
        source_generation=source_generation,
        source_receipt_id=source_receipt,
        source_lineage=lineage,
        source_sha256=source_hash,
        source_authority_status=authority_status,
        source_authority_verifier_id=authority_verifier_id,
        source_authority_proof_id=authority_proof_id,
        source_authority_verified_at=authority_verified_at,
        board_counts=board_counts,
        sector_counts=sector_counts,
        board_coverage_ratio=board_ratio,
        sector_coverage_ratio=sector_ratio,
        coverage_ratio=coverage_ratio,
        coverage_scope=coverage_scope,
        degraded=bool(reason_codes),
        reason_codes=reason_codes,
        receipt_sha256=_snapshot_sha(payload),
    )


def _verify_coverage_receipt(
    receipt: CoverageReceipt,
    *,
    as_of: datetime,
) -> None:
    if not isinstance(receipt, CoverageReceipt):
        raise UniverseContractError("coverage_receipt_required")
    payload = asdict(receipt)
    declared = payload.pop("receipt_sha256", None)
    if (
        not isinstance(declared, str)
        or not _SHA_RE.fullmatch(declared)
        or _snapshot_sha(payload) != declared
    ):
        raise UniverseContractError("coverage_receipt_sha256_mismatch")
    if receipt.contract_id != _COVERAGE_RECEIPT_CONTRACT_ID:
        raise UniverseContractError("coverage_receipt_contract_mismatch")
    if receipt.as_of != _as_of(as_of):
        raise UniverseContractError("coverage_receipt_as_of_mismatch")
    if (
        isinstance(receipt.taxonomy_sector_count, bool)
        or not isinstance(receipt.taxonomy_sector_count, int)
        or receipt.taxonomy_sector_count <= 0
    ):
        raise UniverseContractError("coverage_receipt_semantics_invalid")
    _text(receipt.taxonomy_id, field_name="taxonomy_id")
    _text(receipt.taxonomy_version, field_name="taxonomy_version")
    if (
        isinstance(receipt.source_generation, bool)
        or not isinstance(receipt.source_generation, int)
        or receipt.source_generation <= 0
    ):
        raise UniverseContractError("coverage_receipt_semantics_invalid")
    _text(
        receipt.source_receipt_id,
        field_name="coverage_source_receipt_id",
    )
    if not receipt.source_lineage or len(receipt.source_lineage) != len(
        set(receipt.source_lineage)
    ):
        raise UniverseContractError("coverage_receipt_semantics_invalid")
    for value in receipt.source_lineage:
        _text(value, field_name="coverage_source_lineage")
    count_rows = tuple(
        {
            "dimension_type": item.dimension_type,
            "dimension_id": item.dimension_id,
            "expected_count": item.expected_count,
            "observed_count": item.observed_count,
        }
        for item in receipt.board_counts + receipt.sector_counts
    )
    rebuilt_board, rebuilt_sector = _parse_coverage_counts(count_rows)
    if rebuilt_board != receipt.board_counts or rebuilt_sector != receipt.sector_counts:
        raise UniverseContractError("coverage_receipt_semantics_invalid")
    if canonical_source_sha256(count_rows) != receipt.source_sha256:
        raise UniverseContractError("coverage_receipt_source_sha256_mismatch")
    effective = _aware_datetime(
        receipt.membership_effective_at,
        field_name="membership_effective_at",
    )
    available = _aware_datetime(
        receipt.membership_available_at,
        field_name="membership_available_at",
    )
    valid_through = _aware_datetime(receipt.valid_until, field_name="valid_until")
    if effective > as_of or available > as_of or valid_through < effective:
        raise UniverseContractError("coverage_receipt_semantics_invalid")
    derived = _derive_coverage_status(
        as_of=as_of,
        valid_until=valid_through,
        taxonomy_sector_count=receipt.taxonomy_sector_count,
        board_counts=receipt.board_counts,
        sector_counts=receipt.sector_counts,
    )
    derived_reasons = set(derived[4])
    if receipt.source_authority_status == "external_verification_required":
        if any(
            value is not None
            for value in (
                receipt.source_authority_verifier_id,
                receipt.source_authority_proof_id,
                receipt.source_authority_verified_at,
            )
        ):
            raise UniverseContractError("coverage_receipt_semantics_invalid")
        derived_reasons.add("coverage_source_authority_unverified")
    elif receipt.source_authority_status == "external_verified":
        _text(
            receipt.source_authority_verifier_id,
            field_name="coverage_source_authority_verifier_id",
        )
        _text(
            receipt.source_authority_proof_id,
            field_name="coverage_source_authority_proof_id",
        )
        verified_at = _aware_datetime(
            receipt.source_authority_verified_at,
            field_name="coverage_source_authority_verified_at",
        )
        if verified_at < available or verified_at > as_of:
            raise UniverseContractError("coverage_receipt_semantics_invalid")
    else:
        raise UniverseContractError("coverage_receipt_semantics_invalid")
    derived_reason_codes = tuple(sorted(derived_reasons))
    derived_scope = "full_market" if not derived_reason_codes else "partial_market"
    expected = (
        receipt.board_coverage_ratio,
        receipt.sector_coverage_ratio,
        receipt.coverage_ratio,
        receipt.coverage_scope,
        receipt.reason_codes,
    )
    authoritative_derived = (
        derived[0],
        derived[1],
        derived[2],
        derived_scope,
        derived_reason_codes,
    )
    if authoritative_derived != expected or receipt.degraded is not bool(
        receipt.reason_codes
    ):
        raise UniverseContractError("coverage_receipt_semantics_invalid")


def _verify_external_coverage_authority(
    receipt: CoverageReceipt,
    *,
    as_of: datetime,
    verifier: CoverageAuthorityVerifier | None,
) -> bool:
    if receipt.source_authority_status != "external_verified" or verifier is None:
        return False
    callback = getattr(verifier, "verify", None)
    if callback is None:
        raise UniverseContractError("coverage_source_authority_verifier_invalid")
    count_rows = tuple(
        {
            "dimension_type": item.dimension_type,
            "dimension_id": item.dimension_id,
            "expected_count": item.expected_count,
            "observed_count": item.observed_count,
        }
        for item in receipt.board_counts + receipt.sector_counts
    )
    try:
        verification = callback(
            count_rows=count_rows,
            source_generation=receipt.source_generation,
            source_receipt_id=receipt.source_receipt_id,
            source_sha256=receipt.source_sha256,
            taxonomy_id=receipt.taxonomy_id,
            taxonomy_version=receipt.taxonomy_version,
            taxonomy_sector_count=receipt.taxonomy_sector_count,
            assessed_as_of=as_of,
        )
    except Exception as exc:
        raise UniverseContractError("coverage_source_authority_rejected") from exc
    if not isinstance(verification, CoverageAuthorityVerification):
        raise UniverseContractError("coverage_source_authority_verification_invalid")
    verified_at = _aware_datetime(
        verification.verified_at,
        field_name="coverage_source_authority_verified_at",
    )
    membership_available_at = _aware_datetime(
        receipt.membership_available_at,
        field_name="membership_available_at",
    )
    receipt_as_of = _aware_datetime(receipt.as_of, field_name="coverage_as_of")
    return (
        verification.accepted is True
        and verification.verifier_id == receipt.source_authority_verifier_id
        and verification.proof_id == receipt.source_authority_proof_id
        and verification.source_generation == receipt.source_generation
        and verification.source_receipt_id == receipt.source_receipt_id
        and verification.source_sha256 == receipt.source_sha256
        and verification.taxonomy_id == receipt.taxonomy_id
        and verification.taxonomy_version == receipt.taxonomy_version
        and verification.taxonomy_sector_count == receipt.taxonomy_sector_count
        and verification.assessed_as_of == receipt.as_of
        and membership_available_at <= verified_at <= receipt_as_of
    )


@dataclass(frozen=True)
class UniverseExclusion:
    symbol: str
    reason_code: str


@dataclass(frozen=True)
class AccountTradableUniverseSnapshot:
    """Legacy-named snapshot of simulated policy access, not broker access."""

    contract_id: str
    access_policy_id: str
    access_semantics: str
    broker_permission_status: str
    broker_permission_verified: bool
    real_trading_enabled: bool
    simulation_only: bool
    as_of: str
    source_sha256: str
    symbols: tuple[str, ...]
    exclusions: tuple[UniverseExclusion, ...]
    snapshot_sha256: str


@dataclass(frozen=True)
class FeasibleInstrument:
    symbol: str
    reference_price_cny: float
    price_observed_at: str
    available_at: str
    revision_id: str
    receipt_id: str
    one_lot_notional_cny: float
    one_lot_buy_fee_cny: float
    one_lot_cash_required_cny: float
    one_lot_round_trip_cost_cny: float
    minimum_economic_shares: int
    minimum_economic_notional_cny: float
    minimum_economic_buy_fee_cny: float
    minimum_economic_cash_required_cny: float
    minimum_economic_round_trip_cost_cny: float
    max_buyable_shares: int
    max_buyable_notional_cny: float
    max_buy_cash_required_cny: float


@dataclass(frozen=True)
class SmallCapitalFeasibleUniverseSnapshot:
    contract_id: str
    policy_id: str
    as_of: str
    account_universe_sha256: str
    source_sha256: str
    capital_authority_id: str
    capital_authority_generation: int
    execution_lineage_id: str
    capital_observed_at: str
    available_cash_cny: float
    simulated_equity_cny: float
    authorized_capital_ceiling_cny: float
    risk_capital_base_cny: float
    single_name_cap_cny: float
    single_name_max_pct: float
    minimum_economic_order_cny: float
    max_adv_participation_pct: float
    lot_size_shares: int
    cost_policy_id: str
    execution_reality_model_version: str
    broker_permission_status: str
    real_trading_enabled: bool
    simulation_only: bool
    position_state_applied: bool
    max_buyable_semantics: str
    symbols: tuple[str, ...]
    entries: tuple[FeasibleInstrument, ...]
    exclusions: tuple[UniverseExclusion, ...]
    snapshot_sha256: str


def build_market_context_snapshot(
    rows: Iterable[Mapping[str, Any]],
    *,
    as_of: datetime,
    source_sha256: str,
    coverage_receipt: CoverageReceipt | None = None,
    coverage_authority_verifier: CoverageAuthorityVerifier | None = None,
) -> MarketContextUniverseSnapshot:
    raw_rows = tuple(rows)
    source_hash = _source_hash(raw_rows, source_sha256)
    as_of_text = _as_of(as_of)
    if coverage_receipt is None:
        raise UniverseContractError("coverage_receipt_required")
    _verify_coverage_receipt(coverage_receipt, as_of=as_of)
    observations: list[ContextObservation] = []
    seen: set[str] = set()
    for row in raw_rows:
        if not isinstance(row, Mapping):
            raise UniverseContractError("context_object_not_allowed")
        entity_id = row.get("entity_id")
        instrument_type = row.get("instrument_type")
        if (
            not isinstance(entity_id, str)
            or not _CONTEXT_ID_RE.fullmatch(entity_id)
            or not isinstance(instrument_type, str)
        ):
            raise UniverseContractError("context_object_not_allowed")
        eligibility = classify_instrument(
            entity_id,
            exchange=row.get("exchange", ""),
            instrument_type=instrument_type,
        )
        if (
            not eligibility.context_allowed
            or not eligibility.context_only
            or eligibility.order_identity_allowed
            or eligibility.role.value in {"chinext_common_stock", "star_common_stock"}
        ):
            raise UniverseContractError("context_object_not_allowed")
        normalized = eligibility.normalized_symbol or entity_id
        if eligibility.role.value.endswith("index") and not re.fullmatch(
            r"\d{6}\.(SH|SZ|BJ)", normalized
        ):
            raise UniverseContractError("context_object_not_allowed")
        if normalized in seen:
            raise UniverseContractError("duplicate_context_entity")
        seen.add(normalized)
        observations.append(
            ContextObservation(
                entity_id=normalized,
                role=eligibility.role.value,
                board=eligibility.board,
            )
        )
    observations.sort(key=lambda item: item.entity_id)
    reasons = set(coverage_receipt.reason_codes)
    if not _verify_external_coverage_authority(
        coverage_receipt,
        as_of=as_of,
        verifier=coverage_authority_verifier,
    ):
        reasons.add("coverage_source_authority_unverified")
    observed_roles = {item.role for item in observations}
    if "chinext_index" not in observed_roles:
        reasons.add("chinext_aggregate_context_missing")
    if "star_index" not in observed_roles:
        reasons.add("star_aggregate_context_missing")
    if "sector_aggregate" not in observed_roles:
        reasons.add("sector_aggregate_context_missing")
    receipt_sector_ids = {
        item.dimension_id.upper() for item in coverage_receipt.sector_counts
    }
    observed_sector_ids = {
        item.entity_id.upper()
        for item in observations
        if item.role == "sector_aggregate"
    }
    if observed_sector_ids != receipt_sector_ids:
        reasons.add("taxonomy_sector_context_gap")
    if reasons:
        reasons.add("full_market_coverage_missing")
    reason_codes = tuple(sorted(reasons))
    coverage_scope = "full_market" if not reason_codes else "partial_market"
    payload = {
        "contract_id": POLICY_ID,
        "as_of": as_of_text,
        "source_sha256": source_hash,
        "coverage_receipt_sha256": coverage_receipt.receipt_sha256,
        "coverage_scope": coverage_scope,
        "degraded": bool(reason_codes),
        "reason_codes": list(reason_codes),
        "observations": [asdict(item) for item in observations],
    }
    return MarketContextUniverseSnapshot(
        contract_id=POLICY_ID,
        as_of=as_of_text,
        source_sha256=source_hash,
        coverage_receipt_sha256=coverage_receipt.receipt_sha256,
        coverage_scope=coverage_scope,
        degraded=bool(reason_codes),
        reason_codes=reason_codes,
        observations=tuple(observations),
        snapshot_sha256=_snapshot_sha(payload),
    )


def build_account_tradable_snapshot(
    rows: Iterable[Mapping[str, Any]],
    *,
    as_of: datetime,
    source_sha256: str,
) -> AccountTradableUniverseSnapshot:
    raw_rows = tuple(rows)
    source_hash = _source_hash(raw_rows, source_sha256)
    as_of_text = _as_of(as_of)
    symbols: list[str] = []
    exclusions: list[UniverseExclusion] = []
    seen: set[str] = set()
    for row in raw_rows:
        if not isinstance(row, Mapping):
            raise UniverseContractError("asset_row_must_be_mapping")
        try:
            symbol = _text(row.get("symbol"), field_name="symbol")
        except UniverseContractError:
            symbol = "<invalid>"
        eligibility = classify_instrument(
            row.get("symbol"),
            exchange=row.get("exchange", ""),
            instrument_type=row.get("instrument_type", "common_stock"),
        )
        canonical = eligibility.normalized_symbol or symbol
        if canonical in seen:
            raise UniverseContractError("duplicate_asset_symbol")
        seen.add(canonical)
        if eligibility.order_identity_allowed:
            symbols.append(canonical)
        else:
            exclusions.append(
                UniverseExclusion(
                    symbol=symbol,
                    reason_code=eligibility.reason_code,
                )
            )
    symbols.sort()
    exclusions.sort(key=lambda item: item.symbol)
    payload = {
        "contract_id": POLICY_ID,
        "access_policy_id": _SIMULATED_ACCESS_POLICY_ID,
        "access_semantics": "policy_allowed_not_broker_verified",
        "broker_permission_status": "not_verified",
        "broker_permission_verified": False,
        "real_trading_enabled": False,
        "simulation_only": True,
        "as_of": as_of_text,
        "source_sha256": source_hash,
        "symbols": symbols,
        "exclusions": [asdict(item) for item in exclusions],
    }
    return AccountTradableUniverseSnapshot(
        contract_id=POLICY_ID,
        access_policy_id=_SIMULATED_ACCESS_POLICY_ID,
        access_semantics="policy_allowed_not_broker_verified",
        broker_permission_status="not_verified",
        broker_permission_verified=False,
        real_trading_enabled=False,
        simulation_only=True,
        as_of=as_of_text,
        source_sha256=source_hash,
        symbols=tuple(symbols),
        exclusions=tuple(exclusions),
        snapshot_sha256=_snapshot_sha(payload),
    )


def _positive_number(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UniverseContractError(f"{field_name}_invalid")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise UniverseContractError(f"{field_name}_invalid")
    return number


def _nonnegative_number(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UniverseContractError(f"{field_name}_invalid")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise UniverseContractError(f"{field_name}_invalid")
    return number


def _pit_instant(value: object, *, field_name: str, as_of: datetime) -> str:
    if isinstance(value, datetime):
        instant = value
    elif isinstance(value, str) and value and value == value.strip():
        try:
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise UniverseContractError(f"{field_name}_invalid") from exc
    else:
        raise UniverseContractError(f"{field_name}_invalid")
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise UniverseContractError(f"{field_name}_timezone_required")
    if instant > as_of:
        raise UniverseContractError(f"{field_name}_after_as_of")
    return instant.isoformat()


def _buy_fee(notional_cny: float) -> float:
    return round(
        commission(notional_cny, ASHARE_RESEARCH_COST_POLICY_V1)
        + transfer_fee(notional_cny, ASHARE_RESEARCH_COST_POLICY_V1),
        6,
    )


def _max_buyable_shares(*, price_cny: float, cash_budget_cny: float) -> int:
    if cash_budget_cny <= 0:
        return 0
    lots = math.floor(cash_budget_cny / (price_cny * 100))
    while lots > 0:
        shares = lots * 100
        notional = shares * price_cny
        if notional + _buy_fee(notional) <= cash_budget_cny:
            return shares
        lots -= 1
    return 0


def build_small_capital_feasible_snapshot(
    account: AccountTradableUniverseSnapshot,
    market_rows: Iterable[Mapping[str, Any]],
    *,
    source_sha256: str,
    capital_authority_id: str,
    capital_authority_generation: int,
    execution_lineage_id: str,
    capital_observed_at: datetime,
    available_cash_cny: float,
    simulated_equity_cny: float,
    authorized_capital_ceiling_cny: float,
    cost_policy_id: str,
    execution_reality_model_version: str,
    single_name_max_pct: float = 0.15,
    minimum_economic_order_cny: float = 2_000.0,
    max_adv_participation_pct: float = 0.01,
) -> SmallCapitalFeasibleUniverseSnapshot:
    if not isinstance(account, AccountTradableUniverseSnapshot):
        raise UniverseContractError("account_snapshot_required")
    _verify_snapshot_sha(
        account,
        error_code="account_snapshot_sha256_mismatch",
    )
    if (
        account.access_policy_id != _SIMULATED_ACCESS_POLICY_ID
        or account.access_semantics != "policy_allowed_not_broker_verified"
        or account.broker_permission_status != "not_verified"
        or account.broker_permission_verified is not False
        or account.real_trading_enabled is not False
        or account.simulation_only is not True
    ):
        raise UniverseContractError("simulated_access_snapshot_required")
    if len(set(account.symbols)) != len(account.symbols):
        raise UniverseContractError("simulated_access_snapshot_symbol_invalid")
    for symbol in account.symbols:
        eligibility = classify_instrument(symbol, instrument_type="common_stock")
        if (
            not eligibility.order_identity_allowed
            or eligibility.role.value != "mainboard_common_stock"
        ):
            raise UniverseContractError("simulated_access_snapshot_symbol_invalid")
    authority_id = _text(
        capital_authority_id,
        field_name="capital_authority_id",
    )
    if authority_id != _CAPITAL_AUTHORITY_ID:
        raise UniverseContractError("capital_authority_id_mismatch")
    if (
        isinstance(capital_authority_generation, bool)
        or not isinstance(capital_authority_generation, int)
        or capital_authority_generation <= 0
    ):
        raise UniverseContractError("capital_authority_generation_invalid")
    lineage_id = _text(
        execution_lineage_id,
        field_name="execution_lineage_id",
    )
    available_cash = _nonnegative_number(
        available_cash_cny,
        field_name="available_cash_cny",
    )
    simulated_equity = _nonnegative_number(
        simulated_equity_cny,
        field_name="simulated_equity_cny",
    )
    if available_cash > simulated_equity:
        raise UniverseContractError("available_cash_exceeds_simulated_equity")
    authorized_ceiling = _positive_number(
        authorized_capital_ceiling_cny,
        field_name="authorized_capital_ceiling_cny",
    )
    if authorized_ceiling != _AUTHORIZED_CAPITAL_CEILING_CNY:
        raise UniverseContractError("authorized_capital_ceiling_mismatch")
    if single_name_max_pct != 0.15:
        raise UniverseContractError("single_name_policy_mismatch")
    minimum_economic_order = _positive_number(
        minimum_economic_order_cny,
        field_name="minimum_economic_order_cny",
    )
    max_adv_participation = _positive_number(
        max_adv_participation_pct,
        field_name="max_adv_participation_pct",
    )
    if max_adv_participation > 1:
        raise UniverseContractError("max_adv_participation_pct_invalid")
    if cost_policy_id != ASHARE_RESEARCH_COST_POLICY_V1.policy_id:
        raise UniverseContractError("cost_policy_id_mismatch")
    if (
        execution_reality_model_version
        != ASHARE_RESEARCH_COST_POLICY_V1.execution_reality_model_version
    ):
        raise UniverseContractError("execution_reality_model_version_mismatch")
    try:
        decision_as_of = datetime.fromisoformat(account.as_of.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UniverseContractError("account_as_of_invalid") from exc
    if decision_as_of.tzinfo is None or decision_as_of.utcoffset() is None:
        raise UniverseContractError("account_as_of_timezone_required")
    capital_observed_at_text = _pit_instant(
        capital_observed_at,
        field_name="capital_observed_at",
        as_of=decision_as_of,
    )
    raw_rows = tuple(market_rows)
    source_hash = _source_hash(raw_rows, source_sha256)
    market_by_symbol: dict[str, Mapping[str, Any]] = {}
    for row in raw_rows:
        if not isinstance(row, Mapping):
            raise UniverseContractError("market_row_must_be_mapping")
        symbol = _text(row.get("symbol"), field_name="symbol")
        if symbol in market_by_symbol:
            raise UniverseContractError("duplicate_market_symbol")
        market_by_symbol[symbol] = row

    risk_capital_base = min(simulated_equity, authorized_ceiling)
    single_cap = risk_capital_base * single_name_max_pct
    buy_budget = min(single_cap, available_cash)
    entries: list[FeasibleInstrument] = []
    exclusions: list[UniverseExclusion] = []
    for symbol in account.symbols:
        row = market_by_symbol.get(symbol)
        if row is None:
            exclusions.append(UniverseExclusion(symbol, "market_data_missing"))
            continue
        if "data_quality" not in row:
            exclusions.append(UniverseExclusion(symbol, "market_data_quality_missing"))
            continue
        if row.get("data_quality") != "ready":
            exclusions.append(UniverseExclusion(symbol, "market_data_degraded"))
            continue
        if "next_bar_open" in row:
            raise UniverseContractError("future_price_field_forbidden")
        observed_at = _pit_instant(
            row.get("price_observed_at"),
            field_name="price_observed_at",
            as_of=decision_as_of,
        )
        available_at = _pit_instant(
            row.get("available_at"),
            field_name="available_at",
            as_of=decision_as_of,
        )
        if datetime.fromisoformat(available_at) < datetime.fromisoformat(observed_at):
            raise UniverseContractError("available_at_before_price_observation")
        revision_id = _text(row.get("revision_id"), field_name="revision_id")
        receipt_id = _text(row.get("receipt_id"), field_name="receipt_id")
        if row.get("listing_status") != "listed":
            exclusions.append(UniverseExclusion(symbol, "listing_status_ineligible"))
            continue
        if type(row.get("risk_warning")) is not bool:
            raise UniverseContractError("risk_warning_invalid")
        if row.get("risk_warning") is True:
            exclusions.append(UniverseExclusion(symbol, "risk_warning_ineligible"))
            continue
        if type(row.get("suspended")) is not bool:
            raise UniverseContractError("suspended_invalid")
        if row.get("suspended") is True:
            exclusions.append(UniverseExclusion(symbol, "suspended_at_decision"))
            continue
        price = _positive_number(
            row.get("decision_reference_price"),
            field_name="decision_reference_price",
        )
        adv = _positive_number(row.get("adv20_cny"), field_name="adv20_cny")
        one_lot = price * 100
        one_lot_buy_fee = _buy_fee(one_lot)
        one_lot_cash_required = one_lot + one_lot_buy_fee
        max_buyable_shares = _max_buyable_shares(
            price_cny=price,
            cash_budget_cny=buy_budget,
        )
        if max_buyable_shares < 100:
            exclusions.append(UniverseExclusion(symbol, "lot_not_affordable"))
            continue
        lots = max(1, math.ceil(minimum_economic_order / one_lot))
        minimum_shares = lots * 100
        minimum_notional = minimum_shares * price
        minimum_buy_fee = _buy_fee(minimum_notional)
        minimum_cash_required = minimum_notional + minimum_buy_fee
        if minimum_shares > max_buyable_shares:
            exclusions.append(
                UniverseExclusion(symbol, "minimum_economic_order_not_affordable")
            )
            continue
        if minimum_notional > adv * max_adv_participation:
            exclusions.append(UniverseExclusion(symbol, "liquidity_too_low"))
            continue
        one_lot_round_trip = estimate_round_trip_cost(
            quantity=100,
            entry_reference_price=price,
            exit_reference_price=price,
            policy=ASHARE_RESEARCH_COST_POLICY_V1,
        )
        minimum_round_trip = estimate_round_trip_cost(
            quantity=minimum_shares,
            entry_reference_price=price,
            exit_reference_price=price,
            policy=ASHARE_RESEARCH_COST_POLICY_V1,
        )
        max_buyable_notional = max_buyable_shares * price
        max_buy_cash_required = max_buyable_notional + _buy_fee(max_buyable_notional)
        entries.append(
            FeasibleInstrument(
                symbol=symbol,
                reference_price_cny=round(price, 6),
                price_observed_at=observed_at,
                available_at=available_at,
                revision_id=revision_id,
                receipt_id=receipt_id,
                one_lot_notional_cny=round(one_lot, 6),
                one_lot_buy_fee_cny=one_lot_buy_fee,
                one_lot_cash_required_cny=round(one_lot_cash_required, 6),
                one_lot_round_trip_cost_cny=one_lot_round_trip.total_cost_cny,
                minimum_economic_shares=minimum_shares,
                minimum_economic_notional_cny=round(minimum_notional, 6),
                minimum_economic_buy_fee_cny=minimum_buy_fee,
                minimum_economic_cash_required_cny=round(
                    minimum_cash_required,
                    6,
                ),
                minimum_economic_round_trip_cost_cny=(
                    minimum_round_trip.total_cost_cny
                ),
                max_buyable_shares=max_buyable_shares,
                max_buyable_notional_cny=round(max_buyable_notional, 6),
                max_buy_cash_required_cny=round(max_buy_cash_required, 6),
            )
        )
    entries.sort(key=lambda item: item.symbol)
    exclusions.sort(key=lambda item: item.symbol)
    payload = {
        "contract_id": POLICY_ID,
        "policy_id": "ashare-small-account-50000-v1",
        "as_of": account.as_of,
        "account_universe_sha256": account.snapshot_sha256,
        "source_sha256": source_hash,
        "capital_authority_id": authority_id,
        "capital_authority_generation": capital_authority_generation,
        "execution_lineage_id": lineage_id,
        "capital_observed_at": capital_observed_at_text,
        "available_cash_cny": round(available_cash, 6),
        "simulated_equity_cny": round(simulated_equity, 6),
        "authorized_capital_ceiling_cny": authorized_ceiling,
        "risk_capital_base_cny": round(risk_capital_base, 6),
        "single_name_cap_cny": round(single_cap, 6),
        "single_name_max_pct": single_name_max_pct,
        "minimum_economic_order_cny": minimum_economic_order,
        "max_adv_participation_pct": max_adv_participation,
        "lot_size_shares": 100,
        "cost_policy_id": cost_policy_id,
        "execution_reality_model_version": execution_reality_model_version,
        "broker_permission_status": "not_verified",
        "real_trading_enabled": False,
        "simulation_only": True,
        "position_state_applied": False,
        "max_buyable_semantics": (
            "cash_and_single_name_upper_bound_before_position_check"
        ),
        "symbols": [item.symbol for item in entries],
        "entries": [asdict(item) for item in entries],
        "exclusions": [asdict(item) for item in exclusions],
    }
    return SmallCapitalFeasibleUniverseSnapshot(
        contract_id=POLICY_ID,
        policy_id="ashare-small-account-50000-v1",
        as_of=account.as_of,
        account_universe_sha256=account.snapshot_sha256,
        source_sha256=source_hash,
        capital_authority_id=authority_id,
        capital_authority_generation=capital_authority_generation,
        execution_lineage_id=lineage_id,
        capital_observed_at=capital_observed_at_text,
        available_cash_cny=round(available_cash, 6),
        simulated_equity_cny=round(simulated_equity, 6),
        authorized_capital_ceiling_cny=authorized_ceiling,
        risk_capital_base_cny=round(risk_capital_base, 6),
        single_name_cap_cny=round(single_cap, 6),
        single_name_max_pct=single_name_max_pct,
        minimum_economic_order_cny=minimum_economic_order,
        max_adv_participation_pct=max_adv_participation,
        lot_size_shares=100,
        cost_policy_id=cost_policy_id,
        execution_reality_model_version=execution_reality_model_version,
        broker_permission_status="not_verified",
        real_trading_enabled=False,
        simulation_only=True,
        position_state_applied=False,
        max_buyable_semantics=(
            "cash_and_single_name_upper_bound_before_position_check"
        ),
        symbols=tuple(item.symbol for item in entries),
        entries=tuple(entries),
        exclusions=tuple(exclusions),
        snapshot_sha256=_snapshot_sha(payload),
    )


__all__ = [
    "AccountTradableUniverseSnapshot",
    "ContextObservation",
    "CoverageDimensionCount",
    "CoverageReceipt",
    "FeasibleInstrument",
    "MarketContextUniverseSnapshot",
    "SmallCapitalFeasibleUniverseSnapshot",
    "UniverseContractError",
    "UniverseExclusion",
    "build_account_tradable_snapshot",
    "build_coverage_receipt",
    "build_market_context_snapshot",
    "build_small_capital_feasible_snapshot",
    "canonical_source_sha256",
]
