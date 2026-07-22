# TradingAgent 当前状态

> 最后更新：2026-07-23 CST。本文件只记录当前代码、服务器旁路、现役 runtime 与外部依赖的分层事实；长期规则见 [AGENTS.md](AGENTS.md)，运行与回滚见 [docs/operations.md](docs/operations.md)。历史候选、旧测试数字与作废证据从 Git 和服务器只读证据目录审计，不在这里维护流水账。

## 当前结论

GitHub review #23 已把 catalog-driven parity、专用服务身份、front unit 与安全 cron
迁移的仓库合同普通合并到 `main`，merge commit 为 `1907df9`。这些机器条目仍是
`TARGET_CONTRACT / repository_contract / production=false`：合并只证明合同存在，
不证明 12-profile parity、credential、front、worker、timer 或生产 runtime 已切换。

随后完成的 local/server identity preflight 只创建了
`tradingagent:tradingagent`（UID/GID 均为 `987`）和一份 immutable release。它没有
切换 current 指针或 front，没有安装 fresh TA-scoped token，也没有应用 tmpfiles；
observation timer 继续保持未安装、未启用。对既有 secret 路径只做了 metadata-only
读回：`/run/secrets/tradingagent` 为 `0700 marketgraph:marketgraph` 目录，既有叶
文件为 `0600 marketgraph:marketgraph` 的 regular single-link file（`nlink=1`）。
这不是 TA credential handoff，也没有读取或记录 token 值/内容哈希。preflight 也没有
停止或隔离既有 `tradingagent-front-api.service`；其当前 PID/cgroup 仍是由旧
`marketgraph` UID 运行的 TA legacy front，因此零-holder gate 尚未通过。

最新 TradingDatas 上游 handoff metadata 指向 production `c3232d0…`、
`catalog_version=v1-fcc1aaa39c20743e`，目录为 190 total / 12 active / 178 paused，
其中 5 ready / 7 impaired；新增 3 项为 `partial/degraded`，
`cn.dataset.suspend_d` 为 `stale/degraded`。这与 review #23 中 190/9/181、
5 ready/4 impaired 的 **旧九 profile fixture** 是两层证据。TA 尚未运行 12-profile
authenticated parity，当前状态固定为 **NOT RUN / BLOCKED**；仍缺覆盖全部 12 项的
获批 secret-free profiles 和 fresh TA credential，本文件不声明二者已经生成或存在。

全系统继续保持 **simulation-only / 无真实交易权限**，
`REAL_TRADING_ENABLED=false`。2026-07-22 的五项 formal API one-shot 仍只是使用旧
`marketgraph` 只读 credential 的历史兼容 observation；它不能替代最新 12-profile
TA authenticated parity。仓库主线、服务器 identity/release preflight、现役
current/front、credential、cron/timer 和真实交易 authority 继续分别验收。

## 六层事实

| 层级 | 当前事实 | 不能据此推断 |
|---|---|---|
| 本地主线 | review #23 的 catalog parity/identity 仓库合同已进入本地与远端 `main`；精确一致性仍用本轮 `git rev-parse` 读回 | `TARGET_CONTRACT` 合入主线不等于服务器 12-profile parity、credential 或 runtime 激活 |
| 本地发布候选 | review #23 之后的 paused-cron merge hardening 与本次 truth sync 仍是隔离候选；机器状态保持 `TARGET_CONTRACT / production=false` | 候选测试不等于已 apply cron、tmpfiles、token、current/front 或 timer |
| GitHub 主线 | review #23 已普通合并 catalog parity、sysusers/front 与 cron 迁移合同 | GitHub main 不等于上游 catalog metadata 已成为 TA authenticated parity，也不等于服务器进程加载 |
| 服务器旁路/preflight | 既有 `0dfcb6d...` sidecar 证据仍只属历史功能旁路；最新 preflight 另创建 UID/GID 987 与 immutable release | immutable release 和 identity 存在不等于 current/front/token/tmpfiles/cron/timer 已切换 |
| 服务器现役 | current 指针与 front 未切换；既有 front PID/cgroup 仍是旧 `marketgraph` UID 的 TA legacy service；fresh TA-scoped token/tmpfiles 未安装，timer 未安装/未启用 | 不能把 UID/GID-only preflight 或 immutable release 解释为 token 已安装、零-holder 或 full cutover |
| 外部能力 | 最新上游 catalog handoff 是 190/12/178、5 ready/7 impaired 的 metadata；TA 12-profile authenticated parity 为 `NOT RUN / BLOCKED` | 不能把 catalog metadata、旧九 profile fixture 或旧五项 one-shot 当成 12-profile TA parity；不证明 profiles/token 存在 |

本地主线与远端主线的 fresh readback 统一使用 `git rev-parse HEAD origin/main`；结果只写入当次验收证据，不把会被下一次提交立即作废的提交号固化进上表两行。

## 当前发布证据

- review #23 / `1907df9` 已合并 catalog parity 与 identity 的仓库合同。其 190/9/181、5 ready/4 impaired 只来自九 profile fixture，全部 `research_snapshot_eligible=false`，不得改造成 12 项生产 fixture。最新上游 handoff metadata 为 production `c3232d0…`、catalog `v1-fcc1aaa39c20743e`、190/12/178、5 ready/7 impaired；新增三项 `partial/degraded`，`cn.dataset.suspend_d` 为 `stale/degraded`。TA 12-profile authenticated parity 尚未运行，因缺全 12 项获批 secret-free profiles 与 fresh TA credential 固定 blocked。
- identity preflight 已在服务器创建 `tradingagent:tradingagent` UID/GID 987 和 immutable release；这是可诚实声明的 UID/GID-only preflight，不是 token/full cutover。current/front 未切换，既有 front PID/cgroup 仍由旧 `marketgraph` UID 运行；fresh TA-scoped token 未安装，tmpfiles 未应用，timer 未安装/未启用。既有 `/run/secrets/tradingagent` 与叶文件仅按 metadata 读回为 `0700 marketgraph:marketgraph` 目录和 `0600 marketgraph:marketgraph` regular、`nlink=1` 叶；未读取内容，也不得把该旧叶称为 TA credential。

- 2026-07-22 TradingDatas Bearer 消费合同已完成离线 fixture/mock 与仓库合同验收；此前记录的聚焦、quick 与全仓数字只对应当时冻结字节，不替代后续候选 fresh 测试。它不证明 TA token 已发放、formal endpoint 已由 TA 读取或生产已激活。
- 2026-07-22 新 A股候选使用现有 `marketgraph` 只读 token 仅完成一次 formal 18082 compatibility observation：五数据集 bounded/same-observation probe、3041只主板投影和 exact replay 均通过；snapshot `6c44ab3d...`，未产生 capital/order/fill/outbox/reconcile/journal。完整读回见 `docs/reports/2026-07-22-ashare-observation-readback.md`。这不授权长期复用该身份或 token。
- 上述 `a7488e9` 读回是保留的真实历史证据，但只包含 snapshot/probe/aggregate observation receipt 旧三件绑定；它没有逐股 membership ledger、forward-history readiness 或 paper-planning decision。新候选必须使用 fresh state root，不得补写旧状态或把旧 `tradable_*` 别名当作订单 authority。
- 2026-07-22 A股五项绑定候选按 canonical `tests/ta_v1_candidate_manifest.txt` 执行得到 `2366 passed`，全后端测试得到 `3633 passed`；Ruff、compileall、YAML parse 与 `git diff --check` 通过，独立功能/安全审计均为 `P0=0 / P1=0`。review #20 合并后，review #21 又以独立回归测试修复粗时间戳文件系统上的 token 原地改写检测缺口；该修复本地全后端仍为 `3633 passed`。
- 仓库 consumer 合同已把 provider-native rows 与 envelope source proof 对齐：rows 不再被要求携带伪造的 `available_time/revision_id/receipt_id`；dataset-specific identity/domain-event 映射、`current_observation`、`historical_pit_eligible=false`、受 `max_pages/max_rows` 限制的 opaque-cursor 遍历、跨页 metadata/identity/顺序守恒和同一 observation 双跑均由仓库测试门禁覆盖。本次合同验收没有联网、读取 token、部署或触碰 TradingDatas；主线与远端状态仍以本轮发布后的 fresh Git readback 为准。
- 最近已归档主线证据：review #15 候选全仓后端 `3296 passed`，架构/退役聚焦集合 `50 passed`；Ruff format/check 和 `git diff --check` 通过。更早 Crypto 原子候选的 Crypto 永久 lane 定向证据为 `144 passed`。任何后续候选必须在最新字节上重新运行测试与独立 review；这些旧数字不替代当前候选验收。
- 独立审计：Crypto 原子候选最终为 `P0=0 / P1=0`；review #15 的架构文档与旧执行退役复核也为 `P0=0 / P1=0`。保留的 P2 是 package-private writer 只防合作式调用、未来多标的需要账户级 PIT mark 完整性，以及祖先目录 symlink 加固，不影响当前 fixture-only 发布门禁。
- GitHub：review #23 已把 catalog parity、sysusers/front 与 cron 迁移合同合入 `main`；其状态仍是 `TARGET_CONTRACT / production=false`。此前 review #20/#21 的功能旁路 SHA `0dfcb6d737943f33059ca8289b3b825ced0b00cf` 仍只证明当时冻结字节，不能继承为 review #23 的服务器激活或 12-profile parity 证据。
- 服务器：`0dfcb6d...` 候选全后端 `3633 passed, 218 subtests passed`；CNFutures fixture 聚焦 `99 passed`；A股与 Crypto 均通过首次运行、同根幂等重放和跨根业务 artifact 字节一致；前端 44 个测试文件、`297 passed`，lint 为 0 warnings/0 errors，`build:all` 通过；扩展 `compileall` 覆盖 `shared/Ashare/CNFutures/Crypto/tools/scripts`。
- 冻结模拟盘：A股基线首次运行/同根重放/跨根输出字节一致；Crypto 首次运行、同根幂等重放和跨根业务 bundle 字节一致，且保持 `execution_eligible=false / execution_authority=false / durable_execution_receipt=false / local_fixture_opening_baseline_only`；CNFutures fixture 聚焦集合通过。
- API canary：仅监听 `127.0.0.1:18787`；health/snapshot 为 200，`Cache-Control: no-store`，POST 为 405、未知路由为 404，顶层 `mode=simulated` 且所有真实交易标志为 false；随后按精确 PID 停止并确认端口关闭。
- 既有 sidecar 验收结束时的现役 HEAD、未跟踪运行/回滚资产、systemd unit、8787 listener/health 与两份 crontab 一致性只属于该次历史证据。最新 identity preflight 已新增 UID/GID 987 与 immutable release，但没有切 current/front、fresh token、tmpfiles 或 timer；不得继续用“服务器完全未变”概括这两层事实。
- 失败证据：`/opt/investment/release-evidence/tradingagent/20260720T200109Z-ta-crypto-fixture-4baaa90` 固定记录服务器 `3293 passed / 1 failed` 的文件遍历顺序问题，结果为 `server_sidecar_failed_pre_canary`；未启动 canary，现役保持不变。
- 成功证据：`/opt/investment/release-evidence/tradingagent/20260720T200846Z-ta-crypto-fixture-b38838a`；排序证据清单摘要为 `1db30f05...b6715724`，文件均收紧为 `0600`、目录为 `0700`。结果固定为 `server_validated_non_authority_simulation_only`、`active_production_activated=false`。
- 新失败证据：`/opt/investment/release-evidence/tradingagent/20260722T174223Z-ta-ashare-phase1-febafd6` 固定记录 review #20 合并字节在 ext4 上的 `3632 passed / 1 failed`；失败用例为 token 文件读取期间内容改变未稳定 fail closed。该候选在 canary 前停止，没有晋级、覆盖或清理。
- 新成功证据：`/opt/investment/release-evidence/tradingagent/20260722T175724Z-ta-token-reread-0dfcb6d`；65 个证据文件逐项校验通过，排序清单 `evidence.sha256` 的摘要为 `9caeda18766d312fcaec501957dd327b7d195372a8eade3f054438027d814cc5`。现役 HEAD、49 条 status、service/PID、全部 `tradingagent*` unit/timer、两份 crontab、secret 路径元数据和 8787 健康在旁路前后保持一致；`active_production_activated=false`。

以上每条只证明其标注层。旁路验证期间 `TRADINGDATAS_API_URL`、旧名墓碑 `SHAREDSIGNALS_API_URL` 与 `MARKETGRAPH_API_URL` 均显式为空，DeepSeek 网络关闭，未调用 broker 或真实交易。

## 当前架构边界

1. **TradingDatas consumer**：只消费显式配置的 `GET /v1/catalog` 与 `POST /v1/query`。八字段 QueryRequest、完整 envelope、逐 dataset filters/as-of policy、nullable source proof、provider-native rows、identity/event mapping 和 page/row budgets 均 fail closed。receipt/data-through/observed-at/lineage 只从 envelope 绑定；domain event-time 不冒充可知时间，缺 first-seen/revision authority 时固定 current-observation、禁止历史训练资格。完整 probe 透明遍历 cursor 到 terminal page并双跑；循环、metadata drift、重复 identity、预算超限或顺序漂移均拒绝。最新 12-active catalog handoff 目前只是上游 metadata，不能替代 TA authenticated parity；旧五项 one-shot 也不能扩张为全 active-set 证明。长期 worker 只允许由 fresh TA-scoped token-file 注入；认证失败及 dataset 异常无旧 credential、8082、文件或 provider fallback。
2. **A股 Universe 分层**：当日 `observation_universe` 只是主板观察初筛，它不是 Account Tradable Universe、Small-Capital Feasible Universe、候选、仓位或订单池。只有后层独立权威全部通过的沪深主板普通股才可继续；创业板、科创板和北交所个股不分析、不交易。`index_classify`/`sw_daily` 只是行业分类/行业指数 `context_only`；当前没有成分 denominator 与 coverage authority，不声称完整行业宽度。
3. **小资金组合**：A股和 CNFutures 各有独立 50,000 CNY simulated authority；A股保留100股买入单位、卖出零股例外、T+1、费用/滑点、最低经济订单、no-trade band、现金和六维投资论点风险门。Crypto 只有已合并但仍隔离的 10,000 USDT `local_fixture_opening_baseline_only`，没有 current/runtime/live capital authority。以上参数是风险上界，不是收益承诺。
4. **市场隔离**：A股、CNFutures、Crypto 使用独立 market kernel、账户、原生币种、订单/成交状态与未来 adapter family；只共享机械基础设施，不换汇、不跨市场汇总货币金额/收益/回撤、不净额或复用 broker payload。PM/US/HK 的仓库级 runtime、包装器和专用测试已从主线物理退役；服务器安装态若仍有旧引用，只能进入清理证据链，不能成为兼容回退。
5. **研究与自动化**：新候选每日逐 symbol 绑定 membership 与排除原因；只有同一私有 state root/session lock 内重验五项 committed binding 的 strict loader 才能产生 verified bundle，并将 membership/transaction-complete hash 贯穿 runtime/history/planner/mark lineage。history 至少需要 21 个 forward-collected session 才覆盖 20 日 momentum/volatility 的最小窗口，但缺交易日连续性与公司行动/复权 authority 时即使计数达标也仍然 blocked。当前 ledger 不启用标签，daily-only planner 固定 `paper_trade_session=null`、`abstain/completed_with_blocks`，不产生资金、订单或成交副作用。Opportunity Radar/Ledger、多期限 forecast、三风格 router、Decision Ledger、RunBundle、label maturity 与 counterfactual 仍是 shadow/nonpromotion 合同；没有 live scheduler 时不声称自动模拟盘已持续运行。
6. **LLM**：DeepSeek 仅能产生带 provenance 的 evidence，默认网络关闭，不能输出订单、仓位、目标权重或风险预算；一次历史 schema-rejected 请求不等于 accepted evidence。
7. **自我进化**：生产模型只能自动收紧、隔离或降级；晋级、恢复和扩风险仍需显式人工复核。当前没有用最近盈亏自动改写 Champion 或生产参数。

## 退役与兼容状态

- Mini/Hermes webhook、file consumer、`RealSignalQueue` 和专用源码已在仓库层 `RETIRED_BLOCKED`，不得恢复。零值环境变量和 fail-closed wrapper 目前只是安装态墓碑；服务器/旧 Mini 的 cron、process、port 与历史回执仍需独立清零证据。
- PM/US/HK 专用包、模拟 wrapper、配置、策略、测试及旧 StyleRunner/PerformanceTracker 执行栈已从仓库主线物理删除。冻结的历史输出只允许法证读取；不得参与当前市场状态、收益、交易量、readiness、自我进化或执行决策。
- 旧 A股数据 reader、screening/research、review/runtime wrapper 与多市场 wrapper 仍按 `active-compatibility / retirement-pending / hard-blocked` 分类。只有满足 `shared/governance/legacy_inventory.yaml` 的消费者清零、同 `as_of` parity、已安装 runtime readback 和回滚证据后才可物理删除。
- 服务器安装态 cron 的最新已保存 readback 仍指向旧 SharedSignals 与旧 TradingAgent wrapper；identity preflight 没有应用 paused TA cron。paused PASS 必须由 merge tool 原子安装并同时读回：`0` 条 TA recurring job、`# TRADINGAGENT_SCHEDULE_STATE=paused_until_tradingdatas_fresh_handoff` 恰好 `1` 次、non-TA 行的字节/顺序/有效环境赋值不变；任一缺失都不能靠删除仓内 tombstone 假装退役。
- 2026-07-20 本地只读检查确认 `~/Desktop/Investment` 已不存在，且本机 LaunchAgents、当前进程与 Nicholas 用户 crontab 的精确路径/Hermes/Mini 扫描均未发现引用；它已从 active legacy inventory 移除。此事实不推断服务器或其它主机副本也已清理。

## 明确未完成

- 2026-07-22 formal loopback 的五项 observation one-shot 使用旧 `marketgraph` 只读 credential，仅限历史兼容验收；它不是 latest active-set parity。最新上游 handoff 的 catalog/version/counts 已记录，但 TA 对 12 个 active profile 的 authenticated parity 为 `NOT RUN / BLOCKED`，仍缺覆盖全部 12 项的获批 secret-free profiles 与 fresh TA credential；本文件不声明 profiles 或 credential 已存在。
- 上游 handoff 同时声明日线首轮 parity 必须使用精确 `trade_date` filter，受控日期分区可完整分页，而无界跨分区查询会超过上游进度预算。TA 必须通过显式 filters、`max_pages/max_rows` 与 terminal cursor 证明完整读取，不允许无界重试、第一页截断或其它数据路径。
- 正式 TA 持续接入已完成 sysusers-only 的 UID/GID 987 preflight，机器门禁允许只声明这一事实，但禁止据此声称 token 已安装或 full cutover。current/front 未切，legacy front 仍以旧 `marketgraph` UID 运行；fresh TA-scoped token 未安装、tmpfiles 未应用、paused cron 与全量零-holder 证据未完成。零-holder 前必须先停止/隔离 legacy front，随后立即扫描全部 TA service/cgroup 名称、旧 `marketgraph` UID、新 `tradingagent` UID 987 及其它 UID。长期运行必须使用发布侧独立 TA-scoped token 和仓外 manifest，且全程 simulation-only、无 fallback。
- 长期 observation worker 必须使用专用 `tradingagent:tradingagent` 身份；旧 `marketgraph:marketgraph` 一次性运行和既有 secret 叶只是历史兼容证据。timer 仍未安装/未启用，也缺 fresh credential、12-profile parity 与可信每日 immutable manifest rollover；禁止安装/启用静态 `as_of` 重放调度。
- 当前 active 日线数据不具备 bid/ask、盘口数量、30秒 freshness或分钟成交量 authority；自动 paper day 只能积累观察、评估历史/特征 readiness 和记录 `abstain/completed_with_blocks` plan，不能从日线合成模拟 fill。
- T 日收盘 observation 只能在预测前冻结交易日历授权后映射到 T+1；当前 planner 不能自行推导下一 session。membership ledger 固定无 label horizons，缺 calendar/minute/market-truth/adjustment authority 时不得回填标签。
- 尚未安装 current-v1 live paper scheduler，也未积累真实 TradingDatas 驱动的至少 21 个 forward-collected observation session、20 个连续单日收益区间（不假设统计独立）和 60–120 个交易日冻结 OOS 样本。
- 现役服务器仍运行旧源码、旧调度与旧 `marketgraph` front；identity/release preflight 没有切换 service、cron、页面或公网路由。
- 没有 accepted DeepSeek evidence、真实 broker/account、公开 ingress 或真实交易授权；`REAL_TRADING_ENABLED=false`。

## 下一阶段入口

1. 三个市场任务此后只在各自长期 lane 写域独立推进；共享合同/治理修改先由单一 shared-kernel owner 合入 `main`，再在干净检查点同步三条 lane，禁止市场线程直接双写 shared/root。
2. 下一独立阶段严格按 [operations](docs/operations.md) 顺序推进：backup/preflight → sysusers UID/GID-only readback → merge-tool 原子应用 paused TA cron并读回 `0` TA job + `# TRADINGAGENT_SCHEDULE_STATE=paused_until_tradingdatas_fresh_handoff` 恰好 `1` 次 + non-TA 字节/顺序/环境不变 → 停止并隔离 legacy `tradingagent-front-api.service` → 立即扫描全部 TA service/cgroup、旧 `marketgraph` UID、新 `tradingagent` UID 987 与其它 UID 的 process/cgroup/cwd/root/open-FD/mmap holder并证明全零 → 协调 credential freeze → 应用 tmpfiles → publisher 原子安装 fresh `0600 tradingagent:tradingagent` regular/single-link TA 叶 → metadata-only readback → unfreeze。暂停后失败固定 consumer unavailable；front 停止后失败还必须保持 stopped/isolated，只允许修复前滚，不恢复旧 front/credential/TA cron，也不回退 8082。随后仍须完成 12-profile authenticated parity 与 daily immutable manifest/as-of rollover；在此之前 timer 保持未安装/未启用。
3. 并行积累 current-observation 与真实历史覆盖；分钟/L1 authority 未齐时自动 paper scheduler 只记录 `abstain/completed_with_blocks` plan。分钟证据达到冻结合同后，再开放 simulated reserve/fill/outbox/capital commit，并验证 crash/restart、对账、幂等和持续运行。
4. 至少 21 个 forward-collected session/20 个连续单日收益区间（不假设统计独立）的工程闭环后评估出口，再积累 60–120 个交易日 OOS/多状态样本。月收益 20% 只作为收益分布上尾指标，不是强制交易、满仓或 PASS 条件。
