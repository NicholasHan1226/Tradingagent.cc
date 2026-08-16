# Autonomous Development Workflow

This repository uses an agent-first workflow. Routine human code review is not a merge requirement, and GitHub Actions is not a mandatory gate.

## Normal workflow

1. Start from the latest `main` and check for concurrent changes before integration.
2. Keep one task scoped to one branch or isolated candidate. Prefer `agent/<agent>/<task>`, `feat/<task>`, `fix/<task>`, `refactor/<task>`, or `chore/<task>`.
3. Do not overwrite unrelated work and do not force-push or rewrite shared history.
4. Run the smallest deterministic local/server validation that is relevant and available. For runtime/deployment work, fresh canary/readback/receipt evidence is more important than a hosted CI badge.
5. Push the candidate branch when useful for review, handoff, rollback, or concurrent work. A pull request is useful visibility but is not required for routine owner-controlled integration.
6. Before merging, verify ancestry/diff scope and rebase or reapply onto the latest `main` if concurrent work has advanced it.
7. The controller/trusted agent may merge or fast-forward validated work without waiting for a human approval or GitHub Actions run. Never use force to resolve divergence.
8. If GitHub Actions is available and runs, treat it as supplemental validation. A missing/skipped/billing-blocked Actions run must not stop otherwise validated development, simulation deployment, data collection, or simulation lifecycle work.
9. Do not put secrets, credentials, databases, runtime state, logs, broker material, or production artifacts in Git.
10. Keep candidate, GitHub `main`, server release, effective runtime, data readback, and live-trading authority as separate evidence layers.

## Workflow-governance changes

Changes under `.github/workflows/` require an explicit diff/policy check so a task cannot silently weaken safety boundaries. This check is machine-reviewable; it does not create a routine human approval requirement. Hosted CI must never be configured as the sole merge authority because repository Actions capacity may be unavailable.

## Authority boundary

Autonomous code merge/deployment does not grant authority to enable real trading, expand risk automatically, create/change credentials/accounts/permissions, expose a public entry point, or perform destructive data/database operations. Those boundaries remain governed by `AGENTS.md` and `docs/EVOLUTION_PROGRAM.md`.
