# Cron Migration Plan

## Overview

Current state: **101 cron jobs** in a single crontab on `marketgraph@8.138.181.177`.
All jobs currently reference scripts under `/opt/investment/MarketGraph/deploy/` but
serve three distinct repos with different responsibilities.

Target state: **3 separate crontabs** (one per repo) with a shared environment loader.

## Phase 1: Document Current State (DONE)

- Inventoried all 101 active cron jobs from `crontab -l`
- Output: [`cron_inventory.csv`](cron_inventory.csv)
- Each job tagged with: schedule, command, target_repo, target_module, current_frequency, suggested_frequency, migration_status

### Current State Summary

| Metric | Count |
|--------|-------|
| Total active jobs | 101 |
| Env vars (BASH_ENV, SHELL) | 2 |
| Comment lines | ~20 |
| Jobs in `/opt/investment/MarketGraph/deploy/` | 101 |
| Jobs already in Tradings repo | 0 |
| Jobs already in SharedSignals repo | 0 |

### Jobs by Target Repo (from inventory)

| Target Repo | Jobs | Responsibility |
|-------------|------|----------------|
| SharedSignals | ~45 | Data collection, cache warming, Tushare, RSS, health monitors |
| MarketGraph | ~30 | Analysis agents, regime detection, sentiment, API server, dashboard |
| Tradings | ~26 | Trading signals, shadow/forward execution, risk, review, notify |

### Jobs by Module

| Module | Count | Examples |
|--------|-------|----------|
| collection | ~42 | job_collect_*, job_cache_warm*, job_tushare_*, RSSCollector |
| analysis | ~20 | mg_agent/agent_*, job_regime, promote_chain, signal_to_strategy |
| trading | ~12 | job_trading_signals, job_premarket_signals, job_ashare_sim_exec, wrappers/job_*_exec |
| review | ~14 | job_weekly_review, job_daily_brief, gate_review, postclose_review |
| risk | ~3 | job_pm_risk, job_stress_test, job_auto_position |
| health | ~5 | job_health_sweep, job_health_monitor, job_runtime_checkpoint, _dashboard, _api_server |
| notify | ~2 | job_email_notify, job_alert |

## Phase 2: Tag Each Job with Target Repo (DONE)

Classification rules applied:

| Pattern | Target Repo | Target Module |
|---------|-------------|---------------|
| `job_collect_*`, `job_cache_warm*`, `job_tushare_*`, `job_marketdata_db_*`, `job_ashare_daily_backfill*` | SharedSignals | collection |
| `RSSCollector/*`, `collector.py`, `start_rsshub.sh` | SharedSignals | collection |
| `job_health_sweep`, `job_health_monitor`, `auto_maintain.sh` | SharedSignals | health |
| `source_auto_graduate`, `signal_log_cleanup` | SharedSignals | collection |
| `mg_agent/agent_*`, `promote_chain`, `repair_pack` | MarketGraph | analysis |
| `job_regime`, `job_sentiment_fast_promote`, `signal_to_strategy` | MarketGraph | analysis |
| `_dashboard.py`, `_api_server.py`, `_auto_tune.py`, `job_runtime_checkpoint` | MarketGraph | health |
| `job_weekly_calibrate`, `job_seasonal_context` | MarketGraph | analysis |
| `wrappers/job_*_exec`, `wrappers/job_*_shadow`, `wrappers/job_*_forward` | Tradings | trading |
| `job_trading_signals`, `job_premarket_signals`, `job_ashare_sim_exec`, `job_pm_promote` | Tradings | trading |
| `job_weekly_review`, `job_daily_brief`, `gate_review`, `postclose_review` | Tradings | review |
| `job_pm_risk`, `job_stress_test` | Tradings | risk |
| `job_email_notify`, `job_alert` | Tradings | notify |
| `wrappers/job_*_review`, `wrappers/job_*_report`, `wrappers/job_*_attribution` | Tradings | review |

### Migration Status Tags

| Status | Count | Meaning |
|--------|-------|---------|
| `move` | ~71 | Job belongs to a different repo; will be moved when crontab splits |
| `keep` | ~30 | Job stays in MarketGraph (analysis/health jobs that are MarketGraph-native) |
| `later` | 0 | Deferred — not needed in this pass |

## Phase 3: Split Crontab into 3 (PLANNED — not executed)

When ready, the single crontab will be split into three:

### 3a. SharedSignals crontab (~45 jobs)

```
# SharedSignals crontab — data collection, cache warming, health
BASH_ENV=/opt/investment/Tradings/shared/env_loader.sh
# All job_collect_*, job_cache_warm*, job_tushare_*, RSSCollector, health monitors
```

### 3b. MarketGraph crontab (~30 jobs)

```
# MarketGraph crontab — analysis agents, regime, API server, dashboard
BASH_ENV=/opt/investment/Tradings/shared/env_loader.sh
# All mg_agent/agent_*, job_regime, _dashboard.py, _api_server.py, etc.
```

### 3c. Tradings crontab (~26 jobs)

```
# Tradings crontab — trading signals, execution, risk, review, notify
BASH_ENV=/opt/investment/Tradings/shared/env_loader.sh
# All wrappers/job_*_exec, job_trading_signals, job_*_review, job_*_risk, etc.
```

### Execution Steps (when ready)

1. Copy scripts to target repo directories (or symlink from deploy/)
2. Create per-repo crontab files:
   - `/opt/investment/SharedSignals/deploy/crontab.txt`
   - `/opt/investment/MarketGraph/deploy/crontab.txt`
   - `/opt/investment/Tradings/shared/crontab.txt`
3. Install each crontab to the `marketgraph` user (or separate users if desired)
4. Verify all jobs still run via health monitors
5. Remove old monolithic crontab entries

**Important**: Do NOT move any cron jobs now. This document is the plan only.

## Unified Environment Loader

All three crontabs will source [`env_loader.sh`](env_loader.sh) to get a consistent
environment:

- `TRADINGS_ROOT` → `/opt/investment/Tradings`
- `SHARED_SIGNALS_ROOT` → `/opt/investment/SharedSignals`
- `MARKETGRAPH_ROOT` → `/opt/investment/MarketGraph`
- `MARKETGRAPH_RUNTIME_ROOT` → `/opt/investment/MarketGraphRuntime`
- Sources `marketgraph_cron.env` for API keys and Tushare tokens

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Script paths break after move | Keep symlinks from `/opt/investment/MarketGraph/deploy/` until all refs updated |
| Tushare token missing in new env | env_loader.sh sources marketgraph_cron.env centrally |
| Job collision/duplication | Inventory CSV tracks every job; verify count before/after split |
| Cron user permissions | All jobs run as `marketgraph` user — no change needed |
| BASH_ENV chain | env_loader.sh replaces marketgraph_cron_loader.sh as the single entry point |
