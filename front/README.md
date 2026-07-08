# TradingAgent Front

TradingAgent 的前端看版层。它负责把自动化交易系统的结果展示给用户：
实时收益、机会管道、持仓、决策结果、风险边界和复盘信息。

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

- 实时收益：模拟盘当前收益、收益率、目标差、回撤和收益曲线放在同一主面板。全市场默认按 `marketSummaries[]` 聚合各市场资金和盈亏；切到单一市场时，只展示该市场口径。A股/CNFutures 默认按 200,000 CNY 展示并可保留人民币档位账本事实，US/Crypto/PM 默认按 10,000 USD/USDT/USDC 原币运行并折算成人民币汇总；美元市场的历史账本 `capital_base` 不覆盖该规范本金，避免旧样本把看板误导回 200,000 CNY。
- 机会管道：从机会进入、初筛、研究、风控到队列结果的动态筛选过程。优先使用真实 `funnelEvents[]`；没有新信号但有持仓时，管道仍显示为“暂无新机会”，只在同一面板底部补充持仓跟踪状态，不显示机会转化率，也不把持仓伪装成完整交易漏斗。
- 当前结果：用户现在该关注什么，收益来自哪里，哪些机会在推进，哪些风险已被挡住。

主题页按结果分类：

- 收益：收益曲线、目标、贡献来源；累计收益曲线支持今日/7日/30日/全部切换。
- 机会：当前机会、错过/放弃原因、筛选进度。
- 持仓：模拟盘持仓和未来实盘接入口状态。
- 决策：研究、交易、风控形成的判断及结果。
- 风险：回撤、敞口、风险节省、边界状态。
- 复盘：关闭机会、执行结果和复盘线索。

## 当前实现

- UI：React + TypeScript + Vite。
- 图表：Recharts，生产构建中拆成独立 `charts` chunk。
- 本地开发：Vite dev server 会挂载只读快照 API。
- 生产 API：`src/server/tradingAgentSnapshotHttp.ts` 可独立启动 Node 只读服务。
- 读取入口：`GET /api/trading-agent/snapshot`。
- 浏览器客户端：`src/api/tradingAgentIntegration.ts`。
- TradingAgent 读取器：`src/server/tradingAgentSnapshot.ts`。
- 真实数据适配：`src/api/tradingAgentReadModel.ts` 和
  `src/adapters/tradingAgentReadModel.ts`。
- 本地预览数据：`src/data/dashboard.ts`，只在快照接口不可用时用于开发预览；如果接口可用但某个领域返回空数组，前端必须展示真实空状态，不得回退到样例收益、机会或持仓。

## 数据边界

可以读取：

- `../signals/{pending,claimed,running,filled,cancelled,expired,failed,partial}/*.json`
- `../signals/positions/*.json`
- `../shared/accounting/position_plan.jsonl`
- `../shared/review/daily/daily_brief.jsonl`
- `../shared/review/ashare/research_evidence_latest.json`
- `../shared/review/attribution/*.jsonl`
- `../shared/risk/risk_limits.yaml`

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
- 后端已预留并提供权益快照生成入口：`shared/runtime_test/write_equity_snapshots.py`。生产运行时应由服务器定时或手动调用该入口，把模拟账本的已实现收益、未实现收益、本金、回撤、持仓数和价格缺失状态写入 `daily_mark_to_market.jsonl`，供首页实时收益主面板优先读取。
- 机会管道已能从 signal 状态和模拟账本成交路径派生 `发现 / 评分 / 风控 / 待执行 / 成交 / 继续观察 / 拒绝` 阶段，并在只读快照中额外输出 `funnelEvents[]`，供首页动态机会管道按真实事件展示。更精确的阶段时间线仍需要后端在每个机会上写入完整 stage timestamps。
- 首页机会管道优先展示来自 `signal_queue` 的真实阶段事件；如果同一标的后续有 `sim_ledger` 成交结果，可以补到“结果”阶段。纯模拟账本成交只能展示为“成交回放”，不得标成实时机会漏斗。没有实时事件时，只显示等待态或已有信号推导，不用静态样例或占位粒子伪装成真实筛选。若只有持仓，管道必须显示“暂无新机会”，并用账户事实表达“多少持仓、多少正贡献、多少需观察”。没有真实机会、信号或持仓时，管道保持轻量等待态，不展示五段零值漏斗。
- 首页收益口径必须区分“真实 0 收益”和“收益尚未写入”：只有成交、非零收益、连续收益点或 A 股账户事实存在时，才把数字展示为收益结果；空账本或单个零值快照显示为等待收益。
- 首页右栏只保留下一步关注，不再重复展示实时收益卡已有的账户、收益和风险数字；收益页曲线只展示走势、事件和区间切换，当前收益/目标差/回撤由页面摘要板承载。
- 多市场收益曲线按账本来源逐条前向补齐最新权益快照，避免某个市场短暂没有 5 分钟快照时把总本金缩小、导致收益曲线出现不真实跳变。
- 午盘复盘、策略归因和风险限额文件已列为可用来源，但仍需补充到 snapshot 构建后才能作为完整面板展示。
- 实盘只保留未来接入口；未验证账户授权前，前端不得展示为已接入。
- 首页顶部和实时收益主面板必须使用同一 `portfolio` 视图口径：全市场看聚合组合，A股看 A股模拟账户，其它市场看该市场摘要。不要再次拆成“模拟盘收益”和“现在收益”两个数字。
- 首页右侧摘要只展示结果关系：当前收益状态、当前焦点、风险边界和最多三条下一步，不展示内部字段、系统原因或调试文案。

## 用户可见文案规范

- 用户界面优先使用结果语言：当前收益、当前机会、持仓贡献、风险边界、资金状态、市场状态、下一步关注。
- 不在界面直接展示后台术语：运行证据、闭环、接口预留、有效样本、隔离样本、账户事实、策略资金、收益口径、代理样本、漏斗留存、流失。
- A股样本质量、成交价来源、候选层来源和链路验证仍可写在开发文档或 tooltip 的极短说明中，但主标签必须翻译成用户能理解的表达，例如“可复盘”“不计入复盘”“账户现金”“资金分配”。
- 真实账户未验证前只展示“真实账户预留 / 实盘未接入”，不得用同等权重文案暗示实盘已经可用。
- 首页右侧摘要不得重复实时收益卡里的金额、收益率、目标差和回撤数字；它只回答“现在先看什么”和“为什么要看”。
