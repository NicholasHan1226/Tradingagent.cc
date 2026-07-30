from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import Crypto.delayed_paper_epoch as epoch_module
import Crypto.delayed_paper_runtime as runtime_module
from Crypto.delayed_paper_epoch import (
    CryptoDelayedPaperEpochContext,
    CryptoDelayedPaperEpochError,
    load_crypto_delayed_paper_epoch_manifest,
    prepare_crypto_delayed_paper_epoch,
    validate_epoch_runtime_context,
)
from Crypto.delayed_paper_epoch_runtime import (
    run_crypto_delayed_paper_epoch_once,
)
from Crypto.delayed_paper_runtime import (
    CryptoDelayedPaperRuntimeError,
    run_crypto_delayed_paper_server_once,
)
from tests.test_crypto_5m_support import (
    FixtureTradingDatasTransport,
    WINDOW_END,
)
from tests.test_crypto_delayed_paper_runtime import (
    _factory,
    _manifest_payload,
    _shifted_transport,
    _write_manifest,
)


EPOCH_ID = "crypto-delayed-paper-epoch-g2-20260729"


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _epoch_payload(
    *,
    output_root: Path,
    archived_root: Path,
) -> dict[str, Any]:
    return {
        "schema": "tradingagent.crypto.delayed_paper_epoch_manifest.v1",
        "epoch_id": EPOCH_ID,
        "epoch_generation": 2,
        "current_output_root": str(output_root),
        "archived_output_root": str(archived_root),
        "archived_epoch_policy": "read_only_archive_no_resume",
        "capital_baseline_policy_id": "crypto-capital-v1",
        "aggregate_with_archived_epoch": False,
        "safety": {
            "real_trading_enabled": False,
            "production_eligible": False,
            "execution_authority": False,
            "testnet_enabled": False,
            "live_broker_enabled": False,
            "model_network_enabled": False,
            "automatic_promotion_enabled": False,
            "automatic_risk_expansion_enabled": False,
        },
    }


def _write_epoch_manifest(
    path: Path,
    *,
    payload: dict[str, Any],
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_bytes(payload))
    path.chmod(0o600)
    return path


def _configure_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path]:
    archived_root = tmp_path / "crypto-delayed-paper"
    epoch_parent = tmp_path / "crypto-delayed-paper-epochs"
    output_root = epoch_parent / EPOCH_ID
    manifest_path = tmp_path / "crypto-delayed-paper.epoch.json"
    token_file = tmp_path / "tradingdatas-crypto-read.token"
    archived_root.mkdir(mode=0o700)
    epoch_parent.mkdir(mode=0o700)
    monkeypatch.setattr(
        epoch_module,
        "LEGACY_ARCHIVE_ROOT",
        archived_root,
    )
    monkeypatch.setattr(
        epoch_module,
        "EPOCH_ROOT_PARENT",
        epoch_parent,
    )
    monkeypatch.setattr(
        epoch_module,
        "EPOCH_MANIFEST_PATH",
        manifest_path,
    )
    monkeypatch.setattr(
        runtime_module,
        "RUNTIME_TOKEN_FILE",
        token_file,
    )
    _write_epoch_manifest(
        manifest_path,
        payload=_epoch_payload(
            output_root=output_root,
            archived_root=archived_root,
        ),
    )
    return manifest_path, archived_root, output_root, token_file


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_epoch_manifest_creates_immutable_identity_without_touching_archive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest_path, archived_root, output_root, _ = _configure_paths(
        monkeypatch,
        tmp_path,
    )
    (archived_root / "archive-proof.txt").write_text(
        "9 observations; 9 completions; preserve\n",
        encoding="utf-8",
    )
    archived_before = _tree_bytes(archived_root)

    context = load_crypto_delayed_paper_epoch_manifest(manifest_path)
    prepared = prepare_crypto_delayed_paper_epoch(context)
    identity_before = prepared.identity_path.read_bytes()
    replay = prepare_crypto_delayed_paper_epoch(context)

    assert prepared.output_root == output_root
    assert replay.identity_path.read_bytes() == identity_before
    identity = json.loads(identity_before)
    assert identity["epoch_id"] == EPOCH_ID
    assert identity["epoch_generation"] == 2
    assert identity["capital_baseline_policy_id"] == "crypto-capital-v1"
    assert identity["capital_baseline_usdt"] == "10000"
    assert identity["capital_generation_scope"] == (
        "local_fixture_opening_baseline_only"
    )
    assert identity["aggregate_with_archived_epoch"] is False
    assert identity["execution_authority"] is False
    assert identity["production_eligible"] is False
    assert _tree_bytes(archived_root) == archived_before


def test_runtime_validation_uses_existing_read_only_current_epoch_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest_path, _, output_root, _ = _configure_paths(monkeypatch, tmp_path)
    context = load_crypto_delayed_paper_epoch_manifest(manifest_path)
    prepare_crypto_delayed_paper_epoch(context)
    lock_path = output_root.parent / ".current_epoch.lock"
    before = lock_path.stat()
    original_open = epoch_module.os.open
    lock_flags: list[int] = []

    def recording_open(
        path: str | Path,
        flags: int,
        mode: int = 0o777,
    ) -> int:
        if Path(path) == lock_path:
            lock_flags.append(flags)
        return original_open(path, flags, mode)

    monkeypatch.setattr(epoch_module.os, "open", recording_open)
    validate_epoch_runtime_context(context, output_root=output_root)
    after = lock_path.stat()

    assert lock_flags
    assert all(
        flags & epoch_module.os.O_ACCMODE == epoch_module.os.O_RDONLY
        for flags in lock_flags
    )
    assert (before.st_ino, before.st_size, before.st_mtime_ns) == (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda payload: payload.update(aggregate_with_archived_epoch=True),
            "epoch_manifest_safety_invalid",
        ),
        (
            lambda payload: payload.update(epoch_generation=1),
            "epoch_generation_invalid",
        ),
        (
            lambda payload: payload.update(
                epoch_generation=3,
                epoch_id="crypto-delayed-paper-epoch-g3-20260729",
                current_output_root=str(
                    Path(str(payload["current_output_root"])).parent
                    / "crypto-delayed-paper-epoch-g3-20260729"
                ),
            ),
            "epoch_generation_invalid",
        ),
        (
            lambda payload: payload["safety"].update(real_trading_enabled=True),
            "epoch_manifest_safety_invalid",
        ),
        (
            lambda payload: payload["safety"].update(real_trading_enabled=0),
            "epoch_manifest_safety_invalid",
        ),
        (
            lambda payload: payload.update(archived_epoch_policy="resume_after_outage"),
            "epoch_archive_policy_invalid",
        ),
    ],
)
def test_epoch_manifest_rejects_unsafe_restart_contracts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: Any,
    reason: str,
) -> None:
    manifest_path, archived_root, output_root, _ = _configure_paths(
        monkeypatch,
        tmp_path,
    )
    payload = _epoch_payload(
        output_root=output_root,
        archived_root=archived_root,
    )
    mutation(payload)
    _write_epoch_manifest(manifest_path, payload=payload)

    with pytest.raises(CryptoDelayedPaperEpochError, match=reason):
        load_crypto_delayed_paper_epoch_manifest(manifest_path)


def test_epoch_identity_conflict_fails_closed_instead_of_adopting_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest_path, _, output_root, _ = _configure_paths(
        monkeypatch,
        tmp_path,
    )
    output_root.mkdir(mode=0o700)
    (output_root / "foreign-ledger.jsonl").write_text(
        "{}\n",
        encoding="utf-8",
    )
    context = load_crypto_delayed_paper_epoch_manifest(manifest_path)

    with pytest.raises(
        CryptoDelayedPaperEpochError,
        match="current_epoch_missing_with_existing_state",
    ):
        prepare_crypto_delayed_paper_epoch(context)


def test_epoch_identity_rejects_falsey_rechecksummed_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest_path, _, _, _ = _configure_paths(
        monkeypatch,
        tmp_path,
    )
    context = load_crypto_delayed_paper_epoch_manifest(manifest_path)
    prepared = prepare_crypto_delayed_paper_epoch(context)
    identity = json.loads(prepared.identity_path.read_text(encoding="utf-8"))
    identity["production_eligible"] = 0
    identity.pop("epoch_identity_sha256")
    identity["epoch_identity_sha256"] = epoch_module._sha256(identity)
    prepared.identity_path.write_bytes(_canonical_bytes(identity))
    prepared.identity_path.chmod(0o600)

    with pytest.raises(
        CryptoDelayedPaperEpochError,
        match="epoch_identity_conflict",
    ):
        prepare_crypto_delayed_paper_epoch(context)


def test_epoch_runtime_uses_only_new_root_and_replay_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        epoch_manifest,
        archived_root,
        output_root,
        token_file,
    ) = _configure_paths(monkeypatch, tmp_path)
    (archived_root / "capital-events.jsonl").write_text(
        "legacy-event\n" * 41,
        encoding="utf-8",
    )
    archived_before = _tree_bytes(archived_root)
    runtime_manifest = _write_manifest(
        tmp_path / "runtime",
        payload=_manifest_payload(),
    )
    transport = FixtureTradingDatasTransport()

    first = run_crypto_delayed_paper_epoch_once(
        epoch_manifest=epoch_manifest,
        runtime_manifest=runtime_manifest,
        token_file=token_file,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(transport),
    )
    first_tree = _tree_bytes(output_root)
    replay = run_crypto_delayed_paper_epoch_once(
        epoch_manifest=epoch_manifest,
        runtime_manifest=runtime_manifest,
        token_file=token_file,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(transport),
    )
    assert _tree_bytes(output_root) == first_tree
    adjacent = run_crypto_delayed_paper_epoch_once(
        epoch_manifest=epoch_manifest,
        runtime_manifest=runtime_manifest,
        token_file=token_file,
        now=WINDOW_END + timedelta(minutes=5, seconds=55),
        transport_factory=_factory(_shifted_transport(5)),
    )
    adjacent_two = run_crypto_delayed_paper_epoch_once(
        epoch_manifest=epoch_manifest,
        runtime_manifest=runtime_manifest,
        token_file=token_file,
        now=WINDOW_END + timedelta(minutes=10, seconds=55),
        transport_factory=_factory(_shifted_transport(10)),
    )

    assert first["status"] == "completed"
    assert first["requested_window_end"] == (
        WINDOW_END.isoformat().replace("+00:00", "Z")
    )
    assert replay["status"] == "noop"
    assert adjacent["status"] == "completed"
    assert adjacent_two["status"] == "completed"
    assert first["epoch_id"] == replay["epoch_id"] == EPOCH_ID
    assert adjacent["epoch_id"] == EPOCH_ID
    assert adjacent_two["epoch_id"] == EPOCH_ID
    assert first["epoch_generation"] == 2
    assert first["aggregate_with_archived_epoch"] is False
    assert first["archived_epoch_consumed"] is False
    assert first["capital_baseline_usdt"] == "10000"
    assert first["real_trading_enabled"] is False
    assert first["execution_authority"] is False
    assert first["production_eligible"] is False
    assert _tree_bytes(archived_root) == archived_before
    assert (output_root / "capital").is_dir()
    assert (output_root / "delayed_paper").is_dir()
    assert (
        len(list((output_root / "delayed_paper" / "completions").glob("*.json"))) == 3
    )


def test_core_rejects_epoch_root_without_validated_epoch_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        _,
        _,
        output_root,
        token_file,
    ) = _configure_paths(monkeypatch, tmp_path)
    runtime_manifest = _write_manifest(
        tmp_path / "runtime",
        payload=_manifest_payload(),
    )

    with pytest.raises(
        CryptoDelayedPaperRuntimeError,
        match="runtime_epoch_context_required",
    ):
        run_crypto_delayed_paper_server_once(
            runtime_manifest=runtime_manifest,
            token_file=token_file,
            output_root=output_root,
            now=WINDOW_END + timedelta(seconds=55),
            transport_factory=_factory(FixtureTradingDatasTransport()),
        )


def test_core_rejects_legacy_archive_without_epoch_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        _,
        archived_root,
        _,
        token_file,
    ) = _configure_paths(monkeypatch, tmp_path)
    runtime_manifest = _write_manifest(
        tmp_path / "runtime",
        payload=_manifest_payload(),
    )

    with pytest.raises(
        CryptoDelayedPaperRuntimeError,
        match="runtime_epoch_context_required",
    ):
        run_crypto_delayed_paper_server_once(
            runtime_manifest=runtime_manifest,
            token_file=token_file,
            output_root=archived_root,
            now=WINDOW_END + timedelta(seconds=55),
            transport_factory=_factory(FixtureTradingDatasTransport()),
        )
    assert _tree_bytes(archived_root) == {}


def test_forged_context_cannot_escape_epoch_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        manifest_path,
        archived_root,
        _,
        token_file,
    ) = _configure_paths(monkeypatch, tmp_path)
    trusted = load_crypto_delayed_paper_epoch_manifest(manifest_path)
    escaped_root = tmp_path / "escaped-epoch-root"
    forged = CryptoDelayedPaperEpochContext(
        epoch_id=str(escaped_root),
        epoch_generation=True,
        output_root=escaped_root,
        archived_output_root=archived_root,
        manifest_path=manifest_path,
        manifest_sha256="f" * 64,
        _proof=epoch_module._CONTEXT_PROOF,
    )

    with pytest.raises(
        CryptoDelayedPaperEpochError,
        match="epoch_runtime_context_invalid",
    ):
        prepare_crypto_delayed_paper_epoch(forged)
    with pytest.raises(
        CryptoDelayedPaperRuntimeError,
        match="runtime_epoch_context_invalid",
    ):
        run_crypto_delayed_paper_server_once(
            runtime_manifest=_write_manifest(
                tmp_path / "runtime",
                payload=_manifest_payload(),
            ),
            token_file=token_file,
            output_root=escaped_root,
            now=WINDOW_END + timedelta(seconds=55),
            transport_factory=_factory(FixtureTradingDatasTransport()),
            epoch_context=forged,
        )
    assert trusted.output_root != escaped_root
    assert not escaped_root.exists()


def test_current_epoch_anchor_rejects_same_generation_root_switch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        manifest_path,
        archived_root,
        output_root,
        _,
    ) = _configure_paths(monkeypatch, tmp_path)
    first = load_crypto_delayed_paper_epoch_manifest(manifest_path)
    prepared = prepare_crypto_delayed_paper_epoch(first)
    current_path = epoch_module.EPOCH_ROOT_PARENT / ".current_epoch.json"
    current_before = current_path.read_bytes()
    second_epoch_id = "crypto-delayed-paper-epoch-g2-20260729-b"
    second_root = epoch_module.EPOCH_ROOT_PARENT / second_epoch_id
    switched = _epoch_payload(
        output_root=second_root,
        archived_root=archived_root,
    )
    switched["epoch_id"] = second_epoch_id
    _write_epoch_manifest(manifest_path, payload=switched)
    second = load_crypto_delayed_paper_epoch_manifest(manifest_path)

    with pytest.raises(
        CryptoDelayedPaperEpochError,
        match="current_epoch_conflict",
    ):
        prepare_crypto_delayed_paper_epoch(second)

    assert prepared.output_root == output_root
    assert current_path.read_bytes() == current_before
    assert not second_root.exists()


def test_missing_current_anchor_never_reclaims_populated_epoch_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        manifest_path,
        archived_root,
        _,
        _,
    ) = _configure_paths(monkeypatch, tmp_path)
    first = load_crypto_delayed_paper_epoch_manifest(manifest_path)
    prepare_crypto_delayed_paper_epoch(first)
    current_path = epoch_module.EPOCH_ROOT_PARENT / ".current_epoch.json"
    current_path.unlink()

    with pytest.raises(
        CryptoDelayedPaperEpochError,
        match="current_epoch_missing_with_existing_state",
    ):
        prepare_crypto_delayed_paper_epoch(first)

    second_epoch_id = "crypto-delayed-paper-epoch-g2-after-anchor-loss"
    second_root = epoch_module.EPOCH_ROOT_PARENT / second_epoch_id
    switched = _epoch_payload(
        output_root=second_root,
        archived_root=archived_root,
    )
    switched["epoch_id"] = second_epoch_id
    _write_epoch_manifest(manifest_path, payload=switched)
    second = load_crypto_delayed_paper_epoch_manifest(manifest_path)

    with pytest.raises(
        CryptoDelayedPaperEpochError,
        match="current_epoch_missing_with_existing_state",
    ):
        prepare_crypto_delayed_paper_epoch(second)

    assert not second_root.exists()


def test_core_rejects_epoch_context_until_identity_is_persisted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        epoch_manifest,
        _,
        output_root,
        token_file,
    ) = _configure_paths(monkeypatch, tmp_path)
    runtime_manifest = _write_manifest(
        tmp_path / "runtime",
        payload=_manifest_payload(),
    )
    context = load_crypto_delayed_paper_epoch_manifest(epoch_manifest)

    with pytest.raises(
        CryptoDelayedPaperRuntimeError,
        match="runtime_epoch_context_invalid",
    ):
        run_crypto_delayed_paper_server_once(
            runtime_manifest=runtime_manifest,
            token_file=token_file,
            output_root=output_root,
            now=WINDOW_END + timedelta(seconds=55),
            transport_factory=_factory(FixtureTradingDatasTransport()),
            epoch_context=context,
        )
    assert not output_root.exists()


def test_epoch_manifest_rejects_duplicate_keys_and_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest_path, archived_root, output_root, _ = _configure_paths(
        monkeypatch,
        tmp_path,
    )
    manifest_path.write_text(
        (
            '{"schema":"tradingagent.crypto.delayed_paper_epoch_manifest.v1",'
            '"schema":"duplicate"}\n'
        ),
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)
    with pytest.raises(
        CryptoDelayedPaperEpochError,
        match="epoch_manifest_duplicate_key",
    ):
        load_crypto_delayed_paper_epoch_manifest(manifest_path)

    real = tmp_path / "real-epoch-manifest.json"
    _write_epoch_manifest(
        real,
        payload=_epoch_payload(
            output_root=output_root,
            archived_root=archived_root,
        ),
    )
    manifest_path.unlink()
    manifest_path.symlink_to(real)
    with pytest.raises(
        CryptoDelayedPaperEpochError,
        match="epoch_manifest_file_untrusted",
    ):
        load_crypto_delayed_paper_epoch_manifest(manifest_path)


def test_epoch_manifest_must_remain_repository_external(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest_path, _, _, _ = _configure_paths(
        monkeypatch,
        tmp_path,
    )
    monkeypatch.setattr(
        epoch_module,
        "_REPO_ROOT",
        tmp_path,
    )

    with pytest.raises(
        CryptoDelayedPaperEpochError,
        match="epoch_manifest_file_untrusted",
    ):
        load_crypto_delayed_paper_epoch_manifest(manifest_path)
