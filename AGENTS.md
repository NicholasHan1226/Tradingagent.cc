# Tradings/


## 三系统定位

- Tradings 是交易判断和任务队列系统：读取 SharedSignals 的市场/事件数据与 MarketGraph 的研究证据，生成影子盘、模拟盘、实盘复核信号、日报和周报。
- 三系统当前不是 MCP 互调，也不是强耦合单体；SharedSignals 供数，MarketGraph 供研究图谱，Tradings 做交易判断、队列、回执汇总和通知。
- A 股模拟盘执行闭环必须走 `job_ashare_sim_exec -> signals/pending -> Mac Mini Hermes -> signals/filled/failed/positions`；Hermes/mini 只执行和回写，不做买卖判断。
- 未来系统间调用优先通过公开服务接口、只读 API/MCP read model 或明确数据契约完成；Tradings 不应直接依赖 MarketGraph 的内部实现细节，也不应把 MarketGraph 当执行入口。
## 目标
 交易模拟盘/影子盘, 高频训练策略, 每日2次复盘。

## 现有代码
- /opt/investment//tools/ (约20个工具)

## Projects 工作区同步补充

本仓库位于 `/Users/nicholashan/Projects/Finance/TradingAgent` 时，按 Projects 工作区统一同步规则执行：

- 仓库地址、remote 名称和默认分支以本仓库内 `git remote -v`、`git branch --show-current` 和项目文档为准，不从其它项目继承。
- 开发前检查 `git status -sb`、`git remote -v`、当前分支和是否落后远端；工作树不干净时先判断改动来源，不得覆盖并发 agent、cron、桌面自动化或 Nicholas 的改动。
- 涉及交易 agent 行为、邮件/API、部署、配置、数据契约、风控边界、服务器路径、定时任务或协作流程的变更，必须同步更新核心文档，例如 `README.md`、`docs/data_contract.md`、`docs/email_setup.md`、`docs/INFRASTRUCTURE.md`、`docs/repo_structure.md` 或对应市场/模块文档。
- 涉及真实资金、实盘执行、账号凭据、2FA、私钥、邮件发送通道或生产服务器的操作，必须先说明授权边界、回退方式和验证方式；研究、模拟盘和影子盘不得被汇报成实盘结果。
- 提交时只暂存本次审计过的文件；数据库、缓存、日志、staging、密钥、本机运行产物和交易临时输出默认不提交，除非项目文档明确要求并已审计。
- 从旧 `Desktop/Investment` 或其它 iCloud 管理目录迁移时，优先使用当前 Projects 下真实 clone；旧目录只作为对照和补漏来源，不直接搬运 `.git`。

## 2026-07-01 Runtime Guardrails

- A股实盘复核信号不是自动实盘执行。当前盘中复核/观察摘要由 MarketGraph `deploy/job_trading_signals.sh` 在 `marketgraph` 用户 crontab 中每 30 分钟刷新 `outputs/trading_signals.json`，输出固定 `capital_layer=shadow`、`real_money_allowed=N`；真正的模拟/执行队列仍归 Tradings 与 mini/Hermes。
- PM shadow wrappers must rely on `shared/wrappers/_common.sh::run_job` for job locking. Do not take the same `${TRADINGS_STATE_ROOT}/${JOB_NAME}.lock` before calling `run_job`; that self-deadlocks the wrapper and causes repeated `skipped=already_running`.
- A-share simulated execution must not enqueue a new batch while the Mac mini Hermes receiver reports unfinished work. `job_ashare_sim_exec.sh` checks mini health and skips when `pending + in_progress > ASHARE_SIM_MINI_BUSY_LIMIT` (default 0).
- A-share simulated orders are same-day idempotent by `market + account + trade_date + symbol + side`. `shared/orchestrator.py` must check existing `pending|claimed|running|filled|failed|partial|expired|cancelled` signal cards before calling the mini webhook; duplicate same-day symbols should record `status=duplicate` and must not enqueue a new mini order.
- On 2026-07-01, stale server-only A-share simulated pending cards from the pre-webhook bridge were moved to `signals/expired`; cleanup manifest: `/opt/investment/agent_backups/ashare_sim_pending_cleanup_20260701113617.jsonl`. Do not move these back to pending unless intentionally replaying that historical batch.
- A股候选池是动态 5 层结构（holdings/watch/candidate/universe/fundamental），但所有 A股入口必须只保留普通 A股代码段；`200xxx.SZ`、`900xxx.SH`、北交所等非本模拟执行链路标的必须在 `Ashare.adapter`、`shared/screening/universe_filter.py`、`shared/screening/candidate_pool.py` 三层被过滤，不能进入 `signals/pending`。
- A股候选池当前是“每次调度动态重建”的 5 层池，不是已完成持久化升降级状态机。`promote()` 只是本地工具函数；在持久化层级状态、demote/退出规则、层内停留时间和复盘驱动迁移落地前，不得对外声称每层已有完整晋升/降级/退出闭环。
- A股可执行候选必须有近期日线 close > 0；无日线覆盖的股票必须在 universe/candidate 阶段排除。Tushare daily `amount` 在 read model 中按千元口径存储，流动性比较前必须换算为元，避免把正常流动性股票误判为 illiquid。
- A股 5 分钟级模拟执行调度默认使用 `TRADINGS_DEBATE_MODE=fast`，用六维分数生成确定性 belief_score，不能同步阻塞等待 DeepSeek。DeepSeek/LLM 只用于研究层、多空复核、日报/周报和慢速校准；Hermes/mini 不做交易判断，只做 GUI 执行、截图、回执和账户同步。
- mini health gate 会被 `pending + in_progress` 队列阻塞；迁移测试、拒绝测试和生产模拟交易任务应隔离，避免测试 pending 导致 `job_ashare_sim_exec` 长时间 skipped=mini_busy。
- 2026-07-01 13:39-15:29 CST 已修复 mini/Hermes 同花顺模式识别：优先用 AX 标签确认 `模拟`，不再让 Vision 把资金/账户区域误判成 `实盘=是`；`A股` 标签本身不是实盘标识，显式真实风险标识是 `实盘`、`资金账号`、`普通交易`、`融资融券`。
- 2026-07-01 后验复核推翻了 `000002.SZ`、`000006.SZ` 的早期“成交确认”：裁剪后的同花顺持仓表只显示旧持仓 `600029`，不包含 `000002`/`000006`/`000007`。这些 false-positive filled/position 记录已从 server `signals/filled`、`positions`、legacy receipt/ledger 中撤回并改为 posthoc unconfirmed failed；备份目录 `/opt/investment/agent_backups/ashare_sim_false_confirm_reconcile_20260701T152920`。
- mini/Hermes 点击提交但没有严格持仓表/委托/成交确认时不得继续消费队列。此场景必须写 unconfirmed failed receipt，创建 mini 本地 `signals/executor_halt.json`，等待账户/持仓同步或人工复核后再清理 halt 恢复。当前 `000007.SZ` 属于此状态，剩余 pending 不得自动重试到重复点击。未来截图确认只能看裁剪后的持仓表区域，不能看整窗、买入输入框或右侧自选列表。
- 2026-07-01 15:35 CST 起，Mac mini Hermes 当前不应走同局域网地址；远程维护和健康检查使用 Tailscale mini `100.125.4.113` / SSH alias `macmini-tailscale`。`192.168.5.2` 是 RSS 服务器线索，不是 MarketGraph 主服务器，也不是 Hermes mini 执行桥。
- 2026-07-01 15:40 CST 已更新 `job_ashare_sim_exec.sh`：mini `/health` 返回 `halted=true` 时服务器日志必须写 `skipped=mini_halted`，不能再把它当普通 `mini_busy`。当前主服务器经 `127.0.0.1:9865/health` 可见 `000007.SZ` 点击未确认暂停。
- 2026-07-01 15:43 CST，mini 上 16 条 13:32 批次 A股模拟 pending 已全部过期且未执行，已在保留 `executor_halt.json` 的前提下归档为 failed_final 并同步失败回执到主服务器；mini 备份目录 `~/.hermes/ashare-runtime/signals/backup_expired_pending_20260701_154334`。
- 2026-07-01 16:03-16:07 CST，mini 只读模拟账户同步确认当前同花顺为 `模拟练习`，持仓表只有 `600029.SH 南方航空`，`000007.SZ`/`000002.SZ`/`000006.SZ` 均不在持仓；server `signals/positions/simulated_ashare_positions.json` 已按 mini read-only snapshot 对齐为 `600029.SH` 100 股。`000007.SZ` halt 已归档到 mini `signals/halt_archive/executor_halt_20260701_160658_000007.SZ.json`，mini health 当前 `execution_status=ready`。
- 2026-07-01 16:00 CST 起，`job_ashare_sim_exec.sh` 增加 A股交易时段保护；非工作日或非 `09:30-11:30` / `13:00-14:57` 直接 `skipped=market_closed`，防止收盘后清理 halt 时重新下发模拟单。

## 2026-07-01 修复记录：通知、影子队列、A股影子账本
- 邮件发送：`shared/notify/email_sender.py` 使用 Cloudflare Email Service REST endpoint `/client/v4/accounts/{account_id}/email/sending/send`；交易通道 `notice@tradingagent.cc -> tradingadviser@coze.email`，系统通道 `notice@tradingagent.cc -> soc@coze.email`。
- 影子信号：影子盘研究信号不再写入可执行队列 `signals/pending`，统一写入 `signals/shadow/pending`；执行队列只保留真实/模拟待执行信号。
- A股影子账本：`shared/execution/shadow_broker.py` 会拒绝 A股链路中的非普通 A股代码，例如 `200xxx.SZ`；历史污染流水已隔离到 `shared/logs/maintenance/` 下的 cleanup 目录。
- 影子收益口径：`shadow_pnl.json` 同时保留 `realized_pnl`、`unrealized_pnl`、`market_value`、`total_pnl`；历史记录曾使用 `valuation_source=latest_shadow_trade_price`；当前 A股影子盘估值已升级为优先 SharedSignals 日线收盘价，缺失时才回退最近影子成交价。

## 2026-07-01 A股健康检查入口
- A股市场健康检查入口：`PYTHONPATH=/opt/investment/Tradings python3 shared/runtime_test/market_health.py --market ashare --pretty`。
- 输出文件可保存到 `shared/runtime_test/ashare_health_latest.json`；默认只读，不发邮件、不点击同花顺、不改变交易状态。
- 当前检查覆盖：A股 universe 合规性、影子账本污染和收益口径、执行/影子队列隔离、mini/Hermes 健康、模拟持仓快照、邮件模板/发送记录、失败回执可复盘性。
- 通过标准：`overall_status=pass` 且 `signals/pending|claimed|running` 为 0、`signals/shadow/*` 可有影子研究记录、`200xxx.SZ/900xxx.SH` 不出现在 A股影子账本。

## 2026-07-01 A股健康告警与双模拟盘补充
- A股健康检查已接入包装器 `shared/wrappers/job_ashare_health_check.sh`，调用 `shared/runtime_test/ashare_health_alert.py`；健康通过只写 `shared/runtime_test/ashare_health_latest.json` 和 history，不发邮件；warn/fail 才走 `system_health` 模板发系统通道 `soc@coze.email`。
- A股 simulated 现在分成两套账：同花顺模拟盘仍由 mini/Hermes GUI 执行和回写；服务器本地模拟盘由 `shared/execution/local_sim_ledger.py` 记录 paper fill 备份，按 `idempotency_key` 去重，不能替代同花顺成交确认。
- A股影子盘估值优先读取 SharedSignals `market_bars_daily.close`；缺失时才回退最近影子成交价，并在 `valuation_source` 标明来源。

## 2026-07-01 A股 10 分钟级调度调整
- A股 simulated 同花顺执行保持 5 分钟级 job_ashare_sim_exec，因为它已快于 10 分钟且受 mini/Hermes busy/halt 保护。
- A股影子盘 job_ashare_shadow 从每 30 分钟调整为交易时段每 10 分钟；run_job 锁会防止上一轮未结束时重叠执行。
- A股观察/实盘复核摘要 job_trading_signals.sh 从每 30 分钟调整为交易时段每 10 分钟。
- A股健康检查 job_ashare_health_check.sh 曾短暂调整为交易时段每 10 分钟；该节奏已被下方 2 小时健康告警节奏取代，健康通过不发邮件，warn/fail 才发系统通道。

## 2026-07-01 A-share cadence update

- A股健康检查用于系统异常邮件，不是交易信号循环；`job_ashare_health_check.sh` 在 `marketgraph` crontab 中调整为交易时段约 2 小时一次（09:10/11:10/13:10/15:10）并保留 08:10 盘前、16:10 盘后检查，只在 warn/fail 时发系统邮件到 `soc@coze.email`。
- A股复盘迭代保持每天两次主复盘：15:30 盘后复盘和 22:00 夜间复盘；07:30 是晨报，不计为复盘迭代次数。
- A股观察信号摘要仍每 10 分钟运行；若没有 5% 突破/跌破，会输出最多 10 条价格异动观察 `top_mover_observation`，仅用于复盘和候选池校准，不触发模拟或实盘执行。
- 尾盘集合竞价前增加 14:57 的观察信号刷新；该刷新只写观察摘要和邮件，不扩大 Hermes/同花顺执行窗口。
- A股观察信号摘要必须优先读取最新非空 `intraday_snapshot`；盘后或异常采集产生的空快照只能作为质量线索记录，不能覆盖当天已采到的市场状态。

## 2026-07-01 A股复盘与尾盘扫描修正

- A股主交易复盘改为两次有增量交易数据的复盘：11:45 午盘复盘、15:30 收盘复盘；22:00 不再发送重复交易日报，改为 `job_ashare_night_calibration` 夜间校准，汇总研究、归因、回测、尾盘候选和次日计划输入。
- 尾盘新增 `job_ashare_closing_scan.sh`，在 14:40/14:50/14:56 生成 `MarketGraph/outputs/ashare_closing_buy_candidates.json`；该文件仅是影子/模拟候选观察，不入实盘、不直接下单。
- SharedSignals 是独立供数层：定时采集/维护先沉淀数据，Tradings 通过 reader/read model 按需读取；Tradings 不应在每次交易判断时重新现场采集 Tushare。

## 2026-07-01 shadow signal state fix

- `signals/shadow` 必须和执行队列一样具备 `pending/claimed/running/filled/expired/cancelled/failed/partial` 状态目录，并由 `marketgraph:marketgraph` 写入；否则 Crypto/PM/US/A股影子盘会出现 `PermissionError: signals/shadow/claimed`，导致 shadow run `state=degraded`。
- `job_self_heal` 的 `job_signal_sweep_expired` 同时清理执行队列和影子队列的过期 pending 卡；影子卡只在 `signals/shadow/*` 内流转，不进入 Hermes/实盘执行队列。

## 2026-07-01 PM/A股运行闭环修复

- PM shadow scan 已从每 5 分钟调整为每 10 分钟；原因是单次 PM shadow 约 4-5 分钟，5 分钟频率没有足够缓冲。`job_pm_scan.sh` 继续通过 `_common.sh::run_job` 使用 `job_pm_shadow.lock`，上一轮未结束时必须跳过，不得并发。
- `job_self_heal` 的 cron 日志判断只扫描每个日志最近 80 行，且后续 `success`/`state=ok` 会清除旧失败；缺少 `signal_count` 字段的历史日报不再计入 `signal_starvation`。2026-07-01 21:31 CST 验证 `issues_found=0`。
- A股 shadow/sim 候选在组合构建前必须过滤 `price <= 0`，记录到 `skipped_candidates`，不能让零价候选污染 `execution.shadow_broker` 或 `execution.sim_broker` 健康状态。
- A股健康检查 `ashare_health_alert.py --send-on never --pretty` 于 2026-07-01 21:31 CST 验证 `overall_status=pass`；健康通过不发邮件，warn/fail 才发系统通道。
- 邮件模板渲染验证覆盖 `trading_signal/daily_report/weekly_report/system_health`；交易邮件通道为 `notice@tradingagent.cc -> tradingadviser@coze.email`，系统邮件通道为 `notice@tradingagent.cc -> soc@coze.email`。Cloudflare 最新发送记录为 `status=sent`、`status_code=200`。

## 2026-07-01 Tradings 运维报告与缺陷复盘
- 新增统一运维报告入口：`PYTHONPATH=/opt/investment/Tradings python3 shared/runtime_test/ops_report.py --send-on never --pretty`；定时任务入口为 `shared/wrappers/job_ops_report.sh` / `tradings_cron_entry.py --job job_ops_report`。
- `job_ops_report` 每小时 17 分运行，写出 `shared/review/ops/tradings_ops_latest.json` 和 `shared/review/ops/tradings_ops_history.jsonl`；`overall_status=fail` 才向系统通道 `notice@tradingagent.cc -> soc@coze.email` 发邮件，历史失败导致的 `warn` 只入报告，不重复打扰。
- 运维报告同时覆盖执行队列 `signals/{pending,claimed,running,filled,failed,expired,cancelled}` 与影子队列 `signals/shadow/{pending,claimed,running,filled,failed,expired,cancelled}`，并输出按市场分布、失败原因聚合、Mini/Hermes 回执完整性、服务器本地模拟账本和影子盘 PnL 摘要。
- Webhook 发送结果新增 `payload_sha256`，用于后续和 Mini/Hermes 回执指纹对账；旧回执没有签名时只能标记为 `unsigned`，不能当作篡改或失败。
- 日报/周报模板新增可选“运行状态”段，展示执行队列、影子队列、回执校验和失败分类；模板缺少运维字段时保持兼容。
- 当前已知缺陷边界：服务器侧已经能识别 unsigned/invalid receipt，但 Mini/Hermes 回执本身尚未写入 `payload_sha256`/`receipt_sha256`，所以完整端到端指纹闭环需要在 mini 执行器中继续补齐。
