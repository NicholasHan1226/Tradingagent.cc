# 50,000 CNY Capital Growth Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a simulation-only, evidence-gated capital-growth engine that treats 50,000 CNY as one shared A-share/CN-futures capital pool, measures net realized expectancy after costs, and proves whether MarketGraph improves decisions without promising profit or enabling automatic real trading.

**Architecture:** TradingAgent remains the decision, risk, simulated execution, accounting, and review owner. A new master-capital governor allocates the single 50,000 CNY epoch across A-share notional, CN-futures margin, and cash reserve; market ledgers remain append-only sub-ledgers but cannot independently mint capital. MarketGraph is consumed through a matched baseline/enhanced decision-context experiment and may only boost, downgrade, veto risk, or request thesis review; it never creates an order or bypasses TradingAgent gates.

**Tech Stack:** Python 3.12, dataclasses, JSON/JSONL append-only ledgers, YAML policy, `fcntl` file locking, existing `TradingagentDataReader`, pytest/unittest, existing runtime acceptance modules, React read-only frontend only where current API fields must be exposed.

## Global Constraints

- Current scope is simulated/shadow research only. `REAL_TRADING_ENABLED` remains false; no broker, CTP, SimNow, Hermes real-account click, automatic order, automatic cancel, credential read, or real-account mutation is permitted.
- The system must not promise stable profit. It may report only observed net expectancy, realized PnL, drawdown, sample size, confidence interval, and readiness blockers.
- A-share and CN-futures share one canonical initial equity of exactly `50_000.0 CNY` under capital epoch 2; they must not each receive an independent 50,000 CNY production budget.
- A-share has exactly one authoritative server-local 50,000 CNY simulation execution account and ledger. Legacy aggressive/balanced/grid ledgers remain retired evidence; no style, challenger, experiment, or dashboard may mint a parallel 50k/100k/200k production account or add shadow capital to portfolio equity.
- Initial simulation allocation policy is A-share deployable notional `30_000 CNY`, CN-futures margin `5_000 CNY`, and protected cash reserve `15_000 CNY`; realized PnL changes equity but does not silently change these policy ratios.
- Existing A-share single-name hard limit remains `15%` of master equity. Budget application, lot rounding, replacement buys, and retries must never expand a risk-approved weight.
- Futures quantity is zero when the minimum one-lot margin or modeled stop loss exceeds the style and master risk budget. `max(1, ...)` behavior is forbidden.
- A-share risk expansion requires at least 20 verified execution samples, 10 realized round trips, 20 labeled 60-minute outcomes, positive realized PnL after costs, current capital epoch, and current evidence date.
- Current epoch data and legacy epoch/tier evidence must be physically and logically separated. Epoch mismatch fails closed.
- SharedSignals remains the only market-data provider. TradingAgent must not open a sibling SharedSignals database or collect directly from Tushare/exchanges.
- MarketGraph remains an optional read-only research provider. Missing/stale MG evidence cannot block the SS-only baseline and cannot become an execution error.
- Candidate learning uses three explicitly separated intents: `observe`/`counterfactual`, `exploration`, and `exploitation`. Sample debt is an input to exploration selection, never a standalone reason to suppress all candidates or all labels.
- Every data-qualified candidate is recorded before mature-strategy and execution thresholds are applied, with prediction timestamp, strategy version, score/probability, market regime, MG on/off, intended channel, and a concrete no-trade reason. Only unreliable market data may make the candidate ineligible for forward labeling.
- Exploration may lower only strategy gates such as candidate score, minimum modeled edge, or research completeness. It must never relax freshness/provenance, real price/fill evidence, liquidity, T+1, session, cash/margin, idempotency, lot size, single-name/portfolio exposure, daily loss, losing streak, maximum drawdown, or real-trading isolation.
- A-share exploration is simulation-only, limited to at most one new position per trading day, and must derive executable size from current master equity, the existing 15% single-name cap, available cash, remaining exploration exposure/loss budget, 100-share board lots, and modeled costs/slippage. Old probe budgets are not single-name allowances and must not be reused as such.
- Every valid CN-futures session emits a replayable decision record. A contract may enter simulated execution only when its real minimum lot, margin, modeled stop loss, slippage, night-gap allowance, and losing-streak gates fit the master budget; otherwise it is labeled `counterfactual_only` rather than force-sized to one lot.
- Evolution promotion standards are unchanged by exploration. No channel may expand risk without completed round trips, matured forward labels, sample-quality evidence, net expectancy after fees/slippage, calibration evidence, and controlled drawdown.
- A-share style diversity exists only in prediction and immutable counterfactual shadow evidence. One portfolio arbiter owns conflict resolution, correlation, cash, risk, T+1, price-limit, liquidity, lot, and idempotency gates and may emit at most one real-specification simulated order for the same symbol and trade date.
- Initial A-share hypotheses must be economically distinct, not threshold variants: trend/strength continuation, pullback or short-horizon reversal, event catalyst with price confirmation, and defensive/low-volatility abstention baseline. Existing strategy files are mapped into these families after audit; duplicate or unexplained styles remain shadow-paused.
- Every selected fill carries `primary_style`, `supporting_styles`, `style_scores`, `style_versions`, and `decision_policy_version`. Unselected and disagreeing styles retain their predictions, thesis, abstain/reject reason, and forward labels so training is not conditioned only on executed samples.
- Champion styles may receive normal simulation allocation; challengers remain shadow-only or exploration-only until out-of-sample, net-of-cost expectancy, drawdown, and calibration gates pass. Demotion/pause is append-only and never rewrites historical predictions or fills.
- A style sample is `execution_eligible` only when real price provenance, modeled commission/tax/slippage, 100-share lot, T+1, price-limit state, liquidity, cash, and fill evidence are complete. Incomplete rows remain useful observation/counterfactual evidence but must never be counted as executable fills or promotion-grade round trips.
- Every state-changing implementation step must be TDD-first, append-only where facts are involved, fail-closed, reversible, and committed separately. Do not push, deploy, modify cron, migrate production, or delete production files until the repository release gate and explicit production preflight pass.
- Existing user-owned or concurrent changes must not be overwritten. Never use force-push, history rewrite, `git reset --hard`, or destructive checkout.

## File Map

### New focused modules

- `shared/capital/__init__.py` — public exports for the master-capital package.
- `shared/capital/policy.py` — load and validate the exact 50k allocation/risk policy.
- `shared/capital/master_ledger.py` — append-only master-capital events, locked latest snapshot, reconciliation, and reservation decisions.
- `shared/capital/capital_policy.yaml` — canonical simulation allocation and loss/drawdown guardrails.
- `shared/research/__init__.py` — public exports for research decision context.
- `shared/research/decision_context.py` — normalized SS baseline plus bounded MG action overlay.
- `shared/review/mg_ablation.py` — matched baseline/enhanced counterfactual observations and delta metrics.
- `shared/review/capital_growth_metrics.py` — cost-aware round trips, net expectancy, profit factor, drawdown, and sample confidence.
- `shared/review/capital_growth_gate.py` — readiness state machine; never enables real trading.
- `shared/review/prediction_observations.py` — append-only pre-gate candidate snapshots and concrete no-trade reasons for all three sample intents.
- `shared/review/forward_label_contract.py` — due-time scheduling and idempotent m30/m60/close/next-day/3d/5d labels independent of execution eligibility.
- `Ashare/exploration_policy.py` — rank/quantile exploration selection and dynamically derived 50k-safe lot/cost/exposure budget.
- `Ashare/style_hypotheses.py` — audit and normalize existing strategies into a small set of orthogonal, explainable hypothesis families.
- `Ashare/style_shadow_ledger.py` — append-only per-style predictions, thesis, expected-return distribution, abstentions, disagreements, and forward-label links without virtual capital.
- `Ashare/portfolio_arbiter.py` — one-account conflict/correlation/idempotency arbitration and immutable execution attribution.
- `Ashare/style_promotion.py` — append-only champion/challenger promotion, demotion, and pause decisions under unchanged evidence gates.
- `CNFutures/session_observations.py` — one decision/hold/rejection observation per valid futures session with executable versus counterfactual classification.
- `shared/runtime_test/sample_flow_acceptance.py` — preopen/opening/ops proof that qualified candidates and due labels cannot silently return to zero.
- `Ashare/epoch_review.py` — current-epoch validation and reversible stale-review reset/rebuild.
- `tools/rebuild_current_epoch_reviews.py` — dry-run/apply operator entry for post-cutover review repair.
- `docs/architecture.md` — canonical three-system and TradingAgent internal architecture.
- `docs/capital_growth_validation.md` — current 50k validation objective, KPIs, gates, and non-guarantee boundary.
- `docs/operations.md` — canonical local/production read-only verification and release boundary.

### Existing modules modified

- `shared/orchestrator.py` — final A-share risk cap, master-capital reservation, matched MG variants, diagnostics.
- `Ashare/capital_plan.py` — policy-aligned deployable budget; no post-risk expansion.
- `Ashare/evolution_controller.py` — current-epoch/current-date/positive-realized-PnL expansion gate.
- `Ashare/portfolio_evolution.py` — epoch-tagged evidence and cost-aware metrics.
- `Ashare/forward_validation.py` — epoch-tagged labels.
- `Ashare/formal_close_refresh.py` — current-epoch propagation and stale-derived-state rebuild.
- `CNFutures/sim_runner.py` — zero-quantity affordability decision and master margin reservation.
- `CNFutures/review.py` — master capital and cost-aware review fields.
- `CNFutures/observation_report.py` — affordable-product coverage and readiness blockers.
- `shared/execution/sim_account_epoch.py` — epoch-scoped derived-state manifest.
- `tools/migrate_sim_capital_epoch.py` — include review repair in dry-run/apply plan.
- `shared/markets/sim_capital.py` — preserve native defaults for offline callers while directing production A-share/CNFutures through the master policy.
- `shared/screening/six_dimension_scorer.py` — explicit baseline versus MG-enhanced modes.
- `shared/data/reader.py` — bounded MG metadata/errors without direct file fallback.
- `shared/review/goals.yaml` — simulation evidence goals only; remove automatic real/scaled progression language.
- `shared/review/daily_review.py` — canonical capital-growth metrics and gate state.
- `shared/review/weekly_review.py` — sample/out-of-sample and MG ablation review.
- `shared/review/daily_review.py`, `shared/review/weekly_review.py` — separately report observe/counterfactual, exploration, and exploitation counts and economics, including gate-block distributions.
- `shared/runtime_test/market_health.py` — master reconciliation, epoch, affordability, and gate checks.
- `shared/runtime_test/self_evolution_health.py` — reject stale epoch and unrealized-only expansion.
- `shared/runtime_test/full_acceptance.py` — register the new focused acceptance checks.
- `README.md`, `AGENTS.md`, `STATUS.md`, `Ashare/AGENTS.md`, `CNFutures/AGENTS.md`, `shared/AGENTS.md`, `shared/risk/AGENTS.md`, `shared/review/AGENTS.md`, `docs/AGENTS.md`, `docs/data_contract.md`, `docs/data_sources.md`, `docs/write_end_contract.md`, `docs/INFRASTRUCTURE.md` — canonical boundaries and entry points.

### Retired after dependency proof

- `shared/orchestrator_design.md`
- `docs/archive/AGENTS.md`
- `docs/archive/BATCH_PLAN_20260630.md`
- `docs/archive/cron_gap_20260629.md`
- `docs/runtime_incidents_20260701.md`
- `docs/runtime_incidents_20260702.md`
- `docs/superpowers/plans/2026-07-11-tradingagent-cron-env-isolation.md`
- `shared/signals/signal_cards.jsonl`
- `shared/signals/pm/pm_forward_signals.jsonl`
- active production integration of `Ashare/tier_experiments.py`; historical epoch archive evidence and the capital migration audit remain preserved.

---

### Task 1: P0 — Prevent A-share Post-Risk Budget Expansion

**Files:**
- Modify: `shared/orchestrator.py:943-979`
- Modify: `Ashare/capital_plan.py:140-259`
- Test: `tests/test_sim_loop.py`
- Test: `tests/test_ashare_capital_plan.py`

**Interfaces:**
- Consumes: risk-approved `order_meta[symbol]["weight"]: float`, `capital_plan["position_budget_by_symbol"]: dict[str, float]`, `capital: float`.
- Produces: `_apply_position_budgets(...) -> None` with each final `position["weight"] <= order_meta[symbol]["weight"] <= 0.15`, plus `position["requested_budget"]`, `position["risk_capped_budget"]`, and `position["budget_cap_reason"]` diagnostics.

- [ ] **Step 1: Write the failing post-risk expansion tests**

```python
def test_ashare_position_budget_cannot_expand_risk_approved_weight():
    portfolio = {"positions": [{"ts_code": "600000.SH", "price": 10.0, "shares": 700, "weight": 0.14}]}
    _apply_position_budgets(
        market="ashare",
        portfolio=portfolio,
        order_meta={"600000.SH": {"price": 10.0, "weight": 0.14}},
        capital_plan={"enabled": True, "position_budget_by_symbol": {"600000.SH": 15_000.0}},
        capital=50_000.0,
    )
    position = portfolio["positions"][0]
    assert position["weight"] <= 0.14
    assert position["amount"] <= 7_000.0
    assert position["requested_budget"] == 15_000.0
    assert position["budget_cap_reason"] == "risk_adjusted_weight_cap"


def test_ashare_position_budget_rounding_cannot_cross_fifteen_percent():
    portfolio = {"positions": [{"ts_code": "600000.SH", "price": 7.31, "shares": 100, "weight": 0.01}]}
    _apply_position_budgets(
        market="ashare",
        portfolio=portfolio,
        order_meta={"600000.SH": {"price": 7.31, "weight": 0.15}},
        capital_plan={"enabled": True, "position_budget_by_symbol": {"600000.SH": 17_500.0}},
        capital=50_000.0,
    )
    assert portfolio["positions"][0]["amount"] <= 7_500.0
    assert portfolio["positions"][0]["weight"] <= 0.15
```

- [ ] **Step 2: Run RED tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_sim_loop.py::test_ashare_position_budget_cannot_expand_risk_approved_weight \
  tests/test_sim_loop.py::test_ashare_position_budget_rounding_cannot_cross_fifteen_percent -q
```

Expected: both fail because the current 15,000 CNY capital-plan budget rewrites the final position to 30%.

- [ ] **Step 3: Implement the minimal final budget cap**

```python
requested_budget = budget
risk_weight_cap = min(0.15, max(0.0, _safe_float(meta.get("weight"), 0.0)))
risk_capped_budget = min(requested_budget, capital * risk_weight_cap)
shares = int(risk_capped_budget // price)
shares = (shares // 100) * 100
amount = shares * price
position["requested_budget"] = round(requested_budget, 2)
position["risk_capped_budget"] = round(risk_capped_budget, 2)
position["budget_cap_reason"] = (
    "risk_adjusted_weight_cap" if risk_capped_budget < requested_budget else "capital_plan_budget"
)
position["shares"] = shares
position["amount"] = round(amount, 2)
position["weight"] = round(amount / max(capital, 1.0), 6)
```

Do not increase `meta["weight"]`; it is the already approved incremental weight. Do not change `single_stock_max` from 0.15 in this task.

- [ ] **Step 4: Make the capital-plan diagnostics honest**

Update `CapitalPlan.to_dict()` output so `max_single_position_pct` describes the maximum requested plan budget while every suggested buy also carries:

```python
{
    "requested_budget": round(requested_budget, 2),
    "risk_limit_budget": round(total_capital * 0.15, 2),
    "executable_budget": round(min(requested_budget, total_capital * 0.15), 2),
}
```

The plan may explain a concentration preference, but it must not claim that 25%–35% is executable under the unchanged 15% hard limit.

- [ ] **Step 5: Run GREEN tests and focused regressions**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_sim_loop.py tests/test_ashare_capital_plan.py tests/test_multi_market_p2_tools.py -q
```

Expected: PASS; no final A-share position exceeds the risk-approved weight or 15%.

- [ ] **Step 6: Commit the isolated P0 fix**

```bash
git add shared/orchestrator.py Ashare/capital_plan.py tests/test_sim_loop.py tests/test_ashare_capital_plan.py
git commit -m "fix(ashare): prevent post-risk budget expansion"
```

---

### Task 2: P0 — Require Current, Realized Evidence Before A-share Expansion

**Files:**
- Modify: `Ashare/evolution_controller.py:59-160`
- Modify: `Ashare/evolution_controller.py:192-204`
- Modify: `shared/orchestrator.py:900-939`
- Test: `tests/test_ashare_evolution_controller.py`

**Interfaces:**
- Consumes: `portfolio_evolution` containing `capital_epoch`, `trade_date`, `actions`, `pnl.realized_pnl`, `pnl.total_pnl`, and `evolution_evidence`.
- Produces: `build_evolution_decision(..., current_epoch_id: int = 2) -> dict[str, Any]`; expansion is possible only with current date/epoch and positive realized PnL after every sample gate.
- Produces: `decision_market_context(decision, *, target_trade_date: str, current_epoch_id: int) -> dict[str, Any]` with `evidence_usable` and explicit stale reason.

- [ ] **Step 1: Write failing expansion and stale-evidence tests**

```python
def _qualified_evidence(**overrides):
    payload = {
        "capital_epoch": 2,
        "trade_date": "20260711",
        "strategy_sample_count": 20,
        "actions": [{"action": "observe", "reason": "non_positive_realized_pnl"}],
        "pnl": {"total_pnl": 100.0, "realized_pnl": 0.0, "equity": 50_100.0},
        "evolution_evidence": {
            "eligible_sample_count": 20,
            "realized_round_trip_count": 10,
            "forward_label_count": 20,
        },
    }
    payload.update(overrides)
    return payload


def test_unrealized_profit_never_expands_risk():
    decision = build_evolution_decision(
        _qualified_evidence(), target_trade_date="20260711", current_epoch_id=2
    )
    assert decision["recommended_action"] == "observe_and_label_candidates"
    assert "non_positive_realized_pnl" in decision["reasons"]


def test_stale_epoch_never_enters_capital_plan_context():
    decision = build_evolution_decision(
        _qualified_evidence(capital_epoch=1), target_trade_date="20260711", current_epoch_id=2
    )
    context = decision_market_context(decision, target_trade_date="20260711", current_epoch_id=2)
    assert context["evidence_usable"] is False
    assert context["evidence_rejection_reason"] == "capital_epoch_mismatch"
    assert context["strategy_sample_valid_count"] == 0.0
```

- [ ] **Step 2: Run RED tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_ashare_evolution_controller.py::test_unrealized_profit_never_expands_risk \
  tests/test_ashare_evolution_controller.py::test_stale_epoch_never_enters_capital_plan_context -q
```

Expected: the unrealized-only test currently returns `expand_risk_candidate`; stale epoch is not checked.

- [ ] **Step 3: Implement strict evidence ordering**

Use this decision order in `build_evolution_decision`:

```python
if evidence_epoch != current_epoch_id:
    state, action = "evidence_pending", "observe_and_label_candidates"
    reasons.append("capital_epoch_mismatch")
elif evidence_date != target_date:
    state, action = "evidence_pending", "observe_and_label_candidates"
    reasons.append("portfolio_evolution_trade_date_stale")
elif strategy_sample_count < min_samples:
    state, action = "evidence_pending", "observe_and_label_candidates"
    reasons.append("cumulative_strategy_samples_below_minimum")
elif eligible_sample_count < min_evolution_evidence_samples:
    state, action = "evidence_pending", "observe_and_label_candidates"
    reasons.append("insufficient_verified_execution_evidence")
elif realized_round_trip_count < max(1, min_evolution_evidence_samples // 2):
    state, action = "evidence_pending", "observe_and_label_candidates"
    reasons.append("insufficient_realized_round_trips")
elif forward_label_count < min_evolution_evidence_samples:
    state, action = "evidence_pending", "observe_and_label_candidates"
    reasons.append("insufficient_forward_validation")
elif realized_pnl <= 0:
    state, action = "evidence_pending", "observe_and_label_candidates"
    reasons.append("non_positive_realized_pnl")
elif total_pnl < 0:
    state, action = "risk_tightening", "tighten_risk"
    reasons.append("negative_mark_to_market_pnl")
else:
    state, action = "expansion_candidate", "expand_risk_candidate"
    reasons.append("positive_realized_pnl_after_all_gates")
```

- [ ] **Step 4: Make stale context fail closed in the orchestrator**

Call:

```python
evolution_context = decision_market_context(
    evolution_decision,
    target_trade_date=date,
    current_epoch_id=read_epoch_state()["current_epoch_id"],
)
```

When `evidence_usable` is false, set `strategy_sample_valid_count=0`, preserve the rejection reason in `capital_plan.evolution_decision`, and never use stale counts to unlock sample collection or expansion.

- [ ] **Step 5: Run GREEN tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_ashare_evolution_controller.py tests/test_sim_loop.py tests/test_ashare_portfolio_evolution.py -q
```

Expected: PASS; only current, realized, fully labeled evidence can become an expansion candidate.

- [ ] **Step 6: Commit**

```bash
git add Ashare/evolution_controller.py shared/orchestrator.py tests/test_ashare_evolution_controller.py
git commit -m "fix(ashare): require realized current-epoch expansion evidence"
```

---

### Task 3: P0 — Isolate and Rebuild Stale Epoch-Derived Reviews

**Files:**
- Create: `Ashare/epoch_review.py`
- Create: `tools/rebuild_current_epoch_reviews.py`
- Modify: `Ashare/portfolio_evolution.py:155-365`
- Modify: `Ashare/forward_validation.py:166-198`
- Modify: `Ashare/formal_close_refresh.py`
- Modify: `shared/execution/sim_account_epoch.py`
- Test: `tests/test_ashare_epoch_review.py`
- Test: `tests/test_sim_account_epoch.py`

**Interfaces:**
- Produces: `validate_review_epoch(payload: dict, *, current_epoch_id: int, current_cutover_timestamp: str) -> tuple[bool, str]`.
- Produces: `build_epoch_reset_plan(review_dir: Path, archive_dir: Path, epoch_state: dict) -> dict` without writes.
- Produces: `apply_epoch_reset_plan(plan: dict) -> dict` that moves stale derived files into the immutable epoch archive and atomically writes empty epoch-2 latest snapshots.
- CLI: `python tools/rebuild_current_epoch_reviews.py --dry-run|--apply --pretty`.
- Review authority is the exact persisted triple: epoch `2`, capital `50,000 CNY`, and the original cutover timestamp string. A timezone-equivalent rendering is not accepted as the same authority.
- Tier replay filters source trades before replay and reports only current-authority trade statistics; rejected rows remain rejection diagnostics and must never be relabeled as epoch 2.
- Build, apply, already-applied detection, and current-set validation reject symlinks in the review/archive roots, controlled ancestors, and every Task-3 latest/log child.

- [ ] **Step 1: Write failing epoch-isolation tests**

```python
def test_epoch_one_review_is_rejected_after_epoch_two_cutover(tmp_path):
    payload = {"capital_epoch": 1, "generated_at": "2026-07-10T08:58:08+00:00"}
    valid, reason = validate_review_epoch(
        payload, current_epoch_id=2, current_cutover_timestamp="2026-07-10T20:56:58+00:00"
    )
    assert valid is False
    assert reason == "capital_epoch_mismatch"


def test_reset_plan_archives_legacy_reviews_and_bootstraps_empty_current_epoch(tmp_path):
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    (review_dir / "portfolio_evolution_latest.json").write_text(
        json.dumps({"capital_epoch": 1, "strategy_sample_count": 3}), encoding="utf-8"
    )
    plan = build_epoch_reset_plan(
        review_dir,
        tmp_path / "archive",
        {"current_epoch_id": 2, "capital_cny": 50_000.0, "cutover_timestamp": "2026-07-10T20:56:58+00:00"},
    )
    assert plan["status"] == "ready"
    assert plan["move_count"] == 1
    assert plan["bootstrap"]["strategy_sample_count"] == 0
    assert plan["bootstrap"]["capital_epoch"] == 2
```

- [ ] **Step 2: Run RED tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider tests/test_ashare_epoch_review.py -q
```

Expected: FAIL because `Ashare.epoch_review` does not exist.

- [ ] **Step 3: Implement the dry-run plan and fail-closed validator**

Use the exact active derived file set:

```python
CURRENT_DERIVED_FILES = (
    "portfolio_evolution_latest.json",
    "portfolio_evolution_log.jsonl",
    "evolution_decision_latest.json",
    "evolution_decision_log.jsonl",
    "forward_validation_latest.json",
    "forward_validation.jsonl",
    "sample_learning_latest.json",
    "sample_learning_log.jsonl",
    "tier_experiments_latest.json",
)
```

The plan records source path, destination path, SHA256, size, and whether the file is epoch-tagged. Missing files are not errors. Destination collisions fail closed.

- [ ] **Step 4: Implement atomic apply and empty epoch-2 bootstrap**

The bootstrap payload must include:

```python
{
    "capital_epoch": 2,
    "capital_cny": 50_000.0,
    "strategy_sample_count": 0,
    "today_strategy_sample_count": 0,
    "evolution_evidence": {
        "eligible_sample_count": 0,
        "realized_round_trip_count": 0,
        "forward_label_count": 0,
        "blockers": ["current_epoch_has_no_verified_samples"],
    },
    "pnl": {
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "total_pnl": 0.0,
        "cash": 50_000.0,
        "market_value": 0.0,
        "equity": 50_000.0,
    },
    "real_trading_enabled": False,
}
```

Use temp-file plus `os.replace`; never edit old JSONL rows in place.

- [ ] **Step 5: Propagate epoch fields from writers**

Add to portfolio evolution, forward labels, formal-close refresh, and evolution decisions:

```python
"capital_epoch": int(epoch_state["current_epoch_id"]),
"capital_cny": float(epoch_state["capital_cny"]),
"epoch_cutover_timestamp": str(epoch_state["cutover_timestamp"]),
```

Reject source trades whose explicit epoch differs. Legacy rows without epoch may only be read from the immutable epoch archive, never the current review path.

- [ ] **Step 6: Extend capital cutover dry-run/apply manifests**

`apply_cutover` must include the derived-review reset plan in the same operator report. A review-reset failure leaves the epoch state unadvanced or reports `cutover_requires_review_repair`; it must never claim a fully current epoch while stale derived state remains eligible.

- [ ] **Step 7: Run GREEN tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_ashare_epoch_review.py tests/test_sim_account_epoch.py \
  tests/test_ashare_portfolio_evolution.py tests/test_ashare_forward_validation.py \
  tests/test_ashare_formal_close_refresh.py -q
```

Expected: PASS; current epoch reports cannot contain epoch-1 trades, tiers, PnL, or labels.

- [ ] **Step 8: Commit**

```bash
git add Ashare/epoch_review.py tools/rebuild_current_epoch_reviews.py \
  Ashare/portfolio_evolution.py Ashare/forward_validation.py Ashare/formal_close_refresh.py \
  shared/execution/sim_account_epoch.py tests/test_ashare_epoch_review.py tests/test_sim_account_epoch.py
git commit -m "fix(ashare): isolate derived state by capital epoch"
```

---

### Task 4: P0 — Reject Unaffordable Minimum Futures Contracts

**Files:**
- Modify: `CNFutures/sim_runner.py:702-715`
- Modify: `CNFutures/sim_runner.py:1020-1120`
- Modify: `CNFutures/observation_report.py`
- Test: `tests/test_cn_futures_sim_runner.py`
- Test: `tests/test_cn_futures_observation_report.py`

**Interfaces:**
- Replaces private integer-only sizing with `quantity_for_style_decision(symbol: str, price: float, capital: float, style: dict) -> dict[str, Any]`.
- Produces: `build_affordability_hold(*, symbol: str, style_name: str, size_decision: dict, cadence: str, bar_time: str, session: str) -> dict[str, Any]`.
- Produces: `quantity`, `margin_per_lot`, `margin_budget`, `modeled_loss_per_lot`, `loss_budget`, `eligible`, and `reason`.

- [ ] **Step 1: Write failing affordability tests**

```python
def test_minimum_contract_above_risk_budget_returns_zero():
    decision = quantity_for_style_decision(
        symbol="RB2610.SHF",
        price=3_500.0,
        capital=50_000.0,
        style={"risk_per_trade": 0.01, "max_margin_usage": 0.30, "weight": 0.25, "stop_loss_pct": 0.01},
    )
    assert decision["margin_budget"] == 125.0
    assert decision["quantity"] == 0
    assert decision["eligible"] is False
    assert decision["reason"] == "minimum_contract_exceeds_risk_budget"


def test_unaffordable_contract_creates_hold_not_error():
    decision = quantity_for_style_decision(
        symbol="RB2610.SHF",
        price=3_500.0,
        capital=50_000.0,
        style={"risk_per_trade": 0.01, "max_margin_usage": 0.30, "weight": 0.25, "stop_loss_pct": 0.01},
    )
    hold = build_affordability_hold(
        symbol="RB2610.SHF",
        style_name="trend",
        size_decision=decision,
        cadence="5min",
        bar_time="2026-07-11T09:35:00+08:00",
        session="day",
    )
    assert hold["stage"] == "risk"
    assert hold["reason"] == "minimum_contract_exceeds_risk_budget"
    assert hold["size_decision"]["quantity"] == 0
```

- [ ] **Step 2: Run RED tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_cn_futures_sim_runner.py::test_minimum_contract_above_risk_budget_returns_zero \
  tests/test_cn_futures_sim_runner.py::test_unaffordable_contract_creates_hold_not_error -q
```

Expected: current `_quantity_for_style` returns one lot because of `max(1, ...)`.

- [ ] **Step 3: Implement zero-safe sizing**

```python
margin_budget = capital * min(max_margin_usage, risk_per_trade * weight)
loss_budget = capital * risk_per_trade * weight
quantity_by_margin = int(margin_budget // margin_per_lot)
quantity_by_loss = int(loss_budget // modeled_loss_per_lot) if modeled_loss_per_lot > 0 else 0
quantity = min(quantity_by_margin, quantity_by_loss)
eligible = quantity >= 1
reason = "eligible" if eligible else "minimum_contract_exceeds_risk_budget"
```

`modeled_loss_per_lot = price * contract_multiplier * stop_loss_pct + round_trip_fees + modeled_round_trip_slippage`. Missing stop-loss or contract-rule inputs fail closed with `missing_contract_risk_inputs`.

- [ ] **Step 4: Persist affordability evidence as a hold**

Before constructing an order:

```python
if not size_decision["eligible"]:
    holds.append({
        "stage": "risk",
        "style": style_name,
        "symbol": symbol,
        "product": _product_or_empty(symbol),
        "reason": size_decision["reason"],
        "size_decision": size_decision,
    })
    continue
```

The observation report must show raw distinct products and affordable distinct products separately.

- [ ] **Step 5: Run GREEN tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_cn_futures_sim_runner.py tests/test_cn_futures_observation_report.py \
  tests/test_cn_futures_review.py -q
```

Expected: PASS; insufficient capital is an explainable observation hold, never a forced one-lot trade or system error.

- [ ] **Step 6: Commit**

```bash
git add CNFutures/sim_runner.py CNFutures/observation_report.py \
  tests/test_cn_futures_sim_runner.py tests/test_cn_futures_observation_report.py
git commit -m "fix(cnfutures): reject unaffordable minimum contracts"
```

---

### Task 5: Build the Single 50,000 CNY Master-Capital Governor

**Files:**
- Create: `shared/capital/__init__.py`
- Create: `shared/capital/policy.py`
- Create: `shared/capital/master_ledger.py`
- Create: `shared/capital/capital_policy.yaml`
- Create: `shared/capital/AGENTS.md`
- Modify: `shared/orchestrator.py`
- Modify: `CNFutures/sim_runner.py`
- Modify: `shared/markets/sim_capital.py`
- Test: `tests/test_master_capital.py`
- Test: `tests/test_sim_loop.py`
- Test: `tests/test_cn_futures_sim_runner.py`

**Interfaces:**
- Produces: `CapitalPolicy.load(path: Path | None = None) -> CapitalPolicy`.
- Produces: `MasterCapitalLedger.snapshot(epoch_id: int) -> MasterCapitalSnapshot`.
- Produces: `MasterCapitalLedger.reserve(request: CapitalReservationRequest) -> CapitalReservationDecision`.
- Produces: `MasterCapitalLedger.release(reservation_id: str, amount: float, reason: str) -> dict`.
- Uses one lock: `shared/logs/capital/.master_capital.lock`; facts append to `master_capital_events.jsonl`; latest projection is `master_capital_latest.json`.

- [ ] **Step 1: Write policy and reconciliation RED tests**

```python
def test_default_policy_allocates_one_fifty_thousand_pool():
    policy = CapitalPolicy.load()
    assert policy.initial_equity_cny == 50_000.0
    assert policy.ashare_notional_limit_cny == 30_000.0
    assert policy.cn_futures_margin_limit_cny == 5_000.0
    assert policy.protected_cash_reserve_cny == 15_000.0
    assert (
        policy.ashare_notional_limit_cny
        + policy.cn_futures_margin_limit_cny
        + policy.protected_cash_reserve_cny
    ) == policy.initial_equity_cny


def test_cross_market_reservations_cannot_mint_a_second_fifty_thousand(tmp_path):
    ledger = MasterCapitalLedger(tmp_path, policy=CapitalPolicy.load())
    assert ledger.reserve(CapitalReservationRequest("ashare", "A1", 30_000.0, 2)).approved
    assert ledger.reserve(CapitalReservationRequest("cn_futures", "F1", 5_000.0, 2)).approved
    rejected = ledger.reserve(CapitalReservationRequest("ashare", "A2", 1_000.0, 2))
    assert rejected.approved is False
    assert rejected.reason == "ashare_allocation_exhausted"
    snapshot = ledger.snapshot(2)
    assert snapshot.total_reserved_cny == 35_000.0
    assert snapshot.protected_cash_cny >= 15_000.0
```

- [ ] **Step 2: Run RED tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider tests/test_master_capital.py -q
```

Expected: FAIL because `shared.capital` does not exist.

- [ ] **Step 3: Add the exact simulation policy**

```yaml
schema_version: capital-policy.v1
capital_epoch: 2
currency: CNY
initial_equity_cny: 50000.0
allocations:
  ashare_notional_limit_cny: 30000.0
  cn_futures_margin_limit_cny: 5000.0
  protected_cash_reserve_cny: 15000.0
risk:
  ashare_single_name_max_pct: 0.15
  master_daily_loss_pause_pct: 0.03
  master_drawdown_tighten_pct: 0.05
  master_drawdown_halt_pct: 0.07
execution:
  capital_layer: simulated
  real_trading_enabled: false
```

Validation rejects negative values, allocation sums other than exactly 50,000, non-CNY currency, epoch other than 2, or `real_trading_enabled=true`.

- [ ] **Step 4: Implement append-only reservations and reconciliation**

Every event has:

```python
{
    "event_id": str,
    "event_type": "bootstrap|reserve|release|mark|realized_pnl|reconcile",
    "capital_epoch": 2,
    "market": "ashare|cn_futures|cash",
    "reference_id": str,
    "amount_cny": float,
    "currency": "CNY",
    "created_at": str,
    "real_trading_enabled": False,
}
```

`snapshot()` replays events and enforces:

```python
available_equity_cny = initial_equity_cny + realized_pnl_cny
total_reserved_cny = ashare_reserved_cny + cn_futures_margin_reserved_cny
protected_cash_cny = available_equity_cny - total_reserved_cny
reconciled = total_reserved_cny + protected_cash_cny == available_equity_cny
```

- [ ] **Step 5: Gate A-share and futures orders through the master ledger**

For A-share, reserve final order notional before simulated submission and release on rejected/expired/cancelled orders. For futures, reserve actual simulated margin before fill and release it on flatten/close. Duplicate `reference_id` is idempotent.

When the master ledger is unavailable or unreconciled, new positions fail closed with `master_capital_unavailable`; risk-reducing exits remain allowed.

- [ ] **Step 6: Keep `default_sim_capital` compatible without using it as a second production pool**

Add:

```python
def production_master_capital_cny() -> float:
    return CapitalPolicy.load().initial_equity_cny
```

Document that `default_sim_capital("ashare")` and `default_sim_capital("cn_futures")` are sizing compatibility values only; production capacity comes from `MasterCapitalLedger` reservations.

- [ ] **Step 7: Run GREEN and concurrent-lock tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_master_capital.py tests/test_sim_loop.py tests/test_cn_futures_sim_runner.py \
  tests/test_p1_stress_s1_concurrent.py -q
```

Expected: PASS; concurrent reservations cannot overspend or corrupt the append-only ledger.

- [ ] **Step 8: Commit**

```bash
git add shared/capital shared/orchestrator.py CNFutures/sim_runner.py \
  shared/markets/sim_capital.py tests/test_master_capital.py tests/test_sim_loop.py \
  tests/test_cn_futures_sim_runner.py
git commit -m "feat(capital): govern A-share and futures with one 50k pool"
```

---

### Task 6: Extend Epoch Isolation Across Master Capital, Orders, Labels, and Reviews

**Files:**
- Modify: `shared/capital/master_ledger.py`
- Modify: `shared/execution/local_sim_ledger.py`
- Modify: `shared/accounting/sim_ledger.py`
- Modify: `CNFutures/sim_runner.py`
- Modify: `CNFutures/review.py`
- Modify: `Ashare/forward_validation.py`
- Modify: `Ashare/portfolio_evolution.py`
- Modify: `shared/review/sim_ledger_reader.py`
- Modify: `shared/runtime_test/market_health.py`
- Test: `tests/test_epoch_end_to_end.py`

**Interfaces:**
- Every new trade/order/reservation/review/label has `capital_epoch: int` and `master_capital_event_id: str` where money is reserved.
- Produces: `filter_current_epoch(rows: Iterable[dict], current_epoch_id: int) -> tuple[list[dict], list[dict]]`; rejected rows retain reason evidence.

- [ ] **Step 1: Write the cross-ledger RED test**

```python
def test_current_epoch_review_excludes_every_legacy_market_row(tmp_path):
    rows = [
        {"trade_id": "OLD-A", "market": "ashare", "capital_epoch": 1, "realized_pnl": 500.0},
        {"trade_id": "OLD-F", "market": "cn_futures", "capital_epoch": 1, "realized_pnl": 700.0},
        {"trade_id": "NEW-A", "market": "ashare", "capital_epoch": 2, "realized_pnl": -20.0},
    ]
    accepted, rejected = filter_current_epoch(rows, 2)
    assert [row["trade_id"] for row in accepted] == ["NEW-A"]
    assert {row["trade_id"] for row in rejected} == {"OLD-A", "OLD-F"}
    assert all(row["epoch_rejection_reason"] == "capital_epoch_mismatch" for row in rejected)
```

- [ ] **Step 2: Run RED test**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider tests/test_epoch_end_to_end.py -q
```

Expected: FAIL because the common epoch filter does not exist.

- [ ] **Step 3: Add epoch fields at every write boundary**

At order creation, local-sim append, CN-futures receipt, forward label, and review creation, require:

```python
capital_epoch = int(read_epoch_state()["current_epoch_id"])
if capital_epoch != CURRENT_EPOCH_ID:
    raise SafetyViolation("active capital epoch is not current")
payload["capital_epoch"] = capital_epoch
```

Do not infer epoch 2 from a missing field. Missing epoch in an active runtime row is `legacy_epoch_unknown` and excluded.

- [ ] **Step 4: Add current-epoch read filters and diagnostics**

Review readers return accepted current rows plus:

```python
"epoch_filter": {
    "current_epoch_id": 2,
    "accepted_count": int,
    "rejected_count": int,
    "rejection_reasons": dict[str, int],
}
```

No rejected row contributes to cash, PnL, win rate, labels, evolution, or MG ablation.

- [ ] **Step 5: Add health acceptance**

`market_health` fails if:

- current master snapshot epoch differs from the A-share or futures latest review;
- an active current ledger row lacks epoch;
- current review counts include an archived trade ID;
- master reconciliation is false.

- [ ] **Step 6: Run GREEN tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_epoch_end_to_end.py tests/test_local_sim_ledger.py \
  tests/test_cn_futures_review.py tests/test_market_health.py -q
```

Expected: PASS; current-epoch metrics cannot be contaminated by legacy data.

- [ ] **Step 7: Commit**

```bash
git add shared/capital/master_ledger.py shared/execution/local_sim_ledger.py \
  shared/accounting/sim_ledger.py CNFutures/sim_runner.py CNFutures/review.py \
  Ashare/forward_validation.py Ashare/portfolio_evolution.py \
  shared/review/sim_ledger_reader.py shared/runtime_test/market_health.py \
  tests/test_epoch_end_to_end.py
git commit -m "feat(capital): propagate epoch through all validation facts"
```

---

### Task 7: Establish One Cost-Aware PnL and Expectancy Contract

**Files:**
- Create: `shared/review/capital_growth_metrics.py`
- Modify: `shared/execution/local_sim_ledger.py`
- Modify: `shared/accounting/sim_ledger.py`
- Modify: `CNFutures/review.py`
- Modify: `Ashare/portfolio_evolution.py`
- Modify: `shared/review/daily_review.py`
- Test: `tests/test_capital_growth_metrics.py`

**Interfaces:**
- Produces: `build_round_trips(trades: list[dict], *, market: str) -> list[RoundTrip]` using FIFO lots.
- Produces: `capital_growth_metrics(round_trips: list[RoundTrip], equity_curve: list[dict], initial_equity: float) -> dict[str, Any]`.
- Canonical outputs: `gross_pnl`, `commission`, `stamp_duty`, `exchange_fees`, `slippage_cost`, `net_realized_pnl`, `net_expectancy`, `profit_factor`, `win_rate`, `payoff_ratio`, `max_drawdown_pct`, `turnover`, `closed_round_trip_count`, `cost_coverage_pct`.

- [ ] **Step 1: Write exact cost/expectancy RED tests**

```python
def test_net_expectancy_uses_closed_round_trips_after_all_costs():
    trips = [
        RoundTrip("A", gross_pnl=120.0, commission=10.0, stamp_duty=5.0, exchange_fees=0.0, slippage_cost=5.0),
        RoundTrip("B", gross_pnl=-60.0, commission=10.0, stamp_duty=5.0, exchange_fees=0.0, slippage_cost=5.0),
    ]
    metrics = capital_growth_metrics(trips, [], 50_000.0)
    assert metrics["net_realized_pnl"] == 20.0
    assert metrics["net_expectancy"] == 10.0
    assert metrics["profit_factor"] == 100.0 / 80.0
    assert metrics["closed_round_trip_count"] == 2


def test_open_positions_do_not_count_as_realized_expectancy():
    metrics = capital_growth_metrics([], [{"equity": 51_000.0}], 50_000.0)
    assert metrics["net_realized_pnl"] == 0.0
    assert metrics["net_expectancy"] is None
    assert metrics["closed_round_trip_count"] == 0
```

- [ ] **Step 2: Run RED tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider tests/test_capital_growth_metrics.py -q
```

Expected: FAIL because the canonical cost-aware metric module does not exist.

- [ ] **Step 3: Implement FIFO round trips and explicit unknown-cost handling**

Each trip contains:

```python
@dataclass(frozen=True)
class RoundTrip:
    round_trip_id: str
    market: str
    symbol: str
    opened_at: str
    closed_at: str
    gross_pnl: float
    commission: float
    stamp_duty: float
    exchange_fees: float
    slippage_cost: float
    net_pnl: float
    cost_complete: bool
```

Unknown fees are not silently zero for readiness. They set `cost_complete=False`; such trips remain audit facts but do not satisfy the positive-net-expectancy promotion gate.

- [ ] **Step 4: Standardize A-share and futures write fields**

Every fill writes:

```python
"requested_price": float,
"filled_price": float,
"gross_notional": float,
"commission": float,
"stamp_duty": float,
"exchange_fees": float,
"slippage_bps": float,
"slippage_cost": float,
"total_cost": float,
```

The existing append-only fill is never rewritten. New projection code normalizes older current-epoch rows and flags missing costs.

- [ ] **Step 5: Run GREEN tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_capital_growth_metrics.py tests/test_local_sim_ledger.py \
  tests/test_sim_ledger.py tests/test_cn_futures_review.py tests/test_daily_review.py -q
```

Expected: PASS; realized expectancy never includes open-position mark-to-market profit.

- [ ] **Step 6: Commit**

```bash
git add shared/review/capital_growth_metrics.py shared/execution/local_sim_ledger.py \
  shared/accounting/sim_ledger.py CNFutures/review.py Ashare/portfolio_evolution.py \
  shared/review/daily_review.py tests/test_capital_growth_metrics.py
git commit -m "feat(review): measure net capital growth after all costs"
```

---

### Task 8: Add Matched SharedSignals Baseline vs MarketGraph-Enhanced Decisions

**Files:**
- Create: `shared/research/__init__.py`
- Create: `shared/research/decision_context.py`
- Create: `shared/review/mg_ablation.py`
- Modify: `shared/screening/six_dimension_scorer.py`
- Modify: `shared/data/reader.py`
- Modify: `shared/orchestrator.py`
- Test: `tests/test_decision_context.py`
- Test: `tests/test_mg_ablation.py`
- Test: `tests/test_data_reader.py`

**Interfaces:**
- Produces: `score_stock_variant(..., research_mode: Literal["baseline_ss_only", "mg_enhanced"]) -> dict`.
- Produces: `DecisionContext.from_scores(baseline: dict, mg_evidence: dict, as_of: str) -> DecisionContext`.
- Produces only `observe`, `boost`, `downgrade`, `risk_veto`, or `thesis_invalidation_review`.
- Produces: `MatchedDecisionObservation` with one `observation_id`, identical market snapshot hash, baseline result, enhanced result, and exactly one executable lane.

- [ ] **Step 1: Write baseline independence and bounded-overlay RED tests**

```python
def test_baseline_never_calls_marketgraph():
    class ReaderWithMarketGraphThatRaises:
        def get_macro_factors(self, *args, **kwargs):
            return [{"factor_name": "cn_pmi:pmi010000", "value": 52.0}]

        def get_events(self, *args, **kwargs):
            return []

        def get_factors(self, *args, **kwargs):
            return []

        def get_market_data(self, *args, **kwargs):
            return []

        def get_capital_flow(self, *args, **kwargs):
            return []

        def get_sentiment(self, *args, **kwargs):
            return []

        def get_regime(self):
            raise AssertionError("baseline must not call MarketGraph regime")

        def get_event_candidates(self):
            raise AssertionError("baseline must not call MarketGraph events")

    reader = ReaderWithMarketGraphThatRaises()
    score = score_stock_variant(
        "Ashare", "600000.SH", "20260711", reader=reader, research_mode="baseline_ss_only"
    )
    assert score["research_mode"] == "baseline_ss_only"
    assert score["marketgraph_used"] is False


def test_marketgraph_cannot_create_a_candidate_from_baseline_rejection():
    context = DecisionContext.from_scores(
        baseline={"candidate": False, "combined": 0.40},
        mg_evidence={"action": "boost", "confidence": 1.0, "score_delta": 0.30},
        as_of="2026-07-11T10:00:00+08:00",
    )
    assert context.executable_candidate is False
    assert context.action == "observe"
    assert context.reason == "mg_cannot_create_candidate"


def test_stale_marketgraph_context_degrades_to_observe():
    context = DecisionContext.from_scores(
        baseline={"candidate": True, "combined": 0.65},
        mg_evidence={"action": "boost", "as_of": "2026-07-10T09:00:00+08:00", "ttl_seconds": 300},
        as_of="2026-07-11T10:00:00+08:00",
    )
    assert context.action == "observe"
    assert context.reason == "marketgraph_context_stale"
```

- [ ] **Step 2: Run RED tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_decision_context.py tests/test_mg_ablation.py -q
```

Expected: FAIL because current MG behavior is a fallback inside dimensions, not an explicit matched variant.

- [ ] **Step 3: Split SS baseline scoring from MG overlay**

Baseline dimensions consume only SharedSignals. Enhanced mode starts with the exact baseline object, then loads MG regime/impact evidence and applies bounded annotations:

```python
ALLOWED_MG_ACTIONS = {
    "observe",
    "boost",
    "downgrade",
    "risk_veto",
    "thesis_invalidation_review",
}
MAX_ABSOLUTE_SCORE_DELTA = 0.10
```

MG cannot add a symbol to the candidate layer, relax price/liquidity/session/capital/risk gates, or write an order.

- [ ] **Step 4: Generate matched counterfactual observations**

```python
observation_id = sha256(
    f"{market}|{symbol}|{bar_time}|{market_snapshot_sha256}|{capital_epoch}".encode()
).hexdigest()
```

Persist both variants under `shared/review/experiments/mg_ablation.jsonl`. Initially set:

```python
"executable_variant": "baseline_ss_only",
"counterfactual_variant": "mg_enhanced",
"real_trading_enabled": False,
```

Only the executable variant can enter simulated execution; the counterfactual never creates a second fill or reserves capital.

- [ ] **Step 5: Add source/version/TTL evidence**

Each enhanced decision records MG endpoint, evidence IDs, API schema/version, `as_of`, TTL, confidence, requested action, applied action, and rejection reason. API failures preserve the baseline and append `marketgraph_evidence_debt`.

- [ ] **Step 6: Run GREEN tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_decision_context.py tests/test_mg_ablation.py tests/test_data_reader.py \
  tests/test_six_dimension_scorer_matching.py tests/test_sim_loop.py -q
```

Expected: PASS; baseline works with MG absent, and MG remains bounded research context.

- [ ] **Step 7: Commit**

```bash
git add shared/research shared/review/mg_ablation.py shared/screening/six_dimension_scorer.py \
  shared/data/reader.py shared/orchestrator.py tests/test_decision_context.py \
  tests/test_mg_ablation.py tests/test_data_reader.py
git commit -m "feat(research): measure bounded MarketGraph decision value"
```

---

### Task 9: Add Capital-Growth KPI and Simulation Promotion Gates

**Files:**
- Create: `shared/review/capital_growth_gate.py`
- Modify: `shared/review/goals.yaml`
- Modify: `shared/review/daily_review.py`
- Modify: `shared/review/weekly_review.py`
- Modify: `shared/runtime_test/self_evolution_health.py`
- Test: `tests/test_capital_growth_gate.py`
- Test: `tests/test_self_evolution_health.py`

**Interfaces:**
- Produces: `evaluate_capital_growth_gate(metrics: dict, evidence: dict, mg_ablation: dict) -> dict`.
- States: `blocked_data`, `collecting_samples`, `initial_evidence`, `out_of_sample_validation`, `simulation_scale_candidate`; no state enables real trading.

- [ ] **Step 1: Write RED tests for false profitability claims**

```python
def test_positive_unrealized_pnl_cannot_pass_capital_growth_gate():
    gate = evaluate_capital_growth_gate(
        metrics={"net_realized_pnl": 0.0, "net_expectancy": None, "closed_round_trip_count": 0},
        evidence={"verified_execution_samples": 20, "forward_60m_labels": 20},
        mg_ablation={},
    )
    assert gate["state"] == "collecting_samples"
    assert "positive_net_realized_expectancy_missing" in gate["blockers"]
    assert gate["real_trading_enabled"] is False


def test_initial_evidence_requires_all_existing_20_10_20_gates():
    gate = evaluate_capital_growth_gate(
        metrics={
            "net_realized_pnl": 100.0,
            "net_expectancy": 10.0,
            "profit_factor": 1.2,
            "max_drawdown_pct": 0.02,
            "closed_round_trip_count": 10,
            "cost_coverage_pct": 1.0,
        },
        evidence={"verified_execution_samples": 20, "forward_60m_labels": 20, "capital_epoch": 2},
        mg_ablation={},
    )
    assert gate["state"] == "initial_evidence"
    assert gate["simulation_scale_allowed"] is False
```

- [ ] **Step 2: Run RED tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider tests/test_capital_growth_gate.py -q
```

Expected: FAIL because no unified gate exists.

- [ ] **Step 3: Replace active goals with evidence stages**

```yaml
capital_growth_validation:
  initial_evidence:
    verified_execution_samples_min: 20
    realized_round_trips_min: 10
    forward_60m_labels_min: 20
    net_realized_pnl: positive
    net_expectancy: positive
    cost_coverage_pct_min: 1.0
  out_of_sample_validation:
    independent_closed_round_trips_min: 50
    distinct_market_regimes_min: 2
    net_expectancy: positive
    profit_factor_min: 1.0
    max_drawdown_pct_max: 0.07
  simulation_scale_candidate:
    independent_closed_round_trips_min: 100
    positive_out_of_sample_windows_min: 3
    max_drawdown_pct_max: 0.07
    real_trading_enabled: false
```

Remove automatic `sim -> shadow -> real -> scaled` progression. A future real-money phase requires a separate explicit user authorization and design.

- [ ] **Step 4: Implement blockers and confidence reporting**

The gate returns raw numerator/denominator counts, Wilson win-rate interval, bootstrap expectancy interval when at least 20 closed trips exist, and every blocker. If the interval crosses zero, state cannot exceed `out_of_sample_validation`.

- [ ] **Step 5: Add MG value metrics without making them mandatory for baseline**

Report matched count, baseline net outcome, enhanced counterfactual net outcome, avoided losses, missed gains, and `mg_delta_expectancy`. Missing MG evidence is `not_measured`, not a baseline failure.

- [ ] **Step 6: Run GREEN tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_capital_growth_gate.py tests/test_daily_review.py tests/test_weekly_review.py \
  tests/test_self_evolution_health.py -q
```

Expected: PASS; no KPI or evolution report equates chain health, floating profit, or tiny samples with repeatable profit.

- [ ] **Step 7: Commit**

```bash
git add shared/review/capital_growth_gate.py shared/review/goals.yaml \
  shared/review/daily_review.py shared/review/weekly_review.py \
  shared/runtime_test/self_evolution_health.py tests/test_capital_growth_gate.py \
  tests/test_self_evolution_health.py
git commit -m "feat(review): gate capital growth on net realized evidence"
```

---

### Task 10: Record Pre-Gate Candidate Observations and Independent Forward Labels

**Files:**
- Create: `shared/review/prediction_observations.py`
- Create: `shared/review/forward_label_contract.py`
- Modify: `shared/orchestrator.py`
- Modify: `Ashare/forward_validation.py`
- Test: `tests/test_prediction_observations.py`
- Test: `tests/test_forward_label_contract.py`

**Interfaces:**
- `record_candidate_observation(path, observation, *, now) -> dict` appends one idempotent snapshot before strategy/execution gates.
- `due_label_horizons(observed_at, session_calendar, *, now) -> tuple[str, ...]` returns only matured horizons from `m30`, `m60`, `close`, `next_day`, `d3`, and `d5`.
- `build_forward_label(observation, horizon, price_evidence) -> dict` requires trustworthy price evidence but does not require an executed trade.

- [ ] **Step 1: Write failing pre-gate and label-independence tests**

```python
def test_data_qualified_candidate_is_recorded_when_strategy_rejects(tmp_path):
    item = record_candidate_observation(
        tmp_path / "observations.jsonl",
        {
            "market": "ashare",
            "symbol": "600000.SH",
            "prediction_ts": "2026-07-13T09:31:00+08:00",
            "strategy_version": "ashare-v2",
            "score": 0.41,
            "probability": 0.54,
            "market_regime": "risk_off",
            "mg_enabled": False,
            "sample_intent": "observe",
            "data_quality": "qualified",
            "execution_decision": "hold",
            "no_trade_reason": "score_below_exploitation_threshold",
        },
        now="2026-07-13T09:31:01+08:00",
    )
    assert item["label_eligible"] is True
    assert item["no_trade_reason"] == "score_below_exploitation_threshold"


def test_execution_gate_does_not_block_due_forward_label():
    label = build_forward_label(
        {"observation_id": "obs-1", "entry_price": 10.0, "execution_decision": "hold"},
        "m60",
        {"price": 10.2, "source": "sharedsignals", "quality": "qualified"},
    )
    assert label["return_bps"] == 200.0
    assert label["executed"] is False
```

- [ ] **Step 2: Run RED tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_prediction_observations.py tests/test_forward_label_contract.py -q
```

Expected: FAIL because the observation and horizon-label contracts do not exist.

- [ ] **Step 3: Implement the append-only observation contract**

```python
REQUIRED_FIELDS = {
    "market", "symbol", "prediction_ts", "strategy_version", "score",
    "probability", "market_regime", "mg_enabled", "sample_intent",
    "data_quality", "execution_decision", "no_trade_reason",
}
VALID_INTENTS = {"observe", "counterfactual", "exploration", "exploitation"}

def observation_id(payload: Mapping[str, Any]) -> str:
    identity = "|".join(str(payload[key]) for key in (
        "market", "symbol", "prediction_ts", "strategy_version", "mg_enabled"
    ))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()

def label_eligible(payload: Mapping[str, Any]) -> bool:
    return payload.get("data_quality") == "qualified"
```

Append under an exclusive file lock and return the existing row when the same `observation_id` is retried. Reject missing fields, invalid intents, and an empty no-trade reason for non-executed observations.

- [ ] **Step 4: Implement matured, idempotent forward labels**

```python
HORIZONS = ("m30", "m60", "close", "next_day", "d3", "d5")

def build_forward_label(observation, horizon, price_evidence):
    if horizon not in HORIZONS:
        raise ValueError("unsupported_label_horizon")
    if observation.get("label_eligible", True) is not True:
        raise ValueError("observation_not_label_eligible")
    if price_evidence.get("quality") != "qualified" or not price_evidence.get("source"):
        raise ValueError("unreliable_price_evidence")
    entry = float(observation["entry_price"])
    price = float(price_evidence["price"])
    return {
        "label_id": f'{observation["observation_id"]}:{horizon}',
        "observation_id": observation["observation_id"],
        "horizon": horizon,
        "return_bps": round((price / entry - 1.0) * 10_000.0, 4),
        "executed": observation.get("execution_decision") == "sim_trade",
        "source": price_evidence["source"],
    }
```

Use the existing exchange calendar to determine maturity; never replace session-aware maturity with elapsed wall-clock days.

- [ ] **Step 5: Wire observation before thresholds and run regressions**

Call `record_candidate_observation` after data-quality qualification and before exploitation/exploration selection. Then run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_prediction_observations.py tests/test_forward_label_contract.py \
  tests/test_ashare_forward_validation.py tests/test_sim_loop.py -q
```

Expected: PASS; strategy rejection does not suppress observation or future labels, while unreliable data remains label-ineligible.

- [ ] **Step 6: Commit**

```bash
git add shared/review/prediction_observations.py shared/review/forward_label_contract.py \
  shared/orchestrator.py Ashare/forward_validation.py \
  tests/test_prediction_observations.py tests/test_forward_label_contract.py
git commit -m "feat(review): record pre-gate predictions and forward labels"
```

---

### Task 11: Add Orthogonal A-share Style Shadows and One-Account Arbitration

**Files:**
- Create: `Ashare/style_hypotheses.py`
- Create: `Ashare/style_shadow_ledger.py`
- Create: `Ashare/portfolio_arbiter.py`
- Create: `Ashare/style_promotion.py`
- Modify: `shared/orchestrator.py`
- Modify: `shared/execution/local_sim_ledger.py`
- Modify: `Ashare/research_evidence.py`
- Test: `tests/test_ashare_style_hypotheses.py`
- Test: `tests/test_ashare_style_shadow_ledger.py`
- Test: `tests/test_ashare_portfolio_arbiter.py`
- Test: `tests/test_ashare_style_promotion.py`

**Interfaces:**
- `audit_style_hypotheses(strategy_configs) -> dict` maps existing configurations to a bounded family registry and rejects duplicate threshold-only families.
- `record_style_predictions(path, *, candidate, predictions, decision_context) -> list[dict]` records every style's prediction without assigning capital.
- `arbitrate_candidate(*, candidate, style_predictions, account, risk_context, policy) -> dict` emits zero or one order decision for the single account.
- `evaluate_style_status(*, style_evidence, current_status, gates, as_of) -> dict` emits an append-only champion/challenger/pause decision.

- [ ] **Step 1: Write failing orthogonality and no-virtual-capital tests**

```python
def test_initial_style_registry_contains_distinct_hypothesis_families():
    result = audit_style_hypotheses(load_existing_ashare_strategy_configs())
    assert set(result["active_families"]) == {
        "trend_strength_continuation",
        "pullback_short_horizon_reversal",
        "event_catalyst_confirmation",
        "defensive_low_vol_abstain",
    }
    assert result["independent_execution_accounts"] == 0
    assert result["duplicate_threshold_variants"] == []


def test_shadow_predictions_never_allocate_or_sum_capital(tmp_path):
    rows = record_style_predictions(
        tmp_path / "style_predictions.jsonl",
        candidate={"symbol": "600000.SH", "prediction_ts": "2026-07-13T09:31:00+08:00"},
        predictions=[
            {"style": "trend_strength_continuation", "score": 0.66},
            {"style": "pullback_short_horizon_reversal", "score": 0.31},
        ],
        decision_context={"master_equity": 50_000.0, "decision_policy_version": "arbiter-v1"},
    )
    assert len(rows) == 2
    assert all("capital" not in row and "virtual_budget" not in row for row in rows)
```

- [ ] **Step 2: Run RED tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_ashare_style_hypotheses.py tests/test_ashare_style_shadow_ledger.py -q
```

Expected: FAIL because bounded hypothesis families and the capital-free shadow ledger do not exist.

- [ ] **Step 3: Implement the audited family registry and full prediction contract**

The registry must map the current strategy files by their actual entry logic, horizon, catalyst dependency, and risk thesis. A style is not independently active merely because it has a different score threshold or stop percentage. Each shadow row must contain:

```python
{
    "style_prediction_id": str,
    "observation_id": str,
    "symbol": str,
    "prediction_ts": str,
    "style_family": str,
    "style_version": str,
    "entry_thesis": str,
    "exit_thesis": str,
    "holding_horizon": str,
    "expected_return_distribution": {
        "p10": float, "p50": float, "p90": float, "probability_positive": float
    },
    "requested_risk_budget": float,
    "prediction": "long" | "abstain" | "reject",
    "abstain_or_reject_reason": str | None,
    "mg_enabled": bool,
    "decision_policy_version": str,
}
```

The ledger is append-only and idempotent by `(observation_id, style_family, style_version)`. It contains no equity, balance, or independently spendable budget field.

- [ ] **Step 4: Write failing one-order arbitration and attribution tests**

```python
def test_conflicting_styles_produce_one_order_and_preserve_disagreement():
    result = arbitrate_candidate(
        candidate={"symbol": "600000.SH", "trade_date": "20260713"},
        style_predictions=[
            {"style_family": "trend_strength_continuation", "style_version": "v3", "score": 0.72, "prediction": "long"},
            {"style_family": "pullback_short_horizon_reversal", "style_version": "v2", "score": 0.64, "prediction": "abstain"},
        ],
        account={"account_id": "ashare-sim-epoch-2", "equity": 50_000.0},
        risk_context=all_safety_gates_pass(),
        policy={"version": "arbiter-v1"},
    )
    assert len(result["orders"]) == 1
    assert result["orders"][0]["primary_style"] == "trend_strength_continuation"
    assert result["orders"][0]["supporting_styles"] == []
    assert result["style_disagreement"] is True


def test_same_symbol_same_day_is_idempotent_across_styles():
    first = arbitrate_and_reserve(symbol="600000.SH", trade_date="20260713", styles=["trend_strength_continuation"])
    retry = arbitrate_and_reserve(symbol="600000.SH", trade_date="20260713", styles=["event_catalyst_confirmation"])
    assert first["order_id"] == retry["order_id"]
    assert retry["new_orders_created"] == 0
```

- [ ] **Step 5: Implement the single-account arbiter and immutable fill attribution**

The idempotency key is derived from `(capital_epoch, account_id, market, symbol, trade_date, side, decision_policy_version)`, never from style name. The arbiter first resolves style status, disagreement, correlation, and combined thesis, then invokes the existing immutable risk gates once. A fill/receipt must preserve:

```python
{
    "primary_style": "trend_strength_continuation",
    "supporting_styles": ["event_catalyst_confirmation"],
    "style_scores": {
        "trend_strength_continuation": 0.72,
        "event_catalyst_confirmation": 0.67,
        "pullback_short_horizon_reversal": 0.31,
    },
    "style_versions": {
        "trend_strength_continuation": "v3",
        "event_catalyst_confirmation": "v1",
        "pullback_short_horizon_reversal": "v2",
    },
    "decision_policy_version": "arbiter-v1",
}
```

Every unselected style prediction remains forward-label eligible. Style conflict is stored as a feature; it is not collapsed into the winning style.

- [ ] **Step 6: Write failing champion/challenger tests**

```python
def test_challenger_cannot_receive_exploitation_weight_without_all_gates():
    result = evaluate_style_status(
        style_evidence={"oos_round_trips": 8, "net_expectancy": 12.0,
                        "max_drawdown": -0.03, "calibration_error": 0.07},
        current_status="challenger",
        gates={"min_oos_round_trips": 10, "min_net_expectancy": 0.0,
               "max_drawdown": -0.10, "max_calibration_error": 0.10},
        as_of="2026-07-13T15:10:00+08:00",
    )
    assert result["next_status"] == "challenger"
    assert result["execution_channel"] in {"shadow", "exploration"}


def test_failed_champion_is_paused_without_rewriting_history():
    result = evaluate_style_status(
        style_evidence={"oos_round_trips": 20, "net_expectancy": -5.0,
                        "max_drawdown": -0.12, "calibration_error": 0.14},
        current_status="champion",
        gates=promotion_gates(),
        as_of="2026-07-13T15:10:00+08:00",
    )
    assert result["next_status"] == "paused"
    assert result["history_action"] == "append"
```

- [ ] **Step 7: Implement append-only promotion and run GREEN regressions**

Promotion requires all existing sample-quality gates plus out-of-sample completed round trips, net expectancy after fees/slippage, maximum drawdown, and calibration thresholds. Challengers may be shadow or exploration but never exploitation. Then run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_ashare_style_hypotheses.py tests/test_ashare_style_shadow_ledger.py \
  tests/test_ashare_portfolio_arbiter.py tests/test_ashare_style_promotion.py \
  tests/test_local_sim_ledger.py tests/test_ashare_research_evidence.py \
  tests/test_sim_loop.py -q
```

Expected: PASS; all styles predict, only the arbiter can reserve one account order, fills retain full attribution, and no style creates capital.

- [ ] **Step 8: Commit**

```bash
git add Ashare/style_hypotheses.py Ashare/style_shadow_ledger.py \
  Ashare/portfolio_arbiter.py Ashare/style_promotion.py Ashare/research_evidence.py \
  shared/orchestrator.py shared/execution/local_sim_ledger.py \
  tests/test_ashare_style_hypotheses.py tests/test_ashare_style_shadow_ledger.py \
  tests/test_ashare_portfolio_arbiter.py tests/test_ashare_style_promotion.py
git commit -m "feat(ashare): add style shadows and one-account arbitration"
```

---

### Task 12: Add A-share Safe Exploration Without Relaxing Safety Gates

**Files:**
- Create: `Ashare/exploration_policy.py`
- Modify: `shared/orchestrator.py`
- Modify: `shared/capital/policy.py`
- Test: `tests/test_ashare_exploration_policy.py`
- Test: `tests/test_sim_loop.py`

**Interfaces:**
- `select_exploration_candidate(candidates, *, normal_trade_count, sample_debt, quantile) -> dict | None` selects only from safety-qualified candidates and ranks within the current cohort.
- `derive_exploration_order(candidate, master_snapshot, risk_policy, costs) -> dict` returns zero shares with a concrete immutable safety reason or a 100-share-lot simulation order tagged `sample_intent=exploration`.

- [ ] **Step 1: Write failing exploration and immutable-gate tests**

```python
def test_sample_debt_can_select_one_safe_ranked_exploration_candidate():
    candidates = [
        {"symbol": "A", "score": 0.43, "safety_qualified": True},
        {"symbol": "B", "score": 0.51, "safety_qualified": True},
    ]
    picked = select_exploration_candidate(
        candidates, normal_trade_count=0, sample_debt=12, quantile=0.5
    )
    assert picked["symbol"] == "B"
    assert picked["sample_intent"] == "exploration"


def test_exploration_never_bypasses_t_plus_one_or_daily_loss_gate():
    blocked = select_exploration_candidate(
        [{"symbol": "B", "score": 0.9, "safety_qualified": False,
          "safety_reasons": ["t_plus_one", "exploration_daily_loss_limit"]}],
        normal_trade_count=0, sample_debt=12, quantile=0.0,
    )
    assert blocked is None


def test_exploration_budget_is_derived_from_master_cap_and_board_lot():
    order = derive_exploration_order(
        {"symbol": "600000.SH", "price": 31.0},
        {"equity": 50_000.0, "available_cash": 50_000.0,
         "ashare_deployable_remaining": 30_000.0},
        {"single_name_weight": 0.15, "daily_loss_limit": 0.03,
         "max_positions": 5, "board_lot": 100, "stop_loss_bps": 300},
        {"commission_bps": 3, "slippage_bps": 5},
    )
    assert order["exploration_daily_loss_budget"] == 300.0
    assert order["exploration_total_exposure_cap"] == 7_500.0
    assert order["shares"] == 200
    assert order["notional"] <= 7_500.0
```

- [ ] **Step 2: Run RED tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_ashare_exploration_policy.py -q
```

Expected: FAIL because exploration selection and derived sizing do not exist.

- [ ] **Step 3: Implement relative selection and derived sizing**

```python
portfolio_daily_loss_budget = snapshot.equity * policy.daily_loss_limit
exploration_daily_loss_budget = portfolio_daily_loss_budget / policy.max_positions
modeled_loss_rate = (
    policy.stop_loss_bps + costs.buy_cost_bps + costs.slippage_bps
) / 10_000.0
exploration_total_exposure_cap = min(
    snapshot.ashare_deployable_remaining,
    snapshot.equity * policy.single_name_weight,
    exploration_daily_loss_budget / modeled_loss_rate,
)
hard_cap = min(
    snapshot.available_cash,
    exploration_total_exposure_cap - snapshot.current_exploration_exposure,
)
per_share_loss = price * (policy.stop_loss_bps + costs.slippage_bps) / 10_000.0
loss_budget_remaining = exploration_daily_loss_budget - snapshot.exploration_daily_loss
loss_limited_shares = int(loss_budget_remaining // per_share_loss)
cash_limited_shares = int(hard_cap // (price * (1.0 + costs.buy_cost_bps / 10_000.0)))
shares = min(loss_limited_shares, cash_limited_shares)
shares = (shares // policy.board_lot) * policy.board_lot
```

This creates an independent exploration loss budget by dividing the already-approved portfolio daily-loss allowance across the existing maximum-position capacity; it then converts that risk unit into an exposure cap using the strategy stop and modeled costs. Do not introduce a new percentage. Read every input from the existing validated risk policy/master snapshot; if any input is absent or non-positive, fail closed with `exploration_budget_missing`. Enforce one new exploration position per A-share trading day through the append-only reservation ledger and idempotency key.

- [ ] **Step 4: Separate exploration from exploitation accounting**

Every simulated fill, reservation, round trip, and review row must preserve:

```python
{"sample_intent": "exploration", "promotion_eligible": False}
```

`promotion_eligible` becomes true only in the later promotion evaluator after the normal evidence gates pass; it is not set by the order path.

- [ ] **Step 5: Run GREEN and safety regressions**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_ashare_exploration_policy.py tests/test_sim_loop.py \
  tests/test_ashare_capital_plan.py tests/test_master_capital_ledger.py -q
```

Expected: PASS; a safe candidate can create at most one tiny simulated position, and every immutable gate still blocks it.

- [ ] **Step 6: Commit**

```bash
git add Ashare/exploration_policy.py shared/orchestrator.py shared/capital/policy.py \
  tests/test_ashare_exploration_policy.py tests/test_sim_loop.py
git commit -m "feat(ashare): add risk-bounded exploration samples"
```

---

### Task 13: Record Every CN-Futures Session and Separate Executable From Counterfactual

**Files:**
- Create: `CNFutures/session_observations.py`
- Modify: `CNFutures/sim_runner.py`
- Modify: `CNFutures/observation_report.py`
- Test: `tests/test_cn_futures_session_observations.py`
- Test: `tests/test_cn_futures_sim_runner.py`

- [ ] **Step 1: Write failing session-completeness and one-lot tests**

```python
def test_each_valid_session_emits_a_replayable_decision(tmp_path):
    result = record_session_decision(
        tmp_path / "sessions.jsonl",
        market_session="2026-07-13:day-am",
        candidate={"contract": "IF2607", "direction": "long", "score": 0.62},
        decision="risk_reject",
        reason="minimum_lot_risk_exceeds_budget",
        executable=False,
    )
    assert result["sample_intent"] == "counterfactual"
    assert result["execution_class"] == "counterfactual_only"


def test_unaffordable_contract_stays_zero_quantity_but_remains_labelable():
    result = classify_contract(
        minimum_lots=1, margin_required=18_000.0, modeled_stop_loss=900.0,
        margin_budget=5_000.0, risk_budget=250.0,
    )
    assert result["quantity"] == 0
    assert result["execution_class"] == "counterfactual_only"
    assert result["label_eligible"] is True
```

- [ ] **Step 2: Run RED tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_cn_futures_session_observations.py tests/test_cn_futures_sim_runner.py -q
```

Expected: FAIL because session-level replay records and execution classification are absent.

- [ ] **Step 3: Implement session idempotency and execution classification**

```python
session_decision_id = hashlib.sha256(
    f"{market_session}|{contract}|{strategy_version}|{direction}".encode("utf-8")
).hexdigest()
executable = (
    minimum_lots == 1
    and margin_required <= margin_budget
    and modeled_stop_loss <= risk_budget
    and all(immutable_gate_results.values())
)
quantity = 1 if executable else 0
execution_class = "sim_executable" if executable else "counterfactual_only"
```

The record must include session, strategy version, prediction timestamp, direction, score/probability, MG on/off, contract multiplier, minimum lot, margin, stop risk, slippage, night-gap allowance, losing-streak state, decision, reason, and later label IDs.

- [ ] **Step 4: Require one record per valid session**

Register a session-completeness check against the existing exchange calendar. A normal trade, hold, or concrete risk rejection all satisfy the record requirement. A closed/non-trading session is explicitly `not_applicable`, not a missing record.

- [ ] **Step 5: Run GREEN and affordability regressions**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_cn_futures_session_observations.py tests/test_cn_futures_sim_runner.py \
  tests/test_cn_futures_observation_report.py tests/test_multi_market_p2_tools.py -q
```

Expected: PASS; unaffordable contracts remain quantity zero and labelable, while affordable contracts still obey every existing futures gate.

- [ ] **Step 6: Commit**

```bash
git add CNFutures/session_observations.py CNFutures/sim_runner.py \
  CNFutures/observation_report.py tests/test_cn_futures_session_observations.py \
  tests/test_cn_futures_sim_runner.py
git commit -m "feat(cnfutures): preserve session counterfactual samples"
```

---

### Task 14: Prove Sample Flow at Preopen, Opening, and Review

**Files:**
- Create: `shared/runtime_test/sample_flow_acceptance.py`
- Modify: `shared/runtime_test/opening_acceptance.py`
- Modify: `shared/runtime_test/full_acceptance.py`
- Modify: `shared/review/daily_review.py`
- Modify: `shared/review/weekly_review.py`
- Test: `tests/test_sample_flow_acceptance.py`
- Test: `tests/test_opening_acceptance.py`
- Test: `tests/test_daily_review_driver.py`

- [ ] **Step 1: Write failing no-silent-zero and separated-metrics tests**

```python
def test_sample_debt_alone_cannot_explain_zero_exploration():
    result = evaluate_sample_flow(
        qualified_candidates=3, observations=3, exploration_trades=0,
        exploration_block_reasons=["sample_debt"], due_labels=2, written_labels=2,
    )
    assert result["status"] == "blocked"
    assert result["reason"] == "invalid_zero_exploration_reason"


def test_review_separates_intents_and_reports_gate_distribution():
    metrics = summarize_sample_channels([
        {"sample_intent": "observe", "gate_reason": "score", "net_pnl": 0.0},
        {"sample_intent": "exploration", "gate_reason": None, "net_pnl": -5.0},
        {"sample_intent": "exploitation", "gate_reason": None, "net_pnl": 20.0},
    ])
    assert metrics["counts"] == {"observe": 1, "exploration": 1, "exploitation": 1}
    assert metrics["gate_distribution"] == {"score": 1}
    assert metrics["exploration"]["net_pnl"] == -5.0


def test_style_dashboard_does_not_sum_shadow_capital():
    report = summarize_style_evidence(
        master_account={"equity": 50_000.0, "risk_used": 1_200.0},
        style_rows=[
            {"style_family": "trend_strength_continuation", "sample_type": "prediction"},
            {"style_family": "pullback_short_horizon_reversal", "sample_type": "counterfactual"},
        ],
    )
    assert report["master_account"]["equity"] == 50_000.0
    assert all("equity" not in row for row in report["styles"])
```

- [ ] **Step 2: Run RED tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_sample_flow_acceptance.py tests/test_opening_acceptance.py \
  tests/test_daily_review_driver.py -q
```

Expected: FAIL because sample-flow acceptance and channel-separated review metrics do not exist.

- [ ] **Step 3: Implement fail-closed sample-flow acceptance**

The acceptance payload must contain:

```python
{
    "qualified_candidates": int,
    "observations_added": int,
    "labels_due": int,
    "labels_written": int,
    "channel_counts": dict,
    "sample_type_counts": {
        "observation_counterfactual": int,
        "exploration_fills": int,
        "exploitation_fills": int,
        "completed_round_trips": int,
        "exit_stop_samples": int,
        "safety_risk_rejections": int,
        "chain_validation": int,
    },
    "style_metrics": dict,
    "master_account_equity": 50_000.0,
    "master_account_total_risk": float,
    "completed_round_trips": int,
    "exploration_zero_reason": str | None,
    "futures_session_expected": int,
    "futures_session_recorded": int,
    "counterfactual_only_count": int,
    "sim_executable_count": int,
    "gate_distribution": dict,
    "real_trading_enabled": False,
}
```

Zero exploration passes only when there are no data-qualified candidates or at least one concrete immutable safety gate blocked every otherwise eligible candidate. Missing due labels, missing valid-session futures records, or `REAL_TRADING_ENABLED != false` blocks acceptance.

- [ ] **Step 4: Extend daily and weekly evidence metrics**

For each intent and each active/shadow style family report candidates, predictions, exploration fills, exploitation fills, completed round trips, exit/stop samples, safety/risk rejections, chain-validation samples, matured `m30/m60/close/next_day/d3/d5` labels, wins, losses, win rate, average win, average loss, expectancy, fees, slippage, net PnL, maximum drawdown, calibration error, and rejection distribution. Never combine exploration and exploitation expectancy into the promotion metric. Style rows contain attribution metrics only; portfolio equity, cash, exposure, and risk appear once under `master_account` and must never be summed from styles.

- [ ] **Step 5: Register preopen/opening/ops checks and run GREEN**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_sample_flow_acceptance.py tests/test_opening_acceptance.py \
  tests/test_full_acceptance.py tests/test_daily_review_driver.py \
  tests/test_weekly_review.py -q
```

Expected: PASS; fixtures prove qualified observations, due labels, futures session decisions, channel separation, and fail-closed real-trading isolation.

- [ ] **Step 6: Commit**

```bash
git add shared/runtime_test/sample_flow_acceptance.py \
  shared/runtime_test/opening_acceptance.py shared/runtime_test/full_acceptance.py \
  shared/review/daily_review.py shared/review/weekly_review.py \
  tests/test_sample_flow_acceptance.py tests/test_opening_acceptance.py \
  tests/test_daily_review_driver.py
git commit -m "feat(acceptance): prevent silent zero-sample regressions"
```

---

### Task 15: Consolidate Canonical Documentation and Retire the Old System Safely

**Files:**
- Create: `docs/architecture.md`
- Create: `docs/capital_growth_validation.md`
- Create: `docs/operations.md`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `STATUS.md`
- Modify: `Ashare/AGENTS.md`
- Modify: `CNFutures/AGENTS.md`
- Modify: `shared/AGENTS.md`
- Modify: `shared/risk/AGENTS.md`
- Modify: `shared/review/AGENTS.md`
- Modify: `docs/AGENTS.md`
- Modify: `docs/data_contract.md`
- Modify: `docs/data_sources.md`
- Modify: `docs/write_end_contract.md`
- Modify: `docs/INFRASTRUCTURE.md`
- Delete after dependency scan: files listed in “Retired after dependency proof” above.
- Test: `tests/test_documentation_contract.py`

**Interfaces:**
- `README.md` is the short product entry.
- `AGENTS.md` contains permanent operational/risk rules only.
- `STATUS.md` contains current evidence, blockers, last verification, and next gate only.
- `docs/architecture.md`, `docs/capital_growth_validation.md`, `docs/data_contract.md`, and `docs/operations.md` are the only long-form canonical architecture/validation/operations documents.

- [ ] **Step 1: Write the documentation contract RED test**

```python
def test_canonical_docs_exist_and_retired_docs_are_absent():
    ROOT = Path(__file__).resolve().parents[1]
    required = {
        "README.md",
        "AGENTS.md",
        "STATUS.md",
        "docs/architecture.md",
        "docs/capital_growth_validation.md",
        "docs/data_contract.md",
        "docs/operations.md",
    }
    retired = {
        "shared/orchestrator_design.md",
        "docs/archive/BATCH_PLAN_20260630.md",
        "docs/archive/cron_gap_20260629.md",
        "docs/runtime_incidents_20260701.md",
        "docs/runtime_incidents_20260702.md",
        "shared/signals/signal_cards.jsonl",
        "shared/signals/pm/pm_forward_signals.jsonl",
    }
    assert all((ROOT / path).exists() for path in required)
    assert all(not (ROOT / path).exists() for path in retired)


def test_active_docs_do_not_restore_retired_write_paths():
    ROOT = Path(__file__).resolve().parents[1]
    CANONICAL_DOCS = (
        "README.md",
        "AGENTS.md",
        "STATUS.md",
        "docs/architecture.md",
        "docs/capital_growth_validation.md",
        "docs/data_contract.md",
        "docs/operations.md",
    )
    active_text = "\n".join((ROOT / path).read_text() for path in CANONICAL_DOCS)
    for forbidden in ("executions/sim/", "SharedSignals.backtest_cache", "local_sim_tiers"):
        assert forbidden not in active_text
```

- [ ] **Step 2: Run RED tests and dependency scan before deletion**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider tests/test_documentation_contract.py -q
rg -n "orchestrator_design|BATCH_PLAN_20260630|cron_gap_20260629|runtime_incidents_2026070[12]|shared/signals/signal_cards|shared/signals/pm/pm_forward_signals|tier_experiments" \
  --glob '!docs/superpowers/plans/2026-07-11-50000-capital-growth-engine.md' .
```

Expected: test fails because canonical docs are not consolidated and retired files still exist. The dependency scan output is reviewed before applying any delete.

- [ ] **Step 3: Write the canonical documents**

Each document must include:

- purpose and source of truth;
- current capital epoch and simulation-only boundary;
- SS/MG/TA ownership;
- master-capital reconciliation;
- A-share and futures affordability/risk gates;
- MG baseline/enhanced experiment boundary;
- exact local and production read-only verification commands;
- rollback/disable paths;
- current known limitations, including no profitability guarantee and incomplete live order-book/forced-liquidation modeling.

- [ ] **Step 4: Compress `STATUS.md` to current evidence**

Use this stable section structure:

```markdown
# TradingAgent Current Status

## Objective and boundary
## Current capital epoch
## Current A-share evidence
## Current CN-futures evidence
## Master-capital reconciliation
## MarketGraph ablation evidence
## Acceptance results
## Active blockers and next gate
## Last verified Git/production/runtime state
```

Do not copy historical release diaries into the current file; Git history remains the audit trail.

- [ ] **Step 5: Remove retired tracked artifacts and old tier integration**

Use `apply_patch` deletions for tracked files. Before removing `Ashare/tier_experiments.py`, delete current production imports and cron-coverage expectations while preserving `tools/migrate_sim_capital_epoch.py` support for archiving an already existing legacy tier directory. Keep immutable epoch archive evidence outside active review inputs.

- [ ] **Step 6: Re-run dependency and stale-language scans**

```bash
rg -n "executions/sim/|SharedSignals\.backtest_cache|local_sim_tiers|automatic real|自动实盘|稳定盈利|保证收益" \
  README.md AGENTS.md STATUS.md docs Ashare/AGENTS.md CNFutures/AGENTS.md shared/AGENTS.md
rg -n "shared/signals/signal_cards|shared/signals/pm/pm_forward_signals|Ashare\.tier_experiments" \
  --glob '*.py' --glob '*.sh' --glob '*.md' .
```

Expected: no active dependency on deleted facts; mentions of “stable profit” appear only as a prohibited claim or measurement objective.

- [ ] **Step 7: Run GREEN tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider tests/test_documentation_contract.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add -A README.md AGENTS.md STATUS.md docs Ashare/AGENTS.md CNFutures/AGENTS.md \
  shared/AGENTS.md shared/risk/AGENTS.md shared/review/AGENTS.md shared/signals \
  Ashare/tier_experiments.py tests/test_documentation_contract.py
git commit -m "docs: consolidate capital growth architecture and retire legacy paths"
```

---

### Task 16: Add Unified Acceptance and Release Gates

**Files:**
- Create: `shared/runtime_test/capital_growth_acceptance.py`
- Modify: `shared/runtime_test/full_acceptance.py`
- Modify: `shared/runtime_test/market_health.py`
- Test: `tests/test_capital_growth_acceptance.py`

**Interfaces:**
- CLI: `python -m shared.runtime_test.capital_growth_acceptance --pretty`.
- Produces checks: `master_capital`, `capital_epoch`, `ashare_final_risk`, `futures_affordability`, `cost_coverage`, `realized_evidence_gate`, `mg_ablation`, `real_trading_boundary`, and `documentation_contract`.
- Exit non-zero on reconciliation, epoch, risk, cost, or real-trading boundary failures; sample insufficiency is warn/collecting, not a system failure.

- [ ] **Step 1: Write acceptance aggregation RED tests**

```python
def test_acceptance_fails_on_reconciled_code_but_stale_epoch_review():
    def acceptance_fixture(**overrides):
        payload = {
            "master_reconciled": True,
            "review_epoch": 2,
            "current_epoch": 2,
            "closed_round_trips": 0,
            "real_trading_enabled": False,
        }
        payload.update(overrides)
        return payload

    report = build_acceptance_report(
        acceptance_fixture(master_reconciled=True, review_epoch=1, current_epoch=2)
    )
    assert report["overall_status"] == "fail"
    assert report["checks"]["capital_epoch"]["status"] == "fail"


def test_zero_samples_is_warn_not_profitability_claim():
    report = build_acceptance_report(
        {
            "master_reconciled": True,
            "review_epoch": 2,
            "current_epoch": 2,
            "closed_round_trips": 0,
            "real_trading_enabled": False,
        }
    )
    assert report["overall_status"] == "warn"
    assert report["capital_growth_state"] == "collecting_samples"
    assert report["profitability_proven"] is False
    assert report["real_trading_enabled"] is False
```

- [ ] **Step 2: Run RED tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider tests/test_capital_growth_acceptance.py -q
```

Expected: FAIL because the acceptance module does not exist.

- [ ] **Step 3: Implement the read-only acceptance report**

The report must separate:

```python
{
    "code_health": "pass|warn|fail",
    "data_health": "pass|warn|fail",
    "capital_reconciliation": "pass|fail",
    "strategy_evidence": "collecting_samples|initial_evidence|out_of_sample_validation|simulation_scale_candidate",
    "profitability_proven": False,
    "real_trading_enabled": False,
}
```

`profitability_proven` remains false until an explicit future statistical-proof specification defines that claim; this implementation reports evidence state only.

- [ ] **Step 4: Run the complete local test matrix**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider tests -q
PYTHONDONTWRITEBYTECODE=1 python3 -m shared.runtime_test.full_acceptance --profile quick --pretty
PYTHONDONTWRITEBYTECODE=1 python3 -m shared.runtime_test.capital_growth_acceptance --pretty
```

Expected: all tests pass; acceptance may be `warn` solely because current epoch lacks closed samples, never because of capital, epoch, risk, cost, or safety failures.

- [ ] **Step 5: Run static and legacy-path scans**

```bash
python3 -m compileall -q Ashare CNFutures shared tools
rg -n "REAL_TRADING_ENABLED\s*=\s*(1|true)|real_trading_enabled[\"']?\s*[:=]\s*true" \
  Ashare CNFutures shared tools
rg -n "SharedSignals.*sqlite|marketdata\.db|executions/sim/|shared/signals/signal_cards" \
  Ashare CNFutures shared tools docs README.md AGENTS.md STATUS.md
```

Expected: no active automatic-real enablement, sibling-database dependency, or retired write path.

- [ ] **Step 6: Commit the acceptance gate**

```bash
git add shared/runtime_test/capital_growth_acceptance.py shared/runtime_test/full_acceptance.py \
  shared/runtime_test/market_health.py tests/test_capital_growth_acceptance.py
git commit -m "test: add 50k capital growth acceptance gate"
```

---

### Task 17: Production Read-Only Re-Gate and Controlled Handoff

**Files:**
- Modify only if evidence changes: `STATUS.md`
- Produce runtime evidence outside Git-tracked source: `shared/runtime_test/capital_growth_acceptance_latest.json`

**Interfaces:**
- No production mutation is authorized by this task.
- Uses `safe-release-check` before any later push/deploy/migration request.

- [ ] **Step 1: Verify the implementation branch before publication**

```bash
git status --short --branch
git log --oneline --decorate origin/main..HEAD
git diff --check origin/main...HEAD
git diff --stat origin/main...HEAD
```

Expected: only intended source/tests/docs; no runtime ledgers, secrets, build artifacts, or user changes.

- [ ] **Step 2: Run the safe-release preflight without publishing**

Read and apply `safe-release-check`. Verify target repository, branch, exact commits, test evidence, rollback commits, database/file migration dry-run, and separate local/GitHub/production/runtime states.

- [ ] **Step 3: Run production read-only probes only**

```bash
ssh root@8.138.181.177 '
  cd /opt/investment/tradingagent &&
  git status --short --branch &&
  git rev-parse --short HEAD &&
  PYTHONPATH=/opt/investment/tradingagent \
    /opt/investment/tools/venvs/tradingagent-test/bin/python \
    -m shared.runtime_test.full_acceptance --profile prod --pretty
'
```

Expected: capture current baseline only. Do not copy files, pull, restart, modify cron, apply epoch repair, or write master capital on production in this step.

- [ ] **Step 4: Prepare the explicit production change set for separate authorization**

The handoff must enumerate separately:

- GitHub push/merge;
- production source sync;
- epoch review repair dry-run and exact archive destination;
- master-capital bootstrap dry-run;
- runtime/service restart if required;
- cron change if required;
- post-change read-only acceptance;
- rollback for each layer.

Do not execute those external mutations unless the parent task confirms their release authorization after reviewing the complete diff and preflight.

- [ ] **Step 5: Report residual evidence risk honestly**

Even with all code and documentation gates passing, the handoff must state:

- current epoch closed-trade sample count;
- current net realized expectancy and its interval;
- current maximum drawdown;
- current affordable futures-product count;
- MG matched-ablation count;
- that market loss, gap risk, liquidity, exchange rule changes, model error, and insufficient samples cannot be reduced to zero;
- that automatic real trading remains disabled.

No “all objectives achieved,” “stable profit,” or “no residual risk” claim is permitted without future market evidence.

---

## Self-Review Record

### Spec coverage

- Four audited P0 defects are Tasks 1–4 in the required order.
- One shared 50,000 CNY capital pool is Task 5.
- Full epoch propagation and current/legacy isolation are Tasks 3 and 6.
- Cost-aware realized performance is Task 7.
- MarketGraph matched A/B evidence is Task 8.
- KPI/evidence promotion gates are Task 9.
- Pre-gate observe/counterfactual snapshots and execution-independent forward labels are Task 10.
- Orthogonal A-share style shadows, one-account arbitration, attribution, and champion/challenger are Task 11.
- A-share rank-based, dynamically sized, safety-gated exploration is Task 12.
- CN-futures valid-session decisions and executable/counterfactual separation are Task 13.
- Preopen/opening/ops no-silent-zero acceptance and channel/style-separated review metrics are Task 14.
- Canonical documentation and safe legacy retirement are Task 15.
- Unified local/production read-only verification is Tasks 16–17.
- Real-money automation remains explicitly outside scope in every relevant task.

### Known dependencies and limits

- CN-futures directional and rejection samples can accumulate for every valid session, but executable simulated fills still require SharedSignals to expose current, independently useful, **capital-affordable** products; that dependency must not be “fixed” by lowering TA risk gates.
- MarketGraph A/B cannot demonstrate value until MG returns versioned, timestamped, stock/industry-scoped decision context with evidence IDs and TTL. Missing rows remain `not_measured`.
- Current epoch begins with zero valid realized round trips. Passing code/tests proves the measurement and safety system, not profitability.
- Exact exchange-grade queue priority, forced liquidation, dynamic margin notices, and delivery calendars remain outside the present simulator when SharedSignals does not provide those inputs; documentation must preserve this limitation.

### Placeholder and type consistency review

- The plan contains no unresolved implementation placeholders.
- `capital_epoch`, `master_capital_event_id`, `observation_id`, `style_prediction_id`, `primary_style`, `supporting_styles`, `sample_intent`, `execution_class`, `research_mode`, `net_expectancy`, and `closed_round_trip_count` use the same names across writers, reviews, gates, and acceptance.
- Every deletion is preceded by an active dependency scan and preserves immutable financial audit evidence.
