# TradingAgent 当前状态

> 最后更新：2026-07-14 08:18 CST。本文件只记录当前工作树证据、阻塞和下一门禁；长期规则见 [AGENTS.md](AGENTS.md)，运行命令见 [docs/operations.md](docs/operations.md)。

## 隔离候选：A股 sample ops P0（未集成）

- 候选 worktree：`/Users/nicholashan/Projects/Finance/.worktrees/tradingagent-sample-ops-p0`；分支 `codex/ashare-sample-ops-p0`；基线 `6c12fbed29db925019f85a6016774626f63b857a`。这是本地未提交候选，不代表 `main`、GitHub、生产文件/runtime、外部路由或真实业务写入已变化。
- 候选实现固定 evidence availability/receipt cutoff 与 canonical Journal head；同轮 labels/KPI/decision/maturity 共用 frozen H0 + 显式 task-owned delta H1，未知并发 append fail closed。
- exact pending snapshot IDs 消除 backlog 放大；单次 Journal parse/index、同 symbol/date/run-as-of 行情复用与 100–250 条批量 label append 降低重复扫描、调用、锁和 fsync。
- KPI/decision/maturity 共用 `projection_input_sha256`，通过内容寻址 generation + 单一原子 current pointer 发布；提供 append-only invalid/superseded audit，不修改历史。
- 独立验收复现的 5 个 reader/publication 缺口已在候选修复：final physical-H1 CAS 持锁穿过 pointer publish；pointer 封存 manifest content SHA；generation 已存在但 current 缺失/非法时健康检查 fail closed；nested PIT receipt/availability 全量校验；前端从 canonical current generation 读取并要求安全字段显式 false，绝不把 root mirrors 当事务点。
- Nicholas 提供的 Storage migration final evidence（本隔离任务未访问生产独立复核）显示：production A股 Journal 尾部 1,000 条 label update 全部 rejected，其中 996 条 `reference_timestamp_timezone_mismatch`、4 条 `missing_reference_price`；KPI cutoff 停在 14:20，Journal 延续到 19:03，`ready_labels=0`、`N_eff=0`、current-epoch trades=0，两条 sample-ops cron 仍按计划禁用。
- 候选诊断定位到 reference timestamp 与 prediction/data-as-of 的 naive/aware 比较路径。当前候选只把契约明确的 A股 `bar_time`/`trade_time` 无偏移原值绑定 `Asia/Shanghai`，统一按 UTC instant 比较并保留 raw/normalized/rule lineage；通用 timestamp、prediction、data-as-of、receipt 无时区以及未来/冲突证据仍 fail closed。`missing_reference_price` 改为 retryable/degraded pending，不伪造价格或 terminal label。
- 最新独立 review P0 已修：reference/entry 和每个 exit point 在候选排序前统一重算 PIT Evidence Gate；只有 `complete=true,status=valid` 且 receipts 不晚于冻结边界的证据可生成 ready label。非法/未来/nested-naive exit 保持 retryable pending/degraded，高价非法 point 不能影响合法 point 选择；不再先选 point 后仅附加 invalid lineage。
- 后续独立 review 的 generation/lineage/legacy 缺口也已在候选修复：publisher/Python/front 共用同一 generation identity 合同且 reader 从 input SHA + canonical 三投影 SHA map 重算 ID；reference/decision lineage 缺失、字段不全或不一致不再得到 verified；legacy-only health 强制 `legacy_degraded`、maturity evidence untrusted、promotion false，前端不读取 legacy mirrors。已补跨语言 golden、hash-consistent forged-ID、strict-lineage 与 legacy-green 负例。
- 最新双时钟 P0 已同时在 materializer 与所有原始入口收口：provider/bar/reference 在任何 first-nonempty 归一化前先构建 EvidenceEnvelope，保留 root、PIT root、PIT timestamps 与 adapter 的全部 event/receipt aliases 和来源路径。event aliases 必须解析为同一 UTC instant；所有 present receipt aliases 必须合法、带时区、顺序一致且最晚 receipt 不晚于边界。不能再用较早 `available_at` 覆盖晚到 `published_at`、用任务 `as_of` 填造 provider retrieval，或把首选 clock 复制成伪一致 nested lineage。顶层/nested 窗口、排序、`evidence_at` 与 lineage 只使用 validated canonical instant；冲突高价 point、naive secondary clock、hidden future receipt 均不能生成 ready，同一 instant 的 `+08:00`/UTC 表示通过，entry/reference 使用相同合同。
- `8eefddff...` 冻结候选被后续 independent acceptance 判定 FAIL，现已作废。该轮新增 reference selection P0 已修复：collector 对每个原始 row 传入真实 prediction boundary，先排除 invalid/future sibling，再从合法 rows 按 canonical event instant 选价；provider 输入顺序和无效高价不能控制 reference。没有合法 row 时 price 为空、`qualified=false`、snapshot pending/degraded、exploration not-selected；被过滤 row 只保留在 rejection audit，不进入 candidate/snapshot PIT。
- `fe2c5a69...` 冻结候选的 reference Phase A 通过，但 projection Phase A 复现 mirror-log 在 final generation validation 后被换成 hardlink 仍切换 current，因此 fe2c 已作废。六份 compatibility mirrors/logs 现已在写完后冻结完整身份，并在 pointer pre-`os.replace` callback 内逐项复验；mirror/log 任一 rename、symlink、hardlink、内容或 metadata 漂移都使 publisher 失败，旧 pointer bytes 逐字节保持。
- `2f4b5856...` 随后的 compatibility 矩阵通过，但 fresh projection reviewer 又复现 final generation validation 后把投影换成同字节、同 mode、不同 inode 仍可切换 current，因此 2f4b 也已作废。当前修复从同一次 final validation 封存 generation 目录及 manifest + 三投影的 path/dev/inode/mode/nlink/size/mtime_ns/ctime_ns/content SHA，并在 pointer callback 重验内容后与该身份逐项比较；内容相同不再能替代对象身份，失败保持旧 pointer 原始 bytes 不变。
- `2b982b62366d7daa3043e12a5ab3662cf52737b4c81d70e163d4679e7563e6fc` 随后被 backend Phase B 判定 FAIL，已正式作废，不能再作为 PASS 或主集成接手指纹。该轮全后端为 `1836 passed / 21 failed / 12 skipped`：8 项 localhost sandbox、6 项 base 同样失败、7 项候选新增；候选新增包括 CNFutures forward-label/maturity、3 项 A股 sample-lineage/exploration 与 2 项 sim-loop receipt fixture。
- `f02183b5...` 冻结候选虽曾得到完整后端 `1872 passed / 0 failed / 12 skipped`，但 fresh Phase A 后续复现 5 个 P1，因此该指纹和其“可接手”结论已作废。当前未冻结工作树正在修复这些缺口：非法 provider envelope 仍原样进入 Gate，同时保存独立 HTTP transport audit 并进入 cache；embedded `structure_errors` 在重复 canonicalization 中不可逆传播；receipt 顺序改为逐 alias 跨 stage 上下界校验；SampleJournal/lock 要求 single-link regular FD/path identity；maturity 与 actual-cost 共用 strict completed-round-trip validator。没有放松 PIT、data-quality、sim-only、authority 或 conservative-cost 门禁。
- 后续 `9dbe...` provisional 包又被独立复核证明 strict actual-cost 只验 SHA 形状而未绑定内容，因此同样作废且未冻结。当前修复要求 maturity 与 actual-cost 从同一 frozen view 唯一关联 prediction、entry fill、exit stop：重算 prediction canonical content/source binding、fill/stop canonical receipt/local-trade payload SHA，以及 round-trip source/content SHA；显式空 envelope、任意 64hex、payload/hash 任一方向漂移和 entry/exit fingerprint 错配全部回退 conservative cost。Fresh strict-cost 复验又发现 prediction source SHA 未与 frozen Journal 内容重算绑定、多腿 exit SHA 数组只做普通 list equality；当前未冻结工作树已让新 prediction append 保存 canonical source payload，validator 从 frozen event 重算并 constant-time 校验，历史缺 payload 继续 conservative；exit receipt/local-trade 数组改为等长、同序、逐元素 constant-time 校验。Journal identity/hardlink 复核已独立 PASS，本轮没有扩大该实现；非协作同 UID 窗口继续按既有 P1 OS 隔离边界处理。
- Fresh strict-cost 终审已独立 PASS（P0/P1=0）：决定性 5 节点 `5 passed`、0.31s，producer、frozen evidence index、constant-time digest、历史 conservative 回退和 production 同 frozen view 全部核对。`a6e86ab16eca6bb5689ea683dc117a4f679ac78d7bc209cb2eab4e214798af83` 仅是完整候选验收开始前的 historical strict-cost checkpoint，不是当前全候选的自证指纹；后续完整验收与 `STATUS.md` 自身的文档修订均会改变 diff。当前候选身份以仓外冻结 manifest 与交付报告为准。
- 首轮完整后端在 97% 处发现 4 个 sim-loop fixture 迁移漏项：模拟 broker 回执仅有 `fill_time`，没有可审计 execution receipt clocks，因此被未放松的 Evidence Gate 正确拒绝。只在测试自有 fixture 补入明确的 `filled_at/available_at/ingested_at/retrieved_as_of`，不从 prediction、`as_of` 或 wall clock 补造；4 节点 `4 passed`，整个 `test_sim_loop.py` 为 `65 passed / 6 subtests passed`、12.10s。修复后第二轮完整后端终态为 `1889 passed / 0 failed / 12 skipped / 101 subtests passed`、1105.24s（0:18:25）。12 个 skip 全部是 clean overlay 中明确缺少兄弟仓 SharedSignals `reader.py/api_server.py` 或 MarketGraph `_api_server.py` 的条件性跨仓 P1 edge-case，不是候选失败。
- Quick sim-only acceptance 为 `216 passed / 35 subtests passed`、67.94s。`2000 snapshots / 250 symbol-date / 8 variants` 性能基准为 `1 passed`、4.26s：logical/physical/cache-hit 分别为 4000/500/3500，Journal append batch/fsync 均为 10。Front canonical 为 `63/63`，front full 为 `218/218`；client build、API build 和 oxlint 全部通过。改动的 27 个 Python 文件 Ruff check/format 通过，仓内 361 个 Python 文件 compile 通过，7 个改动 Markdown 文件的 14 个本地链接目标全部存在。所有测试、cache、temp、JUnit、npm dependencies 和 build 产物均位于 `/private/tmp/tradingagent-freeze-20260714.OEZ4mh` 及仓外 artifacts，未写入源工作树 ignored 路径。
- 本候选未访问生产、数据库、cron、邮件、同花顺、broker 或真实交易；未 commit、merge、push、deploy，也未删除任何 worktree、Journal、ledger 或历史。

本节以下的生产与真实市场叙述继承自基线 STATUS，未由本隔离任务刷新；本轮只把本地候选、测试与版本控制层作为新鲜证据。

## 结论

- 本地重构已采用两个独立 fresh-start 50,000 CNY simulated authority；旧共享资金口径不再是当前设计。
- A股多风格 observation/exploration/exploitation、SampleJournal、forward labels、KPI、成熟度与原子资本 commit 已进入本地集成。
- CNFutures 独立资金、一手 affordability、会话样本、原子开/平仓资本 commit 和 durable outbox 已进入本地集成。
- 双市场 actual MTM reconcile writer、期货追加式 forward labels/maturity 与主页成熟度看板已完成集成；A股 `15:32 ops` 会在盘后固定价格交易结束后追加日级 MTM SampleJournal 证据。production managed cron 已通过项目 merge 工具完成 backup/dry-run/apply/readback，233 行 SHA 保持一致，coverage 56/56。
- A股执行现实统一为 `ashare-execution-reality-20260706-v1`：主板风险警示股 10%、独立收盘集合竞价/盘后固定价 session、价格笼子、100 股整手、卖方印花税 5bps、双向过户费各 0.1bps；华创佣金仍是待合同/交割单核实的保守临时口径。
- 样本科学口径已区分 label cells、raw N、unique decision clusters、独立交易日与 N_eff；PIT、校准、benchmark null、逐日 MTM 回撤和 universe recall 均不再由布尔字段或默认 0 自证。
- CNFutures 已加入 append-only order events、checksum projection 与 startup reconcile；目录漂移只 HALT 新增模拟风险，observation/counterfactual 继续。旧伪 Sharpe 已改为诊断比率，`sharpe=null` 且不得用于 DSR/晋级。
- `REAL_TRADING_ENABLED=false`；生产 systemd 已显式设置 `ASHARE_SIM_HERMES_ENABLED=0` 与 `ASHARE_SIM_WEBHOOK_ENABLED=0`，没有邮件、同花顺操作、broker 接入或真实交易。
- 2026-07-13 P0 修复已合入、push 并部署 production main `7db5a3c`：只读 capital 命令不再重写 latest；cron permission coverage 纳入两个 capital root、A股 execution root 与 CNFutures replay；新增 staging-only fixed-lineage/zero-import bootstrap CLI；SharedSignals gate 的 market 推断改为边界匹配，`opening_gate` red 现在会阻断 A股；A股 sample pipeline 将无时区 `bar_time` 绑定交易所时区并用真实 `collected_at` receipt 构建 PIT，present-but-invalid chronology fail closed。PIT/forward-label 相关 105 项通过；最终完整套件 1758 项通过、7 项时钟依赖失败与未改 `85fd3db` 基线的同一组失败一致。
- production 权限副作用已关闭：两个 capital latest 与 CNFutures replay latest/history 均为 `marketgraph:marketgraph 0600`；11:35 replay attempt=1 success。capital 只读命令不改 inode/mtime/SHA，cron coverage 为 56/56、runtime permission blocker=0。
- A股 production 已切换为固定 `ashare-sim-fresh-20260712-v1` 的独立 50,000 CNY authority 与 zero-import execution root；旧随机-lineage event root 整目录归档，旧 SHA 保持不变。隔离 reconcile 无 lineage mismatch，production 首次 `ops` reconcile 后为 2 events、fresh/reconciled=true、零持仓/成交/PnL、real=false；CNFutures 同时保持独立 50,000 CNY、fresh/reconciled=true。
- 当前仍不是“首日完整闭环完成”：A股 09:25/09:30 opening 已错过且不得补造，13:11 已实证按 red gate fail-closed；14:46 普通日内周期在 14:51:24 success，新增 2,000 条 prediction，其中 1,996 条 `eligible + PIT-complete`、4 条缺证据按设计 rejected，全部绑定当前 authority/lineage 且无 live marker。0 成交/0 回执并不影响 observation/risk-reject 样本成立；15:32 daily MTM 与 17:40 KPI/maturity 仍必须按实际时点验收。
- CNFutures 当日早盘因 release/source gate 未就绪而没有有效 `day_morning` 样本，禁止补造；`day_afternoon` session acceptance 已以 checksum verification 通过：495 条决策（494 hold、1 risk reject）、27 条 counterfactual-only、0 fill/real violation。风险拒绝样本包含完整 prediction、PIT、`cn-futures-capital-v1` generation 1 与 runtime lineage；一手 IF 保证金/损失预算超出 50,000 CNY authority 时保持 quantity=0。
- 15:32 A股正式日级 MTM 已由 managed cron 自主落地：SampleJournal 恰好一条 `account_daily_mtm_equity`，event time `2026-07-13T15:32:01+08:00`、equity 50,000 CNY，绑定当前 authority/generation/fixed lineage；capital chain 5 events valid，现金 50,000、持仓/预约/已实现与未实现 PnL 全为 0。17:40 sample ops 的 KPI/maturity 重建仍须按实际产物验收。

## 当前资本 authority

| 市场 | authority | generation | 初始权益 | 当前上限 |
|---|---|---:|---:|---|
| A股 | `ashare-capital-v1` | 1 | 50,000 CNY | 股票总敞口 45,000；单票 7,500；容量 8 |
| CNFutures | `cn-futures-capital-v1` | 1 | 50,000 CNY | 保证金使用 25,000 |

- 两市场各自持有现金、预约、持仓/保证金、MTM、PnL、回撤、checksum chain 与 execution lineage；总览不做货币聚合。
- 5% 回撤仅收紧到 0.75 倍风险预算；7% 回撤暂停。日亏与连续亏损也按市场独立触发。
- 旧资本事件、持仓和 PnL 已定义为只读冻结源；新 authority 不继承、不导入。production 两个默认 root 各 50,000 CNY、零持仓/预约/PnL、`real=false`；A股 active capital/execution lineage 已一致，旧随机 append-only root 只归档保留且禁止改写。

## A股本地证据

- 资金计划从 A股独立 policy 计算 90% gross、15% 单票、8 个仓位容量，并输出 deployed/committed/planned utilization、dynamic operating cash、undeployed capital/reasons。
- 四类正交风格共享候选、生成 counterfactual，并由单一组合账户消除同票重复成交。
- Exploration 使用 top-K epsilon/分层随机，记录 selection probability/propensity；每日最多一个、累计 7,500 CNY、日亏 225 CNY。
- 本地 fill/partial/sell 通过 durable outbox 与 capital ledger 原子提交，使用 actual quantity/price、commission/stamp duty/transfer fee、PIT lineage、receipt/local-trade fingerprints 和 ledger-head CAS；预约和成交账本读取同一 ExecutionRealityModel。
- A股 `SampleJournal` 保存 observation、exploration、exploitation、round trip、exit/stop、risk reject 与 chain validation；统一 sample ops 生成标签、KPI、manual-only evolution decision 和 maturity 投影。15:31 前的资本 checkpoint 不会冒充每日正式 MTM 回撤点。
- 第 5、10 个交易日只触发人工复核状态；自动晋级、自动扩风险和 live transition 均关闭。

尚缺的运行证据：下个有效交易日起连续每日 prediction、到期标签、探索/成熟成交、完整回合、资金利用率、未部署原因和 day-5/day-10 人工复核记录。

## CNFutures 本地证据

- 每个有效会话要求 prediction/candidate/hold/risk reject/simulated fill 之一，并区分方向预测与当前本金可执行性。
- 真实一手、保证金、费用、滑点、夜盘、换月和风险预算适配时才允许 execution-eligible simulated fill；否则保留 counterfactual。
- 开仓 `fill_commit` 与平仓 `position_close_commit` 使用 actual fill、margin、fee/PnL、PIT lineage、CAS 和 durable outbox；pending action 保守阻断新增风险并可重放。
- 期货 sample ops 会先追加 `m30/m60/close/1d/3d/5d` 标签，再按 exact authority/lineage、cluster 权重和 actual round-trip evidence 重建 maturity；无签名 summary、旧 authority 或重复 cluster 不计数。
- 期货成熟度已接入独立 runtime 投影和主页 read model，按品种、波动/会话、夜盘、换月、极端风险、费用后结果和稳定性展示，不读取 A股模拟天数。订单事件 projection 可从 append-only journal 重建；篡改或目录漂移会显式 HALT 新增风险。

尚缺的运行证据：不同有效会话和品种的连续记录、后续标签、真实规格模拟完整回合、夜盘/换月/极端场景覆盖与长期稳定性。

## 样本与演化 authority

- `shared/review/ashare/sample_journal.jsonl` 是 A股唯一演化事实源；KPI、decision 和 maturity 是可重建投影。
- paired MG 消融使用同一 immutable base snapshot；`mg_off` 不读取 MG 特征。
- heuristic score 与 uncalibrated prior 保持原名，不能包装为已校准概率或已验证预期收益。
- 自动 lifecycle/risk promotion 已关闭。证据 readiness 与 Nicholas 的人工授权是两个独立状态。

## 本地验证层级

- 完整后端：`1889 passed, 12 skipped, 101 subtests passed`，0 failed；skip 为 clean overlay 缺兄弟仓源文件的 12 个条件性跨仓 P1 edge-case。
- Quick sim-only acceptance：`216 passed, 35 subtests passed`。
- A股 sample-ops 性能基准：2,000 predictions / 250 symbol-date / 8 variants，physical provider calls 上界 500，`1 passed`。
- 前端：canonical `63 passed`，40 个 test files / `218 passed`；oxlint、client build 与 API build 通过。
- 改动的 27 个 Python 文件 Ruff/format、仓内 361 个 Python 文件 compile、`git diff --check` 和改动 Markdown 本地链接检查通过。
- 测试 fixture、隔离临时账本和浏览器截图不是真实市场样本，也不证明策略正期望。

## 当前阻塞与下一门禁

1. A股普通日内首样本门禁已完成：14:46 周期新增 2,000 条 observation/prediction，1,996 条 eligible/PIT-complete；早先 17 条 risk reject 保留，0 fill。明确 `missed_opening=true`，不得补造 09:30 证据。
2. CNFutures `day_afternoon` 已通过真实有效会话验收，11:35 replay 有 636 windows/3 counterfactual rejects；`day_morning` 缺失与 opening 错过必须保留为缺口，不得补造。
3. A股 15:32 daily MTM、资金守恒与唯一日级 SampleJournal 已验证；17:40 sample ops 后验证 forward labels、KPI、maturity，未到时点只等待不伪造。
4. 只有上述运行证据、三仓文档、GitHub/production readback 与 rollback 证据齐全后才评估 worktree/本地分支清理；append-only ledger、样本、归档和运行证据不得删除。

## 环境层级

| 层级 | 当前事实 |
|---|---|
| 本地工作树 | 主工作树 `main` 干净、HEAD `6c12fbe`；本隔离候选同基线、37 个 tracked+untracked 改动/新增文件，未提交 |
| GitHub | 开工 fetch 后 `origin/main=6c12fbe`；本候选未 push，任务结束时未再次联网刷新 |
| 生产文件/runtime | 本隔离任务禁止访问，未验证、未改变；不能从本地候选或测试推断 |
| 生产 cron | 本隔离任务禁止访问/apply，未验证、未改变 |
| 外部邮件/同花顺/broker | 未实现、未发送、未连接 |
| 真实市场样本 | 2026-07-13 opening 已错过且不补造；14:46 普通日内新增 2,000 predictions，1,996 eligible/PIT-complete、4 rejected、0 fill/live；15:32 唯一 MTM/50,000 CNY 守恒已验证；17:40 KPI/maturity 待实际产物 |
