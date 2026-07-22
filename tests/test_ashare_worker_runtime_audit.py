from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = REPO_ROOT / "tools" / "audit_ashare_worker_runtime.py"


def _load_audit_module():
    assert AUDIT_PATH.is_file(), "A-share worker runtime audit tool is missing"
    spec = importlib.util.spec_from_file_location(
        "ashare_worker_runtime_audit", AUDIT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _token(path: Path, *, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("fixture-secret-must-never-be-reported\n", encoding="utf-8")
    path.chmod(mode)
    return path


def _directories(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    release = tmp_path / "release"
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    log = tmp_path / "log"
    for path in (release, state, runtime, log):
        path.mkdir(mode=0o700)
    release.chmod(0o555)
    return release, state, runtime, log


def test_runtime_audit_accepts_separated_owned_directories_and_private_token(
    tmp_path: Path,
) -> None:
    audit = _load_audit_module()
    release, state, runtime, log = _directories(tmp_path)
    token = _token(tmp_path / "secrets" / "tradingdatas-read.token")

    directories = audit.audit_directory_layout(
        release_root=release,
        state_root=state,
        runtime_root=runtime,
        log_root=log,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
    )
    token_metadata = audit.audit_token_file(
        token,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
    )

    assert directories["separated"] is True
    assert token_metadata == {
        "link_count": 1,
        "mode": "0600",
        "owner_gid": os.getgid(),
        "owner_uid": os.getuid(),
        "regular": True,
    }
    assert "fixture-secret" not in repr(token_metadata)


@pytest.mark.parametrize("mode", (0o400, 0o640, 0o660, 0o700))
def test_token_audit_rejects_any_mode_other_than_0600(
    tmp_path: Path,
    mode: int,
) -> None:
    audit = _load_audit_module()
    token = _token(tmp_path / "secrets" / "tradingdatas-read.token", mode=mode)

    with pytest.raises(audit.RuntimeAuditError, match="token_mode_invalid"):
        audit.audit_token_file(
            token,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
        )


def test_token_audit_rejects_symlinked_parent_without_reading_secret(
    tmp_path: Path,
) -> None:
    audit = _load_audit_module()
    real_parent = tmp_path / "real-secrets"
    token = _token(real_parent / "tradingdatas-read.token")
    alias = tmp_path / "secret-alias"
    alias.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(audit.RuntimeAuditError, match="path_symlink_forbidden") as exc:
        audit.audit_token_file(
            alias / token.name,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
        )

    assert "fixture-secret" not in str(exc.value)


def test_token_audit_rejects_hardlink_nonregular_and_wrong_owner(
    tmp_path: Path,
) -> None:
    audit = _load_audit_module()
    token = _token(tmp_path / "secrets" / "tradingdatas-read.token")
    alias = token.with_name("alias.token")
    os.link(token, alias)
    with pytest.raises(audit.RuntimeAuditError, match="token_link_count_invalid"):
        audit.audit_token_file(
            token,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
        )

    alias.unlink()
    token.unlink()
    token.mkdir(mode=0o600)
    with pytest.raises(audit.RuntimeAuditError, match="token_regular_file_required"):
        audit.audit_token_file(
            token,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
        )

    token.rmdir()
    token = _token(token)
    with pytest.raises(audit.RuntimeAuditError, match="token_owner_invalid"):
        audit.audit_token_file(
            token,
            expected_uid=os.getuid() + 1,
            expected_gid=os.getgid(),
        )


def test_directory_audit_rejects_overlap_symlink_and_wrong_mode(tmp_path: Path) -> None:
    audit = _load_audit_module()
    release, state, runtime, log = _directories(tmp_path)

    with pytest.raises(audit.RuntimeAuditError, match="runtime_roots_not_separated"):
        audit.audit_directory_layout(
            release_root=release,
            state_root=state,
            runtime_root=state,
            log_root=log,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
        )

    runtime.rmdir()
    runtime.symlink_to(state, target_is_directory=True)
    with pytest.raises(audit.RuntimeAuditError, match="path_symlink_forbidden"):
        audit.audit_directory_layout(
            release_root=release,
            state_root=state,
            runtime_root=runtime,
            log_root=log,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
        )

    runtime.unlink()
    runtime.mkdir(mode=0o755)
    with pytest.raises(audit.RuntimeAuditError, match="runtime_directory_mode_invalid"):
        audit.audit_directory_layout(
            release_root=release,
            state_root=state,
            runtime_root=runtime,
            log_root=log,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
        )


def test_runtime_audit_cli_requires_exact_mg_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    audit = _load_audit_module()
    release, state, runtime, log = _directories(tmp_path)
    arguments = [
        "--release-root",
        str(release),
        "--state-root",
        str(state),
        "--runtime-root",
        str(runtime),
        "--log-root",
        str(log),
        "--expected-user",
        "fixture-user",
        "--expected-group",
        "fixture-group",
    ]
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    monkeypatch.setenv("MARKETGRAPH_MODE", "off")

    assert audit.main(arguments) == 2
    rejected = json.loads(capsys.readouterr().err)
    assert rejected["error"] == "marketgraph_mode_must_be_mg_off"

    monkeypatch.setenv("MARKETGRAPH_MODE", "mg_off")
    monkeypatch.setattr(audit, "_identity", lambda *_: (os.getuid(), os.getgid()))
    monkeypatch.setattr(
        audit,
        "audit_directory_layout",
        lambda **_: {"separated": True},
    )
    monkeypatch.setattr(
        audit,
        "audit_token_file",
        lambda *_args, **_kwargs: {"mode": "0600", "regular": True},
    )

    assert audit.main(arguments) == 0
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["marketgraph_mode"] == "mg_off"
