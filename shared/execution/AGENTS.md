# TradingAgent / shared/execution

> 阅读顺序：[../../AGENTS.md](../../AGENTS.md) → [../../STATUS.md](../../STATUS.md) → 本文件。

## 职责

执行层负责 server-local 模拟撮合、不可变成交事实、回执重验证、durable outbox、capital commit、shadow 记录和可选 Hermes 模拟对照。当前没有真实下单入口。

## A股闭环

- 本地V1目标链由`compose_capital_backed_paper_runtime`把同一个`PaperCapitalAccount`交给canonical small-account、capital-backed risk、simulation execution、outbox commit和reconcile stage；目前只是test-only candidate，没有CLI、scheduler或live sample。
- `Ashare/sim_executor.py → sim_broker.py → local_sim_ledger.py`是time-boxed legacy/compatibility诊断链，不得作为V1 fallback。`ashare-sim-fresh-20260712-v1`只是历史基线lineage；current root必须由verified capital snapshot的`execution_lineage_id`派生为`shared/logs/execution_lineages/<execution_lineage_id>/`，旧`shared/logs/local_sim/`只读冻结。
- 完整 append-only 账户事实校验现金和可卖持仓；过滤后的策略视图不能放行透支或超卖。
- current position source 只能在读取本地交易事实之前接收已验证的 market-capital authority context，由 `local_sim_ledger` 自己重放 positions 并计算 count/fingerprint/envelope；调用方不得在读取后补 identity。磁盘 reporting snapshot 没有 current envelope 时只作诊断，不能进入普通 risk。
- producer 重放的 open lot 必须发布 source-owned `oldest_open_date` 与同值 `entry_date`，使 sell/trim/exit 统一执行 T+1；调用方不得凭当前日期或未验证 snapshot 猜测开仓日。
- `filled/partial` 只使用 actual quantity/price/time/fee/slippage 与 verified 5-minute evidence；请求价格、请求数量、pending/unknown 不可伪造成交。
- immutable local fill 与 capital `fill_commit`/`ashare_sell_commit` 通过 durable outbox 关联。capital commit 成功或幂等重放成功前，策略样本不能标记 execution-eligible。
- partial 只消费实际预约；终态原子释放剩余。outbox pending 保守占用风险并在重启后重放。
- 每个成交保存 authority/generation、execution lineage、PIT timestamp、candidate/prediction snapshot、receipt/local-trade fingerprints、style attribution、sample intent 和 actual costs。

## CNFutures 闭环

- 方向判断不等于成交；一手 affordability、止损损失预算、保证金/费用/滑点、会话/夜盘/换月和独立 capital reservation 全部通过后才可模拟执行。
- 开仓提交 `fill_commit`，平/减仓提交 `position_close_commit`；actual margin、fee、gross realized PnL、position fingerprint 和 ledger-head CAS 缺一不可。
- pending commit 阻断新增风险但不得阻断有证据的风险降低型退出；错误必须可见、可重放且不重复记账。

## 幂等与故障

- 同一 execution fill identity + 相同 payload 幂等；identity 相同但 payload 冲突 fail closed。
- 资本 ledger、local ledger、receipt、position 或 outbox 任一 checksum/lineage 不一致时，保留证据并停止新增风险；不伪造释放或可用资金。
- chain-validation 样本可保存链路故障，但不得进入胜率、expectancy、费用后 PnL 或成熟度。

## Hermes/Mini

- 默认 server-local 模拟闭环不依赖 Mini；当前仓库cron/env模板强制`ASHARE_SIM_HERMES_ENABLED=0`与`ASHARE_SIM_WEBHOOK_ENABLED=0`，truthy/未知值会在任务正文前fail closed。已安装生产cron/env本轮未验证。恢复GUI模拟对照需要单独发布授权和门禁审计，不能只改服务器环境变量。
- Mini 不判断买卖、不分配资本、不修改 capital authority。点击不是成交；无法用委托/成交/持仓证据严格确认时必须 failed + halt，禁止自动重试下单。
- 拟议邮件 → Nicholas → 同花顺人工复核实盘仍是设计，未实现、未授权；Mini 不发送邮件、不读取真实账户、不点击真实交易。

## 实盘红线

任何 real/live/direct-execution 标记必须安全失败，不能改写为 simulated/shadow。未来 broker gateway 需独立架构、权限、风控、人工确认和发布验收。
