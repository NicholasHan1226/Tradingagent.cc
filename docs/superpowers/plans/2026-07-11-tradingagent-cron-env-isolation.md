# TradingAgent Cron Environment Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure every installed TradingAgent cron entry executes under the TradingAgent environment loader and health checks fail visibly when scheduled readers are degraded.

**Architecture:** Keep the multi-repository crontab model, but make the TradingAgent merge block self-contained by placing its `BASH_ENV` assignment immediately before its schedule lines. Extend the read-only coverage gate to calculate the effective `BASH_ENV` at each TradingAgent schedule line. Treat a scheduled result carrying `reader_degraded=true` or reader errors as a data-quality warning even when a fresh interactive probe currently sees a strategy-wait condition.

**Tech Stack:** Python 3, `unittest`/pytest, cron text parsing, shell wrapper contracts.

## Global Constraints

- Do not change tokens, `.env` files, production credentials, API contracts, capital, queues, strategies, or execution behavior.
- Do not modify SharedSignals or MarketGraph code.
- Use TDD: add each regression test and observe the expected failure before changing production code.
- Kimi may edit only the files listed below and may not commit, merge, push, deploy, or access production.

---

### Task 1: Make the TradingAgent merge block environment-self-contained

**Files:**
- Modify: `tools/merge_tradingagent_crontab.py`
- Modify: `tests/test_merge_tradingagent_crontab.py`

**Interfaces:**
- Consumes: `merge(current_text: str, template_text: str) -> str | None`
- Produces: merged text containing `BASH_ENV=/opt/investment/tradingagent/shared/env_loader.sh` directly before the appended TradingAgent schedule block.

- [ ] **Step 1: Write the failing test**

```python
def test_appended_ta_block_resets_effective_bash_env(self):
    current = CURRENT + "BASH_ENV=/opt/investment/MarketGraph/deploy/marketgraph_cron_loader.sh\n"
    result = merge(current, TA_TEMPLATE)
    lines = result.splitlines()
    first_ta = next(i for i, line in enumerate(lines) if _is_ta_schedule_line(line))
    self.assertEqual(lines[first_ta - 1], "BASH_ENV=/opt/investment/tradingagent/shared/env_loader.sh")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_merge_tradingagent_crontab.py::MergeTests::test_appended_ta_block_resets_effective_bash_env -q`

Expected: FAIL because the current merge appends only schedule lines after the MarketGraph loader.

- [ ] **Step 3: Write the minimal implementation**

Add a constant and append it immediately before `ta_raw`:

```python
TRADINGAGENT_BASH_ENV = "BASH_ENV=/opt/investment/tradingagent/shared/env_loader.sh"

return "\n".join(kept + [TRADINGAGENT_BASH_ENV] + ta_raw) + "\n"
```

Avoid deleting other repositories' environment assignments; later assignments are allowed because the TradingAgent block now resets its own effective environment.

- [ ] **Step 4: Run the focused tests**

Run: `python3 -m pytest tests/test_merge_tradingagent_crontab.py -q`

Expected: all merge tests pass.

### Task 2: Detect effective cron environment drift

**Files:**
- Modify: `shared/runtime_test/cron_coverage.py`
- Modify: `tests/test_cron_coverage.py`

**Interfaces:**
- Consumes: installed combined crontab text.
- Produces: `environment_mismatch_count`, `environment_mismatches`, and failure `installed_crontab_environment_mismatch`.

- [ ] **Step 1: Write failing coverage tests**

```python
def test_fails_when_tradingagent_entries_inherit_marketgraph_bash_env(self):
    schedules = "\n".join(cron_coverage.tradingagent_entries((cron_coverage.ROOT / "shared/crontab.txt").read_text()))
    installed = "BASH_ENV=/opt/investment/MarketGraph/deploy/marketgraph_cron_loader.sh\n" + schedules
    report = cron_coverage.check_cron_coverage(crontabs={
        "marketgraph_text": installed,
        "marketgraph_error": "",
        "root_text": "",
        "root_error": "no root crontab",
    })
    self.assertIn("installed_crontab_environment_mismatch", report["failures"])
    self.assertEqual(report["environment_mismatch_count"], len(cron_coverage.tradingagent_entries(schedules)))

def test_accepts_tradingagent_block_after_marketgraph_loader(self):
    template = (cron_coverage.ROOT / "shared/crontab.txt").read_text()
    schedules = "\n".join(line for line in template.splitlines() if cron_coverage._is_cron_schedule_line(line))
    installed = "BASH_ENV=/opt/investment/MarketGraph/deploy/marketgraph_cron_loader.sh\nBASH_ENV=/opt/investment/tradingagent/shared/env_loader.sh\n" + schedules
    report = cron_coverage.check_cron_coverage(crontabs={
        "marketgraph_text": installed,
        "marketgraph_error": "",
        "root_text": "",
        "root_error": "no root crontab",
    })
    self.assertEqual(report["environment_mismatch_count"], 0)
```

- [ ] **Step 2: Run tests to verify the first fails for the right reason**

Run: `python3 -m pytest tests/test_cron_coverage.py -q`

Expected: FAIL because coverage currently compares task presence only.

- [ ] **Step 3: Implement effective environment parsing**

Add:

```python
TRADINGAGENT_BASH_ENV = "/opt/investment/tradingagent/shared/env_loader.sh"

def _tradingagent_environment_mismatches(text: str) -> list[dict[str, str]]:
    effective_bash_env = ""
    mismatches = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("BASH_ENV="):
            effective_bash_env = line.split("=", 1)[1].strip()
        elif _is_cron_schedule_line(line) and effective_bash_env != TRADINGAGENT_BASH_ENV:
            mismatches.append({"entry": _normalize_entry(line), "effective_bash_env": effective_bash_env})
    return mismatches
```

Call it on the installed crontab, add the failure when non-empty, and expose counts/details in the report.

- [ ] **Step 4: Run coverage and merge tests**

Run: `python3 -m pytest tests/test_cron_coverage.py tests/test_merge_tradingagent_crontab.py -q`

Expected: all tests pass.

### Task 3: Preserve reader degradation in market-health classification

**Files:**
- Modify: `shared/runtime_test/market_health.py`
- Modify: `tests/test_market_health.py`

**Interfaces:**
- Consumes: `latest_cron_result.payload.reader_degraded` and `reader_errors`.
- Produces: `warn`/`market_data_wait` with reason `latest_cron_reader_degraded`; never `pass`/`strategy_wait` while the scheduled reader is degraded.

- [ ] **Step 1: Write the failing regression test**

```python
def test_crypto_reader_degraded_cron_payload_cannot_pass_as_strategy_wait(self) -> None:
    with patch.object(market_health, "_probe_market_data", return_value={
        "status": "warn",
        "reason": "crypto_momentum_threshold_not_met",
        "priced_signal_count": 5,
        "strategy_candidate_count": 0,
    }), patch.object(market_health, "_latest_cron_result", return_value={
        "payload": {
            "status": "no_trade_signals",
            "reader_degraded": True,
            "reader_errors": ["HTTP 401: Unauthorized"],
        }
    }):
        check = market_health._check_sim_market_loop("crypto", "job_crypto_sim.sh")
    self.assertEqual(check.status, "warn")
    self.assertEqual(check.details["diagnostic_class"], "market_data_wait")
    self.assertIn("latest_cron_reader_degraded", check.details["warn_reasons"])
```

- [ ] **Step 2: Run test and observe RED**

Run: `python3 -m pytest tests/test_market_health.py::MarketHealthTests::test_crypto_reader_degraded_cron_payload_cannot_pass_as_strategy_wait -q`

Expected: FAIL because the current code ignores cron payload degradation when a fresh probe yields a strategy-wait reason.

- [ ] **Step 3: Implement the minimal classification guard**

After parsing `payload`, add `latest_cron_reader_degraded` when `reader_degraded` is true or `reader_errors` is non-empty. Exclude strategy-wait classification when that warning exists, and include it in market-data-wait classification.

- [ ] **Step 4: Run focused regression tests**

Run: `python3 -m pytest tests/test_market_health.py tests/test_opening_acceptance.py -q`

Expected: all tests pass.

### Task 4: Synchronize operational documentation

**Files:**
- Modify: `STATUS.md`
- Modify: `README.md`
- Modify: `cron/AGENTS.md`

**Interfaces:**
- Consumes: verified cron merge and coverage behavior.
- Produces: explicit per-block `BASH_ENV` requirement and production verification commands.

- [ ] **Step 1: Update documentation**

Document that combined crontab environment assignments are positional, the merge installer resets TradingAgent `BASH_ENV` immediately before its schedule block, and `cron_coverage` fails when an installed TradingAgent entry inherits another repository's loader.

- [ ] **Step 2: Run documentation/contract checks**

Run: `python3 -m pytest tests/test_env_loader_boundary.py tests/test_cron_coverage.py tests/test_merge_tradingagent_crontab.py -q`

Expected: all tests pass.

### Task 5: Final verification (no worker commit)

**Files:**
- Verify all files changed by Tasks 1-4.

- [ ] **Step 1: Run targeted acceptance**

Run: `python3 -m pytest tests/test_merge_tradingagent_crontab.py tests/test_cron_coverage.py tests/test_market_health.py tests/test_opening_acceptance.py tests/test_env_loader_boundary.py -q`

- [ ] **Step 2: Run quick project acceptance**

Run: `python3 -m shared.runtime_test.full_acceptance --profile quick --pretty`

- [ ] **Step 3: Inspect exact diff**

Run: `git status --short && git diff --check && git diff -- tools/merge_tradingagent_crontab.py shared/runtime_test/cron_coverage.py shared/runtime_test/market_health.py tests/test_merge_tradingagent_crontab.py tests/test_cron_coverage.py tests/test_market_health.py STATUS.md README.md cron/AGENTS.md`

- [ ] **Step 4: Hand off without committing**

Return changed paths, RED and GREEN test evidence, unverified production items, and rollback notes. The primary Codex assistant performs review, commit, push, deployment, crontab apply, and production verification.
