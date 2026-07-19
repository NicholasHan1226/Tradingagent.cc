"""Bind the small-account optimizer to the canonical simulated capital ledger.

This adapter is intentionally read-only.  It projects one already reconciled
``MarketCapitalLedger`` head into the optimizer contract and returns an
independent verifier that re-reads the ledger at decision time.  Any intervening
capital mutation, missing mark timestamp, active reservation, or T+1 ambiguity
fails closed instead of creating a second account truth.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Mapping

from shared.portfolio.small_account_optimizer import (
    AccountAuthoritySnapshot,
    AccountAuthorityVerification,
    AccountAuthorityVerifier,
    AccountPositionSnapshot,
    account_authority_content_sha256,
    account_position_snapshot_sha256,
)

from .capital_stages import PaperCapitalAccount, PaperCapitalStageError


_CN_TZ = timezone(timedelta(hours=8))
_ZERO_SHA256 = "0" * 64


class CanonicalAccountAuthorityError(RuntimeError):
    """Raised when canonical capital cannot be projected without guessing."""


def _aware(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CanonicalAccountAuthorityError(f"{field_name}_timezone_required")
    if value.utcoffset() is None:
        raise CanonicalAccountAuthorityError(f"{field_name}_timezone_required")
    return value


def _trade_date(value: str) -> str:
    normalized = str(value or "").strip().replace("-", "")
    if len(normalized) != 8 or not normalized.isdigit():
        raise CanonicalAccountAuthorityError("canonical_account_trade_date_invalid")
    try:
        datetime.strptime(normalized, "%Y%m%d")
    except ValueError as exc:
        raise CanonicalAccountAuthorityError(
            "canonical_account_trade_date_invalid"
        ) from exc
    return normalized


def _normalized_observations(
    values: Mapping[str, datetime],
) -> Mapping[str, datetime]:
    try:
        rows = dict(values)
    except (TypeError, ValueError) as exc:
        raise CanonicalAccountAuthorityError(
            "canonical_account_mark_observations_invalid"
        ) from exc
    normalized: dict[str, datetime] = {}
    for raw_symbol, raw_instant in rows.items():
        symbol = str(raw_symbol or "").strip().upper()
        if not symbol or symbol in normalized:
            raise CanonicalAccountAuthorityError(
                "canonical_account_mark_observations_invalid"
            )
        normalized[symbol] = _aware(
            raw_instant,
            field_name="canonical_account_mark_observed_at",
        )
    return MappingProxyType(normalized)


def _unverified_snapshot(
    *,
    account: PaperCapitalAccount,
    decision_time: datetime,
    trade_date: str,
    mark_observed_at: Mapping[str, datetime],
) -> AccountAuthoritySnapshot:
    if type(account) is not PaperCapitalAccount:
        raise CanonicalAccountAuthorityError("paper_capital_account_required")
    decision = _aware(decision_time, field_name="canonical_account_decision_time")
    normalized_date = _trade_date(trade_date)
    if decision.astimezone(_CN_TZ).strftime("%Y%m%d") != normalized_date:
        raise CanonicalAccountAuthorityError("canonical_account_trade_date_mismatch")

    capital = account.ledger.snapshot()
    if (
        capital.market != "ashare"
        or capital.real_trading_enabled
        or capital.authority_id != account.ledger.policy.capital_authority_id
        or capital.authority_generation != account.ledger.policy.authority_generation
    ):
        raise CanonicalAccountAuthorityError("canonical_account_identity_invalid")
    if not capital.reconciled:
        raise CanonicalAccountAuthorityError("canonical_account_not_reconciled")
    if capital.unreconciled_fill_commit_ids:
        raise CanonicalAccountAuthorityError(
            "canonical_account_unreconciled_fills_present"
        )
    if (
        abs(capital.active_reservations_cny) > 1e-9
        or abs(capital.reserved_cash_cny) > 1e-9
        or abs(capital.reserved_exposure_cny) > 1e-9
        or abs(capital.frozen_order_cash_cny) > 1e-9
    ):
        raise CanonicalAccountAuthorityError("canonical_account_pending_orders_present")

    quantities = dict(capital.positions_quantity_by_risk_unit)
    observations = _normalized_observations(mark_observed_at)
    if set(observations) - set(quantities):
        raise CanonicalAccountAuthorityError(
            "canonical_account_unknown_mark_observation"
        )
    if set(quantities) != set(observations):
        raise CanonicalAccountAuthorityError(
            "canonical_account_mark_observation_missing"
        )
    try:
        evidence_observations, mark_evidence_sha256 = (
            account.verified_mark_evidence_binding(
                symbols=tuple(sorted(quantities)),
                pit_timestamp=decision.isoformat(),
                trade_date_value=datetime.strptime(
                    normalized_date,
                    "%Y%m%d",
                )
                .date()
                .isoformat(),
            )
        )
    except PaperCapitalStageError as exc:
        raise CanonicalAccountAuthorityError(
            "canonical_account_mark_evidence_invalid"
        ) from exc
    if dict(observations) != dict(evidence_observations):
        raise CanonicalAccountAuthorityError(
            "canonical_account_mark_observation_evidence_mismatch"
        )
    try:
        sellable = account.ledger.ashare_sellable_quantities(normalized_date)
    except Exception as exc:
        raise CanonicalAccountAuthorityError(
            "canonical_account_sellable_projection_failed"
        ) from exc
    if set(sellable) != set(quantities):
        raise CanonicalAccountAuthorityError(
            "canonical_account_sellable_projection_mismatch"
        )

    positions: list[AccountPositionSnapshot] = []
    for symbol in sorted(quantities):
        quantity = quantities[symbol]
        mark = account.mark_prices.get(symbol)
        observed_at = observations[symbol]
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity <= 0
            or isinstance(mark, bool)
            or not isinstance(mark, (int, float))
            or float(mark) <= 0.0
            or observed_at > decision
        ):
            raise CanonicalAccountAuthorityError(
                "canonical_account_position_projection_invalid"
            )
        positions.append(
            AccountPositionSnapshot(
                symbol=symbol,
                total_shares=quantity,
                sellable_shares=int(sellable[symbol]),
                mark_price_cny=float(mark),
                price_observed_at=observed_at,
            )
        )
    frozen_positions = tuple(positions)
    gross = round(
        sum(row.total_shares * row.mark_price_cny for row in frozen_positions),
        6,
    )
    if abs(gross - capital.positions_market_value_cny) > 1e-6:
        raise CanonicalAccountAuthorityError("canonical_account_market_value_mismatch")
    available_cash = round(
        capital.cash_balance_cny
        - capital.frozen_order_cash_cny
        - capital.reserved_cash_cny,
        6,
    )
    if available_cash < -1e-9:
        raise CanonicalAccountAuthorityError("canonical_account_cash_invalid")
    receipt_id = (
        f"market-capital:{capital.event_id}:{capital.event_checksum}:"
        f"marks:{mark_evidence_sha256}"
    )
    return AccountAuthoritySnapshot(
        capital_authority_id=capital.authority_id,
        authority_generation=capital.authority_generation,
        account_as_of=decision,
        available_cash_cny=max(0.0, available_cash),
        current_gross_cny=gross,
        positions=frozen_positions,
        position_snapshot_receipt_id=receipt_id,
        position_snapshot_sha256=account_position_snapshot_sha256(frozen_positions),
        verification_receipt_sha256=_ZERO_SHA256,
        authority_source_class="canonical_authority",
    )


class MarketCapitalAccountAuthorityVerifier(AccountAuthorityVerifier):
    """Re-read the canonical ledger and reject any post-snapshot drift."""

    verifier_id = "market-capital-account-authority-verifier"
    verifier_version = "1"

    def __init__(
        self,
        *,
        account: PaperCapitalAccount,
        trade_date: str,
        mark_observed_at: Mapping[str, datetime],
    ) -> None:
        self._account = account
        self._trade_date = _trade_date(trade_date)
        self._mark_observed_at = _normalized_observations(mark_observed_at)

    def verify(
        self,
        snapshot: AccountAuthoritySnapshot,
        *,
        decision_time: datetime,
    ) -> AccountAuthorityVerification:
        try:
            current = _unverified_snapshot(
                account=self._account,
                decision_time=decision_time,
                trade_date=self._trade_date,
                mark_observed_at=self._mark_observed_at,
            )
        except CanonicalAccountAuthorityError:
            raise
        if (
            account_authority_content_sha256(current)
            != account_authority_content_sha256(snapshot)
            or current.position_snapshot_receipt_id
            != snapshot.position_snapshot_receipt_id
        ):
            raise CanonicalAccountAuthorityError("canonical_account_authority_drifted")
        proof = AccountAuthorityVerification.create(
            snapshot=current,
            verifier_id=self.verifier_id,
            verifier_version=self.verifier_version,
            verified_at=decision_time,
            valid_until=decision_time,
            promotion_eligible=False,
        )
        if proof.verification_receipt_sha256 != (snapshot.verification_receipt_sha256):
            raise CanonicalAccountAuthorityError(
                "canonical_account_verification_receipt_mismatch"
            )
        return proof


def build_canonical_account_authority(
    *,
    account: PaperCapitalAccount,
    decision_time: datetime,
    trade_date: str,
    mark_observed_at: Mapping[str, datetime],
) -> tuple[AccountAuthoritySnapshot, MarketCapitalAccountAuthorityVerifier]:
    """Create one immutable optimizer input and its live canonical verifier."""

    snapshot = _unverified_snapshot(
        account=account,
        decision_time=decision_time,
        trade_date=trade_date,
        mark_observed_at=mark_observed_at,
    )
    proof = AccountAuthorityVerification.create(
        snapshot=snapshot,
        verifier_id=MarketCapitalAccountAuthorityVerifier.verifier_id,
        verifier_version=MarketCapitalAccountAuthorityVerifier.verifier_version,
        verified_at=decision_time,
        valid_until=decision_time,
        promotion_eligible=False,
    )
    bound = replace(
        snapshot,
        verification_receipt_sha256=proof.verification_receipt_sha256,
    )
    verifier = MarketCapitalAccountAuthorityVerifier(
        account=account,
        trade_date=trade_date,
        mark_observed_at=mark_observed_at,
    )
    verifier.verify(bound, decision_time=decision_time)
    return bound, verifier


__all__ = [
    "CanonicalAccountAuthorityError",
    "MarketCapitalAccountAuthorityVerifier",
    "build_canonical_account_authority",
]
