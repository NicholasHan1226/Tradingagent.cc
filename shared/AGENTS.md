# TradingAgent / shared

> 阅读顺序：[../AGENTS.md](../AGENTS.md) → [../STATUS.md](../STATUS.md) → 本文件。

## 职责

`shared/` 提供跨市场筛选、风控、组合、执行、资本、复盘、记账和运行验收。市场专属语义保留在 Ashare/CNFutures；共享层不得把一个市场的资金、数据或成熟度泛化到另一个市场。

## 当前 authority

- `capital/`：A股与 CNFutures 两套独立 50,000 CNY append-only capital authorities；禁止跨市场预约、净额或补资。
- `execution/`：server-local simulated、不可变成交事实、durable outbox、回执和可选 Hermes 模拟对照。
- `review/`：SampleJournal、forward labels、actual-cost KPI、人工 evolution assessment 与市场成熟度。
- `risk/`：硬门禁、市场独立风控和 exploration 可调策略门槛。
- `runtime_test/`：只读或隔离副本验收；缺输入不能静默通过。
- `data/`：SharedSignals API-first reader；生产不直读兄弟仓文件。

## 事实路径

- A股资本：`shared/logs/capital/ashare/`
- CNFutures 资本：`shared/logs/capital/cn_futures/`
- A股 server-local 执行：`shared/logs/execution_lineages/ashare-sim-fresh-20260712-v1/`；旧 `shared/logs/local_sim/` 不得回退读取。
- 执行队列与回执：`signals/`
- A股样本：`shared/review/ashare/sample_journal.jsonl`
- KPI/decision/maturity：`shared/review/ashare/` 下的可重建投影
- 展示输出：`outputs/` 或 front snapshot；不得反写事实。

## 共享约束

- 任何 API 都必须显式指定 market/authority/generation/execution lineage；不能回退到隐含共享资本。
- reservation、fill/sell/close commit、reconcile 使用 stable reference、PIT lineage、source fingerprints、checksum chain、ledger-head CAS 和 crash-replay outbox。
- observation 不受成熟阈值阻断；execution-eligible fill 必须有实际价格、数量、费用/滑点、市场证据和资本 commit。
- observation/exploration/exploitation/chain validation 分层；风格 shadow 不能成为资金或组合权益。
- SampleJournal/KPI 是唯一演化 authority；所有自动 promotion、自动风险扩张和 live transition 关闭。
- 任何 live/real 标记 fail closed，不自动降级。
- maintenance/backfill/smoke/bootstrap/dry-run 必须隔离或显式排除，不污染交易量、PnL、KPI 和成熟度。
- 旧共享资本、旧演化 writer、旧重复 cron/docs 和历史执行路径均已退役；历史只读冻结，不恢复为入口。

完整字段见 [../docs/data_contract.md](../docs/data_contract.md)，运维见 [../docs/operations.md](../docs/operations.md)。
