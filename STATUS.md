# TradingAgent 状态

> **给所有 agent：** 读完 [AGENTS.md](AGENTS.md) 理解规则后，读本文件理解"现在在哪、要去哪、能做什么"。
>
> **⚠️ 变更后必须更新本文件。**
>
> 最后更新：2026-07-11 (市场因果终端与 SharedSignals 行情脉冲已发布)

---

## 一、当前状态

- **市场覆盖、显式归因与观察轨迹已发布（2026-07-11）**：功能运行时提交为 `512cb89`。非 A 股行情脉冲只接受显式 `marketDataSymbol`，A 股可使用交易代码；不会猜测 BTC、期货合约或预测市场 ID。只读 API 保留最多 12 次 fresh 覆盖观察，缓存命中不追加；公网连续观测已达到 12 条上限，当前 A 股 `000776.SZ` 为 `sourced`，US/Crypto/HK/PM/CNFutures 均诚实显示 `no_representative`。A 股模拟持仓仅在剩余头寸来自唯一、已记录买单时透传 `order_id`，多来源头寸省略归因，不改变现金或 PnL。发布前本地 local-sim 20 项、前端 40 个测试文件/188 项、lint、客户端/API 构建和 quick acceptance 195 项通过；生产隔离测试环境 `/opt/investment/tools/venvs/tradingagent-test` 的 quick acceptance 同样 195 项通过。真实浏览器已验证机会周期 5/5 阶段、事件账本 31→8 筛选及 back/forward 恢复；公网资源为 `index-ZYsISdCB.js`，服务 active，内部健康与公开页面/快照均正常。冗余回滚已清理，只保留 `20260711141550` 与 `20260711143515` 两组。仍不改变队列、资金、策略、cron、执行器、账户或实盘边界。

- **市场因果终端已发布（2026-07-11）**：设计提交 `2a831cd`、功能提交 `0ebf81f` 已同步 GitHub 与生产源码。前端新增只读 SharedSignals 行情脉冲、真实点位 sparkline、按 `opportunity` URL 关联的机会周期/原始事件筛选、`⌘K` 命令面板、焦点闭环与版本化本地密度/列偏好；缺行情继续显示 `—`，不合成价格。生产使用 Node v24.4.1 构建，资源为 `index-BO0Kz8nR.js`，`tradingagent-front-api.service` active，内部健康与公开页面/快照均为 HTTP 200，公开构建标识为 `20260711-market-causal-terminal`，公开快照已返回 `marketPulses` 和 `sharedSignalsMarketPulse` 来源。发布前 lint、40 个测试文件/182 项测试、客户端/API 构建通过，quick acceptance 本地环境 195 项通过；1440×900 与 1280×720 无横向溢出，命令面板、焦点闭环与密度切换通过真实浏览器验证。发布当时系统 Python 缺少 pytest；现已在仓库外建立隔离只读测试环境并完成 195 项生产 quick acceptance。旧回滚戳 `20260711130915` 与 `20260711135014` 已在后续版本健康验收后清理。仍不改变队列、资金、策略、cron、执行器、账户或实盘边界。

- **前端证据自适应终端已发布（2026-07-11）**：功能提交为 `0cc285e`，首次合并主线为 `288804c`。顶部导航、市场头、二级页指标和检查器统一使用 `live / idle / stale / degraded` 心跳，当前无 `pending` 时明确显示“调度正常 · 当前空闲”；收益页按真实证据在 520px 活跃图、300px 静默图和等待状态间切换；空持仓改为展示敞口、可用资金、最近关闭结果和快照时间；过程页把真实 `funnelEvents[]` 按机会聚合为五阶段因果链，并保留下方原始事件账本。内部策略/来源代码统一翻译，外层装饰渐变已移除，保持更接近 Hyperliquid 的平直连续终端。main 合并后 lint、36 个测试文件/166 项测试、客户端和只读 API 构建通过；1440×900 检查总览/收益/过程/持仓，1280×720 六页无横向溢出或控制台错误，设计评分 94/100。生产备份戳 `20260711130915`，Node v24.4.1 构建资源为 `index-Indyh1Gj.js` / `index-BjhIXV3N.css`，服务 active，内部健康和公开页面/快照为 HTTP 200，公开构建标识为 `20260711-evidence-adaptive-terminal`。真实生产快照为 signals 4、holdings 0、funnelEvents 31，过程页聚合为 4 个机会周期。仍是只读展示，不改变队列、资金、策略、cron、执行器、账户或实盘边界；移动端继续延期。

- **跨仓 cron 环境隔离与非交易日验收修复（2026-07-11，生产已发布）**：生产合并表曾把 TradingAgent schedule 追加在 MarketGraph `BASH_ENV` 之后，导致 TA cron 继承 MarketGraph loader 中的 SharedSignals token；该 token 对 localhost SharedSignals 返回 401，而交互式 TA loader 无 token时可正常读取，形成“交互检查通过、Crypto/PM/US cron 401”的环境漂移。合并安装器现会在 TA schedule 前重写 TradingAgent `BASH_ENV`，`cron_coverage` 逐条验证有效 loader，`market_health` 保留 scheduled reader degradation，禁止把 401 污染的 cron 结果伪装成策略等待。生产 crontab 已 apply 并复核 51/51 条、环境漂移 0、root 残留 0、权限阻塞 0，真实 cron 环境下 Crypto/PM/US reader 均无 401。A 股开盘验收同时接入交易日判断，周末或节假日直接记录 `ashare_non_trading_day`，不再把闭市日午后误报为缺 5 分钟 bar。GitHub 与生产源码已同步至 `c9e3523`；生产开盘验收 A 股/CNFutures 均 pass，`full_acceptance --profile prod` 为 warn，剩余 warn 仅来自 US 模拟盘数据等待，不再有 cron 环境或 A 股周末验收 fail。

- **A股/CNFutures 50,000 CNY 资金切换（2026-07-11，生产已迁移）**：代码级当前默认本金统一为 50,000 CNY，旧资金环境变量不能切回 100,000/200,000；A股生产 apply 已在保持 `shared/logs/local_sim/.local_sim.lock` inode 不变的前提下，把旧 200,000 元账本内容、持仓快照和 8 个历史 tier 文件归档到 epoch 1，并在原权威路径 bootstrap 50,000 元 epoch 2。旧 PnL/成交文件与归档 SHA256 一致，当前现金 50,000、持仓 0、成交文件为空，重复 apply 返回 `already_migrated`；state/metadata 不一致、状态损坏、归档碰撞或锁超时均 fail-closed，不存在 state-only 激活入口。CNFutures 未发现需迁移的活跃旧账本，生产默认本金已同步为 50,000。看板只展示当前主账户，忽略旧 tier manifest，并以当前主账本覆盖旧风格 PnL 分解。本地完整 Python 964 项、资金/迁移回归 113 项和前端 140 项测试通过，前端 lint 与客户端/API 构建通过。

- **2026-07-11 生产验收**：TradingAgent 运行时代码已发布至 `deee254`，SharedSignals 供数层已发布至 `e54e5bd`。SharedSignals 修正 Sina 周五夜盘跨午夜时把下一交易日误写为自然 `bar_time` 的问题，并清理 2 条部署前错误行；`/realtime_5min?market=Futures` 现按最大自然 `bar_time` 返回批次，生产实测返回 3 个铜合约、最新 `2026-07-11 01:00:00`、`degraded=false`。TradingAgent CNFutures 查询、live check 和单市场健康均通过；模拟 wrapper 读取 `universe_count=3`，因仅 1 个独立品种而按规则进入 `observation_only / insufficient_distinct_product_coverage`，`filled_count=0`、`error_count=0`、PnL 为 0。开盘验收中的 CNFutures 子项通过；总验收仍因独立的 `tradingagent_health.json=critical` 失败，并有 US 模拟盘 warn，不能归因到期货链路。

- **前端 Hyperliquid 终端语言重构已发布（2026-07-11）**：UI 发布提交为 `cbc77a5`。六页统一为连续终端画布、紧凑指标条、主数据面和 316px 只读 Automation Inspector；新增 Process Book、Portfolio Ledger 与 Risk Ledger，过程页只把真实 `pending` 记录算作运行中，无运行记录时回退最近完成结果；持仓页按 CNY/USD/百分比/多币种真实口径汇总并用横向敞口条替换大环图；风险页明确 5% 预警与 7% 限制；复盘字段改为只读“自动校准”。本地设计 QA 在 1440×900 对照当前 Hyperliquid 参考，并逐页验证 1280×720：六页均无横向溢出、浏览器无错误/告警，收益/回撤在顶部、终端指标与检查器之间使用同一结果。发布前 lint、27 个测试文件/139 项测试、前端构建和只读 API 构建全部通过。GitHub `main`、生产源码与静态资源已同步；生产使用 Node `v24.4.1` 构建，`tradingagent-front-api.service=active`，内部 `/healthz` 为 HTTP 200，公开 `https://dashboard.tradingagent.cc/` 为 HTTP 200 且加载 `index-BsAS4weL.js` / `index-B7yyFM8C.css`，公开快照包含 performance/signals/holdings/decisions/risk 五域；回滚副本戳 `20260711044810`。本次未改变快照 API 契约、交易队列、资金、策略、cron、执行器或真实资金行为；移动端继续延期。
- **前端终端运营层已发布（2026-07-11）**：功能发布提交为 `1098746`。在既有 Hyperliquid 连续终端语言上增加六市场状态带和五域证据健康；统一 `pending / completed / review` 状态解析；过程页新增按时间/序列排序的来源/延迟事件账本；持仓快照和模拟账本可选透传数量、均价、现价、成本、市值、当日盈亏与来源；风险账本纳入滞后/异常/隔离证据域；复盘直接展示置信度、影响、证据和自动校准。过程/事件/持仓/风险/复盘账本统一本地搜索、排序、列显隐，URL 保存 `page/market/range`，支持 `Alt+1…6`、`Alt+←/→` 和 `/` 快捷键。本地合并后 lint、32 个测试文件/156 项测试、前端与只读 API 构建通过；1440×900 同屏参考评分 92/100，1280×720 六页无文档级横向溢出，浏览器 back/forward 与真实快捷键通过。GitHub `main`、生产源码和静态资源已同步；生产备份戳 `20260711053556`，服务 active，内部健康与公开页面/快照均为 HTTP 200，公开构建标识为 `20260711-terminal-operations`，资源为 `index-JgfUxzbd.js` / `index-DdWiSlIC.css`。仍是只读展示，不改变队列、资金、策略、cron、执行器、账户或实盘边界；移动端继续延期。

- **Crontab 合并安装器（2026-07-10 精简重构，2026-07-11 环境隔离加固）**：`tools/merge_tradingagent_crontab.py` 采用单文件最小实现，仅剥离当前 crontab 中 `/opt/investment/tradingagent/` schedule 行，追加 `BASH_ENV=/opt/investment/tradingagent/shared/env_loader.sh` 与模板 TA schedule 行，不动 env/注释/空行/跨仓条目。合并后的组合 crontab 按文本位置解释环境变量，TradingAgent 块前插入自己的 `BASH_ENV` 确保不受前面仓库 loader 影响。默认 dry-run；`--apply` 备份 → 安装 → readback + coverage 验证，失败自动 rollback 并 readback 确认原文本恢复。`--current-file`/`--output` 文件模式不碰系统。空模板 fail-closed。**严禁**直接 `crontab shared/crontab.txt`。已通过 13 项关键用例（`tests/test_merge_tradingagent_crontab.py`）。`cron_coverage` 现检测已安装 crontab 中的环境继承错误：TA schedule 行上一条有效的 `BASH_ENV` 不是 TradingAgent 自己的 loader 时标记 `installed_crontab_environment_mismatch`。

- **A股事件、演化与资金门禁修复（2026-07-10）**：事件评分改为 SharedSignals 最近 3 日 SS-first，文本方向推断使用固定 0.30 审慎置信度并输出无方向/低置信度诊断，不把中性公告伪造成催化；演化层保持 `verified_5min_market_data`、正成交量和 -5/+15 分钟时间窗不变，同时输出具体拒绝原因；defensive `target_positions=0` 不再回退触发机会成本换仓，风险卖出仍保留，策略现金预算上限收紧到真实账户可用现金，A股卖出来源统一为 `ashare_rebalance_sell`。生产已发布并抽样验证：`300759.SZ` 的减持事件保留正式事件证据，`000776.SZ`、`600030.SH` 的中性公告不再伪造成催化；当前演化样本 3 个、eligible 0 个，其中 1 个执行证据类别未验证、2 个缺执行证据类别，继续禁止把浮盈当成已验证 alpha。

- **A股开盘验收移除 SQLite 依赖（2026-07-10）**：`ashare_opening_validator.py` 完全移除对兄弟 SharedSignals SQLite/read-model 文件的直接读取。`validate_pre_open()` 复用 `ashare_preopen_dry_run._api_daily_coverage_from_reader` 检查日线覆盖（含覆盖率门禁、日期新鲜度、intraday-vs-daily 日期门禁和 asset universe 可用性），asset_count<=0 时 fail-closed。`validate_opening()` 通过 `get_realtime_5min_batch()` API 检查 5 分钟数据，只计数 `_is_supported_ashare_code` 通过的 A 股标的，输出 `latest_bar_age_minutes`；API 异常、0 行、无 A 股标的、覆盖不足、最新 bar 超 10 分钟均 fail（不再 warn）。移除 `_check_api_availability` 私有属性检查，以实际 API 调用成功/异常为准。`first_sample_alerts()` 本地证据收集完全保留。`opening_acceptance.py` 聚合验收不再用 preopen dry-run 二次 API 调用把 API 不可用 fail 转为 pass；CNFutures 的 `sqlite_db` 恢复使用 `DEFAULT_SHARED_SIGNALS_DB`，A 股不传 sqlite-db。三个 wrapper 不传 A 股 --sqlite-db。

- **A股数据覆盖与正式收盘验收（2026-07-10）**：盘前/盘中执行只能使用 SharedSignals API 日线 read model，生产环境变量不能重新启用同机 SQLite 兜底。日线覆盖低于普通 A 股资产入口 90% 时，数据门禁和执行子门禁同时 fail-closed；生产实测覆盖率由 54.92% 逐步恢复至 90.38% 后自动转为 pass，没有人工放行。正式收盘任务在 17:40 首次运行、22:40 有界补跑，只接受目标交易日精确日线收盘价；任一持仓缺价时主账户、50,000/100,000 元实验账户与演化文件保持不变。生产已验证 3/3 持仓统一估值、正式 PnL 与收盘复盘一致、同日第二次执行幂等跳过；`marketgraph` 用户 cron 覆盖 48/48、无缺项或权限阻塞。

- **A股上午闭环复核（2026-07-10）**：上午 5 分钟任务稳定运行，10:54 的 `000776.SZ` server-local simulated retry 成交完成信号卡、append-only 成交、签名回执、持仓与组合复盘链路，无重复入账；该笔成交使用的 5 分钟行情相对成交时刻过期，因此保留为策略账户成交但降级为弱执行证据，不进入演化样本。修复午休 bootstrap 重放成交价覆盖主账户盯市 PnL 的问题；50,000 / 100,000 元档位现与主账户使用同一轮 SharedSignals mark prices；新 retry 成交会把 `retry_of/retry_attempt` 写入成交、签名回执与复盘行。历史签名回执保持 append-only，不原地补写。

- **策略证据门槛（2026-07-10）**：A股取消“每日必须形成一笔成交”的交易配额；无成交只形成 `observation_gap`，不会再触发 `force_sample_collection`。弱成交证据（仅 signal card 价格或缺 bar time/volume）继续保留在模拟账本，但不进入演化样本；风险扩张必须同时具备足量强成交证据、已实现回合、60 分钟前向标签和正已实现收益。六维评分若某个维度在整个候选批次均无证据，会对整批统一移除该权重并写出批次证据可用性，不能让中性 `0.5` 伪装为信息。盘前报告新增实际 `score_limit`，便于识别配置漂移。CNFutures 模拟执行默认要求至少 3 个独立底层品种；同品种跨月不足时进入 `observation_only`，不产生成交或演化样本。
- **执行与诊断证据（2026-07-10）**：A股 simulated 主循环会按每笔订单自身标的附加最新有效 SharedSignals 5 分钟 quote/bar；当日条线超过 15 分钟不得作为执行证据。仅含 `execution_evidence_class=verified_5min_market_data`、报价、bar time 与 bar volume 的成交可进入演化，价格兜底成交继续保留账本但不能学习或扩张风险。评分诊断会明确显示整批已移除的中性维度及其证据可用率。CNFutures 将连续确认不足标为 `insufficient_consecutive_5min_bars`，并按期货品种输出 hold 汇总，区分策略确认不足与真实行情缺失。
- **A股模拟资金范围（2026-07-10，2026-07-11 更新本金）**：server-local 账本将策略成交与链路验证成交标成 `capital_scope=strategy|validation`；当前两者各自按 50,000 元硬现金/持仓门禁回放，验证样本保留审计与回执但不再占用策略资金，消除资金计划与写账拒单的口径冲突。
- **A股可恢复失败重试（2026-07-10）**：同日失败卡默认仍阻断重复下单；只有 server-local 模拟账本的 `insufficient_cash` 会在资金范围修复后保留原卡并最多生成 2 次新 retry card，避免旧的可恢复拒单永久锁死候选，也避免无限重试。

- **A 股多风格模拟盘**：完整闭环运行（信号生成 → server-local paper fill → sim 账簿 → 复盘）；旧层已完全退役（0 文件、0 cron）；A股资产入口已通过 SharedSignals `/tushare?api_name=stock_basic` 恢复；2026-07-06 已修复候选池/执行门禁退化问题：auto pipeline 的 A股入口改走 `AshareAdapter` 过滤后的 universe 与真实分钟/日线价格，不再从资产表顺序取样或使用 `price=1.0`，`run_sim_loop` 的 A股 simulated 新买入只允许 candidate 层，watch/空池/candidate pool 异常均 fail-closed 为无交易，缺名称/缺日线/缺流动性证据标的不进入可执行候选；A股 simulated 订单必须写入 `candidate_pool_layer` 与 `execution_source`，`sim_broker` 与本地模拟账本双层拒绝缺来源买入/卖出，signal card 会持久化同一来源字段，复盘可直接确认买入来自 candidate 层或卖出来自 rebalance；缺少来源字段的历史 A股 simulated 成交只保留在隔离备份中作事故复盘，不进入当前模拟账户、策略有效胜率、方向命中、归因、策略 PnL 或自我进化；2026-07-07 起 A股评分覆盖扩到 500 个 universe 样本，候选池仍坚持 `combined >= 0.55` 的 candidate 门禁，若候选为 0，会在 `no_trade_explanation.score_diagnostics` 写出已评分数量、阈值、Top 分数、各维度 0.5 中性默认计数、缺失计数、全维度 0.5 样本数和样本列表，并新增 `evidence_reason_summary`、`missing_and_default_like_dimension_counts`、`evidence_coverage_distribution` 和 reason sample；2026-07-08 起 A股 candidate 额外要求证据元数据支持：最低 evidence coverage 且 event/fundamental/sentiment 至少一个研究维度有真实证据，避免纯技术/资金高分叠加缺失维度 0.5 中性默认分进入可执行候选；不满足证据门禁但分数较高的标的留在 watch。盘前 dry-run 已改为复用 `candidate_pool.build_pool`，与模拟主循环、开盘验收共用同一候选池口径；2026-07-09 起盘前 dry-run 的数据覆盖证明和流动性排序只读取 SharedSignals API 的 `/tushare?api_name=daily` 批量日线 read model，再按最新普通 A股交易日 `amount` 排序；单标的 `/market_data` 仅用于评分细节和执行门禁价格，不读取兄弟 SharedSignals SQLite，也不再把资产表前段顺序当成高流动性样本；已传入预计算 scores 的 5 分钟/盘前高频入口默认跳过低频 fundamental 全量观察池，避免候选池验收被长期基本面池拖慢；诊断会区分“分数过阈值但实际 candidate 为空”的分层/证据门禁拦截。
- **A 股模拟盘**：默认走服务器本地闭环，不依赖 Mac Mini Hermes；Hermes/同花顺 GUI 路径已降级为第二选择，只在 `ASHARE_SIM_HERMES_ENABLED=1` 时启用并投递 `signals/pending`；A股 simulated signal card 显式固定 `real_trading_enabled=false`；2026-07-05 已修复 A股 sim account 字符串阻断 server-local fill 的问题，隔离真实数据 smoke 验证 9/9 本地成交、local_sim 账本与 `signals/positions/simulated_ashare_positions.json` 持仓快照均可生成；A股 simulated capital 已显式固定为 200,000 元，`job_ashare_sim_exec` 会在开盘前/盘中首轮保证空账本快照存在，尚无成交时输出 `bootstrap_state=no_trades_yet`、现金与空持仓，避免 dashboard/验收等待第一笔成交；2026-07-06 已统一 A股 server-local 执行器和本地模拟账本默认资金为 200,000 元，本地账本会从成交回放写出 `cash_available`，资金计划优先读取账户快照现金而不是用本金倒推；`pending`/未成交回执不会写入 server-local filled 账本；2026-07-07 起 `Ashare/sim_executor.py` 自身按 A股交易日历与连续竞价时段拒绝非交易时段 server-local fill 与 Hermes pending，避免绕过 wrapper 的手工/验收调用在收盘后产生模拟成交；修复前已发生的非连续竞价 simulated 成交保留为账户事实，但复盘/看板统一归类为 `outside_ashare_regular_session` 链路验证样本，不进入策略胜率、方向命中、策略 PnL 或自我演化样本；2026-07-08 起新的 server-local 成交会在 raw response、local_sim 账本和签名回执中保留 `market_snapshot`、`fill_price_source`、`fill_price_source_class` 与 `fill_evidence`；A股样本质量门禁要求候选来源和成交价来源同时存在，缺成交价来源的历史/手工样本只作链路验证样本，不进入策略 PnL/胜率/演化；A股资金计划已从固定集中升级为动态闸门：按候选质量、风控拒绝率、数据异常率和近期表现决定 0/1/2/3 只，强信号集中，弱信号留现金/逆回购，并把 `capital_plan` 写入模拟主循环结果、portfolio 和 `shared/review/ashare/capital_plan_YYYYMMDD.jsonl`；2026-07-09 起资金计划和 no-trade 证据会写出 `existing_position_count` 与 `capacity_reason`，明确区分目标持仓已满、防御模式无目标仓位、现金不足和其他容量为零原因，避免把机会成本换仓不足误读为单纯现金不足；旧/分批持仓按唯一标的计数，模拟主循环已能生成 simulated sell 压缩单，优先处理止损、低分、机会成本和超目标持仓压缩，止损/压缩/机会成本释放资金会写入 `capital_plan.replacement_budget` 并允许同轮替换买入；新增 `shared/runtime_test/ashare_preopen_dry_run.py` 作为 08:35 盘前只读预演入口，提前验证日线覆盖、最新高流动性普通 A 股小样本的候选池、动态资金计划和执行门禁，默认样本上限 50 只且 wrapper 90 秒超时，避免 10 只小样本误判候选池，只写 runtime_test 报告，不写 `signals/`、账本、pending 或 review，异常时走系统邮件
- **TradingAgent 分析边界**：A股和 CNFutures 的短周期机会发现、候选池、交易门禁、资金计划、模拟撮合、复盘和演化由 TradingAgent 自身负责；SharedSignals API/read model 是基础数据入口，TradingAgent 应最大化消费其行情、宏观、事件、基本面、资金、情绪、行业和 5 分钟接口能力；MarketGraph 作为外部宏观研究、事件图谱和中长线研究补充，不替代 TradingAgent 的执行前判断。A股当前核心维度为六维评分（macro/event/fundamental/capital/technical/sentiment）、五层候选池、盘前集合竞价异常、尾盘动能、动态资金计划和机会成本换仓；CNFutures 当前核心维度为分钟动量、均线偏离、量能确认、开盘/跳空冷却、波动/噪声过滤、方向一致性、追高过滤、手续费/保证金和前向标签校准。外部研究缺失时必须作为证据债/中性默认/安全空跑处理，不能绕过本地候选与执行门禁。
- **MarketGraph 研究 API 接入**：2026-07-09 起 `TradingagentDataReader` 增加独立 MarketGraph 只读 HTTP client；A股六维评分基础证据按 SharedSignals-first：宏观优先 `/macro`，事件优先 `/events`，情绪优先 `/sentiment`，基本面/资金/技术分别优先 SharedSignals fundamentals/capital_flow/market_data；MarketGraph `/regime`、`/contract` 的 `association_impact_relations` 等只作为增强证据。`MARKETGRAPH_API_TOKEN` 由 TradingAgent 自己的生产 env 提供，TradingAgent cron 不 source MarketGraph deploy env，保持三系统可独立部署。若 MarketGraph regime/event/sentiment 缺失或未授权，A股记录 evidence debt 并按 SharedSignals 证据或中性/降级评分处理，不把研究缺失当成执行故障，也不绕过 candidate/资金/风控门禁。
- **数据入口收口**：Ashare/Crypto/US/HK 通用市场适配器与 `auto_pipeline` 不再直接访问 `reader.shared` 或同机 SharedSignals SQLite 兜底；生产取数只能通过 `TradingagentDataReader` 暴露的 SharedSignals API facade。API 返回空时进入空池、数据等待或策略等待，不从旧路径补数。2026-07-09 起 `/realtime_5min` 单票读取同时发送 `ts_code/symbol/date/trade_date` 参数别名，并在 TradingAgent 侧按目标 symbol/ts_code 过滤批量返回，避免 SharedSignals 某个参数别名不生效时前向验证读到空或误读其它股票。CNFutures 仍仅保留显式诊断开关下的只读 SQLite 排障路径；本地 sim ledger SQLite 属交易账本，不是市场数据源。
- **生产取数口径**：TradingAgent runtime 以 SharedSignals HTTP API 为唯一市场数据入口；本地 SQLite 只允许保存模拟交易账本、看板快照和明确诊断输出，不允许作为 SharedSignals API 失败时的市场行情兜底。
- **SharedSignals 证据 API 消费契约**：2026-07-09 新增 `shared.runtime_test.sharedsignals_evidence_contract`，只读检查 TradingAgent 依赖的 `/macro`、`/events`、`/sentiment`、`/capital_flow` 是否可通过 SharedSignals API 返回 list 结构和最小 schema；接口不可达或 schema 缺关键字段为 fail，端点可达但当前空行记录为 `evidence_debts`，默认不阻断 TA 模拟闭环，`--strict-empty` 才将空证据升为 warn。该检查已接入 `full_acceptance --profile prod` 与 quick 关键测试集，不修改 SharedSignals 采集/API 实现，避免和 SharedSignals 独立改造冲突。
- **复盘与自我演化验收**：A股 server-local 成交进入日报复盘时必须保留 `filled_price/avg_price`、`trade_timestamp_bj`、`ashare_session_valid`、`fill_price_source`、`fill_price_source_class`、`fill_evidence`、`candidate_pool_layer`、`execution_source` 等样本判断字段。组合演化只消费带市场报价、bar time、bar volume 的强成交证据；风险扩张还必须满足至少 20 个强成交样本、10 个已实现回合、20 个 60 分钟前向标签和正已实现收益。`self_evolution_health` 只验证链路，不把样本不足或浮盈当成正向演化证明。`portfolio_evolution.py` 先用同一批 SharedSignals 盯市价刷新本地模拟 PnL/持仓快照，再生成组合和资金档位实验复盘；成交事实继续 append-only。每日成交配额和 `force_sample_collection` 已彻底退役，盘中 monitor 只记录观察证据。
- **SharedSignals 源状态门禁**：2026-07-09 新增 TradingAgent 侧 `/source_status` 只读门禁与测试；交易 wrapper 在执行前检查 SharedSignals source governance，红灯/不可达 fail-closed，但按市场隔离判断。A股/CNFutures 不会因 Crypto/PM 新鲜度红灯停摆，Crypto/PM 自身红灯仍会阻断对应任务；健康检查仍展示全局 source_status 作为系统风险。
- **旧 cron 清单清理**：`shared/automation_tasks.md`、`shared/cron_inventory.csv`、`shared/cron_migration.md` 已删除；这些文件记录的是 6 月底 MarketGraph wrapper 迁移期清单，包含已退役路径。当前任务入口以 `AGENTS.md`、`STATUS.md`、`cron/`、`shared/wrappers/` 和生产 crontab 验收为准。
- **执行桥**：Mac Mini `~/.hermes/` 下 Hermes 仍保留为 GUI 执行桥，只执行和回写，不做买卖判断；当前 A 股服务器本地模拟闭环不要求 mini 在线；A股健康检查已把 Hermes 降为 `mini_hermes_optional`，默认未启用时不影响主链路健康结论
- **PM（预测市场）**：多风格 simulated 扫描每 30 分钟运行；checked-in config 使用 USDC；PM sim/style 输出写入 `shared/review/pm/style_comparison.json`；`PM/probability_model.py` 是 PM 研究概率消费/融合入口，优先读取 `TRADINGAGENT_PM_MODEL_PROBABILITY_FILE` / `PM_MODEL_PROBABILITY_FILE` 或默认 `shared/review/pm/model_probabilities.jsonl` 的研究概率；没有独立研究概率时只写入 `pm_market_consensus_baseline`，即模型概率等于市场概率、`model_confidence=0`，用于说明“暂无独立 edge”，不会制造交易。2026-07-07 起 `PM/research_probability.py` 和 `job_pm_research_probability` 通过 MarketGraph 统一 API `GET /pm/research-probabilities` / MCP `read_pm_research_probabilities` 读取 PM 独立研究概率，再与 SharedSignals `/pm_markets` 市场元数据和 `/pm_prices` 价格快照合并写入 `shared/review/pm/model_probabilities.jsonl` 与 summary；2026-07-09 起该入口改为每 30 分钟错峰运行，并在 SharedSignals PM 市场/价格采集后触发。SharedSignals 只供市场/价格数据，行内判断概率字段会被忽略。MarketGraph API 不可用、无研究概率或缺少 SharedSignals 市场价时会清空旧概率文件，避免历史 edge 残留并安全空跑；若 `/pm_markets` 市场行缺价，会读取 `/pm_prices` 最近价格补齐，但不会从 MarketGraph research row 的 `price/market_probability` 兜底。2026-07-07 生产确认 MarketGraph PM producer 正常运行但 `record_count=0`，主因是 PM 研究证据仍有样本债/方向证据不足/部分缺市场价格，TradingAgent 因此安全空跑；这不是执行器故障，也不能通过放宽阈值解决。`run_sim.py` 在无 PM 交易信号时输出结构化诊断（市场行数、可定价行数、模型概率行数、显式方向行数、策略候选数、edge 阈值和原因），用于区分 `pm_market_rows_empty`、`pm_prices_missing`、`pm_model_probability_missing` 与 `pm_model_edge_below_threshold`；`market_health` 对 PM 缺 MarketGraph 研究概率或模型 edge 不足标记为 pass/策略等待观察态，缺 SharedSignals 市场行或价格仍标记为 warn/数据等待，只有应成交却无账本才算执行故障。
- **多市场**：PM/Crypto/US/HK sim executor 和 config schema 已加真实执行拒绝；US/HK simulator 入口已拒绝真实 order/account payload，fill 结果不回显 account payload；共享安全扫描递归覆盖 `direct_execution`/`real_execution`/`live` 别名；Crypto/US/HK Phase D P0 工具已独立实现；US/HK P1 report/validation/promotion 工具已补齐；Crypto/PM P1 report/validation/promotion 工具已补齐；Crypto/US/PM/HK P2 risk/portfolio/replay 工具已本地模块级实现；Crypto/PM/US 的 JSON 驱动多风格 simulated 已扩展为绩效追踪、权重调节、paused/deprecated 状态和 variant 生成闭环；HK 工具与 styles 仅保留为预留能力，默认 fail-closed，不纳入 production sim / health / evolution；基础 `styles/*.json` 已恢复为只读配置，运行态权重/状态写入 `shared/review/<market>/style_weights.json`，自动生成风格写入 `shared/review/<market>/generated_styles/`；新增 evolution guard 防止全风格亏损、组合回撤和连续多市场亏损时继续自演化；新增 `shared/execution/auto_pipeline.py` 将 universe、研究、DecisionEngine、StyleRunner 和 daily evolution 串成 simulated 自动管线；本地 production sim 层已补齐 `sim_engine`、`risk_manager`、`sim_ledger` 并接入 auto pipeline；当前生产模拟盘范围为 A股/Crypto/PM/US/CNFutures，HK 暂不接入生产调度
- **模拟资金口径**：`shared/markets/sim_capital.py` 是默认资金事实源。US/Crypto/PM 默认使用 10,000 USD/USDT/USDC 原币本金，并按当前 FX 折回 RMB 展示；A股与 CNFutures 当前生产本金固定为 50,000 CNY，环境变量不能切回旧档位，非法显式档位回退 50,000 CNY。100,000 / 200,000 只允许通过函数显式参数用于受控离线历史分析，不进入生产 cron、当前看板或演化排名。多风格不是每个风格各给一份本金，而是在该市场总账户内按 active style weight 归一化拆分；看板不再把 US/Crypto/PM 的有效本金强行 floor 到旧人民币本金，也不再让历史账本里的旧正数本金覆盖 10,000 美元规范本金；权益快照缺失而回退 `style_performance.jsonl` 时，US/Crypto/PM 的 PnL、realized/unrealized PnL 和 max drawdown 会先折算成人民币，再与人民币本金相除，避免原币 PnL / 人民币本金或人民币 PnL / 旧本金混算。
- **维护样本隔离**：2026-07-07 修复 US/Crypto/PM 维护重跑样本被前端计入生产模拟交易量和盈亏的问题。`trade_journal.jsonl`、`daily_mark_to_market.jsonl`、`positions.json`、`style_performance.jsonl` 与 `style_comparison.json` 均支持 `exclude_from_dashboard=true` 或 `run_context/run_mode/run_source/sample_type` 包含 `maintenance/backfill/smoke/repair/bootstrap/dry-run` 的排除标记；`front/` 快照聚合会统一跳过这些样本。生产模拟样本默认继续计入；手动回补、烟测或修复重跑必须设置 `TRADINGAGENT_SIM_RUN_CONTEXT` 或 `TRADINGAGENT_SIM_EXCLUDE_FROM_DASHBOARD=1`，避免污染看板、复盘和演化输入。2026-07-08 起新增 `shared/runtime_test/quarantine_legacy_usd_capital.py` 安全维护工具，用于批量隔离 US/Crypto/PM 模拟账本中旧本金口径（capital_base > 12,000 原币、> 80,000 CNY，或 `--before <cutover_iso>` 资金口径切换前）的历史行和旧持仓状态，写入 `exclude_from_dashboard=true`、`run_context=legacy_usd_capital_quarantine` 与 `quarantine_reason`；默认 dry-run，`--apply` 时先备份 `.bak` 再原地修改，不删行，不改 A股/CNFutures/real_execution 行，已隔离行幂等跳过。2026-07-08 追加修复目录级隔离传播：若 `shared/logs/sim_ledger/<market>/<style>/positions.json` 已隔离，`SimLedger.daily_mark_to_market()` 会继承该隔离标记，前端读取同目录 `daily_mark_to_market.jsonl`、`equity_snapshots.jsonl`、`trade_journal.jsonl` 与对应 `shared/review/<market>/style_performance.jsonl` 风格行时也跳过，避免后续权益快照或风格绩效重跑把旧持仓状态的 PnL/tradeCount 重新带回看板。
- **多市场信号门禁**：2026-07-07 起 Crypto/US/PM/HK 通用 `run_sim.py` 不再把 SharedSignals 行情行直接转换成 `buy` 模拟信号；输入行显式带 `side/action/direction/signal/decision/recommendation=buy|sell`，或由市场专属策略生成 `signal_source=explicit_strategy_signal` 后，才会进入 StyleRunner。当前内置策略口径为 Crypto 动量突破、US 趋势跟随、PM 模型概率相对市场概率价差；价格行只用于估值/成交价。人工价格行烟测必须显式设置 `TRADINGAGENT_SIM_ALLOW_PRICE_ONLY_SIGNALS=1`，且这类样本自动写入 `exclude_from_dashboard=true` 与 `sample_type=price_only_smoke`，不进入生产看板/复盘/演化口径。看板信号表会展示成交账本中的 `strategy_name`、`signal_source`、`reason` 与 `conviction`，用于证明成交来自明确策略，而不是价格行样本。
- **Crypto 空跑归因**：2026-07-07 起 `run_sim.py` 在 Crypto 无交易信号时输出结构化诊断，包含检查币种数、可定价 K 线数、显式方向行数、策略候选数、单根/回看动量阈值、每个样本的 `one_bar_return` / `lookback_return` 和原因；`market_health` 会把 `crypto_momentum_threshold_not_met` 识别为 pass/策略等待观察态，把 K 线空缺识别为 warn/数据等待，不再把“策略未触发”误判为执行闭环故障。若出现策略候选但账本仍无成交，仍按 fail 处理。
- **看板交易数口径**：2026-07-07 起 `front/` 对 sim ledger replay 信号按 `market + symbol + status + stage` 去重，市场级 `tradeCount` 不再用风格层 `filled_count` 放大；多风格子账户同一标的的成交仍保留在 style comparison 的 `filledCount` 中，用户看板的市场摘要按唯一市场机会展示。`StyleRunner` 与 `SimLedger` 会把 `strategy_name`、`signal_source`、`reason`、`conviction/score` 等策略来源写入成交账本，便于复盘证明成交来自明确策略。
- **A股权益曲线口径**：2026-07-07 修复 `shared/review/equity_snapshots.py` 将 A股本地模拟 `cash_available` 误当 `capital_base` 的问题；当前 epoch 2 快照按 50,000 初始资金重放成交现金流，`total_equity = cash + market_value`，`total_pnl = realized + unrealized`，避免看板在买入后把现金余额误读为账户总权益。旧 200,000 epoch 1 曲线只保存在归档证据中。
- **实盘安全基础设施**：新增 `shared/execution/real_trading_gate.py` 与 `signals_real.py`，真实交易默认拒绝，必须显式环境开关、人工确认 token、资金上限、交易时段、T+1 与 halt 检查全部通过；sim → real promotion 只接受经 sim 审计的来源；`signals/real/*` 为隔离队列，不代表自动下单或已成交
- **模拟撮合引擎**：`shared/execution/sim_engine.py` 已进入 Phase 1，统一支持 bid/ask marketability、盘口量/5m bar volume 部分成交、A股买入整手、A股 T+1 可卖数量、A股涨跌停边界、PM 概率价格边界、现金可用性检查和轻量对手盘环境参数；A股 server-local 模拟执行与 `auto_pipeline` 本地 fallback 已接入该引擎；A股执行器会从 server-local 模拟账本补齐 `cash_available` 与 T+1 `sellable_qty`，`auto_pipeline` 会从 SharedSignals reader 的 5分钟/日线 bars 生成 `market_snapshot`；该层仍是 paper-only，不接真实券商/交易所撮合。
- **CNFutures 模拟盘**：国内期货只跑模拟盘，无单独影子盘；多风格模拟会写 `shared/review/data/cn_futures_sim_reviews.jsonl`，并同步输出 `shared/review/cn_futures/style_comparison.json`、`style_performance.jsonl`、`observation_report.json` 与盘后 `win_rate_calibration_report.{json,md}` 供现有看板/巡检接入；观察报告已提供 `dashboard` 和 `next_validation` 顶层字段，便于看板直接展示 readiness、下一交易时段验收步骤和是否继续累计样本；`signals/positions/cn_futures_sim_positions.json` 的模拟持仓快照已接入生产前端 holdings 解析；`score_summary`、`error_summary`、`style_health`、`hold_count`、`hold_reason_summary`、`forward_label_summary` 和 `dynamic_threshold_candidates` 标记样本不足、手续费、保证金占用、名义金额、可用 PnL 样本、风格状态、风控拒绝原因、主动不交易原因、前向标签和动态阈值候选；`CNFutures/live_gateway.py` 为未来 CTP/期货公司接入预留 fail-closed 占位，当前拒绝全部真实期货订单；2026-07-08 起盘前 TradingagentDataReader 日线验收改查最近 30 天窗口，避免开盘前只查当天而漏掉上一交易日收盘数据；生产 crontab 已改为期货日盘/夜盘 5 分钟级运行 `job_cn_futures_sim.sh`，并读取 SharedSignals `market_bars_intraday` 的 Futures 5 分钟数据；5 分钟 runner 已加入 10 分钟默认数据新鲜度闸门、同风格/同合约连续同方向重复暴露限制、tick/slippage 成交价、静态涨跌幅边界、bar volume 部分成交、模拟持仓快照、风格保证金 cap、不过夜强制平仓、换月保护和反向平仓 PnL 估算；2026-07-08 起换月保护按合约月开始日前后配置窗口禁止新开仓，已进入合约月后的临近交割合约也会被拦截；2026-07-08 起午休 11:30-13:00 被 5 分钟 runner 视为闭市，写正常 `market_closed` 复盘行，不再用上午最后一根 bar 触发 `stale_intraday_bar`；夜盘若全部风格因 `style_session_not_allowed` / `night_session_not_allowed` 主动 hold，首样本验收标记为 pass/观察而非系统 warn；开盘验收的 TradingagentDataReader 路径会跳过泛合约、过期合约、未上市合约和不支持产品，优先用当前可执行合约验证 SharedSignals API，避免被资产表历史合约挤成 `reader_shortfall` 后直接依赖 SQLite 汇总兜底；2026-07-07 起 5 分钟 runner 调用 `get_bars_intraday` 时显式传入交易日 start/end，并且 intraday universe 优先读取 SharedSignals 当日 `market_bars_intraday` 最新一批 5分钟 bar 中的可执行合约，拒绝 `CU.SHF` 这类泛合约、仅有历史 bar 的旧合约和早一批已滞后的合约，避免开盘后把非目标交易日分钟线、过期合约或 stale 合约误当作可交易候选；盘前验收已复用同一可执行合约过滤并报告 raw/executable symbol、产品覆盖、5分钟 read model 可达性和运行时风格状态；闭市时段的盘中 runner 会写正常 `market_closed` 复盘行，不再把收盘后的最后一根有效 5 分钟 bar 误报为 `stale_intraday_bar`，但交易时段内真正滞后的分钟线仍会 fail/warn；手续费模型已显式区分 `rate` 与 `fixed_per_lot`，不再靠费率数值大小推断；非指数基础风格默认拒绝夜盘 bar，只有显式 `night_session_allowed=true` 才允许夜盘模拟；`CN_FUTURES_SIM_DISABLED=1` 可临时暂停模拟任务但保留观察报告；`index_intraday_directional` 已加日盘-only、趋势一致、成交量确认、开盘冷却、跳空冷却、低波动、方向连续性、最新 bar 反转、信号噪声比、bar gap、K线实体质量、连续同向 bars 和 late-chase 过滤，并输出场景标签与模拟出场计划，演化器按 `win_rate_first_risk_adjusted` 目标生成小型候选族群；force-flatten 平仓现在按已有持仓成本和实际成交价写 realized PnL，`score_records` 增加 `pnl_attribution`，可区分 `no_closed_pnl`、样本不足和真实已实现收益。
- **CNFutures 夜盘边界**：2026-07-10 修复夜盘活跃交易日和品种收盘边界误报。`CNFutures/session.py` 统一输出期货活跃交易日，21:00 后默认滚到下一交易日；`run_simulation`、期货 live check 和模拟盘健康检查均使用该日期查询 SharedSignals `/realtime_5min?market=Futures`，不再混用 UTC/本地日历日。合约规则补充夜盘收盘分钟，铜等 01:00 正常收盘后的最后一根 5分钟 bar 归为 `product_night_session_closed` 的可解释 hold，不再误报 `stale_intraday_bar`；当前 review 读取也按活跃交易日过滤，历史 stale 只作复盘背景。
- **CNFutures 冷启动样本保护**：2026-07-06 修复样本不足导致全部风格被 runtime overlay 标记为 `paused/enabled=false` 后模拟器永久空跑的问题；`sample_insufficient` 现在保持 `active/observe`，继续在 simulated-only 层积累样本，不允许晋升为真实交易。真正 `blocked/deprecated/disabled` 或会话不允许的风格会写入 `hold_reason_summary`（如 `style_paused`、`style_session_not_allowed`），避免以后出现“cron 正常但无成交、无原因”的假正常状态。
- **cron 解耦入口**：Crypto/US/PM 模拟 cron 已按 30 分钟级安装并与 SharedSignals 采集错峰：PM 研究概率 `job_pm_research_probability` 在 `4,34` 刷新，PM sim 在 `7,37` 运行，Crypto sim 在 `8,38` 运行，US sim 在 `10,40` 于美股覆盖窗口运行；A股工作日交易时段 5 分钟级模拟 cron 已安装且默认服务器本地执行，A股开盘验收 cron 已安装：08:35 盘前 dry-run 预演数据→候选池→资金计划→执行门禁、08:55 盘前数据验收、09:35/13:05 数据验收、09:45/13:10 首样本告警；A股只读研究证据 cron 已加入 09:26/14:56/15:10，生成集合竞价、尾盘动能、204001 逆回购估算和风格证据面板输入，不写交易队列；CNFutures 5 分钟模拟 cron 已安装并相对 SharedSignals 采集错后 1 分钟，闭市/午休/夜盘收盘后 `market_closed` 视为正常观察状态不触发失败重试，观察报告错后 2 分钟刷新，风格演化按日盘/夜盘 30 分钟级运行，盘后胜率校准报告在 15:45 与 02:45 触发，开盘前只读验收在 08:55/12:55/20:55 触发，开盘后数据验收在 09:05/13:05/21:05 与 00:35 触发，首样本告警在 09:10/13:10/21:10 与 00:40 触发；`job_opening_acceptance.sh` 作为 A股+CNFutures+SharedSignals+模拟盘总验收入口，08:56、09:06/09:45、13:06/13:45、20:56、21:06/21:45、00:41 只读运行并输出短文本结论；HK 5 分钟模拟 cron 已按 Nicholas 最新决策停用且 wrapper 默认需要 `TRADINGAGENT_HK_SIM_ENABLED=1` 才能运行；`shared/wrappers/job_sim_market_health.sh` 每 10 分钟只读巡检 A股/Crypto/PM/US/CNFutures 模拟闭环，并写出 `shared/runtime_test/sim_market_health_latest.json` 供看板读取当前运行状态；`shared/wrappers/job_equity_snapshots.sh` 每 5 分钟追加模拟账本权益快照，供前端实时收益曲线使用，不写交易队列；`job_style_evolution` 模板每 4 小时只跑 Crypto/PM/US simulated 演化；复盘节奏按 `job_daily_brief_morning` 07:30 / `job_daily_brief_day` 11:45 / `job_daily_brief_night` 15:30 / `job_ashare_night_calibration` 22:00 四个 wrapper 分时运行；旧 `cron/daily_review.sh` 16:00 已退役避免与 15:30 收盘复盘重复；`cron/health_check.sh` 上报 SharedSignals/TradingAgent/MarketGraph 统一健康，优先使用 `sim_market_health_latest.json` 判断模拟盘整体新鲜度，并通过 HTTP health/API 探针验证三系统可读状态，不再依赖 MarketGraph 仓库内已删除的三系统合并 crontab 安装器；关键 wrapper 的 early setup stderr 已进入各自 `shared/logs/cron/*.log`，便于定位 env/source 启动失败；均带 flock 与独立日志；2026-07-08 起 cron/wrapper 只在 `.env` 可读时 source，生产密钥文件若为 root-only 权限不会再导致只读健康检查、模拟巡检、权益快照、复盘或演化入口直接退出。
- **生产运行用户**：TradingAgent cron 由 `marketgraph` 用户运行；生产运行态归 TradingAgent 自有 `runtime/`、`shared/logs/`、`signals/` 和 `shared/review/` 管理。后续生产烟测若必须用 root 发起，应立即用 `marketgraph` 身份复跑或恢复运行态目录归属，避免 cron 触发但写入失败。
- **SharedSignals API 消费**：`SharedSignalsAPIClient` 已覆盖核心数据消费端点；`TradingagentDataReader` 生产只通过 SharedSignals API 取数，API 不可用时 fail-closed。生产环境变量不能再启用 SQLite/read-model 回退；单元测试只能显式注入隔离 reader。A股、期货、Crypto、PM、US 的模拟入口不得读取兄弟系统目录；MarketGraph 研究证据通过 `MARKETGRAPH_API_URL` 获取。
- **A股盘前覆盖门禁**：盘前日线覆盖率必须达到 SharedSignals API 可见普通 A股资产的 90%，且不能落后最近已完成的 5 分钟交易日证据；覆盖不足、日期落后或 API 空数据会直接阻断新买入。盘前候选和价格检查已删除 SQLite/read-model 回退。
- **A股正式收盘复核**：17:40 首次运行 `job_ashare_formal_close_refresh`，22:40 在 SharedSignals EOD 长任务之后有界补跑；同一交易日成功后幂等跳过。任务只接受目标交易日精确日线收盘价；任一持仓缺价时保持当前 50,000 元主账本不变并记录失败，完整时用同一价格表刷新主账户、前向标签、组合演化与收盘复盘。旧资金档位账本不再是生产输入。
- **数据源边界复核**：2026-07-04 主服务器生产路径审计未发现 TradingAgent 活动代码直接调用 Tushare/Binance/Polymarket/Alpaca/Yahoo 等行情源；HTTP 调用保留在 SharedSignals API 客户端、健康检查、邮件/webhook 和研究 LLM 路径。误拷贝的 untracked `Users/` 旧目录已从服务器删除，`.gitignore` 已防止再次出现。
- **旧市场工具目录退役口径**：2026-07-05 开盘前复核已修正 `US/AGENTS.md` 与 `PM/AGENTS.md`；旧 `/opt/investment/US/tools/`、`/opt/investment/PredictionMarkets/tools/` 只保留为历史迁移线索，不是现役生产代码、采集或执行入口。
- **旧 cron 迁移快照退役**：根目录 `cron_gap.md` 已移入 `docs/archive/cron_gap_20260629.md`，只作历史参考；当前 cron 依据为 `STATUS.md`、仓库 `crontab.txt`、`shared/wrappers/` 和服务器 live crontab。
- **研究/筛选增强**：新增 `shared/screening/fundamental_analyzer.py` 和 `shared/research/multi_perspective.py`，只读消费 SharedSignals API/DB，输出基本面质量分、同业比较、red flags 和 bull/bear/macro/technical 多视角共识报告；`auto_pipeline` 消费这些研究结果生成 simulated 决策，不触碰实盘队列
- **复盘节奏**：11:45 午盘 / 15:30 收盘 / 22:00 夜间校准 / 07:30 晨报
- **复盘/报告输入**：日报、周报、归因和汇总邮件默认通过 `load_review_trades()` 读取 legacy shadow fills + `shared/logs/sim_ledger/<market>/<style>/trade_journal.jsonl` + A股 `shared/logs/local_sim/local_sim_trades.jsonl`；报告保留 `review_trade_count`、`shadow_trade_count`、`simulated_trade_count` 三个计数，避免服务器本地模拟成交被误判为无样本
- **影子盘状态闭环**：US/Crypto/PM/HK 本地 shadow runner 的 `simulated_fill.status=filled|partial` 会立即推进到 `signals/shadow/filled`；若状态机推进失败，卡片进入 `signals/shadow/failed` 并保留 `settlement_warning`，不再把已模拟成交卡片长期留在 `shadow/pending`
- **A股本地模拟回执**：`local_sim_ledger` 在写入 server-local simulated trade、positions、PnL 和 `signals/positions/simulated_ashare_positions.json` 的同时，会追加带 `receipt_sha256` 的 `signals/sim_execution_receipts.jsonl`；server-local 成交事实源固定为 `shared/logs/local_sim/local_sim_trades.jsonl`，不存在 `signals/local_sim_trades.jsonl` 活跃路径；默认持仓/PnL/快照为策略有效样本视图，链路验证样本只进入 `audit_pnl`、`audit_positions_by_account` 和样本质量统计，不消耗策略现金、目标持仓数、收益率、胜率或自我演化输入；首样本验收会按 `trade_id` / `order_id` / `idempotency_key` 核对当天本地成交与签名回执，能发现“有回执行数但某笔成交缺回执”的孤儿样本；健康检查默认读取 TradingAgent 本地回执，旧 MarketGraph 回执只在历史文件存在时作为兼容输入，并能识别“尚无首笔本地模拟成交”的 bootstrap 状态，避免把无样本误报为链路故障
- **模拟盘健康检查**：`market_health` 已区分交易时段样本缺失与闭市等待首样本；A股通过 `Ashare.t_plus_1.is_trading_day()` 判断真实交易日，法定节假日不会仅因工作日误判为应有样本；A股和 CNFutures 在周末/闭市且尚未进入应产生样本的时段时不再误报 warn，进入或经过交易时段后仍无数据/成交会继续告警；A股健康检查已补充 stale 执行卡、server-local 账本/持仓快照数量与现金一致性、最新 `capital_plan` 看到的持仓数与快照持仓数对账。
- **A股/CNFutures 开盘验收框架**：A股 `shared/runtime_test/ashare_opening_validator.py` 提供 `validate_pre_open` / `validate_opening` / `first_sample_alerts` 三个只读入口；所有市场数据检查走 `TradingagentDataReader`/SharedSignals HTTP API，日线覆盖（含 90% 门禁、日期新鲜度）和 5 分钟数据新鲜度均通过 API 检查，API 不可用、空返回、覆盖不足或过期时 fail-closed，不再用第二次相同 API 调用、泛化健康状态或旧本地快照转绿；本地样本证据（信号卡、local_sim 成交、签名回执、复盘日志、no-trade 归因）完全保留在 TradingAgent 自身数据范围内。`first_sample_alerts` 会输出 `no_trade_explanation`，并只按当天本地模拟成交计数，避免旧成交掩盖当天未交易原因。三个 A股 wrapper 已写入生产 crontab，且不再接收 SharedSignals SQLite 路径。CNFutures `opening_validator.py` 保留其原有只读诊断和 live-check 业务证据降级路径。`shared/runtime_test/opening_acceptance.py` 聚合 SharedSignals API、watchdog 输入、halt 文件、模拟盘总巡检、A股验收和 CNFutures 验收；A股午休路由到下午盘前验收，正常闭市空档标记为通过/等待下一窗口。所有开盘验收均固定 `real_trading_enabled=false`，只读执行。
- **CNFutures 验收入口兼容**：2026-07-05 复核发现 `CNFutures/opening_validator.py` 直接脚本启动会被相对导入阻断；已补兼容，`python -m CNFutures.opening_validator` 与 `python CNFutures/opening_validator.py` 均可用于只读验收。生产 wrapper 仍使用模块启动。
- **多市场绩效去重**：Crypto/PM/US/CNFutures 共用的 `style_performance.jsonl` 已从 5 分钟 append-only 改为按 `(market, style_name, date)` 幂等写入，历史读取也会取同键最新值，避免 5 分钟任务把 runs/trades/PnL 重复放大并污染风格演化。
- **多市场收益口径**：新增 `shared/review/pnl_summary.py` 统一摘要层，按 `realized_pnl + mark-to-market unrealized_pnl` 聚合 Ashare/Crypto/PM/US/CNFutures 模拟账本；Ashare 用 SharedSignals 日收盘价做 mark-to-market（缺失则回退成交价），其他市场用 `SimLedger` journal 重放盯市；PM 持仓 now 按 `market_id + outcome` 区分 YES/NO，NO 持仓按显式 `no_price` 或 `1 - yes_price` 估值，避免把 NO 成本与 YES 市价相减造成虚假高浮盈；PM 模拟成交层在 price history 缺失时会从 SharedSignals `/pm_markets` 当前行取 YES/NO 价格，不再统一按 0.5 熵值兜底成交；日报、周报、运维报告、`market_health` 和 `metrics_dashboard` 均输出 `ledger_realized_pnl` / `ledger_unrealized_pnl` / `ledger_total_pnl` / `ledger_market_value` / `ledger_open_position_count` / `ledger_missing_mark_count` / `ledger_pnl_source`；A股额外输出 `strategy_total_pnl`、`strategy_market_value`、`strategy_open_position_count` 与 `sample_quality`，用于区分真实账户账本结果和可用于策略评价的样本；Crypto/PM/US 的 `StyleRunner` 主收益口径同样基于统一模拟账本；CNFutures `sim_runner.py` 和 `review.py` 已补 unrealized 输出；HK 仍暂停，不纳入本口径。
- **A股收益自动盯市**：2026-07-08 起 `sim_ledger_pnl_summary(markets=("ashare",))` 在未显式传入 `ashare_mark_prices` 时，会先从 A股 server-local 本地模拟账本读取持仓，再通过 SharedSignals reader 自动加载最近可用日线收盘价做 mark-to-market。2026-07-09 起 `Ashare.portfolio_evolution --write` 会把该盯市结果同步刷新到 `local_sim_pnl.json` 和持仓快照，避免“组合演化 PnL”和“本地模拟盘 PnL”展示不同数字。健康检查、日报、周报、运维报告和看板摘要不再默认停留在 `ashare_local_sim_trade_price_fallback`；只有 SharedSignals 价格不可用时才保守回退成交价，并通过 `missing_mark_count` / `pnl_source` 暴露。
- **服务端**：阿里云华南3/广州 `8.138.181.177`，生产路径 `/opt/investment/tradingagent/`
- **运行监控**：每小时运维报告（`ops_report.py`），覆盖执行队列、sim 队列、回执完整性、PnL 摘要
- **邮件模板**：11 类 TradingAgent 邮件已统一为移动端 30 秒决策版，顶部决策条、交易执行边界、三张摘要卡和日报/周报 inline SVG 图表已补齐；通道映射未变
- **前端/看板入口**：唯一活跃生产前端是本仓库 `front/`，生产服务 `tradingagent-front-api.service` 指向 `/opt/investment/tradingagent/front`；快照 API 同时支持 `/healthz` 与 `/health` 运维探针。独立 `TradingAgentDashboard` 原型不再作为开发、部署或文档入口。首页以实时收益、机会管道和下一步关注为核心，避免在右栏重复展示收益/账户/风险数字；机会管道优先读取 `funnelEvents`，展示“机会进入 → 初筛 → 研究 → 风控 → 待执行 → 成交/观察/复盘/放弃”的动态流动，没有事件时才回退到信号阶段推导，避免把已成交账本回放误当成当前筛选转化率。收益页的累计收益曲线支持“今日/7日/30日/全部”切换，图表只负责走势和事件点，当前收益、目标差、回撤等权威数字由页面摘要板/实时收益卡承载，不在同一面板重复展示。收益曲线优先读取模拟账本权益快照 `shared/logs/sim_ledger/*/*/daily_mark_to_market.jsonl`，该快照由 `shared/runtime_test/write_equity_snapshots.py` 追加写入，字段包含本金、权益、已实现/未实现收益、回撤、交易数、价格缺失状态、原始币种、`fx_to_cny` 与 CNY 折算字段；前端 API 会按 5 分钟 bucket 汇总为整盘收益，最多保留 360 个点，支撑今日/7日/30日曲线查看，默认全部按 RMB 展示，避免跨 US/USDT/USDC/CNY 直接混合；持仓面板同时读取 `signals/positions/*.json`，已兼容 CNFutures `positions[]` 快照；信号表展示策略来源列，优先显示账本中的 `strategy_name` 与 `signal_source`，市场摘要读取 30 分钟内的 `shared/runtime_test/sim_market_health_latest.json`，把 Crypto/PM 的“策略等待”和执行故障分开；健康 latest 过期时回退到账本/风格证据，避免旧健康结论覆盖当前看板。市场摘要仍会读取当天 `shared/logs/ashare_no_trade_explanations.jsonl` 并展示 A股无交易原因和下一步检查方向；首页右栏新增闭环证明面板，按市场展示运行态、信号/成交/持仓计数和 A股 no-trade 结构化证据，区分“等待机会”和“需要处理”；A股个股资金流面板只展示信号自身携带的 `capital/moneyflow` 真实评分或净流入字段，没有真实字段时显示等待，不使用样例或视频数据。缺少快照时才回退到日复盘 return 字段或按日 style performance。默认本地 fallback 不再展示暂停的 HK 样例，改用 CNFutures simulated-only 样例；真实 sim ledger 默认也跳过 HK，只有显式 `TRADINGAGENT_HK_SIM_ENABLED=1` 才读取港股旧账本。
- **A股收益看板口径**：A股权益快照只接受 canonical `ashare/ashare_sim`，由 server-local `shared/logs/local_sim` 账本生成；该快照默认使用 `account_view=strategy_samples_only`，只展示策略有效样本的现金、持仓市值和 PnL；`audit_pnl` / `audit_positions_by_account` 仅用于追溯链路验证样本，不进入首页收益、交易表现或演化评分。旧 `ashare/<style>` 多风格测试账本和 200,000 元 epoch 1 不再进入 dashboard 当前汇总，避免历史样本污染当前 50,000 元模拟盘口径。
- **A股复盘样本口径**：A股日报/周报/归因默认只读取 server-local `shared/logs/local_sim/local_sim_trades.jsonl`；旧 `shared/logs/sim_ledger/ashare/<style>/trade_journal.jsonl` 风格账本视为退役历史样本，不再进入默认复盘输入。复盘 normalizer 不得丢弃样本质量字段后重新分类；有策略样本但 `shared/review/ashare/evolution_log.jsonl` 或风格绩效仍显示 `trades=0` 时，必须视为复盘/演化断链风险。
- **A股开盘验收与无交易分层**：`ashare_opening_validator` 的 first-sample 报告已把 5分钟 bar、信号状态、服务器本地模拟成交、签名回执、成交-回执配对审计、复盘行数和 no-trade 分类汇总到同一报告；若当天没有交易，会优先读取最新 `shared/logs/ashare_no_trade_explanations.jsonl`，把无候选、无信号卡、风控全拒、资金/组合构建阻塞、重复幂等、执行跳过、执行失败、回执缺失和复盘待生成区分开。缺回执只在当天已经出现服务器本地模拟成交后才告警，避免把“尚无成交”误报成“成交后缺回执”。`opening_acceptance.py` 短文本同步展示 bar/信号/成交/回执/复盘计数。2026-07-08 起 no-trade 证据门扩展到空候选场景：当日 `orders=0` 的日志必须有 `candidate_decision_trace` 字段（空候选时允许空列表）、`capital_plan_decision` 和 `portfolio_decision`；健康检查和无交易汇总缺任一项都判 incomplete/warn。
- **A股科学空跑分类**：2026-07-08 起，A股首样本验收与模拟盘总巡检会把带有最新 no-trade 日志解释的 `no_portfolio_orders`、风控全拒、重复幂等、无候选/无信号等科学空跑归为 pass/策略等待观察态，并保留 info 级原因；没有 no-trade 日志解释、数据缺失、执行失败或已成交后缺回执仍保持 warn/fail，避免告警噪音掩盖真实故障。
- **A股 no-trade 逐候选归因**：2026-07-08 起，A股 simulated `run_sim_loop` 在无成交或无订单时不只写 counts，还会在 `no_trade_explanation` 与返回值中附带 `candidate_layer_breakdown`、`candidate_decision_trace`、`capital_plan_decision` 和 `portfolio_decision`。因此 `3213 universe / 3 candidates / 0 orders` 这类状态必须能进一步解释为价格缺失、风控拒绝、目标持仓已满、现金不足、组合构建为空、整手/预算为 0、重复幂等或执行跳过等具体门禁；空候选也必须留下空 `candidate_decision_trace`、资金计划决策和组合决策，不能只写 `no_candidates` 分类。2026-07-09 起 `capital_plan_decision.capacity_reason` 会把 `target_positions_reached`、`defensive_no_target_positions`、`insufficient_investable_cash` 等原因拆开，逐候选 `drop_reason` 复用同一细分口径。`score_diagnostics` 对 A股不再只在 candidate 为 0 时输出，候选存在但未成单时也保留评分/证据分布。A股健康检查和开盘首样本验收会把当日 `orders == 0` 但缺 `candidate_decision_trace` / `capital_plan_decision` / `portfolio_decision` 的 no-trade 日志判为 evidence incomplete，不再作为 pass/策略等待放行；健康检查同时按当天 `today_trade_rows` 判断是否已有本日样本，历史成交不能再掩盖今天无成交/无证据的问题。
- **盘前验收修复**：2026-07-07 已修复 `job_opening_acceptance` 生产 cron 依赖当前工作目录导致寻找 `/home/marketgraph/shared/...` 失败的问题，wrapper 会强制切到 `TRADINGAGENT_ROOT` 并使用绝对脚本路径；统一开盘验收会写 `shared/runtime_test/opening_acceptance_latest.json` 与 history，并在 warn/fail 时走系统通道邮件，cron 使用 `--exit-zero` 避免同一异常重试三次刷屏。SharedSignals 盘前核心 API 探针改用轻量 `/cache/status` + `/capabilities`，`/health` 超时只作为降级项；A股盘前日线检查新增 `latest_daily_age_days`，日线过旧会提前 warn，不再只因历史日线数量足够而误判通过。
- **A股旧测试账本归档**：新增 `shared/runtime_test/archive_ashare_legacy_ledgers.py`，只归档 `shared/logs/sim_ledger/ashare/*` 中非 canonical `ashare_sim` 的旧风格账本，默认 dry run，`--apply` 时移动到 `shared/logs/archive/ashare_legacy_style_ledgers/<batch>` 并写 manifest；不得删除或归档 `ashare_sim`。旧样本不再作为活跃输入，确认归档 manifest 后可按批次永久删除归档副本。
- **A股只读研究证据**：`Ashare/research_evidence.py` 与 `job_ashare_research_evidence.sh` 统一输出 opening auction 异常、closing momentum 候选、204001 逆回购预估收益和 A股风格证据；集合竞价缺少 09:15-09:25 数据时会显式标记 `first_5m_proxy`，标的选择会优先读取 SharedSignals 当日 `rt_min/stk_mins` 已有分钟线的样本，再回退资产表，避免把“扫错无分钟线样本”误判为全市场无数据；204001 优先读取 SharedSignals `/market_data` 日线收益率，尾盘候选带 `next_trading_day` 与 open/high/close 兑现标签（数据未到时 `pending_next_day_bar`），风格资金按 `shared/review/ashare/style_weights.json` 运行时 active 权重切分当前 50,000 元虚拟预算；结果写入 `shared/review/ashare/research_evidence_latest.json` 与 append-only `research_evidence.jsonl`，固定 `read_only=true`、`real_trading_enabled=false`，不进入 simulated/real 执行队列。
- **A股六维评分供数**：2026-07-07 生产隔离验收发现 A股 500 个评分样本六维全部为 0.5，直接原因是 SharedSignals P0 日线历史仅 7 个交易日、`/fundamentals`/`/capital_flow`/`/macro` 当前无可用行，且 TradingAgent 旧 `get_factors()` 没有对 `ashare → Ashare` 做 canonical 查询。已修复 `get_factors()` 的 A股市场名/代码双格式兜底，并让 `six_dimension_scorer` 消费 SharedSignals `/macro`、`/fundamentals`、`/capital_flow`、`/market_data`、`/events`、`/sentiment` read model 行；MarketGraph regime/event graph 只作增强，不再是宏观维度唯一来源。技术维度仍要求足够日线历史，不用 7 根 K 线硬凑信号。SharedSignals 已把 P0 `daily/stk_factor/stk_factor_pro` 评分相关接口窗口提高到 90 天，并把 `moneyflow` 调整为 P1 盘后全市场日频采集。TradingAgent 资金维度只使用资金金额字段（如 `net_mf_amount`），不再把 `net_mf_vol` 这类成交量混入金额分；候选池和轮动条件已统一识别 `api_name:metric` 因子名前缀。需等待生产 moneyflow 回补后复验 `score_diagnostics.neutral_default_like_dimension_counts` 不再全等于 `scored_count`。

## 二、已知问题

- HK 按 Nicholas 最新决策暂不接入生产模拟盘；`hk_basic` 正常但 `hk_daily` 当前不作为生产模拟输入。HK 代码、wrapper 和数据诊断保留，默认不跑 cron、不纳入多市场健康/evolution 结论；手动运行也需要显式 `TRADINGAGENT_HK_SIM_ENABLED=1`，HSI 代理回退需要额外 `SIM_HK_PROXY_ENABLED=1`。
- A股 2026-07-06 真实市场开盘时段的模拟盘恢复验收已通过：SharedSignals 当日 5 分钟线已落入 read model；TradingAgent 修复了 `5m/5min`、日期格式、A股日线回看、盘中价格取值、买入整手和健康检查误报后，服务器本地模拟盘已产生 server-local filled、签名回执、持仓快照和成交回执邮件；后续已修正候选质量缺陷：先对 20 个候选打分，再按 combined score 排序进入 A股动态资金计划，`max_portfolio_positions=3` 作为上限而非硬买目标；同轮被价格、风控或执行跳过的候选会写 `shared/review/ashare/execution_exclusions_YYYYMMDD.jsonl` 并进入日报 `execution_quality`；同轮资金计划会写 `shared/review/ashare/capital_plan_YYYYMMDD.jsonl`，超目标旧持仓、止损持仓和轻量机会成本持仓会进入 `rebalance.sells` 并按 simulated sell 路径执行，计划卖出释放的资金会进入 `replacement_budget` 以避免满仓止损后只卖不换。
- A股本地模拟回执链路已具备签名回执文件；生产环境仍需等待下一次真实市场开盘时段产生 cron 模拟盘生产样本，用于验证 cron 样本写入和收益复盘质量。健康检查已能区分“无首笔成交样本”和“有失败/有成交但缺回执”，后者才会告警。
- Hermes/Mini GUI 路径已按 Nicholas 最新要求搁置为第二选择；只有未来显式启用 `ASHARE_SIM_HERMES_ENABLED=1` 时才需要重新验证 mini health、同花顺按钮识别、截图回执和账户同步。
- 多市场旧系统 symlink 依赖已全部清除（61 个死 symlink）；工具独立实现已完成，剩余风险在 A股下一个交易日生产样本与晋降级/guard 的持续运行验证
- 集合竞价已进入只读研究证据层；SharedSignals 若没有 09:15-09:25 竞价 bars，会输出 `no_auction_data` 而不是伪造信号。该层不接模拟/实盘执行。
- A 股实盘路径仍是人工；当前只补齐本地 fail-closed 安全门和 `signals/real/*` 隔离队列，未部署为自动下单路径
- 尾盘动能风格 `Ashare/styles/closing_momentum.json` 已作为 research style 预留并保持 `paused`；只读研究证据已可输出 14:40-14:56 候选、次交易日 open/high/close 兑现标签和 204001 现金管理估算。激活前仍需要累计足够样本并通过阈值复核。
- A股研究证据的当日分钟线依赖 SharedSignals P0 `rt_min/stk_mins` 入库；若开盘后仍无当日 5 分钟数据，问题应归因到 SharedSignals 采集/bridge/cron，而不是 TradingAgent 执行队列。
- PM 当前安全空跑的生产阻塞在 MarketGraph 研究概率样本债和可匹配市场价格/方向证据不足；TradingAgent 已按 MarketGraph API 消费，不应从 SharedSignals PM 行内字段生成判断概率，也不应放宽 PM edge 阈值来制造交易。

## 三、下一步

1. [x] **P2：Crypto/US/PM/HK 多市场工具独立实现** — Crypto risk/portfolio/replay、US portfolio/replay、PM risk、HK portfolio 已补齐；HK 工具保留但暂不接入生产模拟调度
2. [ ] **P2：多市场模拟盘生产闭环** — 服务器侧 A股/Crypto/PM/US simulated cron、SharedSignals reader/API-first、统一账本、日报/周报复盘读取和健康检查已完成首轮验证；剩余为 A股下一个交易日生产样本、promotion/权重演化/guard halt-thaw 的持续运行验证
3. [ ] **P2：A 股实盘路径设计** — 需先确认安全边界和人工确认环节
4. [x] **P2：SharedSignals HTTP API 消费迁移** — 15/15 端点客户端已完成；`TradingagentDataReader` 已对 `get_market_data` / `get_events` / `is_trading_day` / `get_bars_intraday` 接入 API-first 访问；SQLite 只读路径仅保留为显式测试/应急诊断
5. [ ] **A股/CNFutures 下一个真实市场时段开盘验收** — A股新增 `shared/runtime_test/ashare_opening_validator.py` 与三个 wrapper（pre_open / opening / first_sample_alert），并已写入生产 crontab；只读验证 SharedSignals 日线/5分钟数据、本地模拟成交、签名回执、复盘日志和 filled signals，异常才发系统告警；CNFutures `opening_validator.py` 已增强 filled/receipt/review 样本告警；聚合验收已修复午休/闭市窗口误报，等待下一市场窗口生产样本验证。

### 2026-07-05 opening validation residual fixes

- [x] TradingAgent front snapshot API 同时支持 `/healthz` 与 `/health`，方便 systemd、反代或外部监控统一探针。
- [x] TradingAgent front 默认 fallback 数据移除暂停 HK 信号、持仓和分配样例，改用 CNFutures simulated-only 样例，避免真实 snapshot 缺失时页面误展示港股生产机会。
- [x] A股 `first_sample_alerts` 新增 `no_trade_explanation`，把没有交易拆成数据读取失败、未到首样本窗口、无 5 分钟数据、覆盖不足、无信号/全被风控拒绝、执行缺失、回执缺失、复盘待生成和闭环 ready；本地模拟成交只按当天日期计数。A股 simulated orchestrator 每轮也会返回并在无成交时追加 `shared/logs/ashare_no_trade_explanations.jsonl`，记录候选数、订单数、风控拒绝、价格/手数跳过、重复信号、执行失败/挂起等漏斗原因，方便盘后复盘。
- [x] CNFutures `first_sample_alerts` 新增 `opening_30m_review`，开盘 30 分钟前标记为累计样本，30 分钟后若仍无 5 分钟数据、模拟成交或回执会给出标准原因和下一步。
- [x] CNFutures 日盘信号时间桶和开盘冷却从真实 09:00 起算，09:00-09:30 标记为 `day_open_first_30m`，避免遗漏期货开盘前 30 分钟行为。
- [x] A股模拟健康检查接真实交易日历，法定节假日不会仅因周一至周五而预期产生当天样本。
- [x] CNFutures 同日反向成交 realized PnL 不再重复扣两次 round-trip 手续费；上一笔成交已预扣开平仓估算费时，反向平仓只使用这一笔 round-trip fee，避免演化/复盘样本过度悲观。
- [x] A股 manifest 删除不存在的旧 `a_share_no_trade_attribution.py` 入口；无交易归因以后从开盘首样本验收报告读取，不再维护第二套入口。

### 2026-07-05 CNFutures observation, win-rate filters, and faster simulation evolution

- [x] `CNFutures/observation_report.py` 新增只读 5 分钟交易观察报告，汇总数据新鲜度、最新模拟样本、风格排名、运行态权重、生成变体和告警，可供内置 `front/` 后续接入。
- [x] `CNFutures/opening_validator.py` 新增只读开盘验收入口，验证当前日盘/夜盘开盘后 Futures 5 分钟 bar 是否落地以及合约覆盖数。
- [x] `shared/runtime_test/cn_futures_live_check.py` 新增 `observation_phase` 与 `alerts`，把“等待 5 分钟数据 / 等待模拟样本 / 等待风格复盘 / ready / blocked”直接暴露给看板和运维。
- [x] `shared/runtime_test/cn_futures_live_check.py` 新增 `next_validation`，把下一个交易时段要验收的 fresh 5分钟数据、模拟复盘样本、风格输出和实盘关闭状态显式化。
- [x] `CNFutures/observation_report.py` 新增看板稳定字段：`schema_version`、`dashboard` 和 `next_validation`；UI 可直接读 readiness、top style、filled count、latest bar 和下一步。
- [x] CNFutures 复盘和 run summary 会输出 `cadence`、`latest_bar_time` 和 `real_trading_enabled=false`；live check 对 5分钟成交缺少 bar_time 或误带实盘标记的样本会告警。
- [x] `CNFutures/opening_validator.py` 输出已明确 `data_source="SharedSignals read_model"` 和 `read_only=true`，避免被误解为 TradingAgent 自采集。
- [x] `CNFutures/opening_validator.py` 新增 `--pre-open` 与 `--first-sample` 两个只读模式，分别用于开盘前 SharedSignals 日线可用性验收和开盘后首个 5 分钟样本/模拟样本告警；新增 wrapper `job_cn_futures_pre_open_validation.sh` 与 `job_cn_futures_first_sample_alert.sh`。
- [x] `CNFutures/review.py`、`sim_runner.py`、`run_simulation.py` 和 `observation_report.py` 已记录并暴露 `hold_count`、`hold_reason_summary` 与看板 `top_hold_reason`，用于区分策略主动不交易与真实数据/执行缺口。
- [x] `index_intraday_directional` 已新增模拟复盘字段：确认信号输出 `scenario_tags` 与 `exit_plan`；runner 对每笔记录写入 `forward_outcome`、`scenario_tags` 和 `exit_plan`；review 输出 `forward_label_summary` 与 `dynamic_threshold_candidates`；observation report 暴露 forward labeled/pending 计数和动态阈值候选数量。
- [x] `CNFutures/calibration.py` 新增盘后轻量校准：读取 CNFutures simulated filled/partial signal cards 和 SharedSignals 5分钟 bars，写 `forward_labels.jsonl` 与 `win_rate_calibration_report.{json,md}`；新增 wrapper `job_cn_futures_calibration_report.sh` 与日盘/夜盘盘后 cron。
- [x] `index_intraday_directional` 胜率质量过滤已增强：默认要求日盘-only、不过夜、动量与均线方向一致、`min_volume_ratio=1.05` 成交量确认，并新增开盘 15 分钟冷却、1% 以上跳空 30 分钟冷却、低于 0.1% 近期波动过滤、方向连续性过滤、最新 bar 反转过滤、信号噪声比过滤、bar gap 过滤、K线实体质量过滤、连续同向 bars 过滤和 late-chase 过滤；弱确认信号转为 `hold`；非指数基础风格默认拒绝夜盘 bar，避免夜盘未充分验证时误开仓。
- [x] `CNFutures/evolution.py` 从单一变体生成升级为小型参数族群，优秀风格可按 `precision/fast/smooth` 并行生成最多 3 个 simulated-only 候选，目标为 `win_rate_first_risk_adjusted`。
- [x] `CN_FUTURES_SIM_DISABLED=1` 可暂停期货模拟 runner，并输出 `state=paused`、`real_trading_enabled=false` 的 JSON 摘要；cron 模板已补期货演化和开盘验收固定入口。

### 2026-07-05 Crypto/PM/US simulated performance dedupe

- [x] `shared/markets/performance_tracker.py` 的 `save_run()` 改为同一市场/风格/日期幂等写入，`load_history()` 与 `compare_styles()` 读取时也会自动折叠旧重复行，防止 5 分钟任务把当日重复样本累计成虚假的 runs/trades/PnL。
- [x] 生产侧已备份并压缩 `shared/review/{crypto,pm,us}/style_performance.jsonl` 历史重复数据；压缩后 crypto=13、pm=11、us=6 条唯一市场/风格/日期记录。
- [x] 回归测试覆盖同日同风格重复写入、旧 JSONL 压缩和 evolution/runtime style 兼容；当前 Crypto/PM/US 模拟链路 health 为 pass。
- [x] `shared/accounting/sim_ledger.py` 新增 `total_pnl()`，按成交账本的 FIFO realized PnL 和最新信号价格对持仓 mark-to-market；缺价格时按成本价保守盯市并输出 `missing_mark_count`。
- [x] `shared/markets/style_runner.py` 不再用 `_estimate_pnl()` 作为 Crypto/PM/US 主收益口径；风格层 `pnl` 改为 `realized_pnl + unrealized_pnl`，并保留 cash/market_value/equity 供看板和 evolution 使用。
- [x] 回归测试覆盖买入后持仓浮盈、卖出后已实现收益 + 剩余持仓浮盈、缺失盯市价格按成本价保守处理。
- [x] win_rate/max_dd/sharpe 已改为优先基于模拟账本的已实现收益和持仓盯市样本计算；没有账本样本时才回退单次成交 PnL。
- [x] `job_cn_futures_observation_report.sh` 已接入日盘/夜盘固定 cron，模拟任务后刷新只读 `shared/review/cn_futures/observation_report.json`；`job_cn_futures_opening_validation.sh` 已加入开盘验收 cron。

### 2026-07-05 CNFutures execution realism

- [x] `CNFutures/sim_executor.py` 不再理想化按信号价全额成交：模拟成交价会按 tick size 取整、加入 slippage bps，并按静态涨跌幅边界拒绝离谱价格。
- [x] 模拟成交已使用 5 分钟 bar volume 和 `volume_participation` 限制可成交手数，超出部分返回 `partial`，并落到 `signals/partial` 状态目录。
- [x] 保证金、名义金额和手续费改为基于实际模拟成交价与实际成交手数计算。
- [x] `CNFutures/sim_runner.py` 在同风格同合约反向成交时估算 round-trip realized PnL，供复盘胜率和演化样本使用；当前仍是同日反向信号估算，不等同真实 CTP 持仓回报。
- [x] 新增 `signals/positions/cn_futures_sim_positions.json` 模拟持仓快照；新开仓会检查同风格现有保证金占用，超过 `max_margin_usage` 时拒绝。
- [x] `no_overnight` 风格在日盘收盘前窗口生成 simulated flatten 单；`rollover_min_days_to_contract_month_start` 可阻止临近/进入合约月的新开仓。
- [x] 若 5 分钟 bar/order 带一级 bid/ask 与可用量，模拟成交会用 buy→ask、sell→bid，并用对应盘口量限制成交；显式 `last_trade_date` / `expiry_date` 会触发到期保护。
- [x] SharedSignals 5 分钟期货字段已在 TradingAgent reader、CNFutures order 和 simulated receipt 中完整留痕：`bid_price/ask_price/bid_size/ask_size/last_trade_date/expiry_date`；成功成交与 partial 回执均保留字段，便于复盘和看板解释成交价、成交量限制与合约到期保护。

## 四、活跃任务

（当前无活跃迁移任务）

## 五、最近完成

### 2026-07-10 A股样本学习、交易假设与因子研究闭环

- [x] 新增 `Ashare/sample_learning.py` 收盘学习报告：读取策略成交、forward validation、样本目标监控、no-trade 解释和三账户实验，输出样本质量分层、交易假设汇总、收盘 blocker 归因、动态探索仓建议、三账户目标拆分和因子研究状态。
- [x] A股 simulated 订单新增 `hypothesis_id` 与 `research_hypothesis`，记录本次交易验证的假设、因子快照、样本意图和失败条件；本地模拟账本会保留这些字段，旧账本无需迁移。
- [x] 样本收集探索仓从固定 20,000-35,000 升级为候选质量动态预算：候选越接近 0.75，建议越靠近 35,000；低质量但过门槛候选靠近 20,000；弱候选、高风险和数据异常仍防守。
- [x] 因子研究当前定位为“已消费因子，未证明因子”：`sample_learning.factor_research` 只有在因子快照和 forward return 样本数达到阈值后才可标为 ready；样本不足时保持 `sample_debt`，不得把六维评分直接当作已验证 alpha。
- [x] 新增 `shared/wrappers/job_ashare_sample_learning.sh` 与 15:40 cron 模板；`cron_coverage` 已纳入新任务和 `sample_learning_latest/log` 权限候选。

### 2026-07-10 A股盘中观察证据监控

- [x] `Ashare/sample_target_monitor.py` 按当日 `portfolio_evolution_latest.json`、`evolution_decision_latest.json` 和 no-trade 解释记录成交/候选证据；无成交为 `observation_gap`，不告警为交易失败、不触发交易配额。
- [x] 监控只写 `shared/review/ashare/sample_target_monitor_latest.json` 与 append-only log；不刷新交易决策、不写订单、不写 pending、不绕过 candidate/价格/风控/现金/整手/T+1/交易时段门禁，保持 simulated-only。
- [x] 新增 `shared/wrappers/job_ashare_sample_target_monitor.sh` 与 09:45、11:45、14:30、15:30 cron 模板；`cron_coverage` 已纳入新任务和新 review 输出权限候选。
- [x] 覆盖测试 `tests/test_ashare_sample_target_monitor.py` 和 cron 覆盖测试，验证目标达成、盘中欠样本、收盘仍欠样本三种状态。

### 2026-07-09 后端机会漏斗事件写入器

- [x] 新增 `shared/review/opportunity_funnel.py`，统一规范机会事件 JSONL：`发现 → 研判 → 风控 → 待确认 → 结果`，状态统一为 `进入/通过/等待/成交/机会/拦截/复盘`，默认写入前端只读路径 `shared/review/opportunities/funnel_events.jsonl`。
- [x] 新增 `shared/runtime_test/sync_opportunity_funnel_events.py`，可从 `signals/{pending,claimed,running,filled,partial,failed,expired,cancelled}` 信号状态目录同步机会事件，支持 dry-run，`--apply` 写入时用稳定 `event_id` 去重；该工具只生成复盘/看板事件，不移动信号、不触发成交、不修改队列状态。
- [x] 覆盖测试 `tests/test_opportunity_funnel_events.py` 与 `tests/test_sync_opportunity_funnel_events.py`，验证阶段/状态规范化、前端读取路径、坏 JSONL 容错、从信号卡生成阶段路径，以及重复运行幂等。
- [x] 新增 `shared/wrappers/job_opportunity_funnel_sync.sh` 与 5 分钟 cron 模板，把信号状态目录同步为机会漏斗看板事件；该入口只写 `shared/review/opportunities/funnel_events.jsonl`，不移动信号、不触发交易、不改资金账本，并已纳入快速验收。

### 2026-07-09 A股成交后资金刷新 + 前向验证；CNFutures 5分钟 replay

- [x] A股模拟主循环在出现 server-local filled 后追加 `post_execution` 资金计划刷新行，重新读取策略有效账户视图并写入 `shared/review/ashare/capital_plan_YYYYMMDD.jsonl`；该行只记录成交后的现金/持仓状态，不生成新买入，不改变同轮订单。
- [x] 新增 `Ashare/forward_validation.py` 只读前向验证入口，对策略有效成交打 30/60 分钟、当日收盘、次交易日 open/high/close 标签；链路验证、盘外或缺来源样本只标记 skipped，不进入胜率、PnL 或自我演化。
- [x] 新增 `CNFutures/session.py` 统一日盘、午休、夜盘和凌晨夜盘时段判断；午休 `11:30-13:00` 是正常观察态，不应被健康检查或开盘验收误判为数据故障。
- [x] 新增 `CNFutures/replay.py` 只读历史 5分钟 replay：从 SharedSignals API/read model 读取 Futures 5分钟 bars，对现有风格逐窗口回放，统计 buy/sell/hold 与原因，不写订单、不写持仓、不接实盘。replay 已复用 live 风格产品过滤，并为历史触发样本标注 `execution_eligible`、保证金门禁、午休/闭市边界和不可执行原因，避免把历史 buy/sell 误读为当前可成交信号。生产验证 20260709 可回放 20 个合约、4 个风格、834 个过滤后窗口。
- [x] `shared/runtime_test/opening_acceptance.py` 聚合验收按市场保留不同边界：A股只接受 SharedSignals API 日线/5分钟业务证据，API 不可用、空返回、覆盖不足或过期时直接 fail-closed，不再读取 SQLite 或通过二次调用转绿；CNFutures 旧日线诊断失败时仍可读取 `cn_futures_live_check` 的 5 分钟数据、复盘和 hold 摘要，只有该 runtime evidence 通过才可降级为 pass。
- [x] 看板只读快照已接入 A股 `forward_validation_latest.json` 与 CNFutures `replay_latest.json`：A股面板展示成交验证/待确认，期货市场摘要展示 replay 候选、可执行数量、主原因和合约/风格覆盖。
- [x] 生产 crontab 固定任务已收口到 `marketgraph` 用户合并表：A股前向验证与 CNFutures replay 必须出现在 `sudo -u marketgraph crontab -l`，root 用户不得保留 TradingAgent 残留条目。新增 `shared.runtime_test.cron_coverage` 只读守卫，检查 `crontab.txt` 与 `shared/crontab.txt` 是否一致、生产用户是否覆盖完整模板、root 是否残留 TradingAgent 任务；2026-07-09 起同步检查 `shared/review/ashare/forward_validation*`、`portfolio_evolution*`、`tier_experiments_latest.json`、`shared/logs/local_sim_tiers/`、`shared/logs/trade_audit_trail.jsonl` 等 A股复盘/资金档位/审计输出是否被 root 权限残留阻塞；非 root 运行时不再把当前用户 crontab 误判为 root 表，root 残留需用 root 身份单独验收；`full_acceptance --profile prod` 已接入该守卫，避免以后单仓模板或误装 root 表导致“测试通过但生产调度漏项/复盘写入失败”。
- [x] CNFutures 运维复盘摘要已拆分 `current` 与 `historical`：当前健康只看最新 actionable review，历史累计 `missing_intraday_bars` / `stale_intraday_bar` / 旧风控分类保留为复盘背景，不再与当前 live health 并列展示成今天故障。
- [x] CNFutures 首样本验收改为生产 API-first：优先读取 SharedSignals `/realtime_5min?market=Futures`，只有显式测试/诊断 SQLite 才走本地 read model；有数据且策略主动 hold 归为正常观察，不再要求为了通过验收而产生模拟成交。
- [x] A股开盘验收聚合层已识别“SQLite 诊断未启用但 runtime evidence 通过”的纯旧诊断告警：仅该告警存在且盘前 dry-run 显示数据、候选池、资金计划和执行门禁可用时才降为 pass；若 runtime evidence 不通过，或同时存在真实样本/执行/回执告警，仍保持 warn/fail。
- [x] TradingAgent front 快照已修复 CNFutures 当前状态来源：最新 review 行优先，`style_comparison.json` 不再与最新 review 计数相加，避免 filled/error/hold 被翻倍或被旧运行污染。
- [x] CNFutures 模拟盘 cron 日志事实源统一为 `shared/logs/cron/job_cn_futures_sim.log`；`market_health` 与 cron 模板已改用该标准 wrapper 日志，并兼容期货日志中的 `state=ok/market_closed` 状态字段；旧 `cn_futures_sim.log` 仅保留为历史兼容读取，避免健康检查展示过期日志年龄或 `latest_cron_status=None` 误报。
- [x] 2026-07-09 追加收口 cron 日志事实源与权限守卫：`_common` wrapper 的 crontab 外层重定向统一到 `job_*.log`，`sim_market_health`/`equity_snapshots` 使用各自真实日志，A股健康检查读取 `job_ashare_sim_exec.log`；`cron_coverage` 同步检查 runtime lock、机会漏斗 review 和活跃 cron log 是否被 root 权限残留阻塞，避免 root 手工烟测后 `marketgraph` cron 无法写入。

### 2026-07-08 A股策略资金视图隔离验证样本

- [x] `AshareAdapter.get_sim_account()` 从 server-local 模拟账本额外生成 `strategy_positions`、`strategy_cash_available` 和 `strategy_sample_quality`，只统计候选来源、成交价来源和交易时段都合格的策略有效样本。
- [x] `shared/orchestrator.py` 的 A股资金计划、机会成本换仓、买入容量和 portfolio existing positions 改用策略有效样本视图；账户事实持仓仍保留在 `account_positions` 与快照中，便于看板/复盘追溯链路验证样本。
- [x] `shared/runtime_test/ashare_preopen_dry_run.py` 同步使用策略资金视图，并输出 data/candidate_pool/capital_plan/execution_gate/total 各段耗时，避免开盘前检查慢时无法定位瓶颈。
- [x] 2026-07-09 追加修复 A股策略资金视图与账本最终门禁：常规交易时段、候选层、server-local 策略成交即使价格来源为 `signal_card_price` 也会占用策略资金；完全缺价格来源或盘外样本仍隔离为链路验证。`local_sim_ledger` 写入成交前会按本地账本实时回放现金，现金不足返回 `insufficient_cash`，`sim_broker` 会把这类结果上抛为 rejected，防止过期账户快照或同轮多单把 200,000 元模拟本金打成负现金。A股信号卡 T+1 可卖日期同步改为下一交易日。
- [x] 2026-07-09 追加修复开盘验证器测试诊断边界：生产默认 SharedSignals SQLite 诊断仍需显式环境开关；测试或人工显式传入的非默认 sqlite_db 允许只读诊断，避免 A股/CNFutures 开盘验收在全量测试中误报 `sqlite_diagnostic_disabled`。`daily_review` 保留 `SharedSignalsReader` 兼容别名，默认仍走 `TradingagentDataReader`。
- [x] 2026-07-09 追加修复 CNFutures 风控分类：`margin_cap_exceeded` 现在作为 `hold_reason_summary` 中的正常风险拒单/空跑归因，不再进入 `errors` 导致 5 分钟模拟任务 `degraded`。CLI 摘要同步输出 `hold_reason_summary` 和真实 `error_sample`，便于区分“有候选但保证金门禁拒绝”和“系统/数据异常”。
- [x] 2026-07-09 追加修复 A股 server-local 账本最终视图：`local_sim_ledger` 默认写出策略有效账户视图，链路验证样本隔离到 audit 视图；A股策略口径当前为 2 笔有效成交、2 个持仓，样本质量、持仓快照、资金计划和失败/回执健康检查均通过。
- [x] 2026-07-09 追加修复 A股六维评分证据消费：`six_dimension_scorer` 能把 SharedSignals `/macro` 的 Tushare PMI `factor_name/value` 原始行转成市场级 macro 分数；`/sentiment` 中未带个股代码的市场新闻只作为弱市场 sentiment，且必须有明确利好/利空关键词或方向字段才计入；个股 event 仍要求 SharedSignals/MG 给出个股或图谱关联，避免把全市场新闻伪造成个股催化。
- [x] 2026-07-09 追加修复 A股 MarketGraph 事件增强噪声：未配置 `MARKETGRAPH_API_TOKEN` 时不再请求受保护 `/contract` 合约表，避免 401 干扰 A股评分诊断；SharedSignals events 仍优先，MarketGraph 只在授权可用时作为增强。
- [x] 2026-07-09 追加优化 A股动态资金计划：`sample_collection` 只在累计策略有效样本低于最小样本数、候选质量达标且风控/数据无异常时开放 1 笔 20,000-35,000 元受控探索仓；当天无成交不增加买入容量。弱候选、高风险或数据异常仍防守/谨慎；最终订单仍须通过 candidate、资金、T+1、时段和账本门禁。
- [x] 2026-07-10 追加 A股自动进化控制器：`Ashare/evolution_controller.py` 基于三账户 ranking、强成交证据、已实现回合、前向标签和已实现收益生成 simulated-only 下一步动作；证据不足只能观察和标注候选，风险扩张至少要求 20 个强样本、10 个已实现回合、20 个 60 分钟标签和正已实现收益。
- [x] 2026-07-10 追加修复 A股盘前验收噪声和演化日期门禁：未配置 `MARKETGRAPH_API_TOKEN` 时不再请求 MarketGraph `/regime`，避免盘前 dry-run 反复输出 401；`evolution_controller` 会把过期 `portfolio_evolution.trade_date` 视为今日样本未达标，防止上一交易日样本误挡今日探索仓。生产上 `evolution_decision_latest.json` / log 已恢复为 `marketgraph` 可写。
- [x] 2026-07-09 追加修复 CNFutures 开盘/健康验收：`opening_validator` 与 `market_health` 优先通过 SharedSignals `/realtime_5min?market=Futures` 验证当日 5 分钟条线；`cn_futures_live_check` 将策略主动 hold、无夜盘风格等科学空跑归为 pass/info，只有数据缺失、实盘开关、成交缺 bar time、异常错误或应成交无账本才报警。生产验收确认 Futures 5 分钟 API 返回 20 个合约、最新条线 11:30，live chain 与市场健康均为 pass。
- [x] 2026-07-09 追加修复 CNFutures 夜盘首样本误报：当最新 5 分钟 review 明确全部风格因 `style_session_not_allowed` / `night_session_not_allowed` 主动 hold 时，即使夜盘当前 API 只返回少量合约，`first_sample_alerts` 也归类为 `no_night_session` 的科学空跑，不再触发 `futures_5min_missing_in_session` 或 `cn_futures_first_sim_sample_missing`；真实 API error、日盘覆盖不足、非会话限制 hold 仍保持 warn/fail。
- [x] 文档规则已明确：链路验证样本、非连续竞价样本、缺候选来源或缺成交价来源样本不得占用策略现金、目标持仓数、新买入容量或机会成本换仓判断。

### 2026-07-08 A股 no-trade 空候选证据门与 dry-run 验收加固

- [x] A股 no-trade evidence gate 从 `candidates > 0 && orders == 0` 扩展为当日 `orders == 0` 均需结构化证据：空候选允许 `candidate_decision_trace=[]`，但字段必须存在，且 `capital_plan_decision`、`portfolio_decision` 必须非空。
- [x] `market_health` 与 `ashare_no_trade_summary` 均会把空候选但缺资金/组合决策的日志判为 `evidence_status=incomplete` / warn，不再仅凭 `category=no_candidates` 放行。
- [x] `tests/test_ashare_preopen_dry_run.py` 新增 no-write 断言，确认盘前 dry-run 只写 runtime_test 报告，不触碰 `signals/`、ledger、pending 或 review；该测试已纳入 `full_acceptance --profile quick`。
- [x] 旧通用 `shared/wrappers/run_sim.py` 显式拒绝 A股入口，避免 A股被误切回缺少 no-trade 三段证据的 legacy wrapper；A股 simulated 仍必须走 `tradings_cron_entry --job job_ashare_sim_exec`。
- [x] `front/src/App.test.tsx` 测试环境屏蔽后台定时器，消除 React 异步更新警告，测试信号更干净。

### 2026-07-08 CNFutures 收盘空复盘保护

- [x] `CNFutures/sim_runner.py` 收盘/闭市 5 分钟空跑不再追加空 review 行，避免 15:00 后的 `market_closed` cron 覆盖盘中最后一条有 hold/fill 归因的有效复盘。
- [x] 新增回归测试确保收盘空跑保留既有 `hold_reason_summary`，看板和健康检查可以继续读到最后一条盘中主动不交易原因。

### 2026-07-08 A股成交价来源证明与 USD 市场收益折算

- [x] A股 server-local 模拟执行器会把撮合使用的 `market_snapshot` 和成交价来源证据写入 raw response，并通过本地模拟账本持久化 `fill_price_source`、`fill_price_source_class`、`fill_evidence` 到 `local_sim_trades.jsonl` 与签名回执。
- [x] A股样本质量门禁升级：买入仍必须来自 `candidate_pool_layer=candidate` / `execution_source=ashare_candidate_layer`，卖出仍必须来自 `ashare_rebalance_sell`；同时必须有市场数据成交价来源。缺成交价来源的历史/手工样本归类为 `missing_fill_price_provenance` 链路验证样本，不进入策略 PnL、胜率、方向命中或自我演化。
- [x] `front/` 在权益快照缺失、回退 `style_performance.jsonl` 时，会把 US/Crypto/PM 的 PnL、realized/unrealized PnL 和 max drawdown 统一折算为 CNY，并使用 10,000 USD/USDT/USDC 原币账户对应的人民币本金计算收益率；行内 `*_cny` 和 `fx_to_cny` 优先。
- [x] 公开 dashboard 路由修正：`dashboard.tradingagent.cc` 从旧 Cloudflare Pages 自定义域切到现有 TradingAgent Cloudflare Tunnel/Nginx，避免 Pages 旧静态资源继续服务。Pages 项目 `tradingagent-front` 只保留为历史/回滚入口，重新启用前必须先完成最新构建部署并绑定自定义域。
- [x] 验证：`tests/test_ashare_sim.py` 13 passed；`tests/test_pnl_summary.py` 新增缺成交价来源回归；`tests/test_market_health.py` 联合回归通过；`front/src/server/tradingAgentSnapshot.test.ts` 37 passed；`npm run build:api` passed。

### 2026-07-08 A股闭环证明看板与成交来源验收

- [x] `front/` snapshot 在 A股 `MarketSummary` 上透传 `noTradeEvidence`，包含 no-trade 分类、候选数、订单数、证据完整性、资金计划容量、目标持仓、风险模式和组合允许买入数；首页新增“闭环证明”面板，按市场展示运行态、信号/成交/持仓和 A股 no-trade 证据。
- [x] `front/` 信号行新增只读 `capitalEvidence`，从信号卡的 `scores.capital` / `scores.moneyflow` / `net_mf_amount` 等字段读取个股资金证据；首页新增“A股资金/个股流向”面板，没有真实资金字段时显示等待，不展示样例资金流。
- [x] `shared/runtime_test/ashare_no_trade_summary.py` 新增 `trade_source_check`：当天 A股有 filled 成交时，买入必须有 `execution_source` 且来自 `candidate_pool_layer=candidate`，卖出必须来自 `ashare_rebalance_sell`；`full_acceptance --profile prod` 会把成交来源缺失判为 warn，避免“有成交但不可追溯”被误当作闭环完成。
- [x] 验证：`tests/test_ashare_no_trade_summary.py` 7 passed；`front` 相关 48 tests passed；`npm run lint`、`npm run build`、`npm run build:api` passed；`full_acceptance --profile quick` 123 passed。本机 `--profile prod` 因本机缺生产 cron/runtime 数据失败，需以服务器生产验收为准。

### 2026-07-08 A股 no-trade 证据链与资金维度别名

- [x] A股 `candidate > 0 && orders = 0` 若由动态资金计划防御/容量为 0 导致，`no_trade_explanation.category` 改为 `capital_plan_defensive`，不再笼统归为 `no_portfolio_orders`；健康检查仍把它视为有证据时的策略等待/观察态。
- [x] A股无交易日志写入前会从 run result 顶层回填 `candidate_decision_trace`、`capital_plan_decision`、`portfolio_decision`，避免日志层丢失三段证据导致 dashboard/健康检查误报 incomplete。
- [x] 六维资金维度继续以 `capital` 为规范字段，并补 `moneyflow` 兼容别名；交易信号邮件/旧展示读取 `moneyflow` 时不再显示空值。
- [x] 抖音视频 `7月08日午盘板块资金流向，算力站起来了...` 已能打开并确认发布时间 2026-07-08 11:40；视频观点只作为资金流向分析方法启发，不作为系统事实源。Dashboard 后续可展示系统自己从 SharedSignals `/capital_flow` 复现的个股资金分、近 N 日主力净流入和大单/特大单净买入，板块资金榜需先补 SharedSignals sector 聚合/read model。

### 2026-07-08 TradingAgent simplified acceptance + A股当日证据过滤

- [x] 新增 `shared/runtime_test/full_acceptance.py` 作为 TradingAgent 项目内只读验收快捷入口；`--profile quick` 只跑关键本地测试，`--profile prod` 跑生产运行态只读健康检查，`--profile all` 组合关键测试、生产运行态、全量 pytest 和前端构建；该入口会解析 JSON 内的 `overall_status` / A股 evidence 状态，保留 warn，不发送邮件、不创建订单、不写账本、不安装 cron。
- [x] A股模拟盘健康检查读取 `shared/logs/ashare_no_trade_explanations.jsonl` 时按当前交易日过滤，不再把昨日或更早的完整 no-trade 证据误当作今日科学空跑依据；若今天没有对应证据，继续提示 `server_local_sim_has_no_production_trades_yet`。
- [x] 新增 `tests/test_full_acceptance.py` 和 A股旧日期证据污染回归测试；本地全量 `pytest` 与 `front` 构建已通过。
- [x] 生产侧全量审计发现 `front/dist/assets` 权限属于错误用户会阻断服务器前端构建；该项归入生产同步修复，不是前端代码错误。

### 2026-07-08 旧 USD 本金历史样本隔离工具

- [x] 新增 `shared/runtime_test/quarantine_legacy_usd_capital.py`：扫描 `shared/logs/sim_ledger/{us,crypto,pm}/*/` 与 `shared/review/{us,crypto,pm}/` 下的 `daily_mark_to_market.jsonl`、`trade_journal.jsonl`、`positions.json`、`style_performance.jsonl`、`style_comparison.json`，检测 `capital_base > 12,000` 原币、`capital_base_cny > 80,000`，或显式 `--before <cutover_iso>` 资金口径切换前的旧本金口径行和旧持仓状态，添加 `exclude_from_dashboard=true`、`run_context=legacy_usd_capital_quarantine`、`quarantine_reason` 隔离标记。
- [x] 默认 dry-run；`--apply` 时先备份为 `.bak` 再原地修改；不删行；不触碰 A股/CNFutures/HK 与 `real_execution=true` 行；已隔离行幂等跳过；结果写入 `shared/review/ops/quarantine_legacy_usd_capital_<batch>.json` manifest。
- [x] 新增 32 个测试（`tests/test_quarantine_legacy_usd_capital.py`），覆盖行级检测、cutover 时间隔离、dry-run/apply、新旧混合、真实执行排除、A股/期货排除、幂等性、空目录、trade_journal/positions 目录级隔离、style_performance/style_comparison 隔离和 manifest 输出。
- [x] `SimLedger.daily_mark_to_market()` 已从 `positions.json` 继承 `exclude_from_dashboard` 等审计字段，前端 `listSimLedgerFiles()` 会把已隔离 positions 所在风格目录的 MTM/权益/交易文件整体排除，`style_performance.jsonl` 中对应风格行也会跳过；新增测试覆盖“隔离 positions 后重跑 MTM 或 style performance 不复活旧 PnL/tradeCount”。
- [x] 默认命令行入口：`python3 -m shared.runtime_test.quarantine_legacy_usd_capital --pretty --before <cutover_iso>`（dry-run）、`--apply` 执行。

### 2026-07-07 A股 evidence reason diagnostics + CNFutures pre-open/PnL

- [x] A股 `score_diagnostics` 新增 `evidence_reason_summary`、`evidence_source_summary`、`missing_and_default_like_dimension_counts`、`evidence_coverage_distribution` 和全维缺证据样本 reason，用具体 reason 定位 SharedSignals/MarketGraph 哪条证据链缺失，不降低 `combined >= 0.55` 候选门槛。
- [x] `ashare_preopen_dry_run` 在候选池为空时同步输出上述 score diagnostics；`ashare_opening_validator` 会把 `missing_regime`、`missing_capital_flow_rows`、`insufficient_daily_bars` 等映射到 `check_marketgraph_all_weather_regime`、`check_sharedsignals_capital_flow`、`check_sharedsignals_daily_bar_history` 等可执行动作。
- [x] `ashare_preopen_dry_run` 的最新样本选择改为先限定普通 A股代码再取最新交易日；SharedSignals 同库内可转债/债券更新到更新日期时，不会再把普通股票候选池挤成 0。执行门禁价格会在 reader 缺失时回退 read model 最新收盘价；资金计划明确无新买预算时显示 pass/观察态但 `ready=false`，避免把“已有持仓/现金不足所以不新增买入”误报为执行故障。
- [x] CNFutures 将可执行合约判断提升到 `contract_rules.is_executable_contract_symbol`，adapter 与盘前验收共用同一过滤；盘前验收同时报告 raw/executable symbol、产品覆盖、5分钟 read model 可达性和风格状态。
- [x] CNFutures force-flatten 平仓按已有持仓 `avg_price` 和成交价计算 realized PnL；`score_records` 增加 `pnl_attribution`，避免“收益 0”被误读为真实无收益而不是样本不足或未闭合盈亏。

### 2026-07-07 A股健康检查 advisory 收口

- [x] `shared/runtime_test/market_health.py` 将“全部为 `outside_ashare_regular_session` 的 A股链路验证样本”从 warn 降为 pass/info advisory；样本仍在 details 中展示，并继续隔离出策略绩效/方向命中/自我演化口径。
- [x] A股开盘/盘中验收读取 `ashare_no_trade_explanations.jsonl` 时保留 `score_diagnostics`，并将 `candidate_pool_status`、`data_quality_status`、`max_combined`、候选阈值和 threshold counts 带入 `latest_no_trade_log` / `diagnostic_summary`；空池告警可直接区分策略阈值未过、研究维度中性或候选池分层异常，不改变交易阈值。
- [x] A股资金计划对账接入本地模拟样本质量：若资金计划早于 outside-session 链路验证样本快照且不一致，显示 pass/info advisory；若是真实策略样本后的资金计划滞后、缺计划、来源缺失、账本/快照不一致，仍保持 warn/fail。
- [x] A股失败/回执健康检查同步接入样本质量：纯 outside-session 链路验证样本不再要求策略成交回执，显示 pass/info advisory；失败订单、真实策略成交或缺来源样本仍保持 warn。
- [x] 新增/更新 `tests/test_market_health.py` 覆盖链路验证样本 advisory、资金计划 validation-only advisory 和回执 validation-only advisory，保留真实错配/缺来源 warn/fail 测试。
- [x] 生产侧已确认 SharedSignals A股资产入口通过 `/tushare?api_name=stock_basic` 恢复，TradingAgent reader 可读取 5000 条 A股资产样本；本次另发现 SharedSignals `/health` 在大 WAL/并发巡查期间仍可能慢响应，归入 SharedSignals 读侧运维优化，不作为 TradingAgent 策略成交故障。

### 2026-07-07 A股盘前 dry-run 验收

- [x] 新增 `shared/runtime_test/ashare_preopen_dry_run.py`：在开盘前只读预演 A股日线覆盖、最新高流动性普通 A 股小样本候选池评分、200,000 元模拟账户动态资金计划和执行门禁；默认样本上限 50 只，避免盘前检查全市场逐票扫描，也避免 10 只小样本误判候选池；只写 `shared/runtime_test/ashare_preopen_dry_run_latest.json` / history，不写 `signals/`、server-local ledger、pending、review 或实盘队列。
- [x] 新增 wrapper `shared/wrappers/job_ashare_preopen_dry_run.sh` 与 cron 模板 `35 8 * * 1-5`；wrapper 默认 90 秒超时、单次运行，异常时走系统邮件通道，正常通过不发邮件。
- [x] 新增 `tests/test_ashare_preopen_dry_run.py`，覆盖数据过期 fail、候选为空 warn 安全空跑、候选/资金/价格正常时生成只读 synthetic order 且带 `candidate_pool_layer=candidate` 与 `execution_source=ashare_candidate_layer`。

### 2026-07-07 A股/CNFutures 开盘稳定性修复

- [x] A股 `AshareAdapter` 的生产 `score_universe_limit` 从 200 扩到 500，覆盖到候选池 cap，避免只评分 universe 前几百只导致 candidate 层长期为 0；新买入仍必须来自 `candidate_pool_layer=candidate`，不放松候选阈值、不回退到 watch/universe 硬买。
- [x] A股 universe 已在过滤后按近期成交额/流动性做稳定预排序，避免 `score_universe_limit` 截断到代码顺序前段；候选池构建复用本轮 `score_universe` 预计算评分，不再对 candidate/watch 层逐票重算，避免候选分层、排序、资金计划和诊断口径漂移。
- [x] A股 `no_trade_explanation` 已补 `score_diagnostics`：候选为 0 时记录已评分数量、候选阈值、Top 分数、中性默认维度计数和缺失维度计数，后续可直接判断是 SharedSignals/MarketGraph 研究维度不足，还是候选确实没有达到交易门槛。
- [x] A股空池诊断新增 `candidate_above_threshold_count`、`watch_above_threshold_count`、`max_combined`、`candidate_pool_status` 和 `data_quality_status`，用于区分“策略阈值未过”“观察层有效但未达买入”“候选池分层异常”和“研究维度大面积中性”。
- [x] A股六维评分已补供数兼容：`TradingagentDataReader.get_factors()` canonical 查询 `Ashare` 与带后缀代码，`six_dimension_scorer` 在旧 factors 为空时读取 `get_fundamentals()` 与 `get_capital_flow()` 行并映射为 value/growth/quality/capital 分；资金分只混合金额字段并对专用 `/capital_flow` 行优先去重，避免把 `net_mf_vol` 当金额；候选池/轮动条件统一识别 `api_name:metric` 前缀；保留缺数据回退 0.5，不放松 candidate 阈值。
- [x] A股文档已明确候选池样本不能过小，扩大评分覆盖只解决“看得太少”的问题，不改变“无科学候选就留现金/逆回购”的资金门禁。
- [x] CNFutures 继续由 TradingAgent 只读 SharedSignals `market_bars_intraday`；Tushare 5 分钟期货接口空返回的备源修复在 SharedSignals 完成，TradingAgent 不新增独立采集。
- [x] CNFutures adapter 的 universe/合约发现已改为 `TradingagentDataReader` reader 优先，开盘验收也优先通过 reader 查询 Futures 日线/5分钟线；直接 SQLite 只保留为统一 `TRADINGAGENT_ALLOW_SHARED_SIGNALS_SQLITE=1` 的本地测试/紧急诊断路径，不再保留市场专属兜底变量。
- [x] 2026-07-08 追加清理 CNFutures/A股验收和模拟 wrapper 的数据入口：统一通过 SharedSignals API；本地 SQLite 只允许显式诊断开关，不再把 MarketGraph runtime 当事实源或兼容 fallback。
- [x] CNFutures 5 分钟 runner 已修正闭市口径：收盘后再次运行返回 `market_closed` 并写正常复盘行，不再把闭市后的最后一根有效 bar 误报为 stale；交易时段内真实 stale 仍保持拦截。
- [x] CNFutures 开盘/首样本验收已区分 5分钟数据缺失、首模拟样本缺失、策略主动 hold 和夜盘未授权风格不交易；该修复只读复盘 `hold_reason_summary`，不改变模拟成交策略。
- [x] CNFutures 开盘验收的 read-model SQLite 兜底放宽到 `5min`/`5m`/`5` interval 统一口径，不再锁死 `rt_fut_min` provider；reader 短缺时仍标记 `reader_shortfall`，TradingAgent 不新增独立采集。
- [x] PM/Crypto 健康检查已把空跑分成 `market_data_wait`、`strategy_wait`、`execution_fault`：缺行情是数据等待（warn），PM 缺 MarketGraph 独立概率/edge 不足或 Crypto 动量阈值未过是策略等待（pass/观察态），不再同时混入 `market_data_degraded`；只有应成交却无账本才算执行故障。
- [x] 前端市场摘要新增 `runtimeState` / `executionFault`，市场切换后显示“运行中 / 策略等待 / 需要处理 / 等待数据”的结果层，避免把所有 warn 误读为系统坏；桌面和移动页面已做真实切换检查。CNFutures 看板摘要已接入 `shared/review/data/cn_futures_sim_reviews.jsonl`，有复盘 hold 样本时不再误显示为“等待数据”。
- [x] 2026-07-07 盘中已确认的生产症状：A股 job 正常运行但 `candidate_count=0`、无成交、权益约 200,000；CNFutures job 运行但缺 5 分钟 Futures bar、无成交。上述状态不能算“可交易健康”，必须等生产部署后复验 candidate、bar、filled/hold/no-trade 归因。

### 2026-07-05 simulated equity snapshot writer for dashboard

- [x] `shared/accounting/sim_ledger.py` 的 `daily_mark_to_market()` 已补齐前端收益看板所需字段：`capital_base`、`total_equity`、`total_pnl`、`realized_pnl`、`unrealized_pnl`、`return_pct`、`target_return_pct`、`max_drawdown_pct`、`trade_count`、`missing_mark_count` 和 `pnl_source`。
- [x] 新增 `shared/review/equity_snapshots.py` 与运维入口 `shared/runtime_test/write_equity_snapshots.py`，可扫描 `shared/logs/sim_ledger/<market>/<style>/positions.json`，读取 SharedSignals 价格后追加 `daily_mark_to_market.jsonl`；价格缺失时保守按成本估值并标记 `sim_ledger_cost_fallback`。
- [x] 该入口只写模拟账本权益快照，不写 `signals/`，不触发交易，不接触实盘队列、账户、邮件或 webhook；前端 `front/` 会把这些快照作为实时收益曲线的最高优先级来源。
- [x] 新增 `shared/wrappers/job_equity_snapshots.sh` 与 crontab 模板，每 5 分钟刷新一次模拟盘权益快照；wrapper 带 flock、独立日志和 `TRADINGAGENT_DASHBOARD_TARGET_RETURN_PCT` 目标收益配置。
- [x] `load_mark_prices_for_positions()` 已按市场读取最近可用价格：A股/美股/期货取最近日线，Crypto 取最新 ticker，PM 按 `market_id` 取预测市场最新价格；周末/节假日不再只查当天导致全部成本价兜底。若仍缺价，会继续保守标记 `sim_ledger_cost_fallback`。
- [x] `front/` 首页机会管道已改为事件优先的动态管道展示：机会进入、初筛、研究、风控、待执行和结果分流在同一面板内流动展示；该面板只读 `funnelEvents` / signals，不写队列、不触发执行。

### 2026-07-05 combined-cron health reporting

- [x] `cron/health_check.sh` 已从旧 `combined_crontab` 脚本检查迁移为只读 HTTP/API 探针：验证 SharedSignals cache/capabilities/health、TradingAgent 模拟盘最新输出和 MarketGraph health；三系统合并 crontab 不再由 TradingAgent 或 MarketGraph 仓库脚本安装/覆盖。
- [x] 检查结果写入 SharedSignals `logs/watchdog_inputs/tradingagent_health.json(.jsonl)`，复用现有 watchdog 与系统邮件链路；不新增 daemon、不安装新 crontab、不修改交易队列。
- [x] SharedSignals API 探针增加 3 次短重试，避免 API 单次慢响应或 SQLite 瞬时锁竞争把整条健康链路误报为 `critical`；连续失败仍按 critical 上报。
- [x] 该检查只读执行，不会安装或覆盖 live crontab；跨系统 crontab 合并属于仓库外运维层，不能恢复 MarketGraph 已删除的合并安装脚本。

### 2026-07-05 cross-repo path and stale docs cleanup

- [x] TradingAgent 模拟回执只读取/写入 `signals/sim_execution_receipts.jsonl`；旧跨系统输出路径不再作为兼容读取面。
- [x] `TradingagentDataReader` 不再默认读取同机 MarketGraph 仓库，也不再读取 MarketGraph CSV；MarketGraph 研究证据通过 API 读取，便于未来三系统分服务器独立运行。
- [x] A股 T+1 日历不再扫描 SharedSignals 目录，改为通过 `TradingagentDataReader.is_trading_day` API 判断；API 缺失时使用内置节假日保守回退。
- [x] `shared/env_loader.sh` 不再 source MarketGraph deploy env，也不再把 MarketGraph 仓库加入 `PYTHONPATH`；TradingAgent env 优先，公共 finance env 仅作为兼容密钥来源。
- [x] TradingAgent cron 模板移除 MarketGraph 观察任务；MarketGraph 任务归属 MarketGraph 自有调度，TradingAgent 只通过 API 健康检查读取其状态。
- [x] 删除 2026-06-30 过期 handoff 文档，重写 `docs/data_sources.md`，修正 `docs/AGENTS.md` 中过期/不存在入口。

### 2026-07-05 simulated matching residual risk fixes

- [x] 主服务器 live crontab 已恢复为 SharedSignals + TradingAgent 合并版本；CNFutures 单市场健康检查中 `cn_futures_cron` 从 fail 恢复为 pass。修复前缺失的是 SharedSignals `cn_futures_5min.sh` 采集调度，不是 TradingAgent 模拟执行器。
- [x] 当前 `market_health.py --market cn_futures` 生产结果为 warn/0 fail：cron 已齐，剩余 warn 是周末/闭市尚无 Futures 5分钟数据、模拟复盘样本和风格输出，等待下一交易时段产生样本。
- [x] `CNFutures/contract_rules.py` 与 `margin_model.py` 已将期货手续费从隐式数值判断升级为显式 `open_fee_type` / `close_fee_type`，支持 `rate` 与 `fixed_per_lot`，当前测试覆盖 `rb` 费率制和 `m` 固定每手制。
- [x] `shared/runtime_test/market_health.py --market cn_futures` 已接入 CNFutures 只读 live-chain validator，方便单市场排查 SharedSignals 5分钟数据、cron、模拟日志、复盘样本、风格输出和实盘关闭状态。
- [x] `front/` 看板信号漏斗新增 `stageEvidence`，成交账本回放标记为 `replay`；风险拒绝、触发、成交和部分阶段时间会在只读 snapshot 中体现，前端不会写入任何交易队列。

- [x] `SimExecutionEngine` 市场单边界检查改为按预估执行价判断，避免 dummy `limit_price` 误触发涨跌停/概率边界；新增轻量 `counterparty_profile` / liquidity / impact 参数用于 A股散户/对手盘环境模拟。
- [x] A股 server-local 执行器不再用默认 `ask_size=quantity` 覆盖 5分钟 `bar_volume`，无盘口量时会按 bar volume 与整手约束形成 partial fill。
- [x] A股 server-local 执行器会从本地模拟账本补齐 `cash_available` 与 T+1 `sellable_qty`，覆盖字符串 account 路径；同日卖出和现金不足已有集成测试。
- [x] `auto_pipeline` 已从 SharedSignals reader 的 5分钟/日线 bars 生成 `market_snapshot`，`StyleRunner` 会透传盘口、bar volume、previous_close、现金/可卖量和对手盘环境字段。
- [x] `auto_pipeline` 已兼容当前 `DecisionEngine` 旧接口，新增 all-stage smoke；A股基础 styles 已全部通过统一 `TradeStyle` 校验，`closing_momentum` 保持 paused。
- [x] `TradingagentDataReader.get_bars_intraday()` 已改为 SharedSignals API-first，通过 `/realtime_5min?market=...` 读取 A股/期货 5 分钟 read model；API 不可用或返回空壳时生产 fail-closed，仅显式诊断开关允许本机 SQLite 只读排障。
- [x] `market_health` 对 A股/CNFutures 首样本状态加入交易时段判断：闭市等待不再形成系统 warn；交易时段应有样本但缺失时仍保持 warn，避免周末误报和开盘漏报。

### 2026-07-05 Ashare health bootstrap receipts

- [x] A股健康检查对 `signals/positions/simulated_ashare_positions.json` 增加 bootstrap 判断：服务器本地模拟盘尚无成交时，缺少持仓快照不再误报为异常；一旦出现本地模拟成交，仍要求快照可读。
- [x] `failure_receipts` 检查增加本地模拟成交计数：无失败、无本地模拟成交时视为“回执待首笔事件生成”；有失败或有成交时仍必须存在可读回执。
- [x] 新增回归测试覆盖无交易 bootstrap、失败无回执告警和影子盘状态推进，避免健康检查在周末/首日空样本时持续误报。

### 2026-07-05 style evolution runtime state isolation

- [x] `shared/markets/evolution_engine.py` 不再把演化后的 weight/status/paused/deprecated 写回 `Crypto/PM/US/HK/styles/*.json`。
- [x] 自动生成 variant 已迁到 `shared/review/<market>/generated_styles/`；`StyleRunner` 会合并读取基础 styles 与 runtime generated styles。
- [x] `shared/markets/performance_tracker.py` 移除 Python 版本不兼容的 `zip(..., strict=True)`，恢复本机/服务器兼容。
- [x] 新增 `tests/test_evolution_runtime_styles.py`，覆盖基础 styles 文件不变和 runtime variant 可被 StyleRunner 加载。

### 2026-07-04 CNFutures health/ops/dashboard data outlet

- [x] `CNFutures/review.py` 在追加 `cn_futures_sim_reviews.jsonl` 后，同步写入 `shared/review/cn_futures/style_comparison.json` 和 `style_performance.jsonl`，复用现有 metrics/style-comparison 数据出口，不新建单独看板。
- [x] 期货复盘新增 `error_summary` 与 `style_health`，按风格归类 `stale_intraday_bar`、`repeated_same_side_exposure` 等拒绝原因，并给出 simulated-only 的观察/降权建议。
- [x] `shared/runtime_test/market_health.py --market sim` 默认纳入 `cn_futures`，使用 CNFutures append-only review 作为模拟账本依据；真实样本未出现时按 warn 处理，不误报为生产故障。
- [x] `shared/runtime_test/ops_report.py` 新增 `cn_futures_review_summary`；`shared/review/metrics_dashboard.py` 可读取 `shared/review/cn_futures/style_performance.jsonl`，后续看板可直接接入。
- [x] 边界：本次只增加复盘、健康和看板数据出口，不写实盘队列、不接 CTP、不修改生产 crontab。

### 2026-07-04 email route residual fixes

- [x] `.env.example` 的 `CLOUDFLARE_EMAIL_FROM` 使用 `notice@tradingagent.cc`。
- [x] `shared/notify/alert_router.py` 文档注释已同步为交易通道 `notice@tradingagent.cc -> tradingadviser@coze.email`。
- [x] simulated evolution circuit breaker 的系统告警不再硬发 `tradingadviser@coze.email`；`send_template_email()` 已改为显式 `channel` 优先解析默认收件人。

### 2026-07-04 system email smoke verification

- [x] 主服务器实测 TradingAgent 系统邮件：Cloudflare Email Service 从 `notice@tradingagent.cc` 发往 `soc@coze.email` 成功，主题含 `[SMOKE][TradingAgent][系统]`。
- [x] 邮件边界保持不变：交易类发 `tradingadviser@coze.email`，系统类发 `soc@coze.email`，发件邮箱使用 `notice@tradingagent.cc`。

### 2026-07-04 CNFutures 5-minute simulated trading cadence

- [x] `CNFutures/adapter.py` 已支持读取 SharedSignals `market_bars_intraday`，使用 `market="Futures"`、`interval="5min"` 作为期货 5 分钟模拟交易输入。
- [x] `CNFutures/adapter.py` 默认读取器已修正为 `TradingagentDataReader`，保证默认路径走 SharedSignals API-first；SQLite 直接读只保留为显式诊断/测试，不再因 `SHARED_SIGNALS_DB` 自动启用。
- [x] `CNFutures/run_simulation.py` 默认 `--cadence 5min`；`CNFutures/sim_runner.py` 会优先读取分钟线，订单幂等键包含最新 `bar_time`，避免 5 分钟调度被同日幂等挡住。
- [x] 5 分钟 runner 已加入 `--max-intraday-bar-age-minutes` / `CN_FUTURES_MAX_INTRADAY_BAR_AGE_MINUTES`，默认最新 bar 超过 10 分钟则拒绝模拟下单并记录 `stale_intraday_bar`。
- [x] 同一交易日、同一风格、同一合约的连续同方向模拟信号会被标记为 `repeated_same_side_exposure`，避免每 5 分钟重复加同方向风险；反向信号仍允许形成新模拟成交。
- [x] `shared/wrappers/job_cn_futures_sim.sh` 显式以 `--cadence 5min` 运行，仍只写 simulated signal/review，不写实盘队列。
- [x] 生产 crontab 模板已改为期货日盘/夜盘每 5 分钟运行，并相对 SharedSignals 采集错后 1 分钟读取最新 bar。
- [x] 生产已确认 Tushare/QuickSync `rt_fut_min` 权限不足；SharedSignals 已退役 AKShare/Sina 隐式备源，CNFutures 5 分钟链路在主 provider 无权限时必须显式 degraded/failed。TradingAgent 继续只读 SharedSignals API，不直接调用 AKShare/Tushare。

### 2026-07-04 CNFutures review scoring + fail-closed live reserve

- [x] `CNFutures/review.py` 新增 `score_records()`，复盘 JSONL 每轮追加 `score_summary`；open-only 或样本不足默认 `sample_insufficient`，不伪造收益能力。
- [x] 评分字段覆盖 `trade_count`、`filled_count`、`fee`、`margin_required`、`notional`、`realized_pnl`、`win_rate`、`max_drawdown`、`score` 和 `status`。
- [x] 新增 `CNFutures/live_gateway.py` 作为未来 CTP/SimNow/期货公司接入占位；当前 `real_trading_enabled=false`、`broker_adapter_ready=false`，所有真实期货订单请求抛 `SafetyViolation`，不得降级为 simulated。
- [x] `CNFutures/README.md` 已同步评分用途与实盘预留边界。
- [x] `shared/crontab.txt` 已补 CNFutures 模拟入口；SharedSignals 负责期货行情采集，TradingAgent 只读 SharedSignals read model 做 simulated 交易。

### 2026-07-04 SharedSignals-only data-source audit

- [x] 主服务器 `/opt/investment/tradingagent` 已确认：TradingAgent 生产模拟盘、影子盘、健康检查和研究路径不直接采集外部市场数据；市场数据入口是 SharedSignals API-first reader，SQLite read model 只作显式诊断读取。
- [x] 服务器误拷贝的 untracked `Users/` 目录含 7 个过期文件，落后于当前 `shared/wrappers/tradings_cron_entry.py`、`shared/execution/local_sim_ledger.py`、A股节假日/T+1 修复等生产代码；已删除并在 `.gitignore` 中忽略 `Users/` 防止复发。
- [x] `crontab.txt` 与 `shared/crontab.txt` 已改为 2026-07-04 生产快照/边界说明；旧 2026-07-03 模板不再作为当前生产事实。
- [x] 服务器运行状态：A股本地模拟、Crypto、US、PM、健康检查、复盘、CNFutures 模拟入口均在 `marketgraph` 用户 crontab 中；HK 按 Nicholas 决策继续停用。

### 2026-07-04 server-side simulated trading closure

- [x] 后续调整：HK 按 Nicholas 最新决策暂不接入生产模拟盘，HK cron 已停用，默认模拟健康检查范围改为 A股/Crypto/PM/US；HK wrapper 和配置保留，未来可显式恢复。

- [x] A股模拟盘默认改为服务器本地 `server_local_sim_only` paper fill，Hermes/同花顺 GUI 路径保留为 `ASHARE_SIM_HERMES_ENABLED=1` 的第二选择；默认不写 `signals/pending`，不再因 Mini 不在线阻断服务器训练数据。
- [x] `job_ashare_sim_exec` 默认关闭 Hermes/webhook，只运行服务器本地模拟闭环；Hermes 启用后仍保留 mini health/backpressure 和回执保护。
- [x] `sim_broker` 支持在新进程中自动加载 A股/Crypto 内建 executor，并只对 `filled|partial|pending` 结果写本地模拟备份，避免 failed/rejected 污染账本。
- [x] 多市场 `StyleRunner` 已接统一 `shared/accounting/sim_ledger.py`，Crypto/PM/US/HK 的 filled/partial simulated 结果写入 `shared/logs/sim_ledger/<market>/<style>/`，重复订单按 `order_id` 幂等跳过。
- [x] 新增 `shared/review/sim_ledger_reader.py`，日报/周报/归因/汇总邮件已从旧 shadow-only 输入升级为 review 输入：legacy shadow fills + 统一 simulated ledger + A股 server-local simulated ledger；邮件和 JSON 结果新增 review/shadow/simulated/real 分层计数。
- [x] Crypto 模拟器在 SharedSignals 行情缺少可用价格时使用信号自带价格兜底，避免仅因行情接口空值丢失可训练样本。
- [x] HK 新增 `job_hk_sim.sh`，`run_sim.py` 已支持 HK；2026-07-05 按 Nicholas 最新决策改为默认 fail-closed，只有显式 `TRADINGAGENT_HK_SIM_ENABLED=1` 才能手动运行，`Global/HSI` 代理回退需额外 `SIM_HK_PROXY_ENABLED=1`。
- [x] `market_health.py --market sim` 新增多市场模拟健康检查，覆盖 cron、SharedSignals 数据、最新运行 JSON 和统一模拟账本；`job_sim_market_health.sh` 已加入 marketgraph crontab，每 10 分钟只读巡检；当前结果为 Crypto/PM/US pass，A股/HK warn，0 fail。
- [x] 验证：A股隔离执行确认不启用 Hermes 时可本地成交且不写 pending；手动运行 crypto/pm/us `run_sim.py` 返回 ok；HK 旧 HSI 代理成交仅作历史样本，当前默认 disabled；日报/周报新增模拟账本读取回归；目标测试与 `py_compile` 通过记录见本轮回执。

### 2026-07-04 A股 SharedSignals API universe + health fix

- [x] `TradingagentDataReader.get_assets()` 新增 A股 API-first 资产入口，通过 SharedSignals `/tushare?api_name=stock_basic` 读取 3781 条资产，健康检查识别 3480 条普通 A股。
- [x] `get_bars_daily()` / `get_market_data()` 修复单日查询参数：只有 end/date 时自动设置 start=end，避免 SharedSignals `/market_data` 返回空壳行。
- [x] A股健康检查改用生产同款 `TradingagentDataReader`，空影子账本用 `shadow_broker` 回放为 0 PnL；模拟持仓快照缺失从 fail 调整为 warn；脚本可直接运行并默认使用本机 SharedSignals API `127.0.0.1:8082`。
- [x] 补齐真实交易安全门日志路径所需 `logging` 导入，避免错误分支复盘日志触发 `NameError`。
- [x] 验证：`tests/test_data_reader.py tests/test_market_health.py` 12 passed；A股健康检查 6 pass / 2 warn / 0 fail；隔离闭环测试确认 Mini webhook 禁用时不写 Hermes `signals/pending`，仍可完成服务器本地 paper fill。

### 2026-07-04 P2 cleanup smoke coverage

- [x] `shared/orchestrator.py` 新增可选批量打分路径：默认依赖使用 `score_universe`，不可用时回退逐标的 `score_stock`。
- [x] `shared/wrappers/tradings_cron_entry.py` 移除 `_DEPRECATED` shadow job handler 注册，保留当前 `job_trading_signals`、sim 和日度入口。
- [x] 新增最小 smoke 测试覆盖 `shared/execution/sim_engine.py` 与 `shared/execution/auto_pipeline.py` 的 import/init/no-crash；新增 orchestrator 批量打分回归。
- [x] 验证：本地目标 `py_compile` 与 pytest 结果见本轮回执。

### 2026-07-04 P1 runtime hardening fixes

- [x] `shared/execution/execution_router.py` 路由历史读取已从整文件 `.readlines()` 改为 tail 读取最近固定行数，避免 JSONL 日志增长后一次性占用过多内存。
- [x] `shared/risk/pre_trade_check.py` 与 `shared/portfolio/exit_manager.py` 的 A 股 T+1 fallback 已补 2026 已知休市日；当 `Ashare.t_plus_1` 不可用时不再只跳周末。
- [x] 新增 `.github/workflows/test.yml` 与最小 `requirements.txt`，CI 覆盖 `compileall` 与 pytest。
- [x] 验证：本地目标 `py_compile` 与 pytest 结果见本轮回执。

### 2026-07-04 P0 resilience fixes

- [x] `position_ledger.py`、`capital_ledger.py`、`signal_state_machine.py`、`local_sim_ledger.py`、`shadow_broker.py` 的阻塞 `flock(LOCK_EX)` 已改为 `LOCK_EX | LOCK_NB`，并加 3 次递增等待重试；拿不到锁时显式 `TimeoutError`，不假成功。
- [x] `webhook_sender.py` 的 `time.sleep(0)` 已替换为指数退避，避免 Mini webhook 异常时忙等。
- [x] `shared/review/benchmark.py` 的 SQLite 直接读取已改为显式诊断模式；未设置 `TRADINGAGENT_ALLOW_SHARED_SIGNALS_SQLITE=1` 时不直接打开 SharedSignals read model。
- [x] 验证：目标 Python `py_compile` 通过；受影响 TradingAgent pytest 集合 54 项 + 6 subtests 通过；2026-07-07 已将 `WEBHOOK_SECRET` 空值告警改为仅在实际发送 Mini webhook 时触发，普通模拟盘/研究/健康检查导入路径不再刷生产日志。

### 2026-07-04 A股 API-first 与邮件通道对齐

- [x] `shared/env_loader.sh` 已默认注入 `SHAREDSIGNALS_API_URL=http://127.0.0.1:8082`，A股 `job_ashare_sim_exec` 运行时通过 SharedSignals/ShareChannel API 优先取数。
- [x] Cloudflare 邮件凭据加载入口已收口到 TradingAgent 自有 `/opt/tradingagent/.env` 或 `/opt/investment/tradingagent/.env`，不再读取 MarketGraph env；继续兼容 `CF_EMAIL_*` alias。
- [x] `/opt/investment/.env` 的旧邮件地址漂移已修正；交易通道和系统通道按 `AGENTS.md` 分流。

### 2026-07-04 测试状态泄漏修复

- [x] `tests/test_signal_state_machine.py`、`test_real_money_boundary.py`、`test_t_plus_1_integration.py` 中直接改写的模块级路径已改为 `patch.object` + cleanup，测试结束后自动恢复原值。
- [x] `tests/test_sim_loop.py` 已保存并恢复 `sim_executor_registry` 注册表，避免测试注册的 market executor 泄漏到后续用例。
- [x] 验证：`python3 -m pytest tests/test_signal_state_machine.py tests/test_real_money_boundary.py tests/test_t_plus_1_integration.py tests/test_sim_loop.py -q` 通过（23 passed，1 个既有 `WEBHOOK_SECRET` 空值 warning）。

### 2026-07-04 六维打分异常处理修复

- [x] `shared/screening/six_dimension_scorer.py` 的 6 个 `_score_*` 维度函数已改为：数据缺失仍返回中性缺省，真实异常记录 `logger.error(..., exc_info=True)` 并返回 `None`。
- [x] `score_stock()` 统一处理 `None` 为 `combined.missing_default`，避免维度函数把异常静默伪装成有效 0.5 分。
- [x] 验证：`py_compile shared/screening/six_dimension_scorer.py` 通过；本地 smoke 确认异常维度返回 `None`，外层按缺失数据回退。

### 2026-07-04 shadow broker + evolution guard/cron 修复

- [x] 新增 `shared/execution/shadow_broker.py`：影子盘只记录不执行，支持 JSONL trade log、JSON positions/pnl、按 strategy/date/market 查询 PnL，并拒绝 real/live/direct payload。
- [x] `shared/markets/evolution_engine.py` 的 `evaluate_all_markets()` 已前置调用 `evaluate_guard()`；guard 阻断时返回 `state=guard_blocked`，不执行调权或 variant 生成。
- [x] `cron/evolution.sh` 已改为调用函数入口 `evaluate_all_markets()`，保留 flock、日志、timeout、`.env` 读取和可配置 markets/review root。
- [x] 验证：`py_compile`、`bash -n cron/evolution.sh`、shadow/router 聚焦回归 20 项 + 4 subtests、orchestrator/sim loop 回归 9 项 + 2 subtests、evolution guard smoke 通过。

### 2026-07-04 多市场 shadow runner 补齐

- [x] 新增 `PM/shadow_runner.py`、`Crypto/shadow_runner.py`、`US/shadow_runner.py`、`HK/shadow_runner.py`，均为 sim/shadow-only：读取 universe、生成最小 mock order、调用本地 simulator、写入 `shadow/pending` signal card。
- [x] 写入卡片固定 `capital_layer=shadow`、`account_type=shadow`、`real_execution=false`、`direct_execution=false`，并复用现有安全 guard 拒绝 real/live/direct payload。
- [x] 验证：新增文件 `py_compile` 通过；`tests/test_market_base_layer.py tests/test_pm_workflow_config.py tests/test_crypto_phase_d_tools.py tests/test_us_hk_phase_d_p0.py` 通过（15 passed，7 subtests passed）。

### 2026-07-04 production-grade simulated execution layer

- [x] 新增 `shared/execution/sim_engine.py`：`SimOrder` / `SimFill` / `SimPosition`、A-share 1bps commission + 2.5bps sell stamp duty、波动率滑点、partial fill、queue position、price improvement 和 `pending -> open -> partial/filled/cancelled/rejected` 状态机。
- [x] 新增 `shared/execution/risk_manager.py`：JSON profile 驱动的 max order/position/notional/daily loss、global/market/symbol/hardware halt、volatility/loss circuit breaker、gross/net/delta/beta exposure 和 symbol/sector/market concentration 检查。
- [x] 新增 `shared/accounting/sim_ledger.py`：append-only trade journal、FIFO position/tax lots、cash ledger、double-entry records、daily mark-to-market、CSV/JSON audit export。
- [x] `auto_pipeline` 的 `execute_sim` 阶段已先走 production sim risk -> engine -> ledger，再保留原 StyleRunner 风格比较输出；所有输出继续固定 `capital_layer=simulated`、`real_execution=false`。
- [x] 每个迁移对象提供 `to_real()` placeholder，默认受 `REAL_TRADING_ENABLED` fail-closed 保护，不会自动变成真实交易入口。
- [x] 验证：新增 `tests/test_sim_production.py` 6 项通过；相关回归 `tests/test_sim_production.py tests/test_auto_pipeline.py tests/test_sim_broker_v2.py tests/test_local_sim_ledger.py tests/test_multi_style_sim.py` 共 16 项通过；新增/修改 Python `py_compile` 通过。

### 2026-07-04 simulated auto pipeline

- [x] 新增 `shared/execution/decision_engine.py`：将基本面质量分、多视角 consensus、red flags 合成为 buy/watch/skip 决策，并通过组合构建输出 simulated target positions。
- [x] 新增 `shared/execution/auto_pipeline.py`：按 Crypto/US/PM/Ashare 执行 pre-market scan、research、decision、sim execution 和 daily review/evolution；候选、决策、账户和信号均强制 `capital_layer=simulated`、`real_execution=false`，发现 real/live 负载直接跳过或拒绝。
- [x] 新增 `cron/auto_pipeline.sh` 与 `shared/crontab.txt` 模板行：`0 9 * * 1-5 /opt/investment/tradingagent/cron/auto_pipeline.sh`；脚本带 flock、日志和 timeout。
- [x] 验证：`py_compile` 通过；`bash -n cron/auto_pipeline.sh` 通过；`tests/test_auto_pipeline.py` 3 项通过；auto pipeline + multi-style/evolution/fundamental/multi-perspective 相关回归 12 项通过。
- [x] 生产 crontab 已安装 `cron/auto_pipeline.sh`；Ashare 当前通过本地 simulated adapter 保持不触碰 Hermes/Mac Mini/同花顺，Hermes 仅作为显式启用的第二路径。

### 2026-07-04 主动基本面分析 + 多视角研究报告

- [x] 新增 `shared/screening/fundamental_analyzer.py`：通过 SharedSignals API/DB 读取 income、balancesheet、cashflow、fina_indicator、daily_basic、industry/reference，计算 ROE、ROA、debt/equity、current ratio、gross margin trend、revenue growth YoY、FCF yield、PE/PB 5 年分位和 PEG。
- [x] 基本面报告输出 0-100 composite quality score、SW L3 行业同业比较、估值分位、数据覆盖和 red flags；同业样本不足或财报缺失时降级记录，不伪造结论。
- [x] 新增 `shared/research/multi_perspective.py`：bull、bear、macro、technical 四视角分别打分并提供 evidence，综合 weighted consensus、conviction level 和 disagreement areas。
- [x] `TradingagentDataReader.get_tushare()` 补充额外参数透传，支持 `daily_basic`/财务接口扩展读取；仍为只读 API-first，失败返回空列表。
- [x] 验证：新增/修改 Python `py_compile` 通过；`tests/test_fundamental_analyzer.py`、`tests/test_multi_perspective.py` 3 项通过；`tests/test_data_reader.py` 5 项通过。

### 2026-07-04 simulated evolution guard + 跨系统健康上报

- [x] 新增 `shared/markets/evolution_guard.py`：全风格当日亏损暂停 evolution、组合回撤 -20% 冻结权重、恢复时 thaw，连续 3 天所有市场亏损时写 `shared/review/SIM_HALT.json` 并可发送紧急告警。
- [x] `evaluate_all_markets()` 接入 guard；多市场自动演化任务在 guard 阻断时返回 `state=guard_blocked`，不生成新 variant、不调权。
- [x] 新增 `cron/health_check.sh`：检查 SharedSignals API、TradingAgent `style_comparison.json` 新鲜度、MarketGraph 数据新鲜度，并写入 SharedSignals `logs/watchdog_inputs/tradingagent_health.json` 供 watchdog 汇总。
- [x] 验证：`py_compile` 通过；`bash -n cron/health_check.sh` 通过；`tests/test_evolution.py` 3 项通过。
- [x] 生产 crontab 已安装 `cron/health_check.sh`；guard circuit breaker 已完成本地语法和既有演化回归验证，生产侧由统一健康 cron 上报。

### 2026-07-04 多市场多风格 simulated 自演化闭环

- [x] 新增 `shared/markets/performance_tracker.py`：`StylePerformance`、`style_performance.jsonl` 幂等写入、90 天历史加载、PnL 趋势回归和风格综合排序。
- [x] 新增 `shared/markets/evolution_engine.py`：按风格综合分、趋势和连续亏损天数执行 promote/demote/deprecated，并能从优胜风格生成下一代 variant。
- [x] `StyleRunner` 接入 evolved `style_weights.json`，active 风格按权重切分 simulated capital，paused/deprecated 不参与执行；每轮 style metrics 自动写绩效 JSONL。
- [x] 各市场 `styles/*.json` 补齐 `status`、`weight`、`created_at`、`last_modified`、`generation` 和 `auto_generate` 参数范围；HK 继续 paused。
- [x] `daily_review` 收盘复盘接入演化摘要，显著变化时使用 `strategy_invalidation` 模板发送 simulated 策略调整通知。
- [x] 新增 `job_style_evolution.sh` 与 crontab 模板：演化每 4 小时运行，daily review 16:00 运行；尚未声明生产 crontab 已安装。
- [x] 新增 `tests/test_evolution.py` 覆盖绩效追踪、权重执行、调权、废弃和变体生成。

### 2026-07-04 多市场多风格 simulated 比较层

- [x] 新增 `shared/markets/style_config.py` / `style_runner.py`：`TradeStyle` dataclass、JSON 风格加载、每个信号并行套用 enabled styles、输出 `style_comparison` 矩阵。
- [x] Crypto/PM/US/HK 各新增 6 个 `styles/*.json`；Crypto/PM/US 默认启用，HK 默认暂停（`enabled=false`）。
- [x] 四个市场 simulator 新增 `run_style_simulation()`，workflow 新增 `run_*_sim_cycle()`；旧兼容入口已退役为 sim-only 路径。
- [x] cron/wrapper 新增 `job_crypto_sim_exec`、`job_pm_sim_exec`、`job_us_sim_exec`、`job_hk_sim_exec`；旧兼容入口改跑 multi-style simulated，旧 cron 当前为 0 enabled。
- [x] 所有 style run 固定 `capital_layer=simulated`、`account_type=simulated`、`real_execution=false`，不触碰 `signals/real`。

### 2026-07-03 TradingAgent 邮件模板移动端决策版

- [x] 11 个邮件模板全部改为移动优先决策结构：顶部 `ACT/WAIT/IGNORE` 决策条、三张摘要卡、暗色 header、白色卡片和 HTML5 section/article/figure 语义结构。
- [x] 交易类模板补齐执行边界字段：market、capital_layer、route、signal_time、expires_at、data_fresh_at、broker_status、receipt_status；系统类模板不展示交易执行边界，避免误读为下单指令。
- [x] 日报和周报新增 3 类纯 inline SVG 图表：PnL sparkline、策略贡献横条、持仓热力图。
- [x] `system_health` / `emergency_alert` 将技术状态转为面向交易判断的自然语言，例如“交易信号管道异常，当前信号不可信”和“数据校验通过，信号质量正常”。
- [x] 验证：11 个模板 `py_compile` 通过，最小 render smoke 确认每个模板 1 个决策条和 3 张摘要卡，日报/周报各 3 个 SVG；TradingAgent 全量测试通过（214 passed，17 subtests passed）。

### 2026-07-03 R24 real signal promotion source guard

- [x] `shared/execution/signals_real.py`：旧 promotion guard 新增来源路径校验，只接受经 sim 审计的信号来源，非 sim 或缺失来源路径均 fail-closed。
- [x] 实盘 review card 保留 sim 来源路径，便于后续审计 promotion 来源。
- [x] 新增回归测试覆盖非 sim 来源拒绝，并更新合法 promotion 用例带上 sim 来源路径。
- [x] 验证：`tests/test_real_trading_gate.py tests/test_real_money_boundary.py` 17 项通过；全量 `python3 -m pytest tests/ -q --tb=line` 通过（214 passed，17 subtests passed）。

### 2026-07-03 实盘安全基础设施（Phase B）

- [x] 新增 `shared/execution/real_trading_gate.py`：`REAL_TRADING_ENABLED` 默认拒绝，显式人工 token、单笔/单日资金上限、A股交易时段、T+1 与 emergency halt 任一失败均抛 `SafetyViolation`。
- [x] 新增 `shared/execution/signals_real.py`：`RealSignalQueue` 使用 `signals/real/*` 隔离队列，promotion、manual confirm、pending submit、签名回执和持仓对账均不触碰 sim 队列。
- [x] 更新 `shared/execution/__init__.py` 导出实盘安全门和真实队列入口。
- [x] 更新 `AGENTS.md` 固化实盘安全门边界：`signals/real/pending` 不代表自动下单或已成交，回执必须带 checksum。
- [x] 验证：新增 `tests/test_real_trading_gate.py` 11 项；执行层相关回归 30 项通过；全量 `python3 -m pytest tests/ -q --tb=line` 通过（213 passed，17 subtests passed）；新增/修改 Python 文件 `py_compile` 通过。

### 2026-07-03 cron 解耦入口补齐

- [x] 新增多市场 cron 兼容入口，当前已改跑 `job_*_sim_exec`。
- [x] HK 兼容入口已改跑 sim cycle，只写 sim 输出。
- [x] 新增 `cron/daily_review.sh`：调用 `shared.review.daily_review.run_daily_review()`，按 sim 日志汇总多市场 review。
- [x] 新增 `cron/AGENTS.md`：约束 cron wrapper 不内嵌 broker 凭据、实盘 payload 或审批捷径。
- [x] 生产 crontab 已安装多市场模拟与健康检查脚本；真实资金执行保持 fail-closed，多市场生产闭环继续按真实市场时段积累模拟样本。

### SharedSignals API 15/15 端点迁移对齐（2026-07-03；2026-07-08 事件过滤补齐）

- [x] `SharedSignalsAPIClient` 已覆盖 15 个数据端点：trading day、market data、fundamentals、reference、macro、capital flow、events、sentiment、crypto、PM、associations、impacts、industry、realtime 5min、tushare。
- [x] `TradingagentDataReader` 已接入 API-first 访问核心读取路径；API 不可用时生产 fail-closed，只有显式诊断开关会读取 SQLite 并打 degraded 状态。
- [x] `get_events()` 现在向 SharedSignals `/events` 透传 `market`、`symbol` 和 `subject_code`；API 返回空壳或空候选时会再检查 SQLite 只读 `market_events` fallback，避免把候选层为空误判为正式事件证据也为空。
- [x] TradingAgent 侧不再把 15/15 客户端视为孤儿代码；MarketGraph 当前生产采集边界已切到 SharedSignals-owned collectors，研究图谱读取仍保留只读文件/DB 兼容路径。

### 多市场 P2 工具本地实现（2026-07-03）

- [x] `Crypto/risk.py`：新增 `CryptoRiskBackground`，基于 funding/news/volatility 的公开数据风险评分。
- [x] `Crypto/portfolio.py`：新增 `CryptoPortfolioOptimizer`，输出相关矩阵与波动率自适应 sim 权重。
- [x] `Crypto/replay.py`：新增 `CryptoHistoricalReplay`，用公开历史 bars 回放 sim 规则。
- [x] `US/portfolio.py` / `US/replay.py`：新增美股相关性持仓门与历史 bars 回放。
- [x] `PM/risk.py`：新增 `PMRiskControl`，执行单市场 5% 上限与相关 topic 上限。
- [x] `HK/portfolio.py`：新增 HKD lot sizing 与 sector cap 组合工具。
- [x] 所有 P2 工具保持 sim only，拒绝 real/live/direct execution 负载或配置。
- [x] 验证：新增 `tests/test_multi_market_p2_tools.py` 14 项通过；P0/P1/P2 关联测试 39 项通过；新增文件 `py_compile` 通过。

### final Codex review HIGH/MEDIUM 安全修复（2026-07-03）

- [x] `shared/markets/safety.py`：`reject_real_execution_payload()` 改为递归扫描嵌套 dict/list，并拒绝 `direct_execution=True`、`real_execution=True`、`live=True` 等真实执行别名。
- [x] `US/simulator.py`、`HK/simulator.py`：`simulate()` 对 order 和 account 同时执行真实执行拒绝；fill 结果固定 `capital_layer=simulated`、`account_type=simulated`，不再回显 account payload。
- [x] 新增回归测试覆盖嵌套真实执行负载、US/HK simulator order/account 拒绝和 account 不回显。
- [x] 验证：`python3 -m pytest tests/ -q --tb=line` 通过（188 passed，17 subtests passed）。

### 多市场 promotion tier 命名统一（2026-07-02）

- [x] `Crypto/promotion.py`、`PM/promotion.py` 晋级口径统一为 `research -> sim_candidate -> sim`，与当前多风格模拟盘口径一致。
- [x] Crypto/PM `eligible_for_sim` 与 `target_layer=simulated` 改为只在 `tier=sim` 时成立。
- [x] 更新 Crypto/PM P1 测试断言，覆盖统一五档 tier 名。
- [x] 验证：`tests/test_crypto_p1_tools.py`、`tests/test_pm_p1_tools.py`、`tests/test_us_hk_p1_tools.py` 共 17 项通过；完整 `python3 -m pytest tests/ -q --tb=line` 187 项通过。

### SharedSignals HTTP API 消费迁移（2026-07-02）

- [x] `TradingagentDataReader` 新增 `api_client` 参数；配置 `SHAREDSIGNALS_API_URL` 时自动创建 `SharedSignalsAPIClient`。
- [x] `get_market_data` / `get_events` / `is_trading_day` 优先走 SharedSignals HTTP API；API 不可用时生产 fail-closed，显式诊断读取 SQLite 时设置 `degraded=True`。
- [x] `SharedSignalsAPIClient` 移除 deprecated 状态，校准 15 个当前 API server 端点，补充 timeout / retry / backoff 配置，去除 `X-API-Key` 双重暴露。
- [x] `.env.example` 的 `SHAREDSIGNALS_API_URL` 默认指向 `http://127.0.0.1:8082`；SQLite 只保留为显式本机诊断路径，不再配置默认 DB。
- [x] 验证：`py_compile`、导入 smoke、`tests/test_data_reader.py` 通过。

### 多市场 P1 Codex review 修复（2026-07-02）

- [x] `PM/report.py`：`_outcome()` 改为显式 outcome 白名单，`cancelled`、`void`、`pending`、`unresolved` 等未知/未决状态不再计为 resolved YES。
- [x] `Crypto/validation.py`、`PM/validation.py`：OOS 日期比较前统一规整为 `YYYYMMDD`，兼容 `2026-07-02`、`20260702`、`2026-7-2`。
- [x] `US/validation.py`、`HK/validation.py`：补齐 `train_end` OOS 过滤，排除训练期及晚于 `as_of` 的记录；`_to_float()` 增加 NaN 防护。
- [x] 测试覆盖 PM 未决 outcome、Crypto/PM 日期格式规整、US/HK OOS 过滤和 NaN 防护；指定 P1 pytest 与变更文件 `py_compile` 通过。

### Crypto/PM P1 工具（2026-07-02）

- [x] `Crypto/report.py`：新增 `CryptoDailyReport(BaseReport)`，生成每日 sim 复盘并执行 no-empty-trigger 规则，未触发时返回 no-send。
- [x] `Crypto/validation.py`：新增 `CryptoForwardValidation`，计算 OOS win rate、PnL、direction hit rate 与样本质量评分。
- [x] `Crypto/promotion.py`：新增 `CryptoStrategyPromotion`，提供 sim 晋级门。
- [x] `PM/report.py`：新增 `PMDailyReport(BaseReport)`，生成每日 Brier + PnL sim 报告。
- [x] `PM/validation.py`：新增 `PMForwardValidation`，计算 OOS Brier、PnL 与校准分箱。
- [x] `PM/promotion.py`：新增 `PMStrategyPromotion`，提供 research→sim 晋级门。
- [x] 所有 P1 工具保持 sim 边界，拒绝 real/live/direct execution 配置或负载。
- [x] 新增 `tests/test_crypto_p1_tools.py` 与 `tests/test_pm_p1_tools.py`，Crypto/PM 各 4 项测试；`py_compile`、`compileall`、Crypto/PM P0 回归测试通过。

### US/HK P1 工具（2026-07-02）

- [x] `US/report.py` / `HK/report.py`：新增 Markdown 日度 sim 报告；HK 报告包含 lot size；默认只渲染不发送。
- [x] `US/validation.py` / `HK/validation.py`：新增 OOS 前向验证；US 覆盖 earnings/momentum funnel，HK 使用 HKD 口径。
- [x] `US/promotion.py` / `HK/promotion.py`：新增 `research -> sim_candidate -> sim` 策略晋级分类。
- [x] 所有 P1 工具维持 sim 边界，拒绝 real/live/direct execution 配置或负载。
- [x] 新增 `tests/test_us_hk_p1_tools.py`，US/HK 各 3 项测试；`py_compile`、US/HK P0 回归和 diff 检查通过。

### R8/R9 多市场安全修复（2026-07-02）

- [x] `PM/config.yaml` currency 从 USD 修正为 USDC，并补充 `PMWorkflow()` 读取 checked-in config 的回归测试。
- [x] PM 旧 runner 已纳入 sim-only 状态机写入路径。
- [x] `shared/markets/safety.py`、`base_tools.py`、`config_schema.py` 增加真实执行/live broker/direct execution 拒绝；`execute_sim_order()` 不再把真实负载静默改写成 simulated 后派发。
- [x] `PM/simulator.py` 拒绝 real order，fill 结果固定返回 `capital_layer=simulated`、`account_type=simulated`。
- [x] `PM/Crypto/US` sim executor 入口新增真实执行负载拒绝；修复 `US/sim_executor.py` 删除 account 后再访问的 `UnboundLocalError`。
- [x] `HK/workflow.py` 补齐 `HKWorkflow` / sim cycle，对齐 US workflow 模式。
- [x] 新增/更新测试覆盖 PM config load、HK workflow smoke、US/HK live broker rejection、sim executor safety。

### Mini/服务器执行桥路径修复（2026-07-02）

- [x] 禁用旧 `ai.hermes.sim-remote-sync`，停止 `~/Desktop/Investment/Ashare/outputs/account` 被周期性重建。
- [x] 禁用旧 `ai.hermes.condition-cleanup`，停止访问已退役的 `~/Desktop/Investment` tradebook 清理路径。
- [x] Mac Mini executor 明确设置 `SIM_REMOTE_TRADINGS_SIGNAL_DIR=/opt/investment/tradingagent/signals`。
- [x] 服务器合并 GitHub 最新 main，修复 `TradingagentDataReader` 导入/导出和 reader 回归；`tests/test_data_reader.py` 通过。
- [x] `shared/execution/execution_router.py` 已提交：A 股 sim broker 正式通过仓库内 `Ashare/sim_executor.py`，不再依赖旧 `/opt/investment/Ashare/tools`。
- [x] Mac Mini live executor / receiver / health-check 默认路径已改到 `~/.hermes/ashare-runtime` 与 `/opt/investment/tradingagent/signals`。
- [x] 清理 `mini/README.md`、`mini/mini_consumer.py`、`Ashare/AGENTS.md` 等参考副本里的误导性旧执行器路径。
- [x] 归档 Mac Mini 非活跃旧脚本/备份/禁用 LaunchAgent；active scripts、LaunchAgents、crontab 旧路径审计为 0 命中。
- [x] 记录事件日志：[docs/runtime_incidents_20260702.md](docs/runtime_incidents_20260702.md)。

**残余风险：**
- 未在交易时段发送测试交易信号；完整端到端验证需等交易时段。

### Goal 2 审计 — SharedSignals → TradingAgent → MarketGraph 数据流

**2 轮审计，10 维度，46 发现，10 项修复全部完成。**

**Round 1（5 agents，17 发现）：**
- API 客户端：`is_trading_day()` 默认返回 False（fail-safe）、API sentinel→TTL 恢复、4xx/5xx 重试分化
- 健康检查：sockstat 端口检测替代 HTTP health check（30s SIGALRM 超时）
- 配置一致性：端口 8082/8900 不一致修复、MarketGraph `.env.example` port 更新

**Round 2（5 agents，29 发现，5 维度）：**
- 数据新鲜度：LRU cache 无 TTL、naive `datetime.now()`（20+ 处）、无每日管线 fallback cron
- 错误传播：dead `api` property、`errors`/`stale` 从未消费、SQLite 错误静默吞掉、无死人手刹
- 历史配置漂移：SharedSignals root 指向错误、端口 8900/8082 不一致、MarketGraph env 文件路径冲突、15 个未文档化环境变量；当前规范以 `SHAREDSIGNALS_API_URL` 和 `MARKETGRAPH_API_URL` 为准。
- MarketGraph 直接读取器：从未使用 HTTP API、reference/ 下断 symlink、直接导入无鉴权
- 密钥暴露：`api_tokens.json` 在 git 中追踪、无盐 SHA256、X-API-Key 双重暴露、`.env.*` 不在 gitignore

**已应用修复（10 项）：**
1. [x] `SharedSignals/.gitignore`：添加 `config/api_tokens.json` + `.env.*`
2. [x] `.env.example`：移除 `SHAREDSIGNALS_ROOT`/默认 SQLite 路径，生产默认 API-only
3. [x] `.env.example`：历史 MarketGraph env 路径冲突已清理；新增配置使用 `MARKETGRAPH_API_URL`
4. [x] `SharedSignals/tools/api_server.py`：端口默认值 8900 → 8082（docstring + env.get）
5. [x] `shared_signals_api.py`：移除 X-API-Key 双重暴露（服务器仅检查 Authorization）
6. [x] `reader.py`（TradingagentDataReader）：移除 dead `api` property + 未使用的 `import time`
7. [x] `SharedSignals/auth.py`：添加 salt token hashing（PBKDF2-HMAC-SHA256，100k 迭代，向后兼容）
8. [x] `SharedSignals/reader.py`：LRU cache 失效 — 14 个缓存函数已注册，TTL（默认 5 分钟）+ 文件 mtime 自动检测，`clear_caches()` + `/cache/invalidate` + `/cache/status` 端点

### Goal 2 审计 Round 3（高强度终检 — TradingAgent 侧）

**TradingAgent 历史发现（CRITICAL/HIGH，当前状态见上方 2026-07-04/07-05 条目）：**
- **MarketGraphCSVReader 路径错误：** `intake` 路径缺少 `data/` 目录，`get_regime()` 路径错误 — 导致体制信号、事件候选、情绪信号三个关键 CSV 静默加载失败（已修复）
- **SharedSignalsAPIClient 孤儿代码：** 已修复；`TradingagentDataReader` 默认 API-first，SQLite 只保留显式只读诊断
- **TradingagentDataReader 无数据新鲜度检查：** 已补健康检查、错误告警和市场 loop 巡检
- **N+1 查询扇出：** 评分管线对每只股票做 5-6 次独立查询，20 只股票 > 100 次调用，无批量接口
- **直接 SQLite 读取绕过了 API 鉴权：** 已修复为 SharedSignals API-first，SQLite 仅为显式测试/应急诊断路径
- **无死人手刹：** 已补连续错误日志告警

**已应用修复（Round 3，影响 TradingAgent）：**
9. [x] `tradingagent/shared/data/reader.py`：MarketGraphCSVReader `intake` 路径从 `self.root / "intake"` → `self.root / "data" / "intake"`，`get_regime()` 路径从 `self.root / "all_weather_regime.csv"` → `self.root / "data" / "all_weather_regime.csv"`

### Goal 2 审计 Round 4（终检 — TradingAgent 侧）

**TradingAgent 相关发现（CRITICAL/HIGH）：**
- **SharedSignalsAPIClient 孤儿代码：** 214 行 HTTP 客户端从未被生产代码导入使用（已加 DeprecationWarning）
- **TradingagentDataReader 无死人手刹：** `errors` 列表无限增长但从不消费，`stale` 标志从未检查 — 已修复：添加 `_maybe_alert()` 每 10 条错误 WARNING
- **SharedSignalsReader 无 SQLite busy_timeout：** 连接无超时，写锁期间读立即失败 — 已修复：添加 `busy_timeout = 5000`
- **静默回退到 `/dev/null/does_not_exist.sqlite`：** 初始化失败时所有查询返回空，零告警 — 已修复：`_maybe_alert()` 在回退激活时日志记录
- **重复的交易日历实现：** `t_plus_1.py` 和 `position_schema.py` 各自独立实现 is_trading_day，行为不一致
- **try/except 将类设为 None 反模式：** `daily_review.py` 和 `benchmark.py` 将导入失败转换为 None，隐藏真实错误
- **N+1 查询扇出：** 评分管线每只股票 5-6 次独立查询，无批量接口
- **数据类型不一致：** CSV 路径返回字符串，SQLite 路径返回正确 Python 类型

**已应用修复（Round 4，影响 TradingAgent）：**
10. [x] `reader.py`：SharedSignalsReader 连接添加 `PRAGMA busy_timeout = 5000`
11. [x] `reader.py`：TradingagentDataReader 添加 `_maybe_alert()` — errors 每 10 条 WARNING，所有 9 个 error.append 点均已接线
12. [x] `shared_signals_api.py`：添加 DeprecationWarning 模块级警告
13. [x] `SharedSignals/reader.py`：`get_market_data()` 查询添加 `market` 过滤（从 ts_code 后缀推导）
14. [x] `SharedSignals/collectors/rss/`：event_hash 从 64 位升级到 128 位（collector.py + gap_filler.py）
15. [x] `SharedSignals/api_server.py`：恢复 `log_message()` HTTP 请求日志 + 500 错误日志

### 2026-07-02 Goal 2 审计 Round 5（五维度最终审计 — 58 发现，23 修复）

**5 新维度并行审计。TradingAgent 相关发现和修复：**

**TradingAgent 相关发现（CRITICAL/HIGH）：**
- **Universe collapse（CRITICAL）：** `adapter.py:_exclude_asset()` 将 `None`（DB 错误）等价于"低流动性" — 所有股票被排除，TradingAgent 生成零交易
- **SQLite 读写模式（HIGH）：** `reader.py` 以 rw 模式打开 SharedSignals 只读模型 DB — 损坏风险，空 DB 静默创建
- **`/dev/null` fallback（HIGH）：** `SharedSignalsReader` 初始化失败静默回退到 `/dev/null/does_not_exist.sqlite` — 完全数据丢失零告警
- **DatabaseError 未捕获（HIGH）：** `_query()` 只捕获 `sqlite3.OperationalError` — DB 损坏绕过防御
- **硬编码 secrets（HIGH）：** `webhook_sender.py` 中 WEBHOOK_SECRET 和 WEBHOOK_URL 为硬编码字面量
- **env 自动加载在 import-time（MEDIUM）：** 多个模块在 import 时 mutate `os.environ`

**已应用修复（Round 5，5 TradingAgent 相关项）：**
1. [x] `Ashare/adapter.py`：`_exclude_asset()` — `amount is None` 返回 False（保留），记录 WARNING
2. [x] `shared/data/reader.py`：SQLite 连接从 rw 改为 `mode=ro` URI
3. [x] `shared/data/reader.py`：`DatabaseError` 和 `OperationalError` 同时捕获
4. [x] `shared/data/reader.py`：`/dev/null` fallback → RuntimeError（fail-fast）
5. [x] `shared/execution/webhook_sender.py`：Hardcoded secrets → env vars

### Goal 1 退役清理

- [x] Ashare 依赖迁移：`execution_router.py` sim_broker 通道从旧 `/opt/investment/Ashare/tools/a_share_simulated_trade_executor` 迁移到 `tradingagent/Ashare/sim_executor.py`
- [x] Tushare API 包装器迁移：旧 A股直连包装器已迁出 TradingAgent，生产数据入口改为 SharedSignals API；服务器历史兼容 symlink 不再作为现役开发入口
- [x] Ashare/tools 全面退役：142 文件归档至 `_archive/Ashare_tools_20260702/`，目录仅剩 3 个 compat symlink
- [x] `Ashare/AGENTS.md` 添加迁移注释
- [x] 旧系统残留清理：删除 61 个死 symlink（PM 20 + Crypto 21 + US 20）
- [x] 代码层 Tradings/KimiWork 引用全部修复（0 残留）
- [x] 服务器 crontab 37 条旧注释清理
- [x] 所有修改提交并 push，服务器同步确认
- [x] `sim_broker.py` L8 + `slippage_model.py` L8 注释引用路径更新至 `_archive/Ashare_tools_20260702/`

## 六、7/1 事故复盘（已完成）

以下事项已修复并固化为 [AGENTS.md](AGENTS.md) 中的永久规则：

- 虚假成交确认 → Mini/Hermes 健康门 + 未确认回执 halt
- 过期 pending 清理 → job_self_heal
- 回执指纹闭环 → receipt_sha256 验证
- sim 信号过滤 → 200xxx.SZ 等非普通 A 股代码三层过滤

详细时间线：[docs/runtime_incidents_20260701.md](docs/runtime_incidents_20260701.md)

## 六、关联系统状态

- [SharedSignals STATUS](../SharedSignals/STATUS.md) — 数据采集与存储状态
- [MarketGraph STATUS](../MarketGraph/STATUS.md) — 研究图谱与因果状态
- [Finance STATUS](../STATUS.md) — 根工作区总览
