"""Causal evaluation and network-closed fixture runtime orchestration."""

from __future__ import annotations

from decimal import Decimal, ROUND_UP
from pathlib import Path
import stat
from typing import Any, Mapping

from Crypto.capital_policy import CRYPTO_CAPITAL_POLICY, CryptoCapitalPolicy

from .contracts import (
    DECISION_CONTRACT,
    FROZEN_CHAMPION,
    FROZEN_SLIPPAGE_BPS,
    FROZEN_TAKER_FEE_RATE,
    MONEY_QUANTUM,
    ORDER_INTENT_CONTRACT,
    PAPER_BROKER_CONTRACT,
    PAPER_RECEIPT_CONTRACT,
    RUN_BUNDLE_CONTRACT,
    WIRE_CONTRACT,
    CryptoEvidenceError,
    CryptoLedgerError,
    CryptoSafetyError,
    FrozenChampionCandidate,
    OrderIntent,
    PaperFillReceipt,
    QualifiedFixtureEvidence,
    TimeframeDecision,
    _assert_simulation_only,
    _assert_canonical_champion,
    _assert_canonical_policy,
    _canonical_json,
    _canonical_value,
    _ceil_to_step,
    _floor_to_step,
    _is_step_aligned,
    _non_authority_fields,
    _sha256,
)
from .evidence import qualify_fixture_evidence
from .ledger import CryptoCapitalLedger, _open_runtime_ledger
from .replay import (
    _assert_frozen_position_cap,
    _business_snapshot,
    _cycle_claim_payload,
    _fill_event_payload,
    _journal_llm_sidecar,
    _read_json,
    _reserve_event_payload,
    _run_id,
    _sample_review_payload,
    _verify_run_bundle,
    _write_projection,
)


def evaluate_frozen_champion(
    evidence: QualifiedFixtureEvidence,
    champion: FrozenChampionCandidate = FROZEN_CHAMPION,
) -> TimeframeDecision:
    _assert_canonical_champion(champion)
    bars_1h = evidence.bars_5m[-12:]
    bars_15m = evidence.bars_5m[-3:]
    regime_return = bars_1h[-1].close / bars_1h[0].open - Decimal("1")
    decision_return = bars_15m[-1].close / bars_15m[0].open - Decimal("1")
    regime = (
        "risk_on" if regime_return >= champion.minimum_regime_return else "defensive"
    )
    if regime == "risk_on" and decision_return >= champion.minimum_decision_return:
        action = "buy"
        reason = "frozen_momentum_threshold_passed"
    else:
        action = "observe"
        reason = "frozen_momentum_threshold_not_met"
    material = {
        "contract": DECISION_CONTRACT,
        "champion_sha256": champion.sha256,
        "symbol": evidence.symbol,
        "execution_slot": evidence.next_executable_quote.observed_at,
        "regime_return": regime_return,
        "decision_return": decision_return,
        "action": action,
        "evidence_receipt_id": evidence.receipt_id,
        "market_evidence_sha256": evidence.market_evidence_sha256,
    }
    return TimeframeDecision(
        contract=DECISION_CONTRACT,
        decision_id=f"crypto-decision-{_sha256(material)[:24]}",
        champion_id=champion.champion_id,
        champion_sha256=champion.sha256,
        symbol=evidence.symbol,
        regime_interval=champion.regime_interval,
        decision_interval=champion.decision_interval,
        execution_interval=champion.execution_interval,
        execution_slot=evidence.next_executable_quote.observed_at,
        decision_observed_at=evidence.observed_at,
        regime_return=regime_return,
        decision_return=decision_return,
        regime=regime,
        action=action,
        reason=reason,
        evidence_receipt_id=evidence.receipt_id,
        market_evidence_sha256=evidence.market_evidence_sha256,
    )


def build_order_intent(
    evidence: QualifiedFixtureEvidence,
    decision: TimeframeDecision,
    *,
    policy: CryptoCapitalPolicy = CRYPTO_CAPITAL_POLICY,
    champion: FrozenChampionCandidate = FROZEN_CHAMPION,
    fee_rate: Decimal = FROZEN_TAKER_FEE_RATE,
) -> OrderIntent:
    _assert_canonical_policy(policy)
    _assert_canonical_champion(champion)
    expected_decision = evaluate_frozen_champion(evidence, champion)
    if _canonical_json(decision.to_payload()) != _canonical_json(
        expected_decision.to_payload()
    ):
        raise CryptoEvidenceError("decision_not_issued_by_frozen_champion")
    if decision.action != "buy":
        raise CryptoEvidenceError("decision_is_not_order_eligible")
    quote = evidence.next_executable_quote
    price = _ceil_to_step(
        quote.ask * (Decimal("1") + FROZEN_SLIPPAGE_BPS / Decimal("10000")),
        evidence.rules.price_tick,
    )
    if not _is_step_aligned(price, evidence.rules.price_tick):
        raise CryptoEvidenceError("execution_price_off_tick")
    target_notional = policy.initial_cash * champion.target_capital_pct
    quantity = _floor_to_step(target_notional / price, evidence.rules.quantity_step)
    if quantity < evidence.rules.min_quantity or not _is_step_aligned(
        quantity, evidence.rules.quantity_step
    ):
        raise CryptoEvidenceError("order_quantity_not_eligible")
    notional = quantity * price
    if notional < evidence.rules.min_notional:
        raise CryptoEvidenceError("order_notional_below_minimum")
    if not isinstance(fee_rate, Decimal) or fee_rate != FROZEN_TAKER_FEE_RATE:
        raise CryptoEvidenceError("paper_fee_rate_must_match_frozen_contract")
    maximum_fee = (notional * fee_rate).quantize(MONEY_QUANTUM, rounding=ROUND_UP)
    material = {
        "contract": ORDER_INTENT_CONTRACT,
        "broker_contract": PAPER_BROKER_CONTRACT,
        "authority_id": policy.authority_id,
        "authority_generation": policy.generation,
        "account_id": policy.account_id,
        "symbol": evidence.symbol,
        "side": "buy",
        "order_type": "fixture_market_at_next_quote",
        "quantity": quantity,
        "quote_bid": quote.bid,
        "quote_ask": quote.ask,
        "spread": quote.spread,
        "slippage_bps": FROZEN_SLIPPAGE_BPS,
        "slippage_amount": price - quote.ask,
        "reference_price": price,
        "notional": notional,
        "fee_rate": fee_rate,
        "maximum_fee": maximum_fee,
        "execution_slot": quote.observed_at,
        "evidence_receipt_id": evidence.receipt_id,
        "market_evidence_sha256": evidence.market_evidence_sha256,
        "champion_sha256": champion.sha256,
        "decision_id": decision.decision_id,
    }
    return OrderIntent(
        contract=ORDER_INTENT_CONTRACT,
        intent_id=f"crypto-intent-{_sha256(material)[:24]}",
        broker_contract=PAPER_BROKER_CONTRACT,
        authority_id=policy.authority_id,
        authority_generation=policy.generation,
        account_id=policy.account_id,
        symbol=evidence.symbol,
        side="buy",
        order_type="fixture_market_at_next_quote",
        quantity=quantity,
        quote_bid=quote.bid,
        quote_ask=quote.ask,
        spread=quote.spread,
        slippage_bps=FROZEN_SLIPPAGE_BPS,
        slippage_amount=price - quote.ask,
        reference_price=price,
        notional=notional,
        fee_rate=fee_rate,
        maximum_fee=maximum_fee,
        execution_slot=quote.observed_at,
        evidence_receipt_id=evidence.receipt_id,
        market_evidence_sha256=evidence.market_evidence_sha256,
        champion_id=champion.champion_id,
        champion_sha256=champion.sha256,
        decision_id=decision.decision_id,
    )


def execute_fixture_paper_order(
    intent: OrderIntent,
    evidence: QualifiedFixtureEvidence,
) -> PaperFillReceipt:
    """Create a deterministic local receipt without an external broker call."""

    _assert_simulation_only()
    _assert_canonical_policy(CRYPTO_CAPITAL_POLICY)
    _assert_canonical_champion(FROZEN_CHAMPION)
    expected_decision = evaluate_frozen_champion(evidence, FROZEN_CHAMPION)
    if expected_decision.action != "buy":
        raise CryptoEvidenceError("fixture_evidence_not_order_eligible")
    expected_intent = build_order_intent(evidence, expected_decision)
    if _canonical_json(intent.to_payload()) != _canonical_json(
        expected_intent.to_payload()
    ):
        raise CryptoEvidenceError("intent_not_issued_by_frozen_champion")
    if intent.broker_contract != PAPER_BROKER_CONTRACT:
        raise CryptoSafetyError("paper_broker_contract_mismatch")
    if intent.market_evidence_sha256 != evidence.market_evidence_sha256:
        raise CryptoEvidenceError("intent_market_evidence_mismatch")
    if intent.evidence_receipt_id != evidence.receipt_id:
        raise CryptoEvidenceError("intent_evidence_receipt_mismatch")
    quote = evidence.next_executable_quote
    expected_price = _ceil_to_step(
        quote.ask * (Decimal("1") + FROZEN_SLIPPAGE_BPS / Decimal("10000")),
        evidence.rules.price_tick,
    )
    if intent.reference_price != expected_price:
        raise CryptoEvidenceError("intent_execution_price_mismatch")
    if (
        intent.symbol != evidence.symbol
        or intent.quantity < evidence.rules.min_quantity
    ):
        raise CryptoEvidenceError("intent_instrument_binding_mismatch")
    if not _is_step_aligned(intent.quantity, evidence.rules.quantity_step):
        raise CryptoEvidenceError("intent_quantity_off_step")
    if not _is_step_aligned(intent.reference_price, evidence.rules.price_tick):
        raise CryptoEvidenceError("intent_price_off_tick")
    if intent.notional != intent.quantity * intent.reference_price:
        raise CryptoEvidenceError("intent_notional_mismatch")
    if intent.notional < evidence.rules.min_notional:
        raise CryptoEvidenceError("intent_notional_below_minimum")
    expected_fee = (intent.notional * intent.fee_rate).quantize(
        MONEY_QUANTUM, rounding=ROUND_UP
    )
    if intent.maximum_fee != expected_fee:
        raise CryptoEvidenceError("intent_fee_mismatch")
    if intent.execution_slot != quote.observed_at:
        raise CryptoEvidenceError("intent_execution_slot_mismatch")
    if quote.observed_at < evidence.observed_at:
        raise CryptoEvidenceError("execution_quote_precedes_observation")
    material = {
        "contract": PAPER_RECEIPT_CONTRACT,
        "intent_id": intent.intent_id,
        "status": "fixture_simulated",
        "filled_quantity": intent.quantity,
        "average_price": intent.reference_price,
        "notional": intent.notional,
        "fee": intent.maximum_fee,
        "filled_at": quote.observed_at,
        "evidence_receipt_id": evidence.receipt_id,
    }
    return PaperFillReceipt(
        contract=PAPER_RECEIPT_CONTRACT,
        receipt_id=f"crypto-paper-receipt-{_sha256(material)[:24]}",
        broker_contract=PAPER_BROKER_CONTRACT,
        authority_id=intent.authority_id,
        authority_generation=intent.authority_generation,
        account_id=intent.account_id,
        intent_id=intent.intent_id,
        symbol=intent.symbol,
        side=intent.side,
        status="fixture_simulated",
        filled_quantity=intent.quantity,
        average_price=intent.reference_price,
        notional=intent.notional,
        fee=intent.maximum_fee,
        fee_asset="USDT",
        filled_at=quote.observed_at,
        evidence_receipt_id=evidence.receipt_id,
        market_evidence_sha256=evidence.market_evidence_sha256,
    )


def _run_prepared_fixture_cycle(
    *,
    bundle_path: Path,
    ledger: CryptoCapitalLedger,
    run_id: str,
    fixture_payload: Mapping[str, Any],
    evidence: QualifiedFixtureEvidence,
    policy: CryptoCapitalPolicy,
    champion: FrozenChampionCandidate,
    decision: TimeframeDecision,
    prepared_intent: OrderIntent | None,
    prepared_receipt: PaperFillReceipt | None,
) -> dict[str, Any]:
    _assert_canonical_policy(policy)
    _assert_canonical_champion(champion)
    if bundle_path.exists():
        bundle = _read_json(bundle_path)
        _verify_run_bundle(
            bundle,
            run_id=run_id,
            fixture_payload=fixture_payload,
            evidence=evidence,
            policy=policy,
            champion=champion,
            decision=decision,
            expected_intent=prepared_intent,
            expected_receipt=prepared_receipt,
            ledger=ledger,
        )
        return {"bundle": bundle, "idempotent_replay": True}

    ledger._ensure_opening()
    mark = evidence.next_executable_quote.bid
    mark_inputs = {
        "marks": {evidence.symbol: mark},
        "mark_slots": {evidence.symbol: evidence.next_executable_quote.observed_at},
        "valuation_slot": evidence.next_executable_quote.observed_at,
    }
    reconcile_reference = f"reconcile:{run_id}"
    existing_reconcile = ledger.event_by_reference(reconcile_reference)
    claim_reference = f"cycle:{run_id}"
    claim_payload = _cycle_claim_payload(
        run_id=run_id,
        fixture_payload=fixture_payload,
        evidence=evidence,
        policy=policy,
        champion=champion,
    )
    existing_claim = ledger.event_by_reference(claim_reference)
    prechecked_before: dict[str, Any] | None = None
    if existing_reconcile is None:
        incomplete_runs, last_valuation, account_valuation = ledger.account_cycle_guard(
            symbol=evidence.symbol
        )
        foreign_incomplete = incomplete_runs - {run_id}
        if foreign_incomplete:
            raise CryptoLedgerError("capital_prior_cycle_incomplete")
        if (
            existing_claim is None
            and last_valuation is not None
            and evidence.next_executable_quote.observed_at <= last_valuation
        ):
            raise CryptoLedgerError("capital_execution_slot_not_monotonic")
        if (
            existing_claim is None
            and account_valuation is not None
            and evidence.next_executable_quote.observed_at < account_valuation
        ):
            raise CryptoLedgerError("capital_account_valuation_regressed")
        if existing_claim is None:
            prechecked_before = _business_snapshot(ledger.snapshot(**mark_inputs))
            if prepared_intent is not None:
                _assert_frozen_position_cap(
                    prechecked_before,
                    intent=prepared_intent,
                    mark=mark,
                    policy=policy,
                    champion=champion,
                )
    _, head_checksum = ledger.head()
    claim_event, _ = ledger._append_event(
        event_type="cycle_claim",
        reference_id=claim_reference,
        payload=claim_payload,
        expected_head_checksum=head_checksum,
    )
    intent_payload: dict[str, Any] | None = None
    receipt_payload: dict[str, Any] | None = None
    fill_event: dict[str, Any] | None = None
    if decision.action == "buy":
        if prepared_intent is None or prepared_receipt is None:
            raise CryptoLedgerError("prepared_order_or_receipt_missing")
        intent = prepared_intent
        receipt = prepared_receipt
        reserve_reference = f"reserve:{intent.intent_id}"
        reserve_event = ledger.event_by_reference(reserve_reference)
        if reserve_event is None:
            before = prechecked_before or _business_snapshot(
                ledger.snapshot(**mark_inputs)
            )
            _assert_frozen_position_cap(
                before,
                intent=intent,
                mark=mark,
                policy=policy,
                champion=champion,
            )
        else:
            reserve_payload = reserve_event.get("payload")
            if not isinstance(reserve_payload, Mapping) or not isinstance(
                reserve_payload.get("before_snapshot"), Mapping
            ):
                raise CryptoLedgerError("capital_reserve_before_snapshot_missing")
            before = _canonical_value(reserve_payload["before_snapshot"])
        reserve_payload = _reserve_event_payload(
            intent,
            evidence,
            policy,
            before,
            run_id=run_id,
        )
        _, head_checksum = ledger.head()
        ledger._append_event(
            event_type="reserve",
            reference_id=reserve_reference,
            payload=reserve_payload,
            expected_head_checksum=head_checksum,
        )
        _, head_checksum = ledger.head()
        fill_event, _ = ledger._append_event(
            event_type="fill",
            reference_id=f"fill:{receipt.receipt_id}",
            payload=_fill_event_payload(intent, receipt, run_id=run_id),
            expected_head_checksum=head_checksum,
        )
        intent_payload = intent.to_payload()
        receipt_payload = receipt.to_payload()
    elif existing_reconcile is not None:
        existing_payload = existing_reconcile.get("payload")
        if not isinstance(existing_payload, Mapping):
            raise CryptoLedgerError("capital_reconcile_payload_invalid")
        before = _canonical_value(existing_payload)
    else:
        before = prechecked_before or _business_snapshot(ledger.snapshot(**mark_inputs))

    if existing_reconcile is not None:
        existing_payload = existing_reconcile.get("payload")
        if not isinstance(existing_payload, Mapping):
            raise CryptoLedgerError("capital_reconcile_payload_invalid")
        reconcile_payload = _canonical_value(existing_payload)
        after_fill = reconcile_payload
        reconcile_event = existing_reconcile
        final_snapshot = {
            **reconcile_payload,
            "head_sequence": reconcile_event["sequence"],
            "head_checksum": reconcile_event["checksum"],
        }
    else:
        after_fill = _business_snapshot(ledger.snapshot(**mark_inputs))
        reconcile_payload = after_fill
        _, head_checksum = ledger.head()
        reconcile_event, _ = ledger._append_event(
            event_type="reconcile",
            reference_id=reconcile_reference,
            payload=reconcile_payload,
            expected_head_checksum=head_checksum,
        )
        final_snapshot = ledger.snapshot(**mark_inputs)

    sample_review = _sample_review_payload(
        run_id=run_id,
        evidence=evidence,
        champion=champion,
        decision=decision,
        intent=prepared_intent,
        receipt=prepared_receipt,
    )
    bundle = {
        "contract": RUN_BUNDLE_CONTRACT,
        "run_id": run_id,
        "status": "fixture_simulated",
        "market": "crypto",
        "market_session": "24x7",
        "mode": "fixture_mock_only",
        "wire_contract": WIRE_CONTRACT,
        "capital_policy": _canonical_value(policy),
        "evidence_qualification": evidence.qualification_payload(),
        "champion": champion.to_payload(),
        "decision": decision.to_payload(),
        "order_intent": intent_payload,
        "paper_receipt": receipt_payload,
        "capital": {
            "before": before,
            "after_fill": after_fill,
            "final": final_snapshot,
            "cycle_claim_event_id": claim_event["event_id"],
            "cycle_claim_event_checksum": claim_event["checksum"],
            "fill_event_id": fill_event["event_id"] if fill_event else None,
            "fill_event_checksum": fill_event["checksum"] if fill_event else None,
            "reconcile_event_id": reconcile_event["event_id"],
            "reconcile_event_checksum": reconcile_event["checksum"],
            **_non_authority_fields(),
        },
        "sample_review": sample_review,
        "safety": {
            "network_used": False,
            "testnet_used": False,
            "live_broker_used": False,
            "model_network_used": False,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "real_trading_enabled": False,
            **_non_authority_fields(),
        },
        **_non_authority_fields(),
        "business_bundle_sha256": "",
    }
    digest_material = dict(bundle)
    digest_material.pop("business_bundle_sha256", None)
    bundle["business_bundle_sha256"] = _sha256(digest_material)
    _verify_run_bundle(
        bundle,
        run_id=run_id,
        fixture_payload=fixture_payload,
        evidence=evidence,
        policy=policy,
        champion=champion,
        decision=decision,
        expected_intent=prepared_intent,
        expected_receipt=prepared_receipt,
        ledger=ledger,
    )
    _write_projection(bundle_path, bundle)
    return {"bundle": _canonical_value(bundle), "idempotent_replay": False}


def run_fixture_auto_sim(
    payload: Mapping[str, Any],
    *,
    output_root: Path | str,
    policy: CryptoCapitalPolicy = CRYPTO_CAPITAL_POLICY,
    champion: FrozenChampionCandidate = FROZEN_CHAMPION,
) -> dict[str, Any]:
    """Run one deterministic local cycle and return bundle + replay metadata."""

    _assert_simulation_only()
    _assert_canonical_policy(policy)
    _assert_canonical_champion(champion)
    evidence = qualify_fixture_evidence(payload)
    decision = evaluate_frozen_champion(evidence, champion)
    prepared_intent = (
        build_order_intent(
            evidence,
            decision,
            policy=policy,
            champion=champion,
        )
        if decision.action == "buy"
        else None
    )
    prepared_receipt = (
        execute_fixture_paper_order(prepared_intent, evidence)
        if prepared_intent is not None
        else None
    )
    root = Path(output_root)
    if root.exists() and root.is_symlink():
        raise CryptoSafetyError("output_root_symlink_not_allowed")
    for child in (root / "capital", root / "runs"):
        if child.is_symlink():
            raise CryptoSafetyError("output_nested_symlink_not_allowed")
        if child.exists() and not child.is_dir():
            raise CryptoSafetyError("output_nested_directory_invalid")
    for child in (
        root / "capital" / "events.jsonl",
        root / "capital" / "head.json",
        root / "capital" / ".lock",
        root / "capital" / ".cycle.lock",
    ):
        if (
            child.exists()
            and stat.S_ISREG(child.lstat().st_mode)
            and child.lstat().st_nlink != 1
        ):
            raise CryptoSafetyError("output_nested_hardlink_not_allowed")
    run_id = _run_id(evidence, policy=policy, champion=champion)
    bundle_path = root / "runs" / f"{run_id}.json"
    ledger = _open_runtime_ledger(root / "capital", policy=policy)
    with ledger._cycle_lock():
        result = _run_prepared_fixture_cycle(
            bundle_path=bundle_path,
            ledger=ledger,
            run_id=run_id,
            fixture_payload=payload,
            evidence=evidence,
            policy=policy,
            champion=champion,
            decision=decision,
            prepared_intent=prepared_intent,
            prepared_receipt=prepared_receipt,
        )
    try:
        sidecar = _journal_llm_sidecar(root, run_id=run_id, evidence=evidence)
    except (CryptoLedgerError, OSError) as exc:
        sidecar = {
            "status": "degraded",
            "authority": "none",
            "reason": str(exc),
        }
    except Exception as exc:  # pragma: no cover - non-authority sidecar isolation
        sidecar = {
            "status": "degraded",
            "authority": "none",
            "reason": f"llm_sidecar_unexpected:{exc.__class__.__name__}:{exc}",
        }
    return {**result, "llm_sidecar": sidecar}


def run_fixture_file(
    fixture_path: Path | str,
    *,
    output_root: Path | str,
) -> dict[str, Any]:
    _assert_simulation_only()
    return run_fixture_auto_sim(
        _read_json(Path(fixture_path)),
        output_root=output_root,
    )
