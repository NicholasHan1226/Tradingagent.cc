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

当既有 A 股 capital bootstrap 的随机 lineage 与 canonical execution lineage 不一致、且尚无首个策略样本时，不得手写 manifest 或修改 append-only event。先用 staging-only CLI 生成一对新的 zero-import authority；该命令默认 dry run，拒绝生产默认 root、非 50,000 CNY、任何持仓/预留/盈亏、未来 PIT 和 `REAL_TRADING_ENABLED=true`：

```bash
python3 tools/ashare_fresh_authority_bootstrap.py \
  --capital-root /path/to/staging/capital/ashare \
  --execution-root /path/to/staging/execution/ashare-sim-fresh-20260712-v1 \
  --source-opening-manifest /path/to/operator/opening_manifest.json \
  --legacy-freeze-manifest /path/to/operator/legacy_freeze_manifest.json \
  --output-opening-manifest /path/to/new/evidence/opening_manifest.json \
  --lineage-started-at YYYY-MM-DDTHH:MM:SS+08:00 \
  --point-in-time-as-of YYYY-MM-DDTHH:MM:SS+08:00 \
  --confirm-zero-import
```

只有 dry run、`--apply`、capital checksum、execution manifest、非 root 用户读写和隔离 reconcile 均通过后，才可在独立备份下把两个 staging root 原子切换为 production default。CLI 本身不激活 production root，也不写 fresh/reconciled 状态。

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
  --label-batch-size 200 \
  --pretty
```

`--as-of` 是 evidence availability/receipt cutoff。Journal 任一纳入候选行在顶层或 `point_in_time_lineage[.timestamps]` 中出现非法/无时区 receipt/availability，或没有任何可用 receipt 时，任务必须在写 label/投影前 fail closed。运行报告应检查 frozen head、pending/selected/terminal、Journal parse/bytes/lock/fsync、HTTP logical/physical/cache/timeout/retry/latency、as-of drift、task-owned delta、最终 physical-H1 CAS、共同 `projection_input_sha256` 与 generation ID。

reference 时间诊断必须同时查看 `data_quality.price_timestamp`、reference timestamp lineage、`prediction_at`、`data_as_of`、decision timestamp lineage 与 PIT receipt chain。reference/decision lineage 必须显式存在，并保存 source/raw/normalized/semantics/rule/valid；缺失或字段不完整只能 pending/degraded，present-but-invalid、raw/normalized 冲突或 instant 不匹配必须 data-quality fail closed，不能回退为 `verified_reference_data`。只有字段名明确为 A股交易所 `bar_time`/`trade_time` 的无偏移原值可按 `Asia/Shanghai` 标准化；通用 timestamp、prediction、data-as-of 或 receipt 无时区时继续 fail closed。`missing_reference_price` 是 retryable/degraded pending，不得用收盘价、前值或零值代填，也不得伪造成 terminal label。

forward-label 验收必须从 provider/bar/reference 原始入口检查统一 EvidenceEnvelope，而不能只直接调用 materializer。所有 present event aliases 必须保留原始路径、逐一解析并表示同一 UTC instant；`bar_time/trade_time` 的明确 A股墙钟可绑定 `Asia/Shanghai`，通用 secondary timestamp 无时区则阻断。root、PIT root、PIT `timestamps` 的 21 条 Journal receipt/availability 路径以及 provider `published_at/retrieved_at/collected_at_dt` 均须逐一解析；取最晚 evidence receipt 与 prediction/label boundary 比较，任一 future/invalid/naive 阻断，不能用较早别名或任务 `as_of` 覆盖。跨 stage 还必须验证 `event <= min(all receipts)`、`max(availability) <= min(ingestion)`、`max(ingestion) <= min(retrieval)`；单个同组晚值不能掩盖较早的反序 alias。embedded `structure_errors` 要在首次、二次和多次 canonicalization 后始终 invalid 且去重。reference collector 必须逐 row 传入真实 prediction boundary 并在选择价格前过滤无效行；合法低价与冲突/未来高价 sibling 的两种输入顺序都必须得到同一合法 reference。无合法 row 时应看到 null reference、`qualified=false`、snapshot pending/degraded、exploration not-selected；被过滤 sibling 只能在 rejection audit 中出现，不能进入 candidate/snapshot PIT。只有完整 envelope 验证后才允许合成 nested 四钟，并保留原始字段审计。测试还必须覆盖 only-conflict、naive secondary、`+08:00`/UTC 同义、hidden future published/received、future retrieved_at，以及 21 receipt 路径逐一 future 全部 non-ready。

CNFutures 的同一验收必须从 SharedSignals HTTP response 开始：确认实际 response receipt 在 cache 前写入合法 row envelope；provider envelope 或 retrieval group 非 mapping 时必须原样保留非法值，并在 sibling `sharedsignals_response_lineage` 保存真实 HTTP endpoint/received-at，cache 第二读一致且 HTTP 只发生一次。transport audit 不得让下游变 ready。prediction snapshot、session review 和 `_price_evidence` 保留所有原始 event/receipt aliases、structure errors 与 nested PIT。valid receipt 的 reference/exit 可形成六个 horizon；missing/invalid/naive/future/conflicting receipt 全部 non-ready，provider 输入顺序不改变合法 point 的选择，历史缺 receipt 不得用 `as_of` 或 bar/prediction time 修补。

SampleJournal 文件与 `.<journal>.lock` 的安全验收必须覆盖 journal hardlink、lock hardlink、journal/parent symlink，以及取得协作锁并封存既有 inode 后的 path replacement。每项都应在 append 前失败，并逐字节确认外部 target 未变化；普通 append/batch/crash replay、frozen H0/H1 与 projection-head guard 仍需通过。不要为修复 hardlink 去改写或删除既有 Journal。

actual-cost 验收必须用同一 frozen Journal view 中的真实 prediction、entry fill、exit stop 与 completed-round-trip fixture。validator 要从权威 prediction event 保存的 canonical `source_snapshot_payload` 重算 source SHA 与 canonical content SHA，从 fill/stop 的显式 canonical receipt/local-trade payload 重算 fingerprint，再重算 round-trip source/content SHA；supplied SHA 必须 constant-time 等于重算结果，不能只验 64-hex 形状。随后分别测试任意 64hex + 缺 source payload、source payload 错绑、prediction SHA 错绑、显式空 envelope + 顶层便利字段、payload 改而 hash 不改、hash 改而 payload 不改，以及 entry/exit fingerprint 错配。多腿 exit 还要覆盖 receipt/local-trade SHA 数组换序、单元素漂移与长度差，并继续覆盖 invalid/naive/future/conflicting receipt。每项都必须确认 `actual_execution_cost_used=0`、`actual_execution_costs_v1` 不被选用、版本化保守成本仍在。历史 prediction 缺 source payload 时应继续 conservative，不得补造。maturity 与 cost path 必须调用同一 strict validator 和同一 frozen evidence index，禁止各自维护较松规则。

canonical 投影入口是 `projection_current.json` 指向的 `projection_generations/<generation_id>/`；reader 必须先用 pointer 的 `generation_manifest_sha256` 校验 manifest 原始内容，再校验 manifest metadata、共同 input SHA、显式 false 的全部安全字段与三个 projection SHA。随后必须按 data contract 的 canonical identity 算法从 input SHA + 三 projection SHA map 重算 generation ID，并与 pointer、directory 和 manifest 全等；复制 projection 并重签 manifest/pointer 到伪造 ID 必须阻断。publisher 遇到已存在 generation 时必须在写 mirrors/current 前调用同一个完整 validator：目录只能有 manifest + 三投影四个 regular non-symlink/single-link 文件，逐文件 raw SHA、JSON、input lineage 与安全字段全通过；manifest-only、缺文件、extra、symlink、hardlink、可写 generation 或 tamper 均不得改变旧 current bytes。完整同内容幂等复用必须先封存为只读。整个 generation/mirror/log/final validation/pointer swap 在 `.projection_publish.lock` 独占锁内；final generation validation 必须封存目录及四文件的 path/dev/inode/mode/nlink/size/mtime_ns/ctime_ns/content-SHA，三 mirrors + 三 logs 写完后也保存相同身份。pointer 临时文件 fsync 后、`os.replace` 前在同一 callback 内重新 FD 校验并与两组快照逐项相等。运维负例必须在 final-validation seam 注入 generation 同字节、同 mode、不同 inode 替换，并分别对 mirror 和 log 注入 rename、symlink、hardlink；每项都必须断言 publisher 失败、旧 pointer bytes 完全不变。只重算内容 hash 或仅在 append 时检查 link count 都不足以关闭窗口。`sample_kpi_latest.json`/log、`evolution_decision_latest.json`/log、`market_maturity_latest.json`/log 继续写出供旧消费者兼容，但不能作为跨三文件原子发布证明。generation 已存在或 `TRADINGAGENT_ASHARE_CANONICAL_PROJECTIONS_REQUIRED=true` 时，current 缺失/非法必须报警并 fail closed，不能回退 latest；操作员可从 Journal 重建新的完整 generation，禁止反向改写 journal。

明确 legacy-only 且从未出现 generation 体系时，health 可只读 mirrors 统计诊断量，但必须输出 degraded/legacy 证据，并强制 `maturity_stage=legacy_degraded`、`maturity_evidence_trusted=false`、`promotion_evidence_ready=false`、自动晋级/扩风险/live 全部 false。前端不把 legacy mirrors 作为 active maturity/KPI reader；missing/invalid current 必须返回无 canonical maturity，而不是展示成熟绿灯。

若 generation 构建中断且 current 未替换，保持旧 current，不手工拼接三份 latest。若发现已污染投影，只追加 `invalid`/`superseded` audit；本地候选代码不授权修改生产 history。SharedSignals batch API、HTTP 并发、持久化 sidecar index 和增量 KPI 均未在此 P0 实现。

`job_ashare_sample_ops.sh` 是唯一活跃 A股 labels/KPI/evolution-assessment/maturity wrapper。旧 sample-learning、旧 forward-validation、旧 portfolio-evolution 和重复监控入口不得恢复。

当前由运行证据指出的两条 sample-ops cron 保持禁用；本候选不恢复或应用 cron。未来重新启用前必须一次性满足并留存以下门禁：

1. 在隔离副本或获批的 sim-only 单次受控运行中，新增 label update 的 `reference_timestamp_timezone_mismatch` 为 0；非法/未来/冲突 fixture 仍按预期拒绝，缺价仍为 retryable degraded。
2. canonical `projection_current.json` 通过 manifest/content/projection SHA 校验；KPI `data_as_of` 追平批准的 Journal cutoff，`H1` 与该次运行结束时 physical Journal fresh head 一致，三份投影共享同一 `projection_input_sha256`，且没有未解释的 cutoff/exclusion 漂移。
3. 该轮使用的既有 SharedSignals `source_status` 中与 A股行情/receipt 相关的来源均无 red；本门禁只消费现有状态，不扩展 SharedSignals schema。
4. 执行前明确记录单次资源预算：frozen Journal 事件/字节上限、exact pending IDs/unique symbol-date 数量、wall/CPU/RSS 上限、HTTP physical request 上限（不得超过 `2 × unique symbol-date`）、timeout/retry 上限、batch size 100–250 与预计 fsync 次数；运行实际指标必须在预算内且不得开启 P1 请求并发。
5. 上述证据经人工复核后，才可按独立 cron 变更流程做 export、diff、backup、apply/readback 和 rollback 验证；本地候选测试、latest mirrors 或单个 health=200 均不能替代该授权。
6. 完成 production writer inventory：逐项列出会写 SampleJournal、generation、current、compatibility mirrors/logs 的 cron/service/手工入口并确认全部使用协作锁；read back 每个 writer 的 UID/GID、目标路径 owner/mode/ACL、mount options、filesystem 类型与 rename/link 语义。任何未登记 writer、锁绕过或权限证据缺失都保持 cron disabled。最后一次用户态 validation 返回到 kernel rename 间的非协作同 UID 窗口按 P1 OS 隔离处理，当前锁协议不宣称覆盖该威胁。

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
7. generation 发布失败时保留上一 `projection_current.json`；不要通过逐个覆盖三份 current 文件“补齐”。回退只能把 current 原子指向已校验的完整旧 generation，并追加 superseded audit，不能删除 generation。
8. 前端可独立回滚到旧只读 build，但仍不得展示跨市场货币聚合或写入接口。

禁止的“回滚”：删除/改写 capital events、清空 PnL/持仓、改变 generation、导入历史冻结数据、把两个账户合并、恢复退役 cron/writer、发送补偿邮件或开启真实交易。

## 11. 实盘晋级边界

- A股 day 5/day 10 只做人工 review；1–2 周不是自动切换条件。
- 数据完整、signal→order→receipt→position→capital→journal 闭环、actual costs、执行可行性、风险/回撤、市场覆盖和故障降级都满足，仍需 Nicholas 单独明确确认。
- 若未来确认，完整 50,000 CNY 账户仅以 20%–30% 初始订单敞口人工试运行；不得自动扩大。
- 邮件 → Nicholas → 同花顺人工下单仍未实现；在设计获审阅前不得编码或发邮件。broker automation gateway 属于更晚的独立项目。
- CNFutures 没有实盘日期，继续长期模拟。
