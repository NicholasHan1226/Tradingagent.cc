
## Concurrency Rules (2026-07-04)
- All cron scripts use flock with per-job lock files under shared/logs/locks/
- StyleRunner is single-instance per market via file lock
- EvolutionEngine writes atomically (temp + os.replace)
- Multiple agents can READ concurrently; only one agent WRITES at a time
- Git operations: check git status before commit; never force-push; stash before pull
