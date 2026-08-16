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


def test_main_ci_publishes_the_tested_release_artifact() -> None:
    workflow = _read(".github/workflows/test.yml")

    assert "npm run build:all" in workflow
    assert "Package tested release" in workflow
    assert "git -C \"$GITHUB_WORKSPACE\" archive \"$GITHUB_SHA\"" in workflow
    assert "front/dist-server/server/tradingAgentSnapshotHttp.js" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "name: tradingagent-release-${{ github.sha }}" in workflow
    assert "github.event_name == 'push' && github.ref == 'refs/heads/main'" in workflow


def test_deploy_workflow_is_exact_sha_and_artifact_gated() -> None:
    workflow = _read(".github/workflows/deploy-production.yml")

    assert "workflow_run:" in workflow
    assert "- TradingAgent Tests" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "github.event.workflow_run.event == 'push'" in workflow
    assert "github.event.workflow_run.head_branch == 'main'" in workflow
    assert "vars.DEPLOY_ENABLED == 'true'" in workflow
    assert "actions: read" in workflow
    assert "actions/download-artifact@v5" in workflow
    assert "run-id: ${{ github.event.workflow_run.id }}" in workflow
    assert "ref: ${{ github.event.workflow_run.head_sha }}" in workflow
    assert "tar -xOf \"$archive\" ./.source-sha" in workflow
    assert "sudo -n /usr/local/sbin/tradingagent-release" in workflow
    assert "deploy/release.sh" not in workflow


def test_root_release_helper_enforces_immutable_cutover_and_rollback() -> None:
    helper = _read("deploy/release.sh")

    assert "[[ \"$EUID\" -eq 0 ]]" in helper
    assert "must run from $installed_path" in helper
    assert "arguments are not accepted" in helper
    assert "root:root" in helper
    assert "unsupported archive member type" in helper
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
