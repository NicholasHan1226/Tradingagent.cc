"""Contracts for the isolated scale500 preflight entry point."""

import getpass
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "scale500_paper_preflight.sh"


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_preflight_requires_a_positive_numeric_budget_before_any_runner() -> None:
    text = _script()
    assert 'set -eu' in text
    assert "max_seconds_invalid" in text
    assert "[ \"$MAX_SECONDS\" -gt 0 ]" in text

    completed = subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            **os.environ,
            "PREFLIGHT_RUN_USER": getpass.getuser(),
            "PREFLIGHT_MAX_SECONDS": "not-a-number",
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert "PREFLIGHT_FAIL max_seconds_invalid" in completed.stdout


def test_preflight_refuses_any_active_production_timer_or_service() -> None:
    text = _script()
    assert 'SCALE500_TIMER="tradingagent-ashare-minute-scale500-paper.timer"' in text
    assert 'SCALE500_SERVICE="tradingagent-ashare-minute-scale500-paper.service"' in text
    assert 'systemctl is-active --quiet "$SCALE500_TIMER"' in text
    assert 'systemctl is-active --quiet "$SCALE500_SERVICE"' in text
    assert "production_service_active_wait_for_completion" in text


def test_preflight_stops_before_runner_when_the_service_is_active(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_systemctl = fake_bin / "systemctl"
    fake_systemctl.write_text(
        "#!/bin/sh\n"
        "[ \"$1\" = is-active ] || exit 99\n"
        "case \"$3\" in\n"
        "  *timer) exit 1 ;;\n"
        "  *service) exit 0 ;;\n"
        "esac\n"
        "exit 99\n",
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o755)
    token = tmp_path / "token"
    token.write_text("unused", encoding="utf-8")
    env_file = tmp_path / "scale500.env"
    env_file.write_text("unused", encoding="utf-8")
    completed = subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "PREFLIGHT_RUN_USER": getpass.getuser(),
            "PREFLIGHT_PYTHON": "/usr/bin/true",
            "PREFLIGHT_TOKEN_FILE": str(token),
            "PREFLIGHT_ENV_FILE": str(env_file),
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert "PREFLIGHT_FAIL production_service_active_wait_for_completion" in completed.stdout


def test_preflight_keeps_universe_digest_and_throwaway_roots_bound_to_runtime() -> None:
    text = _script()
    assert '--expected-universe-sha256 "$UNIVERSE_SHA"' in text
    assert '--scale-state-root "$STATE_ROOT"' in text
    assert '--rollback30-state-root "$ROLLBACK_ROOT"' in text
    assert "REAL_TRADING_ENABLED=false" in text
    assert "rm -rf \"$STATE_ROOT\" \"$ROLLBACK_ROOT\"" in text
