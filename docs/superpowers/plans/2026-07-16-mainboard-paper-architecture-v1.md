# Mainboard Small-Capital Paper Architecture V1 Implementation Plan

> **Historical naming notice (2026-07-20):** This is an implementation-history document. References below to “SharedSignals” preserve the names and ownership model used when the plan was written. The current upstream product is **TradingDatas**; TA still consumes only `GET /v1/catalog` and `POST /v1/query`. The old SharedSignals runtime, routes, SQLite and dual registry are not valid fallbacks or dependencies. Do not use this historical plan as current runtime status; see `STATUS.md`, `README.md` and `docs/system_state_matrix.md`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve TradingAgent's proven simulated-capital and execution core while replacing only the TA consumer edge with the externally owned SharedSignals V1 query contract, enforcing mainboard-only individual-security scope, adding explicit small-capital feasibility and safe model/LLM lifecycle contracts, and producing one automatic, auditable paper-trading day loop.

**Architecture:** `SharedSignalsQueryClient -> DataEvidenceGate -> immutable ResearchDataSnapshot -> three Universe snapshots -> frozen Champion -> small-account plan -> thesis-risk authority -> existing risk/OMS/sim broker/capital authority -> SampleJournal/DecisionLedger`. ChiNext and STAR index/sector aggregates are context-only. A provider-neutral LLM evidence sidecar may enrich research artifacts but has no dependency path to risk, capital, orders or broker code. Existing capital, execution lineage and SampleJournal remain canonical; legacy data clients are removed after parity and no-fallback tests.

**Tech Stack:** Python 3.9+, dataclasses/JSON Schema-style validation, pytest, existing TradingAgent runtime and React/Vite read-only front, DeepSeek-compatible HTTP only behind an optional provider adapter.

**Ownership correction (2026-07-16):** This plan implements only the TradingAgent consumer, decision, simulation, governance and read-only presentation side. It does not implement, modify, test or accept the SharedSignals server. References to `/v1/catalog` and `/v1/query` below mean TA-side contracts, fixtures and fail-closed client behavior; SharedSignals delivery remains owned and accepted by its separate single-writer lane.

## Global Constraints

- `REAL_TRADING_ENABLED=false`; no broker, email, GUI order, real account or live credential use.
- Preserve current `ashare-capital-v1`, fresh execution lineage and append-only SampleJournal invariants.
- Individual security analysis, candidates, forecasts, shadow books, intents, orders, fills and positions are Shanghai/Shenzhen mainboard common stocks only.
- ChiNext/STAR indices and full-market sector aggregates are context-only and can never acquire a symbol-level order path.
- Query V1 failure is fail-closed; no Tushare, sibling SQLite, legacy endpoint, CSV or cached data-only fallback.
- One frozen rank-score Champion for Phase 0-3; uncalibrated output is never called probability.
- Daily paper trading may be automatic. Model promotion, risk expansion and live transition remain human-authorized only.
- LLM outage or invalid output cannot stop or change the deterministic paper loop.
- Tests precede behavior and every compatibility item has an owner, successor, sunset phase and deletion gate.

---

## 2026-07-16 local-candidate checkpoint

This plan is a task ledger, not a release manifest. An unchecked box can have a local implementation candidate while still lacking final review, state promotion, cutover or runtime evidence. A checked box may only be used after the main agent freezes one candidate and records fresh evidence in `STATUS.md` and the machine-readable governance state.

> 2026-07-17 superseding note: Task 7 now has a default-closed, strict HTTPS client candidate for direct `POST https://api.deepseek.com/chat/completions`. It has only been exercised with injected fake openers and synthetic credentials; no real credential was read, no provider request was sent, and no server/production activation is implied. The prior 2026-07-16 checkpoint remains historical evidence for the earlier offline-only candidate.

| Task | Local candidate evidence | Still blocking completion |
|---|---|---|
| 1 | Human/machine state matrix, universe contracts, legacy inventory and architecture guards exist locally | Installed runtime and external-consumer inventory remain unverified |
| 2 | Strict provider-neutral `/v1/catalog` and `/v1/query` client, metadata-preserving evidence gate and negative tests exist locally | SharedSignals live endpoint, auth, catalog version and dataset IDs are upstream-owned and not frozen |
| 3 | Three immutable universe snapshots, mainboard classifier, zero-leakage tests and externally verified CoverageReceipt contract exist locally | Real SS/account metadata, production coverage verifier and final pre-OMS cutover proof remain pending |
| 4 | 50,000 CNY optimizer、无默认account verifier/detached proof、canonical simulated ledger adapter、current generation/lineage、买入整手/卖出零股规则、cost-policy plan与独立费用复算已存在；Champion score绑定current selection/artifact/model/spec和独立复核的数值PIT feature snapshot，rank-only、固定probe sizing；六维论点风险 authority 绑定人工policy、逐成员proof、完整候选/持仓/pending exposure set、跨决策连续性及同symbol group identity，day loop再绑定权威receipt复核 | fixture与canonical-capital均不证明真实broker账户、Champion/feature registry、生产论点映射或风险上限authority |
| 5 | TA tests no longer locate/import/execute sibling readers, SQLite internals or servers；current-v1 consumer/候选门禁只接受catalog/query；front market-context local candidate uses strict V1 only | 旧reader/screening/benchmark仍服务非A股、人工只读或legacy regression的active-compatibility；A股硬阻断路径、安装态与外部消费者尚未全部清零，物理退役未完成 |
| 6 | Label maturity、Decision Ledger、无默认calendar verifier/detached proof、外部内容寻址plan artifact门、SampleJournal/ops显式贯穿plan、同authority A股forward targets已存在；metrics verifier固定本地implementation trust root并重读完整artifact/receipt，lifecycle与单调负向drift候选已存在 | calendar与metrics均只有本地完整性合同，proof不是签名，尚无真实独立重算/受信artifact registry；生产calendar、market-truth与冻结端到端统计验证仍待完成 |
| 7 | Provider-neutral LLM sidecar、严格证据authority/schema、离线fixture，以及固定官方端点、禁止代理/重定向/重试/fallback、校验TLS/主机名、隔离raw-secret、严格JSON/字节上限和完整HTTP receipt绑定的默认关闭HTTPS客户端已存在 | 供应商侧旧密钥revoke/rotate、新密钥注入、认证readback、真实provider canary、生产verifier、durable sink、代表性冻结基准和增量价值仍待完成 |
| 8 | Offline-only fixture CLI 与独立capital-backed test composition、blocked-day closeout、durable store/publisher、capital outbox、mark/quote evidence authority、显式trusted fixture clock、不截断时间因果、原因特异的共享失败回执，以及sim-submit/capital-commit前和commit时钟校验后的drift/Champion/freshness复核已存在；commit后崩溃只恢复ledger已证明的幂等事实，intent-before-commit不绕过收紧门；零成交release核对canonical完整剩余预约并以精确事件前缀证明立即terminal/全余额零，部分释放、guard拒绝、legacy别名和terminal fill冒充release均fail closed | capital-backed composition无CLI；无生产market verifier/clock、live SS adapter、accepted scheduler、外部authority+commit原子门禁或真实paper-session证据；fixture fill不是市场样本 |
| 9 | Read-only Today panel consumes only the active projection root, while the fixture CLI is physically isolated under `<output-root>/shared/runtime_test/phase1_paper_fixture/` | The fixture deliberately cannot populate Today; no accepted scheduler, active-root publication, live SS input or real paper-session artifact exists |
| 10 | Canonical focused-test manifest and architecture/doc consistency guards exist；历史候选清单`1380 passed`，前端43文件`276 passed`且lint/build/真实本地渲染通过，并拒绝非loopback监听与`*` CORS；三次隔离CLI验证同根幂等和跨根字节稳定；新DeepSeek HTTPS overlay须以本轮fresh结果更新`STATUS.md` | 本地测试与fixture通过不代表Git主线、SS live、真实paper、已安装scheduler、真实DeepSeek provider调用或生产；live数据/authority门仍按上游与运行阶段验收 |

Production, Git main, SharedSignals live API, cron/scheduler, broker and real trading remain unverified or disabled. Local receipt hashes are integrity bindings, not external signatures. `REAL_TRADING_ENABLED=false` is not a substitute for evidence that the whole paper runtime ran.

---

## Task 1: Establish state, universe and legacy governance

**Files:** `docs/system_state_matrix.md`, `docs/universe_contract.md`, `docs/architecture.md`, `docs/data_contract.md`, `STATUS.md`, `shared/governance/*`, `tests/test_architecture_contract_guards.py`.

- [x] Create a fresh `SystemStateMatrix`, canonical-path list and `LegacyInventory` covering code, config, cron, runtime paths, front consumers and known external consumers.
- [x] Define contract IDs/versions and machine-readable states: current, target, planned, time-boxed compatibility, historical read-only and retired-blocked.
- [x] Add CI-style guards for planned-as-current drift, legacy references and documentation/constant/fixture version drift.
- [x] Record `shared/data/shared_signals_api.py`, `shared/data/reader.py`, CNFutures direct SQLite paths and front/runtime consumers with explicit migration gates.

## Task 2: Implement the complete TA-side SharedSignals V1 consumer and evidence gate

**Files:** `shared/data/sharedsignals_v1.py`, `shared/data/evidence_gate.py`, `tests/test_sharedsignals_v1.py`, `tests/test_data_evidence_gate.py`.

- [x] Write failing tests for complete catalog/query metadata preservation, pagination, normal and all degraded/runtime states.
- [x] Write failing tests for auth, timeout, schema/protocol errors, missing lineage/freshness/quality, stale/failed/paused/unobserved and no fallback.
- [x] Implement typed `CatalogSnapshot`, `DatasetContract`, `QueryResult` and `EvidenceDecision` objects.
- [x] Cache only complete validated responses; cache keys bind query, catalog, schema, receipt watermark and access policy.
- [x] Preserve catalog hash, response SHA, PIT mode, replay floor and receipt lineage into downstream snapshots.

## Task 3: Implement three universes and the mainboard scope policy

**Files:** `shared/universe/policy.py`, `shared/universe/snapshots.py`, `shared/screening/universe_filter.py`, `shared/screening/candidate_pool.py`, `Ashare/sim_executor.py`, `shared/risk/pre_trade_check.py`, `tests/test_mainboard_scope_policy.py`, `tests/test_three_universe_snapshots.py`.

- [x] Write zero-leakage tests at feature, candidate, forecast, target position, trade intent, shadow, order, fill and position boundaries.
- [x] Reject 300/301/688/689, Beijing, B-share, fund/index/ETF and unknown-board individual securities from tradable scope.
- [x] Preserve broad-market, ChiNext/STAR index and full-sector aggregate context with explicit `context_only=true` and no order identity.
- [x] Build immutable `MarketContextUniverseSnapshot`, `AccountTradableUniverseSnapshot` and `SmallCapitalFeasibleUniverseSnapshot` with reason codes and source hashes.
- [x] Apply a final mainboard scope assertion immediately before OMS/sim execution.

## Task 4: Add small-capital feasibility and frozen Champion contracts

**Files:** `shared/portfolio/small_account_optimizer.py`, `shared/portfolio/champion.py`, `shared/execution/cost_policy.py`, existing capital/portfolio modules, `tests/test_small_account_optimizer.py`, `tests/test_frozen_champion.py`.

- [x] Encode 50,000 CNY, 100-share lots, 15% per symbol, 90% gross, cash, minimum economic order, fee/slippage and no-trade band from versioned policies.
- [x] Use deterministic integer allocation and explain every undeployed yuan through bounded reason codes.
- [x] Bind industry, thesis, raw-material, policy/event, crowding and model-family exposure to an explicit reviewed policy, detached per-member proofs and one complete current/pending exposure-set receipt; never self-sign or reset the book at runtime.
- [x] Freeze one 4-8 feature rank-score Champion and a cash/simple feasible baseline; Challenger output is shadow-only.
- [x] Prove signal-bar and fill-bar separation and conservative fill/cost behavior.

## Task 5: Cut A-share TA consumption to V1 and retire old TA data paths

**Files:** `shared/data/reader.py`, `shared/data/shared_signals_api.py`, A-share wrappers/runtime tests/front consumer, `tests/test_sharedsignals_cutover.py`, `tests/test_no_data_fallback.py`. This task never modifies or accepts the SharedSignals server.

- [ ] Inventory every A-share call and map it to a required/optional V1 dataset profile.
- [ ] Run same-as-of parity against the legacy client with full metadata comparison.
- [ ] Switch A-share production paths to V1 `QueryResult`; keep only explicit injected test fixtures where necessary.
- [ ] Delete A-share direct SQLite/emergency fallback, classic endpoint URL/config and active-doc references in the same change.
- [ ] Keep CNFutures compatibility time-boxed until its own V1 parity suite passes; it cannot be a hidden A-share fallback.
- [x] Remove TA tests that import/reimplement sibling SharedSignals reader/server internals and guard the V1 candidate against their return.

## Task 6: Add safe learning and model lifecycle contracts

**Files:** `shared/review/label_maturity.py`, `shared/review/decision_ledger.py`, `shared/models/lifecycle.py`, `shared/models/release_manifest.py`, `shared/models/drift_policy.py`, `tests/test_label_maturity.py`, `tests/test_model_lifecycle.py`, `tests/test_drift_policy.py`.

- [x] Add `LabelMaturityRecord` and `DecisionExposureRecord` that separate market truth, paper, shadow and unavailable oracle outcomes.
- [x] Add `ValidationPlan` with time split, purge/embargo, decision-cluster identity, OOS reuse count and experiment family.
- [x] Add immutable `ModelReleaseManifest` and lifecycle states from draft through shadow/review/current/quarantine/retired.
- [x] Permit automatic negative actions only: quarantine, reduce-only, require-review or stop-new-risk.
- [x] Prove no code path can automatically promote a Challenger, expand risk or enable live trading.

## Task 7: Refactor LLM analysis into an optional evidence sidecar

**Files:** `shared/llm/gateway.py`, `shared/llm/router.py`, `shared/llm/schema.py`, `shared/llm/evaluation.py`, `shared/adversarial/bull_bear_debate.py`, `tests/test_llm_sidecar.py`, `tests/test_llm_evaluation.py`.

**Progress (2026-07-17, uncommitted local overlay):** provider-neutral contracts, strict evidence schema, source-authority receipt/verifier boundary, sensitive-payload and obfuscated-prompt-injection gates, network-free default, an immutable response-fixture evaluator, and shadow/sim authority-isolation tests exist locally. In addition to the exact offline adapter, a default-closed HTTPS client now binds the exact official endpoint, raw-secret file, canonical request hash, strict response parsing and transport receipt. Tests inject a fake opener and synthetic secret; live model existence and request-field compatibility have not been read back from the provider. A rotated credential, real provider canary, production verifier, representative frozen benchmark and measured incremental value remain pending. Vendor model IDs are configuration, not domain contract constants.

- [x] Define provider-neutral request/response contracts, model routing, strict JSON/schema validation, prompt version/hash, document cutoff and evidence references.
- [x] Route the logical `flash` role to a configured low-cost extraction model and `pro_thinking` to a configured reasoning/review model. DeepSeek official public docs were checked on 2026-07-16 for the current V4 Flash/Pro IDs, OpenAI-format base URL, JSON and thinking features; authenticated model readback and a separately authorised canary remain required before network enablement, and no vendor ID is frozen in domain code.
- [x] Implement a default-closed direct HTTPS client for the exact official chat-completions endpoint with explicit dual activation, isolated raw-secret loading, no proxy/redirect/retry/fallback, strict TLS/JSON/size validation, sanitized errors and request/response receipt binding. This is a local transport candidate, not evidence of a provider call or server/production activation.
- [x] Redact broker keys, account/cash/positions/order plans and private strategy payloads before any cloud request.
- [x] Fail closed to `unavailable/invalid` evidence; deterministic fast debate and the paper loop continue unchanged.
- [ ] Build a frozen evaluation set for extraction accuracy, citation coverage, contradiction detection, latency, cost and invalid-output rate.

## Task 8: Assemble one automatic paper-trading day bundle

**Files:** `shared/runtime/day_loop.py`, `shared/runtime/run_bundle.py`, `shared/runtime/file_store.py`, `shared/runtime/publisher.py`, `shared/runtime/composition.py`, `shared/runtime/stage_ports.py`, `tools/run_phase1_paper_fixture.py`, existing wrappers/cron/sim/reconcile/sample-ops modules, related runtime tests.

- [x] Define the trading-day/session state machine and idempotent run identity.
- [x] Compose preopen evidence, context receipt, mainboard universe, Champion decision, risk, OMS/sim broker, reconciliation, labels and reports without duplicating their authorities.
- [x] Add crash/restart, duplicate event, stale data, unknown order and unreconciled-account fault injection.
- [x] Stop new risk on any frozen-version, scope, data, order or accounting invariant failure while preserving exit evaluation where authority is valid.
- [x] Keep the offline fixture CLI physically incapable of network/broker construction, publish only `environment=local_candidate`, and prove its output path cannot be mistaken for the front or production canonical path.

## Task 9: Expose a read-only Today dashboard and maintain docs

**Files:** `front/src/server/*`, `front/src/*`, `docs/operations.md`, `docs/capital_growth_validation.md`, `README.md`, `STATUS.md`, front tests.

- [x] Show today's run state, data evidence, market context, mainboard candidates, actions, cash/positions, blocks, no-trade reasons and learning due dates.
- [x] Label rank score, calibrated probability, LLM evidence, simulated fact and historical comparison distinctly.
- [x] Keep front strictly read-only and display explicit degraded/unverified states instead of synthetic zeros.
- [x] Update architecture, data, operations, validation, current STATUS and retirement records with the same code changes.

## Task 10: Verify and freeze the TradingAgent candidate

- [x] Run focused V1, universe, small-account, lifecycle, LLM, day-loop and no-fallback tests.
- [x] Run full backend, front lint/tests/build/API build and Python compile checks. The execution host recycled long-lived child sessions, so the exact 2,869-test collection was run in eight ordered partitions covering all 174 test files once; every partition returned zero.
- [x] Run an end-to-end local trading-day replay through the injected, network-closed `/v1/catalog` + `/v1/query` contract fixture and fresh simulated account; do not implement or emulate the SharedSignals server inside TA.
- [x] Verify restart/reconcile and exact cash/position/frozen-funds invariants.
- [x] Run independent spec and code-quality reviews, `git diff --check`, docs/link/state guards and freeze a candidate manifest.
- [x] Report local artifact, worktree, Git/remote, production file, production runtime, data freshness and real paper samples separately in `STATUS.md`; unknown/live layers remain explicitly unverified rather than inferred from local tests.
