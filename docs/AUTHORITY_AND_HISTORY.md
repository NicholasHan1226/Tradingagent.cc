# Authority, Autonomy and History

本文件定义 TradingAgent 的事实权威、自动晋级范围和历史保存位置，避免 README、AGENTS、STATUS、机器状态和运行日志重复维护同一事实。

## 执行原则

当前优先级固定为：

```text
真实数据/证据可用
-> simulation/shadow 闭环持续运行
-> 自动学习、评估、晋级和迭代
-> 在运行中发现问题并增量修正
-> 非必要工程重构最后进行
```

不得因为 GitHub Actions 不可用、目录不够理想、文档尚未完全整理或某个独立研究模块未完成，而阻断已经具备客观运行证据的其它数据、研究或模拟链。

## 自动化范围

TradingAgent 的研究、simulation 和 shadow 域以机器证据驱动，不依赖周期性人工确认。

### 自动执行

在冻结合同和硬风险边界内，以下过程应自动完成：

- 数据能力消费状态计算；
- observation / label / sample 积累；
- factor / strategy evaluation；
- Challenger 生成与独立评估；
- 满足 `promotion_evidence_ready=true` 后的模拟盘 Champion promotion；
- drift / health / rollback 条件判断；
- 失败后的有界自愈和重新评估；
- 不扩大真实资金权限的参数、模型和研究路线迭代。

模拟盘 promotion 的权威是证据、评估结果和 promotion receipt，而不是人工计划或文档勾选。

### 独立的真实资金边界

自动研究和自动模拟晋级不等于自动获得真实资金权限。当前 `REAL_TRADING_ENABLED=false` 继续作为独立安全边界；任何未来真实 broker/exchange side effect 必须拥有单独、明确、可撤销的账户授权和生产级执行合同。这个边界不应阻断 simulation/shadow 的自主进化。

## 事实权威

| 事实类型 | 权威 | Markdown 的作用 |
| --- | --- | --- |
| 当前跨线自动开发状态 | Mac mini Controller 状态入口（机器归属与写权见根 `AGENTS.md`）+ 本轮新鲜 readback | 只做解释 |
| TradingDatas 数据可用性 | authenticated `catalog/query` envelope + receipt/lineage | 不复制数据健康状态 |
| capital / positions / reservations / fills | 各市场 append-only authority、ledger head、outbox/reconcile | 文档不成为资金事实 |
| Champion / Challenger / promotion | registry + evaluation artifact + promotion receipt | 记录规则和重大变更 |
| service/timer/effective release | 服务器本轮直接 readback | `STATUS.md` 做短摘要 |
| 长期架构/规则 | `AGENTS.md` + stable docs | 长期约束 |
| 长期决策 | `docs/adr/` | 保存原因和后果 |
| 一次性验收/事故/生产读回 | `docs/reports/` | 日期化人工审计 |
| 普通源码演进 | Git history | 不复制成状态日志 |

## 文档职责

### `README.md`

只保留产品定位、稳定架构、当前能力层级和入口。不要承载长篇历史状态；当 README 与更高优先级 `AGENTS.md`/机器 authority 冲突时，应修 README，而不是让 README 成为第二套规则。

### `AGENTS.md`

保存自动 Agent 必须遵守的长期规则。运行中的瞬时数量、commit、timer 时间和一次性 readback 不进入这里。

### `STATUS.md`

只回答“当前是什么状态”。应逐步收敛为短摘要；过去的重要 readback 迁入 `docs/reports/`，普通变化由 Git history 保存。

### `docs/adr/`

保存会长期约束未来实现的决策。新决定改变旧决定时新增 ADR supersede，不重写旧 ADR。

### `docs/reports/`

保存值得回看的生产验收、故障复盘、迁移结果、基准或科学评估。report 永远不是 runtime authority。

## GitHub Actions

GitHub Actions 不应阻断已经运行且有证据的数据、研究或模拟链，但普通代码合并和发布仍须遵守根 `AGENTS.md` 的精确候选、主线 CI 与 Controller 验收门禁。Actions 暂时不可用不授权从本地测试直接跳到 production release。独立 fallback runner 只有另行提供同等级机器证据并满足既有门禁后才能替代 CI；本文件不新增该权限。

发布证据顺序为：

```text
精确候选与确定性测试
-> PR / 精确主线 CI 与 Controller 验收
-> 明确请求的 immutable release
-> service/timer readback
-> 数据/资本/决策 receipt
-> consumer/evaluation readback
```

GitHub `main` 只代表源代码主线；它不自动等于 Mac 工作树、服务器源码、effective release 或生产 runtime。

## 历史应该保留什么

需要长期保留：

1. Git commit 历史；
2. 会影响未来实现的 ADR；
3. capital/execution/decision/sample/promotion 等不可变机器事实；
4. 对未来调查有价值的生产 readback、事故和迁移 reports。

不需要长期重复保留：

- 每次 timer 成功的 Markdown 描述；
- 每个 commit 的手工摘要；
- 已被机器 authority 覆盖的静态状态数字；
- 同一决策在 README、STATUS、ROADMAP、AGENTS 中各写一份。

目标是让自动系统始终读取机器事实，让人或 Agent 能从少量稳定文档快速理解规则，并从 Git/ADR/report 恢复历史原因。
