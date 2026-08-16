# Autonomous Development Workflow

This repository uses an agent-first GitHub workflow. Human code review is not a merge requirement.

## Required workflow

1. Start from the latest `main`.
2. Create one branch for one task. Prefer `agent/<agent>/<task>`, `feat/<task>`, `fix/<task>`, `refactor/<task>`, or `chore/<task>`.
3. Never push task work directly to `main`.
4. Keep the change scoped to the task and do not overwrite unrelated concurrent work.
5. Run the relevant local tests before pushing.
6. Push the branch and open a pull request against `main`.
7. CI is the merge gate. A same-repository PR created by the trusted repository owner is automatically squash-merged only after all CI jobs succeed and the tested SHA still matches the PR head.
8. Fork PRs, draft PRs, untrusted PR authors, failed CI, and workflow-governance changes are never automatically merged.
9. Do not put secrets, credentials, databases, runtime state, logs, or production artifacts in Git.
10. Do not force-push or rewrite shared history.

## Governance boundary

Changes under `.github/workflows/` are intentionally excluded from normal automatic merging. They require a separate trusted bootstrap/governance merge so a task branch cannot weaken its own merge gate.

Automatic code merge does not grant authority to enable real trading, change credentials/accounts/permissions, perform destructive database or data operations, expand risk, or bypass the runtime safety rules in `AGENTS.md`.
