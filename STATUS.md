# TradingAgent 历史状态快照

## 2026-08-16 Crypto 观测链读回

> `observed_at=2026-08-16T00:22:14+08:00`。十币种观测 unit 的 effective
> `WorkingDirectory` 固定在不可变 release
> `25c34f95e4da5f6fafa9249f7808b8a079dfbc17`，不是全局 `current`
> `3e6933d1d0faf2e638bfebf6b6a9160978802d49`。00:18:26–00:19:20 的自然轮
> `Result=success`、`ExecMainStatus=0`，完成 `16:15Z` 槽且
> `spread_status=completed`；同一读回的 store 为 126 events、109
> observations、11 data rejects、6 gaps、`pending=false`，最新 terminal 槽为
> `16:15Z`。timer 为 enabled/active/waiting，下一轮 00:23:26。

> `readback_at=2026-08-16T00:23:30+08:00`。通过 18083 和专用 Crypto read-token
> 对 `crypto.spot.binance.btcusdt.book_ticker` 完成一次认证 catalog+query：1 行、
> `ready/fresh/valid`、非 degraded、非 stale、receipt 与完整 lineage 均存在，
> `observed_at=2026-08-16T00:20:23.374283+08:00`、无 next cursor。该 current
> snapshot 只证明本次只读消费证据，不证明历史 PIT、因子 edge、晋级、风险、资本、
> 执行或 live authority。

## 2026-08-15 当前交付摘要

> `observed_at=2026-08-15T01:17:37+08:00`。本地 source、GitHub `main` 与普通服务器
> 源码均为 `1bc9a0a3275675d68270b952ba70828d2c24083a`；现役 Crypto
> learning/scrub runtime 仍分别钉住不可变 release
> `9a4a174c5631d30afc64d6a1e96ec3832ef43055`，本次 source parity 没有切换 runtime。
> 00:43 的自然 incremental cadence
> 约 32 秒、`exit=0`，返回 `status=projected`、`completion_count=2963`、
> `projected_completion_count=2963`；factor 投影返回 `full_scrub_required`，对应
> evaluation debt 保留到既有 03:35 daily scrub 处理。源码同步、runtime pin、自然轮
> 结果和后续 scrub 是独立证据层，不能互相替代。

> Factor/Strategy MVP 已进入 rolling evaluation：单样本/单窗口只证明 receipt/PIT、
> 成本、基线和确定性 artifact 管线跑通，不证明 edge。参数、晋级、风险或资本变化仍需
> 新增独立结果、滚动覆盖、time-split/OOS 与多重比较意识下的复核。上述 incremental、
> debt 与 scrub 均固定为 offline/private shadow，不具备 promotion、risk expansion、
> execution 或 live authority。

> 本文件冻结于 2026-08-02/03，以下 commit、timer、receipt 和“下一停止线”仅是
> 当时的历史快照，不再代表当前运行事实。当前跨线状态以
> `../autodev-control/AUTODEV_STATE.json` 为机器入口，并且每个 release、service/timer、
> receipt/API、consumer/evaluation 结论都必须由本轮新鲜读回确认；状态文件本身也不能
> 替代运行证据。保留本文是为了审计旧边界，禁止据此派单、发布或宣称当前完成。

## 2026-08-02/03 版本与运行面（历史）

| 层级 | 当前事实 | 证据边界 |
| --- | --- | --- |
| 本地主线 | 使用 `git rev-parse HEAD origin/main` 读取并比较；当前检出的是独立在途分支 | 该工作树不能为了同步状态页切换、合并或覆盖；比较结果不等同于“本地 main 已同步”。 |
| GitHub 主线 | 使用 `git rev-parse HEAD origin/main` 读取并比较 | 合入记录、CI 与精确 commit 以 GitHub 和 Git 历史为准；不是生产切换。 |
| A股 market lane | 与 `origin/main` 同 head（`5631302`），`ahead=0`、`behind=0` | 本轮已在专用 worktree 通过 lane 校验；它不是独立生产 release。 |
| CNFutures / Crypto lane | 未在本轮当前 worktree 清单中核验 | 开始对应市场开发前必须重新建立/定位专用 worktree，并通过各自 lane 校验；不能由 A股结果代替。 |
| TA production current | `56313025af24b645efba0d87e0805d17b9e080ca` | 2026-08-03 已由 immutable release 原子切换；running front process、`current` symlink 与 effective release 三层读回一致。 |
| TradingDatas current | `83573f617341f75c978b944f203938bbc53cf1ae` | 2026-08-03 22:50 CST 由 root-only runtime readback 确认；18082 仅内部监听，generic collector timer active。这个版本事实不等同于 30 股的新鲜 receipt/API consumer readback。 |
| TradingDatas Crypto current | `557a2967bc9582ffef26bc412d702767e0ef5c17` | 18083 独立内部监听。 |

`tradingagent-front-api.service` 为 `active`，仅监听 `127.0.0.1:8787`；`/healthz`
返回 `ok`。`/api/trading-copilot/tracking-universe` 在真实投影尚未生成时返回
`404 unavailable`，不使用静态名单兜底。旧 SharedSignals service/timer 均为 masked，MarketGraph
runtime 保持暂停。当前没有
broker、Testnet、Live、模型网络、公开交易入口或真实交易权限；
`REAL_TRADING_ENABLED=false`。

## A股

- 30 股分钟 session timer 仍是 simulation-only，2026-08-03 22:50 CST 的 root-only
  readback 显示 `tradingagent-ashare-minute-session.timer` 为 `enabled/active/waiting`，下一次为
  2026-08-04 09:18:08 CST；最近一次 09:18 初始化退出码为 `2`，日志仅输出
  `minute session initializer failed closed`。不人工补触发或补写 session。只有下一次自动 session
  产生真实 symbol/name 投影及 session receipt，才可表述 30 股分钟链已恢复或 Copilot 已有真实跟踪名单。
- 500 股 scale500 session/paper timer 均为 `disabled/inactive`。它需要 TradingDatas
  正式连续两根 500/500、同一 bar time、完整 receipt/lineage、terminal pagination
  replay 的证据；周末或候选 loopback 不能替代。
- TradingCopilot event-evidence v2 已在独立候选中以 UID987 完成一次正式
  catalog/query 读回：`cn.dataset.research_report` 接纳 1 条事件；`anns_d`、
  `cctv_news`、`irm_qa_sh`、`irm_qa_sz` 均因
  `ashare_evidence_metadata_not_ready` 拒绝。它没有切换 `current`、启用新 unit、
  创建候选/订单/资本/训练事实或生成情绪标签。详见
  [event-evidence v2 readback](docs/reports/2026-08-02-trading-copilot-event-evidence-v2-readback.md)。
- `cn.dataset.major_news` 已在独立的 `318efe7` 旁路 release 中完成一次正式
  current-observation 读回：1 行、单页同观察重放一致、receipt/lineage 完整。
  它的 public contract 不支持 `as_of`，因此侧车只对该宏观 append-only profile
  省略该可选请求成员，并继续用 event time 与 envelope `observed_at` 验证当前可用性。
  同轮的 `anns_d`、`moneyflow`、`moneyflow_ths` 均因 2026-07-31 日频事实超过
  86400 秒 SLA 被拒绝，四源 sidecar 如实为 `blocked`；零名义金额、无 LLM、候选、
  资金、订单、timer 或 `current` 变更。详见
  [major-news shadow readback](docs/reports/2026-08-02-ashare-major-news-shadow-readback.md)。
- 当前为休市日，不把旧分钟行、HTTP 200 或 timer active 误报为 fresh market evidence。

## Crypto

- G5 round-trip delayed-paper、health、acceptance 三个 timer 均
  `enabled/active`，运行在各自 immutable release，不依赖 TA `current` 指针。
- 最近独立 acceptance 读回（2026-08-02 19:36 CST）显示核心 `healthy`、pending 为
  false、capital `balanced=true`，共 234 个 completed 5-minute observations、468 个
  经核验 decision events、11 个完成的 simulated round trips。
- 由于已有五个历史 completion gaps，最近连续段仅 84 根（420 分钟），尚未达到
  288 根/24 小时的离线学习门槛。核心继续自动积累；learning timer 不启用，
  Challenger 不能自动晋级或扩大风险。
- 当时累计费用为 `21.98507865 USDT`、已实现模拟损益为 `-9.68734405 USDT`。
  这些是费用后的模拟账本审计值，固定 `not_strategy_edge=true`，不能作为策略收益或
  实盘资格结论。

## CNFutures

CNFutures 仅 fixture/mock-first。TradingDatas 尚未提供可消费的 M 合约规格（非空
multiplier/tick/price limit）、日历/交易时段，以及两根相邻 5 分钟正式 receipt/API
readback；不启动 runtime、timer、delayed-paper、模拟成交或 broker。

## TradingDatas 消费边界

TradingAgent 只消费 `GET /v1/catalog` 与 `POST /v1/query`。数据集必须逐项满足
其 receipt、lineage、identity、pagination、freshness、quality 与 degraded 合同；不得
直读 SQLite、调用 provider、使用旧 8082/SharedSignals 或文件 fallback。

TradingDatas 已提供通用 `windowed_unique_primary_key` 与有界 fanout 完整性能力；
`major_news` 已有真实 receipt + formal API readback，并且只可作为当前、零名义的
宏观 shadow 观察。新闻、资金流与其它日频接口仍必须逐项满足自己的 fresh metadata
与 formal API readback，不能由这个单项结果代替。

## 当时的下一停止线（历史，不再调度）

1. **Crypto**：从最近连续 completion 段自然累积至 288 根；随后先运行 disabled
   full scrub 与同槽幂等 replay，再单独决定是否安装离线 learning worker。核心数据、
   资本和 timer 不依赖 learning 成功。
2. **A股**：下一个交易时段验证 30 股当前分钟链；500 股仅在 TD 的两轮正式 500/500
   canary 通过后由独立 scale500 root late-start。
3. **事件/新闻**：在下一个日频事实 fresh 窗口重跑四源 sidecar；只有 `anns_d`、
   `moneyflow` 与 `moneyflow_ths` 也逐项 fresh/valid/non-degraded 时，才可把
   `major_news` 的已验证 current-observation 一并报告为完整 shadow parity。它仍
   不产生候选、训练、订单或 LLM 网络调用。
4. **CNFutures**：等待 TD 的最小 M 日盘 5 分钟纵向切片。通过后先运行 read-only
   observation/hold/risk-reject，不直接进入 delayed-paper。

## 维护规则

- 新鲜当前事实写入 AutoDev 状态并由同轮运行读回验证；本历史快照只保留审计语境。
- `current-v1` 只指固定 catalog/query 的当前消费者；旧 reader 属于
  `active-compatibility` 或 `retirement-pending`，已阻断的旧运行入口保持
  `hard-blocked`，不得成为 fallback。
- 当前临时开发分支已清理；仅保留 `main` 与三个长期 market lane worktree。
- candidate、main、server release、runtime、真实 receipt/API readback 和真实交易
  权限必须分别陈述，任何一层都不能替代另一层。
