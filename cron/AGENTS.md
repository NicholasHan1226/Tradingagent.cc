# tradingagent/cron

This directory contains thin cron wrappers for TradingAgent subsystem jobs.

- Scripts must keep shadow/sim/real boundaries from the root `AGENTS.md`.
- Scripts may call `shared/wrappers/*` or public market workflow entry points.
- Use `flock` locks under `shared/logs/locks/` to avoid overlapping runs.
- Write stdout/stderr to `shared/logs/cron/`.
- Do not place broker credentials, live execution payloads, or approval shortcuts in cron scripts.

## Production crontab installation

**`tools/merge_tradingagent_crontab.py` is the only allowed way to install or
update TradingAgent entries in the production `marketgraph` user crontab.**

Never run `crontab shared/crontab.txt` or `crontab crontab.txt` directly —
that would overwrite SharedSignals and MarketGraph entries.

The merge tool strips only TA schedule lines from the current crontab and
appends the TA schedule lines from `shared/crontab.txt`.  All other lines
(SharedSignals, MarketGraph, env vars, comments, blank lines) are preserved
as-is in their original order.  Behaviour:

- **Default: dry-run** — prints merged crontab to stdout, no system changes.
- **`--apply`** — backs up current crontab to `runtime/backups/crontab/`,
  installs the merged version, readback-verifies that all TA template entries
  are present with no stale/residual entries.  On readback or coverage failure
  the original crontab is automatically reinstalled and verified.
- **`--current-file` / `--output`** — file-only mode, no system access.

Stable commands:

    python3 tools/merge_tradingagent_crontab.py                      # dry-run
    python3 tools/merge_tradingagent_crontab.py --current-file FILE  # file mode
    sudo python3 tools/merge_tradingagent_crontab.py --apply         # production

See `shared/runtime_test/cron_coverage.py` for production read-only audits.
