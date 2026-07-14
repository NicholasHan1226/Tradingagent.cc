# TradingAgent / shared/risk

> 阅读顺序：[../../AGENTS.md](../../AGENTS.md) → [../../STATUS.md](../../STATUS.md) → 本文件。

## 门禁分层

永不放宽：

- 数据来源、新鲜度、PIT lineage 和可靠性；
- 实际价格/成交证据、费用/滑点、流动性和价格限制；
- A股交易时段、T+1、100 股整手；
- 期货会话、最小一手、保证金、止损损失预算、夜盘跳空和换月；
- 对应市场的现金/持仓/保证金、exact reservations、outbox、幂等和 ledger-head CAS；
- A股单票/组合敞口、期货保证金使用率、日亏、连续亏损和 MTM 回撤；
- simulated/real 隔离。

Exploration 只可下调候选 raw score、最小 edge、研究证据完整度等策略门槛。每次下调保存 policy/version/reason/sample intent/propensity；样本债不能单独成为放行或拒绝理由。

## 当前每市场数值

| 项目 | A股 | CNFutures |
|---|---:|---:|
| 独立初始权益 | 50,000 CNY | 50,000 CNY |
| 市场容量 | gross 45,000 | margin 25,000 |
| 单一风险单元 | 单票累计 7,500 | 由一手规格与止损损失预算共同限制 |
| 日亏暂停 | 1,500 | 1,500 |
| 连续亏损暂停 | 3 | 3 |
| 回撤收紧 | 5%，风险预算乘 0.75 | 5%，风险预算乘 0.75 |
| 回撤暂停 | 7% | 7% |

A股 exploration 另有：每日最多一个新增头寸、累计敞口 7,500 CNY、日亏 225 CNY。调用方必须从 market policy/capital plan 读取，不复制另一套常量。

## 结果与退出

- 每次拒绝保存具体 reason、market、style、sample intent、authority/generation、execution lineage 和相关证据。
- A股普通 risk、仓位容量、动态 capital plan 和 rebalance 之前必须先通过唯一 position authority gate；来源缺失、陈旧、identity/checksum/count/fingerprint 不一致或并发双读漂移统一拒绝为 `capital_position_source_mismatch`。此拒绝不得伪装成普通“8 仓已满”，也不得通过默认零仓或 legacy/strategy snapshot 放宽风险。
- 资金未部署保存 deployed/committed/planned utilization、dynamic operating cash、undeployed amount 和 reason distribution；弱市或无合格机会不等于系统故障。
- 5% 回撤不能被实现成“禁止所有新仓”；7% 才暂停。两个市场独立触发，禁止互相净额。
- position authority verified 但日亏/连亏/回撤令 `new_risk_allowed=false` 时，只阻断 buy/open/add；保留 verified positions 供 sell/trim/exit 单独评估，且仍必须满足 T+1/会话、幂等和 capital commit。authority 缺失、非法或 source mismatch 时所有方向均 fail closed。
- 任何未知状态保守处理，不伪造可用资金、释放、成交或收益。
