# TradingAgent / shared/execution

> 阅读顺序：[../../AGENTS.md](../../AGENTS.md) → [../../STATUS.md](../../STATUS.md) → 本文件。

## 职责

执行层只提供跨市场机械原语：执行端口、不可变成交事实、回执重验证、durable outbox、幂等、审计、对账与 fail-closed 实盘门。A股、CNFutures、Crypto 的模拟撮合和未来实盘适配器分别归各自市场域；当前没有真实下单入口。

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

## 市场适配器隔离与旧桥退役

- A股模拟合同固定为`tradingagent.ashare.paper_broker.v1`，只做server-local paper execution；CNFutures和Crypto使用各自不同的模拟合同与未来broker adapter，禁止复用A股payload或状态机。
- Mini/Hermes bridge、webhook sender、Mini file consumer与`RealSignalQueue`均已从源码退役。不得以兼容、健康探针或人工确认名义恢复其import、网络、文件队列或真实信号路径。
- `ASHARE_SIM_HERMES_ENABLED=0`与`ASHARE_SIM_WEBHOOK_ENABLED=0`暂作安装态清理墓碑；truthy/未知值在任务正文前fail closed。已安装服务器cron/env/process/port必须在独立发布前门中做只读readback，源码删除不能冒充安装态已清理。
- 通用`signal_state_machine.py`只保留为隔离模拟/影子状态原语，不具有Mini、券商或真实账户语义。

## 实盘红线

任何 real/live/direct-execution 标记必须安全失败，不能改写为 simulated/shadow。未来每个市场的 broker gateway 都需独立架构、凭据、账户、风控、人工确认和发布验收；共享层只提供接口与审计机械能力。
