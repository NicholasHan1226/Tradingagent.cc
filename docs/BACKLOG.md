# TradingAgent Active Backlog

> 本文件是 `docs/EVOLUTION_PROGRAM.md` 的短执行队列，不是第二套路线图，也不是当前运行事实。当前事实必须由本轮新鲜 server/API/receipt/ledger/readback 与 `AUTODEV_STATE.json` 证明。任务按“先运行并积累证据，再提升科学性，最后处理不阻塞的工程债”排序。

## P0 — 保持数据与系统持续运行

1. **TradingDatas 高价值数据持续可用**
   - 优先修复会阻断 A股/Crypto 当前 observation、sample、label、evaluation 的 freshness/receipt/lineage/query 问题。
   - dataset 独立沿 `contract_ready → observed → stable → consumer-proven` 前进；单项失败只阻断依赖它的能力，不等待全 catalog 完成。
2. **核心 runtime 连续性**
   - A股会话/分钟模拟与 Crypto delayed-paper/observation 保持 restart-safe、幂等、可恢复。
   - gap、reject、no-trade 必须留下明确证据，不为追求“全绿”伪造成功。
3. **代码合并/自动部署使用机器 CI gate，不依赖人工审批**
   - 当前公共仓库的普通 PR 以当前 head SHA 的 CI 成功作为日常 merge gate；共享/治理/部署路径或真实文件重叠必须 fresh-base 后重跑。
   - `main` 的 exact-SHA 测试/打包证据是 GitHub 自动部署的代码 gate；部署后仍必须由 release/runtime/API/receipt/readback 证明生产事实。
   - Actions 暂时不可用时，现役数据采集、市场 core 和模拟演进继续运行，但普通代码 PR 不因此绕过 `main` gate。未来若引入独立 fallback runner，必须产生同等级、可校验的机器证据。
   - 真实资金、broker、公开入口、权限/密钥、破坏性数据操作和自动风险扩张仍属于独立 authority。

## P1 — A股 A3：把已有自动演进链跑实

A股已经拥有 SampleJournal/KPI 科学证据、Challenger producer 和 simulation-only 自动 Champion promotion；当前重点不是再造一套 promotion framework，而是让它在真实模拟运行中形成可重复证据。

1. 连续证明 `SampleJournal → KPI → scientific gate → Challenger → registry promotion receipt` 的自然运行轮。
2. 为 promotion 同等级补齐 demotion、retirement 与 deterministic rollback 证据。
3. 保留**所有** Challenger/trial 的身份、参数、数据窗口、结果与淘汰原因，不能只保留赢家。
4. 扩展不同交易日/决策 cluster/regime/费用环境覆盖，避免单一短窗口驱动晋级。
5. evolution 失败不得使核心 observation/simulation 停止；学习链与市场 core 保持独立故障域。

## P1 — Crypto MVP-1 → MVP-2：先滚动积累真实模拟结果

当前不建设新的 C3 Registry/API、Champion 平台或模型服务。先复用已有
delayed-paper、factor/strategy evaluation、模拟账本和费用事实，把完整 segment 变成
可比较的 resolved outcomes。

1. 用一个真实 receipt-bound、PIT 安全且不跨 gap 的完整 segment 生成首个可复核
   resolved outcome；声明 fee、spread/slippage、exchange filters 与排除范围。
2. 对同一输入运行一个已有 factor/strategy 与简单 baseline，保存确定性 artifact、
   replay 命令、费用后收益、回撤、换手、命中和 abstention。
3. 输出 shadow-only 的 retain/downweight/disable/parameter 建议；不得写资本、订单、
   Testnet/Live 或自动风险扩张。
4. 逐条新增 outcome 进入 MVP-2 rolling evaluation，保留负结果、重复搜索次数和
   symbol/regime/gap 覆盖，不等待全部 40 标的或最新连续 288 根。
5. 只有滚动证据表明确实需要生命周期 authority 时，才重新评估 C3 registry、
   promotion/demotion/rollback 的最小实现；它不是当前模拟和结果积累前置。

## P2 — 复盘层去除旧生命周期语义

1. `shared/review/goals.yaml` 暂时只保留兼容的 review diagnostic thresholds；它不是生命周期 authority。
2. 将 daily/weekly review 逐步改为 **market-specific descriptive scorecards**：
   - A股指向 SampleJournal/KPI evolution authority；
   - Crypto 指向当前 rolling evaluation artifact；
   - CNFutures 暂停期间只做 preserve/read-only diagnostics。
3. 清除 `strategies_for_manual_review`、固定 `stage_1_sim` 等容易被误读为 promotion gate 的旧命名，但不为改名破坏现役复盘输出。
4. `STATUS.md` 只作带时间戳的当前摘要；长期决策进 ADR，详细运行事实进机器 artifact 或日期报告。

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
