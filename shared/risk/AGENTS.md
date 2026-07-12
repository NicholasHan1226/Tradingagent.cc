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
- 资金未部署保存 deployed/committed/planned utilization、dynamic operating cash、undeployed amount 和 reason distribution；弱市或无合格机会不等于系统故障。
- 5% 回撤不能被实现成“禁止所有新仓”；7% 才暂停。两个市场独立触发，禁止互相净额。
- 有真实持仓与成交证据的风险降低型退出单独评估，不能因为新增风险 authority 暂不可用而自动阻断；仍必须满足 T+1/会话、幂等和 capital commit。
- 任何未知状态保守处理，不伪造可用资金、释放、成交或收益。
