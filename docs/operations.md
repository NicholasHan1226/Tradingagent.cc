# TradingAgent 运行、验收与回滚

> 本文是 sim-only 运维入口。当前任务禁止 deploy、apply cron、发邮件、操作 GUI 或真实交易；仓库模板与本地命令成功不代表生产已生效。当前状态见 [STATUS.md](../STATUS.md)。

## 1. 固定安全环境

```bash
cd /Users/nicholashan/Projects/Finance/TradingAgent
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
export REAL_TRADING_ENABLED=false
export SHAREDSIGNALS_API_URL="${SHAREDSIGNALS_API_URL:-http://127.0.0.1:8082}"
```

两个 capital root 独立配置：

```bash
export TRADINGAGENT_ASHARE_CAPITAL_ROOT=/path/to/ashare-capital
export TRADINGAGENT_CN_FUTURES_CAPITAL_ROOT=/path/to/cn-futures-capital
```

禁止设置旧共享 capital root。不要让两个变量指向同一目录；启动前检查它们不是 symlink，且 event/lock/latest 文件名分别属于对应 market。

## 2. Capital authority 只读检查

单市场状态、checksum 和 cutover：

```bash
python3 tools/market_capital_ops.py status --market ashare --trade-date YYYYMMDD
python3 tools/market_capital_ops.py status --market cn_futures --trade-date YYYYMMDD
python3 tools/market_capital_ops.py verify --market ashare
python3 tools/market_capital_ops.py verify --market cn_futures
python3 tools/market_capital_ops.py cutover-audit --market ashare
python3 tools/market_capital_ops.py cutover-audit --market cn_futures
```

并列状态：

```bash
python3 tools/market_capital_ops.py dual-status --trade-date YYYYMMDD
```

输出中两个 market 可并列，但不得求和。`fresh=true` 只在目标交易日有 actual MTM reconcile 后成立。

CLI 的 `reconcile-dry-run` 只检查 ledger 本身，不会制造 fresh：

```bash
python3 tools/market_capital_ops.py reconcile-dry-run --market ashare --trade-date YYYYMMDD
python3 tools/market_capital_ops.py reconcile-dry-run --market cn_futures --trade-date YYYYMMDD
```

日常 actual MTM writer 已统一为 `job_market_capital_reconcile.sh`。它先从 SharedSignals 刷新 PIT mark，再从 fresh execution snapshot、durable outbox 和 capital event chain 证明 cash/position/reservation/fill watermark 守恒，最后提交 `mtm_reconcile()`：

```bash
REAL_TRADING_ENABLED=false \
  shared/wrappers/job_market_capital_reconcile.sh ashare opening

REAL_TRADING_ENABLED=false \
  shared/wrappers/job_market_capital_reconcile.sh cn_futures preopen
```

`opening/preopen/ops` 时点已经写入仓库 cron 模板；生产是否安装必须通过项目 merge 工具的 readback 与 cron coverage 单独证明，不能从模板反推。A股 `14:58` 仍是收盘前资本 checkpoint；独立 `15:32 ops` 在盘后固定价格交易结束后写当日 closing MTM，并向 `shared/review/ashare/sample_journal.jsonl` 追加日级 `account_daily_mtm_equity` chain-validation 证据。15:31 前的 reconcile 不得作为正式逐日回撤点。wrapper 缺 source、mark、exact reservation、commit、lineage 或 ledger-head 证据时必须 blocked；不能用 dry-run 或人工 JSON 伪造 fresh。

## 3. Fresh-start 初始化边界

`init` 不是日常任务，也不会在读取、reserve 或 wrapper 中隐式发生。它只允许对尚未作为当前默认 root 的显式隔离目录执行，并要求 Nicholas 已批准的 fresh-start decision、opening manifest 与真实 legacy freeze manifest：

```bash
python3 tools/market_capital_ops.py init \
  --market ashare \
  --root /isolated/new/ashare-capital \
  --confirm-fresh-start \
  --opening-manifest /evidence/ashare-opening.json \
  --legacy-freeze-manifest /evidence/legacy-freeze.json \
  --trade-date YYYYMMDD
```

CNFutures 使用独立 root 和 opening manifest。禁止复用一个 root、事件文件或 execution lineage。

Opening authority 的完整 contract 必须证明 50,000 CNY cash/equity、零持仓/保证金/预约/冻结/PnL、fresh-start mode、current authority/generation、source SHA、唯一 execution lineage 和 `real=false`。CLI 对 opening JSON 做 dataclass 全字段、类型、集合精确性与 policy 校验，并在任何不一致时 fail-before-write。

Legacy freeze manifest 必须引用真实只读事件文件和真实归档目录，并保存 SHA-256、最后 event ID、行数、带时区 frozen-at 和 `imported=false`。路径/哈希/行数/最后 ID 任一不匹配都必须 fail-before-write。

默认不得对生产 root 执行 init 或切换 runtime env。只有目标明确包含 fresh-start 生产发布且完成对应 preflight 时，主集成者才可把初始化、root 激活和首次 reconcile 作为独立 cutover：先备份并停止相关任务，在两个互异、非 symlink 的隔离 root 初始化并验证，再逐市场激活；日常任务不得隐式创建本金或 authority。

## 4. A股日内与样本运行

仓库 cron authority 是 `shared/crontab.txt`；`shared/wrappers/job_ashare_sim_exec.sh` 运行 server-local simulated loop，并在开市外自行跳过。Hermes 默认关闭：

```bash
export ASHARE_SIM_HERMES_ENABLED=0
export ASHARE_SIM_WEBHOOK_ENABLED=0
```

当前 production cron/env loader 将这两个变量作为必须关闭的发布门禁：`.env`
或共享环境中出现 truthy/未知值时，任务在正文启动前 fail closed。恢复 Mini/Hermes
模拟第二路径需要单独发布授权和新的门禁审计，不能只改服务器环境变量。

盘前与 opening 验收是只读检查：

```bash
python3 -m shared.runtime_test.ashare_preopen_dry_run \
  --now YYYY-MM-DDT09:20:00+08:00 --json --pretty --send-on never --no-write

python3 -m shared.runtime_test.ashare_opening_validator \
  --now YYYY-MM-DDT09:35:00+08:00 --pretty
```

盘前至少检查 SharedSignals 来源/覆盖/新鲜度、普通 A股与流动性、A股 capital fresh/reconciled、server-local cash/positions、outbox 和 simulation-only flags。失败不应阻断 observation 写入，但必须阻断新增风险。

统一 sample ops 会追加到期标签并写 KPI、manual-only evolution decision 和 maturity 投影；它不创建订单、账户、邮件或 live transition：

```bash
python3 -m shared.runtime_test.ashare_sample_ops \
  --journal-path shared/review/ashare/sample_journal.jsonl \
  --review-dir shared/review/ashare \
  --trade-date YYYYMMDD \
  --as-of YYYY-MM-DDTHH:MM:SS+08:00 \
  --pretty
```

预期投影：`sample_kpi_latest.json`/log、`evolution_decision_latest.json`/log、`market_maturity_latest.json`/log。Journal 是 authority；latest 丢失时从 journal 重建，禁止反向改写 journal。

`job_ashare_sample_ops.sh` 是唯一活跃 A股 labels/KPI/evolution-assessment/maturity wrapper。旧 sample-learning、旧 forward-validation、旧 portfolio-evolution 和重复监控入口不得恢复。

## 5. CNFutures 日内与会话验收

活跃模拟 wrapper 是 `shared/wrappers/job_cn_futures_sim.sh`，由 `shared/crontab.txt` 在日盘/夜盘节奏触发。每个实际有效会话必须产生 prediction/candidate/hold/risk reject/fill 之一；闭市时间不计有效会话。

会话证据只读验收：

```bash
python3 -m shared.runtime_test.cn_futures_session_acceptance \
  --input /path/to/cn-futures-runtime.jsonl \
  --trade-date YYYYMMDD \
  --sessions day_morning,day_afternoon,night \
  --verify-checksums \
  --pretty
```

`--sessions` 必须来自当日真实产品/交易所会话，不得为了通过验收虚构。结果按 execution-eligible 和 counterfactual-only 分层，持仓/保证金不足、换月、会话不允许等具体拒绝原因均是有效复盘证据。

期货 sample ops 先追加到期标签，再重建带 checksum 的独立 maturity 投影；不创建订单、账户或晋级：

```bash
python3 -m shared.runtime_test.cn_futures_sample_ops \
  --review-path shared/review/data/cn_futures_sim_reviews.jsonl \
  --review-dir shared/review/cn_futures \
  --trade-date YYYYMMDD \
  --as-of YYYY-MM-DDTHH:MM:SS+08:00 \
  --pretty
```

## 6. Capital-growth 完整验收

快速、本地全量与前端：

```bash
python3 -m shared.runtime_test.full_acceptance --profile quick --pretty
python3 -m pytest -q
python3 -m shared.runtime_test.full_acceptance --profile front --front-tests --pretty
```

运行层只读验收：

```bash
python3 -m shared.runtime_test.full_acceptance --profile prod --pretty
```

“prod”只是运行检查 profile，不授权部署、写 cron 或真实交易。

双资本/样本/会话验收：

```bash
python3 -m shared.runtime_test.full_acceptance \
  --profile capital_growth \
  --trade-date YYYYMMDD \
  --ashare-capital-root "$TRADINGAGENT_ASHARE_CAPITAL_ROOT" \
  --cn-futures-capital-root "$TRADINGAGENT_CN_FUTURES_CAPITAL_ROOT" \
  --ashare-journal shared/review/ashare/sample_journal.jsonl \
  --as-of YYYY-MM-DDTHH:MM:SS+08:00 \
  --cn-futures-records /path/to/cn-futures-runtime.jsonl \
  --cn-futures-sessions day_morning,day_afternoon,night \
  --pretty
```

该 profile 对 A股 journal 使用隔离副本物化 forward labels，不改源 journal；显式 root、journal、as-of、期货 records 或 sessions 缺失时 fail。`REAL_TRADING_ENABLED=true` 时任何子进程启动前 fail。

## 7. 每日验收表

### A股

- 当日每个数据合格候选都有 prediction snapshot 与 paired MG ablation；
- observation/exploration/exploitation 分开计数；
- 无 exploration 时只有“无数据合格候选”或具体硬门禁，不能只有“样本不足”；
- actual fill/partial/sell 与 capital commit、receipt、position、journal 一致；pending outbox 数量为零或有明确 blocker；
- 到期 `m30/m60/close/1d/3d/5d` 标签与成本证据可见；
- 资金计划输出 deployed/committed/planned utilization、未部署金额与原因；
- day index、day-5/day-10 review due、有效样本、成熟度 blockers 与人工授权状态可见。

### CNFutures

- 每个声明有效会话有判断记录；
- execution-eligible 与 counterfactual-only 分层；
- 一手规格、保证金、止损预算、费用、滑点、夜盘、换月和 PIT evidence 完整；
- open/close commit 与持仓/保证金/费用/PnL 一致，outbox 可 crash replay；
- 独立 maturity 显示样本、回合、品种/波动/会话、夜盘/换月/极端风险和稳定性。

### 共同

- 两个 capital authority 分别 fresh/reconciled/checksum-valid；
- 5% 是 tightened、7% 是 halted，两个市场互不影响；
- SampleJournal/KPI 是唯一 evolution authority；所有自动 promotion/risk expansion/live transition 为 false；
- 前端 All Markets 不显示 combined capital/equity/PnL/return/DD。

## 8. Cron 审计与应用边界

静态检查：

```bash
python3 -m shared.runtime_test.cron_coverage --pretty
python3 tools/merge_tradingagent_crontab.py --current-file /path/to/exported-crontab
```

第二条默认只生成/预览 merge 结果。`--apply` 会修改用户 crontab，当前任务禁止执行。仓库模板通过不等于服务器 crontab 已安装；未来应用前必须先导出当前 crontab、diff、多仓归属核对和 rollback 文件。

## 9. 故障与 fail-closed

| 现象 | 新增风险 | Observation | 处理 |
|---|---|---|---|
| capital authority 不可读/非 fresh | 阻断 | 继续 | 保留具体 blocker，修复后 MTM reconcile |
| outbox pending/CAS 冲突 | 阻断 | 继续 | 停止重复提交，重放同一 action，不新建 identity |
| 数据陈旧/无来源/PIT 不完整 | 阻断 | 保存但 label rejected | 修复 SharedSignals/source lineage |
| 无合格候选 | 不下单 | 继续 | 记录 universe/coverage bias 与 undeployed reason |
| 5% 回撤 | 收紧至 0.75 倍 | 继续 | 不误实现为全停 |
| 7% 回撤/日亏/连续亏损 | 暂停 | 继续 | 人工复核该市场；不影响另一市场 |
| actual cost/fill evidence 缺失 | 不计策略绩效 | 保留 chain validation | 补证据，不回写伪值 |
| live/real marker | 全部阻断 | 不写入当前 journal | 安全事件处置 |

## 10. 回滚

回滚目标是停止新增风险并保留可审计事实，不是恢复旧系统。

1. 逐市场停止新 wrapper/任务；不删除 pending outbox、event JSONL、local fills、receipts 或 SampleJournal。
2. 记录最后 event ID/checksum、execution lineage、active reservation manifest、unreconciled commit IDs、positions/cash/margin 和最新投影 SHA。
3. 若 outbox 未清零，保持该市场新增风险 halted；只重放相同 action identity，不手工释放或生成替代事件。
4. 切回能理解现有 event schema 的已验证代码。若旧代码不能读取新事件，保持停机，不得通过恢复旧共享 ledger 绕过。
5. 对 capital ledger 运行 `verify`，对 local execution/receipt/journal 做 checksum 与 lineage 审计，再决定是否恢复 sim-only 任务。
6. KPI/maturity latest 损坏时从 append-only SampleJournal 重建；不得修改 journal 历史。
7. 前端可独立回滚到旧只读 build，但仍不得展示跨市场货币聚合或写入接口。

禁止的“回滚”：删除/改写 capital events、清空 PnL/持仓、改变 generation、导入历史冻结数据、把两个账户合并、恢复退役 cron/writer、发送补偿邮件或开启真实交易。

## 11. 实盘晋级边界

- A股 day 5/day 10 只做人工 review；1–2 周不是自动切换条件。
- 数据完整、signal→order→receipt→position→capital→journal 闭环、actual costs、执行可行性、风险/回撤、市场覆盖和故障降级都满足，仍需 Nicholas 单独明确确认。
- 若未来确认，完整 50,000 CNY 账户仅以 20%–30% 初始订单敞口人工试运行；不得自动扩大。
- 邮件 → Nicholas → 同花顺人工下单仍未实现；在设计获审阅前不得编码或发邮件。broker automation gateway 属于更晚的独立项目。
- CNFutures 没有实盘日期，继续长期模拟。
