# tradingagent/cron

This directory contains thin cron wrappers for TradingAgent subsystem jobs.

- Scripts must keep shadow/sim/real boundaries from the root `AGENTS.md`.
- Scripts may call `shared/wrappers/*` or public market workflow entry points.
- Use `flock` locks under `shared/logs/locks/` to avoid overlapping runs.
- Write stdout/stderr to `shared/logs/cron/`.
- Do not place broker credentials, live execution payloads, or approval shortcuts in cron scripts.
