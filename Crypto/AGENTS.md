# tradingagent/Crypto

> **阅读顺序：** [AGENTS.md](../AGENTS.md) → [STATUS.md](../STATUS.md) → 本文件

## 目标
加密货币交易模拟盘/影子盘，按 7x24 市场语义建设持续观察和复盘能力。当前只验证 fixture/mock 下的候选模块；监控频率、数据覆盖和策略有效性必须由后续样本证明，不能把“高频训练”写成已实现能力。

## 现有代码
- `tradingagent/Crypto/` 内为现役实体代码，不再依赖 `/opt/investment/Crypto/tools/` 旧目录。
- 当前可写能力只有网络关闭的 `fixture_auto_sim.py`/`fixture_sim/` 本地非权威纵向切片。旧 workflow/simulator/executor/shadow writer 已退役为 tombstone；其余 strategy/validation/report 只作研究辅助。尚无 fresh TradingDatas、已安装 scheduler、Testnet 或 live 验收，不能称为完整闭环或生产能力。
- 数据源只读 TradingDatas 的 `GET /v1/catalog` 与 `POST /v1/query`；TradingDatas fresh handoff 前只允许显式 fixture/mock。不得由 Crypto 直接调用 Binance、读取 TradingDatas SQLite，或回退到 `/tushare`、`/source_status`、provider 专用 route。

## 特点
- 目标市场语义为 24/7、无交易所统一休市；当前不表示全天候任务已安装。
- 5min 条件监控是待验证目标频率，不是已部署 SLA。
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
  `main@62d76f8cdcc7671a9523ac15905ab2eb3152e387`；此前 isolated canary
  `025fd24…` 已证明
  `symbol eq + open_time between + as_of + desc + limit=13` 返回精确连续窗口；
  但仍没有正式 Crypto internal HTTP/runtime/timer 与带认证 readback handoff。
  本模块继续 fixture-only，并必须执行 bounded cursor traversal；non-null
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
- `fixture_auto_sim.py` 是薄兼容 facade；实现位于 `fixture_sim/`。该网络关闭纵向切片只接受显式 fixture/mock，以 1h regime、15m decision、closed 5m 证据及 observed-at-or-later executable quote 生成冻结 Champion 的本地 `fixture_simulated` intent/receipt，并写入 Crypto 自有 append-only 资本链、对账和非晋级复盘；它没有 execution authority，也不是 TradingDatas adapter、scheduler、Testnet 或 Live runtime。
- 本批纵向切片是 `crypto-capital-v1` 本地 fixture opening 闭环的唯一可写入口，但仍固定为 `local_fixture_simulated_candidate`，没有 execution/runtime/live authority。旧 `crypto-shadow-sim-v1` 仅保留历史证据。
- ledger 默认构造只读，只有 `fixture_sim/runtime.py` 可通过包内工厂取得写 capability；checksum、文件锁和进程内 capability 仅是协作与损坏防护，不隔离可改代码或文件的同 UID 恶意/失控进程。生产前必须另做单 writer inventory、OS 权限/进程隔离和外部 durable receipt 验证。
- `workflow.py`、`simulator.py`、`sim_executor.py` 与 `shadow_runner.py` 是无条件 fail-closed tombstone；注入 reader、配置或旧账户也不能恢复信号或成交写入。
- `market_data.py` 只接受显式注入的 TradingDatas V1 证据，不得恢复旧 provider 专用入口。
- `adapter.py` 只保留显式 reader 下的 market/universe/strategy 研究映射，不拥有资金、成交、Testnet 或 Live authority；未来三类 broker adapter 仍须分别实现，不能复活 tombstone。
- `capital_policy.py` 是 `crypto-capital-v1` 原生 10,000 USDT 本地 fixture opening baseline 的单一代码来源；它不是 execution、durable receipt、production 或 live capital authority。`config.yaml` 只声明账户币种和风险参数，shared kernel 只能引用而不能另设数值。
- `report.py` 与 `validation.py` 只生成研究辅助证据；`promotion.py` 是只读 scorecard，永久 `eligible_for_sim=false`、`promotion_authority=false`，不能自行晋级或扩风险。
- LLM sidecar 必须在核心 cycle lock 之外独立追加并限制读取大小；损坏或写入失败只形成无权威 degraded 诊断，不得回滚、重复或阻断已提交的核心资本与 bundle replay。
- DeepSeek/LLM 只能作为 `offline_fixture`、`authority=none`、`network_used=false` 的独立 sidecar journal；改变其文本不得改变或阻塞核心 replay、Champion、decision、OrderIntent、数量、费用或资本状态。
- 旧 `/opt/investment/Crypto/tools/` 名称清单已从仓库删除；历史实现只从 Git 或独立只读归档审计，不再维护第二份 manifest。
