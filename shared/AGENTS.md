# TradingAgent / shared

> 阅读顺序：[../AGENTS.md](../AGENTS.md) → [../STATUS.md](../STATUS.md) → 本文件。

## 职责

`shared/` 提供跨市场筛选、风控、组合、执行、资本、复盘、记账和运行验收。市场专属语义保留在 Ashare/CNFutures；共享层不得把一个市场的资金、数据或成熟度泛化到另一个市场。

## 当前 authority

- `capital/`：A股与 CNFutures 两套独立 50,000 CNY append-only capital authorities；禁止跨市场预约、净额或补资。
- `execution/`：跨市场只共享执行端口、不可变成交事实、durable outbox、幂等、审计和对账原语；模拟撮合与未来 live broker adapter 均归各市场域，不能共享协议、账户或密钥。
- `review/`：SampleJournal、forward labels、actual-cost KPI、人工 evolution assessment 与市场成熟度。
- `risk/`：硬门禁、市场独立风控和 exploration 可调策略门槛。
- `runtime_test/`：只读或隔离副本验收；缺输入不能静默通过。
- `data/`：mock-first `GET /v1/catalog` + `POST /v1/query` provider-neutral client/Evidence Gate；base URL、catalog version、dataset IDs 与 policy 显式配置。当前不证明生产 TradingDatas runtime，禁止直读兄弟仓文件、旧专用端点或 provider fallback。`SharedSignalsV1*` 等旧名只可作为明确的兼容代码标识，不能表示仍依赖旧 SharedSignals runtime。

## 事实路径

- A股资本：`shared/logs/capital/ashare/`
- CNFutures 资本：`shared/logs/capital/cn_futures/`
- A股 server-local 执行：从 current capital snapshot 的 `execution_lineage_id` 派生 `shared/logs/execution_lineages/<execution_lineage_id>/`；固定日期 lineage 与旧 `shared/logs/local_sim/` 不得回退读取。
- `signals/` 是 time-boxed 旧队列/回执兼容路径；V1 fixture/day-loop 只使用调用方显式隔离 root。
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
- Mini/Hermes 模拟桥与 `RealSignalQueue` 已从源码退役，禁止成为新依赖或兼容 fallback。`ASHARE_SIM_HERMES_ENABLED`、`ASHARE_SIM_WEBHOOK_ENABLED` 只是在安装态清理阶段保持为必须等于零的墓碑变量；任何 truthy/未知值仍须 fail closed。
- maintenance/backfill/smoke/bootstrap/dry-run 必须隔离或显式排除，不污染交易量、PnL、KPI 和成熟度。
- 旧共享资本、旧演化 writer、旧重复 cron/docs 和历史执行路径已被现役 A 股组合根与仓库调度入口阻断；物理删除、消费者归零、安装态和外部依赖清理仍按 `governance/legacy_inventory.yaml` 与 [系统状态矩阵](../docs/system_state_matrix.md) 分阶段推进。迁移期间仅保留只读/回归证据，不得恢复为 authority 或执行入口。

完整字段见 [../docs/data_contract.md](../docs/data_contract.md)，运维见 [../docs/operations.md](../docs/operations.md)。
