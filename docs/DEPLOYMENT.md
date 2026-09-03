# Production deployment

## Purpose

Production deployment is Controller-authorized, repository-driven, commit-pinned, and artifact-pinned. A merge to `main` does not directly mutate the server. The Finance Delivery Controller must first accept the immutable candidate head and perform the merge. The exact merged `main` commit must then complete the `TradingAgent Tests` workflow successfully. Only then may the Controller explicitly request deployment with that exact successful test-run ID; the deployment workflow downloads the artifact produced by that same run.

The deployment workflow is intentionally disabled until the one-time server bootstrap is complete, the `production` GitHub Environment contains the required SSH secrets, and the repository variable `DEPLOY_ENABLED` is set to `true`.

This mechanism changes code releases only. It does not grant live-trading authority, alter `/etc/tradingagent` application secrets, enable timers, change broker configuration, or prune historical releases.

## Tested release artifact

On every PR and `push` to `main`, the existing frontend CI job performs:

1. `npm ci`;
2. frontend tests;
3. lint;
4. `npm run build:all`;
5. release packaging validation.

The packaging step creates a release archive containing:

- the exact Git tree for `GITHUB_SHA`;
- `front/dist` built from that SHA;
- `front/dist-server` built from that SHA;
- `.source-sha` containing the full Git SHA;
- a SHA-256 checksum file for the archive.

PR runs validate that the release can actually be packaged. Only a successful `push` run on `main` uploads the artifact for production use.

CI coalesces superseded PR and branch runs, but a manual `workflow_dispatch`
verification is isolated by its required exact SHA. It therefore cannot cancel
the current-`main` push run that produces the only deployable artifact. Python
dependency caching may shorten setup time; a cache miss still installs the same
declared `requirements.txt` dependencies and does not relax any test.

The artifact is named:

```text
tradingagent-release-<40-char-git-sha>
```

The production workflow runs only after an explicit `controller-accepted-deploy` repository dispatch initiated by the Controller's GitHub identity. It validates that the supplied test-run ID is a successful `TradingAgent Tests` **push** run for the supplied current-`main` SHA, then downloads the artifact from that exact run, verifies the checksum and `.source-sha`, and never substitutes a newer checkout or a newly rebuilt frontend.

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
- executable files: `0555`, preserving whether the packaged file was executable;
- no group/other writable release member;
- only regular files and directories are accepted from the deployment archive;
- absolute paths, `..`, symlinks, hardlinks, devices and other special archive members are rejected.

The helper records:

```text
.deployed-sha
.release-package-sha256
```

An existing SHA-named release is reused only when both metadata values match **and** the full existing release tree still satisfies the same root ownership, immutable modes and `tradingagent` service-readability checks. A different package may not overwrite an existing immutable release for the same Git SHA.

## Root trust boundary

GitHub Actions does **not** upload a separate privileged deployment script on every release.

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
- requires an existing valid `current` immutable-release symlink before automated cutover;
- after the immutable release is on disk, replaces itself only when `deploy/release.sh` from that same validated release differs, then re-enters so this controller-accepted-deploy applies that SHA's unit bindings.

The SSH deployment account still cannot replace the helper or obtain an unrestricted root shell. The only allowed self-refresh source is the already-validated immutable release for the requested SHA. A stale installed helper that lacks this refresh (or the forty-symbol binding) cannot silently succeed: GitHub verify requires the forty-symbol observer pin to equal the deployed SHA.

The first delivery of helper refresh still requires one controlled reinstall of `/usr/local/sbin/tradingagent-release` from the accepted release. After that, ordinary `controller-accepted-deploy` keeps the helper and the forty-symbol pin on the same SHA.

## One-time server bootstrap

A dedicated SSH deployment user must already exist. It must not be `root`, and it needs an SSH-capable shell because the workflow uses `scp` and `ssh`.

From a trusted checkout of the approved bootstrap commit, run as root:

```bash
sudo ./deploy/bootstrap-production-server.sh <deploy-user>
```

The bootstrap script requires the existing release root and `current` symlink to be valid before it changes anything. It also refuses to proceed if the deployment spool is nonempty, so an old or partial deployment request is never silently deleted during bootstrap.

It then:

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

1. the Finance Delivery Controller has accepted the immutable candidate head, merged it, and issued a `controller-accepted-deploy` request from its GitHub identity containing the SHA and exact main test-run ID;
2. `TradingAgent Tests` completed successfully for that test-run ID;
3. the successful run was triggered by a `push` and tested branch was `main`;
4. the test-run SHA, artifact name, and artifact `.source-sha` all equal the Controller-requested SHA;
5. the repository variable `DEPLOY_ENABLED` is exactly `true`;
6. the `production` Environment secrets are available;
7. the tested release artifact for that exact run exists and its SHA-256 checksum is valid;
8. the tested SHA is still the current `main` HEAD immediately before upload;
9. the tested SHA is still the current `main` HEAD immediately before privileged cutover;
10. SSH host-key verification succeeds;
11. the pre-installed root-owned release helper and fixed deployment spool exist on the server.

### Stale-main protection

Multiple coding agents may merge to `main` in quick succession. A slower CI run for an older commit must therefore never deploy after a newer commit has already become `main`.

The production workflow queries the current GitHub `main` ref twice:

- before uploading a release;
- again after upload but before invoking the root helper.

If the tested SHA is no longer current, the deployment is skipped without changing `current`. If `main` advances during upload, only the fixed `.incoming` files for that attempted upload are removed and no privileged cutover occurs. This prevents queued successful runs from rolling production backward to an older `main` commit.

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
2. verifies that `current` resolves back to that previous release;
3. if the frontend was active before deployment, restarts `tradingagent-front-api.service` against the previous release;
4. performs a bounded health check of the restored frontend;
5. exits non-zero so GitHub records deployment failure.

Historical immutable release directories are not automatically deleted.

## Verification

After the server helper returns success, GitHub Actions independently reads:

```text
/opt/investment/releases/tradingagent/current/.deployed-sha
```

and requires it to equal the successful CI workflow-run SHA. It also reads the effective `WorkingDirectory` and `DropInPaths` of `tradingagent-crypto-forty-symbol-observation.service` and requires the unit to be pinned to that same SHA through `99-tradingagent-release.conf`, with `20-forty-symbol-release.conf` no longer effective. The installed helper must contain that unit name. A current-symlink update alone is not enough.

Repository tests also lock the main deployment trust boundaries: workflow trigger, exact artifact linkage, stale-main prevention, root-helper invocation, immutable modes, immutable-release reuse checks, bounded health check and rollback behavior.

## Change control

Changes under `.github/workflows/`, `deploy/release.sh`, `deploy/bootstrap-production-server.sh`, or the deployment trust boundary are M1 infrastructure changes. They require a fresh Controller review of the immutable candidate head, a Controller merge, and exact-main test evidence.

CI is candidate evidence, not production-deployment authority. The narrowly defined `automerge-m0` path may merge only a trusted, current-main-base PR that changes only `docs/**`, `tests/**`, or Markdown files; it then dispatches exact-main validation and cannot deploy. Everything outside that path—including all business, shared, workflow and deployment changes—remains Controller-merged M1. For each accepted M1 merge, the Controller independently records the merge commit and exact successful main test run before issuing the narrowly scoped deployment dispatch; afterward it reads back GitHub, server source, immutable effective release, and runtime separately. High-authority actions, including real trading, capital, accounts, secrets, and public exposure, remain outside this workflow.

## Push-CI recursion caveat

A merge performed by a workflow using the built-in `GITHUB_TOKEN` does not
trigger a `push`-event run on `main`. When a deployable push run is required,
the merge must be performed with user credentials (for example the controller
squash-merge path); otherwise `main` has no fresh push-event artifact and the
production gate cannot be satisfied for that head.
