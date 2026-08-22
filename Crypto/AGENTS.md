# tradingagent/Crypto

> **阅读顺序：** [AGENTS.md](../AGENTS.md) → [STATUS.md](../STATUS.md) → 本文件

## 目标
加密货币交易模拟盘/影子盘，按 7x24 市场语义建设持续观察和复盘能力。现役
delayed-paper core 与 G5 detached learning/scrub units 只在 simulation/shadow
边界内运行；监控频率、数据覆盖和策略有效性仍必须由后续样本证明，不能把“高频训练”
写成已实现能力或交易 authority。

## 现有代码
- `tradingagent/Crypto/` 内为现役实体代码，不再依赖 `/opt/investment/Crypto/tools/` 旧目录。
- 当前资本写能力只有 `fixture_auto_sim.py`/`fixture_sim/` 本地非权威纵向切片。`delayed_paper_runtime.py` 已作为 sim-only 核心随 `e8ba46d7e0cab847d0fa037290e7368c69c54655` 发布，并由主集成在 2026-07-28 验证 one-shot、幂等重放、相邻自动轮和 timer enabled/active；这只证明本地 delayed-paper 自动积累，不授予 Testnet/live/production execution authority。`delayed_paper_learning.py` 与 `delayed_paper_learning_worker.py` 由现有 G5 learning/scrub units 作为 detached offline 路径消费；其当前 release、enablement 与 readback 以 `STATUS.md`/`AUTODEV_STATE.json` 同轮事实为准，不从本文件推断。旧 workflow/simulator/executor/shadow writer 已退役为 tombstone；其余 strategy/validation/report 只作研究辅助。
- 数据源只读 TradingDatas 的 `GET /v1/catalog` 与 `POST /v1/query`；TradingDatas fresh handoff 前只允许显式 fixture/mock。不得由 Crypto 直接调用 Binance、读取 TradingDatas SQLite，或回退到 `/tushare`、`/source_status`、provider 专用 route。

## 特点
- 目标市场语义为 24/7、无交易所统一休市；这不表示所有可选任务都已安装。
- 5min delayed-paper 核心与现有 G5 learning/scrub units 的服务器 enablement、immutable
  release 和 timer 状态必须分别从 `STATUS.md`/`AUTODEV_STATE.json` 同轮读回；仓库文件存在、
  install-default 或 `[Install]` 不能替代当前运行证据。模拟盘上线要求最近 48 小时窗口覆盖率
  至少 90% 且无完整性错误；旧 epoch 历史缺口只作审计，不得拖住新的可观察模拟积累；策略样本质量
  仍只由后续运行证据证明。
- server-local paper、Binance Spot Testnet 和未来 Binance Spot Live 是三份不同合同、账户与凭据域；不能靠切换 base URL 或环境变量升级。当前 `REAL_TRADING_ENABLED=false`，Live adapter 未实现。

## 当前模块边界

- `five_minute_data.py` 是 Crypto 专属、transport-free 的 TradingDatas
  catalog/query consumer port。它只接受显式注入的 typed client 和 catalog
  profile；当前 profile 只能在 fixture 中绑定 BTC/ETH 各自的 5m bar 与
  instrument-rules 四个候选 dataset，checked-in 配置仍保持未配置。每个 dataset
  必须独立完成 catalog freeze、bounded query、分页完整性、same-observation、
  receipt/lineage/freshness/quality 门禁。Binance kline 的 source
  `close_time` 按 inclusive last millisecond 解释，再派生本地逻辑 5m
  boundary；不得虚构 `closed`、`frequency` 或 `active` 上游字段。
  typed snapshot 在进入 runner 前还必须重新绑定精确 profile、四份
  symbol/kind/dataset/catalog proof、请求窗口、cutoff、row/page budget 和
  freshness；仅重算本地 digest 不能把其它窗口或 future proof 带入资本链。
  上游数据合同代码已合入 TradingDatas
  `main@62d76f8cdcc7671a9523ac15905ab2eb3152e387`；isolated canary
  `025fd24…` 已证明
  `symbol eq + open_time between + as_of + desc + limit=13` 返回精确连续窗口，
  后续主集成已完成 18083 正式 handoff 和核心 delayed-paper 自动轮验证。
  provider-neutral consumer 仍必须执行 bounded cursor traversal；non-null
  cursor 可继续遍历，循环、跨页重复、预算超限或最终窗口不完整才失败关闭，
  禁止忽略 cursor 或接受截断首屏。
- `delayed_paper_runner.py` 只编排已验证 snapshot、Crypto 本地 audit-only
  Decision Ledger 和现有 `run_fixture_auto_sim` 唯一资本写入口。前 12 根 bar
  形成 1h/15m 决策，第 13 根已闭合 bar 仅提供其 open 作为
  `next_closed_bar_open_counterfactual`，并明确使用本地 spread model；实际
  source `observed_at` 是最早可用水位，quote/decision/order/receipt 不得早于
  它。两个 symbol 必须在任何资本调用前全部 preflight；完整 runner cycle
  进程锁与 pending invariant 禁止多个未完成 observation。已有仓位的后续 buy
  由唯一 fixture capital writer 形成 mark-only risk reconcile，不增加仓位。
  同一 observation 的两份合格 fixture 必须共同形成绑定 receipt/digest 的
  account valuation context，并在每次 core 调用中同时更新全部持仓 mark；
  停机或数据拒绝造成的多 slot 间隔不得因另一持仓旧 mark 卡死恢复。
  `delayed_paper_ledger.py` 只保存分段、原子、可恢复的审计事件，不保存或计算
  现金、仓位、订单或成交，不能成为第二资本 authority。
- `delayed_paper_runtime.py` 只接受仓外、secret-free、完整冻结的
  `CryptoFiveMinuteDataProfile` manifest。manifest 必须绑定 catalog version、
  profile SHA、四个 dataset contract SHA 与 loopback IP literal base URL；
  runtime 不从当前 catalog 动态重建或放宽 profile。token leaf 固定为
  `/run/secrets/tradingagent/tradingdatas-crypto-read.token`；输出根只能来自后述
  已验证 outage-epoch context，旧
  `/var/lib/tradingagent/crypto-delayed-paper` 不再是可写 runtime root。HTTP
  transport 在 runner 确认没有
  pending observation 后才懒构造，因此 pending 崩溃恢复不依赖 token 或
  TradingDatas 可用性；若当前请求时槽更新，同一 invocation 最多按顺序处理
  `pending recovery + 1 fresh` 或两个缺失 fresh window，超过预算显式
  `backlog_pending`，下轮继续，不能跳过中间时槽。恢复回执绑定旧 observation
  的 slot/profile，不能由当前 manifest 冒充；实际 wire 仍只有
  `GET /v1/catalog` 与 `POST /v1/query`，无任何 provider/SQLite/fallback。
  相同 5m slot 的
  `window_end` 与 `observation_cutoff` 固定为 bar close +55 秒，不能随 systemd
  jitter 或重跑墙上时钟漂移。current epoch 已有资本且下一历史 exact-as-of
  明确不可恢复时，只允许 runtime 在 pending 为空、当前 13 根窗口完整且全部
  receipt/lineage/freshness/quality 门禁通过、资本链守恒后追加 checksum-bound
  `data_gap`；该事件保存精确跳过范围、拒绝原因、source proof 和首窗
  observation/counterfactual，但不调用资本 writer、不生成候选/订单/成交。
  同槽重放必须验证 gap event/index/ledger 与资本锚点且不得重复；下一根连续窗口
  才恢复原核心。其它错误、证据不完整、pending、gap 或资本篡改一律 fail closed。
- generation-1 运行中断造成 TradingDatas 当前 envelope 无法证明历史 `as_of` 时，
  不得改写 observation state、跳过旧 root 的缺口或降低 PIT/freshness 门禁。
  当时的恢复只使用显式 `delayed_paper_epoch.py` 合同：旧
  `/var/lib/tradingagent/crypto-delayed-paper` 只读封存；仓外 current-epoch
  manifest 绑定唯一 `epoch_id`、本次停机恢复专用 `epoch_generation=2` 和
  `/var/lib/tradingagent/crypto-delayed-paper-epochs/<epoch_id>` 独立 root。
  新 root 用 `crypto-capital-v1` 的 10,000 USDT local fixture opening baseline
  重新开始，但旧、新 epoch 的资本、持仓、订单、PnL、收益率和样本禁止聚合。
  epoch parent 的 `.current_epoch.json` 以 checksum 和进程锁永久钉住唯一
  generation-2 root；同 generation 换 root、回退 generation 或改写 manifest
  均失败关闭。未来新 epoch 必须经新的独立合同与候选，不能靠改 manifest 轮换。
  `delayed_paper_epoch_runtime.py` 是唯一 server wrapper；它先持久化并验证 current
  anchor 与 root identity，再调用核心。旧 `delayed_paper_runtime.py` 直接 CLI
  和无 context 的 Python 调用均已退役为 fail-closed。systemd 只可把一个现役
  service/timer 指向 current epoch，并将旧 root 绑定为只读；回滚只能停新 timer
  并保留两份账本，不能恢复旧 root 写入。
- 学习投影不属于核心 runtime。`delayed_paper_runtime.py` 不得 import、调用或恢复
  learning，也不得读取或创建 `evolution/`；核心回执固定声明
  `learning_mode=detached_offline_worker`、`learning_authority=false`、
  `learning_invoked=false`。独立 `delayed_paper_learning_worker.py` 只消费已完成
  observation/completion，正常轮通过 append-only checkpoint 处理至多一条新增
  completion；出现多条缺口必须交给 daily full scrub。full scrub 校验全部
  completion→projection receipt→sample/KPI/Challenger segments 及 checkpoint
  链；未声明投影可确定性补齐，已声明 receipt/segment 缺失、旧段篡改或链断裂
  必须失败关闭。仓库中的学习 service/timer 与 daily scrub service/timer 安装默认值
  为 disabled；这只是安装安全默认值，不是现役生产状态或长期阶段门禁。现役状态必须
  以同轮 systemd、immutable release 和 checkpoint readback 为准。worker 只能通过固定
  `/etc/tradingagent/crypto-delayed-paper.epoch.json` 加载并验证唯一 current
  epoch，不接受自由 output root；tracked service 静态钉住经复核的 epoch，
  只给其 `evolution/` 写权限，未来换 epoch 必须人工更新 unit。发布侧先在 timer
  disabled 下完成同根 full scrub、幂等 replay、unit/root/rollback 核对，再由 Controller
  可回退地启用并读取自然增量。最新连续 288 根/24 小时只约束 runtime maturity 以及
  后续 promotion/risk/execution，不阻断完整 segment 的离线因子/策略评估或学习积累；
  任何学习失败都不能改变核心 status、exit code、资本、Champion、
  风险或订单。
- `fixture_auto_sim.py` 是薄兼容 facade；实现位于 `fixture_sim/`。该网络关闭纵向切片只接受显式 fixture/mock，以 1h regime、15m decision、closed 5m 证据及 observed-at-or-later executable quote 生成冻结 Champion 的本地 `fixture_simulated` intent/receipt，并写入 Crypto 自有 append-only 资本链、对账和非晋级复盘；它没有 execution authority，也不是 TradingDatas adapter、scheduler、Testnet 或 Live runtime。
- 本批纵向切片是 `crypto-capital-v1` 本地 fixture opening 闭环的唯一可写入口，但仍固定为 `local_fixture_simulated_candidate`，没有 execution/runtime/live authority。旧 `crypto-shadow-sim-v1` 仅保留历史证据。
- ledger 默认构造只读，只有 `fixture_sim/runtime.py` 可通过包内工厂取得写 capability；checksum、文件锁和进程内 capability 仅是协作与损坏防护，不隔离可改代码或文件的同 UID 恶意/失控进程。生产前必须另做单 writer inventory、OS 权限/进程隔离和外部 durable receipt 验证。
- `workflow.py`、`simulator.py`、`sim_executor.py` 与 `shadow_runner.py` 是无条件 fail-closed tombstone；注入 reader、配置或旧账户也不能恢复信号或成交写入。
- `market_data.py` 只接受显式注入的 TradingDatas V1 证据，不得恢复旧 provider 专用入口。
- `adapter.py` 只保留显式 reader 下的 market/universe/strategy 研究映射，不拥有资金、成交、Testnet 或 Live authority；未来三类 broker adapter 仍须分别实现，不能复活 tombstone。
- `capital_policy.py` 是 `crypto-capital-v1` 原生 10,000 USDT 本地 fixture opening baseline 的单一代码来源；它不是 execution、durable receipt、production 或 live capital authority。`config.yaml` 只声明账户币种和风险参数，shared kernel 只能引用而不能另设数值。
- `report.py` 与 `validation.py` 只生成研究辅助证据；`promotion.py` 是只读 legacy scorecard，永久 `eligible_for_sim=false`、`promotion_authority=false`，不能自行晋级或扩风险。十币种自动晋级（模拟域）由 `ten_symbol_factor_strategy_evaluation.py` 的 champion 块与 `champion_promotions/` receipt 承担，实盘仍由 `REAL_TRADING_ENABLED=false` 硬闸。
- LLM sidecar 必须在核心 cycle lock 之外独立追加并限制读取大小；损坏或写入失败只形成无权威 degraded 诊断，不得回滚、重复或阻断已提交的核心资本与 bundle replay。
- 现役 generation-2 epoch 仍是 `crypto-capital-v1` buy/observe-only，禁止把
  新候选写入其 root。`round_trip_capital.py` 与
  `delayed_paper_round_trip.py` 只为独立 `crypto-round-trip-capital-v1`、
  capital generation 2 提供可回放的 buy/sell 模拟资本链；固定 10,000 USDT
  新 baseline，`aggregate_with_prior_generations=false`，不读取或迁移旧现金、
  持仓、订单、PnL。冻结退出规则是 +3% 止盈、-2% 止损、最长 24h，以及
  `observe` 且 1h/15m return 同时小于 0；卖出使用下一根已完成 5m bar 的因果
  quote、2bps 保守滑点和既有 0.1% taker fee。部分/拒绝回执同样进入新账本，
  但仍固定无 execution/production/live authority。
- `delayed_paper_round_trip_epoch.py` 创建独立 epoch-g3，或在 g3 已冻结而 g2
  合法前进后创建显式 g4 successor；两者都不写旧 g2 的 `.current_epoch.json`，也
  不迁移/聚合历史账本。g4 必须绑定不可变 g3 manifest/receipt 与新的 g2 head，
  不得复用或改写 g3 root。`delayed_paper_round_trip_runtime.py` 只从正式
  TradingDatas closed-5m manifest 运行隔离 epoch 的新/pending cycle，并在写入前后
  双重校验 g2 archive 与 epoch identity；专属 systemd unit 默认不启用，
  发布验收的 one-shot、同槽重放与相邻轮通过前不得切换任何 timer。
- `delayed_paper_exit_shadow.py`
  只能从已验证 completion、run bundle 与 capital head 生成止盈、止损、最长持有
  和动量转弱的完整往返反事实；它不得写资本、订单、成交或修改历史 bundle。
  `delayed_paper_health.py` 只生成 no-write 健康快照。两者都固定
  `authority=none`，只能作为 round-trip generation 的对照，不能成为 order、
  receipt、退出触发或资本事实的输入。
- DeepSeek/LLM 只能作为 `offline_fixture`、`authority=none`、`network_used=false` 的独立 sidecar journal；改变其文本不得改变或阻塞核心 replay、Champion、decision、OrderIntent、数量、费用或资本状态。
- `factor_research.py` 只可作为纯函数、read-only 的研究层：它消费已经验证的
  13 根 closed-5m OHLCV 窗口和未来已观察价格，生成证据绑定的特征/标签/固定
  Challenger 比较，不得读写 core、capital、orders、Champion 或 `evolution/`。
  当前只有 BTC/ETH，任何横截面 factor/IC 声称均不成立；只能做时间序列特征
  研究。`build_factor_snapshot`/`build_forward_label`/
  `evaluate_factor_hypotheses` 另有 keyword-only 可选参数（universe 与
  feature set identity），默认值保持 v1 冻结行为且 v1 调用路径字节不变；
  只有 v2 投影模块显式传入 10 币 universe 与
  `crypto-5m-ohlcv-factor-research-v2`。历史回填不具备 PIT 证明时只能用于
  工程/定义检查，不得进入晋级证据。
- **Universe 版本化（10 → 40）。** 上述 10 币链是已落盘 append-only 历史，
  只读封存、不回写；`OBSERVATION_SYMBOLS` 冻结为 10 币，不得原地扩。新增覆盖
  必须使用 `OBSERVATION_SYMBOLS_V40` + 独立 `forty_symbol_*` 契约族 + 独立
  store root `/var/lib/tradingagent/crypto-40-symbol-observation`。40 币观察链
  由 `forty_symbol_observation_runtime.py` 的独立 systemd 候选运行，仍固定
  `authority=none`、无晋级和资本权限；40 币因子投影继续保持 detached，不接晋级。
  40 币因子投影用 feature set `crypto-5m-ohlcv-factor-research-v3`、consumer
  profile `crypto-5m-ohlcv-13bar-forward-labels-v3`、投影命名空间
  `evolution/forty_symbol_factor_research/`，不写旧 `evolution/ten_symbol_factor_research/`。
- `ten_symbol_observation_store.py`、`ten_symbol_observation_profile.py` 与
  `ten_symbol_observation_runtime.py` 组成独立的 10 币 5 分钟 shadow 观测
  积累器，为后续横截面 factor research 提供前向积累的证据级数据源。它与
  delayed-paper core/learning/factor 完全不共享 root、锁或状态，任一故障域
  互不影响；固定 `authority=none`、零 capital/order/model/promotion 权限。
  store 是 append-only checksum 链账本（observation/data_reject/data_gap）；
  terminal observation/data_gap 同槽重放幂等、同槽异 payload fail closed，
  非 terminal data_reject 则按确定性 attempt/event ID 幂等追加，允许同槽不同失败
  原因被分别保留，避免瞬时失败变化永久卡槽。profile 冻结 10 个 bar dataset
  各自的 canonical catalog contract fingerprint、统一 consumer 查询形状与
  外层 SHA；目标 dataset 合同漂移 fail closed，无关 dataset 引起的全局 catalog
  version 前进不阻塞积累，本轮 query 必须绑定同轮实际观察到的 catalog version。
  runtime 只接受仓外冻结 manifest 与固定
  token leaf，懒构造 transport，输出根只能来自 manifest 绑定的
  `/var/lib/tradingagent/crypto-ten-symbol-observation`；slot cutoff 固定
  bar close +55s，每 invocation 最多 2 cycle，并受 120 秒绝对 wall-clock
  budget 约束；每次 wire timeout 都压缩到剩余预算，预算耗尽保留 pending 与已完成
  增量，不能误记为数据拒绝。单次 fresh 采集只对传输层瞬时错误
  （timeout/connection 类及其包装链）做有界同槽重试：最多
  `MAX_COLLECT_ATTEMPTS=3` 次、固定间隔 20s、每次完整独立构造
  transport+client、cutoff 不随重试漂移；数据合同/校验失败、HTTP 状态错误
  （含 401/403，永不重试）与预算耗尽信号立即失败，全部失败仍走原
  fail-closed 路径。候选 timer 固定错开现役 core 的 close+55s，
  在 close+3m25s 启动，居中放在相邻两次 core cadence 之间。120 秒绝对预算从
  invocation 开始计时并包含全部 wire attempts 与 retry sleep；进程启动/停止开销
  不在该函数预算声明内，因此不能单靠静态时间计算宣称绝不会重叠；每次发布仍须
  用前一 core、ten-symbol reader、后一 core 三次自然读回证明共享
  token/API/SQLite surface 没有并发。积压返回
  `backlog_pending`，若本轮已有有序进展则保留明确的 lag/backlog JSON 证据并按
  信息项成功退出，下一轮从最早缺口继续；若本轮 0 cycle 且预算耗尽则仍非零，
  以显式暴露没有进展的运行时问题。不把有序数据滞后冒充状态完整性失败。
  非零退出且不跳槽；历史窗口对 current-read watermark 门禁确定不可恢复时，
  只允许在当前窗口全部门禁通过后追加显式 `data_gap`，不伪造 PIT。证据只能
  前向积累，历史回填不构成证据。fresh 采集成功后 runtime 先把该槽 10 币
  13 根原始 bar 行原子写入 immutable bars sidecar（`bars/<slot>.json`，
  canonical JSON、tmp+rename+fsync、同内容幂等、异内容 fail closed）再落账
  事件；data_gap 恢复首窗同样按恢复槽写 sidecar。sidecar 携带每 source 的
  原始行与 receipt/digest 元数据，消费方可独立重算
  `identity_sha256`/`market_data_sha256` 并与 store 事件逐值比对；crash
  留下的孤儿 sidecar 下轮零网络复用，sidecar 本地校验失败一律 fail
  closed，绝不记为 data_reject。观测事件合同与 digest 定义不变，bar 行不
  进入任何事件 digest。对应 systemd unit 是 install-default 不启用
  的候选；安装/启用必须经 Nicholas 明确批准。
  每槽在 bar 采集成功后追加采样 10 个
  `crypto.spot.binance.<symbol>.book_ticker` 快照（best bid/ask 与 qty）
  作为附加、降级容忍的实测点差证据：spread leg 用独立 client（只配置
  book_ticker dataset id）与独立 catalog 读，其任何失败（per-symbol 记
  `rejected` + reason code，leg-wide 记 `unavailable` + reason code）绝不
  触发 bar 重试或丢失 bar 观测，预算耗尽信号永远穿透。快照行、receipt
  `observed_at`（唯一时间权威，必须在槽结束与 cutoff 之间，与 bar 同一
  watermark 门禁）/freshness 元
  数据与实测 `catalog_contract_sha256` 写入 immutable
  `spreads/<slot>.json`（写在 bars sidecar 之后、事件之前）；
  `observation`/`data_gap` 事件新增 `spread` 状态块（contract
  `tradingagent.crypto.ten_symbol_observation_spread.v1`，status ∈
  completed/degraded/unavailable + `spread_sha256`），快照行不进入任何
  observation digest。契约演进不引入新版本号：event/profile contract v1
  均不变，book_ticker 指纹每槽实测记录而非冻结进 profile，服务器既有
  manifest 继续有效；旧槽无 `spread` 键视同 feature-ineligible。零网络恢
  复路径绝不重采样，spreads sidecar 缺失记
  `crypto_spread_sidecar_missing`、校验失败 fail closed。点差落账后由
  独立 detached 只读投影 `ten_symbol_spread_projection.py` 消费，不接
  策略/factor 投影 record。
- `ten_symbol_factor_research.py`/`ten_symbol_factor_research_worker.py`
  是 10 币观测积累器的 detached offline 因子投影（v2）：只读观测事件链与
  bars sidecar，投影根固定 `<store_root>/evolution/ten_symbol_factor_research/`
  （records/receipts/labels/checkpoints、immutable 写、checkpoint hash 链，
  机制镜像 `delayed_paper_factor_research.py`）。每个 terminal 槽先从
  sidecar 原始行独立重算每 source 的 `identity_sha256`/
  `market_data_sha256` 并与 store 事件逐值比对；sidecar 缺失或 digest
  不符的槽永不投影、视同 gap 切断 segment（checkpoint 记录
  `sidecar_ineligible`），也不让 label 跨段结算。冻结 consumer profile
  `crypto-5m-ohlcv-13bar-forward-labels-v2`（10 symbol、required horizon
  60min、aux 240/720/1440、feature set
  `crypto-5m-ohlcv-factor-research-v2`）；三个预注册假设不变，横截面只加
  标注为 context 的 1h/15m return 排名描述，不加新假设。incremental 不回填
  label，落后时单次 invocation 按槽序有界自恢复（最多
  `MAX_CATCHUP_UNITS=12`，checkpoint 与 terminal 槽 1:1，绝不跳槽）；
  追平返回 `projected_incremental`，仍落后返回非错误 status
  `backlog_remaining`（退出码 0），segment 逐 unit 滚动且与 full scrub
  `_segment_ids` 语义一致。daily full scrub 仍是唯一全链校验、补 record、
  结算同段到期 label 并出 hypothesis report 的路径；超时走可重试
  deferred debt。worker 只绑定固定
  `/etc/tradingagent/crypto-ten-symbol-observation.runtime.json` 推导
  store root，不接受自由 output root，执行前后重验 manifest 字节与 root
  identity。固定 `authority=none`，零 core/资本/order/Champion/learning
  写权限；50 标签初筛不构成 edge 或晋级授权；费用后策略评估由下游
  `ten_symbol_factor_strategy_evaluation.py` 承担。对应
  systemd unit 是 install-default 不启用的候选；安装/启用必须经 Nicholas
  明确批准。
- `ten_symbol_factor_strategy_evaluation.py` 是 v2 投影的 detached 费用后
  策略评估（v1 `factor_strategy_evaluation` + post_projection 的 v2
  port），只可作为 full scrub 的下游运行：从投影根重建全部 resolved
  (snapshot, label) 样本（required 60min + auxiliary 240/720/1440min）并
  逐样本按 v2 合同核验（snapshot/label integrity、同 segment、
  record/receipt/checkpoint 三件套、store 事件 checksum 与 bars sidecar
  sha 双重绑定、checkpoint 链重放、cost policy
  `crypto-round-trip-taker-v1` 逐项比对、gross/net 重算、future 不晚于
  evaluation_as_of），失败一律 fail closed 不跳过。成本滑点腿接入
  `ten_symbol_spread_projection` 的 checkpoint 绑定 artifact：逐单元
  取不晚于样本槽日的最近充足日桶（sample_count ≥ 12）p75 半点差作
  每边滑点，fee 口径不变；投影命名空间缺失或桶样本不足时显式回退
  假设成本并逐单元标记 `cost_source: assumed|measured`（bundle 内
  `cost_model`/`cost_source_counts` + immutable 伴随
  `strategy_evaluation_cost_attributions/{outcome}.json` 逐单元清单），
  链校验/合同漂移 fail closed；评估 outcome 身份纳入所消费 spread
  outcome，实测证据更新即重评估。每个 horizon × 每个
  预注册假设各产出一份评估，在同一 immutable bundle 内按 horizon 分组
  （always-invest 基线、cash 基线、signal/abstention/coverage/hit_rate/
  cost_adjusted_net_return/baseline_delta/cash_baseline_delta/drawdown/
  turnover/round_trip_leg_rate），aux horizon 标注
  `research_attribution=true` 仅作方向性证据，样本不足的 horizon 报
  `insufficient_resolved_samples` 不产出评估也不 fail；**recommendation
  只基于 required 60min 口径**，∈ {disable, downweight,
  retain_for_more_evidence}，`evaluated_status` 固定
  `exploratory_insufficient_edge`。required 60min 口径完成后在模拟域内
  **自动选 Champion**：按 highest positive cost-adjusted net return 选
  signal>0 且费用后净收益>0 的最优假设，同收益按名字确定性 tie-break；
  bundle 写入 `champion` 块（`champion_id`/`champion_sha256`/
  `simulated_capital_authority_id=crypto-round-trip-capital-v1`/
  `simulated_capital_allocated=true`）与 immutable
  `champion_promotions/{promotion_receipt_sha256}.json`（
  `automatic_champion_replacement=true`、`promotion_authorized=true`、
  `automatic_risk_expansion_enabled=false`、`real_trading_enabled=false`、
  `authority=none`）；无合格候选则 `champion=null` 且不写 receipt、不
  自动替换。bundle 写入
  `strategy_evaluations/{outcome_sha}.json`（outcome 覆盖含 aux 在内的
  全部 resolved 样本集合）并用 compact
  `strategy_evaluation_checkpoint.json` 幂等：同 outcome 返回
  `no_new_outcome`，0 resolved 返回 `insufficient_resolved_samples`。
  metric_basis 按 horizon 标注重叠率与有效独立样本折算（11/12、47/48、
  143/144、287/288），HAC/非重叠子样本显著性留待后续。worker 的
  incremental 模式只走 compact checkpoint 快速路径（首次 scrub 前明确跳过
  而非 fail closed）；评估失败只记为可重试 debt，绝不改变已完成 scrub
  的事实与退出码。固定 `authority=none`，零 core/资本/order/Champion
  写权限；自动 Champion 只作用于模拟域，实盘仍由
  `REAL_TRADING_ENABLED=false` 硬闸，不授予 edge、参数变更、风险扩张或
  执行授权。
- `ten_symbol_spread_projection.py` 是 spreads sidecar 的 detached 只读
  消费投影：镜像 bars sidecar 消费路径（事件 `spread` 状态块
  shape-check + sidecar 重算 + `spread_sha256` 逐值比对），把实测
  book_ticker 点差聚合成 symbol × UTC 自然日的样本级成本证据研究
  artifact（bps 均值/中位数/p25/p75/min/max、拒收率、时间覆盖，1e-8
  bps 量化、type-7 分位数）。投影根独立固定
  `<store_root>/evolution/ten_symbol_spread_projection/`（artifacts/
  + compact checkpoint，outcome 键控 immutable 写，幂等重跑）；
  sidecar 缺失只显式剔除该槽并记录，sidecar 损坏或 digest 漂移一律
  fail closed；rejected 条目只计拒收统计，绝不进入点差统计。固定
  `authority=none`、`research_only=true`、零网络、零 core/资本/order/
  Champion/learning 写权限；投影自身不接任何策略/评估逻辑，费用后
  评估对 artifact 的消费已在
  `ten_symbol_factor_strategy_evaluation.py` 按上述合约实现。
- `ten_symbol_research_loop.py` 是研究进化闭环第一阶段：离线、只读、
  detached 一次性假设重估调度器。输入为观测 store 根目录与预筛
  horizon 配置；复用 factor v2 投影的 sidecar 资格门禁与
  `ten_symbol_factor_prescreen.analyze` 评估口径（不改评估逻辑、不
  复制实现），把四个已注册预筛候选在最新观测证据上重估，产出
  `<store_root>/evolution/ten_symbol_research_loop/` 下 checksum 绑定的
  immutable 评审报告（每候选×horizon 费用前后指标、与上份报告的
  diff、自动 `review.recommendation`：逐候选按最优非重叠费用后净收益
  推导 `auto_promote`/`auto_demote`/`auto_retain`）与
  compact checkpoint；同输入重跑 `no_new_input` 且字节不变（幂等），
  链/sidecar/checkpoint/report 篡改一律 fail closed。第一阶段不生成
  新假设（注册集合漂移 fail closed）、不接 systemd、无 worker；晋级
  在模拟域内自动，实盘仍由 `REAL_TRADING_ENABLED=false` 硬闸。固定
  `authority=none`、零 core/资本/order/Champion/learning 写权限，不构成
  edge、参数变更、风险扩张或执行授权。
- `ten_symbol_health_watch.py` 是十币种观测链与 TradingDatas 数据面的
  只读健康检查器：只经 store lock-free 只读路径读取，绝不重建
  head/index、绝不写任何 store 文件；检查 latest_terminal_slot 滞后
  （600s degraded / 900s failed）、最近 12 槽 reject/gap 占比
  （>0 degraded / >0.25 failed）、spreads sidecar 采样完整性（缺失
  degraded、校验失败 fail closed）与 TD catalog/query 活性及
  bars/book_ticker/open_interest freshness。输出单份机器可读 JSON
  （contract `tradingagent.crypto.ten_symbol_health_watch.v1`），退出码
  0/1/2。固定 `authority=none`、零 core/资本/order/Champion/learning
  写权限；无 systemd unit，安装/启用须经 Nicholas 明确批准。
- `ten_symbol_hypothesis_generator.py` 是研究进化闭环第二阶段：离线、
  只读、detached 一次性假设生成器。仓内冻结版本化生成配置
  （`crypto-ten-symbol-hypothesis-generation-v1`，五族 B 类因子、每族
  ≤5 组参数、horizon 12/48/144/288，共 23 候选）确定性展开候选假设，
  逐候选做轻量可行性检查（ohlcv_bars 由已验证 store 链实测样本量，
  realized_spreads/open_interest_5m/premium_index 三 plane 只能由严格
  校验的调用方数据面 manifest 声明，未声明即 unavailable），产出
  `<store_root>/evolution/ten_symbol_hypothesis_generator/` 下 checksum
  绑定的 immutable 注册提案（feasible 候选自动
  `registration_status=auto_registered`、
  `registered_into_prescreen/registered_into_evaluation=true`，review 自动
  `auto_register`/`blocked`）与 compact checkpoint；同输入重跑
  `no_new_input` 且字节不变（幂等），配置漂移、manifest/链/checkpoint/
  提案篡改一律 fail closed。第二阶段对 feasible 候选自动注册进预筛集合
  （blocked 候选未注册、预筛/一阶段注册集合漂移仍 fail closed）、不运行
  任何评估、不接 systemd、无 worker；晋级在模拟域内自动，实盘仍由
  `REAL_TRADING_ENABLED=false` 硬闸。固定 `authority=none`、零
  core/资本/order/Champion/learning 写权限，不构成 edge、参数变更、
  风险扩张或执行授权。
- `delayed_paper_factor_research.py`/worker 只能从受版本化 G4 manifest 绑定的、
  已完成 observation/completion 建立独立 `evolution/factor_research/` 追加投影；
  不接受自由 output root。已验证的完整、连续且 gap-bounded segment 可以进入 detached
  offline projection/evaluation；缺失、篡改或链断裂必须 fail closed。最近连续 288 根
  completion 只约束 automatic runtime maturity 及后续 promotion、风险扩张和执行，
  不阻断完整 segment 的离线投影/评估。没有 service/timer、核心/capital/order/Champion
  或 `round_trip_learning` 写权限，50 标签初筛也不构成 edge 或晋级授权。
- 旧 `/opt/investment/Crypto/tools/` 名称清单已从仓库删除；历史实现只从 Git 或独立只读归档审计，不再维护第二份 manifest。
