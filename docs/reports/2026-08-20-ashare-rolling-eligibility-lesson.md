# A 股滚动资格扩容复盘（2026-08-20）

## 结论

把固定的 3193 只股票集合同时满足“上市满 30 日”作为模拟交易启动条件，是一个
范围设计错误。上市、退市、停牌和风险状态由外部证券主数据动态决定；只要把它们
作为全局门禁，就会出现新股不断加入、全量窗口不断重置的潜在无限延期。

严格全量检查保留为覆盖率和审计声明的门禁，不再阻塞当前合格股票的模拟运行。
模拟运行改用逐股滚动 partition：当前合格股票进入 active，新股进入 pending，
退市或数据不合格股票逐股隔离，禁止静默替换身份。

## 本轮修正

- `minute_session_initializer` 新增 `allow_pending_recent_listings`，生成 active
  universe 与 `pending_listings` 两个明确集合；manifest 同时保存 source/effective
  universe hash。
- `minute_scale500_runtime` 新增 `--rolling-eligible`，保留原严格 Scale500 路径
  作为显式 full-universe 模式；滚动模式只校验当前 active 集合的精确回执。
- Scale500 session/paper 候选 unit 已传入 `--rolling-eligible`；late-start 仍不
  自动放宽，避免把缺失历史窗口伪装成连续模拟。
- 退市风险和风险警示仍可作为 observation 记录，但不会因为它们而取得候选或执行
  资格；实际退市/缺失身份必须在下一份 source snapshot 中移出 active 集合并保留
  排除记录。

## 经验规则

1. 动态外部状态应是逐对象 membership 状态，不应固化为全局数量门禁。
2. “运行资格”“覆盖率声明”“全量 recall”必须分开；一个集合不完整只能降低声明
   范围，不能抹掉已验证局部结果。
3. 不允许通过替换证券、旧缓存、上一窗口数据或 partial shadow 伪造 active 完整性。
4. 对已纳入的 active 集合，receipt、lineage、same-observation、时间窗和 consumer
   readback 仍是严格门禁；因此放宽的是范围，不是证据质量。
5. 每个窗口必须输出 source/active/pending/excluded 数量及具体 reason code，便于
   追踪上市、退市和数据状态变化。

## 2026-08-21 开盘实跑补充：恢复不能再次要求早盘首槽

开盘后连续实跑暴露了一个与全量门禁不同的时序缺陷：rolling gate 已在 10:58
成功恢复为 `pending_two_live_snapshots`，但 11:02 的 paper timer 仍要求当天
09:35 初始 bar，最终以 `minute_auto_initial_bar_missing` fail closed，并把已经
恢复的 3186 只股票再次切回 fallback。这会让“事故发生后重试”变成“恢复后仍永远
等不到首槽”。

修正后的规则是：rolling 模式只要当天 gate 仍 pending、没有 state bundle、目标是
当前已完成 bar，就可建立一个新的 partial session；它只消费当前 bar，不回填任何
跳过的 bar，gate 明确记录 `late_start=true`、`partial_session=true`，并永久保持
`full_session_complete=false`、`learning_eligible=false`。固定 3193 full-universe
路径的人工 late-start 和独立 500/500 canary 规则不变。

经验补充：开盘前的“timer active”只证明调度存在，不能证明晚恢复后的时序分支可用；
必须在自然 bar 到达后验证 gate、实际 paper receipt 和覆盖回执。rolling 的恢复门禁
应按“当前 bar 可验证 + 不补历史 + 不提升 authority”设计，而不是复用固定全量
late-start 门禁。

## 相似问题审计

- `Ashare/minute_research.py` 的 `eligibility_reason` 是逐股判断，保留；它不是全局
  门禁。
- `shared/universe/snapshots.py` 已按逐股 exclusions 记录 listing/risk 状态，保留。
- `Ashare/adapter.py` 已按逐股排除 delisted，保留。
- `sample_pipeline.py` 的 `full_eligible_universe_recall` 只作为“能否声称全量 recall”
  的声明门禁，保留；不能把它用于阻断局部 observation 或模拟。
- 仅 `minute_scale500_runtime` 的固定 3193 校验属于 full-universe claim-specific
  路径；本轮增加 rolling mode，避免它继续承担运行总门禁。

## 验证

- `PYTHONPATH=. pytest -q tests/test_ashare_minute_session_initializer.py`
- `PYTHONPATH=. pytest -q tests/test_ashare_minute_scale500_runtime.py tests/test_ashare_minute_session_initializer.py`
- 最新候选 head `818f0c9` 已基于 GitHub `main=8677866` 重放；market lane 校验通过，
  滚动资格/Scale500/systemd 定向组合及相邻分钟 runner/research 测试共 `139 passed`，
  GitHub 全仓 CI run `32385177781` 也已通过。
- PR `#328` 已合并为 `main=870fe38138e76add3ecdc5a9b27844853838c303`，并完成同 SHA
  的 production release 发布；两个 Scale500 service 的 effective unit 已安装
  `--rolling-eligible`，session/paper timer 均为 `enabled/active`，下一窗口分别为
  `2026-08-21 09:18:09` 与 `09:42:00` CST。
- 服务器 source snapshot 当前为 `source=3193`、`active=3191`、`pending=2`；本轮尚未
  产生新的模拟 receipt/成交，旧 `current-canary-receipt.json` mtime 为 2026-08-07，
  因此不能把 timer active 描述成已有成功交易轮次。TradingDatas 当前窗口和 TA
  consumer readback 仍需在下一个自然交易窗口独立验证。

## 预演发现的第二个问题

在 2026-08-20 23:45 CST 使用生产同款 systemd unit 做纸面初始化预演时，rolling
模式暴露了 `profile_consumer_profile_drift`：3193→3191 的动态分页配置复用了旧的
consumer profile digest。修复已加入 `_scaled_minute_profile`，保留 dataset contract
fingerprint，但让当前分页配置重新计算 consumer digest；本地 96 项聚焦测试、PR
`#329` CI 和主线 CI `32388920102` 均通过。

这条经验补充为：凡是从固定 profile 派生动态 partition，凡覆盖分页、数量或过滤条件
的字段都必须重新计算派生指纹，不能复制旧窗口的 consumer binding。

## 当前部署状态

修复已继续合并为 `main=3c48e2ddeae8af1b4cb776c7c82f3a5347792e49`，部署运行
`32395738622` 已成功，服务器 current release 已读回同一 SHA。ECS 因预付费到期恢复
后，两个 timer 已重新 `enabled/active`，下一窗口为 session `09:18:04`、paper
`09:42:07` CST。

进一步通过 Aliyun ECS 只读核查确认，服务器 `i-7xv38klbo04losfsa551` 的状态为
`Stopped`，`ExpiredTime=2026-08-20T16:00Z`，并带有 `OperationLocks=financial`。
安全组 `sg-7xv3c2phc12wrnc4xzhx` 明确放行 TCP/22，因此本次 SSH 失败的基础设施
根因是实例预付费到期停机，而不是 SSH key、部署 artifact 或安全组规则。续费和启动
属于有费用/外部状态变更，必须单独授权；在此之前不得把部署或模拟首轮报告为完成。

## 最终隔离预演

使用生产 release、生产 universe source 和模拟明早 `2026-08-21 09:18` 时间点，
在临时 state root 中完成了同款 rolling initializer 预演：active partition `3191`，
日线参考可用 `3186`，逐股 `daily_data_excluded=5`，pending 新股 `2`，profile
`page_limit=100/max_pages=32`，分钟消费按 100 只分片，gate 为
`pending_two_live_snapshots`，状态为 `pass`。
这证明缺失日线和分页上限不会再触发全局 fallback；明早仍需以真实 fresh receipt、
paper 结果和 consumer readback 做最终运行验收。

## 2026-08-21 运行恢复修正

继续审计发现，若分钟查询只返回 active 集合的合法子集，旧 runner 会直接抛出
`minute_paper_snapshot_universe_incomplete`，随后 systemd `OnFailure` 同时关闭 session
和 paper timer。这会把单只股票或单个窗口的数据问题放大成整条线停摆。

本轮修正为：

- rolling paper runner 允许 `coverage_status=partial`，只把同一窗口实际通过且身份仍在
  effective universe 内的股票交给闭环；缺失股票写入 `missing_symbols`，下一窗口重试；
- rolling feature engine 遇到单只股票 bar gap 时只重置该股票 baseline，其它股票继续
  生成 feature/candidate；
- Scale500 服务移除自动 `OnFailure` 停止两个 timer；rollback service 保留为人工明确
  回退工具；session timer 增加 09:24、09:30、09:36 开盘前重试；
- 初始化发现旧 effective hash/count 的 gate 时，将其保留为 `.stale*.json`，重新创建当前
  partition gate；runtime fallback gate 创建时绑定当前 expected count/mode，避免 3193
  旧门锁死 3186 rolling partition；
- 每个成功窗口新增 `coverage-receipts/<HHMMSS>.json`，绑定 source/active/accepted/
  pending/excluded 数量、hash 和股票集合，覆盖率低不再阻止已通过股票进入模拟；
- 运维脚本不再硬编码旧 TA/TD release，改为读取当前 immutable release 或要求显式传入。

这组变化只扩大模拟数据积累的局部连续性，不扩大资本、订单、训练晋级或真实交易权限。

## 2026-08-21 上游实跑补充：大集合必须按 V1 in-filter 上限分片

rolling partial-session 修复部署后的 11:22 自然 bar 验证又捕获了真实的 HTTP 413：
3186 只股票被一次性放入 `ts_code in`，超过 TradingDatas V1 的
`max_in_values=100`。TradingDatas 服务本身健康，直接复现为
`query_request / HTTPStatusError / 413`，因此不能把它归类成行情无数据或继续盲目重试。

修正为分钟 profile 使用 `page_limit=100`、`max_pages=ceil(active_count/100)`，
每个 shard 独立完成 catalog/query、分页、identity 和 replay 双读，再合并为一个
当前 bar snapshot；任意 shard 失败只阻断该 bar/该 shard，已通过的股票不会被静默
替换。这样扩大股票覆盖时，规模增长转化为可审计的分片数量，而不是扩大单次请求体。

同一轮开盘恢复还暴露了另一个相邻边界：TradingDatas 的 catalog 版本是证据型元数据，
可能在交易日内变化；它会派生新的 consumer digest，但不等于 Universe、字段合同或
数据集指纹变化。只要当天仍未生成 state bundle，初始化现在允许刷新 catalog 证据、
consumer digest 和分页摘要，并继续复用未启动日目录；真正的 Universe、数据集合同或
已启动 state 发生变化仍保持冲突并 fail closed。
