# TradingAgent 当前状态

> 最后更新：2026-07-27 CST。本文只维护当前事实与下一停止线；历史候选和失败证据通过 Git 与服务器只读证据目录追溯。长期边界见 [AGENTS.md](AGENTS.md)，运行与回滚见 [docs/operations.md](docs/operations.md)。

## 当前结论

TradingAgent 已完成 TradingDatas 正式内部 API 的专用身份、Bearer token-file
消费合同、历史 26-active 首屏有界验收，以及 A股核心三数据集 observation 的
第一次正式只读运行。`20260724` current-observation 已形成并完成幂等重放。
2026-07-27 又完成动态 catalog/manifest builder 的仓库实现、服务器失败关闭验收
以及 freshness 修正后的正式重跑。正式目录已扩大到 100 active，但系统仍只映射
calendar/security-master/daily 三个审核过的核心角色；builder 已发布内容寻址
manifest，隔离 observation one-shot 和同 root 幂等重放均 PASS。自动模拟交易仍未
启动。

- 本地、`origin/main` 与 GitHub `main` 的当前一致性以交付时
  `git rev-parse HEAD origin/main` 读回为准；本轮 observation 运行代码锚点为
  `6db813c…`；本轮 runtime/front 修复代码锚点为 `eb2e18a…`。
- 服务器已安装对应不可变代码 release：
  `/opt/investment/releases/tradingagent/6db813cdb9c9eaa36ab65c3529ebaeee145aeba2`。
  服务器另安装
  `/opt/investment/releases/tradingagent/eb2e18a6c38b1f5c1139679a8e910c6923fa3edb`
  用于 runtime/unit 验收；动态 builder 对应不可变 release 为
  `/opt/investment/releases/tradingagent/94fcdf767e9e531b18caa1ac0e9ea18cbb1af647`。
  本轮 worker preflight 代码锚点
  `724ea8818feff142df57c4a7bf7b558e29ec0a35` 也已作为
  root-owned、只读的不可变 release 安装到
  `/opt/investment/releases/tradingagent/724ea8818feff142df57c4a7bf7b558e29ec0a35`。
  这些 release 都不是 active/current 切换，也没有启动 front、worker、timer
  或真实交易。
- TradingDatas 正式内部端点为 `http://127.0.0.1:18082`，只消费
  `GET /v1/catalog` 与 `POST /v1/query`。最新 TA 自身读回为
  `catalog_version=v1-3c18b5d842eedfb2`，190 total / 100 active / 90 paused；
  active contract 摘要为
  `3ae63abd22540312489aa101388a59ad790db853cf848ca28167131e7e653eaf`。
- 专用运行身份是 `tradingagent:tradingagent`（UID/GID 987）。token 只从
  `/run/secrets/tradingagent/tradingdatas-read.token` 读取；parent 为
  `root:tradingagent 0710`，leaf 为 `tradingagent:tradingagent 0600` 的 regular
  single-link file。值和内容哈希没有进入代码、日志、消息、manifest 或回执。
- 26 个 active 均完成两次相同的 `limit=1` 首屏查询：
  3 ready、9 stale、14 unobserved；Evidence Gate 为 3 accept / 23 reject，
  query 合同失败为 0。12 项返回 `next_cursor`，因此本轮只证明目录、认证、
  默认请求、省略字段和 metadata fail-closed，不证明完整数据读取。
- 首屏验收时的 3 个 ready 是 `cn.market.trade_calendar`、
  `cn.equity.security_master`、`cn.dataset.index_classify`。此后
  `cn.equity.daily` 已对 `trade_date=20260724` 完成 5526 行采集并正式读回为
  ready/fresh；`cn.dataset.sw_daily` 同日上游返回 QuickSync `40101`
  permission-denied，继续 impaired/fail-closed。
- TradingDatas 随后补齐 `20260723`–`20260725` 日历，正式 18082 直接证明
  `20260724 is_open=1`。TA 用新鲜 decision time/state root 完整读取
  `trade_calendar + security_master + daily`，生成 3041 只沪深主板
  observation Universe，明确排除 2569 条不在权限/第一阶段范围或不满足数据门禁的
  个体。五项 committed evidence 和精确幂等重放均 PASS。
- `index_classify` 在本次重跑时返回 failed/degraded，作为 optional context 被从
  核心 manifest 移除；`context_probe_roles=[]`，不能据此做行业宽度或行业选股。
- `current_observation_snapshot_emitted=true`、`simulation_started=false`、
  `REAL_TRADING_ENABLED=false`。
- 动态 builder 服务器只读运行时，calendar 与 security-master 为
  `runtime_state=success/degraded=false`；daily 为
  `runtime_state=stale/degraded=true`。因此返回
  `core_dataset_evidence_rejected:cn.equity.daily`、退出码 2，且没有创建或更新
  manifest root。这是数据新鲜度停止线，不是认证、目录或代码故障。
- 2026-07-27 使用当前权威 `main` release、专用 UID、正式 18082 和隔离
  manifest root 再次执行 builder，仍以同一 reason code 退出 2，且隔离 root
  为空。TradingDatas 随后确认 `20260724` 的 5526 行 daily 和成功 receipt
  实际存在，但当前通用 freshness 投影用周五分区零点直接比较周末墙上时钟，
  触发 259200 秒 SLA，因此元数据仍诚实保持
  `state=stale/runtime_state=stale/degraded=true`。TA 不覆盖该状态，等待
  TradingDatas 修正交易会话感知的 freshness 合同。
- TradingDatas 在 immutable release
  `98fa9489c4c8e960d392487c99b06d59e3db8f76` 修正盘后日频 freshness 投影后，
  TA 受限身份实际读回 daily 为
  `ready/success/fresh/valid/degraded=false`。builder 随即发布
  `manifest_sha256=7e5bdc5dd75cc4cd33a1a1bb80b66645c34cd2e4ef4cee08612e26e2bdf09d1f`，
  session 为 `20260724`，且仍明确
  `historical_pit_eligible=false/execution_authority=false/simulation_started=false`。
- 隔离 observation one-shot 生成 3041 只沪深主板 observation Universe，排除
  2569 条不符合第一阶段权限、标的或数据门禁的个体；首次运行与同 root 重放的
  snapshot、Universe、ledger、receipt 和 transaction-complete SHA 全部一致，
  `idempotent_replay` 从 `false` 变为 `true`。该结果是
  `observation_only`，不是 candidate、TargetPosition、PaperFill 或账户变更。

正式通过证据：

- 代码 release：
  `/opt/investment/release-evidence/tradingagent/20260726T100807Z-ta-catalog26-code-7cec341`
- 26-active 读回：
  `/opt/investment/release-evidence/tradingagent/20260726T100914Z-ta-catalog26-readback-7cec341`
- 读回状态文件：
  `/var/lib/tradingagent/ashare-observation/catalog26-v1-c19a22c011fc363e.json`
  （`tradingagent:tradingagent 0600`）
- 当前会话代码、失败关闭与最终 PASS 证据：
  `/opt/investment/release-evidence/tradingagent/20260726T105403Z-ta-current-session-6db813c`
- 详细读回报告：
  [docs/reports/2026-07-26-ashare-current-session-readback.md](docs/reports/2026-07-26-ashare-current-session-readback.md)
- runtime/front 退役证据：
  `/opt/investment/release-evidence/tradingagent/20260726T114404Z-ta-runtime-retirement-eb2e18a`
  与
  `/opt/investment/release-evidence/tradingagent/20260726T114546Z-ta-front-base-forwardfix-eb2e18a`
- runtime/front 详细报告：
  [docs/reports/2026-07-26-ashare-runtime-retirement-readback.md](docs/reports/2026-07-26-ashare-runtime-retirement-readback.md)
- 动态 catalog/manifest builder 服务器证据：
  `/opt/investment/release-evidence/tradingagent/20260727T085600Z-ta-ashare-manifest-94fcdf7`
- 动态 builder 详细报告：
  [docs/reports/2026-07-27-ashare-dynamic-manifest-readback.md](docs/reports/2026-07-27-ashare-dynamic-manifest-readback.md)
- 99-active 增量目录读回证据：
  `/opt/investment/release-evidence/tradingagent/20260727T092955Z-ta-catalog99-94fcdf7`
- 99-active 增量报告：
  [docs/reports/2026-07-27-tradingdatas-catalog99-readback.md](docs/reports/2026-07-27-tradingdatas-catalog99-readback.md)
- 当前 `main` release、动态 builder 重跑和 worker 安装预检：
  [docs/reports/2026-07-27-ashare-worker-preflight.md](docs/reports/2026-07-27-ashare-worker-preflight.md)
- freshness 修正后的 manifest 与 observation one-shot：
  [docs/reports/2026-07-27-ashare-observation-pass.md](docs/reports/2026-07-27-ashare-observation-pass.md)
- 同一 catalog 的发布侧 fresh consumer parity 以 UID 987 和既有 TA read scope
  对 99 个 active dataset 逐项执行 `POST /v1/query limit=1`、省略 `as_of`：
  99/99 HTTP 200、0 query-contract failure、79 nonempty、20 legal empty；
  envelope metadata 为 3 ready、92 partial、4 stale。该证据只证明固定 API
  可达和 metadata parity，不是完整分页、研究资格、历史 PIT 或执行 authority。

本地主线与远端主线一致性必须在每次交付时重新执行
`git rev-parse HEAD origin/main`；顶部提交号只标记本轮证据，后续提交会自然作废。

## 六层事实

| 层级 | 当前事实 | 不能据此推断 |
|---|---|---|
| 本地主线 | `main` 已含 provider-neutral client、分页/证据门禁、动态 manifest builder 和 0710 secret parent 安全遍历 | 代码存在不等于服务器已激活 |
| GitHub 主线 | dynamic manifest 已普通合并，GitHub CI `front`/`test` 均通过 | CI 不等于真实数据 fresh 或模拟盘已启动 |
| 服务器代码 | `6db813c…` observation、`eb2e18a…` runtime 与 `94fcdf7…` dynamic builder 不可变 release 已安装、未切 current | release 目录不等于 active worker |
| 服务身份 | UID/GID 987、专用 token-file、正式 18082 认证可用 | token 可读不等于任一 dataset 可用 |
| 数据验收 | 历史99-active首屏parity为99/99 HTTP 200但仅3 ready；freshness 修正后 catalog 为100 active，三核心 manifest 和 `20260724` observation/重放再次 PASS | 首屏可达和单次current observation都不是全目录完整分页、历史PIT、训练样本、行业宽度或执行证明 |
| 交易能力 | front inactive/disabled 且 runtime-masked，8787 closed；worker inactive/static，timer不存在；无 broker 或真实交易 | 模拟合同存在不等于自动模拟盘闭环已运行 |

旧 `8082` listener 仍由旧系统所有者保留，当前 observation consumer 没有探测或
fallback 到该端口。legacy front drop-in 已移出 active systemd 目录，front base
unit 已与当前仓库字节一致；active unit 中旧 `8082`、`SharedSignals`、
`/opt/tradingagent` 和 `marketgraph` 身份引用均为零。TradingDatas collector
timer 保持 inactive/disabled；TradingAgent 不负责启用或修改 TradingDatas
采集调度。

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
   资金统一组合、订单统一净额；当前 observation 尚未形成可运行的行业特征、
   个股 ranking 或策略信号。

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

- 目前只积累 1 个 forward-collected 交易会话，未达到 21 个会话的 20 日特征最低
  覆盖；无正式历史 PIT/revision authority、标签成熟度、冻结 OOS 或
  60–120 交易日模拟样本。
- 申万日线仍 permission-denied，`index_classify` 本次为 failed/degraded；核心
  observation 没有行业上下文，不能冒充行业宽度、行业排名或产业研究输入。
- 当前 snapshot 只证明单次 current observation，不是可训练历史数据，也没有
  feature、ranking、forecast、TargetPosition、PaperFill 或账户对账。
- 日频数据不能合成分钟级 quote、bid/ask 或可成交 fill。正式自动模拟成交仍需要
  经验证的执行时点行情或独立模拟成交政策。
- front 继续停止；本阶段不恢复 `tradingagent.cc` 页面。tracked base unit 已安装
  但保持 inactive/disabled/runtime-masked，旧 drop-in 已退役。当前也没有
  current pointer、自动 worker 或 observation timer 激活。
- 专用 UID 987 已通过新的 root-owned versioned Python runtime 执行真实入口和
  audit；旧 `/opt/tradingagent/venv` 不再被 TA active unit 引用。动态 manifest
  rollover 已完成失败关闭和恢复后 PASS 验收，手工 one-shot 与幂等重放已完成；
  tracked unit、secret-free env 与 `current` 指针尚未安装，失败恢复和 timer
  激活仍未完成。
- 旧 8082、旧服务器 runtime 和退役文档只能按各自 ownership 与证据链清理；
  不以删除代替依赖清零证明。

## 下一阶段入口

依赖顺序固定为：

1. 在独立发布门禁下安装 tracked inactive/static unit、secret-free env 与
   `current` 指针；先验证 unit one-shot、失败恢复和回滚，不直接启 timer；
2. unit one-shot 通过后再单独决定 observation timer，以每个交易日持续积累
   committed current-observation 与 Decision Ledger；创业板、
   科创板和北交所个股继续排除，健康的指数/行业汇总仅作为 context；
3. 等 `index_classify`/`sw_daily` 恢复健康后，独立加入行业上下文，不阻断核心
   主板 observation，也不把汇总数据冒充个股权限；
4. 数据、日历、执行行情和模拟资本权威全部通过后，才开放自动 paper
   `Signal -> TargetPosition -> Risk -> PaperFill -> Reconcile -> Attribution`
   闭环；
5. 只有长期样本、校准和回撤门禁通过后，才讨论模型晋级；真实交易继续保持关闭。
