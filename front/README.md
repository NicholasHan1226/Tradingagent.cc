# TradingAgent Front

TradingAgent 的前端看板层。它是全自动交易系统的只读观测台，只展示自动运行
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

- 收益结果：模拟盘当前收益、收益率、目标差、回撤和收益曲线放在同一主面板。全市场默认按 `marketSummaries[]` 聚合各市场资金和盈亏；切到单一市场时，只展示该市场口径。A股/CNFutures 当前按 50,000 CNY 展示，A股旧 200,000 元 epoch 1 账本不进入当前汇总；US/Crypto/PM 默认按 10,000 USD/USDT/USDC 原币运行并折算成人民币汇总，历史账本本金不得覆盖各市场规范本金。
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
- 市场头下方增加连续市场状态带，集中显示全市场/A股/美股/加密/预测/中国期货的收益、持仓、运行态和时间；右侧证据健康区同步展示收益、信号、持仓、复盘和风险五域，不把空数据包装成正常。
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
- SharedSignals 市场脉冲读取器：`src/server/sharedSignalsMarketPulse.ts`。它只从 `SHAREDSIGNALS_API_URL` 读取持仓/信号涉及的有限代表标的，单请求 900ms 超时并使用 15 秒进程内缓存；`marketPulseCoverage` 会说明每个市场是已取到、无代表映射、不可用还是降级。它不直接调用 provider，不写 SharedSignals，也不让上游不可用拖垮快照。
- 真实数据适配：`src/api/tradingAgentReadModel.ts` 和
  `src/adapters/tradingAgentReadModel.ts`。
- 本地演示数据：`src/data/dashboard.ts`，只在本地开发或显式开启 `VITE_TRADING_AGENT_DEMO_PREVIEW=1` 时用于开发预览；生产接口不可用时必须展示等待/不可用状态，不得回退到样例收益、机会或持仓。如果接口可用但某个领域返回空数组，前端必须展示真实空状态。

## 数据边界

可以读取：

- `../signals/{pending,claimed,running,filled,cancelled,expired,failed,partial}/*.json`
- `../signals/positions/*.json`
- `../shared/accounting/position_plan.jsonl`
- `../shared/review/daily/daily_brief.jsonl`
- `../shared/review/ashare/research_evidence_latest.json`
- `../shared/review/attribution/*.jsonl`
- `../shared/risk/risk_limits.yaml`
- `SHAREDSIGNALS_API_URL` 暴露的只读 HTTP read model，用于可选 `marketPulses[]` 与 `marketPulseCoverage`。A股/CNFutures 使用 5 分钟接口，US/HK 使用日线接口，Crypto 使用 `/crypto`；PM 只采用明确的 canonical YES outcome，无法识别 outcome 时保持空值。

不得执行：

- 写入或移动 `signals/` 队列文件。
- claim / cancel / expire / fill 任何 signal。
- 调用执行器、下单路由、邮件发送、webhook、账户回调。
- 读取或暴露账号凭据、2FA、私钥、资金权限。
- 把不同账户层的收益混成一个数字。

## 生产形态

当前用户访问入口已经切到 Cloudflare Tunnel + 生产服务器 Nginx：

```text
dashboard.tradingagent.cc
  Cloudflare Tunnel
    -> Nginx on TradingAgent server
      /                         -> front/dist
      /api/trading-agent/snapshot -> 127.0.0.1:8787
```

TradingAgent 服务器 Nginx 是当前生产入口：

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

前端默认使用同源接口：

```bash
VITE_TRADING_AGENT_SNAPSHOT_URL=/api/trading-agent/snapshot
```

详细部署和 Nginx 示例见 [docs/integration.md](docs/integration.md)。

Cloudflare 部署说明见 [docs/cloudflare.md](docs/cloudflare.md)。当前形态是：

- 前端静态页面由 TradingAgent 服务器 `front/dist` 通过 Cloudflare Tunnel 对外提供。
- 只读 snapshot API 运行在 TradingAgent 服务器内侧 `127.0.0.1:8787`，同样通过 Cloudflare Tunnel/Nginx 接入。
- API 仍只读，不暴露交易执行、队列写入、账户、回调或密钥。

当前域名说明：

- `dashboard.tradingagent.cc`、`tradingagent.cc` 和 `www.tradingagent.cc` 已通过 Cloudflare Tunnel 指向 TradingAgent 生产服务器 Nginx。
- `api.tradingagent.cc` 已通过同一 Cloudflare Tunnel 指向 TradingAgent 生产服务器内侧 `127.0.0.1:8787`。
- Cloudflare Pages 项目 `tradingagent-front` 是历史/回滚入口；若重新启用 Pages，必须先完成最新构建部署并重新绑定自定义域，避免公开域名继续服务旧静态资源。

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
- `style_performance.jsonl` 作为回退收益来源时，US/Crypto/PM 的 `pnl`、`realized_pnl`、`unrealized_pnl` 和 `max_dd` 默认按 10,000 USD/USDT/USDC 原币账户折算成人民币后再进入 `marketSummaries[]` 与全市场收益曲线；若行内已有 `*_cny` 或 `fx_to_cny`，优先使用行内字段。不得用原币 PnL 除以人民币本金，也不得让旧账本本金覆盖规范本金。
- 维护、回补、烟测或修复重跑样本不得进入用户收益和交易量口径。只读 snapshot 会跳过带 `exclude_from_dashboard=true`、`dashboard_excluded=true`、`excluded_from_dashboard=true`，或 `run_context/run_mode/run_source/sample_type` 包含 `maintenance/backfill/smoke/repair/bootstrap/dry-run` 的模拟账本、权益快照、风格绩效和风格对比记录。
- A股研究证据卡片读取 `shared/review/ashare/research_evidence_latest.json`，只展示集合竞价/09:30 代理、尾盘候选、204001 逆回购估算和风格虚拟预算；该卡片不写队列、不触发交易、不发送邮件。
- A股服务器本地模拟账本读取 `shared/logs/local_sim/local_sim_pnl.json` 和 `shared/logs/local_sim/local_sim_trades.jsonl`，首页收益和持仓摘要会展示账户事实、现金、持仓市值、账户盈亏、可复盘样本和链路验证样本。缺少候选层来源或成交价来源字段的历史成交只作账户事实和链路验证，不计入策略收益、自我进化或胜率归因。新的 A股 server-local 成交会在账本和签名回执中保留 `fill_price_source`、`fill_price_source_class` 与 `fill_evidence`，用于证明模拟成交价来自市场快照而不是信号卡兜底价格。
- 后端已预留并提供权益快照生成入口：`shared/runtime_test/write_equity_snapshots.py`。生产运行时应由服务器定时或手动调用该入口，把模拟账本的已实现收益、未实现收益、本金、回撤、持仓数和价格缺失状态写入 `daily_mark_to_market.jsonl`，供首页收益主面板优先读取。
- 机会漏斗优先读取后端显式事件日志 `shared/review/opportunities/funnel_events.jsonl`，也兼容 `shared/logs/opportunities/funnel_events.jsonl`。每行表示一个真实机会在某个阶段的变化，支持 `opportunity_id/opportunityId`、`symbol/ts_code`、`market`、`stage`、`status`、`timestamp`、`sequence`、`latency_minutes/latencyMinutes`、`terminal`、`label` 和 `reason`。读模型会归一化为 `funnelEvents[]`，供首页动态机会漏斗按真实事件展示。
- 没有显式事件日志时，机会漏斗才从 signal 状态和模拟账本成交路径派生 `发现 / 研判 / 风控 / 待确认 / 结果` 阶段。首页机会漏斗把 `opportunity_log` 与 `signal_queue` 都视为真实阶段事件；如果同一标的后续有 `sim_ledger` 成交结果，可以补到“结果”阶段。纯模拟账本成交只能展示为“历史结果”，不得标成正在筛选的机会漏斗。没有真实事件时，只显示等待态或已有信号推导，不用静态样例或占位粒子伪装成真实筛选。若只有持仓，漏斗必须切换为持仓监控板，显示“暂无新机会”、持仓数量、正贡献、需观察和当前状态，不再硬套五段漏斗。没有真实机会、信号或持仓时，漏斗保持轻量等待态，不展示五段零值漏斗。
- 首页收益口径必须区分“真实 0 收益”和“收益尚未写入”：只有成交、非零收益、连续收益点或 A 股账户事实存在时，才把数字展示为收益结果；空账本或单个零值快照显示为等待收益。
- 首页右轨只展示最高优先级的自动运行状态，不再重复收益卡已有的账户、收益和风险数字；收益页曲线只展示走势、事件和区间切换，当前收益/目标差/回撤由页面摘要板承载。
- 多市场收益曲线按账本来源逐条前向补齐最新权益快照，避免某个市场短暂没有 5 分钟快照时把总本金缩小、导致收益曲线出现不真实跳变。
- 午盘复盘、策略归因和风险限额文件已列为可用来源，但仍需补充到 snapshot 构建后才能作为完整面板展示。
- 实盘只保留未来接入口；未验证账户授权前，前端不得展示为已接入。
- 首页顶部和收益主面板必须使用同一 `portfolio` 视图口径：全市场看聚合组合，A股看 A股模拟账户，其它市场看该市场摘要。不要再次拆成“模拟盘收益”和“现在收益”两个数字。
- 首页右轨只展示过程、阶段、状态、证据、更新时间和简短结果说明，不展示人工建议、内部错误码或调试文案。
- 市场状态带会从当前持仓或信号中为每个市场选择一个代表标的，并由只读 snapshot API 通过 `SHAREDSIGNALS_API_URL` 查询 SharedSignals。只展示真实返回的价格、短走势、区间、成交量和新鲜度；无代表标的、读取超时、上游降级或字段缺失时保持 `—`/“暂无代表行情”，不得生成样例价格。`marketPulseCoverage` 明确展示已取到、待映射、不可用和降级范围。请求限制为每个代表标的最多 24 个点、900ms 超时和 15 秒进程缓存。
- 过程页选择机会周期后，URL 增加 `opportunity=<opportunityId>`，周期行进入选中态，原始事件账本只展示该机会的显式事件；关联条在其它页面继续显示标的、阶段、结果、完整度、关联信号/持仓与可归因盈亏。后两项只接受相同的显式 `opportunityId`，无匹配持仓时保持 `—`。清除关联只改变浏览器展示状态。
- `Cmd/Ctrl+K` 打开桌面终端命令面板，可切换页面、市场和信息密度、清除关联机会。密度与各账本列显示写入版本化浏览器本地偏好，不写服务器、不修改 snapshot。

## 用户可见文案规范

- 用户界面优先使用结果与过程语言：当前收益、运行中、结果写回、持仓贡献、风险边界、资金状态、市场状态、自动复盘。
- 不在界面直接展示后台术语：运行证据、闭环、接口预留、有效样本、隔离样本、账户事实、策略资金、收益口径、代理样本、漏斗留存、流失。
- A股样本质量、成交价来源、候选层来源和链路验证仍可写在开发文档或 tooltip 的极短说明中，但主标签必须翻译成用户能理解的表达，例如“可复盘”“不计入复盘”“账户现金”“资金分配”。
- 实盘未验证前只展示“实盘待接入”，不得用同等权重文案暗示实盘已经可用。
- 首页右轨不得重复收益卡里的金额、收益率、目标差和回撤数字；它只说明自动系统正在运行什么、到了哪个阶段、证据和状态如何。
