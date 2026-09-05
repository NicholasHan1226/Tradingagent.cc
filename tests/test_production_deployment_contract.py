from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
ASHARE_RELEASE_UNITS = (
    "tradingagent-ashare-minute-session.service",
    "tradingagent-ashare-minute-paper.service",
    "tradingagent-ashare-minute-scale500-session.service",
    "tradingagent-ashare-minute-scale500-paper.service",
)
CRYPTO_RUNTIME_RELEASE_UNITS = (
    "tradingagent-crypto-round-trip-g5-acceptance.service",
    "tradingagent-crypto-round-trip-g5-delayed-paper.service",
    "tradingagent-crypto-round-trip-g5-health.service",
    "tradingagent-crypto-round-trip-g5-learning.service",
    "tradingagent-crypto-round-trip-g5-learning-scrub.service",
    "tradingagent-crypto-forty-symbol-observation.service",
    "tradingagent-crypto-ten-symbol-observation.service",
    "tradingagent-crypto-ten-symbol-factor-research.service",
    "tradingagent-crypto-ten-symbol-factor-research-scrub.service",
)


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_snapshot_nginx_local_only_contract() -> None:
    template = _read("deploy/nginx/tradingagent-snapshot-local-only.conf")
    directives = [
        line.strip() for line in template.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert directives == [
        "location = /api/trading-agent/snapshot {",
        "allow 127.0.0.1;",
        "allow ::1;",
        "deny all;",
        "limit_req zone=api burst=20 nodelay;",
        "proxy_pass http://127.0.0.1:8787/api/trading-agent/snapshot;",
        "proxy_set_header Host $host;",
        "proxy_set_header X-Real-IP $remote_addr;",
        "proxy_read_timeout 15s;",
        "}",
    ]


def test_snapshot_ingress_documentation_does_not_treat_proxy_as_user_auth() -> None:
    documentation = _read("front/docs/integration.md")
    assert 'proxy_set_header Authorization "Bearer server-only-token"' not in documentation
    assert "CORS is not access" in documentation
    assert "real_ip_header" in documentation and "set_real_ip_from" in documentation
    assert "edge/ICP 403 is not origin denial" in documentation
    assert "location = /api/trading-agent/snapshot" in documentation


def test_production_deployment_shell_scripts_parse() -> None:
    for relative in ("deploy/release.sh", "deploy/bootstrap-production-server.sh"):
        subprocess.run(
            ["bash", "-n", str(ROOT / relative)],
            check=True,
            capture_output=True,
            text=True,
        )


def test_main_ci_publishes_the_exact_tested_release_artifact() -> None:
    workflow = _read(".github/workflows/test.yml")

    assert "workflow_dispatch:" in workflow
    assert "expected_sha:" in workflow
    assert "SOURCE_SHA:" in workflow
    assert "ref: ${{ env.SOURCE_SHA }}" in workflow
    assert '[[ "$(git rev-parse HEAD)" == "$SOURCE_SHA" ]]' in workflow
    assert "npm run build:all" in workflow
    assert "Package tested release" in workflow
    assert 'git -C "$GITHUB_WORKSPACE" archive "$SOURCE_SHA"' in workflow
    assert 'printf \'%s\\n\' "$SOURCE_SHA" > "$release_root/.source-sha"' in workflow
    assert "front/dist-server/server/tradingAgentSnapshotHttp.js" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "name: tradingagent-release-${{ env.SOURCE_SHA }}" in workflow
    assert "github.event_name == 'push' && github.ref == 'refs/heads/main'" in workflow
    assert "github.event_name == 'workflow_dispatch'" in workflow
    assert "workflow_dispatch' && inputs.expected_sha" in workflow
    assert "cache: pip" in workflow
    assert "cache-dependency-path: requirements.txt" in workflow


def test_automerge_is_limited_to_explicit_progressive_m0_docs_and_tests() -> None:
    workflow = _read(".github/workflows/automerge.yml")

    assert "Automerge Eligible M0 PRs" in workflow
    assert "M0_AUTOMERGE_LABEL: automerge-m0" in workflow
    assert "TRUSTED_PR_AUTHOR: NicholasHan1226" in workflow
    assert "M0 automerge is limited to docs and tests." in workflow
    assert "M0 current-base freshness is not required; GitHub mergeability remains required." in workflow
    assert '[[ "$base_sha" == "$current_main_sha" ]]' not in workflow
    assert 'MERGE_JSON="$(gh api' not in workflow
    assert 'merge_json="$(gh api' in workflow
    assert '"repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/merge"' in workflow
    assert "actions/workflows/test.yml/dispatches" in workflow
    assert "controller-accepted-deploy" in workflow
    assert "deployment remains" in workflow


def test_deploy_workflow_requires_controller_accepted_exact_main_test_run() -> None:
    workflow = _read(".github/workflows/deploy-production.yml")

    assert "repository_dispatch:" in workflow
    assert "- controller-accepted-deploy" in workflow
    assert "github.event.action == 'controller-accepted-deploy'" in workflow
    assert "github.event.sender.login == 'NicholasHan1226'" in workflow
    assert "github.event.client_payload.sha" in workflow
    assert "github.event.client_payload.test_run_id" in workflow
    assert "Validate Controller-accepted main test run" in workflow
    assert 'gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${DEPLOY_RUN_ID}"' in workflow
    assert '"$(jq -r \'.name\' <<<"$run_json")" == "TradingAgent Tests"' in workflow
    assert '"$(jq -r \'.conclusion\' <<<"$run_json")" == "success"' in workflow
    assert '"$(jq -r \'.head_branch\' <<<"$run_json")" == "main"' in workflow
    assert '"$(jq -r \'.event\' <<<"$run_json")" == "push"' in workflow
    assert '"$(jq -r \'.head_sha\' <<<"$run_json")" == "$DEPLOY_SHA"' in workflow
    assert "workflow_run:" not in workflow
    assert "github.event.workflow_run" not in workflow
    assert "vars.DEPLOY_ENABLED == 'true'" in workflow
    assert "actions: read" in workflow
    assert "Discover tested release identity" in workflow
    assert "tradingagent-release-[0-9a-f]{40}" in workflow
    assert "actions/download-artifact@v5" in workflow
    assert "run-id: ${{ env.DEPLOY_RUN_ID }}" in workflow
    assert "tar -xOf \"$archive\" ./.source-sha" in workflow
    assert "id: publish" in workflow
    assert workflow.count('gh api "repos/${GITHUB_REPOSITORY}/git/ref/heads/main"') == 2
    assert "Skip stale deployment" in workflow
    assert "Main advanced during upload" in workflow
    assert "sudo -n /usr/local/sbin/tradingagent-release" in workflow
    assert "if: steps.publish.outputs.published == 'true'" in workflow
    assert "deploy/release.sh" not in workflow
    assert "tradingagent-crypto-forty-symbol-observation.service" in workflow
    assert "20-forty-symbol-release.conf" in workflow
    assert "99-tradingagent-release.conf" in workflow
    assert "systemctl show -p WorkingDirectory --value" in workflow
    assert "systemctl show -p DropInPaths --value" in workflow
    assert "legacy forty-symbol release drop-in remains effective" in workflow
    assert "grep -Fq '$forty_unit' /usr/local/sbin/tradingagent-release" in workflow


def test_root_release_helper_enforces_immutable_cutover_and_rollback() -> None:
    helper = _read("deploy/release.sh")

    assert "[[ \"$EUID\" -eq 0 ]]" in helper
    assert "must run from $installed_path" in helper
    assert "arguments are not accepted" in helper
    assert "root:root" in helper
    assert "unsupported archive member type" in helper
    assert "validate_immutable_release()" in helper
    assert "validate_immutable_release \"$release_dir\"" in helper
    assert "refresh_installed_helper_from_release \"$release_dir\"" in helper
    assert "'deploy/release.sh'" in helper
    assert helper.index("refresh_installed_helper_from_release \"$release_dir\"") < helper.index(
        "prepare_g5_release_reconciliation\n"
    )
    assert helper.index(
        "refresh_installed_helper_from_release \"$release_dir\"\nprepare_g5_release_reconciliation\n"
    ) < helper.index('reconcile_g5_release_dropins "$release_dir"')
    assert "find \"$staging_dir\" -type d -exec chmod 0755" in helper
    assert "find \"$staging_dir\" -type f -perm /0111 -exec chmod 0555" in helper
    assert "find \"$staging_dir\" -type f ! -perm /0111 -exec chmod 0444" in helper
    assert "runuser -u tradingagent" in helper
    assert "systemctl restart \"$front_unit\"" in helper
    assert "health_url=http://127.0.0.1:8787/healthz" in helper
    assert "rolling current back" in helper
    assert "front API process is not running from the requested immutable release" in helper


def test_root_release_helper_reconciles_runtime_release_dropins_atomically() -> None:
    helper = _read("deploy/release.sh")

    for unit in (*CRYPTO_RUNTIME_RELEASE_UNITS, *ASHARE_RELEASE_UNITS):
        assert unit in helper
    assert (
        "/etc/systemd/system/tradingagent-crypto-forty-symbol-observation.service.d/"
        "20-forty-symbol-release.conf"
    ) in helper
    assert 'test -r "$root/Crypto/forty_symbol_observation_runtime.py"' in helper
    assert 'test -r "$root/deploy/release.sh"' in helper
    assert "refreshed installed helper from" in helper
    assert 'exec "$installed_path"' in helper
    for unit in ASHARE_RELEASE_UNITS:
        assert f"/etc/systemd/system/{unit}.d/20-ashare-release.conf" in helper
    assert 'release_units=("${g5_units[@]}" "${ashare_release_units[@]}")' in helper
    assert "20-ashare-release.conf" in helper
    assert "g5_dropin_name=99-tradingagent-release.conf" in helper
    assert "prepare_g5_release_reconciliation" in helper
    assert "reconcile_g5_release_dropins \"$release_dir\"" in helper
    assert "require_g5_unit_stopped" in helper
    assert '[[ "$state" == inactive ]]' in helper
    assert '[[ "$state" == failed ]]' in helper
    assert "MainPID" in helper
    assert "ControlPID" in helper
    assert '[[ "$main_pid" == 0 && "$control_pid" == 0 ]]' in helper
    assert "G5 failed unit still has a process" in helper
    assert "systemctl reset-failed" not in helper
    assert "validate_managed_g5_dropin" in helper
    assert "unsupported directive" in helper
    assert "systemctl daemon-reload" in helper
    assert "G5 unit WorkingDirectory did not reconcile" in helper
    assert "G5 unit PYTHONPATH did not reconcile" in helper
    assert "legacy G5 release drop-in remains effective" in helper
    assert "restore_g5_release_dropins" in helper
    assert helper.index("restore_g5_release_dropins") < helper.index(
        'systemctl restart "$front_unit"'
    )


def _release_transaction_fixture(
    tmp_path: Path, fault: str = "", fault_unit: str = ""
) -> tuple[subprocess.CompletedProcess[str], Path, dict[str, bytes], dict[str, str]]:
    """Run the actual reconciler/cutover/EXIT rollback against isolated unit files.

    Only systemd and privileged/platform-specific filesystem calls are stubbed;
    the managed-file parser, stopped guards, backup, writes and restore run.
    No installed helper, real systemd, credentials or runtime data are accessed.
    """
    helper = _read("deploy/release.sh")
    systemd = tmp_path / "systemd"
    release_root = tmp_path / "releases"
    for name in ("old", "new"):
        (release_root / name).mkdir(parents=True)
    (release_root / "new" / ".deployed-sha").write_text("new\n")
    (release_root / "current").symlink_to(release_root / "old")
    units = (*CRYPTO_RUNTIME_RELEASE_UNITS, *ASHARE_RELEASE_UNITS)
    timeout_values = ("1min 30s", "45min", "infinity")
    timeouts = {
        unit: timeout_values[index % len(timeout_values)]
        for index, unit in enumerate(units)
    }
    canonical_name = "99-tradingagent-release.conf"
    for index, unit in enumerate(units):
        unit_dir = systemd / f"{unit}.d"
        unit_dir.mkdir(parents=True)
        (unit_dir / "10-sim-only.conf").write_text(
            "[Service]\nEnvironment=REAL_TRADING_ENABLED=false\n"
        )
        (unit_dir / "90-timeout.conf").write_text(
            f"[Service]\nTimeoutStartSec={timeouts[unit]}\n"
        )
        if index % 2:
            (unit_dir / canonical_name).write_text(
                f"[Service]\nWorkingDirectory={release_root}/old\n"
                f"Environment=PYTHONPATH={release_root}/old\n"
                f"TimeoutStartSec={timeouts[unit]}\n"
            )
    crypto_legacy = re.search(r"g5_legacy_dropins=\(\n(.*?)\n\)", helper, re.S)
    assert crypto_legacy is not None
    legacy_paths = [
        path.removeprefix("/etc/systemd/system/")
        for path in crypto_legacy.group(1).split()
    ] + [f"{unit}.d/20-ashare-release.conf" for unit in ASHARE_RELEASE_UNITS]
    for path in legacy_paths:
        (systemd / path).write_text(
            f"[Service]\nWorkingDirectory={release_root}/old\n"
            f"Environment=PYTHONPATH={release_root}/old\nTimeoutStartSec=1s\n"
        )
    if fault == "unsafe-legacy":
        (systemd / f"{fault_unit}.d/20-ashare-release.conf").write_text(
            f"[Service]\nWorkingDirectory={release_root}/old\nExecStart=/bin/false\n"
        )
    before = {str(p.relative_to(systemd)): p.read_bytes() for p in systemd.rglob("*.conf")}
    definitions = helper[helper.index("g5_units=("):helper.index('[[ "$EUID"')]
    definitions = definitions.replace("/etc/systemd/system", str(systemd)).replace(
        "/var/tmp/tradingagent-g5-dropins.XXXXXX", str(tmp_path / "backup.XXXXXX")
    )
    rollback = helper[helper.index("rollback_release() {"):helper.index('cp -- "$archive"')]
    cutover = helper[
        helper.index("prepare_g5_release_reconciliation\n\nlink_tmp="):
        helper.index('\nif [[ "$front_state_before" == active ]]; then\n  systemctl restart')
    ]
    # All real writes are redirected into tmp_path. Unexpected systemctl calls fail.
    harness = r'''
set -euo pipefail
release_root="$TEST_ROOT/releases"
current_link="$release_root/current"
previous_release="$release_root/old"
release_dir="$release_root/new"
sha=new
front_state_before=inactive
g5_dropin_backup_dir=''
g5_dropins_changed=0
switched=0
committed=0
link_tmp=''
staging_dir=''
root_archive="$TEST_ROOT/unused-archive"
stat() {
  case "$2" in
    %U:%G) printf 'root:root\n' ;;
    %h) printf '1\n' ;;
    %a)
      if [[ "$FAULT" == unsafe-directory && "${@: -1}" == "$TEST_ROOT/systemd/$FAULT_UNIT.d" ]]; then
        printf '777\n'
      else printf '755\n'; fi ;;
    *) return 98 ;;
  esac
}
chown() { :; }
install() {
  [[ "$1 $2 $3 $4 $5 $6 $7" == '-D -o root -g root -m 0644' ]] || return 98
  mkdir -p -- "$(dirname -- "$9")"
  cp -- "$8" "$9"
  chmod 0644 "$9"
}
mv() {
  # os.replace supplies Linux mv -T semantics on macOS as well.
  python3 -c 'import os, sys; os.replace(sys.argv[-2], sys.argv[-1])' "$@"
}
readlink() {
  python3 -c 'import os, sys; print(os.path.realpath(sys.argv[-1]))' "$@"
}
systemctl() {
  printf '%s\n' "$*" >> "$TEST_ROOT/systemctl.log"
  local unit="${@: -1}" path
  case "$1" in
    cat)
      [[ "$FAULT" != absent || "$unit" != "$FAULT_UNIT" ]] ;;
    is-active)
      if [[ "$unit" == "$FAULT_UNIT" ]] && {
        [[ "$FAULT" == active-preflight ]] ||
        [[ "$FAULT" == active-cutover && "$g5_dropins_changed" == 1 ]];
      }; then printf 'active\n'; else printf 'inactive\n'; fi ;;
    daemon-reload) : ;;
    show)
      path="$TEST_ROOT/systemd/$unit.d/99-tradingagent-release.conf"
      case "$3" in
        TimeoutStartUSec)
          sed -n 's/^TimeoutStartSec=//p' "$TEST_ROOT/systemd/$unit.d/90-timeout.conf" ;;
        WorkingDirectory)
          if [[ "$FAULT" == readback && "$unit" == "$FAULT_UNIT" ]]; then
            printf '%s/old\n' "$release_root"
          else sed -n 's/^WorkingDirectory=//p' "$path"; fi ;;
        Environment) sed -n 's/^Environment=//p' "$path" ;;
        DropInPaths) printf '%s ' "$TEST_ROOT/systemd/$unit.d/"*.conf ;;
        *) return 98 ;;
      esac ;;
    *) return 98 ;;
  esac
}
'''
    completed = subprocess.run(
        ["bash", "-c", harness + definitions + rollback + cutover + "\ncommitted=1\n"],
        env={
            "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
            "TEST_ROOT": str(tmp_path),
            "FAULT": fault,
            "FAULT_UNIT": fault_unit,
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return completed, systemd, before, timeouts


def test_release_transaction_binds_all_four_ashare_units_and_preserves_overrides(
    tmp_path: Path,
) -> None:
    completed, systemd, before, timeouts = _release_transaction_fixture(tmp_path)
    assert completed.returncode == 0, completed.stderr
    release = tmp_path / "releases/new"
    assert (tmp_path / "releases/current").resolve() == release
    calls = (tmp_path / "systemctl.log").read_text().splitlines()
    assert calls.count("daemon-reload") == 1
    for unit, timeout in timeouts.items():
        unit_dir = systemd / f"{unit}.d"
        assert (unit_dir / "99-tradingagent-release.conf").read_text() == (
            f"[Service]\nWorkingDirectory={release}\nEnvironment=PYTHONPATH={release}\n"
            f"ReadOnlyPaths={release}\nTimeoutStartSec={timeout}\n"
        )
        assert f"cat {unit}" in calls
        assert calls.count(f"is-active {unit}") == 2
        for property_name in ("WorkingDirectory", "Environment", "DropInPaths"):
            assert f"show -p {property_name} --value {unit}" in calls
    for relative, content in before.items():
        path = systemd / relative
        if path.name in ("10-sim-only.conf", "90-timeout.conf"):
            assert path.read_bytes() == content
        elif path.name != "99-tradingagent-release.conf":
            assert not path.exists()
    assert not list(tmp_path.glob("backup.*"))


@pytest.mark.parametrize("unit", ASHARE_RELEASE_UNITS)
@pytest.mark.parametrize(
    "fault",
    ("readback", "active-cutover", "unsafe-directory", "absent", "active-preflight", "unsafe-legacy"),
)
def test_each_ashare_unit_participates_in_preflight_and_transaction_rollback(
    tmp_path: Path, unit: str, fault: str,
) -> None:
    completed, systemd, before, _ = _release_transaction_fixture(tmp_path, fault, unit)
    assert completed.returncode != 0
    assert (tmp_path / "releases/current").resolve() == tmp_path / "releases/old"
    after = {str(p.relative_to(systemd)): p.read_bytes() for p in systemd.rglob("*.conf")}
    assert after == before  # Includes G5, both old/absent canonical files and unrelated overrides.
    calls = (tmp_path / "systemctl.log").read_text().splitlines()
    if fault in ("readback", "active-cutover", "unsafe-directory"):
        assert "rolling current back" in completed.stderr
        # Unsafe directories interrupt canonical writes before the first reload.
        assert calls.count("daemon-reload") == (1 if fault == "unsafe-directory" else 2)
        assert unit in completed.stderr
    else:
        # Missing units are mandatory: fail before current or any drop-in changes.
        assert "rolling current back" not in completed.stderr
        assert "daemon-reload" not in calls
    if fault == "unsafe-legacy":
        assert "unsupported directive" in completed.stderr
    if fault == "unsafe-directory":
        assert "directory is group/other writable" in completed.stderr
    if fault == "absent":
        assert calls[-1] == f"cat {unit}"
    if fault == "active-preflight":
        assert f"must be stopped during release preflight: {unit}" in completed.stderr
    assert not list(tmp_path.glob("backup.*"))


def _refresh_helper_fixture(tmp_path: Path, *, source_text: str, installed_text: str) -> subprocess.CompletedProcess[str]:
    helper = _read("deploy/release.sh")
    match = re.search(
        r"refresh_installed_helper_from_release\(\) \{\n.*?\n\}\n",
        helper,
        flags=re.DOTALL,
    )
    assert match is not None
    installed = tmp_path / "tradingagent-release"
    release_dir = tmp_path / "release"
    source = release_dir / "deploy" / "release.sh"
    source.parent.mkdir(parents=True)
    installed.write_text(installed_text)
    source.write_text(source_text)
    installed.chmod(0o755)
    source.chmod(0o755)
    harness = f"""
set -euo pipefail
installed_path="{installed}"
release_units=({' '.join((*CRYPTO_RUNTIME_RELEASE_UNITS, *ASHARE_RELEASE_UNITS))})
fail() {{ printf '%s\\n' "$*" >&2; exit 97; }}
stat() {{
  case "$2" in
    %U:%G) printf 'root:root\\n' ;;
    %h) printf '1\\n' ;;
    %a) printf '755\\n' ;;
    *) return 98 ;;
  esac
}}
chown() {{ :; }}
exec() {{
  printf '%s\\n' "$*" > "{tmp_path}/reentered"
  exit 0
}}
{match.group(0)}
refresh_installed_helper_from_release "{release_dir}"
"""
    return subprocess.run(
        ["bash", "-c", harness],
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_release_helper_refresh_is_noop_when_installed_matches_release(
    tmp_path: Path,
) -> None:
    if shutil.which("sha256sum", path="/usr/bin:/bin") is None:
        pytest.skip("GNU sha256sum unavailable; full self-refresh runs in Linux CI")
    text = _managed_helper_source()
    completed = _refresh_helper_fixture(
        tmp_path, source_text=text, installed_text=text
    )
    assert completed.returncode == 0, completed.stderr
    assert not (tmp_path / "reentered").exists()
    assert (tmp_path / "tradingagent-release").read_text() == text


def test_release_helper_refresh_replaces_stale_helper_and_reenters(
    tmp_path: Path,
) -> None:
    if shutil.which("sha256sum", path="/usr/bin:/bin") is None:
        pytest.skip("GNU sha256sum unavailable; full self-refresh runs in Linux CI")
    source_text = _managed_helper_source() + "# canonical managed bindings\n"
    completed = _refresh_helper_fixture(
        tmp_path,
        source_text=source_text,
        installed_text="#!/bin/bash\n# stale host helper\n",
    )
    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "tradingagent-release").read_text() == source_text
    assert (tmp_path / "reentered").read_text() == (
        f"{tmp_path / 'tradingagent-release'}\n"
    )
    assert "refreshed installed helper from" in completed.stderr


def test_release_helper_refresh_rejects_source_missing_forty_symbol(
    tmp_path: Path,
) -> None:
    completed = _refresh_helper_fixture(
        tmp_path,
        source_text="#!/bin/bash\n# no forty-symbol unit\n",
        installed_text="#!/bin/bash\n# stale host helper\n",
    )
    assert completed.returncode == 97
    assert "missing forty-symbol observation reconciliation" in completed.stderr
    assert (tmp_path / "tradingagent-release").read_text() == (
        "#!/bin/bash\n# stale host helper\n"
    )
    assert not (tmp_path / "reentered").exists()


def test_forty_symbol_observation_participates_in_release_rollback(
    tmp_path: Path,
) -> None:
    unit = "tradingagent-crypto-forty-symbol-observation.service"
    completed, systemd, before, _ = _release_transaction_fixture(
        tmp_path, "readback", unit
    )

    assert completed.returncode != 0
    assert (tmp_path / "releases/current").resolve() == tmp_path / "releases/old"
    after = {str(p.relative_to(systemd)): p.read_bytes() for p in systemd.rglob("*.conf")}
    assert after == before
    assert "rolling current back" in completed.stderr
    assert unit in completed.stderr
    assert not list(tmp_path.glob("backup.*"))


def test_g5_release_cutover_accepts_only_stopped_units() -> None:
    helper = _read("deploy/release.sh")
    match = re.search(
        r"require_g5_unit_stopped\(\) \{\n.*?\n\}\n",
        helper,
        flags=re.DOTALL,
    )
    assert match is not None
    harness = f"""
set -eu
fail() {{ printf '%s\\n' "$*" >&2; exit 97; }}
systemctl() {{
  if [[ "$1" == is-active ]]; then
    printf '%s\\n' "$MOCK_STATE"
    return 0
  fi
  if [[ "$1" == show && "$2" == -p && "$4" == --value ]]; then
    case "$3" in
      MainPID) printf '%s\\n' "$MOCK_MAIN_PID" ;;
      ControlPID) printf '%s\\n' "$MOCK_CONTROL_PID" ;;
      *) return 98 ;;
    esac
    return 0
  fi
  return 98
}}
{match.group(0)}
require_g5_unit_stopped example.service test-cutover
"""

    for state, main_pid, control_pid, expected_code in (
        ("inactive", "123", "456", 0),
        ("failed", "0", "0", 0),
        ("failed", "123", "0", 97),
        ("failed", "0", "456", 97),
        ("failed", "invalid", "0", 97),
        ("active", "0", "0", 97),
        ("activating", "0", "0", 97),
        ("deactivating", "0", "0", 97),
    ):
        completed = subprocess.run(
            ["bash", "-c", harness],
            env={
                "PATH": "/usr/bin:/bin",
                "MOCK_STATE": state,
                "MOCK_MAIN_PID": main_pid,
                "MOCK_CONTROL_PID": control_pid,
            },
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == expected_code, (
            state,
            main_pid,
            control_pid,
            completed.stderr,
        )


def test_release_helper_preserves_systemd_compound_timeout_values() -> None:
    helper = _read("deploy/release.sh")
    expected = (
        '[[ "$timeout" =~ '
        '^([0-9]+(us|ms|s|min|h|d|w|month|y)'
        '([[:space:]]+[0-9]+(us|ms|s|min|h|d|w|month|y))*)$|^infinity$ ]]'
    )
    assert expected in helper

    harness = """
timeout="$1"
[[ "$timeout" =~ ^([0-9]+(us|ms|s|min|h|d|w|month|y)([[:space:]]+[0-9]+(us|ms|s|min|h|d|w|month|y))*)$|^infinity$ ]]
"""
    for value, expected_code in (
        ("1min 30s", 0),
        ("45min", 0),
        ("infinity", 0),
        ("1min ; rm -rf /", 1),
        ("", 1),
    ):
        completed = subprocess.run(
            ["bash", "-c", harness, "--", value],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == expected_code, (value, completed.stderr)


def test_release_helper_accepts_systemd_normalized_compound_timeout_values() -> None:
    helper = _read("deploy/release.sh")

    # `systemctl show` normalizes a compound duration such as 90 seconds to
    # "1min 30s".  The release helper writes that value into the managed
    # drop-in, so its validation must accept the same safe grammar.
    assert (
        'r"(?:[0-9]+(?:us|ms|s|min|h|d|w|month|y)?)(?:\\s+[0-9]+'
        '(?:us|ms|s|min|h|d|w|month|y)?)*|infinity"'
    ) in helper


def test_server_bootstrap_grants_only_the_fixed_release_helper() -> None:
    bootstrap = _read("deploy/bootstrap-production-server.sh")

    assert "installed_helper=/usr/local/sbin/tradingagent-release" in bootstrap
    assert "spool=/var/tmp/tradingagent-deploy" in bootstrap
    assert "NOPASSWD: %s" in bootstrap
    assert "visudo -cf" in bootstrap
    assert "release root must already be root:root" in bootstrap
    assert "current must already be an immutable-release symlink" in bootstrap
    assert "deployment spool is not empty" in bootstrap


def test_ten_symbol_release_reconciliation_includes_legacy_pins_not_scrub_policy():
    helper = _read("deploy/release.sh")
    for unit, legacy in (
        ("tradingagent-crypto-ten-symbol-observation.service", "20-ten-symbol-release.conf"),
        ("tradingagent-crypto-ten-symbol-factor-research.service", "20-ten-symbol-factor-release.conf"),
        ("tradingagent-crypto-ten-symbol-factor-research-scrub.service", "20-ten-symbol-factor-release.conf"),
    ):
        assert f"/etc/systemd/system/{unit}.d/{legacy}" in helper
    # The independently configured scrub timeout is not a release pin to delete.
    assert "10-scrub-timeout.conf" not in helper
    assert 'test -r "$root/Crypto/ten_symbol_observation_runtime.py"' in helper
    assert 'test -r "$root/Crypto/ten_symbol_factor_research_worker.py"' in helper


def _managed_helper_source(*, omit=()):
    groups = (("g5_units", CRYPTO_RUNTIME_RELEASE_UNITS),
              ("ashare_release_units", ASHARE_RELEASE_UNITS))
    text = "#!/bin/bash\n"
    for name, units in groups:
        text += name + "=(\n"
        text += "".join("  " + unit + "\n" for unit in units if unit not in omit)
        text += ")\n"
    return text + 'release_units=("${g5_units[@]}" "${ashare_release_units[@]}")\n'


def _run_managed_coverage_parser(tmp_path, source_text):
    helper = _read("deploy/release.sh")
    body = helper.split("from pathlib import Path", 1)[1].split("\nCOVERAGE\n", 1)[0]
    body = "from pathlib import Path" + body
    source = tmp_path / "source.sh"
    source.write_text(source_text)
    return subprocess.run([sys.executable, "-", str(source),
                           *CRYPTO_RUNTIME_RELEASE_UNITS, *ASHARE_RELEASE_UNITS],
                          input=body, capture_output=True, text=True, timeout=10)


@pytest.mark.parametrize("actual_helper", [False, True])
def test_managed_coverage_parser_accepts_complete_arrays(tmp_path, actual_helper):
    text = _read("deploy/release.sh") if actual_helper else _managed_helper_source()
    result = _run_managed_coverage_parser(tmp_path, text)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("variant", ["missing", "comments", "dynamic", "unreferenced", "comment_union", "missing_union", "duplicate"])
def test_managed_coverage_parser_rejects_lost_or_ambiguous_coverage(tmp_path, variant):
    unit = "tradingagent-crypto-ten-symbol-observation.service"
    text = _managed_helper_source(omit=(unit,))
    if variant == "comments":
        text = text.replace("g5_units=(\n", "g5_units=(\n  # " + unit + "\n")
        text += "# " + unit + "\n"
    elif variant == "dynamic":
        text = text.replace("g5_units=(\n", 'g5_units=(\n  "${EXTRA_UNIT}"\n')
    elif variant == "unreferenced":
        text = _managed_helper_source().replace(' "${ashare_release_units[@]}"', '')
    elif variant in {"comment_union", "missing_union"}:
        text = _managed_helper_source()
        union = 'release_units=("${g5_units[@]}" "${ashare_release_units[@]}")'
        text = text.replace(union, "# " + union if variant == "comment_union" else "")
    elif variant == "duplicate":
        text += "g5_units=(\n  " + unit + "\n)\n"
    result = _run_managed_coverage_parser(tmp_path, text)
    assert result.returncode != 0


def test_refresh_rejects_old_helper_before_replacement_or_reentry(tmp_path):
    old = "#!/bin/bash\n# existing coordinator\n"
    omitted = tuple(unit for unit in CRYPTO_RUNTIME_RELEASE_UNITS if "ten-symbol" in unit)
    source = _managed_helper_source(omit=omitted)
    source += "".join("# historical unit " + unit + "\n" for unit in omitted)
    completed = _refresh_helper_fixture(tmp_path, source_text=source, installed_text=old)
    assert completed.returncode == 97, completed.stderr
    assert "would drop managed unit coverage" in completed.stderr
    assert (tmp_path / "tradingagent-release").read_text() == old
    assert not (tmp_path / "reentered").exists()
    helper = _read("deploy/release.sh")
    assert helper.index("\nCOVERAGE\n") < helper.index('source_digest="$(sha256sum')
    assert helper.index('refresh_installed_helper_from_release "$release_dir"\n') < helper.index('prepare_g5_release_reconciliation\n')
