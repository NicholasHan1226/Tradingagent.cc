# TradingAgent 当前状态

> 最后更新：2026-07-28 CST。本文只维护当前事实与下一停止线；历史候选和失败证据通过 Git 与服务器只读证据目录追溯。长期边界见 [AGENTS.md](AGENTS.md)，运行与回滚见 [docs/operations.md](docs/operations.md)。

## 当前结论

TradingAgent 已完成 TradingDatas 正式内部 API 的专用身份、Bearer token-file
消费合同、历史 26-active 首屏有界验收，以及 A股核心三数据集 observation 的
第一次正式只读运行。`20260724` current-observation 已形成并完成幂等重放。
2026-07-27 又完成动态 catalog/manifest builder 的仓库实现、服务器失败关闭验收
以及 freshness 修正后的正式重跑。正式目录现为 101 active，但系统仍只映射
calendar/security-master/daily 三个审核过的核心角色；builder 已发布内容寻址
manifest，隔离 observation one-shot 和同 root 幂等重放均 PASS。仓库现已包含
5分钟 fixture/mock 研究闭环：严格或显式 delayed-paper 分钟证据之后可生成透明
滚动特征、未经校准的确定性排名、第一根真正可达K线模拟结算、四个隔离反事实账本、
Decision Ledger 与对账。TradingDatas 已将 `cn.dataset.rt_min` 从10只扩为30只
主板并完成真实30/30自动回读；该数据约晚一个完整5分钟K线，只取得
observation/data accumulation 与 delayed-paper 资格，不满足 TA 的30秒执行证据
门禁。TA现役代码已支持显式审核的500只Universe：分钟分页/行预算按Universe与
catalog上限派生，Universe内容SHA进入manifest，上一日参考价和每根分钟快照都必须
500/500精确覆盖；但500只真实分片运行仍由TradingDatas单独验收，不能由代码容量
推断生产覆盖。TA observation worker 与
durable capital runtime 仍未启动；服务器已用正式18082的30只精确分钟快照手工推进
非生产 delayed-paper one-shot，并已启用仅用于 fixture 自动累计的分钟调度与次日
会话初始化调度。首次分钟自动触发因当日历史缺口失败关闭且账本不变；它不是一次成功
自动模拟轮次，更不授予 broker 或真实交易权限。

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
  5分钟 delayed-paper 与500→全量监控容量代码另以 root-owned、只读不可变
  release 安装在
  `/opt/investment/releases/tradingagent/ac828bf5da25ab061f0b3cc785577f18432334e2`；
  自动累计 wrapper、次日会话初始化器和 tracked unit 的上一回滚 release 是
  `/opt/investment/releases/tradingagent/437fa274f5cfc47bac6ae03f7a26270ec404659c`。
  500只会话消费能力的上一回滚 release 是
  `/opt/investment/releases/tradingagent/65ee4f012fc673b0680d63a5a87195b1c7061adb`；
  支持由 `ASHARE_MINUTE_UNIVERSE_SOURCE` 装载审核 Universe 的 root-owned、
  只读不可变 release 的直接回滚点是
  `/opt/investment/releases/tradingagent/f81acb983ed72d0bbfa9d2331a67d62837289dd2`。
  精确48次分钟timer窗口对应的 root-owned、只读不可变 release
  `/opt/investment/releases/tradingagent/d6f5f2cf6bfdf3826537bda65000f1b32304ed73`
  已成为 `/opt/investment/releases/tradingagent/current`。这只更新了
  simulation-only 调度入口；front、observation worker、broker 与真实交易仍未启动。
- TradingDatas 正式内部端点为 `http://127.0.0.1:18082`，只消费
  `GET /v1/catalog` 与 `POST /v1/query`。TA 不固定跨仓 catalog 版本或 active
  数量，每次运行均以正式 catalog 动态发现并逐数据集失败关闭。
- TradingDatas 的通用
  `tradingdatas-provider-native-collect.timer` 已由 TradingDatas owner 启用，
  固定 `OnCalendar=*-*-* *:0/5:00`、`AccuracySec=1s`、
  `RandomizedDelaySec=15s`、`Persistent=true`；这不是 TA timer，也不授予
  TA 模拟或真实交易权限。TD 的 current release、catalog 和扩容回执以其正式
  独立任务 handoff 为准，不在 TA 文档中硬编码成永久当前值。
- `cn.dataset.rt_min` schema major 2 先以10只股票证明相邻bar连续性，随后
  TradingDatas release `d0edb51…` 完成30只主板的真实
  provider→SQLite receipt→正式18082 `30/30` 回读，元数据为
  `ready/success/fresh/valid/non-degraded`。上游约晚一个完整5分钟K线，不能
  宣称 `bar_end -> observed_at <= 30s`，且数据不含 L1/bid-ask。
- 2026-07-28 15:05自动采集轮已把最终15:00 bar落库；TA身份经正式18082精确
  查询为30/30、ready/success/fresh/valid/non-degraded，receipt与lineage完整。
  这证明当前30只链路完成当日自动收盘，不代表500只已在生产采集。
- 500只TA消费变更已通过全仓`3792 passed`与GitHub前后端CI，并普通合并为
  `65ee4f012fc673b0680d63a5a87195b1c7061adb`；服务器现役release对500行预算
  smoke PASS。TradingDatas的500候选固定为5个100只分片，只有同一bar五片全部
  成功且500个`(ts_code,time)`唯一完整时才可handoff；当前尚未取得连续两轮
  500/500正式证据，所以生产数据覆盖仍是30只。
- 当前首轮 delayed-paper 使用的 previous-close 来自上一完成交易日的
  `daily.close`；非停牌状态只对正式 `rt_min` 完成K线且成交量为正的30只成立，
  两类证据均以 row/envelope hash 绑定。TradingDatas 随后独立回读
  `stk_limit` 为 ready/fresh/valid，`suspend_d` 为带完整 receipt/lineage 的
  合法空、non-degraded/valid；它们是后续增强校验，不反向改写本轮已冻结参考
  证据。`adj_factor`、`sw_daily` 和其它 degraded 数据继续 fail closed。
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
- `current_observation_snapshot_emitted=true`、
  `delayed_fixture_simulation_started=true`、`automatic_scheduler_started=true`、
  `automatic_simulation_successful_round=false`、`REAL_TRADING_ENABLED=false`。
- 2026-07-28 服务器以专用 UID、正式18082、不可变代码 release
  `ac828bf5…` 手工处理 `13:10` 至 `13:45` 八个精确30只分钟快照；每次均
  `30/30`、metadata ready/fresh/valid/non-degraded、audit rejection 为0。
  首根建立滚动基线，后续每根形成30个特征/候选；四个50,000 CNY隔离账本均
  对账通过。两笔历史候选在不可回看的K线上诚实记为 `not_filled`；13:45 快照
  随后以决策后第一根真正可达K线完成 `002436.SZ` 模拟买入：baseline 100股、
  dynamic-position 200股，成交价32.15 CNY，费用分别5.03215和5.0643 CNY。
  两账本各形成1个T+1持仓并继续保留下一候选挂单；同一13:45快照重放退出2且
  bundle SHA不变。状态 bundle 为 `non_production_fixture`、0600、可重启且无
  资本/执行 authority。
- 自动累计与会话初始化候选已普通提交并同步 GitHub main；当前代码锚点
  `437fa27…` 的本地候选测试为 `2511 passed`，服务器不可变 release 上本轮
  定向测试为 `20 passed`。
  `/etc/tradingagent/ashare-minute-paper.env`、tracked service/timer 与
  `current` 指针已安装并通过 `systemd-analyze verify`，环境仍固定
  `REAL_TRADING_ENABLED=false`。分钟 timer 与次日09:20会话 timer 当前均为
  `enabled/active`。14:25:44 首次自动分钟触发因手工状态停在13:45、13:50形成
  真实缺口而退出2；timer继续排定后续轮次，日志只记录fail-closed。正式 bundle
  SHA保持
  `2812f88d34d52df67ed2275f439f1a9904e5c1165a1988b2e3964edbf7511130`，
  系统没有回填、跳过或改写时间。TD中途发布参考数据后catalog由本会话冻结的
  `v1-d27aa31fbfb60a3c`变为`v1-541b1314702f4897`；已开始的会话不原地换约。
  使用当前catalog执行的TA受限只读查询分别对14:30、14:35、14:40、14:45与
  14:50精确bar完成双跑，五次均为30/30、
  ready/fresh/valid/non-degraded、receipt与lineage完整且
  same-replay一致。因此行情数据仍在累计，正式fixture账本不推进是连续性与
  冻结合同门禁，而不是把当天缺口或catalog漂移静默洗白。
- 现役分钟timer已把旧日历表达式收敛为每交易日精确48次：
  `09:40–11:35`与`13:10–15:05`。服务器展开验证为48个不重复触发点，安装后
  下一触发为`2026-07-29 09:40 CST`；已知缺口仍只保留失败关闭证据且不做历史
  补单，午休后段和收盘后不再重复空转。
- 2026-07-28 17:46 CST 对运行态复核发现旧 release `d6f5f2c…` 的 timer
  仍在15:05后触发至15:55，且缺少盘后只读日报模块；因此以 PR #58 的 CI
  `test/front` 双 PASS 和本地17项聚焦测试为门禁，普通合并并原子切换到
  root-owned immutable release `b4f5d600f3d8bb317375a05b2f613e8a06e89c52`。
  服务器 unit 与该 release 字节一致，收盘段只保留显式`15:00:40`和
  `15:05:40`，分钟与会话 timer 均为`enabled/active`，下一次分别为
  `2026-07-29 09:40`和`09:20`；`Ashare/minute_day_report.py`已进入现役
  release，环境仍为`REAL_TRADING_ENABLED=false`。切换前后全部既有
  `state-bundle.json`哈希一致，旧 release 与旧 unit 保留为回滚证据；尚未把
  明日正式09:20初始化、首个成功自动轮或任何盈利结果标为完成。
- TradingDatas 已修正 `20260729` SSE日历
  (`is_open=1/pretrade_date=20260728`) 的envelope水位：未来适用日只保留在row，
  `data_through=observed_at`为实际观察时刻。发布切换瞬间TA initializer曾一次
  `minute_session_catalog_http_failed`，5秒后重跑已通过catalog与calendar，
  当时只在`minute_session_daily_universe_incomplete`失败关闭；未创建
  `20260729`目录，既有bundle不变。
- TradingDatas 16:30盘后通用采集于16:30:25成功结束，`cn.equity.daily`与
  `cn.dataset.rt_min`均写入success receipt。TA UID987随后经正式18082对
  `trade_date=20260728`和30只审核Universe执行有界双跑：返回30/30，
  `state=ready/degraded=false/freshness=fresh/quality=valid`，lineage完整，
  receipt、`data_through`和`observed_at`非空，且无游标。使用现役
  `d6f5f2c…`在独立evidence root初始化`20260729`时首次PASS、重放
  `reused=true`，Universe SHA为
  `0e26f54fc2ab391f0187a5787f9955b90e8a2ff21969957565749b733e035203`。
  隔离目录仅生成三项0600输入；无`state-bundle`、资本、订单或执行authority，
  正式`/var/lib/tradingagent/ashare-minute-paper/20260729`仍不存在。一次辅助
  摘要脚本因误读非合同字段`runtime_state`失败，修正后只读取冻结的
  QueryMetadata字段并通过；没有掩盖失败或修改运行态。明早09:20 timer的实际
  正式初始化、首根连续分钟bar和首个成功自动模拟轮仍需独立读回。
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
| GitHub 主线 | delayed-paper与500→全量监控容量已普通合并，GitHub CI通过 | CI 不等于500只真实数据覆盖或模拟盘已启动 |
| 服务器代码 | `d6f5f2c…` simulation-only分钟累计/会话初始化与精确48次timer release 已安装并切 current；`f81acb9…`及更早验证release保留 | current 只指TA模拟入口，不等于durable capital、broker或真实交易 |
| 服务身份 | UID/GID 987、专用 token-file、正式 18082 认证可用 | token 可读不等于任一 dataset 可用 |
| 数据验收 | 三核心日频 manifest 和 `20260724` observation/重放 PASS；`rt_min` 已有30只主板真实30/30回读；上游50/100/200只分片压测完整 | 500只动态分片尚未正式发布/回读；分钟数据约晚一根K线，不是历史PIT、训练样本、L1或低延迟执行证明 |
| 交易能力 | front inactive/disabled 且 runtime-masked，8787 closed；30只 delayed-paper 手工 one-shot 已保存模拟状态；分钟与会话timer enabled/active，但首轮分钟自动触发因当日缺口失败关闭且账本不变；无 broker 或真实交易 | 启用调度不等于已有成功自动模拟轮次、durable capital 或真实执行 |

旧 `8082` listener 仍由旧系统所有者保留，当前 observation consumer 没有探测或
fallback 到该端口。legacy front drop-in 已移出 active systemd 目录，front base
unit 已与当前仓库字节一致；active unit 中旧 `8082`、`SharedSignals`、
`/opt/tradingagent` 和 `marketgraph` 身份引用均为零。TradingDatas owner 已
启用其自身通用5分钟 collector timer；TradingAgent 只读消费 18082，只启用
simulation-only分钟/会话timer，不负责修改 TradingDatas 采集调度。

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
   资金统一组合、订单统一净额；仓库 fixture 已能对主板样本生成未经校准的
   个股排序，并分别运行 baseline/event/flow/dynamic-position 反事实账本。
   这些分数、账本和成交均不可冒充概率、真实行业特征、生产策略或资金 authority。

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
- 当前日频 snapshot 只证明单次 current observation；`rt_min` 当前只证明
  30只主板的5分钟 OHLCV/amount 读取和既有小批连续性。二者都不是可训练历史
  PIT，也没有绑定 durable capital/outbox 或 TA 生产 worker。
- `rt_min` 约晚一根完整K线且不含 bid/ask；当前只用于数据与非生产模拟样本
  积累。保守下一可达K线策略已在手工 one-shot 中运行；自动 timer 已启用，但
  当日中途缺口使首轮自动触发失败关闭且不改账本。下一交易日输入已完成隔离
  30/30初始化与幂等重放，但正式状态仍必须由09:20 initializer冻结，再从首根
  连续K线启动。durable capital 或更高权限仍需独立门禁；连续5个交易日用于
  稳定性与扩容复核，不再阻塞首批模拟决策。
- front 继续停止；本阶段不恢复 `tradingagent.cc` 页面。tracked base unit 已安装
  但保持 inactive/disabled/runtime-masked，旧 drop-in 已退役。分钟累计已有
  独立 current pointer，simulation-only分钟/会话timer已激活；observation worker、
  durable capital 与真实执行均未激活。
- 专用 UID 987 已通过新的 root-owned versioned Python runtime 执行真实入口和
  audit；旧 `/opt/tradingagent/venv` 不再被 TA active unit 引用。动态 manifest
  rollover 已完成失败关闭和恢复后 PASS 验收，手工 one-shot 与幂等重放已完成；
  分钟自动累计的 tracked unit、secret-free env 与 `current` 指针已安装并启用。
  首次自动触发已证明缺口下失败关闭且bundle不变；真实次日pre-open初始化、首个成功
  自动轮次、崩溃恢复与连续运行验收仍未完成。次日隔离预检已证明30只参考输入
  可生成且精确重放，但它不替代09:20正式timer读回。
- 旧 8082、旧服务器 runtime 和退役文档只能按各自 ownership 与证据链清理；
  不以删除代替依赖清零证明。

## 下一阶段入口

依赖顺序固定为：

1. 保持 TradingDatas 30只主板/5MIN collector 连续运行，核验午休、下午恢复、
   全天48个bar slot的完整率、重复/冲突和失败receipt；首次真正可达K线模拟成交、
   资金/持仓对账和重复快照失败关闭已完成，继续积累当日手工样本；
2. 用现有通用采集链刷新 `daily`、`stk_limit`、`suspend_d`、`adj_factor`；
   数据集逐项 fresh/non-degraded 前不进入自动模拟成交；
3. TradingDatas 以100只/分片完成动态500只 `coverage_canary` 的500/500正式
   回读；500只不能按证券代码前缀偏置抽样，也不能冒充中证500、研究代表性样本或
   交易Universe。500稳定后扩到全部合格沪深主板，仍不新增公共route；
4. 等 `index_classify`/`sw_daily` 恢复健康后，独立加入行业上下文，不阻断核心
   主板 observation，也不把汇总数据冒充个股权限；
5. 当前真实 delayed-paper one-shot 已完成首次可达K线结算与重复快照不变复核；
   专用 TA 分钟 timer 与次日会话初始化 timer 已安装并启用。下一停止线是在下一
   交易日09:20核验正式目录与隔离预检一致，并从09:35首根数据到达后的09:40
   调度开始验证首轮、下一轮、重复轮、崩溃恢复和盘后
   停止；缺参考快照、分钟缺口或任何数据退化时继续失败关闭。连续5个交易日是
   稳定性/扩容复核门，不再阻塞首批模拟决策。
   durable capital 或更高权限的自动 paper
   仍须完整数据、日历、执行与资本权威通过后开放
   `Signal -> TargetPosition -> Risk -> PaperFill -> Reconcile -> Attribution`
   闭环；
6. 只有长期样本、校准和回撤门禁通过后，才讨论模型晋级；真实交易继续保持关闭。
