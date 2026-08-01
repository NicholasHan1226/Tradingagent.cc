from __future__ import annotations

from pathlib import Path

import pytest

from tools.effective_runtime_release import (
    CONTRACT,
    EffectiveRuntimeReleaseError,
    SYSTEMD_PROPERTIES,
    resolve_effective_runtime_release,
)


ROOT = Path("/opt/investment/releases/tradingagent")
CURRENT = "a" * 40
PINNED = "b" * 40


def _properties(**overrides: str) -> dict[str, str]:
    values = {
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "running",
        "MainPID": "987",
        "FragmentPath": "/etc/systemd/system/tradingagent.service",
        "DropInPaths": "",
        "ExecStart": f"python {ROOT}/current/Crypto/runtime.py",
        "ExecStartPre": "",
        "WorkingDirectory": f"{ROOT}/current",
    }
    values.update(overrides)
    assert set(values) == set(SYSTEMD_PROPERTIES)
    return values


def test_active_current_unit_is_bound_to_process_and_release() -> None:
    result = resolve_effective_runtime_release(
        product="tradingagent",
        unit="tradingagent-crypto.service",
        systemd_properties=_properties(),
        release_root=ROOT,
        current_release=CURRENT,
        process_text=f"{ROOT}/{CURRENT}\npython {ROOT}/{CURRENT}/Crypto/runtime.py",
    )

    assert result.contract == CONTRACT
    assert result.effective_release == CURRENT
    assert result.effective_source == "running_process"
    assert result.current_matches_effective is True
    assert result.runtime_verified is True
    assert result.blockers == ()


def test_explicit_dropin_release_wins_and_exposes_current_mismatch() -> None:
    result = resolve_effective_runtime_release(
        product="tradingagent",
        unit="tradingagent-crypto.service",
        systemd_properties=_properties(
            ExecStart=f"python {ROOT}/{PINNED}/Crypto/runtime.py",
            WorkingDirectory=f"{ROOT}/{PINNED}",
            DropInPaths="/etc/systemd/system/tradingagent-crypto.service.d/runtime.conf",
        ),
        release_root=ROOT,
        current_release=CURRENT,
        process_text=f"{ROOT}/{PINNED}\npython {ROOT}/{PINNED}/Crypto/runtime.py",
    )

    assert result.effective_release == PINNED
    assert result.current_matches_effective is False
    assert result.runtime_verified is True


def test_active_process_and_unit_mismatch_fails_closed() -> None:
    result = resolve_effective_runtime_release(
        product="tradingagent",
        unit="tradingagent-crypto.service",
        systemd_properties=_properties(),
        release_root=ROOT,
        current_release=CURRENT,
        process_text=f"{ROOT}/{PINNED}/Crypto/runtime.py",
    )

    assert result.runtime_verified is False
    assert "process_unit_release_mismatch" in result.blockers


def test_active_unit_without_process_release_fails_closed() -> None:
    result = resolve_effective_runtime_release(
        product="tradingagent",
        unit="tradingagent-crypto.service",
        systemd_properties=_properties(),
        release_root=ROOT,
        current_release=CURRENT,
        process_text="/usr/bin/python3 -m Crypto.runtime",
    )

    assert result.effective_release == CURRENT
    assert result.runtime_verified is False
    assert "active_process_release_unproven" in result.blockers


def test_inactive_unit_reports_declared_release_but_not_runtime_verified() -> None:
    result = resolve_effective_runtime_release(
        product="tradingagent",
        unit="tradingagent-crypto.service",
        systemd_properties=_properties(
            ActiveState="inactive", SubState="dead", MainPID="0"
        ),
        release_root=ROOT,
        current_release=CURRENT,
    )

    assert result.effective_release == CURRENT
    assert result.effective_source == "systemd_effective_configuration"
    assert result.runtime_verified is False


def test_unresolved_current_symlink_and_ambiguous_unit_fail_closed() -> None:
    result = resolve_effective_runtime_release(
        product="tradingagent",
        unit="tradingagent-crypto.service",
        systemd_properties=_properties(
            ExecStart=f"python {ROOT}/current/a.py {ROOT}/{PINNED}/b.py"
        ),
        release_root=ROOT,
        current_release=None,
        process_text=f"{ROOT}/{PINNED}/b.py",
    )

    assert result.runtime_verified is False
    assert "current_release_unresolved" in result.blockers


@pytest.mark.parametrize(
    ("product", "unit", "reason"),
    [
        ("../tradingagent", "tradingagent.service", "product_invalid"),
        ("tradingagent", "tradingagent;rm.service", "unit_name_invalid"),
    ],
)
def test_untrusted_identifiers_fail_before_system_inspection(
    product: str, unit: str, reason: str
) -> None:
    with pytest.raises(EffectiveRuntimeReleaseError, match=reason):
        resolve_effective_runtime_release(
            product=product,
            unit=unit,
            systemd_properties=_properties(),
            release_root=ROOT,
            current_release=CURRENT,
        )
