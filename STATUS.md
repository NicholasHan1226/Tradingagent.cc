# TradingAgent 当前状态

> 最后更新：2026-07-20 CST。本文件只记录当前代码、服务器旁路、现役 runtime 与外部依赖的分层事实；长期规则见 [AGENTS.md](AGENTS.md)，运行与回滚见 [docs/operations.md](docs/operations.md)。历史候选、旧测试数字与作废证据从 Git 和服务器只读证据目录审计，不在这里维护流水账。

## 当前结论

已发布基线 `main@123a1122bdd46c3d8cb3fb43b0c3110524a0ac14` 包含市场内核、Mini/Hermes 仓库合同退役、A股/CNFutures/Crypto 独立 lane、市场专用模拟合同、negative-only 演化门和 TradingDatas 命名调整。它已完成一次 loopback-only、network-disabled、simulation-only 的非权威服务器 sidecar 验收，但没有切换现役源码、systemd service、cron、8787 API 或公开入口。

本文件所在的 `codex/ta-postrelease-state-retirement-v1` 是尚未提交、尚未合并、尚未部署的后续候选，正在物理删除 PM/US/HK、旧 Style/Evolution/Exit 执行栈与失效文档，并补齐多币种隔离、lane freshness 和旧读侧退役门。候选已通过最终本地测试与独立审计；普通 GitHub 发布和服务器旁路验收完成以前，仍不得把候选状态写成 `main` 或服务器事实。全系统继续保持 **fixture/mock-first / simulation-only / 无真实交易权限**，`REAL_TRADING_ENABLED=false`。

## 六层事实

| 层级 | 2026-07-20 当前事实 | 不能据此推断 |
|---|---|---|
| 本地主线 | `/Users/nicholashan/Projects/Finance/TradingAgent` 当前仍是已发布基线；精确值必须用 `git rev-parse HEAD origin/main` 读回，既有未跟踪 `.codegraphcontext/` 原样保留 | 本地同步不等于候选已合并或服务器现役切换 |
| 本地候选 | `/Users/nicholashan/Projects/Finance/TradingAgent/.worktrees/ta-postrelease-state-retirement-v1`，分支 `codex/ta-postrelease-state-retirement-v1`；当前仍有未提交改动 | 候选 diff 和测试不等于 GitHub 主线、服务器旁路或生产状态 |
| GitHub 主线 | 当前仍是已发布基线；本轮后续退役候选尚未进入远端评审或合并，精确值在发布 gate 中从远端重新读回 | GitHub main 不等于服务器进程已加载 |
| 服务器旁路基线 | `/opt/investment/tradingagent-candidates/ta-market-kernel-123a112@123a1122bdd46c3d8cb3fb43b0c3110524a0ac14` clean；独立 venv/node_modules、fixture 与 18787 canary 已验证并停止 | 只证明已发布基线可在目标机旁路运行，不证明本轮后续候选，也不是生产激活、live paper 或真实数据接入 |
| 服务器现役 | `/opt/investment/tradingagent@6c12fbed29db925019f85a6016774626f63b857a`；`tradingagent-front-api.service=active`，PID `1043`，只监听 `127.0.0.1:8787`，`/healthz` 为 200；18787 无监听 | 现役代码、service、cron 与入口均未切换 |
| 外部能力 | 未连接 live TradingDatas、accepted DeepSeek evidence、broker、真实账户、邮件、同花顺、GUI、Cloudflare 控制面或公开 API | 不能声称真实数据闭环、真实模型可用或真实交易 |

## 已发布基线与当前候选证据

- 已发布基线本地证据：全仓后端 `3124 passed`；候选清单 `1607 passed`；前端 44 个测试文件、`287 passed`，lint 与 `build:all` 通过；`git diff --check` 与 Python `compileall` 通过。项目环境未声明 Ruff，因此 Ruff 不记为 PASS。
- 已发布基线 GitHub：普通合并后的 CI 成功；本地主线与远端主线随后完成精确读回。
- 已发布基线服务器：后端 `3124 passed, 188 subtests passed`；前端 `287 passed`，lint/build 通过；扩展 `compileall` 覆盖 `shared/Ashare/CNFutures/Crypto/PM/US/tools/scripts`。
- 冻结模拟盘：首次运行、同根重放和跨根运行保持同一 `run_id` 与业务 bundle；重放不重复 fixture transport，全部运行文件跨根字节一致；SampleJournal 全部 `source_class=fixture`、`promotion_eligible=false`，没有写正式 review 根。
- API canary：仅监听 `127.0.0.1:18787`；health/snapshot 为 200，`Cache-Control: no-store`，POST 为 405、未知路由为 404，顶层 `mode=simulated` 且所有真实交易标志为 false；随后按精确 PID 停止并确认端口关闭。
- 已发布基线现役未变：Git HEAD、49 条既有未跟踪运行/回滚资产、systemd unit、PID、8787 health 与 `marketgraph` crontab 在前后 readback 中逐字节一致。Git status 摘要为 `2ac8dc6a...a74f9`，unit 摘要为 `9128f159...2307`，crontab 摘要为 `af3605a8...fc9a`。
- 已发布基线证据目录：`/opt/investment/release-evidence/tradingagent/20260720T123740Z-ta-market-kernel-123a112`；release receipt 摘要 `d504594d...95eb`，排序证据清单摘要 `5b79fac8...c9ea`。结果固定为 `server_validated_non_authority_simulation_only`、`active_production_activated=false`。
- 当前后续候选最终本地字节：候选清单 `1715 passed`，全仓后端 `3007 passed`；前端 44 个测试文件、`297 passed`，lint 与 `build:all` 通过；72 个变更 Python 文件通过 Ruff format-check 与 Ruff check，扩展 `compileall` 和 `git diff --check` 通过。两轮独立审计均为 `P0=0 / P1=0`。这些证据仍不替代尚未发生的 GitHub CI、合并和服务器旁路验收。

以上每条只证明其标注层。旁路验证期间 `TRADINGDATAS_API_URL`、旧名墓碑 `SHAREDSIGNALS_API_URL` 与 `MARKETGRAPH_API_URL` 均显式为空，DeepSeek 网络关闭，未调用 broker 或真实交易。

## 当前架构边界

1. **TradingDatas consumer**：只消费显式配置的 `GET /v1/catalog` 与 `POST /v1/query`；八字段 QueryRequest、完整 envelope、逐 dataset evidence gate 与 nullable source proof 均 fail closed。禁止直读数据库、`/tushare`、`/source_status`、provider 专用 route、localhost/file fallback。
2. **A股三层 Universe**：只有沪深主板普通股进入个股分析、候选、预测、模拟仓位与订单；创业板、科创板和北交所个股不分析、不交易，其指数及全市场行业汇总仅作 `context_only` 市场环境证据。
3. **小资金组合**：A股和 CNFutures 各有独立 50,000 CNY simulated authority；A股保留100股买入单位、卖出零股例外、T+1、费用/滑点、最低经济订单、no-trade band、现金和六维投资论点风险门。Crypto 只有隔离的 10,000 USDT shadow/simulated authority。以上参数是风险上界，不是收益承诺。
4. **市场隔离**：A股、CNFutures、Crypto 使用独立 market kernel、账户、原生币种、订单/成交状态与未来 adapter family；只共享机械基础设施，不换汇、不跨市场汇总货币金额/收益/回撤、不净额或复用 broker payload。本后续候选正在物理退役 PM/US/HK 的仓库级 runtime、包装器和专用测试；合并前它仍只是候选差异。服务器安装态若仍有旧引用，只能进入清理证据链，不能成为兼容回退。
5. **研究与自动化**：Opportunity Radar/Ledger、多期限 forecast、三风格 router、Decision Ledger、RunBundle、label maturity 与 counterfactual 仍是 shadow/nonpromotion 合同；没有 live scheduler 时不声称自动模拟盘已持续运行。
6. **LLM**：DeepSeek 仅能产生带 provenance 的 evidence，默认网络关闭，不能输出订单、仓位、目标权重或风险预算；一次历史 schema-rejected 请求不等于 accepted evidence。
7. **自我进化**：生产模型只能自动收紧、隔离或降级；晋级、恢复和扩风险仍需显式人工复核。当前没有用最近盈亏自动改写 Champion 或生产参数。

## 退役与兼容状态

- Mini/Hermes webhook、file consumer、`RealSignalQueue` 和专用源码已在仓库层 `RETIRED_BLOCKED`，不得恢复。零值环境变量和 fail-closed wrapper 目前只是安装态墓碑；服务器/旧 Mini 的 cron、process、port 与历史回执仍需独立清零证据。
- PM/US/HK 专用包、模拟 wrapper、配置、策略、测试及旧 StyleRunner/PerformanceTracker 执行栈已在本后续候选中标记为物理删除，待普通合并后才成为仓库主线事实。冻结的历史输出只允许法证读取；不得参与当前市场状态、收益、交易量、readiness、自我进化或执行决策。
- 旧 A股数据 reader、screening/research、review/runtime wrapper 与多市场 wrapper 仍按 `active-compatibility / retirement-pending / hard-blocked` 分类。只有满足 `shared/governance/legacy_inventory.yaml` 的消费者清零、同 `as_of` parity、已安装 runtime readback 和回滚证据后才可物理删除。
- 2026-07-20 服务器 readback 仍发现安装态 cron 指向旧 SharedSignals 与旧 TradingAgent wrapper。它们不是新架构依赖，但在迁移完成前不能靠删除仓内 tombstone 假装退役。
- 2026-07-20 本地只读检查确认 `~/Desktop/Investment` 已不存在，且本机 LaunchAgents、当前进程与 Nicholas 用户 crontab 的精确路径/Hermes/Mini 扫描均未发现引用；它已从 active legacy inventory 移除。此事实不推断服务器或其它主机副本也已清理。

## 明确未完成

- TradingDatas clean-slate 重构尚未提供冻结的 fresh internal handoff、catalog version、dataset IDs、service token、receipt authority、分页/排序语义与 runtime readback；旧 SharedSignals runtime/route/dual-registry 不能替代。
- 尚未安装 current-v1 live paper scheduler，也未积累真实 TradingDatas 驱动的连续 20 个交易日自动模拟和 60–120 个交易日冻结 OOS 样本。
- 现役服务器仍运行旧源码与旧调度；本轮 sidecar 没有切换 service、cron、页面或公网路由。
- 没有 accepted DeepSeek evidence、真实 broker/account、公开 ingress 或真实交易授权；`REAL_TRADING_ENABLED=false`。

## 下一阶段入口

1. 先完成旧系统/旧文档依赖审计；只删除已证明无现役消费者且可由 Git 恢复的仓内资产，保留 append-only 事实、服务器回滚资产和未完成迁移的 fail-closed tombstone。
2. 等待 TradingDatas owner 提供 fresh handoff，再以显式配置运行只读 integration probe；任何 dataset degraded/stale/failed 逐数据集 fail closed。
3. 完成真实数据 parity 与旧消费者清零后，再独立发布 current-v1 自动模拟 scheduler，并验证 crash/restart、对账、幂等和持续运行。
4. 连续 20 个交易日工程闭环后评估出口，再积累 60–120 个交易日 OOS/多状态样本。月收益 20% 只作为收益分布上尾指标，不是强制交易、满仓或 PASS 条件。
