from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from shared.runtime.market_evidence_authority import (
    AShareExecutionQuoteEvidence,
    AShareMarkEvidence,
    MarketEvidenceAuthorityError,
    MarketEvidenceAuthorityVerifier,
    MarketEvidenceContext,
    MarketEvidenceVerification,
    MarketSourceBinding,
    NonProductionFixtureMarketEvidenceAuthority,
    NonProductionFixtureMarketEvidenceVerifier,
    freeze_non_production_market_evidence,
)


UTC = timezone.utc
SOURCE_RECEIPT_SHA256 = "1" * 64
SOURCE_LINEAGE_SHA256 = "2" * 64
CALENDAR_RECEIPT_SHA256 = "3" * 64


def _source() -> MarketSourceBinding:
    return MarketSourceBinding(
        dataset_id="fixture.ashare.quotes.v1",
        catalog_version="fixture-catalog-v1",
        source_receipt_id="fixture-receipt-20260716",
        source_receipt_sha256=SOURCE_RECEIPT_SHA256,
        source_lineage_sha256=SOURCE_LINEAGE_SHA256,
        data_through=datetime(2026, 7, 16, 1, 29, 55, tzinfo=UTC),
        observed_at=datetime(2026, 7, 16, 1, 29, 55, tzinfo=UTC),
        available_at=datetime(2026, 7, 16, 1, 29, 56, tzinfo=UTC),
    )


def _context() -> MarketEvidenceContext:
    return MarketEvidenceContext(
        trade_date=date(2026, 7, 16),
        decision_as_of=datetime(2026, 7, 16, 1, 29, 57, tzinfo=UTC),
        capital_authority_id="ashare-capital-v1",
        authority_generation=7,
        execution_lineage_id="ashare-sim-20260716-v7",
        account_type="simulated",
        real_trading_enabled=False,
    )


def _mark() -> AShareMarkEvidence:
    source = replace(
        _source(),
        dataset_id="fixture.ashare.daily_close.v1",
        data_through=datetime(2026, 7, 15, 7, 0, tzinfo=UTC),
        observed_at=datetime(2026, 7, 15, 7, 0, tzinfo=UTC),
        available_at=datetime(2026, 7, 15, 7, 0, 5, tzinfo=UTC),
    )
    return AShareMarkEvidence(
        symbol="600000.SH",
        price_cny=12.34,
        market_session="close",
        source=source,
        session_calendar_receipt_sha256=CALENDAR_RECEIPT_SHA256,
        context=_context(),
    )


def _quote() -> AShareExecutionQuoteEvidence:
    return AShareExecutionQuoteEvidence(
        symbol="600000.SH",
        order_id="order-600000-buy-1",
        bid_price_cny=12.33,
        ask_price_cny=12.35,
        bid_size=20_000,
        ask_size=18_000,
        previous_close_cny=12.10,
        market_session="continuous_auction_am",
        execution_time=datetime(2026, 7, 16, 1, 29, 58, tzinfo=UTC),
        source=_source(),
        session_calendar_receipt_sha256=CALENDAR_RECEIPT_SHA256,
        context=_context(),
    )


def test_fixture_verifier_issues_content_bound_non_production_receipts() -> None:
    mark = _mark()
    quote = _quote()
    verifier = NonProductionFixtureMarketEvidenceVerifier(
        allowed_evidence_sha256s=frozenset({mark.sha256(), quote.sha256()}),
    )

    mark_receipt = verifier.verify(
        mark,
        expected_dataset_id=mark.source.dataset_id,
        frozen_at=datetime(2026, 7, 16, 1, 30, tzinfo=UTC),
    )
    quote_receipt = verifier.verify(
        quote,
        expected_dataset_id=quote.source.dataset_id,
        frozen_at=datetime(2026, 7, 16, 1, 30, tzinfo=UTC),
    )

    assert type(mark_receipt) is MarketEvidenceVerification
    assert mark_receipt.accepted is True
    assert mark_receipt.authority_tier == "non_production_fixture"
    assert mark_receipt.production_eligible is False
    assert mark_receipt.evidence_type == "mark"
    assert mark_receipt.evidence_sha256 == mark.sha256()
    assert mark_receipt.dataset_id == mark.source.dataset_id
    assert mark_receipt.catalog_version == mark.source.catalog_version
    assert mark_receipt.source_receipt_id == mark.source.source_receipt_id
    assert mark_receipt.source_receipt_sha256 == SOURCE_RECEIPT_SHA256
    assert mark_receipt.source_lineage_sha256 == SOURCE_LINEAGE_SHA256
    assert mark_receipt.symbol == mark.symbol
    assert mark_receipt.price_payload_sha256 == mark.price_payload_sha256()
    assert mark_receipt.market_session == mark.market_session
    assert mark_receipt.session_calendar_receipt_sha256 == (CALENDAR_RECEIPT_SHA256)
    assert mark_receipt.order_id is None
    assert mark_receipt.context_sha256 == mark.context.sha256()
    assert mark_receipt.proof_sha256 == mark_receipt.recompute_proof_sha256()

    assert quote_receipt.evidence_type == "execution_quote"
    assert quote_receipt.order_id == quote.order_id
    assert quote_receipt.execution_time == quote.execution_time
    assert quote_receipt.price_payload_sha256 == quote.price_payload_sha256()
    assert quote_receipt.proof_sha256 == quote_receipt.recompute_proof_sha256()
    assert quote_receipt.proof_sha256 != mark_receipt.proof_sha256


def test_verifier_has_no_default_implementation() -> None:
    with pytest.raises(TypeError):
        MarketEvidenceAuthorityVerifier()


def test_fixture_verifier_requires_exact_frozen_allowlist() -> None:
    mark = _mark()

    with pytest.raises(
        MarketEvidenceAuthorityError,
        match="allowed_evidence_sha256s_must_be_nonempty_frozenset",
    ):
        NonProductionFixtureMarketEvidenceVerifier(
            allowed_evidence_sha256s={mark.sha256()},  # type: ignore[arg-type]
        )

    verifier = NonProductionFixtureMarketEvidenceVerifier(
        allowed_evidence_sha256s=frozenset({mark.sha256()}),
    )
    with pytest.raises(
        AttributeError,
        match="fixture_market_evidence_allowlist_is_frozen",
    ):
        verifier._allowed_evidence_sha256s = frozenset({"f" * 64})
    changed = replace(mark, price_cny=12.35)
    with pytest.raises(
        MarketEvidenceAuthorityError,
        match="market_evidence_sha256_not_frozen",
    ):
        verifier.verify(
            changed,
            expected_dataset_id=changed.source.dataset_id,
            frozen_at=datetime(2026, 7, 16, 1, 30, tzinfo=UTC),
        )


def test_fixture_verifier_is_exact_and_cannot_be_promoted_by_subclass() -> None:
    with pytest.raises(TypeError, match="fixture_market_evidence_verifier_is_final"):

        class ForgedProductionVerifier(NonProductionFixtureMarketEvidenceVerifier):
            production_eligible = True


def test_non_production_receipt_cannot_self_declare_production_eligibility() -> None:
    mark = _mark()

    with pytest.raises(
        MarketEvidenceAuthorityError,
        match="non_production_fixture_cannot_be_production_eligible",
    ):
        MarketEvidenceVerification.issue(
            evidence=mark,
            verifier_id=(NonProductionFixtureMarketEvidenceVerifier.verifier_id),
            verifier_version=(
                NonProductionFixtureMarketEvidenceVerifier.verifier_version
            ),
            verifier_implementation_sha256=(
                NonProductionFixtureMarketEvidenceVerifier.verifier_implementation_sha256
            ),
            authority_tier="non_production_fixture",
            production_eligible=True,
            verified_at=datetime(2026, 7, 16, 1, 30, tzinfo=UTC),
            frozen_at=datetime(2026, 7, 16, 1, 30, tzinfo=UTC),
        )


def test_verifier_rejects_dataset_or_freeze_time_mismatch() -> None:
    quote = _quote()
    verifier = NonProductionFixtureMarketEvidenceVerifier(
        allowed_evidence_sha256s=frozenset({quote.sha256()}),
    )

    with pytest.raises(
        MarketEvidenceAuthorityError,
        match="market_evidence_dataset_id_mismatch",
    ):
        verifier.verify(
            quote,
            expected_dataset_id="fixture.ashare.other.v1",
            frozen_at=datetime(2026, 7, 16, 1, 30, tzinfo=UTC),
        )
    with pytest.raises(
        MarketEvidenceAuthorityError,
        match="market_evidence_frozen_before_available",
    ):
        verifier.verify(
            quote,
            expected_dataset_id=quote.source.dataset_id,
            frozen_at=datetime(2026, 7, 16, 1, 29, 55, tzinfo=UTC),
        )


def test_exact_evidence_types_and_receipt_proof_are_enforced() -> None:
    mark = _mark()
    verifier = NonProductionFixtureMarketEvidenceVerifier(
        allowed_evidence_sha256s=frozenset({mark.sha256()}),
    )

    class MarkSubclass(AShareMarkEvidence):
        pass

    forged = MarkSubclass(**mark.__dict__)
    with pytest.raises(
        MarketEvidenceAuthorityError,
        match="market_evidence_type_untrusted",
    ):
        verifier.verify(
            forged,
            expected_dataset_id=forged.source.dataset_id,
            frozen_at=datetime(2026, 7, 16, 1, 30, tzinfo=UTC),
        )

    receipt = verifier.verify(
        mark,
        expected_dataset_id=mark.source.dataset_id,
        frozen_at=datetime(2026, 7, 16, 1, 30, tzinfo=UTC),
    )
    with pytest.raises(
        MarketEvidenceAuthorityError,
        match="market_evidence_verification_proof_mismatch",
    ):
        replace(receipt, source_lineage_sha256="f" * 64)


@pytest.mark.parametrize(
    ("factory", "reason"),
    [
        (
            lambda: replace(_source(), source_receipt_sha256="bad"),
            "source_receipt_sha256_invalid",
        ),
        (
            lambda: replace(_source(), source_lineage_sha256="bad"),
            "source_lineage_sha256_invalid",
        ),
        (
            lambda: replace(
                _source(),
                available_at=datetime(2026, 7, 16, 1, 29, 54, tzinfo=UTC),
            ),
            "market_source_time_order_invalid",
        ),
        (
            lambda: replace(_quote(), ask_price_cny=float("nan")),
            "ask_price_cny_must_be_nonnegative_finite",
        ),
        (
            lambda: replace(_quote(), order_id=" "),
            "order_id_invalid",
        ),
        (
            lambda: replace(_context(), real_trading_enabled=True),
            "real_trading_enabled_must_be_false",
        ),
    ],
)
def test_contract_rejects_malformed_source_price_order_and_context(
    factory,
    reason: str,
) -> None:
    with pytest.raises(MarketEvidenceAuthorityError, match=reason):
        factory()


def test_canonical_hash_binds_source_session_time_order_and_context() -> None:
    quote = _quote()
    variants = (
        replace(quote, symbol="600001.SH"),
        replace(quote, ask_price_cny=12.36),
        replace(quote, market_session="continuous_auction_pm"),
        replace(quote, session_calendar_receipt_sha256="4" * 64),
        replace(quote, execution_time=datetime(2026, 7, 16, 1, 30, tzinfo=UTC)),
        replace(quote, order_id="order-600000-buy-2"),
        replace(
            quote,
            source=replace(quote.source, source_receipt_id="different-receipt"),
        ),
        replace(
            quote,
            context=replace(
                quote.context,
                execution_lineage_id="ashare-sim-20260716-v8",
            ),
        ),
    )

    assert len({quote.sha256(), *(item.sha256() for item in variants)}) == (
        len(variants) + 1
    )


def test_execution_quote_may_arrive_after_decision_but_before_execution() -> None:
    context = replace(
        _context(),
        decision_as_of=datetime(2026, 7, 16, 1, 29, 55, tzinfo=UTC),
    )
    source = replace(
        _source(),
        observed_at=datetime(2026, 7, 16, 1, 29, 56, tzinfo=UTC),
        available_at=datetime(2026, 7, 16, 1, 29, 57, tzinfo=UTC),
    )
    quote = replace(
        _quote(),
        context=context,
        source=source,
        execution_time=datetime(2026, 7, 16, 1, 29, 58, tzinfo=UTC),
    )
    verifier = NonProductionFixtureMarketEvidenceVerifier(
        allowed_evidence_sha256s=frozenset({quote.sha256()}),
    )

    receipt = verifier.verify(
        quote,
        expected_dataset_id=quote.source.dataset_id,
        frozen_at=datetime(2026, 7, 16, 1, 30, tzinfo=UTC),
    )

    assert receipt.decision_as_of < receipt.available_at < receipt.execution_time


def test_frozen_fixture_authority_binds_exact_evidence_and_verification() -> None:
    mark = _mark()
    authority = freeze_non_production_market_evidence(
        mark,
        expected_dataset_id=mark.source.dataset_id,
        frozen_at=datetime(2026, 7, 16, 1, 30, tzinfo=UTC),
    )

    assert type(authority) is NonProductionFixtureMarketEvidenceAuthority
    assert authority.evidence is mark
    assert authority.verification.evidence_sha256 == mark.sha256()
    assert authority.production_eligible is False
    assert len(authority.authority_sha256) == 64

    with pytest.raises(TypeError, match="fixture_market_evidence_authority_is_final"):

        class ForgedFixtureAuthority(NonProductionFixtureMarketEvidenceAuthority):
            pass
