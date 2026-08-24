from __future__ import annotations

from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


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


def test_root_release_helper_enforces_immutable_cutover_and_rollback() -> None:
    helper = _read("deploy/release.sh")

    assert "[[ \"$EUID\" -eq 0 ]]" in helper
    assert "must run from $installed_path" in helper
    assert "arguments are not accepted" in helper
    assert "root:root" in helper
    assert "unsupported archive member type" in helper
    assert "validate_immutable_release()" in helper
    assert "validate_immutable_release \"$release_dir\"" in helper
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

    for unit in (
        "tradingagent-crypto-round-trip-g5-acceptance.service",
        "tradingagent-crypto-round-trip-g5-delayed-paper.service",
        "tradingagent-crypto-round-trip-g5-health.service",
        "tradingagent-crypto-round-trip-g5-learning.service",
        "tradingagent-crypto-round-trip-g5-learning-scrub.service",
        "tradingagent-ashare-minute-paper.service",
    ):
        assert unit in helper
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


def test_server_bootstrap_grants_only_the_fixed_release_helper() -> None:
    bootstrap = _read("deploy/bootstrap-production-server.sh")

    assert "installed_helper=/usr/local/sbin/tradingagent-release" in bootstrap
    assert "spool=/var/tmp/tradingagent-deploy" in bootstrap
    assert "NOPASSWD: %s" in bootstrap
    assert "visudo -cf" in bootstrap
    assert "release root must already be root:root" in bootstrap
    assert "current must already be an immutable-release symlink" in bootstrap
    assert "deployment spool is not empty" in bootstrap
