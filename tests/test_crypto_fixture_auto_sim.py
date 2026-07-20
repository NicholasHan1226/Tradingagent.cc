from __future__ import annotations

import copy
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import pytest
import yaml

import Crypto.fixture_auto_sim as fixture_auto_sim
import Crypto.fixture_sim as fixture_sim_package
import Crypto.fixture_sim.runtime as fixture_runtime
from Crypto.fixture_sim.contracts import _validate_json_tree
from Crypto.fixture_sim.replay import (
    _business_snapshot,
    _cycle_claim_payload,
    _fill_event_payload,
    _reserve_event_payload,
)
from Crypto.capital_policy import (
    CRYPTO_CAPITAL_AUTHORITY_ID,
    CRYPTO_CAPITAL_POLICY,
    CRYPTO_INITIAL_CAPITAL_USDT,
    CryptoCapitalPolicy,
)
from Crypto.fixture_auto_sim import (
    FROZEN_CHAMPION,
    WIRE_CONTRACT,
    CryptoEvidenceError,
    CryptoLedgerError,
    CryptoSafetyError,
    FrozenChampionCandidate,
    build_order_intent,
    evaluate_frozen_champion,
    execute_fixture_paper_order,
    qualify_fixture_evidence,
    run_fixture_auto_sim,
)
from Crypto.fixture_sim.ledger import CryptoCapitalLedger


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "Crypto" / "fixtures" / "auto_sim_spot_cycle_v1.json"


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        keys = {str(key) for key in value}
        for item in value.values():
            keys.update(_all_keys(item))
        return keys
    if isinstance(value, list):
        keys: set[str] = set()
        for item in value:
            keys.update(_all_keys(item))
        return keys
    return set()


def _observation_fixture() -> dict[str, object]:
    payload = _fixture()
    bars = payload["bars_5m"]
    assert isinstance(bars, list)
    replacements = (
        {
            "open": "49850.00",
            "high": "49860.00",
            "low": "49830.00",
            "close": "49840.00",
        },
        {
            "open": "49840.00",
            "high": "49850.00",
            "low": "49820.00",
            "close": "49830.00",
        },
        {
            "open": "49830.00",
            "high": "49840.00",
            "low": "49810.00",
            "close": "49820.00",
        },
    )
    for bar, replacement in zip(bars[-3:], replacements):
        assert isinstance(bar, dict)
        bar.update(replacement)
    return payload


def _retag(payload: dict[str, object], suffix: str) -> dict[str, object]:
    fixture_id = f"crypto-fixture-{suffix}"
    payload["fixture_id"] = fixture_id
    metadata = payload["metadata"]
    assert isinstance(metadata, dict)
    metadata["receipt_id"] = f"fixture-receipt-{suffix}"
    lineage = metadata["lineage"]
    assert isinstance(lineage, dict)
    lineage["fixture_id"] = fixture_id
    return payload


def _shift_timestamp(raw: object, minutes: int) -> str:
    parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    shifted = parsed.astimezone(timezone.utc) + timedelta(minutes=minutes)
    return shifted.isoformat().replace("+00:00", "Z")


def _shifted_fixture(
    payload: dict[str, object], *, minutes: int, suffix: str
) -> dict[str, object]:
    _retag(payload, suffix)
    payload["as_of"] = _shift_timestamp(payload["as_of"], minutes)
    metadata = payload["metadata"]
    assert isinstance(metadata, dict)
    for key in ("observed_at", "data_through"):
        metadata[key] = _shift_timestamp(metadata[key], minutes)
    quote = payload["next_executable_quote"]
    assert isinstance(quote, dict)
    quote["observed_at"] = _shift_timestamp(quote["observed_at"], minutes)
    bars = payload["bars_5m"]
    assert isinstance(bars, list)
    for bar in bars:
        assert isinstance(bar, dict)
        for key in ("open_time", "close_time"):
            bar[key] = _shift_timestamp(bar[key], minutes)
    return payload


def _prepared_fill_ledger(
    root: Path,
) -> tuple[CryptoCapitalLedger, dict[str, object], str]:
    evidence = qualify_fixture_evidence(_fixture())
    decision = evaluate_frozen_champion(evidence)
    intent = build_order_intent(evidence, decision)
    assert intent is not None
    receipt = execute_fixture_paper_order(intent, evidence)
    run_id = "crypto-fixture-direct-ledger-test"
    ledger = fixture_runtime._open_runtime_ledger(root)
    ledger._ensure_opening()
    _, checksum = ledger.head()
    ledger._append_event(
        event_type="cycle_claim",
        reference_id=f"cycle:{run_id}",
        payload=_cycle_claim_payload(
            run_id=run_id,
            fixture_payload=_fixture(),
            evidence=evidence,
            policy=CRYPTO_CAPITAL_POLICY,
            champion=FROZEN_CHAMPION,
        ),
        expected_head_checksum=checksum,
    )
    mark_inputs = {
        "marks": {evidence.symbol: evidence.next_executable_quote.bid},
        "mark_slots": {evidence.symbol: evidence.next_executable_quote.observed_at},
        "valuation_slot": evidence.next_executable_quote.observed_at,
    }
    before = _business_snapshot(ledger.snapshot(**mark_inputs))
    _, checksum = ledger.head()
    ledger._append_event(
        event_type="reserve",
        reference_id=f"reserve:{intent.intent_id}",
        payload=_reserve_event_payload(
            intent,
            evidence,
            CRYPTO_CAPITAL_POLICY,
            before,
            run_id=run_id,
        ),
        expected_head_checksum=checksum,
    )
    _, checksum = ledger.head()
    return ledger, _fill_event_payload(intent, receipt, run_id=run_id), checksum


def _rehash_capital_rows(rows: list[dict[str, object]]) -> None:
    previous_checksum = ""
    for row in rows:
        payload = row["payload"]
        event_type = row["event_type"]
        reference_id = row["reference_id"]
        row["event_id"] = (
            "crypto-capital-event-"
            + fixture_auto_sim._sha256(
                {
                    "event_type": event_type,
                    "reference_id": reference_id,
                    "payload": payload,
                }
            )[:24]
        )
        row["previous_checksum"] = previous_checksum
        unsigned = dict(row)
        unsigned.pop("checksum", None)
        row["checksum"] = CryptoCapitalLedger._event_checksum(unsigned)
        previous_checksum = str(row["checksum"])


def _eth_fixture() -> dict[str, object]:
    payload = _retag(_fixture(), "weekend-eth-v1")
    payload["symbol"] = "ETHUSDT"
    instrument = payload["instrument"]
    assert isinstance(instrument, dict)
    instrument["base_asset"] = "ETH"
    bars = payload["bars_5m"]
    assert isinstance(bars, list)
    for bar in bars:
        assert isinstance(bar, dict)
        bar["symbol"] = "ETHUSDT"
    quote = payload["next_executable_quote"]
    assert isinstance(quote, dict)
    quote["symbol"] = "ETHUSDT"
    return payload


def test_crypto_capital_v1_is_single_10000_usdt_fixture_opening_policy() -> None:
    policy = CRYPTO_CAPITAL_POLICY

    assert policy.authority_id == CRYPTO_CAPITAL_AUTHORITY_ID == "crypto-capital-v1"
    assert policy.initial_cash == CRYPTO_INITIAL_CAPITAL_USDT == Decimal("10000")
    assert policy.currency == "USDT"
    assert policy.generation == 1
    assert policy.capital_layer == policy.account_type == "simulated"
    assert policy.real_trading_enabled is False

    config = yaml.safe_load(
        (ROOT / "Crypto" / "config.yaml").read_text(encoding="utf-8")
    )
    assert "initial_capital" not in config["capital"]
    with pytest.raises(ValueError, match="10000 USDT"):
        CryptoCapitalPolicy(initial_cash=Decimal("9999"))
    with pytest.raises(ValueError, match="currency must be USDT"):
        CryptoCapitalPolicy(currency="CNY")
    with pytest.raises(ValueError, match="real trading must remain disabled"):
        CryptoCapitalPolicy(real_trading_enabled=True)
    with pytest.raises(ValueError, match="generation is immutable"):
        CryptoCapitalPolicy(generation=True)
    with pytest.raises(ValueError, match="10000 USDT"):
        CryptoCapitalPolicy(initial_cash=10000.0)


def test_fixture_declares_only_catalog_query_and_no_dataset_id() -> None:
    payload = _fixture()

    assert payload["wire_contract"] == WIRE_CONTRACT
    assert "dataset_id" not in _all_keys(payload)
    evidence = qualify_fixture_evidence(payload)
    qualification = evidence.qualification_payload()
    assert qualification["wire_contract"] == WIRE_CONTRACT
    assert "dataset_id" not in _all_keys(qualification)
    assert qualification["production_eligible"] is False


def test_weekend_fixture_closes_full_decimal_paper_cycle(tmp_path: Path) -> None:
    result = run_fixture_auto_sim(_fixture(), output_root=tmp_path)
    bundle = result["bundle"]

    assert result["idempotent_replay"] is False
    assert bundle["status"] == "fixture_simulated"
    assert bundle["market_session"] == "24x7"
    assert bundle["decision"]["execution_slot"] == "2026-07-19T00:01:00Z"
    assert bundle["decision"]["regime_interval"] == "1h"
    assert bundle["decision"]["decision_interval"] == "15m"
    assert bundle["decision"]["execution_interval"] == "5m"
    assert bundle["decision"]["action"] == "buy"
    assert bundle["order_intent"]["quantity"] == "0.019996"
    assert bundle["order_intent"]["reference_price"] == "50010.00"
    assert bundle["order_intent"]["notional"] == "999.99996000"
    assert bundle["paper_receipt"]["fee"] == "0.99999996"

    final = bundle["capital"]["final"]
    assert final["cash"] == "8999.00004004"
    assert final["reserved_cash"] == "0.00000000"
    assert final["positions"] == {"BTCUSDT": "0.019996"}
    assert (
        final["orders"][bundle["order_intent"]["intent_id"]]["status"]
        == "fixture_simulated"
    )
    assert final["equity"] == "9998.60008004"
    assert final["balanced"] is True
    assert final["head_sequence"] == 5
    assert bundle["capital"]["cycle_claim_event_id"].startswith("crypto-capital-event-")
    assert len(bundle["capital"]["cycle_claim_event_checksum"]) == 64

    sample = bundle["sample_review"]
    assert sample["sample_type"] == "fixture_simulated_fill"
    assert sample["label_status"] == "pending"
    assert sample["promotion_authorized"] is False
    assert sample["manual_review_required"] is True
    assert sample["net_pnl_after_fee"] == "-1.39991996"
    assert sample["llm_sidecar"]["used_for_decision"] is False
    assert bundle["safety"]["execution_authority"] is False
    assert bundle["safety"]["durability_scope"] == "local_fixture_fsync_only"


def test_decimal_intent_and_receipt_never_use_float_money() -> None:
    evidence = qualify_fixture_evidence(_fixture())
    decision = evaluate_frozen_champion(evidence)
    intent = build_order_intent(evidence, decision)
    receipt = execute_fixture_paper_order(intent, evidence)

    assert isinstance(intent.quantity, Decimal)
    assert isinstance(intent.reference_price, Decimal)
    assert isinstance(intent.notional, Decimal)
    assert isinstance(intent.maximum_fee, Decimal)
    assert intent.quantity == Decimal("0.019996")
    assert intent.notional == Decimal("999.99996000")
    assert intent.maximum_fee == Decimal("0.99999996")
    assert receipt.filled_quantity == Decimal("0.019996")
    assert receipt.notional == Decimal("999.99996000")
    assert receipt.fee == Decimal("0.99999996")
    for key in ("quantity", "reference_price", "notional", "fee_rate", "maximum_fee"):
        assert isinstance(intent.to_payload()[key], str)
    for key in ("filled_quantity", "average_price", "notional", "fee"):
        assert isinstance(receipt.to_payload()[key], str)


def test_decision_and_fill_use_causal_observation_time() -> None:
    evidence = qualify_fixture_evidence(_fixture())
    decision = evaluate_frozen_champion(evidence)
    intent = build_order_intent(evidence, decision)
    receipt = execute_fixture_paper_order(intent, evidence)

    assert decision.execution_slot.isoformat() == "2026-07-19T00:01:00+00:00"
    assert decision.decision_observed_at == evidence.observed_at
    assert receipt.filled_at == evidence.observed_at
    assert receipt.filled_at == decision.execution_slot
    assert intent.reference_price > evidence.bars_5m[-1].close


def test_constructed_order_and_receipt_contracts_fail_closed() -> None:
    evidence = qualify_fixture_evidence(_fixture())
    decision = evaluate_frozen_champion(evidence)
    intent = build_order_intent(evidence, decision)
    receipt = execute_fixture_paper_order(intent, evidence)

    with pytest.raises(CryptoSafetyError, match="capital_authority_invalid"):
        replace(intent, authority_id="not-crypto-capital-v1")
    with pytest.raises(CryptoEvidenceError, match="notional_mismatch"):
        replace(receipt, notional=Decimal("999"))
    with pytest.raises(CryptoSafetyError, match="must_remain_simulated"):
        replace(receipt, real_trading_enabled=True)
    with pytest.raises(CryptoSafetyError, match="capital_authority_invalid"):
        replace(intent, authority_generation=True)
    with pytest.raises(CryptoSafetyError, match="capital_authority_invalid"):
        replace(receipt, authority_generation=True)


def test_capital_writer_is_not_exported_and_default_ledger_is_read_only(
    tmp_path: Path,
) -> None:
    assert not hasattr(fixture_auto_sim, "CryptoCapitalLedger")
    assert not hasattr(fixture_sim_package, "CryptoCapitalLedger")
    assert "CryptoCapitalLedger" not in fixture_auto_sim.__all__
    assert "CryptoCapitalLedger" not in fixture_sim_package.__all__

    root = tmp_path / "capital"
    ledger = CryptoCapitalLedger(root)
    with pytest.raises(CryptoLedgerError, match="write_capability_required"):
        ledger._recover_head()
    with pytest.raises(CryptoLedgerError, match="write_capability_required"):
        ledger._ensure_opening()
    with pytest.raises(CryptoLedgerError, match="write_capability_required"):
        ledger._append_event(
            event_type="opening",
            reference_id="forged",
            payload={},
            expected_head_checksum="",
        )
    with pytest.raises(CryptoLedgerError, match="write_capability_required"):
        with ledger._cycle_lock():
            pass

    assert not root.exists()


def test_runtime_ledger_factory_has_one_source_caller() -> None:
    callers = []
    for path in sorted((ROOT / "Crypto").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        count = source.count("_open_runtime_ledger(")
        if count:
            callers.extend([path.relative_to(ROOT).as_posix()] * count)

    assert callers == [
        "Crypto/fixture_sim/ledger.py",
        "Crypto/fixture_sim/runtime.py",
    ]


def test_only_frozen_champion_can_issue_intent_or_receipt() -> None:
    evidence = qualify_fixture_evidence(_fixture())
    decision = evaluate_frozen_champion(evidence)
    intent = build_order_intent(evidence, decision)

    with pytest.raises(ValueError, match="fields are immutable"):
        replace(
            FROZEN_CHAMPION,
            minimum_decision_return=Decimal("-1"),
            target_capital_pct=Decimal("0.90"),
            maximum_position_pct=Decimal("1"),
        )
    with pytest.raises(ValueError, match="version must be an integer"):
        replace(FROZEN_CHAMPION, version=True)
    forged_decision = replace(decision, decision_id="forged-decision")
    with pytest.raises(CryptoEvidenceError, match="not_issued_by_frozen_champion"):
        build_order_intent(evidence, forged_decision)
    with pytest.raises(CryptoEvidenceError, match="frozen_contract"):
        build_order_intent(evidence, decision, fee_rate=Decimal("0.002"))
    forged_intent = replace(intent, decision_id="forged-decision")
    with pytest.raises(CryptoEvidenceError, match="not_issued_by_frozen_champion"):
        execute_fixture_paper_order(forged_intent, evidence)


def test_same_slot_replay_is_idempotent_and_does_not_append(tmp_path: Path) -> None:
    first = run_fixture_auto_sim(_fixture(), output_root=tmp_path)
    events_path = tmp_path / "capital" / "events.jsonl"
    bundle_path = tmp_path / "runs" / f"{first['bundle']['run_id']}.json"
    events_before = events_path.read_bytes()
    bundle_before = bundle_path.read_bytes()

    second = run_fixture_auto_sim(_fixture(), output_root=tmp_path)

    assert second["idempotent_replay"] is True
    assert second["bundle"] == first["bundle"]
    assert events_path.read_bytes() == events_before
    assert bundle_path.read_bytes() == bundle_before
    assert len(events_path.read_text(encoding="utf-8").splitlines()) == 5


def test_crash_after_ledger_commit_recovers_without_duplicate_fill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write_projection = fixture_runtime._write_projection
    crashed = False

    def crash_once(path: Path, payload: dict[str, object]) -> None:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise OSError("simulated crash before run-bundle projection")
        original_write_projection(path, payload)

    monkeypatch.setattr(fixture_runtime, "_write_projection", crash_once)
    with pytest.raises(OSError, match="simulated crash"):
        run_fixture_auto_sim(_fixture(), output_root=tmp_path)

    events_path = tmp_path / "capital" / "events.jsonl"
    assert len(events_path.read_text(encoding="utf-8").splitlines()) == 5
    assert not (tmp_path / "runs").exists()

    recovered = run_fixture_auto_sim(_fixture(), output_root=tmp_path)
    assert recovered["idempotent_replay"] is False
    assert recovered["bundle"]["capital"]["final"]["head_sequence"] == 5
    assert len(events_path.read_text(encoding="utf-8").splitlines()) == 5

    replay = run_fixture_auto_sim(_fixture(), output_root=tmp_path)
    assert replay["idempotent_replay"] is True
    assert len(events_path.read_text(encoding="utf-8").splitlines()) == 5


@pytest.mark.parametrize("failed_head_write", [1, 2, 3, 4, 5])
def test_each_event_head_write_crash_is_repaired_without_duplicate_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_head_write: int,
) -> None:
    original_write_head = CryptoCapitalLedger._write_head
    calls = 0

    def fail_selected_head(
        self: CryptoCapitalLedger, *, sequence: int, checksum: str
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == failed_head_write:
            raise OSError(f"simulated head crash {failed_head_write}")
        original_write_head(self, sequence=sequence, checksum=checksum)

    monkeypatch.setattr(CryptoCapitalLedger, "_write_head", fail_selected_head)
    with pytest.raises(OSError, match="simulated head crash"):
        run_fixture_auto_sim(_fixture(), output_root=tmp_path)
    events_path = tmp_path / "capital" / "events.jsonl"
    assert (
        len(events_path.read_text(encoding="utf-8").splitlines()) == failed_head_write
    )

    monkeypatch.setattr(CryptoCapitalLedger, "_write_head", original_write_head)
    recovered = run_fixture_auto_sim(_fixture(), output_root=tmp_path)
    assert recovered["bundle"]["capital"]["final"]["head_sequence"] == 5
    assert len(events_path.read_text(encoding="utf-8").splitlines()) == 5


def test_historical_bundle_recovery_keeps_its_reconcile_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write_projection = fixture_runtime._write_projection
    crashed = False

    def crash_first_projection(path: Path, payload: dict[str, object]) -> None:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise OSError("first bundle projection crashed")
        original_write_projection(path, payload)

    monkeypatch.setattr(
        fixture_runtime,
        "_write_projection",
        crash_first_projection,
    )
    with pytest.raises(OSError, match="first bundle projection crashed"):
        run_fixture_auto_sim(_fixture(), output_root=tmp_path)

    later_observation = _shifted_fixture(
        _observation_fixture(), minutes=5, suffix="later-observation"
    )
    later = run_fixture_auto_sim(later_observation, output_root=tmp_path)
    assert later["bundle"]["capital"]["final"]["head_sequence"] == 7

    recovered = run_fixture_auto_sim(_fixture(), output_root=tmp_path)
    assert recovered["idempotent_replay"] is False
    assert recovered["bundle"]["capital"]["final"]["head_sequence"] == 5
    replay = run_fixture_auto_sim(_fixture(), output_root=tmp_path)
    assert replay["idempotent_replay"] is True
    assert replay["bundle"]["capital"]["final"]["head_sequence"] == 5


def test_run_bundle_hash_tampering_is_detected_before_replay(tmp_path: Path) -> None:
    first = run_fixture_auto_sim(_fixture(), output_root=tmp_path)
    bundle_path = tmp_path / "runs" / f"{first['bundle']['run_id']}.json"
    events_path = tmp_path / "capital" / "events.jsonl"
    events_before = events_path.read_bytes()
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    payload["status"] = "forged"
    bundle_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(CryptoLedgerError, match="envelope_mismatch|sha256_invalid"):
        run_fixture_auto_sim(_fixture(), output_root=tmp_path)
    assert events_path.read_bytes() == events_before


def test_rehashed_bundle_cannot_forge_reconciled_capital(tmp_path: Path) -> None:
    first = run_fixture_auto_sim(_fixture(), output_root=tmp_path)
    bundle_path = tmp_path / "runs" / f"{first['bundle']['run_id']}.json"
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    payload["capital"]["final"]["cash"] = "999999"
    material = dict(payload)
    material.pop("business_bundle_sha256")
    payload["business_bundle_sha256"] = fixture_auto_sim._sha256(material)
    bundle_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(CryptoLedgerError, match="capital_reconcile_binding_mismatch"):
        run_fixture_auto_sim(_fixture(), output_root=tmp_path)


@pytest.mark.parametrize(
    "section,field,value,match",
    [
        ("evidence_qualification", "production_eligible", True, "market_evidence"),
        ("champion", "promotion_authorized", True, "champion_mismatch"),
        ("champion", "maximum_position_pct", "1", "champion_mismatch"),
    ],
)
def test_rehashed_bundle_cannot_forge_qualification_or_champion(
    tmp_path: Path,
    section: str,
    field: str,
    value: object,
    match: str,
) -> None:
    first = run_fixture_auto_sim(_fixture(), output_root=tmp_path)
    bundle_path = tmp_path / "runs" / f"{first['bundle']['run_id']}.json"
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    payload[section][field] = value
    material = dict(payload)
    material.pop("business_bundle_sha256")
    payload["business_bundle_sha256"] = fixture_auto_sim._sha256(material)
    bundle_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        (CryptoLedgerError, CryptoSafetyError), match=f"{match}|non_authority"
    ):
        run_fixture_auto_sim(_fixture(), output_root=tmp_path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("real_trading_enabled", True),
        ("promotion_authorized", True),
        ("production_eligible", True),
    ],
)
def test_rehashed_bundle_rejects_added_authority_fields(
    tmp_path: Path, field: str, value: object
) -> None:
    first = run_fixture_auto_sim(_fixture(), output_root=tmp_path)
    bundle_path = tmp_path / "runs" / f"{first['bundle']['run_id']}.json"
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    payload[field] = value
    material = dict(payload)
    material.pop("business_bundle_sha256")
    payload["business_bundle_sha256"] = fixture_auto_sim._sha256(material)
    bundle_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        (CryptoLedgerError, CryptoSafetyError), match="schema_mismatch|non_authority"
    ):
        run_fixture_auto_sim(_fixture(), output_root=tmp_path)


def test_cross_root_business_bundle_is_deterministic(tmp_path: Path) -> None:
    first = run_fixture_auto_sim(_fixture(), output_root=tmp_path / "one")["bundle"]
    second = run_fixture_auto_sim(_fixture(), output_root=tmp_path / "two")["bundle"]

    assert first == second
    assert first["business_bundle_sha256"] == second["business_bundle_sha256"]
    assert first["order_intent"]["intent_id"] == second["order_intent"]["intent_id"]
    assert first["paper_receipt"]["receipt_id"] == second["paper_receipt"]["receipt_id"]


def test_llm_sidecar_cannot_change_champion_decision_or_order(tmp_path: Path) -> None:
    first_payload = _fixture()
    second_payload = copy.deepcopy(first_payload)
    second_payload["llm_evidence"]["summary"] = (
        "Completely different untrusted prose that remains evidence-only."
    )

    first = run_fixture_auto_sim(first_payload, output_root=tmp_path)["bundle"]
    second = run_fixture_auto_sim(second_payload, output_root=tmp_path)["bundle"]

    assert first["run_id"] == second["run_id"]
    assert first["champion"] == second["champion"]
    assert first["decision"] == second["decision"]
    assert first["order_intent"] == second["order_intent"]
    assert first["capital"]["final"] == second["capital"]["final"]
    assert first == second
    journal = (tmp_path / "sidecars" / "llm_evidence.jsonl").read_text(encoding="utf-8")
    assert len(journal.splitlines()) == 2


def test_damaged_llm_sidecar_cannot_block_committed_core_replay(tmp_path: Path) -> None:
    sidecar_root = tmp_path / "sidecars"
    sidecar_root.mkdir(parents=True)
    (sidecar_root / "llm_evidence.jsonl").write_text("{partial", encoding="utf-8")

    first = run_fixture_auto_sim(_fixture(), output_root=tmp_path)
    events_path = tmp_path / "capital" / "events.jsonl"
    events_before = events_path.read_bytes()

    assert first["bundle"]["status"] == "fixture_simulated"
    assert first["llm_sidecar"] == {
        "status": "degraded",
        "authority": "none",
        "reason": "llm_sidecar_partial_tail",
    }

    replay = run_fixture_auto_sim(_fixture(), output_root=tmp_path)
    assert replay["idempotent_replay"] is True
    assert replay["bundle"] == first["bundle"]
    assert replay["llm_sidecar"]["status"] == "degraded"
    assert events_path.read_bytes() == events_before


@pytest.mark.parametrize("serialized", ["[]\n", "null\n", '"x"\n'])
def test_non_object_llm_sidecar_rows_cannot_block_committed_core(
    tmp_path: Path, serialized: str
) -> None:
    sidecar_root = tmp_path / "sidecars"
    sidecar_root.mkdir(parents=True)
    (sidecar_root / "llm_evidence.jsonl").write_text(serialized, encoding="utf-8")

    result = run_fixture_auto_sim(_fixture(), output_root=tmp_path)

    assert result["bundle"]["status"] == "fixture_simulated"
    assert result["llm_sidecar"] == {
        "status": "degraded",
        "authority": "none",
        "reason": "llm_sidecar_row_not_object",
    }
    assert (tmp_path / "capital" / "events.jsonl").is_file()


@pytest.mark.parametrize(
    "row,reason",
    [
        ({"contract": "forged"}, "llm_sidecar_row_schema_mismatch"),
        (
            {
                "contract": "tradingagent.crypto.llm_sidecar_journal.v1",
                "run_id": "forged-run",
                "sha256": "0" * 64,
                "payload": {},
                "authority": "live",
                "used_for_decision": False,
                "network_used": False,
                "execution_eligible": False,
                "execution_authority": False,
                "durable_execution_receipt": False,
                "outbox_id": None,
                "capital_commit_id": None,
                "durability_scope": "local_fixture_fsync_only",
            },
            "non_authority_field_invalid",
        ),
    ],
)
def test_tampered_llm_sidecar_objects_degrade_without_blocking_core(
    tmp_path: Path, row: dict[str, object], reason: str
) -> None:
    sidecar_root = tmp_path / "sidecars"
    sidecar_root.mkdir(parents=True)
    (sidecar_root / "llm_evidence.jsonl").write_text(
        json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    result = run_fixture_auto_sim(_fixture(), output_root=tmp_path)

    assert result["bundle"]["status"] == "fixture_simulated"
    assert result["llm_sidecar"]["status"] == "degraded"
    assert reason in result["llm_sidecar"]["reason"]


def test_llm_sidecar_authority_fields_fail_before_state_write(tmp_path: Path) -> None:
    payload = _fixture()
    payload["llm_evidence"]["quantity"] = "100"

    with pytest.raises(CryptoSafetyError, match="decision_authority_fields"):
        run_fixture_auto_sim(payload, output_root=tmp_path / "state")
    assert not (tmp_path / "state").exists()


def test_nested_llm_authority_fields_fail_before_state_write(tmp_path: Path) -> None:
    payload = _fixture()
    payload["llm_evidence"]["recommendation"] = {
        "side": "buy",
        "quantity": "100",
        "risk_budget": "all",
    }

    with pytest.raises(CryptoSafetyError, match="decision_authority_fields"):
        run_fixture_auto_sim(payload, output_root=tmp_path / "state")
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("real_trading_enabled", True),
        ("network_used", True),
        ("model_network_used", True),
        ("execution_mode", "binance_spot_testnet"),
        ("broker", "live"),
    ],
)
def test_contradictory_or_unknown_fixture_fields_fail_closed(
    tmp_path: Path, field: str, value: object
) -> None:
    payload = _fixture()
    payload[field] = value
    output_root = tmp_path / "state"

    with pytest.raises(
        (CryptoEvidenceError, CryptoSafetyError),
        match="fixture_schema_mismatch|forbidden_fixture_key",
    ):
        run_fixture_auto_sim(payload, output_root=output_root)
    assert not output_root.exists()


@pytest.mark.parametrize(
    "lineage_field,value",
    [
        ("fixture_id", "different-fixture"),
        ("source", "real_live_exchange"),
    ],
)
def test_fixture_lineage_must_bind_checked_in_source(
    tmp_path: Path, lineage_field: str, value: str
) -> None:
    payload = _fixture()
    payload["metadata"]["lineage"][lineage_field] = value
    output_root = tmp_path / "state"

    with pytest.raises(CryptoEvidenceError, match="lineage_binding_invalid"):
        run_fixture_auto_sim(payload, output_root=output_root)
    assert not output_root.exists()


def _degrade(payload: dict[str, object]) -> None:
    payload["metadata"]["degraded"] = True


def _stale(payload: dict[str, object]) -> None:
    payload["metadata"]["freshness"]["state"] = "stale"


def _open_last_bar(payload: dict[str, object]) -> None:
    payload["bars_5m"][-1]["closed"] = False


def _gap_last_bar(payload: dict[str, object]) -> None:
    payload["bars_5m"][-1]["open_time"] = "2026-07-18T23:56:00Z"


def _future_data(payload: dict[str, object]) -> None:
    payload["metadata"]["data_through"] = "2026-07-19T00:05:00Z"


def _delayed_observation(payload: dict[str, object]) -> None:
    payload["metadata"]["observed_at"] = "2026-07-19T00:06:00Z"


def _invent_dataset(payload: dict[str, object]) -> None:
    payload["dataset_id"] = "invented.crypto.5m"


def _old_provider_route(payload: dict[str, object]) -> None:
    payload["provider_route"] = "/crypto"


@pytest.mark.parametrize(
    "mutator,error_type,match",
    [
        (_degrade, CryptoEvidenceError, "metadata_not_ready"),
        (_stale, CryptoEvidenceError, "freshness_failed"),
        (_open_last_bar, CryptoEvidenceError, "bar_must_be_closed"),
        (_gap_last_bar, CryptoEvidenceError, "bar_5m_alignment_invalid"),
        (_future_data, CryptoEvidenceError, "timestamp_order_invalid"),
        (_delayed_observation, CryptoEvidenceError, "observation_lag_exceeded"),
        (_invent_dataset, CryptoSafetyError, "forbidden_fixture_key"),
        (_old_provider_route, CryptoSafetyError, "forbidden_fixture_key"),
    ],
)
def test_invalid_evidence_fails_before_capital_or_order_state(
    tmp_path: Path,
    mutator,
    error_type: type[Exception],
    match: str,
) -> None:
    payload = _fixture()
    mutator(payload)
    output_root = tmp_path / "state"

    with pytest.raises(error_type, match=match):
        run_fixture_auto_sim(payload, output_root=output_root)
    assert not output_root.exists()


@pytest.mark.parametrize("value", ["true", "1", "yes", "live"])
def test_real_trading_flag_fails_before_state_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    output_root = tmp_path / "state"
    monkeypatch.setenv("REAL_TRADING_ENABLED", value)

    with pytest.raises(CryptoSafetyError, match="must_be_false"):
        run_fixture_auto_sim(_fixture(), output_root=output_root)
    assert not output_root.exists()


def test_fixture_cycle_makes_zero_network_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def bomb(*args, **kwargs):
        calls.append("network")
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket, "create_connection", bomb)
    monkeypatch.setattr(socket, "socket", bomb)

    result = run_fixture_auto_sim(_fixture(), output_root=tmp_path)

    assert result["bundle"]["status"] == "fixture_simulated"
    assert calls == []


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("price_tick", "3", "bar_open_off_tick"),
        ("quantity_step", "0.03", "order_quantity_not_eligible"),
        ("min_notional", "1001", "order_notional_below_minimum"),
        ("min_quantity", "0.03", "order_quantity_not_eligible"),
    ],
)
def test_spot_filters_fail_closed_without_state_write(
    tmp_path: Path, field: str, value: str, match: str
) -> None:
    payload = _fixture()
    payload["instrument"][field] = value
    output_root = tmp_path / "state"

    with pytest.raises(CryptoEvidenceError, match=match):
        run_fixture_auto_sim(payload, output_root=output_root)
    assert not output_root.exists()


def test_observation_cycle_reconciles_without_order(tmp_path: Path) -> None:
    result = run_fixture_auto_sim(_observation_fixture(), output_root=tmp_path)
    bundle = result["bundle"]

    assert bundle["decision"]["action"] == "observe"
    assert bundle["order_intent"] is None
    assert bundle["paper_receipt"] is None
    assert bundle["capital"]["final"]["cash"] == "10000"
    assert bundle["capital"]["final"]["positions"] == {}
    assert bundle["capital"]["final"]["orders"] == {}
    assert bundle["capital"]["final"]["head_sequence"] == 3
    assert bundle["sample_review"]["sample_type"] == "observation_only"
    assert bundle["sample_review"]["net_pnl_after_fee"] == "0"


def test_btc_and_eth_share_capital_with_fresh_per_symbol_marks(
    tmp_path: Path,
) -> None:
    btc = run_fixture_auto_sim(_fixture(), output_root=tmp_path)["bundle"]
    eth = run_fixture_auto_sim(_eth_fixture(), output_root=tmp_path)["bundle"]

    assert btc["capital"]["final"]["positions"] == {"BTCUSDT": "0.019996"}
    final = eth["capital"]["final"]
    assert final["positions"] == {"BTCUSDT": "0.019996", "ETHUSDT": "0.019996"}
    assert final["marks"] == {"BTCUSDT": "49990.00", "ETHUSDT": "49990.00"}
    assert final["mark_slots"] == {
        "BTCUSDT": "2026-07-19T00:01:00Z",
        "ETHUSDT": "2026-07-19T00:01:00Z",
    }
    assert final["cash"] == "7998.00008008"
    assert final["equity"] == "9997.20016008"
    assert final["head_sequence"] == 9


def test_account_cycle_lock_prevents_two_same_symbol_orders_exceeding_cap(
    tmp_path: Path,
) -> None:
    first_payload = _retag(_fixture(), "concurrent-a")
    second_payload = _retag(_fixture(), "concurrent-b")
    barrier = Barrier(3)

    def worker(payload: dict[str, object]) -> str:
        barrier.wait(timeout=2)
        try:
            run_fixture_auto_sim(payload, output_root=tmp_path)
        except CryptoLedgerError as exc:
            return str(exc)
        return "completed"

    executor = ThreadPoolExecutor(max_workers=2)
    ledger = fixture_runtime._open_runtime_ledger(tmp_path / "capital")
    try:
        with ledger._cycle_lock():
            futures = [
                executor.submit(worker, first_payload),
                executor.submit(worker, second_payload),
            ]
            barrier.wait(timeout=2)
        outcomes = [future.result(timeout=5) for future in futures]
    finally:
        executor.shutdown(wait=True)

    assert outcomes.count("completed") == 1
    assert (
        sum(
            "execution_slot_not_monotonic" in outcome
            or "position_cap_exceeded" in outcome
            for outcome in outcomes
        )
        == 1
    )
    final = ledger.snapshot(
        marks={"BTCUSDT": "50000.00"},
        mark_slots={"BTCUSDT": "2026-07-19T00:00:00Z"},
        valuation_slot="2026-07-19T00:00:00Z",
    )
    assert final["positions"] == {"BTCUSDT": "0.019996"}
    assert final["head_sequence"] == 5


def test_incomplete_claim_blocks_later_cycle_until_original_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write_head = CryptoCapitalLedger._write_head
    calls = 0

    def crash_after_reserve(
        self: CryptoCapitalLedger, *, sequence: int, checksum: str
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("reserve head crash")
        original_write_head(self, sequence=sequence, checksum=checksum)

    monkeypatch.setattr(CryptoCapitalLedger, "_write_head", crash_after_reserve)
    with pytest.raises(OSError, match="reserve head crash"):
        run_fixture_auto_sim(_fixture(), output_root=tmp_path)
    monkeypatch.setattr(CryptoCapitalLedger, "_write_head", original_write_head)

    later = _shifted_fixture(_observation_fixture(), minutes=5, suffix="blocked-later")
    with pytest.raises(CryptoLedgerError, match="prior_cycle_incomplete"):
        run_fixture_auto_sim(later, output_root=tmp_path)

    recovered = run_fixture_auto_sim(_fixture(), output_root=tmp_path)
    assert recovered["bundle"]["capital"]["final"]["head_sequence"] == 5
    later_result = run_fixture_auto_sim(later, output_root=tmp_path)
    assert later_result["bundle"]["capital"]["final"]["head_sequence"] == 7


def test_position_cap_rejection_does_not_leave_incomplete_cycle(
    tmp_path: Path,
) -> None:
    run_fixture_auto_sim(_fixture(), output_root=tmp_path)
    second_buy = _shifted_fixture(_fixture(), minutes=5, suffix="second-buy")

    with pytest.raises(CryptoLedgerError, match="position_cap_exceeded"):
        run_fixture_auto_sim(second_buy, output_root=tmp_path)
    events_path = tmp_path / "capital" / "events.jsonl"
    assert len(events_path.read_text(encoding="utf-8").splitlines()) == 5

    later_observation = _shifted_fixture(
        _observation_fixture(), minutes=5, suffix="post-cap-observation"
    )
    result = run_fixture_auto_sim(later_observation, output_root=tmp_path)
    assert result["bundle"]["decision"]["action"] == "observe"
    assert result["bundle"]["capital"]["final"]["head_sequence"] == 7


def test_same_symbol_historical_slot_cannot_regress_account_marks(
    tmp_path: Path,
) -> None:
    newer = _shifted_fixture(_fixture(), minutes=10, suffix="newer-buy")
    run_fixture_auto_sim(newer, output_root=tmp_path)
    older = _shifted_fixture(
        _observation_fixture(), minutes=5, suffix="older-observation"
    )

    with pytest.raises(CryptoLedgerError, match="execution_slot_not_monotonic"):
        run_fixture_auto_sim(older, output_root=tmp_path)


def test_capital_chain_tampering_is_detected(tmp_path: Path) -> None:
    result = run_fixture_auto_sim(_fixture(), output_root=tmp_path)
    events_path = tmp_path / "capital" / "events.jsonl"
    rows = events_path.read_text(encoding="utf-8").splitlines()
    fill = json.loads(rows[3])
    fill["payload"]["fee"] = "2.00000000"
    rows[3] = json.dumps(
        fill, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    events_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    ledger = CryptoCapitalLedger(tmp_path / "capital")
    with pytest.raises(CryptoLedgerError, match="checksum_invalid"):
        ledger.snapshot(
            marks={"BTCUSDT": "50000.00"},
            mark_slots={"BTCUSDT": "2026-07-19T00:00:00Z"},
            valuation_slot="2026-07-19T00:00:00Z",
        )
    assert result["bundle"]["capital"]["final"]["balanced"] is True


@pytest.mark.parametrize(
    "event_index,field,value,match",
    [
        (0, "generation", True, "opening_authority_mismatch"),
        (1, "capital_generation", True, "cycle_claim_invalid"),
        (2, "capital_generation", True, "reservation_exposure_invalid"),
        (3, "authority_generation", True, "fill_values_invalid"),
        (2, "maximum_fee", "0", "reservation_exposure_invalid"),
        (3, "fee", "0", "fill_values_invalid"),
    ],
)
def test_rehashed_capital_events_reject_bool_generations_and_unfrozen_fees(
    tmp_path: Path,
    event_index: int,
    field: str,
    value: object,
    match: str,
) -> None:
    run_fixture_auto_sim(_fixture(), output_root=tmp_path)
    events_path = tmp_path / "capital" / "events.jsonl"
    rows = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]
    rows[event_index]["payload"][field] = value
    _rehash_capital_rows(rows)

    ledger = CryptoCapitalLedger(tmp_path / "capital")
    with pytest.raises(CryptoLedgerError, match=match):
        ledger._validate_event_rows_without_head(rows)


def test_candidate_event_sequence_rejects_bool() -> None:
    ledger = CryptoCapitalLedger(Path("unused-read-only-ledger"))
    row: dict[str, object] = {
        "contract": "tradingagent.crypto.capital_ledger.v1",
        "sequence": True,
        "event_id": "unused",
        "event_type": "opening",
        "reference_id": "unused",
        "payload": {},
        "previous_checksum": "",
    }
    row["checksum"] = CryptoCapitalLedger._event_checksum(row)

    with pytest.raises(CryptoLedgerError, match="candidate_chain_invalid"):
        ledger._validate_event_rows_without_head([row])


def test_head_recovery_rejects_bool_generation_without_rewrite(tmp_path: Path) -> None:
    run_fixture_auto_sim(_fixture(), output_root=tmp_path)
    head_path = tmp_path / "capital" / "head.json"
    head = json.loads(head_path.read_text(encoding="utf-8"))
    head["generation"] = True
    head_path.write_text(
        json.dumps(head, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    poisoned = head_path.read_bytes()

    writer = fixture_runtime._open_runtime_ledger(tmp_path / "capital")
    with pytest.raises(CryptoLedgerError, match="head_mismatch"):
        writer._recover_head()

    assert head_path.read_bytes() == poisoned


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("filled_at", "2026-07-19T00:02:00Z", "order_binding_mismatch"),
        ("evidence_receipt_id", "wrong-receipt", "order_binding_mismatch"),
        ("market_evidence_sha256", "f" * 64, "order_binding_mismatch"),
        ("champion_sha256", "e" * 64, "order_binding_mismatch"),
        ("fee_asset", "BTC", "fill_values_invalid"),
        ("status", "filled", "fill_values_invalid"),
        ("authority_id", "wrong-authority", "fill_values_invalid"),
        ("authority_generation", 2, "fill_values_invalid"),
        ("authority_generation", True, "fill_values_invalid"),
        ("fee", Decimal("0"), "fill_values_invalid"),
        ("real_trading_enabled", True, "non_authority_field_invalid"),
        ("promotion_authorized", True, "schema_mismatch|non_authority"),
    ],
)
def test_direct_ledger_rejects_unbound_or_authoritative_fill_without_write(
    tmp_path: Path, field: str, value: object, match: str
) -> None:
    ledger, payload, checksum = _prepared_fill_ledger(tmp_path / "capital")
    payload[field] = value
    events_before = ledger.events_path.read_bytes()

    with pytest.raises((CryptoLedgerError, CryptoSafetyError), match=match):
        ledger._append_event(
            event_type="fill",
            reference_id=f"fill:{payload['receipt_id']}",
            payload=payload,
            expected_head_checksum=checksum,
        )

    assert ledger.events_path.read_bytes() == events_before


def test_frozen_champion_never_authorizes_promotion_or_live() -> None:
    champion = FROZEN_CHAMPION

    assert champion.status == "frozen_candidate"
    assert champion.manual_promotion_required is True
    assert champion.promotion_authorized is False
    assert champion.real_trading_enabled is False
    assert champion.regime_interval == "1h"
    assert champion.decision_interval == "15m"
    assert champion.execution_interval == "5m"
    assert len(champion.sha256) == 64


def test_bounded_json_tree_validator_boundaries_and_dag() -> None:
    depth_16: object = "leaf"
    for _ in range(16):
        depth_16 = [depth_16]
    _validate_json_tree(depth_16)
    with pytest.raises(CryptoEvidenceError, match="depth_exceeded"):
        _validate_json_tree([depth_16])

    _validate_json_tree(list(range(256)))
    with pytest.raises(CryptoEvidenceError, match="values_exceeded"):
        _validate_json_tree(list(range(257)))

    shared = {"leaf": "safe"}
    _validate_json_tree([shared, shared])
    cycle: list[object] = []
    cycle.append(cycle)
    with pytest.raises(CryptoEvidenceError, match="cycle_detected"):
        _validate_json_tree(cycle)


def test_recursive_cycle_and_forbidden_llm_authority_fail_before_output(
    tmp_path: Path,
) -> None:
    payload = _fixture()
    lineage = payload["metadata"]["lineage"]
    assert isinstance(lineage, dict)
    lineage["cycle"] = lineage
    with pytest.raises(CryptoEvidenceError, match="cycle_detected"):
        run_fixture_auto_sim(payload, output_root=tmp_path)
    assert list(tmp_path.iterdir()) == []

    payload = _fixture()
    payload["llm_evidence"]["nested"] = {"order_intent": {"side": "buy"}}
    with pytest.raises(CryptoSafetyError, match="decision_authority_fields"):
        run_fixture_auto_sim(payload, output_root=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_noncanonical_policy_and_champion_fail_at_public_boundaries(
    tmp_path: Path,
) -> None:
    class PolicySubclass(CryptoCapitalPolicy):
        pass

    class ChampionSubclass(FrozenChampionCandidate):
        pass

    with pytest.raises(CryptoSafetyError, match="policy_not_canonical"):
        run_fixture_auto_sim(_fixture(), output_root=tmp_path, policy=PolicySubclass())
    evidence = qualify_fixture_evidence(_fixture())
    with pytest.raises(CryptoSafetyError, match="champion_not_canonical"):
        evaluate_frozen_champion(evidence, ChampionSubclass())
    assert list(tmp_path.iterdir()) == []


def test_global_valuation_watermark_rejects_cross_symbol_time_regression(
    tmp_path: Path,
) -> None:
    run_fixture_auto_sim(
        _shifted_fixture(_fixture(), minutes=10, suffix="newer-btc"),
        output_root=tmp_path,
    )
    older_eth = _eth_fixture()
    _shifted_fixture(older_eth, minutes=5, suffix="older-eth")
    with pytest.raises(CryptoLedgerError, match="account_valuation_regressed"):
        run_fixture_auto_sim(older_eth, output_root=tmp_path)


def test_receipt_and_bundle_are_explicitly_non_authoritative(tmp_path: Path) -> None:
    bundle = run_fixture_auto_sim(_fixture(), output_root=tmp_path)["bundle"]
    for payload in (bundle, bundle["paper_receipt"]):
        assert payload["execution_eligible"] is False
        assert payload["execution_authority"] is False
        assert payload["durable_execution_receipt"] is False
        assert payload["outbox_id"] is None
        assert payload["capital_commit_id"] is None
        assert payload["durability_scope"] == "local_fixture_fsync_only"
    assert bundle["paper_receipt"]["status"] == "fixture_simulated"


def test_partial_tail_and_nested_symlink_fail_closed(tmp_path: Path) -> None:
    run_fixture_auto_sim(_fixture(), output_root=tmp_path)
    events_path = tmp_path / "capital" / "events.jsonl"
    with events_path.open("ab") as stream:
        stream.write(b"{partial")
    ledger = CryptoCapitalLedger(tmp_path / "capital")
    with pytest.raises(CryptoLedgerError, match="partial_tail"):
        ledger.head()

    other = tmp_path.parent / f"{tmp_path.name}-other"
    clean_root = tmp_path.parent / f"{tmp_path.name}-symlink"
    other.mkdir()
    clean_root.mkdir()
    (clean_root / "runs").symlink_to(other, target_is_directory=True)
    with pytest.raises(CryptoSafetyError, match="nested_symlink"):
        run_fixture_auto_sim(_fixture(), output_root=clean_root)
    assert not (clean_root / "capital").exists()
