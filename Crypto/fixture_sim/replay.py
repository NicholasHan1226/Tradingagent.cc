"""Deterministic bundle persistence and replay verification."""

from __future__ import annotations

import json
import os
import fcntl
import stat
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from Crypto.capital_policy import CryptoCapitalPolicy

from .contracts import (
    ALLOWED_SYMBOLS,
    CYCLE_CLAIM_CONTRACT,
    LLM_SIDECAR_CONTRACT,
    RUN_BUNDLE_CONTRACT,
    SAMPLE_REVIEW_CONTRACT,
    WIRE_CONTRACT,
    ZERO,
    CryptoEvidenceError,
    CryptoLedgerError,
    FrozenChampionCandidate,
    OrderIntent,
    PaperFillReceipt,
    QualifiedFixtureEvidence,
    TimeframeDecision,
    _assert_canonical_champion,
    _assert_canonical_policy,
    _assert_recursive_non_authority,
    _aware_utc,
    _canonical_json,
    _canonical_value,
    _decimal,
    _non_authority_fields,
    _sha256,
)
from .ledger import CryptoCapitalLedger


LLM_SIDECAR_MAX_BYTES = 1_048_576
LLM_SIDECAR_KEYS = frozenset(
    {
        "contract",
        "run_id",
        "sha256",
        "payload",
        "authority",
        "used_for_decision",
        "network_used",
        *_non_authority_fields(),
    }
)
RUN_BUNDLE_KEYS = frozenset(
    {
        "contract",
        "run_id",
        "status",
        "market",
        "market_session",
        "mode",
        "wire_contract",
        "capital_policy",
        "evidence_qualification",
        "champion",
        "decision",
        "order_intent",
        "paper_receipt",
        "capital",
        "sample_review",
        "safety",
        "business_bundle_sha256",
        *_non_authority_fields(),
    }
)
RUN_BUNDLE_CAPITAL_KEYS = frozenset(
    {
        "before",
        "after_fill",
        "final",
        "cycle_claim_event_id",
        "cycle_claim_event_checksum",
        "fill_event_id",
        "fill_event_checksum",
        "reconcile_event_id",
        "reconcile_event_checksum",
        *_non_authority_fields(),
    }
)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CryptoEvidenceError(f"fixture_file_unreadable:{path.name}") from exc
    if not isinstance(payload, dict):
        raise CryptoEvidenceError("fixture_file_must_contain_object")
    return payload


def _write_projection(path: Path, payload: Mapping[str, Any]) -> None:
    if path.parent.exists() and path.parent.is_symlink():
        raise CryptoLedgerError("run_bundle_directory_symlink_not_allowed")
    if path.is_symlink():
        raise CryptoLedgerError("run_bundle_symlink_not_allowed")
    if path.exists() and path.lstat().st_nlink != 1:
        raise CryptoLedgerError("run_bundle_hardlink_not_allowed")
    canonical = _canonical_json(payload) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != canonical:
            raise CryptoLedgerError("run_bundle_conflict")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temp.open("w", encoding="utf-8") as stream:
        stream.write(canonical)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _journal_llm_sidecar(
    output_root: Path,
    *,
    run_id: str,
    evidence: QualifiedFixtureEvidence,
) -> dict[str, Any]:
    if not evidence.llm_evidence_present or evidence.llm_evidence_payload is None:
        return {"status": "not_present", "authority": "none"}
    sidecar_root = output_root / "sidecars"
    if sidecar_root.exists() and sidecar_root.is_symlink():
        raise CryptoLedgerError("llm_sidecar_root_symlink_not_allowed")
    sidecar_root.mkdir(parents=True, exist_ok=True)
    journal_path = sidecar_root / "llm_evidence.jsonl"
    lock_path = sidecar_root / ".lock"
    for path in (journal_path, lock_path):
        if path.is_symlink():
            raise CryptoLedgerError("llm_sidecar_nested_symlink_not_allowed")
        if path.exists():
            node = path.lstat()
            if stat.S_ISREG(node.st_mode) and node.st_nlink != 1:
                raise CryptoLedgerError("llm_sidecar_hardlink_not_allowed")
    entry = {
        "contract": LLM_SIDECAR_CONTRACT,
        "run_id": run_id,
        "sha256": evidence.llm_evidence_sha256,
        "payload": evidence.llm_evidence_payload,
        "authority": "none",
        "used_for_decision": False,
        "network_used": False,
        **_non_authority_fields(),
    }
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if (
            journal_path.exists()
            and journal_path.stat().st_size > LLM_SIDECAR_MAX_BYTES
        ):
            raise CryptoLedgerError("llm_sidecar_size_limit_exceeded")
        try:
            existing_bytes = journal_path.read_bytes() if journal_path.exists() else b""
            existing = existing_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise CryptoLedgerError("llm_sidecar_unreadable") from exc
        if len(existing_bytes) > LLM_SIDECAR_MAX_BYTES:
            raise CryptoLedgerError("llm_sidecar_size_limit_exceeded")
        if existing and not existing.endswith("\n"):
            raise CryptoLedgerError("llm_sidecar_partial_tail")
        try:
            rows = [json.loads(line) for line in existing.splitlines() if line]
        except json.JSONDecodeError as exc:
            raise CryptoLedgerError("llm_sidecar_invalid_json") from exc
        for row in rows:
            if not isinstance(row, Mapping):
                raise CryptoLedgerError("llm_sidecar_row_not_object")
            if set(row) != LLM_SIDECAR_KEYS:
                raise CryptoLedgerError("llm_sidecar_row_schema_mismatch")
            _assert_recursive_non_authority(row, path="llm_sidecar_row")
            if (
                row.get("contract") != LLM_SIDECAR_CONTRACT
                or not str(row.get("run_id") or "")
                or len(str(row.get("sha256") or "")) != 64
                or row.get("authority") != "none"
                or row.get("used_for_decision") is not False
                or row.get("network_used") is not False
                or row.get("sha256") != _sha256(row.get("payload"))
            ):
                raise CryptoLedgerError("llm_sidecar_row_binding_invalid")
            for key, expected_value in _non_authority_fields().items():
                if row.get(key) != expected_value:
                    raise CryptoLedgerError("llm_sidecar_row_authority_invalid")
        if any(
            row.get("run_id") == run_id
            and row.get("sha256") == evidence.llm_evidence_sha256
            for row in rows
        ):
            return {"status": "already_present", "authority": "none"}
        encoded_entry = (_canonical_json(entry) + "\n").encode("utf-8")
        if len(existing_bytes) + len(encoded_entry) > LLM_SIDECAR_MAX_BYTES:
            raise CryptoLedgerError("llm_sidecar_size_limit_exceeded")
        with journal_path.open("a", encoding="utf-8") as stream:
            stream.write(encoded_entry.decode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        directory_fd = os.open(sidecar_root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return {"status": "recorded", "authority": "none"}


def _run_id(
    evidence: QualifiedFixtureEvidence,
    *,
    policy: CryptoCapitalPolicy,
    champion: FrozenChampionCandidate,
    valuation_context: Mapping[str, Any] | None = None,
) -> str:
    _assert_canonical_policy(policy)
    _assert_canonical_champion(champion)
    normalized_valuation = _normalize_valuation_context(
        evidence,
        valuation_context,
    )
    material = {
        "market_evidence_sha256": evidence.market_evidence_sha256,
        "execution_slot": evidence.next_executable_quote.observed_at,
        "capital_authority_id": policy.authority_id,
        "capital_generation": policy.generation,
        "champion_sha256": champion.sha256,
    }
    if len(normalized_valuation["marks"]) > 1:
        material["valuation_context"] = normalized_valuation
    return f"crypto-fixture-run-{_sha256(material)[:24]}"


def _normalize_valuation_context(
    evidence: QualifiedFixtureEvidence,
    valuation_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if valuation_context is None:
        valuation_context = {
            "valuation_slot": evidence.next_executable_quote.observed_at,
            "marks": {
                evidence.symbol: {
                    "price": evidence.next_executable_quote.bid,
                    "observed_at": evidence.next_executable_quote.observed_at,
                    "evidence_receipt_id": evidence.receipt_id,
                    "market_evidence_sha256": evidence.market_evidence_sha256,
                }
            },
        }
    if not isinstance(valuation_context, Mapping) or set(valuation_context) != {
        "valuation_slot",
        "marks",
    }:
        raise CryptoEvidenceError("capital_valuation_context_schema_invalid")
    valuation_slot = _aware_utc(
        valuation_context.get("valuation_slot"),
        field_name="capital_valuation_slot",
    )
    marks = valuation_context.get("marks")
    if (
        not isinstance(marks, Mapping)
        or not 1 <= len(marks) <= len(ALLOWED_SYMBOLS)
        or evidence.symbol not in marks
        or any(
            not isinstance(symbol, str) or symbol not in ALLOWED_SYMBOLS
            for symbol in marks
        )
    ):
        raise CryptoEvidenceError("capital_valuation_marks_invalid")
    normalized_marks: dict[str, Any] = {}
    for symbol in sorted(marks):
        raw = marks[symbol]
        if not isinstance(raw, Mapping) or set(raw) != {
            "price",
            "observed_at",
            "evidence_receipt_id",
            "market_evidence_sha256",
        }:
            raise CryptoEvidenceError("capital_valuation_mark_schema_invalid")
        price = _decimal(
            raw.get("price"),
            field_name=f"capital_valuation_mark_{symbol}",
            positive=True,
        )
        observed_at = _aware_utc(
            raw.get("observed_at"),
            field_name=f"capital_valuation_mark_observed_at_{symbol}",
        )
        receipt_id = str(raw.get("evidence_receipt_id") or "").strip()
        evidence_sha256 = str(raw.get("market_evidence_sha256") or "").strip()
        if (
            observed_at != valuation_slot
            or not receipt_id
            or len(evidence_sha256) != 64
            or any(character not in "0123456789abcdef" for character in evidence_sha256)
        ):
            raise CryptoEvidenceError("capital_valuation_mark_binding_invalid")
        normalized_marks[symbol] = {
            "price": price,
            "observed_at": observed_at,
            "evidence_receipt_id": receipt_id,
            "market_evidence_sha256": evidence_sha256,
        }
    current = normalized_marks[evidence.symbol]
    if (
        valuation_slot != evidence.next_executable_quote.observed_at
        or current["price"] != evidence.next_executable_quote.bid
        or current["evidence_receipt_id"] != evidence.receipt_id
        or current["market_evidence_sha256"] != evidence.market_evidence_sha256
    ):
        raise CryptoEvidenceError("capital_valuation_current_evidence_mismatch")
    return _canonical_value(
        {
            "valuation_slot": valuation_slot,
            "marks": normalized_marks,
        }
    )


def _business_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Remove transport head metadata from a capital-state projection."""

    return {
        str(key): _canonical_value(value)
        for key, value in snapshot.items()
        if key not in {"head_sequence", "head_checksum"}
    }


def _cycle_claim_payload(
    *,
    run_id: str,
    fixture_payload: Mapping[str, Any],
    evidence: QualifiedFixtureEvidence,
    policy: CryptoCapitalPolicy,
    champion: FrozenChampionCandidate,
    valuation_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    _assert_canonical_policy(policy)
    _assert_canonical_champion(champion)
    market_fixture_payload = dict(fixture_payload)
    # LLM prose is an independent non-authority sidecar. Keep the required
    # fixture key at a fixed null value so wording changes never alter capital
    # events, run IDs, or bundle identities.
    market_fixture_payload["llm_evidence"] = None
    normalized_valuation = _normalize_valuation_context(
        evidence,
        valuation_context,
    )
    payload = {
        "contract": CYCLE_CLAIM_CONTRACT,
        "run_id": run_id,
        "fixture_payload": _canonical_value(market_fixture_payload),
        "symbol": evidence.symbol,
        "execution_slot": evidence.next_executable_quote.observed_at,
        "evidence_receipt_id": evidence.receipt_id,
        "market_evidence_sha256": evidence.market_evidence_sha256,
        "champion_sha256": champion.sha256,
        "capital_authority_id": policy.authority_id,
        "capital_generation": policy.generation,
        "capital_account_id": policy.account_id,
        "capital_currency": policy.currency,
        **_non_authority_fields(),
    }
    if len(normalized_valuation["marks"]) > 1:
        payload["valuation_context"] = normalized_valuation
    return payload


def _reserve_event_payload(
    intent: OrderIntent,
    evidence: QualifiedFixtureEvidence,
    policy: CryptoCapitalPolicy,
    before_snapshot: Mapping[str, Any],
    *,
    run_id: str,
) -> dict[str, Any]:
    _assert_canonical_policy(policy)
    return {
        "run_id": run_id,
        "intent_id": intent.intent_id,
        "amount": intent.notional + intent.maximum_fee,
        "symbol": intent.symbol,
        "quantity": intent.quantity,
        "reference_price": intent.reference_price,
        "notional": intent.notional,
        "maximum_fee": intent.maximum_fee,
        "currency": policy.currency,
        "execution_slot": intent.execution_slot,
        "evidence_receipt_id": evidence.receipt_id,
        "market_evidence_sha256": evidence.market_evidence_sha256,
        "champion_sha256": intent.champion_sha256,
        "capital_authority_id": policy.authority_id,
        "capital_generation": policy.generation,
        "capital_account_id": policy.account_id,
        "before_snapshot": _canonical_value(before_snapshot),
        **_non_authority_fields(),
    }


def _fill_event_payload(
    intent: OrderIntent,
    receipt: PaperFillReceipt,
    *,
    run_id: str,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "intent_id": intent.intent_id,
        "receipt_id": receipt.receipt_id,
        "broker_contract": receipt.broker_contract,
        "authority_id": receipt.authority_id,
        "authority_generation": receipt.authority_generation,
        "account_id": receipt.account_id,
        "symbol": receipt.symbol,
        "side": receipt.side,
        "quantity": receipt.filled_quantity,
        "price": receipt.average_price,
        "notional": receipt.notional,
        "fee": receipt.fee,
        "fee_asset": receipt.fee_asset,
        "filled_at": receipt.filled_at,
        "evidence_receipt_id": receipt.evidence_receipt_id,
        "market_evidence_sha256": receipt.market_evidence_sha256,
        "champion_sha256": intent.champion_sha256,
        "status": "fixture_simulated",
        "real_trading_enabled": False,
        **_non_authority_fields(),
    }


def _sample_review_payload(
    *,
    run_id: str,
    evidence: QualifiedFixtureEvidence,
    champion: FrozenChampionCandidate,
    decision: TimeframeDecision,
    intent: OrderIntent | None,
    receipt: PaperFillReceipt | None,
) -> dict[str, Any]:
    fee = receipt.fee if receipt else ZERO
    mark_to_market_pnl = (
        receipt.filled_quantity
        * (evidence.next_executable_quote.bid - receipt.average_price)
        if receipt
        else ZERO
    )
    sample_type = "fixture_simulated_fill" if receipt else "observation_only"
    return {
        "contract": SAMPLE_REVIEW_CONTRACT,
        "sample_id": f"crypto-sample-{_sha256({'run_id': run_id, 'sample_type': sample_type})[:24]}",
        "sample_type": sample_type,
        "run_id": run_id,
        "symbol": evidence.symbol,
        "execution_slot": _canonical_value(evidence.next_executable_quote.observed_at),
        "evidence_receipt_id": evidence.receipt_id,
        "market_evidence_sha256": evidence.market_evidence_sha256,
        "champion_id": champion.champion_id,
        "champion_sha256": champion.sha256,
        "decision_id": decision.decision_id,
        "intent_id": intent.intent_id if intent else None,
        "receipt_id": receipt.receipt_id if receipt else None,
        "fees": _canonical_value(fee),
        "mark_to_market_pnl": _canonical_value(mark_to_market_pnl),
        "net_pnl_after_fee": _canonical_value(mark_to_market_pnl - fee),
        "label_status": "pending",
        "promotion_evidence_ready": False,
        "promotion_authorized": False,
        "manual_review_required": True,
        "llm_sidecar": {
            "storage": "separate_non_authority_journal",
            "authority": "none",
            "used_for_decision": False,
            "network_used": False,
        },
        **_non_authority_fields(),
        "real_trading_enabled": False,
    }


def _assert_frozen_position_cap(
    before: Mapping[str, Any],
    *,
    intent: OrderIntent,
    mark: Decimal,
    policy: CryptoCapitalPolicy,
    champion: FrozenChampionCandidate,
) -> None:
    _assert_canonical_policy(policy)
    _assert_canonical_champion(champion)
    positions = before.get("positions")
    orders = before.get("orders")
    if not isinstance(positions, Mapping) or not isinstance(orders, Mapping):
        raise CryptoLedgerError("capital_pretrade_snapshot_invalid")
    existing_quantity = Decimal(str(positions.get(intent.symbol, "0")))
    pending_notional = sum(
        Decimal(str(order.get("notional", "0")))
        for order in orders.values()
        if isinstance(order, Mapping)
        and order.get("status") == "reserved"
        and order.get("symbol") == intent.symbol
    )
    position_value = existing_quantity * mark
    maximum_position_value = policy.initial_cash * champion.maximum_position_pct
    if position_value + pending_notional + intent.notional > maximum_position_value:
        raise CryptoLedgerError("frozen_champion_position_cap_exceeded")


def _verify_run_bundle(
    bundle: Mapping[str, Any],
    *,
    run_id: str,
    fixture_payload: Mapping[str, Any],
    evidence: QualifiedFixtureEvidence,
    policy: CryptoCapitalPolicy,
    champion: FrozenChampionCandidate,
    decision: TimeframeDecision,
    expected_intent: OrderIntent | None,
    expected_receipt: PaperFillReceipt | None,
    ledger: CryptoCapitalLedger,
    valuation_context: Mapping[str, Any] | None = None,
) -> None:
    _assert_canonical_policy(policy)
    _assert_canonical_champion(champion)
    if set(bundle) != RUN_BUNDLE_KEYS:
        raise CryptoLedgerError("run_bundle_schema_mismatch")
    _assert_recursive_non_authority(bundle, path="run_bundle")
    if bundle.get("contract") != RUN_BUNDLE_CONTRACT or bundle.get("run_id") != run_id:
        raise CryptoLedgerError("run_bundle_contract_or_id_mismatch")
    if (
        bundle.get("status") != "fixture_simulated"
        or bundle.get("market") != "crypto"
        or bundle.get("market_session") != "24x7"
        or bundle.get("mode") != "fixture_mock_only"
        or _canonical_json(bundle.get("wire_contract"))
        != _canonical_json(WIRE_CONTRACT)
    ):
        raise CryptoLedgerError("run_bundle_envelope_mismatch")
    claimed_digest = str(bundle.get("business_bundle_sha256") or "")
    material = dict(bundle)
    material.pop("business_bundle_sha256", None)
    if len(claimed_digest) != 64 or claimed_digest != _sha256(material):
        raise CryptoLedgerError("run_bundle_sha256_invalid")
    if _canonical_json(bundle.get("capital_policy")) != _canonical_json(
        _canonical_value(policy)
    ):
        raise CryptoLedgerError("run_bundle_capital_policy_mismatch")
    qualification = bundle.get("evidence_qualification")
    if _canonical_json(qualification) != _canonical_json(
        evidence.qualification_payload()
    ):
        raise CryptoLedgerError("run_bundle_market_evidence_mismatch")
    champion_payload = bundle.get("champion")
    if _canonical_json(champion_payload) != _canonical_json(champion.to_payload()):
        raise CryptoLedgerError("run_bundle_champion_mismatch")
    if _canonical_json(bundle.get("decision")) != _canonical_json(
        decision.to_payload()
    ):
        raise CryptoLedgerError("run_bundle_decision_mismatch")
    expected_sample = _sample_review_payload(
        run_id=run_id,
        evidence=evidence,
        champion=champion,
        decision=decision,
        intent=expected_intent,
        receipt=expected_receipt,
    )
    if _canonical_json(bundle.get("sample_review")) != _canonical_json(expected_sample):
        raise CryptoLedgerError("run_bundle_sample_review_mismatch")
    safety = bundle.get("safety")
    if not isinstance(safety, Mapping) or safety != {
        "network_used": False,
        "testnet_used": False,
        "live_broker_used": False,
        "model_network_used": False,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real_trading_enabled": False,
        **_non_authority_fields(),
    }:
        raise CryptoLedgerError("run_bundle_safety_mismatch")
    for key, expected_value in _non_authority_fields().items():
        if bundle.get(key) != expected_value:
            raise CryptoLedgerError("run_bundle_non_authority_mismatch")
    capital = bundle.get("capital")
    if not isinstance(capital, Mapping) or set(capital) != RUN_BUNDLE_CAPITAL_KEYS:
        raise CryptoLedgerError("run_bundle_capital_missing")
    claim_checksum = str(capital.get("cycle_claim_event_checksum") or "")
    claim_event = ledger.event_by_checksum(claim_checksum)
    if (
        claim_event is None
        or claim_event.get("event_type") != "cycle_claim"
        or claim_event.get("reference_id") != f"cycle:{run_id}"
        or capital.get("cycle_claim_event_id") != claim_event.get("event_id")
        or _canonical_json(claim_event.get("payload"))
        != _canonical_json(
            _cycle_claim_payload(
                run_id=run_id,
                fixture_payload=fixture_payload,
                evidence=evidence,
                policy=policy,
                champion=champion,
                valuation_context=valuation_context,
            )
        )
    ):
        raise CryptoLedgerError("run_bundle_cycle_claim_mismatch")
    reconcile_checksum = str(capital.get("reconcile_event_checksum") or "")
    reconcile_event = ledger.event_by_checksum(reconcile_checksum)
    if (
        reconcile_event is None
        or reconcile_event.get("event_type") != "reconcile"
        or reconcile_event.get("reference_id") != f"reconcile:{run_id}"
        or capital.get("reconcile_event_id") != reconcile_event.get("event_id")
    ):
        raise CryptoLedgerError("run_bundle_reconcile_event_mismatch")
    reconcile_payload = reconcile_event.get("payload")
    before = capital.get("before")
    after_fill = capital.get("after_fill")
    final = capital.get("final")
    if not all(isinstance(item, Mapping) for item in (before, after_fill, final)):
        raise CryptoLedgerError("run_bundle_capital_snapshot_invalid")
    if (
        _canonical_json(after_fill) != _canonical_json(reconcile_payload)
        or _canonical_json(_business_snapshot(final))
        != _canonical_json(reconcile_payload)
        or final.get("head_sequence") != reconcile_event.get("sequence")
        or final.get("head_checksum") != reconcile_checksum
    ):
        raise CryptoLedgerError("run_bundle_capital_reconcile_binding_mismatch")
    intent = bundle.get("order_intent")
    receipt = bundle.get("paper_receipt")
    expected_intent_payload = expected_intent.to_payload() if expected_intent else None
    expected_receipt_payload = (
        expected_receipt.to_payload() if expected_receipt else None
    )
    if _canonical_json(intent) != _canonical_json(
        expected_intent_payload
    ) or _canonical_json(receipt) != _canonical_json(expected_receipt_payload):
        raise CryptoLedgerError("run_bundle_expected_order_mismatch")
    if intent is None or receipt is None:
        if (
            intent is not None
            or receipt is not None
            or capital.get("fill_event_id") is not None
            or capital.get("fill_event_checksum") is not None
            or _canonical_json(before) != _canonical_json(reconcile_payload)
        ):
            raise CryptoLedgerError("run_bundle_observation_order_mismatch")
        return
    if not isinstance(intent, Mapping) or not isinstance(receipt, Mapping):
        raise CryptoLedgerError("run_bundle_order_payload_invalid")
    if (
        intent.get("intent_id") != receipt.get("intent_id")
        or intent.get("symbol") != receipt.get("symbol")
        or intent.get("quantity") != receipt.get("filled_quantity")
        or intent.get("reference_price") != receipt.get("average_price")
        or intent.get("notional") != receipt.get("notional")
        or intent.get("maximum_fee") != receipt.get("fee")
    ):
        raise CryptoLedgerError("run_bundle_intent_receipt_mismatch")
    fill_checksum = str(capital.get("fill_event_checksum") or "")
    fill_event = ledger.event_by_checksum(fill_checksum)
    if (
        fill_event is None
        or fill_event.get("event_type") != "fill"
        or fill_event.get("reference_id") != f"fill:{receipt.get('receipt_id')}"
        or fill_event.get("payload", {}).get("intent_id") != intent.get("intent_id")
        or capital.get("fill_event_id") != fill_event.get("event_id")
    ):
        raise CryptoLedgerError("run_bundle_fill_event_mismatch")
    reserve_event = ledger.event_by_reference(f"reserve:{intent.get('intent_id')}")
    reserve_payload = reserve_event.get("payload") if reserve_event else None
    if (
        not isinstance(reserve_payload, Mapping)
        or _canonical_json(reserve_payload.get("before_snapshot"))
        != _canonical_json(before)
        or _canonical_json(reserve_payload)
        != _canonical_json(
            _reserve_event_payload(
                expected_intent,
                evidence,
                policy,
                before,
                run_id=run_id,
            )
        )
        or _canonical_json(fill_event.get("payload"))
        != _canonical_json(
            _fill_event_payload(
                expected_intent,
                expected_receipt,
                run_id=run_id,
            )
        )
    ):
        raise CryptoLedgerError("run_bundle_order_ledger_binding_mismatch")
