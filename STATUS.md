# TradingAgent 当前状态

> 最后核验：2026-08-02 CST。本文只保留当前运行事实、证据边界和下一停止线；已合入
> 候选、历史事故与旧读回通过 Git 历史及仓外 release-evidence 追溯，不在此重复。

## 当前版本与运行面

| 层级 | 当前事实 | 证据边界 |
| --- | --- | --- |
| 本地主线 | 使用 `git rev-parse HEAD origin/main` 读取并比较 | 本地 `main` 必须与远端主线同 head；状态页不固定会被自身提交淘汰的 SHA。 |
| GitHub 主线 | 使用 `git rev-parse HEAD origin/main` 读取并比较 | 合入记录、CI 与精确 commit 以 GitHub 和 Git 历史为准；不是生产切换。 |
| 三条市场 lane | A股、Crypto、CNFutures 均与 `origin/main` 同 head，`ahead=0`、`behind=0` | 三个长期 worktree 均干净；它们不是独立生产 release。 |
| TA production current | `2b7b52bfb552247478c5a78f854d365eb9fcc335` | 当前 A股 timer 使用该 immutable release。 |
| TradingDatas current | `983c5f63fee1c166db40859420f817b04cc639d9` | 18082 仅内部监听，generic collector timer active。 |
| TradingDatas Crypto current | `a60e5425c9119bf9fe24c1b08a070907db58febd` | 18083 独立内部监听。 |

`tradingagent-front-api.service` 保持 inactive；8082、8787 均未监听。旧
SharedSignals service/timer 均为 masked，MarketGraph runtime 保持暂停。当前没有
broker、Testnet、Live、模型网络、公开交易入口或真实交易权限；
`REAL_TRADING_ENABLED=false`。

## A股

- 30 股分钟 session/paper timer 均为 `enabled/active`，仍是 simulation-only。
- 500 股 scale500 session/paper timer 均为 `disabled/inactive`。它需要 TradingDatas
  正式连续两根 500/500、同一 bar time、完整 receipt/lineage、terminal pagination
  replay 的证据；周末或候选 loopback 不能替代。
- TradingCopilot event-evidence v2 已在独立候选中以 UID987 完成一次正式
  catalog/query 读回：`cn.dataset.research_report` 接纳 1 条事件；`anns_d`、
  `cctv_news`、`irm_qa_sh`、`irm_qa_sz` 均因
  `ashare_evidence_metadata_not_ready` 拒绝。它没有切换 `current`、启用新 unit、
  创建候选/订单/资本/训练事实或生成情绪标签。详见
  [event-evidence v2 readback](docs/reports/2026-08-02-trading-copilot-event-evidence-v2-readback.md)。
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

TradingDatas 已提供通用 `windowed_unique_primary_key` 与有界 fanout 完整性能力；它
没有激活 `major_news` 或任何新数据集。新闻、资金流与其它日频接口只能在各自真实
receipt + formal API readback 后进入消费者。

## 下一停止线

1. **Crypto**：从最近连续 completion 段自然累积至 288 根；随后先运行 disabled
   full scrub 与同槽幂等 replay，再单独决定是否安装离线 learning worker。核心数据、
   资本和 timer 不依赖 learning 成功。
2. **A股**：下一个交易时段验证 30 股当前分钟链；500 股仅在 TD 的两轮正式 500/500
   canary 通过后由独立 scale500 root late-start。
3. **事件/新闻**：等待 TD 以通用 registry/collector 提供 `major_news` 等数据集的
   producer receipt、完整性合同与正式 API metadata；不得用候选能力或空 HTTP 响应
   提前放行。
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
