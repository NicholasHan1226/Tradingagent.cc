from __future__ import annotations

from pathlib import Path
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


def test_automerge_is_limited_to_explicit_current_base_m0_docs_and_tests() -> None:
    workflow = _read(".github/workflows/automerge.yml")

    assert "Automerge Eligible M0 PRs" in workflow
    assert "M0_AUTOMERGE_LABEL: automerge-m0" in workflow
    assert "TRUSTED_PR_AUTHOR: NicholasHan1226" in workflow
    assert "M0 automerge is limited to docs and tests." in workflow
    assert "M0 candidate must be tested on the current main base." in workflow
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


def test_server_bootstrap_grants_only_the_fixed_release_helper() -> None:
    bootstrap = _read("deploy/bootstrap-production-server.sh")

    assert "installed_helper=/usr/local/sbin/tradingagent-release" in bootstrap
    assert "spool=/var/tmp/tradingagent-deploy" in bootstrap
    assert "NOPASSWD: %s" in bootstrap
    assert "visudo -cf" in bootstrap
    assert "release root must already be root:root" in bootstrap
    assert "current must already be an immutable-release symlink" in bootstrap
    assert "deployment spool is not empty" in bootstrap
