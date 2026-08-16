# Autonomous Development Workflow

This repository uses an agent-first, machine-gated workflow. Routine human code review or approval is not a merge requirement.

## Normal workflow

1. Start from the latest `main` and record the exact base SHA used for the task.
2. Use one task per branch and pull request. Prefer `agent/<agent>/<task>`, `feat/<task>`, `fix/<task>`, `refactor/<task>`, or `chore/<task>`.
3. Do not push routine code directly to `main`. Do not force-push or rewrite shared history.
4. Run relevant deterministic local validation before opening or updating the pull request.
5. Open a pull request to `main`. Human review is not required.
6. Pull-request CI is the routine Git merge gate. A failing, missing, cancelled, or stale CI result must not auto-merge.
7. A successful CI run may be squash-merged automatically only when the tested SHA is still the current PR head and the PR comes from a trusted same-repository branch.
8. Main advancing after CI does **not** automatically serialize every agent. If the PR touches only disjoint market/local files and none of its files were changed on `main` since the recorded base, it may merge against current `main` without a forced branch update. If files overlap, or the PR touches shared/governance/deployment authority, the branch must be refreshed and CI rerun.
9. Fork PRs and untrusted authors never auto-merge.
10. Keep candidate, GitHub `main`, server release, effective runtime, data readback, and live-trading authority as separate evidence layers. Passing GitHub CI does not by itself authorize production or real trading.
11. Do not put secrets, credentials, databases, runtime state, logs, broker material, or production artifacts in Git.

## Fresh-base authority paths

These paths are deliberately conservative and require a current-`main` integration check whenever `main` advanced after the PR base:

- `.github/**`
- root `AGENTS.md` and `CONTRIBUTING.md`
- `deploy/**`
- `shared/**`
- dependency roots such as `requirements*`, `pyproject.toml`, and frontend package manifests
- `docs/EVOLUTION_PROGRAM.md` and `docs/operations.md`

A market-scoped PR can still require a fresh base when another merged change touched the same file. File overlap always wins over directory ownership.

## Workflow-governance changes

Changes under `.github/workflows/` must not be self-authorizing. They require a separate trusted controller/machine-governance check before merge; this does not create a routine human-review requirement. A normal application-code PR may not weaken, remove, or replace its own CI/automerge gate.

Repository-side branch/ruleset protection is defense in depth, not something workflow files can prove by themselves. Do not claim `main` is protected unless a fresh GitHub settings/API readback confirms it. If protection is absent, report that as governance debt; do not silently reinterpret workflow convention as branch protection.

If GitHub Actions is temporarily unavailable, leave the affected PR unmerged rather than bypassing `main`. A future independent fallback runner may be added explicitly, but absence of the configured merge gate is not permission to direct-push routine code.

## Post-merge and deployment

Every merge creates a new `main` SHA. Production deployment must depend on successful validation of that exact current `main` SHA and must re-check that it is still current before cutover. This post-merge exact-SHA gate is what prevents a clean but semantically incompatible parallel merge from being deployed silently.

## Authority boundary

Autonomous code merge/deployment does not grant authority to enable real trading, expand risk automatically, create/change credentials/accounts/permissions, expose a public entry point, or perform destructive data/database operations. Those boundaries remain governed by `AGENTS.md` and `docs/EVOLUTION_PROGRAM.md`.
