# execution/

> **阅读顺序：** [../../AGENTS.md](../../AGENTS.md) → [../../STATUS.md](../../STATUS.md) → 本文件

## 目标
订单执行层: Hermes(A股实盘) + 影子盘(多策略并行) + 模拟盘(滑点建模)。

## 自动化级别
5-10分钟级自动化: 信号触发→路由→模拟/影子记录→(实盘待人工确认)。

## 文件
- hermes_bridge.py — A股信号卡桥, 只写入/读取 signals JSON 文件, 不 SSH 到 Mac Mini, 不直接执行
- shadow_broker.py — 影子盘, 仅记录不执行, 多策略并行
- sim_broker.py — 模拟执行, 含滑点建模
- execution_router.py — 按策略阶段路由 (sim→shadow→real)
- slippage_model.py — 滑点模型 (市价/限价)

## 边界
- 真实资金只给Nicholas手工确认, 不自动下单/撤单/点击
- 影子盘和模拟盘可全自动记录
- Hermes/Mac Mini 是执行桥：只负责同花顺 GUI 执行、截图/视觉定位、有限重试、回执和账户同步；不做买卖判断，不拒绝服务器已生成的 simulated 信号。
- A 股模拟盘权威闭环是服务器 TradingAgent `signals/pending` → mini 接收/执行 → 服务器 TradingAgent `signals/filled|failed|positions`；兼容旧 MarketGraph receipt/ledger 输出只作历史留痕，不作为新的权威状态。
- 成功点击后即使本地回执保存或远端同步异常，也必须进入待同步/人工排查，不能把同一信号重新放回待执行造成重复点击。


## Mac Mini Hermes 运行前提

- mini 侧必须同时运行 `com.nicholashan.sim-signal-receiver` 和 `com.nicholashan.sim-signal-executor`；服务器通过反向 SSH 隧道 `127.0.0.1:9865` 投递信号，receiver 落地到本地 `Ashare/signals/pending`，executor 再执行 GUI。
- A 股模拟盘当前按 Nicholas 的业务定义视作模拟盘；LaunchAgent 必须保留 `SIM_ACCEPT_ASHARE_PANEL_AS_SIM=1`，同时保留 DashScope/视觉定位环境，因为按钮定位仍依赖截图和视觉判断。
- 接收器/隧道健康检查优先看服务器 `curl http://127.0.0.1:9865/health` 和 mini 本地 `curl http://127.0.0.1:8654/health`；只看到 ssh 隧道不代表 receiver 正常。

## 2026-07-01 Mini Queue Contract

- The Mac mini receiver `/health` response must include both `pending` and `in_progress`. Server-side A-share simulated scheduling treats `pending + in_progress` as active Hermes workload and must not enqueue another batch while that value is above the configured limit.
- This guard is backpressure only. It must not cancel or rewrite orders already accepted by Hermes; the executor remains responsible for finite retry, GUI execution, receipts, and account/position writeback.
- A-share simulated execution cards must carry a stable idempotency key. Mini receipts should preserve `idempotency_key` when present, but server de-duplication must also handle older filled/failed cards that only have `market + ts_code + date + side`.
- The mini executor must recover stale local `signals/in_progress/*.json` that were claimed but not executed, returning them to `pending` after `SIM_STALE_IN_PROGRESS_SECONDS` (default 600s) unless max attempts are exhausted. This prevents a restart-time orphan from permanently blocking server-side busy checks.

## 2026-07-01 Mini Mode And Confirmation Update

- The mini executor must prefer Tonghuashun accessibility labels over Vision for account-mode detection. The live Tonghuashun window exposes an AX label `模拟`; Vision may still misclassify the same screen as `实盘=是` because it sees account/funds panels.
- `A股` is not a real-money marker. Explicit real-risk markers are `实盘`, `资金账号`, `普通交易`, and `融资融券`.
- A clicked UI order is not a fill. Mini may write `filled` only when the receipt has `confirmation_status=confirmed`, `filled_qty>0`, and `execution_confirmed_by` in `tonghuashun_deal_query|tonghuashun_order_query|tonghuashun_position_delta|tonghuashun_position_table_crop`.
- If the executor clicks submit but cannot confirm the new position/order/deal, it must write an unconfirmed failed receipt, create `signals/executor_halt.json`, and stop consuming the queue until account/position reconciliation clears the halt file. Do not auto-retry that signal, because the click may have reached Tonghuashun.
- Posthoc correction on 2026-07-01 15:29 CST: earlier `000002.SZ` and `000006.SZ` confirmations were false positives caused by full-window Vision verification. Cropped holdings-table screenshots showed only old holding `600029`, not `000002`/`000006`/`000007`. Server records were corrected to posthoc unconfirmed failed and positions were cleared; backup directory `/opt/investment/agent_backups/ashare_sim_false_confirm_reconcile_20260701T152920`.
- Future screenshot confirmation must use only the cropped holdings table and must parse explicit six-digit codes from that table. Do not accept generic Vision answers such as `有`, and do not inspect the full window because the order form and right watchlist can contain unrelated codes.
- When Nicholas is away from the local LAN, operate the Mac mini over Tailscale (`macmini-tailscale`, `100.125.4.113`). Do not use `192.168.5.2` for Hermes; that address is unrelated to the MarketGraph/Hermes execution bridge.
- Receiver `/health` must expose `halted`, `execution_status`, `halt_signal_id`, `halt_reason`, and `expired_pending`. Server scheduling must treat `halted=true` as `mini_halted`, even when `pending + in_progress == 0`, because clearing queue pressure is not the same as clearing an unresolved clicked order.
- 2026-07-01 15:43 CST: after `000007.SZ` triggered `executor_halt.json`, the remaining 16 pending cards were already expired. They were archived as `failed_final` with `cleanup_reason=expired_pending_while_executor_halted`; the halt file remains in place until account/order reconciliation verifies the clicked order state.
- `real-account-sync.sh` now defaults to `~/.hermes/ashare-runtime` and calls `tools/a_share_tonghuashun_readonly_sync.py`. The sync is read-only: screenshots/accessibility only, no credential read, no buy/sell/cancel/confirm click. If the visible Tonghuashun account box shows `模拟`/`模拟练习`, real-account sync must write `not_real_account_visible` and must not write a real snapshot.
- Mac mini LaunchAgent `com.nicholashan.real-account-sync` is loaded for weekday 09:20 and 15:20 read-only real-account status sync. A successful status write returns exit 0 even when `snapshot_usable=false`; consumers must read the JSON status instead of inferring from process exit alone.
- Simulated account read-only reconciliation on 2026-07-01 16:03 CST found only `600029.SH 南方航空` in the cropped holdings table. This evidence cleared the `000007.SZ` executor halt after market close, with pending/in_progress both zero and server market-hours guard active.

## 2026-07-01 执行/影子边界补充
- `signals/pending` 是执行队列，不应放影子盘研究信号；影子盘信号放在 `signals/shadow/pending`，避免被 Hermes 或模拟执行巡检误判为堵塞。
- A股模拟执行仍通过 mini 反向隧道 `127.0.0.1:9865 -> mini:8654` 检查健康状态；mini health `execution_status=ready` 才允许服务器生成模拟执行任务。
- A股影子/模拟链路禁止 `200xxx.SZ` 等非普通 A股代码进入 shadow broker；被拒绝记录应保留在维护 manifest 或 failed/backup 中，不能污染当前 PnL。

## 2026-07-01 A股健康检查入口
- A股市场健康检查入口：`PYTHONPATH=/opt/investment/tradingagent python3 shared/runtime_test/market_health.py --market ashare --pretty`。
- 输出文件可保存到 `shared/runtime_test/ashare_health_latest.json`；默认只读，不发邮件、不点击同花顺、不改变交易状态。
- 当前检查覆盖：A股 universe 合规性、影子账本污染和收益口径、执行/影子队列隔离、mini/Hermes 健康、模拟持仓快照、邮件模板/发送记录、失败回执可复盘性。
- 通过标准：`overall_status=pass` 且 `signals/pending|claimed|running` 为 0、`signals/shadow/*` 可有影子研究记录、`200xxx.SZ/900xxx.SH` 不出现在 A股影子账本。

## 2026-07-01 双模拟盘边界
- `Ashare/sim_executor.py` 继续负责把 A股 simulated 信号送到 Mac mini Hermes，同花顺模拟盘成交必须靠 mini 回执确认。
- `shared/execution/local_sim_ledger.py` 是服务器本地模拟盘备份账本：只记录 server paper fill，用于训练/复盘数据保全；不得把它当同花顺 GUI 成交。
- `shared/execution/sim_broker.py` 对 A股 simulated 订单默认同步写本地备份账本；可用 `TRADINGS_LOCAL_SIM_BACKUP_ENABLED=0` 临时关闭。
