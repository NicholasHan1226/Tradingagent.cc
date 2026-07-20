# TradingAgent / shared/capital

本目录是两个彼此隔离的 simulated capital authorities。任何调用必须显式指定 market；缺 authority、lineage、PIT 或 checksum 时 fail closed。

## 唯一政策源

| 市场 | policy | authority | fresh-start baseline generation | 初始权益 | 容量 |
|---|---|---|---:|---:|---|
| A股 | `ashare_capital_policy.yaml` | `ashare-capital-v1` | 1 | 50,000 CNY | gross 45,000；single-name 7,500；最多8仓 |
| CNFutures | `cn_futures_capital_policy.yaml` | `cn-futures-capital-v1` | 1 | 50,000 CNY | margin 25,000 |

- 默认 roots：`shared/logs/capital/ashare/` 与 `shared/logs/capital/cn_futures/`。
- 覆盖变量：`TRADINGAGENT_ASHARE_CAPITAL_ROOT` 与 `TRADINGAGENT_CN_FUTURES_CAPITAL_ROOT`。
- 事件文件：`ashare_sim_capital_events.jsonl`、`cn_futures_sim_capital_events.jsonl`；latest JSON 只是可重建投影。
- 两市场不可调拨、相加、净额或互补；一个市场的 PnL/DD 不影响另一个市场。
- fresh-start 不继承旧持仓、预约、PnL 或 high-water。legacy freeze manifest 只证明历史已冻结，不导入新 ledger。
- 表中 generation 1 只描述初始基线；每轮消费 current snapshot 的正整数 generation，禁止在 optimizer、runtime、文档示例或测试外固定为 1。
- A股组合常量也只来自同一 YAML：100 股买入整手、最低经济订单 2,000 CNY、无交易区 1,000 CNY、最多 8 个仓位。optimizer、runtime stage、测试和文档不得复制另一套默认值；政策缺字段或跨层不一致时 fail closed。
- 其中 2,000 CNY 与 1,000 CNY 是 Phase 1 首版保守工程假设，不是经验最优值；只有真实费用、滑点、未成交和信号衰减的冻结OOS证据可支持发布新 policy 版本，不能在线自调或为凑频率放宽。

## 风险语义

- 日亏达到 3% 或连续亏损达到 3 次：该市场暂停新增风险。
- MTM 回撤达到 5%：`risk_multiplier=0.75`，不是禁止所有新仓。
- MTM 回撤达到 7%：该市场暂停并复核。
- 上述“暂停”只改变 new-risk eligibility；若 position authority 与全部来源仍 verified，必须保留 positions 供 sell/trim/exit、T+1、幂等和 close commit。authority/source 无效或不一致才全方向阻断。
- A股按 risk unit 聚合持仓市值、pending reservations 和新订单校验单票 15%，并校验 90% gross。
- A股 T+1 可卖数量只能由本账本 append-only `fill_commit` / `ashare_sell_commit` 按 Asia/Shanghai aware `filled_at` 与 FIFO 重放，并与 current position quantity 全等校验；不得信任订单、行情或外部 adapter 自报可卖量覆盖该 authority。
- A股 provider state 必须显式输出 checksum status/last/正整数 event count 和 positions mapping/count/fingerprint；缺字段、非法股票/数量或声明冲突不得推断为空仓。所有 position source 必须与 current authority/generation/lineage/checksum/trade date 和 canonical positions 全等，并用同轮前后双读防止并发绑定漂移。
- CNFutures 保证金使用上限 50%；止损损失预算由执行/风险层另行校验。
- 风险以 current MTM equity/high-water 计算，不能只看 realized PnL。

## 事件与原子性

- `bootstrap`：仅显式 fresh-start 初始化，要求 opening manifest、cutover decision 与真实 legacy freeze manifest。
- `reserve`：最坏情形现金/敞口/保证金预约，stable reference 幂等。
- `fill_commit`：A股买入或期货开仓的 actual fill 原子结算。
- `ashare_sell_commit`：A股卖出的 actual proceeds/fee/PnL 原子结算。
- `position_close_commit`：期货平/减仓的 actual margin release/fee/PnL 原子结算。
- `reconcile`：以 exact reservation manifest、未结 commit IDs、持仓数量/成本/保证金、冻结额和 execution lineage 完成 MTM 守恒证明。

所有 commit 必须包含 authority/generation、execution lineage、PIT timestamp、source/receipt/local fact SHA、fill sequence、idempotency key 和 expected ledger head event/checksum。partial 只消费实际成交部分；终态释放未使用预约。零成交清理只能释放 canonical reservation 当时的完整剩余 cash/exposure/margin，release event 后必须立即得到 `terminal=true` 与三项余额全零；部分 release 不得被 execution receipt 或 reconcile 宣称为成功闭环。冲突重放、未知 reservation、超额/不足释放、symlink、checksum 断链或 CAS 失败全部阻断。

## 运维边界

- 入口：`tools/market_capital_ops.py`。
- 只读：`status`、`verify`、`reconcile-dry-run`、`cutover-audit`、`dual-status`、`migration-plan`。
- actual MTM 入口：`shared/wrappers/job_market_capital_reconcile.sh`；仅消费可验证的执行 snapshot/outbox/TradingDatas mark，并按 market 独立写 reconcile event。TradingDatas fresh handoff 前只能使用显式 fixture/mock mark，禁止 SQLite、旧专用 route 或 provider fallback。
- `init` 必须显式 `--root`，拒绝默认 root，且要求 `--confirm-fresh-start`、全字段 `--opening-manifest` 与 `--legacy-freeze-manifest`；任何字段/哈希/路径不一致都 fail-before-write。这不是日常运行命令，也不单独构成生产授权。
- provider/reservation wrapper 在 authority 缺失时不创建 ledger。
- 只有用户目标明确包含 fresh-start 生产发布且完成相应 preflight 时，主集成者才可执行 staging init、验证、root 激活和 sim-only cron merge；任何日常任务仍禁止隐式初始化。真实账户、broker、邮件或 GUI 委托始终需要独立授权，不能由本目录规则推导。

## 退役边界

- 旧共享资金 policy/ledger/env/CLI 不是兼容入口，不能被新代码或文档引用。
- 历史文件只读冻结；回滚只能停止新任务并保留 append-only 新事实，不能恢复旧 ledger 或改写新事件。
- 测试使用 `tmp_path` 或显式隔离 root，不写默认运行路径。

命令和 manifest 要求见 [../../docs/operations.md](../../docs/operations.md)，字段见 [../../docs/data_contract.md](../../docs/data_contract.md)。
