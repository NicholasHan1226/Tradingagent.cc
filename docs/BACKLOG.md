# TradingAgent Active Backlog

> 本文件是 `docs/EVOLUTION_PROGRAM.md` 的短执行队列，不是第二套路线图，也不是当前运行事实。当前事实必须由本轮新鲜 server/API/receipt/ledger/readback 与 `AUTODEV_STATE.json` 证明。任务按“先运行并积累证据，再提升科学性，最后处理不阻塞的工程债”排序。

## P0 — 保持数据与系统持续运行

1. **TradingDatas 高价值数据持续可用**
   - 优先修复会阻断 A股/Crypto 当前 observation、sample、label、evaluation 的 freshness/receipt/lineage/query 问题。
   - dataset 独立沿 `contract_ready → observed → stable → consumer-proven` 前进；单项失败只阻断依赖它的能力，不等待全 catalog 完成。
2. **核心 runtime 连续性**
   - A股会话/分钟模拟与 Crypto delayed-paper/observation 保持 restart-safe、幂等、可恢复。
   - gap、reject、no-trade 必须留下明确证据，不为追求“全绿”伪造成功。
3. **发布不依赖 GitHub Actions 或人工审批**
   - 正常内部 sim-only release、既有 localhost service/timer 的部署/启用、旁路 canary、readback 与可回滚切换，在既定合同内由系统继续执行。
   - GitHub Actions 仅为可选附加验证；无额度、未运行或被 billing 阻断不能成为普通开发/合并/部署的停止线。
   - 真实资金、broker、公开入口、权限/密钥、破坏性数据操作和自动风险扩张仍属于独立 authority。

## P1 — A股 A3：把已有自动演进链跑实

A股已经拥有 SampleJournal/KPI 科学证据、Challenger producer 和 simulation-only 自动 Champion promotion；当前重点不是再造一套 promotion framework，而是让它在真实模拟运行中形成可重复证据。

1. 连续证明 `SampleJournal → KPI → scientific gate → Challenger → registry promotion receipt` 的自然运行轮。
2. 为 promotion 同等级补齐 demotion、retirement 与 deterministic rollback 证据。
3. 保留**所有** Challenger/trial 的身份、参数、数据窗口、结果与淘汰原因，不能只保留赢家。
4. 扩展不同交易日/决策 cluster/regime/费用环境覆盖，避免单一短窗口驱动晋级。
5. evolution 失败不得使核心 observation/simulation 停止；学习链与市场 core 保持独立故障域。

## P1 — Crypto C2 → C3：从只读评分升级为自动模拟演进

当前 Crypto rolling factor/strategy evaluation 已能积累真实 delayed-paper 证据，但 `Crypto/promotion.py` 仍是无 lifecycle authority 的只读 scorecard。

1. 冻结 Crypto-specific ValidationPlan 与 sample maturity 合同：独立时间跨度、symbol/regime 覆盖、completed round trips、cost/fill evidence。
2. 统一 factor/strategy Challenger identity 与 append-only trial journal，保留负结果和重复搜索次数。
3. 把 TradingDatas data proof、exchange filters、spread/slippage/fee 假设绑定到 evaluation artifact。
4. 建立 Crypto 自有 simulation-only Champion Registry、promotion/demotion/retirement receipt 与 deterministic rollback。
5. 只有科学证据达到 C3 条件时自动 simulation promotion；不要求人工复核，不扩大风险，不启用 Testnet/Live。

## P2 — 复盘层去除旧生命周期语义

1. `shared/review/goals.yaml` 暂时只保留兼容的 review diagnostic thresholds；它不是生命周期 authority。
2. 将 daily/weekly review 逐步改为 **market-specific descriptive scorecards**：
   - A股指向 SampleJournal/KPI evolution authority；
   - Crypto 指向未来 C3 evolution authority；
   - CNFutures 暂停期间只做 preserve/read-only diagnostics。
3. 清除 `strategies_for_manual_review`、固定 `stage_1_sim` 等容易被误读为 promotion gate 的旧命名，但不为改名破坏现役复盘输出。
4. `STATUS.md` 继续只作历史/摘要；长期决策进 ADR，运行事实进机器 artifact。

## P2 — 科学评估提升

在独立样本足够后逐步加入，而不是作为当前上线前置门禁：

1. frozen OOS / time split / walk-forward；
2. unique decision cluster 与 `N_eff`；
3. calibration、benchmark fairness、paired ablation 与 regime sensitivity；
4. trial-count-aware multiple-testing correction（DSR/PBO 或等价方法）；
5. 费用、spread/slippage、容量与尾部状态敏感性；
6. Champion 稳定性与切换频率统计。

任何统计升级不得用 label-cell 数替代独立样本，也不得把短期盈利直接解释为可持续 edge。

## P3 — 从策略演进到组合/Regime 演进

1. 在每个市场内部建立 regime-aware Champion/experts 比较，不跨市场共享资本 authority。
2. exploration 预算有上限、可归因、可回放；不能因探索成功自动扩大账户风险。
3. 区分策略 alpha、现金、benchmark、费用和 execution effect。
4. 自动 allocation change 必须有 evidence receipt、容量约束和 deterministic rollback。

## CNFutures — F0 Preserve

- 当前暂停，不启动新 runtime、timer、模拟成交或策略搜索。
- 保留合同、测试和历史证据，避免代码腐化。
- 未来恢复时按 `F1 data readiness → F2 read-only observation → F3 execution-realistic simulation → F4 scientific autonomous evolution` 重新进入，不继承 A股/Crypto 的阶段或晋级证据。

## TradingCopilot — 独立辅助轨

- 继续作为 A股只读/人工状态辅助 namespace，不成为 Quant Core 第二套资金、订单或演进 authority。
- 多周期图线、公告/新闻、关注/持仓投影仍必须由 TradingDatas 的真实 PIT/receipt/lineage 证明。
- broker、真实邮件、GUI 外部写入或账户操作属于后续单独 authority，不从 Quant Core simulation maturity 自动获得权限。

## P4 — 仅处理会阻塞 P0–P3 的工程债

优先级判定：只有技术债实际造成错误、重复 authority、部署/恢复困难、迭代显著变慢或安全耦合时才重构。

当前候选包括：
- 旧数据客户端/专用 route 的消费者批次退役；
- 大文件拆分与 package 整理；
- 重复 crontab / legacy wrapper authority 清理；
- 文档职责进一步收敛。

文件大、目录不够“标准”、缺少企业级 CI/CD 本身都不是重构理由。
