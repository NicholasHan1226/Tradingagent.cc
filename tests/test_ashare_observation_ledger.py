from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import shared.runtime.ashare_observation_ledger as ledger_module
from shared.data.research_snapshot import (
    ResearchDataSnapshot,
    ResearchDatasetSnapshot,
)
from shared.runtime.ashare_observation_ledger import (
    ASHARE_OBSERVATION_MEMBERSHIP_SCHEMA_ID,
    LABEL_HORIZONS,
    OBSERVED_REASON_CODE,
    AshareObservationLedgerConflict,
    AshareObservationLedgerContractError,
    AshareObservationLedgerCorruption,
    AshareObservationMembershipRecord,
    FileAshareObservationMembershipLedger,
    build_ashare_observation_membership_artifact,
)


SESSION = "20260722"
DECISION_AS_OF = "2026-07-22T07:10:00+00:00"
PROFILE_ID = "ashare-phase1-current-observation-v1"
PROFILE_SHA256 = "1" * 64
CATALOG_VERSION = "v1-fixture-catalog"
PROBE_SHA256 = "2" * 64


def _sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _dataset() -> ResearchDatasetSnapshot:
    rows = [
        {
            "ts_code": "000001.SZ",
            "trade_date": SESSION,
            "close": 12.0,
            "vol": 1000.0,
        },
        {
            "ts_code": "300001.SZ",
            "trade_date": SESSION,
            "close": 18.0,
            "vol": 2000.0,
        },
        {
            "ts_code": "600000.SH",
            "trade_date": SESSION,
            "close": 10.0,
            "vol": 3000.0,
        },
        {
            "ts_code": "688001.SH",
            "trade_date": SESSION,
            "close": 22.0,
            "vol": 4000.0,
        },
    ]
    return ResearchDatasetSnapshot(
        dataset_id="cn.equity.daily",
        role="required_execution",
        api_version="v1",
        catalog_version=CATALOG_VERSION,
        request_id="request-daily-fixture",
        receipt_id="receipt-daily-fixture",
        evidence_state="ready",
        evidence_action="accept",
        eligible=True,
        weight=1.0,
        reasons=(),
        source_proof_complete=True,
        lineage_sha256="3" * 64,
        source_proof_sha256="4" * 64,
        data_through="2026-07-22T07:00:00+00:00",
        observed_at="2026-07-22T07:05:00+00:00",
        next_cursor=None,
        row_count=len(rows),
        observation_mode="current_observation",
        historical_pit_eligible=False,
        query_as_of_mode="decision_as_of",
        minimum_row_count=1,
        max_pages=2,
        max_rows=10,
        identity_fields=("ts_code", "trade_date"),
        row_event_time_field="trade_date",
        row_event_time_format="yyyymmdd",
        row_event_timezone="Asia/Shanghai",
        row_event_time_semantic="session",
        identity_sha256="5" * 64,
        row_observation_sha256="6" * 64,
        max_row_observed_at="2026-07-22T07:05:00+00:00",
        max_row_event_value=SESSION,
        page_count=1,
        pagination_trace_sha256="7" * 64,
        pagination_semantic_sha256="8" * 64,
        page_request_set_sha256="9" * 64,
        page_response_set_sha256="a" * 64,
        cursor_chain_sha256="b" * 64,
        response_sha256="c" * 64,
        _rows_json=json.dumps(
            rows,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )


def _snapshot() -> ResearchDataSnapshot:
    dataset = _dataset()
    identity = {
        "profile_id": PROFILE_ID,
        "profile_contract_sha256": PROFILE_SHA256,
        "catalog_version": CATALOG_VERSION,
        "decision_as_of": DECISION_AS_OF,
        "datasets": [
            {
                "dataset_id": dataset.dataset_id,
                "role": dataset.role,
                "response_sha256": dataset.response_sha256,
            }
        ],
        "blocking_reasons": [],
    }
    return ResearchDataSnapshot(
        profile_id=PROFILE_ID,
        profile_contract_sha256=PROFILE_SHA256,
        catalog_version=CATALOG_VERSION,
        decision_as_of=DECISION_AS_OF,
        datasets=(dataset,),
        execution_eligible=True,
        historical_pit_eligible=False,
        blocking_reasons=(),
        snapshot_sha256=_sha256(identity),
    )


def _records() -> tuple[AshareObservationMembershipRecord, ...]:
    return (
        AshareObservationMembershipRecord(
            symbol="600000.SH",
            disposition="observed",
            reason_code=OBSERVED_REASON_CODE,
        ),
        AshareObservationMembershipRecord(
            symbol="300001.SZ",
            disposition="excluded",
            reason_code="chinext_individual_permission_unavailable",
        ),
        AshareObservationMembershipRecord(
            symbol="000001.SZ",
            disposition="observed",
            reason_code=OBSERVED_REASON_CODE,
        ),
        AshareObservationMembershipRecord(
            symbol="688001.SH",
            disposition="excluded",
            reason_code="star_individual_permission_unavailable",
        ),
    )


def _observation_receipt(snapshot: ResearchDataSnapshot) -> dict[str, object]:
    observed = sorted(
        item.symbol for item in _records() if item.disposition == "observed"
    )
    payload: dict[str, object] = {
        "schema_id": "tradingagent.ashare.observation-receipt.v1",
        "profile_id": PROFILE_ID,
        "catalog_version": CATALOG_VERSION,
        "decision_as_of": DECISION_AS_OF,
        "manifest_sha256": "d" * 64,
        "snapshot_sha256": snapshot.snapshot_sha256,
        "probe_receipt_sha256": PROBE_SHA256,
        "tradable_universe_count": len(observed),
        "tradable_universe_sha256": _sha256(observed),
        "excluded_reason_counts": {
            "chinext_individual_permission_unavailable": 1,
            "star_individual_permission_unavailable": 1,
        },
        "context_probe_roles": ["industry_context"],
        "mode": "observation_only",
        "marketgraph_mode": "mg_off",
        "real_trading_enabled": False,
        "historical_pit_eligible": False,
        "execution_authority": False,
    }
    payload["receipt_sha256"] = _sha256(payload)
    return payload


def _publish(
    root: Path,
    *,
    snapshot: ResearchDataSnapshot | None = None,
    receipt: dict[str, object] | None = None,
    records: tuple[AshareObservationMembershipRecord, ...] | None = None,
):
    frozen_snapshot = _snapshot() if snapshot is None else snapshot
    frozen_receipt = (
        _observation_receipt(frozen_snapshot) if receipt is None else receipt
    )
    return FileAshareObservationMembershipLedger(root).compare_and_swap(
        observation_session=SESSION,
        research_snapshot=frozen_snapshot,
        observation_receipt=frozen_receipt,
        records=_records() if records is None else records,
        expected_content_sha256=None,
    )


def test_publishes_canonical_content_addressed_membership_and_binding(
    tmp_path: Path,
) -> None:
    root = tmp_path / "observation-membership"
    artifact = _publish(root)

    assert artifact.schema_id == ASHARE_OBSERVATION_MEMBERSHIP_SCHEMA_ID
    assert artifact.observation_session == SESSION
    assert artifact.decision_as_of == DECISION_AS_OF
    assert artifact.profile_id == PROFILE_ID
    assert artifact.profile_contract_sha256 == PROFILE_SHA256
    assert artifact.catalog_version == CATALOG_VERSION
    assert artifact.catalog_version_sha256 == _sha256(CATALOG_VERSION)
    assert artifact.snapshot_sha256 == _snapshot().snapshot_sha256
    assert artifact.probe_receipt_sha256 == PROBE_SHA256
    assert (
        artifact.observation_receipt_sha256
        == _observation_receipt(_snapshot())["receipt_sha256"]
    )
    assert artifact.universe_sha256 == _sha256(["000001.SZ", "600000.SH"])
    assert tuple(item.symbol for item in artifact.records) == (
        "000001.SZ",
        "300001.SZ",
        "600000.SH",
        "688001.SH",
    )
    assert artifact.label_horizons == LABEL_HORIZONS
    assert artifact.historical_pit_eligible is False
    assert artifact.learning_eligible is False
    assert artifact.performance_eligible is False
    assert artifact.promotion_eligible is False
    assert artifact.real_trading_enabled is False
    assert len(artifact.content_sha256) == 64

    artifact_path = root / f"artifact-{artifact.content_sha256}.json"
    binding_paths = tuple(root.glob("session-*.json"))
    assert artifact_path.exists()
    assert len(binding_paths) == 1
    for path in (artifact_path, binding_paths[0]):
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.stat().st_nlink == 1
        raw = path.read_text(encoding="utf-8")
        assert raw == json.dumps(
            json.loads(raw),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    forbidden = {
        "capital",
        "order",
        "fill",
        "probability",
        "rank",
        "transport",
        "marketgraph",
        "llm",
    }
    assert not forbidden.intersection(json.loads(artifact_path.read_text()))

    recovered = FileAshareObservationMembershipLedger(root).load_bound_session(
        observation_session=SESSION
    )
    assert recovered == artifact


def test_pure_builder_matches_persisted_artifact_without_creating_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "observation-membership"
    built = build_ashare_observation_membership_artifact(
        observation_session=SESSION,
        research_snapshot=_snapshot(),
        observation_receipt=_observation_receipt(_snapshot()),
        records=_records(),
    )

    assert not root.exists()
    assert _publish(root) == built


def test_missing_root_load_is_side_effect_free(tmp_path: Path) -> None:
    root = tmp_path / "never-created"
    ledger = FileAshareObservationMembershipLedger(root)

    assert ledger.load_bound_session(observation_session=SESSION) is None
    assert not root.exists()


def test_ledger_has_no_observation_runtime_import_cycle() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    source = (
        repository_root / "shared/runtime/ashare_observation_ledger.py"
    ).read_text(encoding="utf-8")
    assert "from shared.runtime.ashare_observation import" not in source
    for imports in (
        "import shared.runtime.ashare_observation; "
        "import shared.runtime.ashare_observation_ledger",
        "import shared.runtime.ashare_observation_ledger; "
        "import shared.runtime.ashare_observation",
    ):
        completed = subprocess.run(
            [sys.executable, "-c", imports],
            check=False,
            capture_output=True,
            text=True,
            cwd=repository_root,
        )
        assert completed.returncode == 0, completed.stderr


def test_exact_replay_is_idempotent_and_conflicting_session_is_rejected(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ledger"
    first = _publish(root)
    replay = _publish(root, records=tuple(reversed(_records())))

    assert replay == first
    changed = tuple(
        replace(item, reason_code="different_exclusion_reason")
        if item.symbol == "688001.SH"
        else item
        for item in _records()
    )
    receipt = _observation_receipt(_snapshot())
    receipt["excluded_reason_counts"] = {
        "chinext_individual_permission_unavailable": 1,
        "different_exclusion_reason": 1,
    }
    receipt["receipt_sha256"] = _sha256(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    with pytest.raises(
        AshareObservationLedgerConflict,
        match="immutable_session_conflict",
    ):
        _publish(root, receipt=receipt, records=changed)


def test_concurrent_exact_replays_publish_one_artifact_and_one_binding(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ledger"

    def publish() -> str:
        return _publish(root).content_sha256

    with ThreadPoolExecutor(max_workers=8) as executor:
        content_hashes = list(executor.map(lambda _item: publish(), range(16)))

    assert len(set(content_hashes)) == 1
    assert len(tuple(root.glob("artifact-*.json"))) == 1
    assert len(tuple(root.glob("session-*.json"))) == 1


def test_compare_and_swap_expected_hash_is_enforced(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    first = _publish(root)
    ledger = FileAshareObservationMembershipLedger(root)

    with pytest.raises(
        AshareObservationLedgerConflict,
        match="compare_and_swap_failed",
    ):
        ledger.compare_and_swap(
            observation_session=SESSION,
            research_snapshot=_snapshot(),
            observation_receipt=_observation_receipt(_snapshot()),
            records=_records(),
            expected_content_sha256="f" * 64,
        )
    assert (
        ledger.compare_and_swap(
            observation_session=SESSION,
            research_snapshot=_snapshot(),
            observation_receipt=_observation_receipt(_snapshot()),
            records=_records(),
            expected_content_sha256=first.content_sha256,
        )
        == first
    )


def test_record_scope_rejects_duplicate_symbols(tmp_path: Path) -> None:
    records = _records() + (
        AshareObservationMembershipRecord(
            symbol="600000.SH",
            disposition="observed",
            reason_code=OBSERVED_REASON_CODE,
        ),
    )
    with pytest.raises(AshareObservationLedgerContractError, match="duplicate_symbol"):
        _publish(tmp_path / "ledger", records=records)


@pytest.mark.parametrize("symbol", ("300001.SZ", "688001.SH"))
def test_chinext_or_star_cannot_be_observed(symbol: str) -> None:
    with pytest.raises(
        AshareObservationLedgerContractError,
        match="observed_symbol_not_mainboard",
    ):
        AshareObservationMembershipRecord(
            symbol=symbol,
            disposition="observed",
            reason_code=OBSERVED_REASON_CODE,
        )


def test_every_daily_symbol_must_have_exactly_one_membership_record(
    tmp_path: Path,
) -> None:
    missing = tuple(item for item in _records() if item.symbol != "688001.SH")
    with pytest.raises(
        AshareObservationLedgerContractError,
        match="daily_symbol_membership_mismatch",
    ):
        _publish(tmp_path / "ledger", records=missing)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("snapshot_hash", "snapshot_sha256_mismatch"),
        ("receipt_hash", "observation_receipt_sha256_mismatch"),
        ("receipt_snapshot", "observation_snapshot_mismatch"),
        ("receipt_probe", "probe_receipt_sha256_invalid"),
        ("universe", "universe_membership_mismatch"),
        ("universe_count_bool", "universe_membership_mismatch"),
        ("excluded", "excluded_reason_counts_mismatch"),
    ),
)
def test_snapshot_and_receipt_cross_bindings_fail_closed(
    tmp_path: Path,
    mutation: str,
    reason: str,
) -> None:
    snapshot = _snapshot()
    receipt = _observation_receipt(snapshot)
    if mutation == "snapshot_hash":
        snapshot = replace(snapshot, snapshot_sha256="f" * 64)
        receipt["snapshot_sha256"] = snapshot.snapshot_sha256
        receipt["receipt_sha256"] = _sha256(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        )
    elif mutation == "receipt_hash":
        receipt["receipt_sha256"] = "f" * 64
    elif mutation == "receipt_snapshot":
        receipt["snapshot_sha256"] = "f" * 64
        receipt["receipt_sha256"] = _sha256(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        )
    elif mutation == "receipt_probe":
        receipt["probe_receipt_sha256"] = "not-a-hash"
        receipt["receipt_sha256"] = _sha256(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        )
    elif mutation == "universe":
        receipt["tradable_universe_sha256"] = "f" * 64
        receipt["receipt_sha256"] = _sha256(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        )
    elif mutation == "universe_count_bool":
        receipt["tradable_universe_count"] = True
        receipt["receipt_sha256"] = _sha256(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        )
    elif mutation == "excluded":
        receipt["excluded_reason_counts"] = {
            "chinext_individual_permission_unavailable": 2
        }
        receipt["receipt_sha256"] = _sha256(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        )
    with pytest.raises(AshareObservationLedgerContractError, match=reason):
        _publish(tmp_path / "ledger", snapshot=snapshot, receipt=receipt)


def test_historical_learning_performance_and_promotion_claims_are_fixed_false(
    tmp_path: Path,
) -> None:
    artifact = _publish(tmp_path / "ledger")
    assert artifact.label_horizons == ()
    assert (
        artifact.historical_pit_eligible,
        artifact.learning_eligible,
        artifact.performance_eligible,
        artifact.promotion_eligible,
        artifact.real_trading_enabled,
    ) == (False, False, False, False, False)
    with pytest.raises(
        AshareObservationLedgerContractError,
        match="artifact_authority_flag_invalid",
    ):
        replace(artifact, learning_eligible=True)


def test_root_path_traversal_and_symlink_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="path_traversal"):
        FileAshareObservationMembershipLedger(tmp_path / "safe" / ".." / "escape")

    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(AshareObservationLedgerCorruption, match="symlink"):
        FileAshareObservationMembershipLedger(linked)


@pytest.mark.parametrize("kind", ("artifact", "binding"))
def test_symlink_and_hardlink_artifacts_are_rejected(
    tmp_path: Path,
    kind: str,
) -> None:
    root = tmp_path / "ledger"
    artifact = _publish(root)
    path = (
        root / f"artifact-{artifact.content_sha256}.json"
        if kind == "artifact"
        else next(root.glob("session-*.json"))
    )
    original = tmp_path / f"{kind}-original.json"
    path.rename(original)
    path.symlink_to(original)
    with pytest.raises(AshareObservationLedgerCorruption, match="symlink"):
        FileAshareObservationMembershipLedger(root).load_bound_session(
            observation_session=SESSION
        )

    path.unlink()
    os.link(original, path)
    alias = tmp_path / f"{kind}-alias.json"
    os.link(original, alias)
    with pytest.raises(AshareObservationLedgerCorruption, match="hardlink"):
        FileAshareObservationMembershipLedger(root).load_bound_session(
            observation_session=SESSION
        )


@pytest.mark.parametrize("kind", ("artifact", "binding"))
def test_noncanonical_or_corrupt_files_fail_closed(
    tmp_path: Path,
    kind: str,
) -> None:
    root = tmp_path / "ledger"
    artifact = _publish(root)
    path = (
        root / f"artifact-{artifact.content_sha256}.json"
        if kind == "artifact"
        else next(root.glob("session-*.json"))
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    if kind == "artifact":
        raw["learning_eligible"] = True
        unsigned = dict(raw)
        unsigned.pop("content_sha256")
        raw["content_sha256"] = _sha256(unsigned)
    else:
        raw["artifact_content_sha256"] = "f" * 64
    path.write_text(
        json.dumps(
            raw,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)

    with pytest.raises(
        AshareObservationLedgerCorruption,
        match="not_canonical|hash_mismatch|authority_flag",
    ):
        FileAshareObservationMembershipLedger(root).load_bound_session(
            observation_session=SESSION
        )


def test_wrong_file_mode_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    artifact = _publish(root)
    path = root / f"artifact-{artifact.content_sha256}.json"
    path.chmod(0o644)
    with pytest.raises(AshareObservationLedgerCorruption, match="mode_invalid"):
        FileAshareObservationMembershipLedger(root).load_bound_session(
            observation_session=SESSION
        )


def _published_path(
    root: Path,
    artifact,
    *,
    kind: str,
) -> Path:
    if kind == "artifact":
        return root / f"artifact-{artifact.content_sha256}.json"
    if kind == "binding":
        return next(root.glob("session-*.json"))
    raise AssertionError(f"unknown kind: {kind}")


def _exercise_recovery(root: Path, *, route: str):
    if route == "load":
        return FileAshareObservationMembershipLedger(root).load_bound_session(
            observation_session=SESSION
        )
    if route == "cas":
        return _publish(root)
    raise AssertionError(f"unknown route: {route}")


@pytest.mark.parametrize(
    ("kind", "route"),
    (("artifact", "load"), ("binding", "cas")),
)
def test_recovers_single_canonical_atomic_publish_tmp_alias_under_session_lock(
    tmp_path: Path,
    kind: str,
    route: str,
) -> None:
    root = tmp_path / "ledger"
    artifact = _publish(root)
    final = _published_path(root, artifact, kind=kind)
    temporary = root / f".tmp-{'1' * 32}.json"
    os.link(final, temporary)

    assert final.stat().st_nlink == 2
    assert temporary.stat().st_ino == final.stat().st_ino

    assert _exercise_recovery(root, route=route) == artifact
    assert not temporary.exists()
    assert final.stat().st_nlink == 1
    assert final.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("kind", "route"),
    (("artifact", "load"), ("binding", "cas")),
)
@pytest.mark.parametrize(
    "mutation",
    (
        "multiple_aliases",
        "wrong_name",
        "wrong_owner",
        "wrong_mode",
        "symlink",
        "nlink_gt_two",
        "noncanonical",
        "too_large",
    ),
)
def test_atomic_publish_recovery_rejects_suspicious_aliases_without_deleting_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    route: str,
    mutation: str,
) -> None:
    root = tmp_path / "ledger"
    artifact = _publish(root)
    final = _published_path(root, artifact, kind=kind)
    temporary = root / f".tmp-{'2' * 32}.json"
    suspicious = [temporary]

    if mutation == "wrong_name":
        temporary = root / ".tmp-not-a-uuid.json"
        suspicious = [temporary]
        os.link(final, temporary)
    elif mutation == "symlink":
        os.link(final, temporary)
        symlink = root / f".tmp-{'3' * 32}.json"
        symlink.symlink_to(final.name)
        suspicious.append(symlink)
    else:
        os.link(final, temporary)
        if mutation == "multiple_aliases":
            second = root / f".tmp-{'3' * 32}.json"
            os.link(final, second)
            suspicious.append(second)
        elif mutation == "nlink_gt_two":
            second = root / "unexpected-hardlink-alias.json"
            os.link(final, second)
            suspicious.append(second)
        elif mutation == "wrong_owner":
            original_lstat = Path.lstat

            def spoof_owner(path: Path):
                observed = original_lstat(path)
                if path == temporary:
                    return SimpleNamespace(
                        st_mode=observed.st_mode,
                        st_uid=observed.st_uid + 1,
                        st_dev=observed.st_dev,
                        st_ino=observed.st_ino,
                        st_nlink=observed.st_nlink,
                        st_size=observed.st_size,
                    )
                return observed

            monkeypatch.setattr(Path, "lstat", spoof_owner)
        elif mutation == "wrong_mode":
            final.chmod(0o644)
        elif mutation == "noncanonical":
            final.write_text(
                final.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )
        elif mutation == "too_large":
            monkeypatch.setattr(
                ledger_module,
                "_MAX_FILE_BYTES",
                final.stat().st_size - 1,
            )

    with pytest.raises(AshareObservationLedgerCorruption):
        _exercise_recovery(root, route=route)

    assert final.exists()
    for path in suspicious:
        assert path.exists() or path.is_symlink()
