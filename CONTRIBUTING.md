# Autonomous Development Workflow

This repository uses an agent-first, machine-gated workflow. Routine human code review or approval is not a merge requirement.

## Normal workflow

1. Start from the latest `main` and check for concurrent changes before integration.
2. Use one task per branch and pull request. Prefer `agent/<agent>/<task>`, `feat/<task>`, `fix/<task>`, `refactor/<task>`, or `chore/<task>`.
3. Do not push routine code directly to `main`. Do not force-push or rewrite shared history.
4. Run relevant deterministic local validation before opening or updating the pull request.
5. Open a pull request to `main`. Human review is not required.
6. The pull-request CI is the routine Git merge gate. A failing, missing, cancelled, or stale CI result must not auto-merge.
7. A successful CI run may be squash-merged automatically only when the tested SHA is still the current PR head and the PR comes from a trusted same-repository branch.
8. Fork PRs and untrusted authors never auto-merge.
9. Keep candidate, GitHub `main`, server release, effective runtime, data readback, and live-trading authority as separate evidence layers. Passing GitHub CI does not by itself authorize production or real trading.
10. Do not put secrets, credentials, databases, runtime state, logs, broker material, or production artifacts in Git.

## Workflow-governance changes

Changes under `.github/workflows/` must not be self-authorizing. They require a separate trusted controller/machine-governance check before merge; this does not create a routine human-review requirement. A normal application-code PR may not weaken, remove, or replace its own CI/automerge gate.

If GitHub Actions is temporarily unavailable, leave the affected PR unmerged rather than bypassing `main`. A future independent fallback runner may be added explicitly, but absence of the configured merge gate is not permission to direct-push routine code.

## Authority boundary

Autonomous code merge/deployment does not grant authority to enable real trading, expand risk automatically, create/change credentials/accounts/permissions, expose a public entry point, or perform destructive data/database operations. Those boundaries remain governed by `AGENTS.md` and `docs/EVOLUTION_PROGRAM.md`.
