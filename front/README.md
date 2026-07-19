# TradingAgent Front

TradingAgent 的前端看板层。它是自动模拟/影子交易与研究系统的只读观测台，只展示自动运行
过程与结果：收益、过程、持仓、风险和复盘。

本目录不是交易执行系统，也不是账户控制台。任何 agent 接手这里时，默认只做
展示、读取和可视化，不得触发交易动作。

## 一句话定位

`front/` 是 TradingAgent 的用户界面和只读快照 API 包装层：

```text
浏览器页面
  ↓
TradingAgent Front
  ↓ 只读
/api/trading-agent/snapshot
  ↓ 只读
TradingAgent signals / positions / review / risk
```

## 展示目标

首页优先展示三件事：

- 收益结果：模拟盘当前收益、收益率、目标差、回撤和收益曲线按所选市场展示。A股与 CNFutures 分别是独立 fresh-start 50,000 CNY authority；A股 gross 上限 45,000 CNY、单票 7,500 CNY，期货保证金上限 25,000 CNY。`All Markets` 只汇总信号/持仓/健康等非货币计数，资本、权益、PnL、收益率、回撤和利用率必须显示 `—` 或提示选择市场，禁止生成跨市场组合资本、净额风险或风格 shadow 资本。
- 自动化过程：展示 `发现 → 研究 → 风控 → 模拟执行 → 结果写回`。优先使用真实 `funnelEvents[]`；没有运行中过程但有持仓时切换为持仓跟踪，不伪造过程吞吐。
- 运行与结果：右轨展示当前自动任务、阶段、状态、证据和更新时间；底部明细分为运行中、持仓、已完成和自动复盘。

## 桌面只读工作台

2026-07-11 起，主页采用 Hyperliquid 启发的连续桌面工作台，而不是等权卡片堆叠：

- 顶部市场条集中展示所选市场、快照新鲜度、账户模式、当前收益、目标差、运行中、已完成、持仓和最大回撤。
- `WorkbenchViewModel` 负责所选市场和组合口径；`AutomationObservatoryViewModel` 负责运行中、已完成、自动复盘和右轨优先级。标题收益与收益曲线末点必须使用同一视图。
- 主工作区由收益图、自动化状态右轨、压缩过程带和底部 blotter 组成。blotter 分为 `运行中 / 持仓 / 已完成 / 自动复盘`；终态记录不得回退到运行中。
- 选择 `实盘` 时进入覆盖整个工作区的 `实盘待接入` 门禁；切换顶部页面也不会绕过门禁。页面只说明账户授权、风险校验和成交回执要求，不展示真实资金或任何买卖、下单、撤单、确认交易控件。
- `partial` 队列记录属于已完成/自动复盘结果，并以“部分成交”展示，不进入运行中，也不误写为风险保护。
- 顶部新鲜度和收益曲线底部时间使用同一快照时间；`performance` 为 `stale` 时明确显示“快照滞后 / 数据滞后”，不使用本机当前时间伪装更新。
- 市场头下方增加连续市场状态带，按市场显示收益、持仓、运行态和时间；`All Markets` 的收益保持 `—`，只汇总非货币运行信息。右侧证据健康区同步展示收益、信号、持仓、复盘和风险五域，不把空数据包装成正常。
- 收益归因缺少明确非零贡献时显示真实空状态；后台 runtime code 统一翻译为用户文案，不原样展示下划线错误码。
- 运行中/已完成/自动复盘由统一状态解析器决定：只有 `pending` 是运行中，`executed`/`partial` 是完成，`blocked`/`missed`/`cancelled` 进入复盘；异步快照变化时自动切到可用结果页签，但保留用户主动查看空页签的能力。
- 顶部导航、市场头、二级页指标和检查器共用同一运行心跳：明确区分“自动过程运行中”“调度正常、当前空闲”“快照滞后”和“证据读取异常”，不再用固定“自动化运行中”覆盖真实空闲状态。
- 收益页按证据自动切换密度：有真实波动时使用完整 520px 图表；只有零值或单点时压缩为 300px 图表并显示样本、最近变化、已实现和未实现证据。
- 持仓为空时不保留大空表，改为显示当前敞口、可用资金、最近关闭结果和快照时间；没有来源的字段保持 `—`。
- 过程页优先按 `opportunityId` 聚合真实事件为机会周期，明确展示五阶段完成/缺失情况；原始事件账本保留在下方供审计。内部 `buy/sell/empty` 和来源代码不会直接展示给用户。

本阶段只验收 1280×720 与 1440×900 桌面视口。移动端导航、触控和手机布局明确延期；快照 API 可选增加 `marketPulses[]` 与 `marketPulseCoverage`，现有字段保持向后兼容。

六个页面统一使用连续终端语言：二级页面由紧凑指标条、主数据面、316px 只读检查器和可选底部账本构成；不再使用大号摘要卡或浮动 SaaS 卡片。内容边界如下：

- 总览：收益结果、自动化状态、五阶段过程、运行/持仓/完成/复盘明细和市场证据。
- 收益：收益曲线、目标差、回撤和收益贡献；支持今日/7日/30日/全部切换。
- 过程：有显式事件时先展示按机会聚合的决策因果链；没有显式事件时回退 Process Book。下方事件账本按时间/序列展示阶段、事件、来源、延迟和原因，没有时间或延迟时保持 `—`。
- 持仓：Portfolio Ledger 在真实字段存在时展示数量、均价、现价、成本、市值、当日/累计盈亏、组合权重和来源；多币种不做虚假汇总。
- 风险：回撤图明确 5% 预警与 7% 限制，检查器展示市场敞口，Risk Ledger 同时展示安全拦截和滞后/异常/隔离的数据域。
- 复盘：关闭过程、置信度、实际影响、证据、归因线索和只读的“自动校准”。

过程、事件、持仓和风险账本统一提供本地搜索、字段排序和列显隐。URL 保存 `page`、`market`、`range`；`Alt+1…6` 切换六页，`Alt+←/→` 切换市场，`/` 聚焦当前账本搜索。以上均为浏览器展示状态，不写服务器。

用户不需要在前端做交易判断；页面不展示“下一步”“还差什么”或人工确认型操作，也不提供队列写入、下单、撤单或资金控制。

## 当前实现

- UI：React + TypeScript + Vite。
- 图表：Recharts，生产构建中拆成独立 `charts` chunk。
- 本地开发：Vite dev server 会挂载只读快照 API。
- 生产 API：`src/server/tradingAgentSnapshotHttp.ts` 可独立启动 Node 只读服务。
- 读取入口：`GET /api/trading-agent/snapshot`。
- 浏览器客户端：`src/api/tradingAgentIntegration.ts`。
- TradingAgent 读取器：`src/server/tradingAgentSnapshot.ts`。
- 今日自动模拟盘状态：只读 snapshot 可选读取
  `shared/runtime/run_bundles/latest.json`及其内容寻址immutable mirror，归一化为
  `paperDayRun`。reader会重算component/payload/bundle hash、run ID和幂等绑定，不信任
  文件内自报摘要。后端已有本地
  `FileRunBundleStore`、`LocalRunBundlePublisher` 和离线 fixture CLI 候选；CLI
  只会在显式隔离output root的`shared/runtime_test/phase1_paper_fixture/`下发布，故意不写
  活动Today路径。它没有 scheduler、真实 SS
  或市场会话运行证据，也不会自动发布到活动项目/生产根；因此文件缺失时
  界面明确显示“不可用”，不把仓库能力、测试 fixture 或 HTTP 200 冒充为今天已经运行。
- SharedSignals 市场脉冲读取器：`src/server/sharedSignalsMarketPulse.ts`。本地候选已只调用
  provider-neutral `GET /v1/catalog` + `POST /v1/query`，并要求显式配置 base URL、
  catalog version、access policy 和逐市场 dataset ID。catalog/dataset identity、响应 envelope、
  freshness/quality/lineage/receipt 或 `as_of` 任一不合格时按数据集 fail closed；返回行还须
  显式匹配目标实体，且 row time 不得晚于 `data_through` 或本次决策时间。不会回退
  provider、兄弟仓 SQLite、旧专用端点或本地拼装。该能力仍只是本地候选，SS upstream
  合同与生产 runtime 尚未冻结，不能描述为 live 可用，也不赋予 paper-day 执行权限。
- 非 A 股代表行情必须由上游显式提供 `market_data_symbol` 或 `marketDataSymbol`；前端不从展示代码转换 Crypto、期货、PM、US 或 HK API 参数。`marketPulseCoverageHistory` 只保留当前服务进程最近 12 次真实来源读取，缓存命中与服务重启不会伪造连续观测。
- 真实数据适配：`src/api/tradingAgentReadModel.ts` 和
  `src/adapters/tradingAgentReadModel.ts`。
- 本地演示数据：`src/data/dashboard.ts`，只在本地开发或显式开启 `VITE_TRADING_AGENT_DEMO_PREVIEW=1` 时用于开发预览；生产接口不可用时必须展示等待/不可用状态，不得回退到样例收益、机会或持仓。如果接口可用但某个领域返回空数组，前端必须展示真实空状态。

## 数据边界

可以读取：

- `../signals/{pending,claimed,running,filled,cancelled,expired,failed,partial}/*.json`（兼容只读投影；未经V1 authority/freshness证明的A股pending行不得进入current/live）
- `../signals/positions/*.json`
- `../shared/accounting/position_plan.jsonl`
- `../shared/review/daily/daily_brief.jsonl`
- `../shared/review/ashare/research_evidence_latest.json`
- `../shared/review/ashare/projection_current.json` 指向的 `projection_generations/<generation_id>/{sample_kpi_latest,evolution_decision_latest,market_maturity_latest}.json`；reader 先校验 manifest content SHA、三个 projection SHA、共同 input SHA、authority 与显式 sim-only 安全字段，再从 input SHA + canonical projection SHA map 重算 generation ID 并要求 pointer/directory/manifest 全等；generation 存在但 pointer 缺失/非法时保持 unavailable，不回退根目录 mirrors
- `../shared/review/cn_futures/market_maturity_latest.json`
- `../shared/logs/capital/{ashare,cn_futures}/*_capital_latest.json`
- `../shared/review/attribution/*.jsonl`
- `../shared/risk/risk_limits.yaml`
- 可选 V1 市场脉冲只读面：同时显式提供 `SHAREDSIGNALS_API_URL`、
  `SHAREDSIGNALS_CATALOG_VERSION`、`SHAREDSIGNALS_ACCESS_POLICY_ID` 和
  `SHAREDSIGNALS_MARKET_PULSE_DATASET_IDS_JSON` 后，前端才会读取 `GET /v1/catalog` 与
  `POST /v1/query`。dataset mapping 缺失、上游未冻结或 envelope 不合格时
  `marketPulses[]` 为空并保留 `marketPulseCoverage` 的 unavailable/degraded 状态；绝不调用旧端点。
- `shared/runtime/run_bundles/latest.json`（可选本地候选快照）：只展示
  RunBundle 阶段、数据证据、候选/决策、模拟订单/成交、风险阻断、Champion
  清单和 LLM 证据角色。必须同时存在字节一致的
  `shared/runtime/run_bundles/runs/<run_id>/<bundle_sha256>.json`；仅接受
  `account_type=simulated` 且`real_trading_enabled=false`，并重验完整manifest和hash链。
  缺失、畸形、单文件自报或不安全内容不回退到样例数据。

首页“成熟度与资金”面板并列显示 A股独立 5 万模拟账户的 Day 5 / Day 10 证据复核，以及 CNFutures 独立 5 万模拟账户的长期模拟成熟度；两者不相加、不互相补资。投影缺失或 hash/authority 无效时保持“等待证据”，并始终显示自动晋级关闭。

不得执行：

- 写入或移动 `signals/` 队列文件。
- claim / cancel / expire / fill 任何 signal。
- 调用执行器、下单路由、邮件发送、webhook、账户回调。
- 读取或暴露账号凭据、2FA、私钥、资金权限。
- 把不同账户层的收益混成一个数字。

## 部署形态与验证边界

当前产品只供 Nicholas 个人内部使用。`tradingagent.cc`保留为个人远程入口，但必须由Cloudflare Access或等价单用户认证保护；只读API仍只监听服务器localhost，不允许匿名访问或API直出：

```text
本机浏览器或经过单用户认证的 tradingagent.cc
  -> Cloudflare Access / authenticated edge
    -> internal Nginx / static server
    /                           -> front/dist
    /api/trading-agent/snapshot -> 127.0.0.1:8787
```

下列服务器参数来自历史部署说明，本轮重构未做生产核验；是否仍可用必须在另行获准部署前按 [STATUS.md](../STATUS.md) 现场验证：

```text
8.138.181.177
  Nginx
    /                         -> front/dist
    /api/trading-agent/snapshot -> 127.0.0.1:8787

  tradingagent-front-api.service
    bind 127.0.0.1:8787
    read /opt/investment/tradingagent

  Node runtime
    /opt/investment/tools/node-v24.4.1/bin/node
```

推荐生产源码路径：

```text
/opt/investment/tradingagent/front
```

内部前端默认使用同源接口：

```bash
VITE_TRADING_AGENT_SNAPSHOT_URL=/api/trading-agent/snapshot
```

详细的内部服务和认证域名部署示例见 [docs/integration.md](docs/integration.md)。[docs/cloudflare.md](docs/cloudflare.md) 保留Cloudflare迁移与回滚记录；其中未配置Access policy的匿名形态不得恢复。重新启用域名、Tunnel或Pages前，必须先配置单用户认证、最小暴露面和可验证撤销路径。

API 始终只读，不暴露交易执行、队列写入、账户、回调或密钥。服务启动时拒绝`0.0.0.0`等非loopback监听和`*` CORS；即使只在私网运行，也不能降低这条边界。

## 本地运行

```bash
npm ci
npm run dev
```

本地页面：

```text
http://127.0.0.1:5173/
```

## 验证

修改本目录后至少运行：

```bash
npm run lint
npm test -- --run
npm run build
npm run build:api
```

## 当前缺口

- 模拟盘持仓和已成交信号已接入 `shared/logs/sim_ledger/*/*/{positions.json,trade_journal.jsonl}`。
- 收益曲线现在优先读取显式权益快照：`shared/review/{portfolio,daily,*}/{equity_snapshots,equity_series}.jsonl`
  或 `shared/logs/sim_ledger/*/*/{daily_mark_to_market,equity_snapshots}.jsonl`。如果后端尚未写入权益快照，snapshot 才回退到 `shared/review/daily/daily_brief.jsonl` 的明确 return 字段，再回退到 `shared/review/*/style_performance.jsonl` 的真实 simulated PnL，并用模拟账本本金换算为收益率；当同市场/同策略/同日期存在模拟账本成交时间戳时，snapshot 会把日级 PnL 展开成交易时间线曲线。若只存在成交日志或持仓成本，snapshot 会保持收益为空并给出缺口说明，前端不得用成交额或成本冒充收益。
- `style_performance.jsonl` 作为回退收益来源时，US/Crypto/PM 的 money fields 可按其显式币种/汇率归一化到各自 `marketSummaries[]`；不得跨市场相加生成全市场收益曲线，也不得用一个市场的本金归一化另一个市场的 PnL。
- 维护、回补、烟测或修复重跑样本不得进入用户收益和交易量口径。只读 snapshot 会跳过带 `exclude_from_dashboard=true`、`dashboard_excluded=true`、`excluded_from_dashboard=true`，或 `run_context/run_mode/run_source/sample_type` 包含 `maintenance/backfill/smoke/repair/bootstrap/dry-run` 的模拟账本、权益快照、风格绩效和风格对比记录。
- A股研究证据卡片读取 `shared/review/ashare/research_evidence_latest.json`，只展示集合竞价/09:30 代理、尾盘候选、204001 现金管理建议和旧四风格的legacy SampleJournal归因计数；它们不是当前三风格shadow router，也没有可相加的虚拟本金。该卡片不写队列、不触发交易、不发送邮件。
- A股服务器本地模拟账本先验证
  `shared/logs/capital/ashare/ashare_sim_capital_latest.json`，再使用其中受验证的
  `authority_generation` 与单段安全 `execution_lineage_id` 定位
  `shared/logs/execution_lineages/<execution_lineage_id>/{simulated_ashare_positions.json,local_sim_pnl.json,local_sim_trades.jsonl}`。
  持仓回执缺失（资本声明非零持仓时）、损坏、身份冲突或数量不一致时显示“持仓权威不可用”，
  capital `updated_at`和position `synced_at`还必须带时区、在36小时内且不晚于读取时点；
  页面生成时间不能冒充证据时间。不读取固定日期 lineage，也不把 `position_plan.jsonl` 当 SQLite 重开。冻结的
  `shared/logs/local_sim/` 和旧账本不得作为当前读取回退。
- 后端已预留并提供权益快照生成入口：`shared/runtime_test/write_equity_snapshots.py`。生产运行时应由服务器定时或手动调用该入口，把模拟账本的已实现收益、未实现收益、本金、回撤、持仓数和价格缺失状态写入 `daily_mark_to_market.jsonl`，供首页收益主面板优先读取。
- `shared/review/opportunities/funnel_events.jsonl`与`shared/logs/opportunities/funnel_events.jsonl`是已退役writer留下的可选冻结法证历史，不是当前OpportunityLedger。读模型把它们统一标记为`legacy_frozen_opportunity_log`，不得据此把signals/risk置为ready、驱动实时心跳或关联当前持仓/PnL；未来替代只能是经过独立合同验收的OpportunityLedger只读投影。
- signal queue仍是兼容只读投影，sim ledger只能形成completed replay。未经V1 authority/freshness证明的A股pending行必须排除；合成的`发现 / 研判 / 风控 / 待确认 / 结果`只是队列状态投影，不能声称显式真实事件。纯模拟账本成交只展示为历史结果。只有持仓时切换为持仓监控板；没有current事件、信号或持仓时保持轻量等待态，不用静态样例或占位粒子伪装真实筛选。
- 首页收益口径必须区分“真实 0 收益”和“收益尚未写入”：只有成交、非零收益、连续收益点或 A 股账户事实存在时，才把数字展示为收益结果；空账本或单个零值快照显示为等待收益。
- 首页右轨只展示最高优先级的自动运行状态，不再重复收益卡已有的账户、收益和风险数字；收益页曲线只展示走势、事件和区间切换，当前收益/目标差/回撤由页面摘要板承载。
- 每个市场的收益曲线只使用该市场自己的 authority/equity snapshots；多个独立市场同时存在时，`All Markets` 不绘制货币收益曲线。
- 午盘复盘、策略归因和风险限额文件已列为可用来源，但仍需补充到 snapshot 构建后才能作为完整面板展示。
- 实盘只保留未来接入口；未验证账户授权前，前端不得展示为已接入。
- 首页顶部和收益主面板必须使用同一“所选市场”口径：A股看 A股独立模拟账户，CNFutures 看期货独立摘要，其它市场看自身摘要；`All Markets` 不显示货币 portfolio。不要再次拆成“模拟盘收益”和“现在收益”两个数字。
- 首页右轨只展示过程、阶段、状态、证据、更新时间和简短结果说明，不展示人工建议、内部错误码或调试文案。
- 市场状态带会从当前持仓或信号中为每个市场选择一个代表标的，并由只读 snapshot API
  通过显式 V1 配置查询 catalog/query。只展示 identity 与 metadata 全部验证通过的真实返回；
  无代表标的、dataset 未配置、读取超时、HTTP 200 但 dataset stale/degraded/failed、
  receipt/lineage 缺失或字段不合格时保持`—`/“暂无代表行情”，不得生成样例价格或回退旧端点。
  `marketPulseCoverage`明确展示已取到、待映射、不可用和降级范围。请求限制为每个代表标的
  最多24个点、900ms超时和15秒进程缓存；该只读脉冲不进入 paper-day 决策/执行链。
- 过程页选择机会周期后，URL 增加 `opportunity=<opportunityId>`，周期行进入选中态，原始事件账本只展示该机会的显式事件；关联条在其它页面继续显示标的、阶段、结果、完整度、关联信号/持仓与可归因盈亏。后两项只接受相同的显式 `opportunityId`，无匹配持仓时保持 `—`。清除关联只改变浏览器展示状态。
- A 股 server-local 模拟持仓只在同一聚合仓位的所有记录买入来源均为同一 `order_id` 时透传该 ID 与未实现 PnL，供只读机会关联使用；多来源仓位不分摊、不归因，历史成交和签名回执不回写。
- `Cmd/Ctrl+K` 打开桌面终端命令面板，可切换页面、市场和信息密度、清除关联机会。密度与各账本列显示写入版本化浏览器本地偏好，不写服务器、不修改 snapshot。
- 首页“今日自动模拟盘”面板始终标记“本地候选 · 非生产”。它没有按钮，
  不产生订单、邮件、回调或队列写入；LLM 只能显示为“仅作证据”或“证据未提供”。
  本地发布器候选与前端读取器的相对路径已经一致，但这不等于活动项目根已有受控发布。
  在没有真实当日 runtime readback 前，正常状态仍是 honest unavailable。

## 用户可见文案规范

- 用户界面优先使用结果与过程语言：当前收益、运行中、结果写回、持仓贡献、风险边界、资金状态、市场状态、自动复盘。
- 不在界面直接展示后台术语：运行证据、闭环、接口预留、有效样本、隔离样本、账户事实、策略资金、收益口径、代理样本、漏斗留存、流失。
- A股样本质量、成交价来源、候选层来源和链路验证仍可写在开发文档或 tooltip 的极短说明中，但主标签必须翻译成用户能理解的表达，例如“可复盘”“不计入复盘”“账户现金”“资金分配”。
- 实盘未验证前只展示“实盘待接入”，不得用同等权重文案暗示实盘已经可用。
- 首页右轨不得重复收益卡里的金额、收益率、目标差和回撤数字；它只说明自动系统正在运行什么、到了哪个阶段、证据和状态如何。
