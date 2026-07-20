# tradingagent/Crypto

> **阅读顺序：** [AGENTS.md](../AGENTS.md) → [STATUS.md](../STATUS.md) → 本文件

## 目标
加密货币交易模拟盘/影子盘，按 7x24 市场语义建设持续观察和复盘能力。当前只验证 fixture/mock 下的候选模块；监控频率、数据覆盖和策略有效性必须由后续样本证明，不能把“高频训练”写成已实现能力。

## 现有代码
- `tradingagent/Crypto/` 内为现役实体代码，不再依赖 `/opt/investment/Crypto/tools/` 旧目录。
- shadow/sim/strategy/validation 源码模块存在于当前隔离候选，但尚无 fresh TradingDatas、已安装 scheduler、Testnet 或 live 验收，不能称为完整闭环或生产能力。
- 数据源只读 TradingDatas 的 `GET /v1/catalog` 与 `POST /v1/query`；TradingDatas fresh handoff 前只允许显式 fixture/mock。不得由 Crypto 直接调用 Binance、读取 TradingDatas SQLite，或回退到 `/tushare`、`/source_status`、provider 专用 route。

## 特点
- 目标市场语义为 24/7、无交易所统一休市；当前不表示全天候任务已安装。
- 5min 条件监控是待验证目标频率，不是已部署 SLA。
- server-local paper、Binance Spot Testnet 和未来 Binance Spot Live 是三份不同合同、账户与凭据域；不能靠切换 base URL 或环境变量升级。当前 `REAL_TRADING_ENABLED=false`，Live adapter 未实现。

## 当前模块边界

- `workflow.py` 只编排 Crypto 自有 fixture/mock 研究流程。
- `market_data.py` 只接受显式注入的 TradingDatas V1 证据，不得恢复旧 provider 专用入口。
- `simulator.py`、`sim_executor.py`、`adapter.py` 分别拥有 Crypto 的小数数量、最小名义金额与模拟/Testnet/Live 合同边界；当前仅模拟合同可运行。
- `capital_policy.py` 是 Crypto 原生 10,000 USDT 初始模拟资本的单一代码权威；`config.yaml` 只声明账户币种和风险参数，加载时由市场配置校验该权威，shared kernel 只能引用而不能另设数值。
- `shadow_runner.py`、`report.py`、`validation.py`、`promotion.py` 只生成 shadow 研究和人工复核证据，不能自行晋级或扩风险。
- 旧 `/opt/investment/Crypto/tools/` 名称清单已从仓库删除；历史实现只从 Git 或独立只读归档审计，不再维护第二份 manifest。
