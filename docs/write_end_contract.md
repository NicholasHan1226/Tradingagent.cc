# Tradings 写入端契约

## 目标
统一 Tradings 的事实写入面，避免 `signals/`、`shared/signals/`、`executions/`、`/opt/investment/Ashare/data/` 并行写入导致资金层混层、状态冲突和复盘口径漂移。

## 单一事实源
- `signals/pending/`：执行队列中的待执行信号。
- `signals/filled/`：执行完成后的成交事实。
- `signals/cancelled/`：失效、撤单、取消的执行事实。
- `signals/positions/`：当前持仓快照。
- `shared/accounting/`：资金账本、现金流水、收益核算、对账。
- `shared/review/data/`：复盘输入沉淀、review/audit JSONL、周期状态。
- `outputs/`：可再生成报表、邮件、导出物；不是事实源，不得反向覆盖上面目录。

## 归并约定
- `shared/signals/`：
  - 视为废弃兼容路径。
  - 若仍有历史读取方，应通过重定向、软迁移或只读桥接接入 `signals/`。
  - 不得继续向 `shared/signals/` 新增事实写入。
- `executions/`：
  - 不再作为独立事实账本。
  - 成交结果统一归并到 `signals/filled/`。
  - 资金变化、手续费、现金余额统一归并到 `shared/accounting/`。
- `/opt/investment/Ashare/data/`：
  - 属旧系统输入。
  - Tradings 仅允许只读兼容，不允许写入、回填、覆盖或迁移为主事实源。

## 资金层字段
- 所有成交、持仓、复盘记录必须显式带 `capital_layer`。
- canonical 值只允许：
  - `real`
  - `simulated`
  - `shadow`
- 历史 `paper` / `paper_portfolio` / `paper_tracking` 路径统一解释为 `capital_layer=shadow`。
- 禁止在 review/accounting/report 中把 `shadow` 或 `simulated` 收益并入 `real` 报表。

## 写入规则
- 新写入优先顺序：
  1. 执行状态写 `signals/`
  2. 资金与收益写 `shared/accounting/`
  3. 复盘结论写 `shared/review/data/`
  4. 展示产物写 `outputs/`
- 不允许同一事实同时写入多个主路径后再依赖人工对齐。
- 任何兼容旧系统的读取都必须标注“只读输入”，不能反向成为 Tradings 主账本。

## 验证要求
- 写入端改动上线前，至少验证：
  - 同一笔 trade/position 只有一个主事实落点。
  - `capital_layer` 能从执行/持仓继承到 review/audit。
  - review 统计按 `capital_layer` 分组，`shadow` 不进入 `real` 汇总。
  - JSONL/CSV 导出每行都带 `capital_layer`。
