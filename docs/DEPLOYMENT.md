# Production deployment

## Purpose

Production deployment is repository-driven and commit-pinned. A merge to `main` does not directly mutate the server. The exact `main` commit must first complete the `TradingAgent Tests` workflow successfully. Only then can `Deploy TradingAgent Production` publish that tested SHA.

The deployment workflow is intentionally disabled until the `production` GitHub Environment is configured and the repository variable `DEPLOY_ENABLED` is set to `true`.

## Release model

The server release root is:

```text
/opt/investment/releases/tradingagent/
```

Each deployed Git commit receives an immutable directory:

```text
/opt/investment/releases/tradingagent/<40-char-git-sha>/
```

The active release is selected by the symlink:

```text
/opt/investment/releases/tradingagent/current
```

`deploy/release.sh` extracts a new release into a staging directory, validates the minimum repository shape, moves it into the SHA-named release directory, and atomically switches `current`. Existing releases are not pruned so rollback remains possible.

The script does **not** install dependencies, alter `/etc/tradingagent`, enable timers, grant live-trading authority, or broadly restart systemd units. Existing units that resolve code through `/opt/investment/releases/tradingagent/current` will use the new release on their next invocation. Any future long-running service restart policy must be added explicitly and reviewed separately.

## GitHub production environment

Create a GitHub Environment named `production` and configure these environment secrets:

- `DEPLOY_HOST` — production server hostname or IP address.
- `DEPLOY_USER` — dedicated unprivileged deployment account.
- `DEPLOY_SSH_KEY` — private key used only by that deployment account.
- `DEPLOY_KNOWN_HOSTS` — trusted `known_hosts` entry for the production SSH server. Do not disable host-key checking.

Configure these **repository variables** under Actions variables:

- `DEPLOY_ENABLED` — keep absent or `false` during bootstrap; set exactly to `true` only when server preparation is complete.
- `DEPLOY_PORT` — optional SSH port; defaults to `22`.

`DEPLOY_ENABLED` must be a repository-level variable because it is evaluated in the job-level `if:` gate before the job is sent to a runner. Environment-level configuration variables are not suitable for this pre-run gate.

Secrets must not be committed to this repository, copied into documentation, or sent through application configuration.

## Server prerequisites

The deployment account must be able to:

1. connect over SSH using the configured key;
2. create directories below `/opt/investment/releases/tradingagent`;
3. atomically replace `/opt/investment/releases/tradingagent/current`;
4. create and remove its own temporary files under `/tmp`.

It should not be `root`. Grant only the filesystem permissions required for the release root. The workflow does not require unrestricted `sudo`.

Existing application secrets and state remain outside the release tree, including `/etc/tradingagent`, `/var/lib/tradingagent`, and `/var/log/tradingagent`.

## Deployment gate

A production deployment runs only when all of the following are true:

1. `TradingAgent Tests` completed successfully;
2. the successful run was a `push` run;
3. the tested branch was `main`;
4. the repository variable `DEPLOY_ENABLED` is exactly `true`;
5. the `production` Environment secrets are present;
6. SSH host-key verification succeeds.

The workflow checks out and archives `github.event.workflow_run.head_sha`, so the deployed tree is the exact commit that passed CI rather than whatever happens to be at `main` later.

## Verification

Each release records its Git SHA in:

```text
/opt/investment/releases/tradingagent/<sha>/.deployed-sha
```

After the atomic switch, the workflow reads:

```text
/opt/investment/releases/tradingagent/current/.deployed-sha
```

and fails unless it exactly matches the tested commit SHA.

This is a release-integrity check, not an application-level health check. The repository already exposes the loopback-only read API health route at `http://127.0.0.1:8787/healthz`; wiring service restart plus this application-level health check is a separate follow-up because the current bootstrap intentionally grants no systemd restart permission.

## Rollback

Rollback is intentionally explicit. Select a previously deployed SHA and atomically repoint `current` to that release on the server, then perform any service-specific restart/reload required by the relevant runtime contract.

Example shape (run by an authorized operator on the server):

```bash
previous_sha=<known-good-40-char-sha>
release_root=/opt/investment/releases/tradingagent
ln -s "$release_root/$previous_sha" "$release_root/.rollback-current"
mv -Tf "$release_root/.rollback-current" "$release_root/current"
```

Do not delete historical release directories until a separate retention policy is approved.

## Change control

Changes under `.github/workflows/` or to deployment trust boundaries are infrastructure changes. They must not be self-approved by the coding agent that authored them. Deployment permissions, production secrets, systemd enablement, real-trading authority, and rollback policy remain separate concerns.
