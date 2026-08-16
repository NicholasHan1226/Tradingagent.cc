# TradingAgent Evolution Program

> **Planning authority, not runtime truth.** This document defines the long-term evolution objective, market-specific phase model, evidence axes, and work decomposition for TradingAgent. Current state must always come from fresh runtime/data readback, durable receipts/ledgers, and the current Git commit. `STATUS.md` is historical context. This plan never authorizes real-money execution.

## 1. North Star

TradingAgent should evolve into a continuously running, evidence-driven personal quantitative system that can:

1. consume trustworthy point-in-time market data from TradingDatas;
2. keep market-specific simulated capital, execution, risk, samples, and history isolated;
3. generate observations, candidates, fills/rejections, labels, and complete round trips continuously;
4. create Challengers from new factors, parameters, combinations, regimes, and execution hypotheses;
5. evaluate them on frozen out-of-sample evidence after costs and with multiple-testing awareness;
6. automatically promote, demote, retire, or keep Champions **inside the simulation/shadow domain** when evidence is sufficient;
7. preserve rollback, lineage, trial history, and negative results so repeated search does not erase failed experiments;
8. self-heal ordinary runtime/data failures without waiting for a human approval step;
9. keep real-money/live transition as a separate authority boundary that is never inferred from simulation success.

The optimization objective is **not** “maximize backtest return.” The system should improve net-of-cost risk-adjusted economic value while preserving data validity, reproducibility, execution realism, and controlled drawdown.

## 2. Operating principles

- **Deploy and observe before polishing.** Prefer a small working vertical slice that produces real evidence over a large refactor that delays runtime.
- **Evidence-based exits, not calendar gates.** A phase advances when its evidence conditions are met; “day 5”, “week 2”, or a human checkpoint is not a simulation promotion authority.
- **Independent market progress.** A-share, Crypto, and CNFutures do not share a global stage gate. One market's missing data must not block another market's safe progress.
- **No GitHub Actions dependency.** Local/server deterministic validation and fresh runtime readback are sufficient for normal autonomous merge/deploy when Actions are unavailable. Actions may provide optional extra evidence only.
- **One fact, one authority.** Runtime truth belongs in machine artifacts/receipts/ledgers; planning belongs here; architecture rationale belongs in ADRs; historical readbacks belong in reports/`STATUS.md`.
- **Research is just-in-time.** External research must answer a concrete market/data/statistical/execution question and produce an implementable contract, test, or evaluation rule. Do not create open-ended research programs that block runtime.
- **Simulation autonomy, live separation.** Strategy/factor/model lifecycle inside simulation can be automatic; automatic risk expansion and automatic live transition remain disabled.

## 3. Evidence axes

A market or strategy is not mature because one scalar metric is high. Every phase should consider the applicable axes below.

| Axis | Minimum evidence type |
| --- | --- |
| Data | PIT/as-of validity, freshness, quality, receipt/lineage, identity, pagination/completeness |
| Sample maturity | independent trading days, unique decision clusters, completed round trips, `N_eff`, regime coverage |
| Economics | post-cost expectancy/PnL, benchmark-relative result where meaningful, turnover/cost burden |
| Risk | account MTM drawdown, concentration, tail loss, liquidity/capacity, loss streak behavior |
| Robustness | frozen OOS/time split, walk-forward/replay, regime stability, ablation, negative controls |
| Multiple testing | complete Challenger/trial ledger; DSR/PBO or equivalent once sample size supports it |
| Execution reality | market-specific lot/tick/notional/session constraints, spread/slippage/fees, fill revalidation |
| Reproducibility | immutable input/output hashes, versioned strategy/model artifact, frozen validation plan, deterministic replay |
| Operations | scheduled runtime continuity, gap recovery, idempotency, restart recovery, health/readback |

A simple win-rate/Sharpe threshold may remain a diagnostic, but it is never sufficient lifecycle authority by itself.

## 4. System-level evolution phases

These are capability milestones, **not a single global gate** that every market must finish simultaneously.

### S0 — Authority and evidence foundations

Goal: every important fact has a single owner and can be replayed/audited.

Exit evidence:
- TradingDatas catalog/query is the only market-data wire authority;
- market capital/execution namespaces are isolated;
- append-only receipts/ledgers and PIT lineage exist;
- real-trading flags fail closed;
- historical/retired paths cannot silently become fallback authorities.

### S1 — Continuous production evidence

Goal: active markets run continuously enough to accumulate real observations and labels without manual babysitting.

Exit evidence:
- core timers/services have deterministic restart/recovery behavior;
- data gaps are visible and self-heal when upstream data returns;
- runtime keeps producing samples even when no trade is allowed, with explicit rejection reasons;
- source/GitHub/release/runtime/readback states remain separately observable.

### S2 — Scientific evaluation

Goal: evaluation quality is good enough to distinguish a promising Challenger from noise.

Exit evidence:
- frozen OOS/time-split evaluation exists;
- cost and fill evidence are bound to the evaluated sample;
- duplicate/cluster dependence is controlled;
- sample-size evidence includes independent days/clusters and `N_eff` where applicable;
- calibration/benchmark/ablation evidence is available for the strategy family being evaluated;
- trial history is preserved so repeated search cannot masquerade as one experiment.

### S3 — Autonomous simulation evolution

Goal: the system creates and evaluates Challengers continuously and changes the simulation Champion without human approval when scientific evidence is ready.

Exit evidence:
- Challenger production is deterministic and evidence-bound;
- automatic promotion/demotion/retirement receipts are durable and replayable;
- rollback to the prior Champion is deterministic;
- promotion never expands risk automatically and never enables live trading;
- negative and rejected Challengers remain queryable history.

### S4 — Regime and portfolio evolution

Goal: evolve not only one strategy but the allocation among strategies/regimes while retaining market-specific capital authority.

Exit evidence:
- regime classification is PIT and independently testable;
- multiple Champions/experts can be compared without cross-account leakage;
- allocation changes are evidence-bound, costed, capacity-aware, and reversible;
- portfolio evaluation separates strategy alpha, cash, benchmark, and execution effects;
- exploration budget remains bounded and cannot silently become risk expansion.

### S5 — Live-readiness dossier (optional future capability)

Goal: produce a complete, auditable readiness package for a separately authorized live adapter.

This phase does **not** authorize live trading. It only proves that data, execution, account reconciliation, broker adapter, compliance boundaries, risk limits, rollback, and monitoring are ready for an explicit external authority decision.

## 5. Market evolution programs

### 5.1 A-share

**Overall target:** a mainboard-first, PIT-correct, costed simulated portfolio that continuously learns from real TradingDatas evidence, automatically evolves its Champion/Challengers, adapts to market regimes, and preserves T+1/lot/price-limit/session realities. Real-money transition remains a separate future authority.

Current assessment: **A3 in implementation / evidence accumulation.** The repository already has SampleJournal/KPI scientific evidence, Challenger production, and automatic simulation promotion code. Completion of A3 requires fresh production evidence proving the full loop, not merely code existence.

Phases:

- **A0 Data contract:** stable security master/calendar/bars plus the context datasets actually used by the strategy. Dataset-by-dataset progress; no “all 222 datasets first” gate.
- **A1 Execution-realistic simulation:** canonical 50,000 CNY simulated authority, T+1, 100-share lot semantics, current marks, fees/slippage, capacity, limits, MTM, restart-safe ledger/outbox.
- **A2 Continuous sample engine:** every eligible session produces observation/candidate/reject/fill/label evidence; independent days and decision clusters accumulate naturally.
- **A3 Scientific autonomous evolution:** SampleJournal/KPI → scientific gate → Challenger → frozen validation → automatic simulation Champion promotion/demotion/rollback.
- **A4 Regime/portfolio evolution:** multiple strategy families and regime-aware allocation compete under one A-share capital/risk authority; exploration remains bounded.
- **A5 Live-readiness dossier:** broker-specific read-only/reconciliation and execution adapter evidence only after the simulation program is mature; no automatic live enablement.

Priority exits for A3:
1. prove repeated real runtime cycles of the existing evolution path;
2. preserve a complete trial ledger, not just the winning Challenger;
3. add DSR/PBO or an equivalent multiple-testing correction after sufficient independent samples exist;
4. measure Champion stability across different market regimes and costs;
5. make demotion/rollback evidence as explicit as promotion evidence.

### 5.2 Crypto

**Overall target:** a 24/7 multi-asset spot simulation system with trustworthy TradingDatas market data, exchange-rule-aware execution, continuous delayed-paper samples, factor/strategy OOS evaluation, and the same evidence-driven automatic Challenger/Champion lifecycle as A-share—without giving the research/learning process capital or live authority.

Current assessment: **C2 rolling evaluation.** Ten-symbol observation and delayed-paper/factor research are running, but `Crypto/promotion.py` remains a read-only scorecard with automatic promotion retired.

Phases:

- **C0 Data health:** stable 5-minute bars/rules/book-ticker for the frozen multi-symbol universe; gaps/freshness/receipt/lineage observable and self-healing.
- **C1 Delayed-paper capital loop:** restart-safe simulated capital/ledger, deterministic quote/fill model, fee/spread/slippage and exchange filters, idempotent round trips.
- **C2 Scientific factor/strategy evaluation:** rolling OOS/time-split factor evidence, baseline comparison, cost sensitivity, symbol/regime coverage, negative results retained.
- **C3 Autonomous simulation evolution:** replace the retired manual-review promotion scorecard with an evidence-bound Challenger/Champion registry and automatic simulation-only promotion/demotion/rollback.
- **C4 Multi-asset/regime portfolio evolution:** allocation across symbols/strategies is costed, risk-bounded, capacity-aware and independently replayable; perpetual-market data may be research context but cannot silently change spot execution authority.
- **C5 Live-readiness dossier:** optional future authenticated account/testnet/live-adapter readiness package; no automatic key/account creation or live transition.

Priority exits for C2→C3:
1. unify factor/strategy trial identity and preserve every tested candidate;
2. freeze a Crypto-specific validation plan and sample-maturity contract;
3. bind exchange filters, spread/slippage and fees to each evaluation artifact;
4. define minimum independent time/regime coverage rather than only trade count;
5. implement simulation-only registry receipts, promotion, demotion and deterministic rollback.

### 5.3 CNFutures

**Overall target if resumed:** an isolated futures simulator that correctly models contract multiplier/tick, session/night-session, margin, long/short/open/close, roll, limits, fees/slippage and account MTM before any strategy evolution is allowed.

Current assessment: **F0 paused/preserve.** Paused means no active development or runtime is required. Existing evidence and capital history remain read-only; this market does not block A-share or Crypto.

Resume phases:

- **F0 Preserve:** keep contracts/tests/history from drifting; no runtime/timer.
- **F1 Data readiness:** current contract specs, trading calendar/session, limits and adjacent market data have fresh TradingDatas evidence.
- **F2 Read-only observation:** session/contract/roll and candidate/hold/risk-reject evidence only.
- **F3 Execution-realistic simulation:** margin/open-close/roll/night-gap/fees/slippage/MTM are deterministic and restart-safe.
- **F4 Scientific autonomous evolution:** futures-specific OOS and Challenger/Champion lifecycle with no cross-market capital authority.
- **F5 Live-readiness dossier:** broker/CTP-specific future work under separate authority.

## 6. TradingDatas foundation track

TradingDatas should not have one monolithic “finished” state. Its useful unit of progress is the dataset/capability:

`contract_ready → observed → stable → consumer-proven`

System priority is to increase the **high-value stable subset used by active A-share/Crypto evaluation**, while continuing broader catalog expansion in parallel. A missing or impaired dataset should block only the consumer/evidence claim that actually depends on it.

For every new dataset/capability, prefer the existing provider → receipt → SQLite → catalog/query → consumer-readback path. New routes, collectors, timers, or tables require a real protocol/shape gap, not convenience.

## 7. Research program

Research should be attached to an implementation question and end in a versioned result.

### R1 Market rules and microstructure

- A-share: track current SSE/SZSE board/session/lot/tick/price-limit/program-trading rule changes and encode only the subset required by active strategies.
- Crypto: track official Binance Spot market-data and exchange-filter contracts, rate-limit behavior, symbol status, tick/step/min-notional semantics and API changes.
- CNFutures: only when resumed, research current exchange/contract/CTP rules needed by the selected contracts.

Deliverable: a dated rule/profile change, test, or explicit “no change required” report. Do not turn rule research into a permanent manual gate.

### R2 Statistical validity

Research and implement, when sample size permits:
- walk-forward/frozen OOS evaluation;
- Deflated Sharpe Ratio / Probability of Backtest Overfitting or an equivalent multiple-testing control;
- effective sample size / clustered decisions;
- calibration and benchmark fairness;
- regime and cost sensitivity;
- trial-count accounting across automated search.

Deliverable: executable evaluation artifact and thresholds/rationale bound to a strategy family.

### R3 Production learning reliability

Apply only the production-ML practices that reduce real failure modes: data validation, training/serving/evaluation consistency, artifact lineage, restart recovery, drift detection, and monitoring. Avoid infrastructure whose only purpose is to look enterprise-grade.

Deliverable: one measurable reduction in failure/recovery/evaluation risk per change.

## 8. Work decomposition and priority

The controller/agents should choose the smallest next slice that advances one active market or a shared dependency.

### P0 — unblock continuous running

1. fix data/runtime failures that stop active sampling;
2. keep TradingDatas high-value datasets fresh and queryable;
3. remove governance/process gates that depend on unavailable GitHub Actions or human review;
4. preserve rollback and append-only evidence.

### P1 — close active evolution loops

1. A-share: prove and harden automatic promotion/demotion/rollback on real simulation evidence;
2. Crypto: migrate from read-only/manual-review promotion semantics to a scientific simulation-only evolution controller;
3. make review dashboards descriptive only and point lifecycle decisions to each market's real evolution authority.

### P2 — improve scientific quality

1. trial ledger across all Challenger searches;
2. stronger OOS/walk-forward and regime coverage;
3. multiple-testing correction when sample maturity supports it;
4. cost/execution sensitivity and benchmark/ablation quality.

### P3 — evolve portfolios, not only strategies

1. regime-aware expert/Champion competition;
2. bounded exploration allocation;
3. portfolio attribution and capacity;
4. automatic demotion/retirement and recovery from drift.

### P4 — reduce technical debt only when it obstructs P0–P3

Refactor large files, packages, deployment mechanics, or documentation when they cause defects, duplicated authority, slow iteration, or unsafe coupling. Size/style alone is not a priority.

## 9. Authority map

- **Current runtime truth:** fresh server/API/receipt/ledger/readback and `AUTODEV_STATE.json`.
- **Evolution program:** this document.
- **A-share lifecycle authority:** SampleJournal/KPI scientific gate + evidence-bound registry receipts.
- **Crypto lifecycle authority:** currently none for automatic promotion; C3 is the target migration.
- **CNFutures lifecycle authority:** none while paused.
- **Daily/weekly review:** descriptive diagnostics only; never a lifecycle or live-transition authority.
- **Real trading:** separate explicit future authority; never inferred from this plan or simulation success.

## 10. Research basis

This program follows four evidence-based ideas:

1. production learning systems require continuous data/model validation and monitoring, not only offline model scores;
2. financial strategy search requires explicit control of selection bias and backtest overfitting;
3. market microstructure rules differ materially across A-share, spot Crypto, and futures and therefore must remain market-specific;
4. autonomous iteration is safest when experimentation/lifecycle authority is separated from capital/live-execution authority.

The plan should be revised when new evidence changes one of these premises. Revisions belong in Git history; durable rationale changes belong in an ADR.