# TradingAgent 当前状态

> 最后更新：2026-07-26 CST。本文只维护当前事实与下一停止线；历史候选和失败证据通过 Git 与服务器只读证据目录追溯。长期边界见 [AGENTS.md](AGENTS.md)，运行与回滚见 [docs/operations.md](docs/operations.md)。

## 当前结论

TradingAgent 已完成 TradingDatas 正式内部 API 的专用身份、Bearer token-file
消费合同和 26-active 首屏有界验收，但尚未进入 A股 observation 或自动模拟交易。

- 本地、`origin/main` 与 GitHub `main` 的当前一致性以交付时
  `git rev-parse HEAD origin/main` 读回为准；本轮已验证运行代码锚点为
  `7cec341…`。
- 服务器已安装对应不可变代码 release：
  `/opt/investment/releases/tradingagent/7cec341633abc9302e0a407a3615ec29d8c59447`。
  该目录不是 active/current 切换，也没有启动 front、worker、timer 或真实交易。
- TradingDatas 正式内部端点为 `http://127.0.0.1:18082`，只消费
  `GET /v1/catalog` 与 `POST /v1/query`；当前
  `catalog_version=v1-c19a22c011fc363e`，190 total / 26 active / 164 paused。
- 专用运行身份是 `tradingagent:tradingagent`（UID/GID 987）。token 只从
  `/run/secrets/tradingagent/tradingdatas-read.token` 读取；parent 为
  `root:tradingagent 0710`，leaf 为 `tradingagent:tradingagent 0600` 的 regular
  single-link file。值和内容哈希没有进入代码、日志、消息、manifest 或回执。
- 26 个 active 均完成两次相同的 `limit=1` 首屏查询：
  3 ready、9 stale、14 unobserved；Evidence Gate 为 3 accept / 23 reject，
  query 合同失败为 0。12 项返回 `next_cursor`，因此本轮只证明目录、认证、
  默认请求、省略字段和 metadata fail-closed，不证明完整数据读取。
- 3 个 ready 是 `cn.market.trade_calendar`、
  `cn.equity.security_master`、`cn.dataset.index_classify`。A股日线
  `cn.equity.daily` 与申万日线 `cn.dataset.sw_daily` 仍为 stale/degraded，
  不进入研究快照、股票选择或模拟决策。
- `research_snapshot_emitted=false`、`simulation_started=false`、
  `REAL_TRADING_ENABLED=false`。

正式通过证据：

- 代码 release：
  `/opt/investment/release-evidence/tradingagent/20260726T100807Z-ta-catalog26-code-7cec341`
- 26-active 读回：
  `/opt/investment/release-evidence/tradingagent/20260726T100914Z-ta-catalog26-readback-7cec341`
- 读回状态文件：
  `/var/lib/tradingagent/ashare-observation/catalog26-v1-c19a22c011fc363e.json`
  （`tradingagent:tradingagent 0600`）

本地主线与远端主线一致性必须在每次交付时重新执行
`git rev-parse HEAD origin/main`；顶部提交号只标记本轮证据，后续提交会自然作废。

## 六层事实

| 层级 | 当前事实 | 不能据此推断 |
|---|---|---|
| 本地主线 | `main` 已含 provider-neutral client、分页/证据门禁、目录默认请求省略和 0710 secret parent 安全遍历 | 代码存在不等于服务器已激活 |
| GitHub 主线 | 本轮两个合同修复已普通合并，GitHub CI `front`/`test` 均通过 | CI 不等于真实数据 fresh 或模拟盘已启动 |
| 服务器代码 | `7cec341…` 不可变 release 已安装、未激活 | release 目录不等于 current/front/worker |
| 服务身份 | UID/GID 987、专用 token-file、正式 18082 认证可用 | token 可读不等于任一 dataset 可用 |
| 数据验收 | 26-active 首屏双跑合同 PASS；3 ready 接收、23 impaired 拒绝 | `limit=1` 不是分页完整性、PIT、历史训练或行业宽度证明 |
| 交易能力 | front inactive 且 runtime-masked，8787 closed；无 scheduler、broker 或真实交易 | 模拟合同存在不等于自动模拟盘闭环已运行 |

旧 `8082` listener 仍由旧系统所有者保留，TradingAgent 没有依赖、探测或 fallback
到该端口。TradingDatas collector timer 保持 inactive/disabled（当前机器读回为
not-found）；TradingAgent 不负责启用或修改 TradingDatas 采集调度。

## A股第一阶段边界

1. **个股范围**：只分析沪深主板普通 A股；创业板、科创板和北交所个股因当前
   账户权限边界不进入个股研究、候选或模拟交易。
2. **环境参考**：上述板块的指数、行业分类和汇总统计可以作为市场环境输入，
   但必须标为 `context_only`，不得反向把无权限个股加入 Universe。
3. **行业起点**：先深挖少数高活跃产业，采用动态研究池，而不是永久概念股票池。
   第一批可研究 AI 算力/半导体/数据中心、机器人/工业自动化、创新药；观察池可放
   商业航天、有色/能源/电网。研究优先级不是买入建议。
4. **小资金优势**：50,000 CNY 只用于 simulation authority。系统允许现金胜出、
   少量高质量机会、no-trade band、整数 100 股、最低经济订单、低容量机会和
   试探—确认—扩仓；不以交易次数或每月 20% 作为强制生产约束。
5. **多风格**：产业趋势、事件/预期差、跨市场错配和现金状态逻辑上独立，
   资金统一组合、订单统一净额；当前仍未获得可运行的正式研究快照。

## 当前架构边界

- **TradingDatas consumer**：只用 catalog/query。HTTP 200 不能覆盖
  stale/unobserved/degraded；source proof、cursor、page/row budget、identity 和
  same-observation 分别验证。未声明 public identity 的 21 个 active dataset 只能
  做 metadata accounting，不能成为研究或执行证据。
- **研究层**：provider-native rows 与 envelope metadata 分离。没有 first-seen 与
  revision authority 时标记 current observation，禁止伪造 PIT 或用于历史训练。
- **决策层**：市场、行业、个股、事件、资金、成本和不确定性输出结构化 evidence；
  `DecisionEvidence -> TargetPosition -> TradeIntent` 之间仍有组合与硬门禁。
- **执行层**：A股、CNFutures、Crypto 保持独立 adapter/account/ledger。当前无
  broker adapter 激活、无真实账户、无订单、成交或资金副作用。
- **LLM**：DeepSeek 只作为 evidence sidecar，用于公告/新闻结构化、产业关系抽取、
  历史事件检索和报告；不能生成最终仓位或绕过确定性校验。
- **自我进化**：Decision Ledger、counterfactual、Champion–Challenger 和漂移/
  校准监控只允许提出候选或自动收紧风险；自动晋级、恢复或扩大风险不在当前权限内。

## 兼容与退役

- `current-v1` 只指 provider-neutral catalog/query 消费链，不包含旧数据 reader。
- 仍有明确消费者的旧 A股路径保持 `active-compatibility` 或
  `retirement-pending`，只允许迁移和回归验证，不新增依赖。
- Mini/Hermes、旧直接执行和已清零的非核心市场入口保持 `hard-blocked`；
  仓库退役不自动证明其它主机的安装态已清理。

## 明确未完成

- 日线和申万日线仍 stale/degraded，因此不能诚实启动 A股股票 observation、
  feature、ranking 或自动模拟盘。
- 26-active 首屏 probe 不是完整分页验收。下一份完整 research manifest 必须按
  catalog 冻结 fields/filter/order/limits，并对需要的 dataset 走到 terminal cursor；
  循环、预算超限、跨页 metadata/identity 漂移均拒绝。
- 三个 ready 目前只有合同可用性；尚未形成交易日历、证券主数据和行业分类的
  current-observation bundle。
- 未积累至少 21 个 forward-collected 交易会话；无正式历史 PIT/revision authority、
  标签成熟度、冻结 OOS 或 60–120 交易日模拟样本。
- 日频数据不能合成分钟级 quote、bid/ask 或可成交 fill。正式自动模拟成交仍需要
  经验证的执行时点行情或独立模拟成交政策。
- front 继续停止；本阶段不恢复 `tradingagent.cc` 页面。当前也没有自动 worker、
  current pointer、服务 unit 或观察 timer 激活。
- 旧 8082、旧服务器 runtime 和退役文档只能由各自 owner 按证据链清理；
  TradingAgent 不使用它们，也不以删除代替依赖清零证明。

## 下一阶段入口

依赖顺序固定为：

1. 等 TradingDatas 将 `cn.equity.daily`（以及需要时的 `cn.dataset.sw_daily`）
   恢复为 ready/non-degraded；
2. 使用 catalog 动态冻结第一阶段最小 manifest，只完整读取
   `trade_calendar + security_master + daily + index_classify`，行业日线继续
   按健康状态可选；
3. 生成 current-observation bundle 和主板 Universe，创业板/科创板个股明确排除，
   指数/行业汇总仅作为 context；
4. 启动无成交副作用的每日 observation 与 Decision Ledger 积累；
5. 数据、日历、执行行情和模拟资本权威全部通过后，才开放自动 paper
   `Signal -> TargetPosition -> Risk -> PaperFill -> Reconcile -> Attribution`
   闭环；
6. 只有长期样本、校准和回撤门禁通过后，才讨论模型晋级；真实交易继续保持关闭。
