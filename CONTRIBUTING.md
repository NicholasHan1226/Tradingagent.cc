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
8. Main advancing after CI does **not** automatically serialize every agent. Freshness is determined by exact file overlap and authority domain:
   - exact file overlap always requires refresh and CI rerun;
   - shared core/deployment/dependency changes require a fresh base after any `main` advance;
   - governance/workflow/documentation changes require refresh when `main` also changed governance/core authority since the frozen base;
   - disjoint market-local changes may remain parallel.
9. Fork PRs and untrusted authors never auto-merge.
10. Keep candidate, GitHub `main`, server release, effective runtime, data readback, and live-trading authority as separate evidence layers. Passing GitHub CI does not by itself authorize production or real trading.
11. Do not put secrets, credentials, databases, runtime state, logs, broker material, or production artifacts in Git.

## Authority domains

### Shared core / deployment

Any `main` advance requires fresh-base integration for PRs touching:

- `deploy/**`
- `shared/**`
- dependency roots such as `requirements*`, `pyproject.toml`, and frontend package manifests

### Governance

Governance changes include:

- `.github/**`
- root `AGENTS.md` and `CONTRIBUTING.md`
- `docs/EVOLUTION_PROGRAM.md` and `docs/operations.md`

A governance-only PR does not need to chase unrelated A-share/Crypto/local-market commits forever. It must refresh when exact files overlap or when `main` changed governance/shared-core authority after its frozen base.

Market-scoped changes outside these authority paths may proceed in parallel when their exact changed files do not overlap changes merged to `main` after the PR base.

## Workflow-governance changes

Changes under `.github/workflows/` must not be self-authorizing. They require a separate trusted controller/machine-governance check before merge; this does not create a routine human-review requirement. A normal application-code PR may not weaken, remove, or replace its own CI/automerge gate.

Repository-side branch/ruleset protection is defense in depth, not something workflow files can prove by themselves. Do not claim `main` is protected unless a fresh GitHub settings/API readback confirms it. If protection is absent, report that as governance debt; do not silently reinterpret workflow convention as branch protection.

If GitHub Actions is temporarily unavailable, leave the affected PR unmerged rather than bypassing `main`. A future independent fallback runner may be added explicitly, but absence of the configured merge gate is not permission to direct-push routine code.

## Post-merge and deployment

Every autonomous merge explicitly dispatches a second validation run bound to the resulting exact `main` SHA. That exact-main run must pass before its SHA-bound release artifact is eligible for production deployment. Deployment still re-checks that the tested SHA is current immediately before cutover. This prevents a clean but semantically incompatible combination of independently tested PRs from being deployed silently.

## Authority boundary

Autonomous code merge/deployment does not grant authority to enable real trading, expand risk automatically, create/change credentials/accounts/permissions, expose a public entry point, or perform destructive data/database operations. Those boundaries remain governed by `AGENTS.md` and `docs/EVOLUTION_PROGRAM.md`.
