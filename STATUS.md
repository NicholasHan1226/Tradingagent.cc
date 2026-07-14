# TradingAgent 当前状态

> 最后更新：2026-07-14 22:22 CST。本文件只记录当前工作树证据、阻塞和下一门禁；长期规则见 [AGENTS.md](AGENTS.md)，运行命令见 [docs/operations.md](docs/operations.md)。

## 当前 GitHub 集成状态（本地/GitHub 层）

- 2026-07-14 22:22 CST fresh fetch、`ls-remote` 与分叉审计：本地 `main`、`origin/main`、live GitHub `main` 均为 `5689c95383244b689ced7d6c19a3ba2fc5c08bc4`，`behind/ahead=0/0`。main tracked/index clean；仅保留既有 `.codegraphcontext/db/falkordb` 与 `.codegraphcontext/db/falkordb.settings` untracked/占用，未触碰。
- 已串行集成并普通 push：sample-ops `4a220ef5f15390bfdc3b600a349a9cbe8b74da94`、notification `9b243208a2584867df4431336d26af7cb9da1c6f`、capital authority `af078070a57de9a806d009dcbdb7ea32f9ac97b2`、baseline hygiene `ece93a1712851cb8aaee9469c125eecbfeb8357d`、sector-flow shadow `5689c95383244b689ced7d6c19a3ba2fc5c08bc4`。
- 上述仅证明本地/GitHub 代码层；本轮没有部署、cron、数据库、Journal/sample_ops、邮件、broker 或真实交易操作，不能替代生产文件/runtime、外部路由或真实样本验收。

## 已集成 GitHub、未生产：A股 sector flow confirmation（v5）

- replay worktree 为 `/private/tmp/tradingagent-sector-flow-v4-replay-ece93a1`，detached 基线 `ece93a1712851cb8aaee9469c125eecbfeb8357d`，仅保留为重放证据。v5 已两路 fresh 独立 PASS，并以普通 commit/push 集成为 `5689c95383244b689ced7d6c19a3ba2fc5c08bc4`；本地/GitHub 层已 readback，生产按本任务禁区未访问或部署。
- ece93a 基线卫生事实继续保留：Wave2 A股 position authority 按 fail-closed 合同阻断非法 authority；`shared/screening/condition_generator.py` 没有带回历史未使用 `last_close`。Capital、notification 及下方当前状态章节均未被旧 9b 文本覆盖。
- 旧 ece93a 候选 aggregate `cc6a043e74b50282323139bc67541759d2f30aaef438b8af2325fecb1d84cf8e`、manifest SHA `6bd682727ebf8931bd8e9142710772f5a7b814d823aece513b2354ae34202f4c` 与 full diff SHA `fdaca8d3987219b68e0a83a18efa87832e331473ba5b594d82b977471f17c240` 已因 fresh review P1 作废，不得交 main 或归档为通过证据。缺口是请求/快照 sector ID、snapshot ID、taxonomy 和 scope 在验证前经 `str()` 隐式转换，bool/number 可形成 confirmed 记录。
- v5 仍严格限定原精确 8 文件。修复只收紧 identity 入口：scope、请求/快照 sector ID、snapshot ID 与 taxonomy 在任何 trim/coercion 前必须是原生非空 string；非法类型或空值一律 degraded，`pair_identity_valid=false`、pair SHA 为 `null`，off/on 回执绑定同一个空 identity 且始终 `consumed=false`。没有增加 decision consumer、资本、风险或执行 authority。
- rotation 数值触发保持不变，只把 symbol-scoped `moneyflow` 明确为 `flow_scope=individual_stock` / `individual_net_inflow`，不再误称板块资金。canonical payload SHA constant-time binding、finite 原生资金值、严格整数 rank、PIT chronology、paired base/decision identity 和完整消费回执均保持原合同。
- TDD 证据：新增 scope/request sector/snapshot sector/snapshot ID/taxonomy × bool/int/float/list/mapping/None/empty/blank 的 40 项矩阵在修复前 `40 failed / 22 deselected`，修复后 `40 passed / 22 deselected`。缺 scope 的历史 reason 兼容性回归曾使独立包 `63 passed / 1 failed`，最小兼容调整后重新得到 `64 passed`；`None` 仍被拒绝且不能形成 pair identity。
- 最终代码上的 sector 验收全部通过：rank16 `16 passed / 46 deselected`、core21 `21 passed / 46 deselected`、feature62 `62 passed`、core67 `67 passed`、Fresh `8 passed`、独立包 `64 passed`、expanded107 `107 passed / 46 deselected`、expanded153 `153 passed`、Wave2 `4 passed`。expanded 使用历史精确 10 文件组成；两次漏收集的 62/108 与 65/111 结果不冒充 expanded，也不进入冻结验收清单。
- Capital/notification 组合全部通过：position-source/pause `29 passed`、capital authority `14 passed`、sim-loop `66 passed`、notification+opening+sim-loop+capital `118 passed`、Wave2/condition/moneyflow/capital 精确组合 `55 passed`。
- Ruff 0.15.14 对 4 个候选 Python 文件、4 文件 compile、完整 8 文件 diff、Markdown 本地链接、禁止路径和 worktree hygiene 已在集成前后复核通过。新鲜 JUnit/cache/pyc/basetemp 位于仓外临时目录；最终 manifest 只列入明确验收的 JUnit，不把中间误收集结果当作通过证据。
- 冻结身份仍以 `/private/tmp/tradingagent-sector-flow-v5-ece93a1-freeze-20260714.manifest.txt` 与 `/private/tmp/tradingagent-sector-flow-v5-ece93a1.full.diff` 为准；其 v5 aggregate `71f71a5e0e9bad8f6dd1175f3c9238c4be3e2c1f99ed748005d0ecf8094b4e74` 与 full diff SHA `13b1ec555fe1d63830cb0a9ab0d93a61cb523d11d080586ce6a9aaecbb1ee859` 已匹配 GitHub commit。仍未访问或修改 orchestrator、wrapper、capital/risk、sample_ops/projection、forecast、生产、cron、数据库、Journal、broker、邮件或真实交易。
- shadow 接口说明见 [docs/sector_flow_confirmation_shadow_handoff.md](docs/sector_flow_confirmation_shadow_handoff.md)。

## 历史候选/失败验收记录：A股 capital position authority / risk P0 v2（最终已集成 GitHub `af078070`，未生产）

- 以下 overlay、作废指纹与“未 commit/push”措辞均为历史候选审计证据。最终通过的 capital authority 结果已集成 GitHub `af078070a57de9a806d009dcbdb7ea32f9ac97b2`；该 GitHub 状态不表示生产已部署或生产 runtime 已刷新。
- v2 源 worktree：`/Users/nicholashan/Projects/Finance/.worktrees/tradingagent-capital-authority-risk-p0-v2`；分支 `codex/capital-authority-risk-p0-v2`；原始基线 `4a220ef5f15390bfdc3b600a349a9cbe8b74da94`。源候选仅保留 patch 来源，不再作为最终组合证据；本地未提交 overlay 不代表 main、GitHub、生产文件/runtime、cron、broker、邮件或真实交易发生变化。
- 旧 `/Users/nicholashan/Projects/Finance/.worktrees/tradingagent-capital-authority-risk-p0` 候选经 fresh reviewer 判定 FAIL/NO-GO，按原样冻结，仅作失败证据；v2 没有在其上叠补，也不得提交旧候选。
- v2 首轮冻结的 content aggregate `56dd533396ed759223889fb4ce4ffba69e730dbd55e687c645804f825027a855` 与 full diff `36ccc3326a9655f2afee2f982c990d76b966960f15cd80dc39a47e127ef96623` 经 fresh reviewer 判定 2P0+1P1 FAIL/NO-GO，已作废且不得复用：shadow A股入口仍可进入普通 risk、adapter 会在读取后制造 current identity/接受别名、generic loader 没有真实 complete-envelope 通过路径。第二轮已将 shadow 纳入同一前置门禁，删除 after-the-fact binder/别名回退，并让 generic loader 只透传 producer 自有完整 dict envelope；legacy list 保持 unbound fail closed。
- v2 第二轮冻结的 content aggregate `5c3566690da72376d827beeabc8b9af14a1ebee34db869e73efe5372bd1cb0ac` 与 full diff `c470a5b69686c54679329c68990078ba251420e7e8414bb821845623ac69760d` 也已作废：冻结后自审复现 position authority verified、但 broader market-capital risk state 阻断时，shadow resolver 错误返回 `reason=approved`，sim 还可能跳过 sources reconciliation 后保留 verified 外壳。该轮将所有 broader risk blocker 等同 authority invalid 的做法又被后续 fresh P1 证明过度阻断；当前合同只把结构/来源失败视为 authority invalid，日亏/连亏/回撤 pause 单独进入 new-risk eligibility。
- v2 第三轮冻结的 content aggregate `4fcd934d4d3a922983d7f4905be276bbb8e2994ec5d5cdec23b0fa0464199129` 与 full diff `98f1b7bee845e89c13fccb210a10e5ee9fcec2c5cc03f78d5344765934a68c47` 同样已作废：冻结后继续入口审计发现 post-execution capital-plan refresh 会直接重读 adapter account，并把未验证 positions/cash 当成当前计划。当前修正要求有 fill 后重新运行 capital A → sources → B；refresh 只接受新 verified view 和 market-capital cash，source 未同步或不一致时明确 blocked。
- 9b 首次正式冻结 `eb903765d4e743daa70d62a1f67c0145ba6dc260b2c4d27fb68faeac1994315b` / full diff `c8ffdbf297e0df4b2607f821976b73eb5ce2658162f3ebadc9ba7fa554807669` 经独立验收判定 1P0+2P1 FAIL/NO-GO，已作废且不得复用：`run_gate_review` 未执行 broader capital risk gate；已提交资本的 partial fill 不触发 post-execution authority refresh；native server-local/adapter producer 没有可生成完整 current envelope 的真实路径。当前修正使 gate review 在 generic loader 前验证 broader state，`filled+partial` 都重新执行 A/source/B，并让 `local_sim_ledger` 在读取交易事实前接收 verified authority context、由 producer 自己重放零仓/非零仓并生成 envelope；adapter 只透传 live producer envelope，无 context 的磁盘 reporting snapshot 保持 blocked。
- 随后的 16 文件冻结 content aggregate `210b38453bf1458160981ac9c8b99bf4087a55515a6a40ba4ce375cb31c5d18d` / full diff `8d19a18f05ae065b8fc07d1c4dde844f246f9808861f606ec6864d64d36656d0` 经 fresh 验收判定 0P0+1P1 FAIL，已作废且不得复用：真实 `shared.accounting.position_ledger.get_positions` 返回裸 `list[dict]`，旧正测却 monkeypatch 为生产上不可能的 dict envelope，导致真实零仓/非零仓永久 `legacy_unbound`。当前修正不接受裸 list，也不在读取后补 identity；`run_gate_review` 先验证 capital A/broader risk，再把 verified authority context 传给 active `local_sim_ledger`，由 producer 自己重放 execution facts 并生成 source-owned envelope，最后执行 capital B 并发绑定。
- 再后冻结的 content aggregate `3d5fdb8c20d25dd8054b5d04bfe03b84950dc04dcfd17ff9282234b0a3066066` / full diff `ba076bf5cc5815fa5cbc0687d711b2e60bd7b07ce1c927671210cd5972b18973` 经 fresh regression review 判定 0P0+1P1 FAIL，已作废且不得复用：position authority 已 verified、真实 local_sim 持有 `600000.SH=100` 时，仅 daily-loss “暂停新增”就会清空 positions 并阻断 sell；wrapper 也未区分方向。当前修正把 position authority validity 与 new-risk eligibility 分开，三类 pause 保留 verified positions，只在普通风险前阻断 buy/open/add；sell/trim/exit 继续执行 source-owned `entry_date`、T+1、幂等、成交与 capital commit。真正 authority invalid/mismatch 仍清空 positions 并全阻断。
- v2 在任何 A股普通 risk、仓位容量、动态 capital plan 或 rebalance 前，从 current market capital ledger 建立唯一可重放 position authority view，并以 capital A → sources → capital B 双读绑定 checksum、authority/generation、execution lineage、trade date、canonical positions/count/fingerprint。缺字段、非法内容、陈旧或并发漂移统一 fail closed 为 `capital_position_source_mismatch`，不再读取 legacy/strategy 后产生普通“8 仓已满”拒绝。
- checksum status/last/event count、显式 positions mapping、严格 A股 symbol/整数 quantity、source envelope 与所有声明/重算值均是必需证据；缺 positions 不推断为空仓。T+1、100 股整手、50,000 CNY、90% gross、15% 单票、8 仓、sim-only 与人工晋级边界未放宽，CNFutures 走独立路径。
- 9b overlay 当前结构性修复后新鲜 pytest 证据：真实 local-sim producer + gate-review 矩阵 28/28；capital 专测 14/14；`tests/test_sim_loop.py` 66/66；native local-ledger 49/49；notification 四态/handlers/opening/daily-brief + capital 组合 118/118；当前结构聚焦集 164/164；adapter/runtime/preopen 52/52；A股政策守恒 92/92；CNFutures 15 文件 331/331，全部 0 failed/0 errors/0 skipped。全新 JUnit 位于 `/private/tmp/ta-cap-v2-main9b-p1-fix.NeaXn7`；9 个改动 Python 文件 Ruff check/format、全仓 364 个 Python 文件 compile 与 `git diff --check` 均通过，notification 的 8 个非-wrapper 保护路径与 9b base byte-identical。所有旧 JUnit 与旧冻结指纹只作失败历史，不替代本轮证据。
- 冲突审计：本地 `main`、`origin/main` 与 overlay base 都是 `9b243208a2584867df4431336d26af7cb9da1c6f`；主工作树另有本任务未创建、未读取、未修改的 untracked `.codegraphcontext/`，因此不宣称 main working tree clean。sector-flow 候选也基于 9b，与 capital 文件级交集为 `STATUS.md`、`docs/data_contract.md`；data contract 是不同 hunk，`STATUS.md` 因双方都修改首行时间并在其后插入候选章节而存在真实文本 hunk 冲突，串行接手必须手工保留两节，禁止整文件覆盖。生产代码、wrapper、risk/capital/execution 与 sector 候选无文件交集。
- notification 集成的原保护路径及其 notification 测试均未修改；本候选的测试改动仅为 `tests/test_sim_loop.py`、全新 capital-authority 专测和全新真实 producer 矩阵。双方共有文件仅为 `shared/wrappers/tradings_cron_entry.py`，本候选 hunk 限于 capital authority/server-local source/risk gate，未改 email 映射、`run_email_notify` 或 morning/day/night/weekly/system-health 通知状态语义。本候选未访问生产、默认数据库、Journal/ledger、cron、broker、邮件或同花顺，且未 commit、merge、push、deploy。

## 历史候选/失败验收记录：A股 sample ops P0（最终已集成 GitHub `4a220ef`，未生产）

- 以下 source worktree、作废指纹与“未 commit/push”措辞均为历史候选审计证据。最终通过的 sample-ops 结果已集成 GitHub `4a220ef5f15390bfdc3b600a349a9cbe8b74da94`；该 GitHub 状态不表示生产已部署或生产 runtime 已刷新。
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

## 当前阻塞与下一门禁（生产截至 2026-07-14 18:05–18:10 CST 的只读事实）

1. 生产仍为 **NO-GO**，没有稳定的当日学习闭环：SharedSignals `/source_status` 为 RED（9 green / 2 yellow / 1 red，red=`opening_gate`）；`/opening_gate` 为 red/closed，`phase=afternoon_resume`，A股 5 分钟当前阶段样本缺失。接口 runtime 同为 yellow（empty 19 / unobserved 35 / observed 79；`SW2021` yellow）。服务 active、8082/8080/8787 HTTP 200 不能替代这些 source/gate 事实。
2. 截至该只读时点，production TradingAgent 仍为 `6c12fbed` 且有 7 个 untracked；不得把当前 GitHub `5689c953`、本地测试或 HTTP 200 误称为生产发布。生产文件/runtime 本轮未重新访问，以上只读时间点之后的变化未验证。
3. A股与 CNFutures 两个独立 50,000 CNY authority 当时均 fresh/reconciled、checksum 有效、`real=false`，且 0 positions / 0 fills；这只证明 simulated authority 的该时点账面，不证明可交易或学习闭环。
4. 两条 A股 `sample_ops` cron 当时仍禁用；Journal 为 19,806 条、最后事件 09:36，KPI/evolution/maturity 最后产物停在 2026-07-13 17:36。不得手工补造 opening、恢复 cron 或以旧产物冒充当前学习证据。
5. 下一门禁是经单独授权的 SharedSignals opening/5min P0 根因修复、source/interface/gate 复验，以及同日真实时序产物的只读验收；在全部闭合前维持 sim-only / production NO-GO。

## 历史阻塞快照（2026-07-13；不代表当前 production 结论）

1. A股普通日内首样本门禁已完成：14:46 周期新增 2,000 条 observation/prediction，1,996 条 eligible/PIT-complete；早先 17 条 risk reject 保留，0 fill。明确 `missed_opening=true`，不得补造 09:30 证据。
2. CNFutures `day_afternoon` 已通过真实有效会话验收，11:35 replay 有 636 windows/3 counterfactual rejects；`day_morning` 缺失与 opening 错过必须保留为缺口，不得补造。
3. A股 15:32 daily MTM、资金守恒与唯一日级 SampleJournal 已验证；17:40 sample ops 后验证 forward labels、KPI、maturity，未到时点只等待不伪造。
4. 只有上述运行证据、三仓文档、GitHub/production readback 与 rollback 证据齐全后才评估 worktree/本地分支清理；append-only ledger、样本、归档和运行证据不得删除。

## 环境层级（2026-07-14 22:22 CST 当前本地/GitHub；生产为 18:05–18:10 CST 只读快照）

| 层级 | 当前事实 |
|---|---|
| 本地工作树 | `main=5689c95383244b689ced7d6c19a3ba2fc5c08bc4`，tracked/index clean；仅既有 `.codegraphcontext` 两项 untracked 且由本地 CodeGraph 占用，本轮未触碰 |
| GitHub | fresh fetch + `ls-remote`：`origin/main` 与 live GitHub `main` 均为 `5689c95383244b689ced7d6c19a3ba2fc5c08bc4`，`behind/ahead=0/0` |
| 生产文件/runtime | 本轮禁止访问/部署，未验证、未改变。最后只读事实为 2026-07-14 18:05–18:10 CST：TA `6c12fbed` + 7 untracked，source/interface/opening 阻断，production NO-GO；不得由 GitHub 或 HTTP 200 推断同步 |
| 生产 cron | 本轮禁止访问/apply；截至上述时点，A股两条 `sample_ops` cron 禁用，未恢复 |
| 外部邮件/同花顺/broker | 本轮未实现、未发送、未连接 |
| 真实市场样本 | 截至上述时点双 50,000 CNY authority 均 `real=false`、0 positions / 0 fills；A股当前阶段 opening/5min 与 KPI/maturity 时序证据未闭合，不能声称稳定学习闭环 |

## 历史环境层级（2026-07-13；不代表当前 GitHub 或 production）

| 层级 | 当前事实 |
|---|---|
| 本地工作树 | 主工作树 `main` 干净、HEAD `6c12fbe`；本隔离候选同基线、37 个 tracked+untracked 改动/新增文件，未提交 |
| GitHub | 开工 fetch 后 `origin/main=6c12fbe`；本候选未 push，任务结束时未再次联网刷新 |
| 生产文件/runtime | 本隔离任务禁止访问，未验证、未改变；不能从本地候选或测试推断 |
| 生产 cron | 本隔离任务禁止访问/apply，未验证、未改变 |
| 外部邮件/同花顺/broker | 未实现、未发送、未连接 |
| 真实市场样本 | 2026-07-13 opening 已错过且不补造；14:46 普通日内新增 2,000 predictions，1,996 eligible/PIT-complete、4 rejected、0 fill/live；15:32 唯一 MTM/50,000 CNY 守恒已验证；17:40 KPI/maturity 待实际产物 |
