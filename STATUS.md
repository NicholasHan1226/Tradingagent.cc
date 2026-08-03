# TradingAgent 当前状态

> 综合核验：2026-08-02 CST；A股/TradingCopilot 发布与运行态增量读回：2026-08-03
> 19:18 CST。本文只保留当前运行事实、证据边界和下一停止线；已合入
> 候选、历史事故与旧读回通过 Git 历史及仓外 release-evidence 追溯，不在此重复。

## 当前版本与运行面

| 层级 | 当前事实 | 证据边界 |
| --- | --- | --- |
| 本地主线 | 使用 `git rev-parse HEAD origin/main` 读取并比较 | 本地 `main` 必须与远端主线同 head；状态页不固定会被自身提交淘汰的 SHA。 |
| GitHub 主线 | 使用 `git rev-parse HEAD origin/main` 读取并比较 | 合入记录、CI 与精确 commit 以 GitHub 和 Git 历史为准；不是生产切换。 |
| 三条市场 lane | A股、Crypto、CNFutures 均与 `origin/main` 同 head，`ahead=0`、`behind=0` | 三个长期 worktree 均干净；它们不是独立生产 release。 |
| TA production current | `64f7b73ba8df580dad064046ffaab7c4b204960b` | 2026-08-03 已由 immutable release 原子切换；running front process、`current` symlink 与 effective release 三层读回一致。 |
| TradingDatas current | `2cd289db369ffebdb7b475ce71d45c9d5993eb48` | 18082 仅内部监听，generic collector timer active。 |
| TradingDatas Crypto current | `557a2967bc9582ffef26bc412d702767e0ef5c17` | 18083 独立内部监听。 |

`tradingagent-front-api.service` 为 `active`，仅监听 `127.0.0.1:8787`；`/healthz`
返回 `ok`。`/api/trading-copilot/tracking-universe` 在真实投影尚未生成时返回
`404 unavailable`，不使用静态名单兜底。旧 SharedSignals service/timer 均为 masked，MarketGraph
runtime 保持暂停。当前没有
broker、Testnet、Live、模型网络、公开交易入口或真实交易权限；
`REAL_TRADING_ENABLED=false`。

## A股

- 30 股分钟 session timer 仍是 simulation-only，最近一次于 2026-08-03 09:18 CST
  fail-closed；该失败发生在新 release 前，旧日志未输出分类原因。2026-08-03 19:18 CST 已
  安装新 initializer、session/bootstrap unit 与 `trading-copilot` 0700 runtime root；它们均经
  byte-level readback 与 `systemd-analyze verify` 核验。未手动触发 session，下一次 timer 为
  2026-08-04 09:18 CST。只有下一次自动 session 产生真实 symbol/name 投影及 session receipt，
  才能表述 30 股分钟链已恢复或 Copilot 已有真实跟踪名单。
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

## 下一停止线

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

- `STATUS.md` 只写当前事实与下一停止线；历史候选、失败与临时数字写入对应 readback
  报告、仓外 evidence 或 Git 历史。
- `current-v1` 只指固定 catalog/query 的当前消费者；旧 reader 属于
  `active-compatibility` 或 `retirement-pending`，已阻断的旧运行入口保持
  `hard-blocked`，不得成为 fallback。
- 当前临时开发分支已清理；仅保留 `main` 与三个长期 market lane worktree。
- candidate、main、server release、runtime、真实 receipt/API readback 和真实交易
  权限必须分别陈述，任何一层都不能替代另一层。
