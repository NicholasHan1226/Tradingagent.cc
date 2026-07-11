# Task 3 Report — Isolate and Rebuild Stale Epoch-Derived Reviews

## Baseline and scope

- Baseline: `5506332ba9cb7177547edf64f24ba47654337ae1`
- Worktree: `/Users/nicholashan/Projects/Finance/.worktrees/tradingagent-capital-growth`
- Production, cron, real trading, remote sync, and push were not touched.
- The rebuild CLI defaults to dry-run; mutation requires explicit `--apply`.

## Implemented

- Added a fail-closed review epoch validator.
- Added a read-only reset planner that records source, destination, SHA256, size, and epoch-tag status for the exact active derived-file set.
- Added reset apply with destination-collision checks, source revalidation, immutable archive moves, atomic latest-snapshot writes, and rollback on failure.
- Bootstraps empty current-epoch portfolio, evolution-decision, forward-validation, sample-learning, sample-target-monitor, tier, and formal-close snapshots. Historical JSONL rows are moved, never rewritten.
- Integrated the derived-review plan into capital cutover dry-run and apply reports. Epoch state advances only after the review reset succeeds; failures return `cutover_requires_review_repair` and roll the ledger cutover back.
- Added capital epoch, capital amount, and cutover timestamp to portfolio evolution, forward labels, formal-close refresh, tier manifests, and evolution decisions.
- Explicit old-epoch trades and all untagged current-path trades are rejected. Legacy rows without epoch metadata are only eligible from immutable historical archives, never from the current review path.
- Old or stale tier manifests are excluded from current portfolio review.
- Added `tools/rebuild_current_epoch_reviews.py` with `--dry-run|--apply --pretty`; direct script execution is covered.

## TDD evidence

- RED 1: `tests/test_ashare_epoch_review.py` failed because `Ashare.epoch_review` did not exist.
- RED 2: cutover integration tests failed on missing `review_dir` support.
- RED 3: writer tests failed because portfolio, forward validation, and formal close did not read epoch state.
- RED 4: direct CLI execution failed with `ModuleNotFoundError: Ashare`.
- Each failure was followed by the minimum implementation and a focused GREEN run before broader regression.

## Review-failure follow-up

- Removed timestamp-based epoch inference from current trade inputs.
- Added sample-target-monitor latest/log and formal-close latest/history to the exact derived-file set.
- Added current-epoch metadata and full epoch/capital/cutover validation to sample-learning and sample-target-monitor writers and consumers.
- Portfolio evolution now validates the forward-validation report before counting labels.
- Reset apply is idempotent for both the original plan and a rebuilt plan after success.
- Review and archive roots are resolved under an explicit safe root; symlink roots, symlinked files, filesystem-root fallbacks, and path escapes are rejected.
- Reset and cutover rollback attempt every recovery action independently. Partial recovery returns `blocked` with per-action audit details instead of hiding failures in `finally`.
- TDD RED evidence covered each review finding, including real temporary symlink escapes, repeated apply, stale forward labels, wrong capital/cutover metadata, and injected rollback failures.

## Fresh verification

- `tests/test_ashare_epoch_review.py`: 23 passed.
- `tests/test_sim_account_epoch.py`: 27 passed.
- `tests/test_ashare_portfolio_evolution.py`: 4 passed.
- `tests/test_ashare_forward_validation.py`: 2 passed.
- `tests/test_ashare_formal_close_refresh.py`: 4 passed.
- `tests/test_ashare_sample_target_monitor.py`: 5 passed.
- `tests/test_ashare_sample_learning.py`: 8 passed.
- Total focused regression surface: 73 passed, 0 failed.
- `git diff --check`: passed.
- AST syntax parse: 11 changed Python files passed.
- Direct CLI `--help`: passed.

## Self-review and concerns

- No old JSONL row is edited in place.
- Review reset destination collisions and source drift fail closed.
- Cutover state is not advanced when review reset fails.
- Current trade rows must carry explicit `capital_epoch`; otherwise the review boundary excludes them. The ledger writer was outside this task's allowed file set, so this remains an upstream data-contract dependency rather than a review-layer fallback.
- No documentation update outside this task report was required because the operator interface and behavior are fully captured by the task brief and the new CLI help.
