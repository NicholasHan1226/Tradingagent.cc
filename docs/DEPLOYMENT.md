# Production deployment

## Purpose

Production deployment is repository-driven, commit-pinned, and artifact-pinned. A merge to `main` does not directly mutate the server. The exact `main` commit must first complete the `TradingAgent Tests` workflow successfully. The deployment workflow then downloads the release artifact produced by that same successful workflow run.

The deployment workflow is intentionally disabled until the one-time server bootstrap is complete, the `production` GitHub Environment contains the required SSH secrets, and the repository variable `DEPLOY_ENABLED` is set to `true`.

This mechanism changes code releases only. It does not grant live-trading authority, alter `/etc/tradingagent` application secrets, enable timers, change broker configuration, or prune historical releases.

## Tested release artifact

On a `push` to `main`, the existing frontend CI job still performs:

1. `npm ci`;
2. frontend tests;
3. lint;
4. `npm run build:all`.

After those steps pass, the job creates a release archive containing:

- the exact Git tree for `GITHUB_SHA`;
- `front/dist` built from that SHA;
- `front/dist-server` built from that SHA;
- `.source-sha` containing the full Git SHA;
- a SHA-256 checksum file for the archive.

The artifact is named:

```text
tradingagent-release-<40-char-git-sha>
```

The production workflow runs only after the entire `TradingAgent Tests` workflow succeeds, so both the Python test job and the frontend job must be green. It downloads the artifact from `github.event.workflow_run.id`, verifies the checksum and `.source-sha`, and never substitutes a newer `main` checkout or a newly rebuilt frontend.

## Immutable server release model

The production release root is:

```text
/opt/investment/releases/tradingagent/
```

Each deployed Git commit receives an immutable directory:

```text
/opt/investment/releases/tradingagent/<40-char-git-sha>/
```

The active release is selected by:

```text
/opt/investment/releases/tradingagent/current
```

The root-owned release helper normalizes a new release before cutover:

- owner/group: `root:root`;
- directories: `0755`;
- non-executable files: `0444`;
- executable files: `0555`, preserving whether the packaged Git/archive file was executable;
- no group/other writable release member;
- only regular files and directories are accepted from the deployment archive;
- absolute paths, `..`, symlinks, hardlinks, devices and other special archive members are rejected.

The helper records:

```text
.deployed-sha
.release-package-sha256
```

An existing SHA-named release is reused only when both metadata values match. A different package may not overwrite an existing immutable release for the same Git SHA.

## Root trust boundary

GitHub Actions does **not** upload and execute a new privileged deployment script on every release.

The repository file:

```text
deploy/release.sh
```

is a source copy of the privileged helper. During one-time server bootstrap it is installed as:

```text
/usr/local/sbin/tradingagent-release
```

with `root:root` ownership and no group/other write permission. Normal production deployments invoke only that already-installed helper through a narrow `sudo` rule.

The helper:

- accepts no command-line arguments;
- reads a fixed request file from `/var/tmp/tradingagent-deploy/request`;
- requires the spool and uploaded archive/request to belong to the non-root sudo caller;
- copies the archive to a root-owned temporary file before validation/extraction;
- validates the requested SHA, checksum, archive member types and release shape;
- requires an existing valid `current` immutable-release symlink before automated cutover.

This prevents the SSH deployment account from obtaining unrestricted root shell access or replacing the privileged helper during an ordinary deployment.

## One-time server bootstrap

A dedicated SSH deployment user must already exist. It must not be `root`, and it needs an SSH-capable shell because the workflow uses `scp` and `ssh`.

From a trusted checkout of the approved bootstrap commit, run as root:

```bash
sudo ./deploy/bootstrap-production-server.sh <deploy-user>
```

The bootstrap script requires the existing release root and `current` symlink to be valid before it changes anything. It then:

1. creates `/var/tmp/tradingagent-deploy` as `0700` owned by the deployment user;
2. installs `deploy/release.sh` as root-owned `/usr/local/sbin/tradingagent-release`;
3. installs a sudoers entry allowing that deployment user to invoke the fixed helper;
4. validates the sudoers file with `visudo`;
5. leaves `DEPLOY_ENABLED` unchanged.

The bootstrap script does not create SSH keys, does not modify application secrets, does not enable services/timers and does not activate deployment by itself.

## GitHub production environment

Create a GitHub Environment named:

```text
production
```

Configure these **Environment Secrets**:

- `DEPLOY_HOST` — production server hostname or IP address;
- `DEPLOY_USER` — the same dedicated non-root user passed to the server bootstrap;
- `DEPLOY_SSH_KEY` — private SSH key for that deployment account;
- `DEPLOY_KNOWN_HOSTS` — pinned trusted SSH host-key entry for the production server.

Host-key checking remains strict. Do not set `StrictHostKeyChecking=no`.

Configure these **repository-level Actions variables**:

- `DEPLOY_ENABLED` — keep absent or `false` until server bootstrap and secrets are complete;
- `DEPLOY_PORT` — optional SSH port, default `22`.

`DEPLOY_ENABLED` must be repository-level because it is evaluated by the job-level `if:` gate before the job is sent to a runner. Environment-level configuration variables are not suitable for that pre-run gate.

## Production gate

A production deployment runs only when all of the following are true:

1. `TradingAgent Tests` completed successfully;
2. the successful run was triggered by a `push`;
3. the tested branch was `main`;
4. the repository variable `DEPLOY_ENABLED` is exactly `true`;
5. the `production` Environment secrets are available;
6. the tested release artifact for the exact workflow-run SHA exists;
7. its SHA-256 checksum is valid;
8. its `.source-sha` exactly equals the successful workflow-run head SHA;
9. SSH host-key verification succeeds;
10. the pre-installed root-owned release helper and fixed deployment spool exist on the server.

## Cutover and application health

Before changing `current`, the privileged helper reads the real state of:

```text
tradingagent-front-api.service
http://127.0.0.1:8787/healthz
```

Two preflight states are accepted:

- `active`: the endpoint must already be healthy before cutover;
- `inactive + disabled`: deployment may update `current`, but must not start the deliberately disabled frontend.

Other ambiguous or failed service states abort the deployment before cutover.

For an active frontend, deployment performs:

1. prepare and validate the new root-owned immutable release;
2. atomically switch `current`;
3. restart **only** `tradingagent-front-api.service`;
4. retry `127.0.0.1:8787/healthz` for a bounded period;
5. verify the new service `MainPID` has its working directory inside the requested immutable release.

There is no wildcard `systemctl restart tradingagent-*`.

Timer/oneshot units that already resolve code through `current` use the new release on their next invocation; this deployment bootstrap does not broadly restart or enable them.

## Automatic rollback

The previous `current` target is captured before cutover.

If any failure occurs after `current` has switched but before the new release is committed healthy, the same privileged deployment action:

1. atomically restores `current` to the previous immutable release;
2. if the frontend was active before deployment, restarts `tradingagent-front-api.service` against the previous release;
3. performs a bounded health check of the restored frontend;
4. exits non-zero so GitHub records deployment failure.

Historical immutable release directories are not automatically deleted.

## Verification

After the server helper returns success, GitHub Actions independently reads:

```text
/opt/investment/releases/tradingagent/current/.deployed-sha
```

and requires it to equal the successful CI workflow-run SHA.

Repository tests also lock the main deployment trust boundaries: workflow trigger, artifact linkage, root-helper invocation, immutable modes, bounded health check and rollback behavior.

## Change control

Changes under `.github/workflows/`, `deploy/release.sh`, `deploy/bootstrap-production-server.sh`, or the deployment trust boundary are infrastructure bootstrap changes. The existing automerge workflow excludes `.github/workflows/` changes, so this initial deployment workflow cannot self-bootstrap through the ordinary agent automerge path.

Future ordinary application PRs may continue through the normal CI/automerge path; production deployment remains separately gated by the exact successful `main` workflow run and `DEPLOY_ENABLED`.
