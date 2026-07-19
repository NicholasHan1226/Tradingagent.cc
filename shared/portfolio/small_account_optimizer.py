"""Deterministic 50k A-share allocation with integer lots and explicit cash."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Iterable, Protocol

from shared.capital.market_policy import (
    CANONICAL_ASHARE_BUY_LOT_SIZE_SHARES,
    CANONICAL_ASHARE_MAX_POSITIONS,
    CANONICAL_ASHARE_MINIMUM_ECONOMIC_ORDER_CNY,
    CANONICAL_ASHARE_NO_TRADE_BAND_CNY,
    CANONICAL_INITIAL_EQUITY_CNY,
    CANONICAL_SINGLE_NAME_MAX_PCT,
    CANONICAL_STOCK_GROSS_EXPOSURE_LIMIT_PCT,
    MarketPolicy,
)
from shared.execution.cost_policy import (
    ASHARE_RESEARCH_COST_POLICY_V1,
    AShareCostPolicy,
    commission,
    conservative_planning_price,
    estimate_round_trip_cost,
    transfer_fee,
)
from shared.execution.execution_lineage import (
    ASHARE_CAPITAL_AUTHORITY_ID,
)
from shared.execution.execution_reality import ashare_sell_quantity_rejection_reason
from shared.portfolio.champion import (
    ChampionContractError,
    ChampionSelectionContext,
    ChampionSelectionVerifier,
    ChampionScoreReceipt,
    FixtureRankEvidence,
    NumericPITFeatureSnapshotVerifier,
    verify_champion_score_receipt,
    verify_fixture_rank_evidence,
)
from shared.portfolio.thesis_risk import (
    ThesisRiskExposureReceipt,
    ThesisRiskGroupEffect,
    ThesisRiskGroups,
    ThesisRiskRuntimeAuthority,
    apply_group_delta,
    initial_group_exposures,
)
from shared.universe.policy import is_mainboard_tradable


_DECISION_REASONS = frozenset(
    {
        "allocated",
        "inside_no_trade_band",
        "minimum_economic_order",
        "lot_not_affordable",
        "cash_limit",
        "gross_exposure_limit",
        "t1_sellable_limit",
        "not_in_candidate_set_hold",
        "reduction_requires_explicit_intent",
        "explicit_reduction_intent",
        "max_positions_limit",
        "ashare_odd_lot_sell_quantity_invalid",
        "risk_group_cap",
        "new_risk_not_authorized",
    }
)
_UNDEPLOYED_REASONS = frozenset(
    {
        "cash_reserve",
        "candidate_capacity_exhausted",
        "lot_rounding",
        "minimum_economic_order",
        "gross_exposure_limit",
        "max_positions_limit",
        "risk_group_cap",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OFFLINE_FIXTURE_AUTHORITY_RE = re.compile(
    r"^ashare-[a-z0-9-]*offline-fixture[a-z0-9-]*$"
)


def _finite(value: object, *, field_name: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name}_must_be_numeric")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise ValueError(f"{field_name}_invalid")
    return number


def _aware(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name}_timezone_required")
    if value.utcoffset() is None:
        raise ValueError(f"{field_name}_timezone_required")
    return value


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def account_position_snapshot_sha256(
    positions: tuple[AccountPositionSnapshot, ...],
) -> str:
    """Content address every quantity and mark used by the optimizer."""

    payload = [
        {
            "symbol": position.symbol,
            "total_shares": position.total_shares,
            "sellable_shares": position.sellable_shares,
            "mark_price_cny": position.mark_price_cny,
            "price_observed_at": position.price_observed_at.astimezone(
                timezone.utc
            ).isoformat(),
        }
        for position in sorted(positions, key=lambda row: row.symbol)
    ]
    return _canonical_sha256(payload)


@dataclass(frozen=True)
class SmallAccountPolicy:
    policy_id: str = "ashare-small-account-50000-v1"
    initial_equity_cny: float = CANONICAL_INITIAL_EQUITY_CNY
    single_name_max_pct: float = CANONICAL_SINGLE_NAME_MAX_PCT
    stock_gross_limit_pct: float = CANONICAL_STOCK_GROSS_EXPOSURE_LIMIT_PCT
    lot_size: int = CANONICAL_ASHARE_BUY_LOT_SIZE_SHARES
    minimum_economic_order_cny: float = CANONICAL_ASHARE_MINIMUM_ECONOMIC_ORDER_CNY
    no_trade_band_cny: float = CANONICAL_ASHARE_NO_TRADE_BAND_CNY
    max_positions: int = CANONICAL_ASHARE_MAX_POSITIONS
    execution_scope: str = "simulated_research_only"

    def __post_init__(self) -> None:
        if self.initial_equity_cny != 50_000.0:
            raise ValueError("initial_equity_must_equal_50000")
        if self.single_name_max_pct != 0.15:
            raise ValueError("single_name_limit_must_equal_15pct")
        if self.stock_gross_limit_pct != 0.90:
            raise ValueError("gross_limit_must_equal_90pct")
        if self.lot_size != 100:
            raise ValueError("lot_size_must_equal_100")
        if self.minimum_economic_order_cny < self.no_trade_band_cny:
            raise ValueError("minimum_order_must_cover_no_trade_band")
        if (
            isinstance(self.max_positions, bool)
            or not isinstance(self.max_positions, int)
            or not 1 <= self.max_positions <= 8
        ):
            raise ValueError("max_positions_must_be_between_1_and_8")
        if self.execution_scope != "simulated_research_only":
            raise ValueError("execution_scope_must_be_simulated_research_only")

    @classmethod
    def from_market_policy(cls, market_policy: MarketPolicy) -> "SmallAccountPolicy":
        """Project the one canonical A-share authority into optimizer policy."""

        if not isinstance(market_policy, MarketPolicy) or market_policy.market != (
            "ashare"
        ):
            raise ValueError("ashare_market_policy_required")
        values = (
            market_policy.max_positions,
            market_policy.buy_lot_size_shares,
            market_policy.minimum_economic_order_cny,
            market_policy.no_trade_band_cny,
        )
        if any(value is None for value in values):
            raise ValueError("ashare_small_account_policy_incomplete")
        return cls(
            initial_equity_cny=market_policy.initial_equity_cny,
            single_name_max_pct=float(market_policy.single_name_max_pct or 0.0),
            stock_gross_limit_pct=float(
                market_policy.stock_gross_exposure_limit_pct or 0.0
            ),
            lot_size=int(market_policy.buy_lot_size_shares),
            minimum_economic_order_cny=float(market_policy.minimum_economic_order_cny),
            no_trade_band_cny=float(market_policy.no_trade_band_cny),
            max_positions=int(market_policy.max_positions),
        )

    def risk_budget_base_cny(self, current_equity_cny: float) -> float:
        """Never auto-expand beyond authorised capital; contract after loss."""

        equity = _finite(current_equity_cny, field_name="current_equity_cny")
        return min(self.initial_equity_cny, equity)


@dataclass(frozen=True)
class AccountPositionSnapshot:
    symbol: str
    total_shares: int
    sellable_shares: int
    mark_price_cny: float
    price_observed_at: datetime

    def __post_init__(self) -> None:
        if (
            not isinstance(self.symbol, str)
            or not self.symbol
            or self.symbol != self.symbol.strip()
        ):
            raise ValueError("position_symbol_invalid")
        for field_name, value in (
            ("total_shares", self.total_shares),
            ("sellable_shares", self.sellable_shares),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name}_must_be_nonnegative_integer")
        if self.sellable_shares > self.total_shares:
            raise ValueError("sellable_shares_exceed_total_shares")
        _finite(self.mark_price_cny, field_name="mark_price_cny", minimum=1e-12)
        _aware(self.price_observed_at, field_name="price_observed_at")


@dataclass(frozen=True)
class AccountAuthoritySnapshot:
    capital_authority_id: str
    authority_generation: int
    account_as_of: datetime
    available_cash_cny: float
    current_gross_cny: float
    positions: tuple[AccountPositionSnapshot, ...]
    position_snapshot_receipt_id: str
    position_snapshot_sha256: str
    verification_receipt_sha256: str
    authority_source_class: str = "canonical_authority"

    def __post_init__(self) -> None:
        if self.authority_source_class == "canonical_authority":
            authority_valid = self.capital_authority_id == ASHARE_CAPITAL_AUTHORITY_ID
        elif self.authority_source_class == "offline_fixture":
            authority_valid = bool(
                self.capital_authority_id != ASHARE_CAPITAL_AUTHORITY_ID
                and _OFFLINE_FIXTURE_AUTHORITY_RE.fullmatch(self.capital_authority_id)
            )
        else:
            authority_valid = False
        if not authority_valid:
            raise ValueError("capital_authority_id_mismatch")
        if (
            isinstance(self.authority_generation, bool)
            or not isinstance(self.authority_generation, int)
            or self.authority_generation <= 0
        ):
            raise ValueError("authority_generation_invalid")
        account_as_of = _aware(self.account_as_of, field_name="account_as_of")
        cash = _finite(self.available_cash_cny, field_name="available_cash_cny")
        declared_gross = _finite(
            self.current_gross_cny,
            field_name="current_gross_cny",
        )
        if not isinstance(self.positions, tuple):
            raise ValueError("positions_must_be_tuple")
        if len({position.symbol for position in self.positions}) != len(self.positions):
            raise ValueError("duplicate_position_symbol")
        for position in self.positions:
            if not isinstance(position, AccountPositionSnapshot):
                raise ValueError("position_snapshot_invalid")
            if position.price_observed_at > account_as_of:
                raise ValueError("position_price_observed_after_account_as_of")
        computed_gross = round(
            sum(
                position.total_shares * position.mark_price_cny
                for position in self.positions
            ),
            6,
        )
        if abs(computed_gross - declared_gross) > 1e-6:
            raise ValueError("declared_gross_position_mismatch")
        if not isinstance(self.position_snapshot_receipt_id, str) or not (
            self.position_snapshot_receipt_id.strip()
            and self.position_snapshot_receipt_id
            == self.position_snapshot_receipt_id.strip()
        ):
            raise ValueError("position_snapshot_receipt_id_invalid")
        for field_name, value in (
            ("position_snapshot_sha256", self.position_snapshot_sha256),
            ("verification_receipt_sha256", self.verification_receipt_sha256),
        ):
            if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
                raise ValueError(f"{field_name}_invalid")
        _finite(cash + declared_gross, field_name="current_equity_cny")


def account_authority_content_sha256(snapshot: AccountAuthoritySnapshot) -> str:
    """Hash every account fact whose mutation could change a decision."""

    if not isinstance(snapshot, AccountAuthoritySnapshot):
        raise TypeError("account_authority_snapshot_required")
    return _canonical_sha256(
        {
            "capital_authority_id": snapshot.capital_authority_id,
            "authority_generation": snapshot.authority_generation,
            "account_as_of": snapshot.account_as_of.astimezone(
                timezone.utc
            ).isoformat(),
            "available_cash_cny": snapshot.available_cash_cny,
            "current_gross_cny": snapshot.current_gross_cny,
            "authority_source_class": snapshot.authority_source_class,
            "position_snapshot_receipt_id": snapshot.position_snapshot_receipt_id,
            "declared_position_snapshot_sha256": snapshot.position_snapshot_sha256,
            "computed_position_snapshot_sha256": (
                account_position_snapshot_sha256(snapshot.positions)
            ),
        }
    )


@dataclass(frozen=True)
class AccountAuthorityVerification:
    """Detached verifier receipt bound to the complete optimizer account input."""

    verifier_id: str
    verifier_version: str
    capital_authority_id: str
    authority_generation: int
    account_as_of: datetime
    authority_source_class: str
    position_snapshot_receipt_id: str
    position_snapshot_sha256: str
    account_content_sha256: str
    verified_at: datetime
    valid_until: datetime
    promotion_eligible: bool
    verification_receipt_sha256: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("verifier_id", self.verifier_id),
            ("verifier_version", self.verifier_version),
            ("capital_authority_id", self.capital_authority_id),
            ("authority_source_class", self.authority_source_class),
            ("position_snapshot_receipt_id", self.position_snapshot_receipt_id),
        ):
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"account_authority_{field_name}_invalid")
        if (
            isinstance(self.authority_generation, bool)
            or not isinstance(self.authority_generation, int)
            or self.authority_generation <= 0
        ):
            raise ValueError("account_authority_generation_invalid")
        _aware(self.account_as_of, field_name="account_authority_account_as_of")
        verified_at = _aware(
            self.verified_at,
            field_name="account_authority_verified_at",
        )
        valid_until = _aware(
            self.valid_until,
            field_name="account_authority_valid_until",
        )
        if valid_until < verified_at:
            raise ValueError("account_authority_validity_window_invalid")
        if type(self.promotion_eligible) is not bool:
            raise ValueError("account_authority_promotion_eligible_invalid")
        if self.authority_source_class == "offline_fixture" and self.promotion_eligible:
            raise ValueError("offline_fixture_authority_cannot_promote")
        for field_name, value in (
            ("position_snapshot_sha256", self.position_snapshot_sha256),
            ("account_content_sha256", self.account_content_sha256),
            ("verification_receipt_sha256", self.verification_receipt_sha256),
        ):
            if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
                raise ValueError(f"account_authority_{field_name}_invalid")
        if self.verification_receipt_sha256 != _canonical_sha256(
            self._receipt_payload()
        ):
            raise ValueError("account_authority_verification_receipt_mismatch")

    def _receipt_payload(self) -> dict[str, object]:
        return {
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
            "capital_authority_id": self.capital_authority_id,
            "authority_generation": self.authority_generation,
            "account_as_of": self.account_as_of.astimezone(timezone.utc).isoformat(),
            "authority_source_class": self.authority_source_class,
            "position_snapshot_receipt_id": self.position_snapshot_receipt_id,
            "position_snapshot_sha256": self.position_snapshot_sha256,
            "account_content_sha256": self.account_content_sha256,
            "verified_at": self.verified_at.astimezone(timezone.utc).isoformat(),
            "valid_until": self.valid_until.astimezone(timezone.utc).isoformat(),
            "promotion_eligible": self.promotion_eligible,
        }

    @classmethod
    def create(
        cls,
        *,
        snapshot: AccountAuthoritySnapshot,
        verifier_id: str,
        verifier_version: str,
        verified_at: datetime,
        valid_until: datetime,
        promotion_eligible: bool,
    ) -> AccountAuthorityVerification:
        payload = {
            "verifier_id": verifier_id,
            "verifier_version": verifier_version,
            "capital_authority_id": snapshot.capital_authority_id,
            "authority_generation": snapshot.authority_generation,
            "account_as_of": snapshot.account_as_of.astimezone(
                timezone.utc
            ).isoformat(),
            "authority_source_class": snapshot.authority_source_class,
            "position_snapshot_receipt_id": snapshot.position_snapshot_receipt_id,
            "position_snapshot_sha256": snapshot.position_snapshot_sha256,
            "account_content_sha256": account_authority_content_sha256(snapshot),
            "verified_at": verified_at.astimezone(timezone.utc).isoformat(),
            "valid_until": valid_until.astimezone(timezone.utc).isoformat(),
            "promotion_eligible": promotion_eligible,
        }
        return cls(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            capital_authority_id=snapshot.capital_authority_id,
            authority_generation=snapshot.authority_generation,
            account_as_of=snapshot.account_as_of,
            authority_source_class=snapshot.authority_source_class,
            position_snapshot_receipt_id=snapshot.position_snapshot_receipt_id,
            position_snapshot_sha256=snapshot.position_snapshot_sha256,
            account_content_sha256=account_authority_content_sha256(snapshot),
            verified_at=verified_at,
            valid_until=valid_until,
            promotion_eligible=promotion_eligible,
            verification_receipt_sha256=_canonical_sha256(payload),
        )


class AccountAuthorityVerifier(Protocol):
    """Port implemented by an independent authority reader, never by optimizer."""

    def verify(
        self,
        snapshot: AccountAuthoritySnapshot,
        *,
        decision_time: datetime,
    ) -> AccountAuthorityVerification: ...


def _verify_account_authority_snapshot(
    *,
    snapshot: AccountAuthoritySnapshot,
    decision_time: datetime,
    verifier: AccountAuthorityVerifier,
) -> AccountAuthorityVerification:
    computed_position_sha256 = account_position_snapshot_sha256(snapshot.positions)
    if snapshot.position_snapshot_sha256 != computed_position_sha256:
        raise ValueError("position_snapshot_content_hash_mismatch")
    verify = getattr(verifier, "verify", None)
    if not callable(verify):
        raise ValueError("account_authority_verifier_required")
    try:
        proof = verify(snapshot, decision_time=decision_time)
    except Exception as exc:
        raise ValueError("account_authority_verification_failed") from exc
    if not isinstance(proof, AccountAuthorityVerification):
        raise ValueError("account_authority_verification_failed")
    expected = {
        "capital_authority_id": snapshot.capital_authority_id,
        "authority_generation": snapshot.authority_generation,
        "account_as_of": snapshot.account_as_of,
        "authority_source_class": snapshot.authority_source_class,
        "position_snapshot_receipt_id": snapshot.position_snapshot_receipt_id,
        "position_snapshot_sha256": computed_position_sha256,
        "account_content_sha256": account_authority_content_sha256(snapshot),
        "verification_receipt_sha256": snapshot.verification_receipt_sha256,
    }
    if any(
        getattr(proof, field_name) != value for field_name, value in expected.items()
    ):
        raise ValueError("account_authority_proof_binding_mismatch")
    if proof.verified_at < snapshot.account_as_of:
        raise ValueError("account_authority_proof_predates_snapshot")
    if proof.verified_at > decision_time:
        raise ValueError("account_authority_proof_after_decision")
    if proof.valid_until < decision_time:
        raise ValueError("account_authority_proof_expired")
    if snapshot.authority_source_class == "offline_fixture" and (
        proof.promotion_eligible
    ):
        raise ValueError("offline_fixture_authority_cannot_promote")
    return proof


@dataclass(frozen=True)
class CandidateAllocationInput:
    symbol: str
    score_evidence: ChampionScoreReceipt | FixtureRankEvidence
    decision_time: datetime
    price_observed_at: datetime
    decision_reference_price: float

    def __post_init__(self) -> None:
        if (
            not isinstance(self.symbol, str)
            or not self.symbol
            or self.symbol != self.symbol.strip()
        ):
            raise ValueError("symbol_invalid")
        decision_time = _aware(self.decision_time, field_name="decision_time")
        price_observed_at = _aware(
            self.price_observed_at,
            field_name="price_observed_at",
        )
        if price_observed_at > decision_time:
            raise ValueError("price_observed_after_decision")
        _finite(
            self.decision_reference_price,
            field_name="decision_reference_price",
            minimum=1e-12,
        )
        try:
            if isinstance(self.score_evidence, ChampionScoreReceipt):
                if self.score_evidence.symbol != self.symbol:
                    raise ChampionContractError("score_receipt_symbol_mismatch")
                if self.score_evidence.decision_time != decision_time:
                    raise ChampionContractError("score_receipt_decision_time_mismatch")
            elif isinstance(self.score_evidence, FixtureRankEvidence):
                verify_fixture_rank_evidence(
                    self.score_evidence,
                    expected_symbol=self.symbol,
                    expected_decision_time=decision_time,
                    expected_champion_selection_manifest_sha256=(
                        self.score_evidence.champion_selection_manifest_sha256
                    ),
                )
            else:
                raise ChampionContractError("candidate_score_evidence_required")
        except ChampionContractError as exc:
            raise ValueError("candidate_score_evidence_invalid") from exc

    @property
    def rank_score(self) -> float:
        """Uncalibrated ordering value; it is never a sizing input."""

        return self.score_evidence.rank_score

    @property
    def score_receipt_sha256(self) -> str:
        return self.score_evidence.receipt_sha256

    @property
    def score_evidence_class(self) -> str:
        return self.score_evidence.evidence_class

    @property
    def champion_selection_manifest_sha256(self) -> str:
        return self.score_evidence.champion_selection_manifest_sha256


@dataclass(frozen=True)
class _ExistingPositionCandidate:
    symbol: str
    decision_time: datetime
    price_observed_at: datetime
    decision_reference_price: float
    rank_score: float = 0.0


@dataclass(frozen=True)
class PositionReductionIntent:
    """Independent, auditable authority for reducing an existing position."""

    intent_id: str
    symbol: str
    action: str
    target_shares: int
    decision_time: datetime

    def __post_init__(self) -> None:
        for field_name, value in (
            ("intent_id", self.intent_id),
            ("symbol", self.symbol),
        ):
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"reduction_{field_name}_invalid")
        if self.action not in {"reduce", "exit"}:
            raise ValueError("reduction_action_invalid")
        if (
            isinstance(self.target_shares, bool)
            or not isinstance(self.target_shares, int)
            or self.target_shares < 0
        ):
            raise ValueError("reduction_target_shares_invalid")
        if self.action == "exit" and self.target_shares != 0:
            raise ValueError("exit_target_shares_must_be_zero")
        if self.action == "reduce" and self.target_shares == 0:
            raise ValueError("reduce_target_shares_must_be_positive")
        _aware(self.decision_time, field_name="reduction_decision_time")


@dataclass(frozen=True)
class AllocationDecision:
    symbol: str
    rank_score: float
    score_semantics: str
    conservative_planning_price_cny: float
    current_shares: int
    sellable_shares: int
    target_shares: int
    order_shares: int
    target_notional_cny: float
    estimated_order_cost_cny: float
    edge_estimate_bps: None
    edge_evidence_status: str
    statistical_promotion_eligible: bool
    expected_round_trip_cost_bps: float | None
    reason_codes: tuple[str, ...]
    reduction_intent_id: str | None = None
    reduction_intent_action: str | None = None
    thesis_risk_evaluated_order_shares: int = 0
    thesis_risk_group_effects: tuple[ThesisRiskGroupEffect, ...] = ()


@dataclass(frozen=True)
class SmallAccountPlan:
    policy_id: str
    execution_scope: str
    cost_policy_id: str
    capital_authority_id: str
    authority_generation: int
    account_as_of: datetime
    position_snapshot_receipt_id: str
    position_snapshot_sha256: str
    verification_receipt_sha256: str
    current_equity_cny: float
    risk_budget_base_cny: float
    max_positions: int
    target_gross_cny: float
    cash_after_orders_cny: float
    estimated_order_costs_cny: float
    estimated_adverse_fill_loss_cny: float
    undeployed_cash_cny: float
    undeployed_reason_codes: tuple[str, ...]
    decisions: tuple[AllocationDecision, ...]
    thesis_risk_policy_id: str
    thesis_risk_policy_sha256: str
    thesis_risk_policy_proof_sha256: str
    thesis_risk_exposure_receipt_sha256s: tuple[str, ...]
    thesis_risk_exposure_proof_sha256s: tuple[str, ...]
    thesis_risk_exposure_set_id: str
    thesis_risk_exposure_set_sha256: str
    thesis_risk_exposure_set_proof_sha256: str
    thesis_risk_runtime_authority_sha256: str
    thesis_risk_initial_group_exposures: tuple[tuple[str, str, float], ...]
    thesis_risk_final_group_exposures: tuple[tuple[str, str, float], ...]
    plan_sha256: str


def _reason_tuple(*reasons: str) -> tuple[str, ...]:
    result = tuple(dict.fromkeys(reasons))
    if any(reason not in _DECISION_REASONS for reason in result):
        raise ValueError("unknown_decision_reason")
    return result


def _max_target_shares(
    *,
    fill_price: float,
    policy: SmallAccountPolicy,
    single_name_max_cny: float,
    cost_policy: AShareCostPolicy,
) -> int:
    """Return one fixed minimum-economic probe, independent of rank."""

    lot_notional = fill_price * policy.lot_size
    lots = max(1, math.ceil(policy.minimum_economic_order_cny / lot_notional))
    while lots > 0:
        shares = lots * policy.lot_size
        notional = shares * fill_price
        if notional + commission(notional, cost_policy) + transfer_fee(
            notional, cost_policy
        ) <= (single_name_max_cny + 1e-9):
            return shares
        lots -= 1
    return 0


def optimize_small_account(
    *,
    candidates: Iterable[CandidateAllocationInput],
    account_snapshot: AccountAuthoritySnapshot,
    decision_time: datetime,
    account_authority_verifier: AccountAuthorityVerifier,
    thesis_risk_authority: ThesisRiskRuntimeAuthority | None = None,
    current_champion_selection_context: ChampionSelectionContext | None = None,
    champion_selection_verifier: ChampionSelectionVerifier | None = None,
    numeric_feature_snapshot_verifier: NumericPITFeatureSnapshotVerifier | None = None,
    reduction_intents: Iterable[PositionReductionIntent] = (),
    allow_new_risk: bool = True,
    policy: SmallAccountPolicy = SmallAccountPolicy(),
    cost_policy: AShareCostPolicy = ASHARE_RESEARCH_COST_POLICY_V1,
) -> SmallAccountPlan:
    if not isinstance(account_snapshot, AccountAuthoritySnapshot):
        raise ValueError("verified_account_snapshot_required")
    if type(allow_new_risk) is not bool:
        raise ValueError("allow_new_risk_must_be_bool")
    resolved_decision_time = _aware(decision_time, field_name="decision_time")
    if account_snapshot.account_as_of > resolved_decision_time:
        raise ValueError("account_snapshot_after_decision")
    _verify_account_authority_snapshot(
        snapshot=account_snapshot,
        decision_time=resolved_decision_time,
        verifier=account_authority_verifier,
    )
    if not isinstance(thesis_risk_authority, ThesisRiskRuntimeAuthority):
        raise ValueError("thesis_risk_runtime_authority_required")
    if thesis_risk_authority.decision_time != resolved_decision_time:
        raise ValueError("thesis_risk_runtime_authority_time_mismatch")
    thesis_risk_policy = thesis_risk_authority.policy
    thesis_risk_policy_proof = thesis_risk_authority.policy_proof
    thesis_risk_receipts = thesis_risk_authority.exposure_receipts
    if any(
        not isinstance(receipt, ThesisRiskExposureReceipt)
        for receipt in thesis_risk_receipts
    ):
        raise ValueError("thesis_risk_exposure_receipt_invalid")
    if len({receipt.exposure_id for receipt in thesis_risk_receipts}) != len(
        thesis_risk_receipts
    ):
        raise ValueError("duplicate_thesis_risk_exposure_id")
    thesis_risk_exposure_proofs = thesis_risk_authority.exposure_proofs
    for position in account_snapshot.positions:
        if not is_mainboard_tradable(position.symbol):
            raise ValueError(f"optimizer_symbol_out_of_scope:{position.symbol}")
    cash = float(account_snapshot.available_cash_cny)
    projected_gross = float(account_snapshot.current_gross_cny)
    current_equity = round(cash + projected_gross, 6)
    risk_budget_base = policy.risk_budget_base_cny(current_equity)
    single_name_max_cny = risk_budget_base * policy.single_name_max_pct
    gross_limit_cny = risk_budget_base * policy.stock_gross_limit_pct
    supplied_rows = tuple(candidates)
    for row in supplied_rows:
        if not isinstance(row, CandidateAllocationInput):
            raise ValueError("candidate_allocation_input_required")
        if isinstance(row.score_evidence, ChampionScoreReceipt):
            if not isinstance(
                current_champion_selection_context,
                ChampionSelectionContext,
            ):
                raise ValueError("current_champion_selection_context_required")
            try:
                verify_champion_score_receipt(
                    row.score_evidence,
                    expected_symbol=row.symbol,
                    expected_decision_time=resolved_decision_time,
                    current_selection_context=(current_champion_selection_context),
                    selection_verifier=champion_selection_verifier,
                    feature_snapshot_verifier=(numeric_feature_snapshot_verifier),
                )
            except ChampionContractError as exc:
                raise ValueError("candidate_score_evidence_invalid") from exc
    for row in supplied_rows:
        if not is_mainboard_tradable(row.symbol):
            raise ValueError(f"optimizer_symbol_out_of_scope:{row.symbol}")
    supplied_symbols = {row.symbol for row in supplied_rows}
    if any(row.decision_time != resolved_decision_time for row in supplied_rows):
        raise ValueError("candidate_decision_time_mismatch")
    position_by_symbol = {
        position.symbol: position for position in account_snapshot.positions
    }
    candidate_risk_receipts = {
        receipt.symbol: receipt
        for receipt in thesis_risk_receipts
        if receipt.exposure_kind == "candidate"
    }
    position_risk_receipts = {
        receipt.symbol: receipt
        for receipt in thesis_risk_receipts
        if receipt.exposure_kind == "position"
    }
    if len(candidate_risk_receipts) != sum(
        receipt.exposure_kind == "candidate" for receipt in thesis_risk_receipts
    ):
        raise ValueError("duplicate_candidate_thesis_risk_receipt")
    if len(position_risk_receipts) != sum(
        receipt.exposure_kind == "position" for receipt in thesis_risk_receipts
    ):
        raise ValueError("duplicate_position_thesis_risk_receipt")
    if set(candidate_risk_receipts) != {row.symbol for row in supplied_rows}:
        missing = {row.symbol for row in supplied_rows} - set(candidate_risk_receipts)
        if missing:
            raise ValueError("candidate_thesis_risk_receipt_missing")
        raise ValueError("candidate_thesis_risk_receipt_unexpected")
    expected_position_symbols = {
        position.symbol
        for position in account_snapshot.positions
        if position.total_shares > 0
    }
    if set(position_risk_receipts) != expected_position_symbols:
        missing = expected_position_symbols - set(position_risk_receipts)
        if missing:
            raise ValueError("position_thesis_risk_receipt_missing")
        raise ValueError("position_thesis_risk_receipt_unexpected")
    for row in supplied_rows:
        receipt = candidate_risk_receipts[row.symbol]
        if (
            receipt.binding_reference_id != row.score_receipt_sha256
            or receipt.binding_sha256 != row.score_receipt_sha256
            or receipt.notional_cny != 0.0
        ):
            raise ValueError("candidate_thesis_risk_binding_mismatch")
        position_receipt = position_risk_receipts.get(row.symbol)
        if position_receipt is not None and receipt.groups != position_receipt.groups:
            raise ValueError("candidate_position_thesis_risk_groups_mismatch")
    pending_groups_by_symbol: dict[str, ThesisRiskGroups] = {}
    for receipt in thesis_risk_receipts:
        if receipt.exposure_kind != "pending":
            continue
        reference_receipt = position_risk_receipts.get(
            receipt.symbol
        ) or candidate_risk_receipts.get(receipt.symbol)
        expected_groups = (
            reference_receipt.groups
            if reference_receipt is not None
            else pending_groups_by_symbol.get(receipt.symbol)
        )
        if expected_groups is not None and receipt.groups != expected_groups:
            raise ValueError("pending_thesis_risk_groups_mismatch")
        pending_groups_by_symbol[receipt.symbol] = receipt.groups
    for position in account_snapshot.positions:
        if position.total_shares <= 0:
            continue
        receipt = position_risk_receipts[position.symbol]
        expected_notional = round(
            position.total_shares * position.mark_price_cny,
            6,
        )
        if (
            receipt.binding_reference_id
            != account_snapshot.position_snapshot_receipt_id
            or receipt.binding_sha256 != account_snapshot.position_snapshot_sha256
            or abs(receipt.notional_cny - expected_notional) > 1e-6
        ):
            raise ValueError("position_thesis_risk_binding_mismatch")
    group_exposures = initial_group_exposures(thesis_risk_receipts)
    reduction_rows = tuple(reduction_intents)
    if any(
        not isinstance(intent, PositionReductionIntent) for intent in reduction_rows
    ):
        raise ValueError("reduction_intent_invalid")
    for intent in reduction_rows:
        if not is_mainboard_tradable(intent.symbol):
            raise ValueError(f"optimizer_symbol_out_of_scope:{intent.symbol}")
    if any(intent.decision_time != resolved_decision_time for intent in reduction_rows):
        raise ValueError("reduction_intent_decision_time_mismatch")
    if len({intent.intent_id for intent in reduction_rows}) != len(reduction_rows):
        raise ValueError("duplicate_reduction_intent_id")
    reduction_by_symbol = {intent.symbol: intent for intent in reduction_rows}
    if len(reduction_by_symbol) != len(reduction_rows):
        raise ValueError("duplicate_reduction_intent_symbol")
    for symbol, intent in reduction_by_symbol.items():
        position = position_by_symbol.get(symbol)
        if position is None:
            raise ValueError("reduction_intent_position_missing")
        if intent.target_shares >= position.total_shares:
            raise ValueError("reduction_target_must_be_below_current_shares")
    for row in supplied_rows:
        position = position_by_symbol.get(row.symbol)
        if (
            position is not None
            and abs(row.decision_reference_price - position.mark_price_cny) > 1e-9
        ):
            raise ValueError("candidate_position_mark_price_mismatch")
    rows_by_symbol = {row.symbol: row for row in supplied_rows}
    if len(rows_by_symbol) != len(supplied_rows):
        raise ValueError("duplicate_candidate_symbol")
    for position in account_snapshot.positions:
        rows_by_symbol.setdefault(
            position.symbol,
            _ExistingPositionCandidate(
                symbol=position.symbol,
                decision_time=resolved_decision_time,
                price_observed_at=position.price_observed_at,
                decision_reference_price=position.mark_price_cny,
            ),
        )
    rows = tuple(rows_by_symbol.values())
    if len({row.symbol for row in rows}) != len(rows):
        raise ValueError("duplicate_candidate_symbol")
    ordered = sorted(rows, key=lambda row: (-row.rank_score, row.symbol))
    projected_position_symbols = {
        position.symbol
        for position in account_snapshot.positions
        if position.total_shares > 0
    }
    decisions: list[AllocationDecision] = []
    thesis_risk_effects_by_symbol: dict[str, tuple[ThesisRiskGroupEffect, ...]] = {}
    undeployed_reasons: set[str] = set()
    estimated_order_costs = 0.0
    estimated_adverse_fill_loss = 0.0

    for candidate in ordered:
        current_position = position_by_symbol.get(candidate.symbol)
        current_shares = (
            current_position.total_shares if current_position is not None else 0
        )
        sellable_shares = (
            current_position.sellable_shares if current_position is not None else 0
        )
        planning_price = conservative_planning_price(
            side="buy",
            decision_reference_price=candidate.decision_reference_price,
            policy=cost_policy,
        )
        reference_price = float(candidate.decision_reference_price)
        current_notional = current_shares * reference_price
        reduction_intent = reduction_by_symbol.get(candidate.symbol)
        candidate_risk_receipt = candidate_risk_receipts.get(candidate.symbol)
        position_risk_receipt = position_risk_receipts.get(candidate.symbol)
        effect_groups = (
            position_risk_receipt.groups
            if position_risk_receipt is not None
            else candidate_risk_receipt.groups
            if candidate_risk_receipt is not None
            else None
        )
        if effect_groups is None:
            raise ValueError("decision_thesis_risk_groups_missing")
        zero_effects, _ = apply_group_delta(
            exposures=group_exposures,
            groups=effect_groups,
            requested_delta_cny=0.0,
            policy=thesis_risk_policy,
            policy_proof_sha256=thesis_risk_policy_proof.proof_sha256,
            enforce_cap=False,
        )
        thesis_risk_effects_by_symbol[candidate.symbol] = zero_effects
        if not allow_new_risk and reduction_intent is None:
            decisions.append(
                AllocationDecision(
                    symbol=candidate.symbol,
                    rank_score=candidate.rank_score,
                    score_semantics="uncalibrated_deterministic_rank_score",
                    conservative_planning_price_cny=reference_price,
                    current_shares=current_shares,
                    sellable_shares=sellable_shares,
                    target_shares=current_shares,
                    order_shares=0,
                    target_notional_cny=round(current_notional, 6),
                    estimated_order_cost_cny=0.0,
                    edge_estimate_bps=None,
                    edge_evidence_status="not_available_uncalibrated_rank_only",
                    statistical_promotion_eligible=False,
                    expected_round_trip_cost_bps=None,
                    reason_codes=_reason_tuple("new_risk_not_authorized"),
                )
            )
            continue
        if (
            current_shares == 0
            and reduction_intent is None
            and len(projected_position_symbols) >= policy.max_positions
        ):
            decisions.append(
                AllocationDecision(
                    symbol=candidate.symbol,
                    rank_score=candidate.rank_score,
                    score_semantics="uncalibrated_deterministic_rank_score",
                    conservative_planning_price_cny=planning_price,
                    current_shares=0,
                    sellable_shares=0,
                    target_shares=0,
                    order_shares=0,
                    target_notional_cny=0.0,
                    estimated_order_cost_cny=0.0,
                    edge_estimate_bps=None,
                    edge_evidence_status=("not_available_uncalibrated_rank_only"),
                    statistical_promotion_eligible=False,
                    expected_round_trip_cost_bps=None,
                    reason_codes=_reason_tuple("max_positions_limit"),
                )
            )
            undeployed_reasons.add("max_positions_limit")
            continue
        if (
            current_position is not None
            and candidate.symbol not in supplied_symbols
            and reduction_intent is None
        ):
            decisions.append(
                AllocationDecision(
                    symbol=candidate.symbol,
                    rank_score=candidate.rank_score,
                    score_semantics="uncalibrated_deterministic_rank_score",
                    conservative_planning_price_cny=reference_price,
                    current_shares=current_shares,
                    sellable_shares=sellable_shares,
                    target_shares=current_shares,
                    order_shares=0,
                    target_notional_cny=round(current_notional, 6),
                    estimated_order_cost_cny=0.0,
                    edge_estimate_bps=None,
                    edge_evidence_status="not_available_uncalibrated_rank_only",
                    statistical_promotion_eligible=False,
                    expected_round_trip_cost_bps=None,
                    reason_codes=_reason_tuple("not_in_candidate_set_hold"),
                )
            )
            continue
        target_shares = _max_target_shares(
            fill_price=planning_price,
            policy=policy,
            single_name_max_cny=single_name_max_cny,
            cost_policy=cost_policy,
        )
        if reduction_intent is not None:
            target_shares = reduction_intent.target_shares
        elif target_shares < current_shares:
            decisions.append(
                AllocationDecision(
                    symbol=candidate.symbol,
                    rank_score=candidate.rank_score,
                    score_semantics="uncalibrated_deterministic_rank_score",
                    conservative_planning_price_cny=reference_price,
                    current_shares=current_shares,
                    sellable_shares=sellable_shares,
                    target_shares=current_shares,
                    order_shares=0,
                    target_notional_cny=round(current_notional, 6),
                    estimated_order_cost_cny=0.0,
                    edge_estimate_bps=None,
                    edge_evidence_status="not_available_uncalibrated_rank_only",
                    statistical_promotion_eligible=False,
                    expected_round_trip_cost_bps=None,
                    reason_codes=_reason_tuple("reduction_requires_explicit_intent"),
                )
            )
            continue
        if (
            target_shares == 0
            and current_shares == 0
            and planning_price * policy.lot_size > single_name_max_cny
        ):
            decisions.append(
                AllocationDecision(
                    symbol=candidate.symbol,
                    rank_score=candidate.rank_score,
                    score_semantics="uncalibrated_deterministic_rank_score",
                    conservative_planning_price_cny=planning_price,
                    current_shares=current_shares,
                    sellable_shares=sellable_shares,
                    target_shares=current_shares,
                    order_shares=0,
                    target_notional_cny=round(current_notional, 6),
                    estimated_order_cost_cny=0.0,
                    edge_estimate_bps=None,
                    edge_evidence_status="not_available_uncalibrated_rank_only",
                    statistical_promotion_eligible=False,
                    expected_round_trip_cost_bps=None,
                    reason_codes=_reason_tuple("lot_not_affordable"),
                )
            )
            undeployed_reasons.add("lot_rounding")
            continue

        delta_shares = target_shares - current_shares
        delta_notional = abs(delta_shares) * reference_price
        if delta_shares == 0:
            decisions.append(
                AllocationDecision(
                    symbol=candidate.symbol,
                    rank_score=candidate.rank_score,
                    score_semantics="uncalibrated_deterministic_rank_score",
                    conservative_planning_price_cny=planning_price,
                    current_shares=current_shares,
                    sellable_shares=sellable_shares,
                    target_shares=current_shares,
                    order_shares=0,
                    target_notional_cny=round(current_notional, 6),
                    estimated_order_cost_cny=0.0,
                    edge_estimate_bps=None,
                    edge_evidence_status="not_available_uncalibrated_rank_only",
                    statistical_promotion_eligible=False,
                    expected_round_trip_cost_bps=None,
                    reason_codes=_reason_tuple("inside_no_trade_band"),
                )
            )
            undeployed_reasons.add("lot_rounding")
            continue
        if delta_shares > 0 and delta_notional <= policy.no_trade_band_cny:
            decisions.append(
                AllocationDecision(
                    symbol=candidate.symbol,
                    rank_score=candidate.rank_score,
                    score_semantics="uncalibrated_deterministic_rank_score",
                    conservative_planning_price_cny=planning_price,
                    current_shares=current_shares,
                    sellable_shares=sellable_shares,
                    target_shares=current_shares,
                    order_shares=0,
                    target_notional_cny=round(current_notional, 6),
                    estimated_order_cost_cny=0.0,
                    edge_estimate_bps=None,
                    edge_evidence_status="not_available_uncalibrated_rank_only",
                    statistical_promotion_eligible=False,
                    expected_round_trip_cost_bps=None,
                    reason_codes=_reason_tuple("inside_no_trade_band"),
                )
            )
            undeployed_reasons.add("lot_rounding")
            continue
        if delta_shares > 0 and delta_notional < policy.minimum_economic_order_cny:
            decisions.append(
                AllocationDecision(
                    symbol=candidate.symbol,
                    rank_score=candidate.rank_score,
                    score_semantics="uncalibrated_deterministic_rank_score",
                    conservative_planning_price_cny=planning_price,
                    current_shares=current_shares,
                    sellable_shares=sellable_shares,
                    target_shares=current_shares,
                    order_shares=0,
                    target_notional_cny=round(current_notional, 6),
                    estimated_order_cost_cny=0.0,
                    edge_estimate_bps=None,
                    edge_evidence_status="not_available_uncalibrated_rank_only",
                    statistical_promotion_eligible=False,
                    expected_round_trip_cost_bps=None,
                    reason_codes=_reason_tuple("minimum_economic_order"),
                )
            )
            undeployed_reasons.add("minimum_economic_order")
            continue

        sell_limited = False
        if delta_shares < 0:
            requested_sell_shares = abs(delta_shares)
            executable_sell_shares = min(requested_sell_shares, sellable_shares)
            if executable_sell_shares == 0:
                decisions.append(
                    AllocationDecision(
                        symbol=candidate.symbol,
                        rank_score=candidate.rank_score,
                        score_semantics="uncalibrated_deterministic_rank_score",
                        conservative_planning_price_cny=conservative_planning_price(
                            side="sell",
                            decision_reference_price=(
                                candidate.decision_reference_price
                            ),
                            policy=cost_policy,
                        ),
                        current_shares=current_shares,
                        sellable_shares=sellable_shares,
                        target_shares=current_shares,
                        order_shares=0,
                        target_notional_cny=round(current_notional, 6),
                        estimated_order_cost_cny=0.0,
                        edge_estimate_bps=None,
                        edge_evidence_status=("not_available_uncalibrated_rank_only"),
                        statistical_promotion_eligible=False,
                        expected_round_trip_cost_bps=None,
                        reason_codes=_reason_tuple(
                            "explicit_reduction_intent",
                            "t1_sellable_limit",
                        ),
                        reduction_intent_id=(
                            reduction_intent.intent_id
                            if reduction_intent is not None
                            else None
                        ),
                        reduction_intent_action=(
                            reduction_intent.action
                            if reduction_intent is not None
                            else None
                        ),
                    )
                )
                continue
            sell_quantity_rejection = ashare_sell_quantity_rejection_reason(
                current_shares=current_shares,
                sellable_shares=sellable_shares,
                requested_shares=executable_sell_shares,
            )
            if sell_quantity_rejection is not None:
                decisions.append(
                    AllocationDecision(
                        symbol=candidate.symbol,
                        rank_score=candidate.rank_score,
                        score_semantics="uncalibrated_deterministic_rank_score",
                        conservative_planning_price_cny=(
                            conservative_planning_price(
                                side="sell",
                                decision_reference_price=(
                                    candidate.decision_reference_price
                                ),
                                policy=cost_policy,
                            )
                        ),
                        current_shares=current_shares,
                        sellable_shares=sellable_shares,
                        target_shares=current_shares,
                        order_shares=0,
                        target_notional_cny=round(current_notional, 6),
                        estimated_order_cost_cny=0.0,
                        edge_estimate_bps=None,
                        edge_evidence_status=("not_available_uncalibrated_rank_only"),
                        statistical_promotion_eligible=False,
                        expected_round_trip_cost_bps=None,
                        reason_codes=_reason_tuple(
                            "explicit_reduction_intent",
                            sell_quantity_rejection,
                        ),
                        reduction_intent_id=(
                            reduction_intent.intent_id
                            if reduction_intent is not None
                            else None
                        ),
                        reduction_intent_action=(
                            reduction_intent.action
                            if reduction_intent is not None
                            else None
                        ),
                    )
                )
                continue
            sell_limited = executable_sell_shares < requested_sell_shares
            delta_shares = -executable_sell_shares

        round_trip = estimate_round_trip_cost(
            quantity=abs(delta_shares),
            entry_reference_price=planning_price,
            exit_reference_price=planning_price,
            policy=cost_policy,
        )
        if delta_shares > 0:
            max_gross_delta = max(0.0, gross_limit_cny - projected_gross)
            maximum_buy_cash = max(0.0, cash)
            affordable_lots = min(
                delta_shares // policy.lot_size,
                int(max_gross_delta // (reference_price * policy.lot_size)),
                int(maximum_buy_cash // (planning_price * policy.lot_size)),
            )
            while affordable_lots > 0:
                affordable_shares = affordable_lots * policy.lot_size
                notional = affordable_shares * planning_price
                if (
                    notional
                    + commission(notional, cost_policy)
                    + transfer_fee(notional, cost_policy)
                    <= cash + 1e-9
                ):
                    break
                affordable_lots -= 1
            order_shares = affordable_lots * policy.lot_size
            if order_shares == 0:
                reason = (
                    "gross_exposure_limit"
                    if max_gross_delta < planning_price * policy.lot_size
                    else "cash_limit"
                )
                decisions.append(
                    AllocationDecision(
                        symbol=candidate.symbol,
                        rank_score=candidate.rank_score,
                        score_semantics="uncalibrated_deterministic_rank_score",
                        conservative_planning_price_cny=planning_price,
                        current_shares=current_shares,
                        sellable_shares=sellable_shares,
                        target_shares=current_shares,
                        order_shares=0,
                        target_notional_cny=round(current_notional, 6),
                        estimated_order_cost_cny=0.0,
                        edge_estimate_bps=None,
                        edge_evidence_status=("not_available_uncalibrated_rank_only"),
                        statistical_promotion_eligible=False,
                        expected_round_trip_cost_bps=round_trip.total_cost_bps_on_entry,
                        reason_codes=_reason_tuple(reason),
                    )
                )
                undeployed_reasons.add(
                    "gross_exposure_limit"
                    if reason == "gross_exposure_limit"
                    else "candidate_capacity_exhausted"
                )
                continue
            if order_shares < delta_shares:
                undeployed_reasons.add("gross_exposure_limit")
            if candidate_risk_receipt is None:
                raise ValueError("candidate_thesis_risk_receipt_missing")
            group_effects, group_cap_exceeded = apply_group_delta(
                exposures=group_exposures,
                groups=candidate_risk_receipt.groups,
                requested_delta_cny=round(order_shares * reference_price, 6),
                policy=thesis_risk_policy,
                policy_proof_sha256=thesis_risk_policy_proof.proof_sha256,
                enforce_cap=True,
            )
            thesis_risk_effects_by_symbol[candidate.symbol] = group_effects
            if group_cap_exceeded:
                decisions.append(
                    AllocationDecision(
                        symbol=candidate.symbol,
                        rank_score=candidate.rank_score,
                        score_semantics="uncalibrated_deterministic_rank_score",
                        conservative_planning_price_cny=planning_price,
                        current_shares=current_shares,
                        sellable_shares=sellable_shares,
                        target_shares=current_shares,
                        order_shares=0,
                        target_notional_cny=round(current_notional, 6),
                        estimated_order_cost_cny=0.0,
                        edge_estimate_bps=None,
                        edge_evidence_status=("not_available_uncalibrated_rank_only"),
                        statistical_promotion_eligible=False,
                        expected_round_trip_cost_bps=(
                            round_trip.total_cost_bps_on_entry
                        ),
                        reason_codes=_reason_tuple("risk_group_cap"),
                        thesis_risk_evaluated_order_shares=order_shares,
                    )
                )
                undeployed_reasons.add("risk_group_cap")
                continue
            final_target_shares = current_shares + order_shares
            order_notional = order_shares * planning_price
            order_cost = round(
                commission(order_notional, cost_policy)
                + transfer_fee(order_notional, cost_policy),
                6,
            )
            cash = round(cash - order_notional - order_cost, 6)
            projected_gross = round(
                projected_gross + order_shares * reference_price,
                6,
            )
        else:
            planning_price = conservative_planning_price(
                side="sell",
                decision_reference_price=candidate.decision_reference_price,
                policy=cost_policy,
            )
            order_shares = delta_shares
            if position_risk_receipt is None:
                raise ValueError("position_thesis_risk_receipt_missing")
            group_effects, _ = apply_group_delta(
                exposures=group_exposures,
                groups=position_risk_receipt.groups,
                requested_delta_cny=round(
                    -abs(order_shares) * reference_price,
                    6,
                ),
                policy=thesis_risk_policy,
                policy_proof_sha256=thesis_risk_policy_proof.proof_sha256,
                enforce_cap=False,
            )
            thesis_risk_effects_by_symbol[candidate.symbol] = group_effects
            final_target_shares = current_shares + order_shares
            order_notional = abs(order_shares) * planning_price
            order_cost = round(
                commission(order_notional, cost_policy)
                + transfer_fee(order_notional, cost_policy)
                + order_notional * cost_policy.sell_stamp_duty_rate,
                6,
            )
            cash = round(cash + order_notional - order_cost, 6)
            projected_gross = round(
                projected_gross - abs(order_shares) * reference_price,
                6,
            )

        estimated_order_costs = round(estimated_order_costs + order_cost, 6)
        estimated_adverse_fill_loss = round(
            estimated_adverse_fill_loss
            + abs(order_shares) * abs(planning_price - reference_price),
            6,
        )

        decisions.append(
            AllocationDecision(
                symbol=candidate.symbol,
                rank_score=candidate.rank_score,
                score_semantics="uncalibrated_deterministic_rank_score",
                conservative_planning_price_cny=planning_price,
                current_shares=current_shares,
                sellable_shares=sellable_shares,
                target_shares=final_target_shares,
                order_shares=order_shares,
                target_notional_cny=round(
                    final_target_shares * reference_price,
                    6,
                ),
                estimated_order_cost_cny=order_cost,
                edge_estimate_bps=None,
                edge_evidence_status="not_available_uncalibrated_rank_only",
                statistical_promotion_eligible=False,
                expected_round_trip_cost_bps=round_trip.total_cost_bps_on_entry,
                reason_codes=_reason_tuple(
                    "allocated",
                    *(("explicit_reduction_intent",) if delta_shares < 0 else ()),
                    *(("t1_sellable_limit",) if sell_limited else ()),
                ),
                reduction_intent_id=(
                    reduction_intent.intent_id if reduction_intent is not None else None
                ),
                reduction_intent_action=(
                    reduction_intent.action if reduction_intent is not None else None
                ),
                thesis_risk_evaluated_order_shares=order_shares,
            )
        )
        if final_target_shares > 0:
            projected_position_symbols.add(candidate.symbol)
        else:
            projected_position_symbols.discard(candidate.symbol)

    undeployed = round(max(0.0, cash), 6)
    if undeployed > 0:
        undeployed_reasons.add("cash_reserve")
        if not ordered:
            undeployed_reasons.add("candidate_capacity_exhausted")
    normalized_reasons = tuple(sorted(undeployed_reasons))
    if any(reason not in _UNDEPLOYED_REASONS for reason in normalized_reasons):
        raise ValueError("unknown_undeployed_reason")
    decision_tuple = tuple(
        replace(
            decision,
            thesis_risk_group_effects=thesis_risk_effects_by_symbol[decision.symbol],
        )
        for decision in decisions
    )
    final_group_exposures = tuple(
        (dimension, group_id, round(exposure, 6))
        for (dimension, group_id), exposure in sorted(group_exposures.items())
    )
    digest_payload = {
        "policy_id": policy.policy_id,
        "execution_scope": policy.execution_scope,
        "cost_policy_id": cost_policy.policy_id,
        "capital_authority_id": account_snapshot.capital_authority_id,
        "authority_generation": account_snapshot.authority_generation,
        "account_as_of": account_snapshot.account_as_of.isoformat(),
        "position_snapshot_receipt_id": (account_snapshot.position_snapshot_receipt_id),
        "position_snapshot_sha256": account_snapshot.position_snapshot_sha256,
        "verification_receipt_sha256": (account_snapshot.verification_receipt_sha256),
        "current_equity_cny": current_equity,
        "risk_budget_base_cny": risk_budget_base,
        "max_positions": policy.max_positions,
        "target_gross_cny": projected_gross,
        "cash_after_orders_cny": cash,
        "estimated_order_costs_cny": estimated_order_costs,
        "estimated_adverse_fill_loss_cny": estimated_adverse_fill_loss,
        "undeployed_cash_cny": undeployed,
        "undeployed_reason_codes": normalized_reasons,
        "sizing_method": "fixed_minimum_economic_probe_v1",
        "candidate_score_receipts": {
            row.symbol: row.score_receipt_sha256 for row in supplied_rows
        },
        "thesis_risk_policy_id": thesis_risk_policy.policy_id,
        "thesis_risk_policy_sha256": thesis_risk_policy.policy_sha256,
        "thesis_risk_policy_proof_sha256": (thesis_risk_policy_proof.proof_sha256),
        "thesis_risk_exposure_receipt_sha256s": tuple(
            sorted(receipt.receipt_sha256 for receipt in thesis_risk_receipts)
        ),
        "thesis_risk_exposure_proof_sha256s": tuple(
            sorted(proof.proof_sha256 for proof in thesis_risk_exposure_proofs)
        ),
        "thesis_risk_exposure_set_id": (
            thesis_risk_authority.exposure_set_receipt.exposure_set_id
        ),
        "thesis_risk_exposure_set_sha256": (
            thesis_risk_authority.exposure_set_receipt.receipt_sha256
        ),
        "thesis_risk_exposure_set_proof_sha256": (
            thesis_risk_authority.exposure_set_proof.proof_sha256
        ),
        "thesis_risk_runtime_authority_sha256": (
            thesis_risk_authority.authority_sha256
        ),
        "thesis_risk_initial_group_exposures": (
            thesis_risk_authority.initial_group_exposures
        ),
        "thesis_risk_final_group_exposures": final_group_exposures,
        "decisions": [asdict(decision) for decision in decision_tuple],
    }
    plan_sha = hashlib.sha256(
        json.dumps(
            digest_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return SmallAccountPlan(
        policy_id=policy.policy_id,
        execution_scope=policy.execution_scope,
        cost_policy_id=cost_policy.policy_id,
        capital_authority_id=account_snapshot.capital_authority_id,
        authority_generation=account_snapshot.authority_generation,
        account_as_of=account_snapshot.account_as_of,
        position_snapshot_receipt_id=(account_snapshot.position_snapshot_receipt_id),
        position_snapshot_sha256=account_snapshot.position_snapshot_sha256,
        verification_receipt_sha256=(account_snapshot.verification_receipt_sha256),
        current_equity_cny=current_equity,
        risk_budget_base_cny=risk_budget_base,
        max_positions=policy.max_positions,
        target_gross_cny=projected_gross,
        cash_after_orders_cny=cash,
        estimated_order_costs_cny=estimated_order_costs,
        estimated_adverse_fill_loss_cny=estimated_adverse_fill_loss,
        undeployed_cash_cny=undeployed,
        undeployed_reason_codes=normalized_reasons,
        decisions=decision_tuple,
        thesis_risk_policy_id=thesis_risk_policy.policy_id,
        thesis_risk_policy_sha256=thesis_risk_policy.policy_sha256,
        thesis_risk_policy_proof_sha256=(thesis_risk_policy_proof.proof_sha256),
        thesis_risk_exposure_receipt_sha256s=tuple(
            sorted(receipt.receipt_sha256 for receipt in thesis_risk_receipts)
        ),
        thesis_risk_exposure_proof_sha256s=tuple(
            sorted(proof.proof_sha256 for proof in thesis_risk_exposure_proofs)
        ),
        thesis_risk_exposure_set_id=(
            thesis_risk_authority.exposure_set_receipt.exposure_set_id
        ),
        thesis_risk_exposure_set_sha256=(
            thesis_risk_authority.exposure_set_receipt.receipt_sha256
        ),
        thesis_risk_exposure_set_proof_sha256=(
            thesis_risk_authority.exposure_set_proof.proof_sha256
        ),
        thesis_risk_runtime_authority_sha256=(thesis_risk_authority.authority_sha256),
        thesis_risk_initial_group_exposures=(
            thesis_risk_authority.initial_group_exposures
        ),
        thesis_risk_final_group_exposures=final_group_exposures,
        plan_sha256=plan_sha,
    )
