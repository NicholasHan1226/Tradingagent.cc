# accounting/

> **阅读顺序：** [../../AGENTS.md](../../AGENTS.md) → [../../STATUS.md](../../STATUS.md) → 本文件

## 目标
资金流水精确到分。持仓台账。每日对账。只追加审计追踪。

## 文件
- `capital_ledger.py` — 资金流水: 买入/卖出/逆回购/利息/手续费。SQLite 存储, 按 real/simulated/shadow 三张表物理隔离, 精确到分。
- `position_ledger.py` — 持仓流水: 开仓/加仓/减仓/平仓。SQLite 存储, 按 real/simulated/shadow 三张表物理隔离。
- `daily_reconcile.py` — provider-neutral 单市场对账原语：系统持仓 vs 该市场 paper/broker adapter 快照，输出 matched/mismatches/actions；它不持有transport、账户或凭据。
- `trade_audit_trail.py` — 审计追踪: signal→decision→risk→execution→result 全链路, JSONL 追加只写, 永不修改。

## 原则
- 每一笔资金变动必须可追溯到源头信号
- 对账差异 > 0.01 元即标记 mismatch
- 审计日志 append-only: 只写不改不删, 事后取证唯一真相
- 持仓以量为准, 资金以额为准, 两者交叉验证
- 逆回购和利息是 A 股资金管理的重要组成部分, 不可遗漏

## 接口
```python
from capital_ledger import record_buy, record_sell, record_reverse_repo, record_interest, get_capital_balance, get_cash_position
from position_ledger import open_position, add_position, reduce_position, close_position, get_positions
from daily_reconcile import reconcile
from trade_audit_trail import record_event
```

## 依赖
- 各市场自己的 paper/broker adapter 快照；A股、CNFutures、Crypto 的账户、字段映射和回执分别实现，不能由本模块互相转换。
- 共享 logs/ 目录 — SQLite 账本、旧 CSV 迁移输入和 JSONL 存储位置
