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
