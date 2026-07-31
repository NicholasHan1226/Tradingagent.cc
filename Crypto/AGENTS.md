# tradingagent/Crypto

> **阅读顺序：** [AGENTS.md](../AGENTS.md) → [STATUS.md](../STATUS.md) → 本文件

## 目标
加密货币交易模拟盘/影子盘，按 7x24 市场语义建设持续观察和复盘能力。当前只验证 fixture/mock 下的候选模块；监控频率、数据覆盖和策略有效性必须由后续样本证明，不能把“高频训练”写成已实现能力。

## 现有代码
- `tradingagent/Crypto/` 内为现役实体代码，不再依赖 `/opt/investment/Crypto/tools/` 旧目录。
- 当前资本写能力只有 `fixture_auto_sim.py`/`fixture_sim/` 本地非权威纵向切片。`delayed_paper_runtime.py` 已作为 sim-only 核心随 `e8ba46d7e0cab847d0fa037290e7368c69c54655` 发布，并由主集成在 2026-07-28 验证 one-shot、幂等重放、相邻自动轮和 timer enabled/active；这只证明本地 delayed-paper 自动积累，不授予 Testnet/live/production execution authority。`delayed_paper_learning.py` 与 `delayed_paper_learning_worker.py` 是后续独立候选，尚未部署或启用。旧 workflow/simulator/executor/shadow writer 已退役为 tombstone；其余 strategy/validation/report 只作研究辅助。
- 数据源只读 TradingDatas 的 `GET /v1/catalog` 与 `POST /v1/query`；TradingDatas fresh handoff 前只允许显式 fixture/mock。不得由 Crypto 直接调用 Binance、读取 TradingDatas SQLite，或回退到 `/tushare`、`/source_status`、provider 专用 route。

## 特点
- 目标市场语义为 24/7、无交易所统一休市；当前不表示全天候任务已安装。
- 5min delayed-paper 核心已开始自动积累，但连续 24 小时稳定性和策略样本质量仍需运行证据。`Crypto/systemd/` 中核心 timer 的服务器状态与新增学习 timer 候选必须分别验证；仓库文件存在或 `[Install]` 不能证明学习 timer 已安装、enabled 或 active。
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
  必须失败关闭。学习 service/timer 与 daily scrub service/timer 是默认未启用的
  部署候选。worker 只能通过固定
  `/etc/tradingagent/crypto-delayed-paper.epoch.json` 加载并验证唯一 current
  epoch，不接受自由 output root；tracked service 静态钉住经复核的 epoch，
  只给其 `evolution/` 写权限，未来换 epoch 必须人工更新 unit。发布侧可在 timer
  disabled 下 one-shot/full scrub 验收，但 timer 必须等待核心连续 24 小时门禁
  和主集成复核；任何学习失败都不能改变核心 status、exit code、资本、Champion、
  风险或订单。
- `fixture_auto_sim.py` 是薄兼容 facade；实现位于 `fixture_sim/`。该网络关闭纵向切片只接受显式 fixture/mock，以 1h regime、15m decision、closed 5m 证据及 observed-at-or-later executable quote 生成冻结 Champion 的本地 `fixture_simulated` intent/receipt，并写入 Crypto 自有 append-only 资本链、对账和非晋级复盘；它没有 execution authority，也不是 TradingDatas adapter、scheduler、Testnet 或 Live runtime。
- 本批纵向切片是 `crypto-capital-v1` 本地 fixture opening 闭环的唯一可写入口，但仍固定为 `local_fixture_simulated_candidate`，没有 execution/runtime/live authority。旧 `crypto-shadow-sim-v1` 仅保留历史证据。
- ledger 默认构造只读，只有 `fixture_sim/runtime.py` 可通过包内工厂取得写 capability；checksum、文件锁和进程内 capability 仅是协作与损坏防护，不隔离可改代码或文件的同 UID 恶意/失控进程。生产前必须另做单 writer inventory、OS 权限/进程隔离和外部 durable receipt 验证。
- `workflow.py`、`simulator.py`、`sim_executor.py` 与 `shadow_runner.py` 是无条件 fail-closed tombstone；注入 reader、配置或旧账户也不能恢复信号或成交写入。
- `market_data.py` 只接受显式注入的 TradingDatas V1 证据，不得恢复旧 provider 专用入口。
- `adapter.py` 只保留显式 reader 下的 market/universe/strategy 研究映射，不拥有资金、成交、Testnet 或 Live authority；未来三类 broker adapter 仍须分别实现，不能复活 tombstone。
- `capital_policy.py` 是 `crypto-capital-v1` 原生 10,000 USDT 本地 fixture opening baseline 的单一代码来源；它不是 execution、durable receipt、production 或 live capital authority。`config.yaml` 只声明账户币种和风险参数，shared kernel 只能引用而不能另设数值。
- `report.py` 与 `validation.py` 只生成研究辅助证据；`promotion.py` 是只读 scorecard，永久 `eligible_for_sim=false`、`promotion_authority=false`，不能自行晋级或扩风险。
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
- `delayed_paper_round_trip_epoch.py` 只创建不激活的 epoch-g3 候选；manifest
  必须绑定旧 generation-2 epoch identity 文件 SHA 与 capital head checksum，
  准备阶段只读双重校验旧 root，不写 `.current_epoch.json`、不切 timer。部署、
  current pointer 与 timer 切换必须另经正式候选验收。
- `delayed_paper_exit_shadow.py`
  只能从已验证 completion、run bundle 与 capital head 生成止盈、止损、最长持有
  和动量转弱的完整往返反事实；它不得写资本、订单、成交或修改历史 bundle。
  `delayed_paper_health.py` 只生成 no-write 健康快照。两者都固定
  `authority=none`，只能作为 round-trip generation 的对照，不能成为 order、
  receipt、退出触发或资本事实的输入。
- DeepSeek/LLM 只能作为 `offline_fixture`、`authority=none`、`network_used=false` 的独立 sidecar journal；改变其文本不得改变或阻塞核心 replay、Champion、decision、OrderIntent、数量、费用或资本状态。
- 旧 `/opt/investment/Crypto/tools/` 名称清单已从仓库删除；历史实现只从 Git 或独立只读归档审计，不再维护第二份 manifest。
