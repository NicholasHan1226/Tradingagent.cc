# TradingAgent 当前状态

> 最后更新：2026-07-22 CST。本文件只记录当前代码、服务器旁路、现役 runtime 与外部依赖的分层事实；长期规则见 [AGENTS.md](AGENTS.md)，运行与回滚见 [docs/operations.md](docs/operations.md)。历史候选、旧测试数字与作废证据从 Git 和服务器只读证据目录审计，不在这里维护流水账。

## 当前结论

功能发布提交 `5158a096a9511cbbee1f4f23ea290292289772c3` 已由 GitHub review #8 普通合并进入 `main`，物理删除 PM/US/HK、旧 Style/Evolution/Exit 执行栈与失效文档，并完成多币种/账户隔离、三市场 lane freshness、Crypto 单一资本权威和旧读侧退役门。

其后 A股20交易日 fixture loop（review #10）、CNFutures fixture closed loop（review #11）和 Crypto fixture opening/旧 direct writer 原子退役（review #12）均已普通合并。服务器首次验证 review #12 时发现一条依赖文件系统遍历顺序的测试，保留失败证据后由 review #13 修复；`b38838a02397cc080160e6cf3dae7c47757d9c85` 是当前已完成目标服务器旁路验收的功能基线。后续主线继续关闭跨市场旧执行逃逸、同步状态文档，并加入 TradingDatas 阶段性 handoff 与分页 fail-closed 合同；当前仓库合同还固定了只从受限 TA token file 注入 Bearer 的消费侧认证边界。后续字节均完成本地验证，但没有继承或冒充 `b38838a...` 的服务器旁路证据。当前 GitHub `main` 与三条市场 lane 的精确 SHA 由 fresh Git readback 确认，不把会被下一次文档合并立即改变的主线 SHA 固化为长期事实。已验证基线的 sidecar 保持 loopback-only、network-disabled、simulation-only；任何一次 review 都没有切换现役源码、systemd service、cron、8787 API 或公开入口。

全系统继续保持 **fixture/mock-first / simulation-only / 无真实交易权限**，`REAL_TRADING_ENABLED=false`。仓库主线发布、服务器旁路通过和现役生产激活仍是三种不同状态：仓库主线已发布，服务器旁路只覆盖 `b38838a...` 功能基线，现役生产没有激活。

## 六层事实

| 层级 | 当前事实 | 不能据此推断 |
|---|---|---|
| 本地主线 | 本轮 fresh readback 与 `origin/main` 一致；A股、CNFutures、Crypto 三条长期 lane 的本地与远端均干净同步到同一 SHA，三个 lane validator 对 `origin/main` 均通过 | 本地/远端同步不等于服务器现役切换；本机代码图缓存不属于产品状态或发布证据 |
| 本地发布候选 | 短生命周期候选不固化为当前能力；是否存在待合并候选及其精确 SHA、测试与 review 结果只以当次隔离 worktree 的 fresh 验收记录为准。被新模块化实现覆盖的旧 Crypto 单体原型仅保存在本地 archive commit `c51f957161662dcb37fd6ebe795bd037b35d060b`，不得 merge/cherry-pick | 候选存在不等于主线已合并；archive 只用于法证和回滚，不是兼容入口或备用 authority |
| GitHub 主线 | 已批准功能均经普通评审合并，`main` 与三条远端市场 lane 的 fresh readback 当前一致；精确 SHA 只保存在当次验收证据 | GitHub main 不等于服务器进程已加载；服务器旁路基线之后的功能字节不能继承旧旁路证据 |
| 服务器旁路 | `/opt/investment/tradingagent-candidates/ta-crypto-fixture-b38838a@b38838a02397cc080160e6cf3dae7c47757d9c85` clean；独立 venv/node_modules、三市场 fixture 检查与 18787 canary 已验证并停止 | 只证明目标服务器环境可安装、测试和旁路运行，不是生产激活、live paper 或真实数据接入 |
| 服务器现役 | `/opt/investment/tradingagent@6c12fbed29db925019f85a6016774626f63b857a`；`tradingagent-front-api.service=active`，PID `1043`，只监听 `127.0.0.1:8787`，`/healthz` 为 200；18787 无监听 | 现役代码、service、cron 与入口均未切换 |
| 外部能力 | 未连接 live TradingDatas、accepted DeepSeek evidence、broker、真实账户、邮件、同花顺、GUI、Cloudflare 控制面或公开 API | 不能声称真实数据闭环、真实模型可用或真实交易 |

本地主线与远端主线的 fresh readback 统一使用 `git rev-parse HEAD origin/main`；结果只写入当次验收证据，不把会被下一次提交立即作废的提交号固化进上表两行。

## 当前发布证据

- 2026-07-22 TradingDatas Bearer 消费合同已完成离线 fixture/mock 与仓库合同验收；此前记录的聚焦、quick 与全仓数字只对应当时冻结字节，不替代后续候选 fresh 测试。它不证明 TA token 已发放、formal endpoint 已由 TA 读取或生产已激活。
- 仓库 consumer 合同已把 provider-native rows 与 envelope source proof 对齐：rows 不再被要求携带伪造的 `available_time/revision_id/receipt_id`；dataset-specific identity/domain-event 映射、`current_observation`、`historical_pit_eligible=false`、受 `max_pages/max_rows` 限制的 opaque-cursor 遍历、跨页 metadata/identity/顺序守恒和同一 observation 双跑均由仓库测试门禁覆盖。本次合同验收没有联网、读取 token、部署或触碰 TradingDatas；主线与远端状态仍以本轮发布后的 fresh Git readback 为准。
- 最近已归档主线证据：review #15 候选全仓后端 `3296 passed`，架构/退役聚焦集合 `50 passed`；Ruff format/check 和 `git diff --check` 通过。更早 Crypto 原子候选的 Crypto 永久 lane 定向证据为 `144 passed`。任何后续候选必须在最新字节上重新运行测试与独立 review；这些旧数字不替代当前候选验收。
- 独立审计：Crypto 原子候选最终为 `P0=0 / P1=0`；review #15 的架构文档与旧执行退役复核也为 `P0=0 / P1=0`。保留的 P2 是 package-private writer 只防合作式调用、未来多标的需要账户级 PIT mark 完整性，以及祖先目录 symlink 加固，不影响当前 fixture-only 发布门禁。
- GitHub：reviews #12/#13/#14/#15 的 `front`、`test` CI 均成功；review #13 的 merge commit `b38838a02397cc080160e6cf3dae7c47757d9c85` 是服务器已验证功能字节。review #14 仅修改状态文档；review #15 包含 fail-closed 功能加固，只完成本地与 GitHub CI，不提升服务器旁路或现役状态。当前主线精确 SHA 以 fresh Git readback 为准。
- 服务器：后端 `3294 passed, 218 subtests passed`；A股/CNFutures fixture 聚焦集合 `167 passed`；前端 44 个测试文件、`297 passed`，lint 为 0 warnings/0 errors，`build:all` 通过；扩展 `compileall` 覆盖 `shared/Ashare/CNFutures/Crypto/tools/scripts`。
- 冻结模拟盘：A股基线首次运行/同根重放/跨根输出字节一致；Crypto 首次运行、同根幂等重放和跨根业务 bundle 字节一致，且保持 `execution_eligible=false / execution_authority=false / durable_execution_receipt=false / local_fixture_opening_baseline_only`；CNFutures fixture 聚焦集合通过。
- API canary：仅监听 `127.0.0.1:18787`；health/snapshot 为 200，`Cache-Control: no-store`，POST 为 405、未知路由为 404，顶层 `mode=simulated` 且所有真实交易标志为 false；随后按精确 PID 停止并确认端口关闭。
- 现役未变：Git HEAD `6c12fbed29db925019f85a6016774626f63b857a`、49 条既有未跟踪运行/回滚资产、systemd unit、PID `1043`、8787 listener/health、root 与 `marketgraph` 两份 crontab 前后逐项一致。Git status 摘要为 `2ac8dc6a...a74f9`，unit 摘要为 `9128f159...2307`，`marketgraph` crontab 摘要为 `af3605a8...fc9a`，root crontab 摘要为 `b104d546...940a`。
- 失败证据：`/opt/investment/release-evidence/tradingagent/20260720T200109Z-ta-crypto-fixture-4baaa90` 固定记录服务器 `3293 passed / 1 failed` 的文件遍历顺序问题，结果为 `server_sidecar_failed_pre_canary`；未启动 canary，现役保持不变。
- 成功证据：`/opt/investment/release-evidence/tradingagent/20260720T200846Z-ta-crypto-fixture-b38838a`；排序证据清单摘要为 `1db30f05...b6715724`，文件均收紧为 `0600`、目录为 `0700`。结果固定为 `server_validated_non_authority_simulation_only`、`active_production_activated=false`。

以上每条只证明其标注层。旁路验证期间 `TRADINGDATAS_API_URL`、旧名墓碑 `SHAREDSIGNALS_API_URL` 与 `MARKETGRAPH_API_URL` 均显式为空，DeepSeek 网络关闭，未调用 broker 或真实交易。

## 当前架构边界

1. **TradingDatas consumer**：只消费显式配置的 `GET /v1/catalog` 与 `POST /v1/query`。八字段 QueryRequest、完整 envelope、逐 dataset filters/as-of policy、nullable source proof、provider-native rows、identity/event mapping 和 page/row budgets 均 fail closed。receipt/data-through/observed-at/lineage 只从 envelope 绑定；domain event-time 不冒充可知时间，缺 first-seen/revision authority 时固定 current-observation、禁止历史训练资格。完整 probe 透明遍历 cursor 到 terminal page并双跑；循环、metadata drift、重复 identity、预算超限或顺序漂移均拒绝。Bearer 只由最终 transport 从受限 TA token file 注入；认证失败及 dataset 异常无旧链、文件或 provider fallback。
2. **A股三层 Universe**：只有沪深主板普通股进入个股分析、候选、预测、模拟仓位与订单；创业板、科创板和北交所个股不分析、不交易，其指数及全市场行业汇总仅作 `context_only` 市场环境证据。
3. **小资金组合**：A股和 CNFutures 各有独立 50,000 CNY simulated authority；A股保留100股买入单位、卖出零股例外、T+1、费用/滑点、最低经济订单、no-trade band、现金和六维投资论点风险门。Crypto 只有已合并但仍隔离的 10,000 USDT `local_fixture_opening_baseline_only`，没有 current/runtime/live capital authority。以上参数是风险上界，不是收益承诺。
4. **市场隔离**：A股、CNFutures、Crypto 使用独立 market kernel、账户、原生币种、订单/成交状态与未来 adapter family；只共享机械基础设施，不换汇、不跨市场汇总货币金额/收益/回撤、不净额或复用 broker payload。PM/US/HK 的仓库级 runtime、包装器和专用测试已从主线物理退役；服务器安装态若仍有旧引用，只能进入清理证据链，不能成为兼容回退。
5. **研究与自动化**：Opportunity Radar/Ledger、多期限 forecast、三风格 router、Decision Ledger、RunBundle、label maturity 与 counterfactual 仍是 shadow/nonpromotion 合同；没有 live scheduler 时不声称自动模拟盘已持续运行。
6. **LLM**：DeepSeek 仅能产生带 provenance 的 evidence，默认网络关闭，不能输出订单、仓位、目标权重或风险预算；一次历史 schema-rejected 请求不等于 accepted evidence。
7. **自我进化**：生产模型只能自动收紧、隔离或降级；晋级、恢复和扩风险仍需显式人工复核。当前没有用最近盈亏自动改写 Champion 或生产参数。

## 退役与兼容状态

- Mini/Hermes webhook、file consumer、`RealSignalQueue` 和专用源码已在仓库层 `RETIRED_BLOCKED`，不得恢复。零值环境变量和 fail-closed wrapper 目前只是安装态墓碑；服务器/旧 Mini 的 cron、process、port 与历史回执仍需独立清零证据。
- PM/US/HK 专用包、模拟 wrapper、配置、策略、测试及旧 StyleRunner/PerformanceTracker 执行栈已从仓库主线物理删除。冻结的历史输出只允许法证读取；不得参与当前市场状态、收益、交易量、readiness、自我进化或执行决策。
- 旧 A股数据 reader、screening/research、review/runtime wrapper 与多市场 wrapper 仍按 `active-compatibility / retirement-pending / hard-blocked` 分类。只有满足 `shared/governance/legacy_inventory.yaml` 的消费者清零、同 `as_of` parity、已安装 runtime readback 和回滚证据后才可物理删除。
- 2026-07-20 服务器 readback 仍发现安装态 cron 指向旧 SharedSignals 与旧 TradingAgent wrapper。它们不是新架构依赖，但在迁移完成前不能靠删除仓内 tombstone 假装退役。
- 2026-07-20 本地只读检查确认 `~/Desktop/Investment` 已不存在，且本机 LaunchAgents、当前进程与 Nicholas 用户 crontab 的精确路径/Hermes/Mini 扫描均未发现引用；它已从 active legacy inventory 移除。此事实不推断服务器或其它主机副本也已清理。

## 明确未完成

- TradingDatas owner 的上游 handoff 已声明 formal loopback `127.0.0.1:18082` 提供冻结的 provider-native row shape、schema major 2 与五个 active dataset；这只是上游声明。本次仓库合同验收未联网、未读取 TA token、未独立重放 catalog/query 或 same-observation parity，因此不能把 handoff 写成 TA live acceptance、生产 readback 或部署证据。
- 上游 handoff 同时声明日线首轮 parity 必须使用精确 `trade_date` filter，受控日期分区可完整分页，而无界跨分区查询会超过上游进度预算。TA 必须通过显式 filters、`max_pages/max_rows` 与 terminal cursor 证明完整读取，不允许无界重试、第一页截断或其它数据路径。
- 正式 TA 接入仍缺独立 credential/服务身份 readback 与 authenticated consumer parity。本次变更没有生成、配置或安装 token，也没有修改 service、cron、生产文件或入口；任何后续联调仍须使用发布侧独立 TA-scoped token 和仓外 manifest，且全程 simulation-only、无 fallback。
- 尚未安装 current-v1 live paper scheduler，也未积累真实 TradingDatas 驱动的连续 20 个交易日自动模拟和 60–120 个交易日冻结 OOS 样本。
- 现役服务器仍运行旧源码与旧调度；本轮 sidecar 没有切换 service、cron、页面或公网路由。
- 没有 accepted DeepSeek evidence、真实 broker/account、公开 ingress 或真实交易授权；`REAL_TRADING_ENABLED=false`。

## 下一阶段入口

1. 三个市场任务此后只在各自长期 lane 写域独立推进；共享合同/治理修改先由单一 shared-kernel owner 合入 `main`，再在干净检查点同步三条 lane，禁止市场线程直接双写 shared/root。
2. 下一独立阶段由发布侧安装 TA-scoped token、生成仓外显式 v2 manifest，并对 handoff 的 formal endpoint 运行只读 integration probe；daily 固定同日 `trade_date` filter，任何认证、dataset、source-proof、cursor、metadata、identity 或预算异常均 fail closed且不切换数据路径。本轮仓库合同通过不替代该联调证据。
3. 完成真实数据 parity 与旧消费者清零后，再独立发布 current-v1 自动模拟 scheduler，并验证 crash/restart、对账、幂等和持续运行。
4. 连续 20 个交易日工程闭环后评估出口，再积累 60–120 个交易日 OOS/多状态样本。月收益 20% 只作为收益分布上尾指标，不是强制交易、满仓或 PASS 条件。
