# TradingAgent 当前状态

> 最后更新：2026-08-02 03:50 CST。本文只维护当前事实与下一停止线；历史候选和失败证据通过 Git 与服务器只读证据目录追溯。长期边界见 [AGENTS.md](AGENTS.md)，运行与回滚见 [docs/operations.md](docs/operations.md)。

> **2026-08-02 TradingCopilot V5 个人状态逻辑候选：** 当前开发分支把关注列表、完整资金与持仓、人工决策记录拆成独立工作区；个股右栏只显示当前股票持仓，并可进入全部持仓。持仓会自动纳入关注，纯关注不计入资产，持仓未移除前不能取消关注。普通 `/?product=copilot` 不再因开发模式注入许继电气等演示资产；演示只由 `?demo=1` 或专用环境开关显式进入，且演示修改不持久化。缺少实时市值时页面不再用成本倒推资产差额，券商资产保留“待人工确认”。移动端保留四个主工作区底部导航并使用卡片列表。该候选仍只写 TradingCopilot 独立状态，不连接券商、不影响 Quant Core 资本/订单/样本/模型晋级；生产文件与 runtime 未改变。

> **2026-08-02 TradingCopilot V3 预测门禁主线：** PR [#155](https://github.com/NicholasHan1226/Tradingagent.cc/pull/155) 已合入 `main` `d33ed92`。同仓 `TradingCopilot/` 领域和 `front/?product=copilot` 已包含申报资金/持仓、关注股、个股图表、关联事件、多空证据与人工意图。V3 新增只读 `GET /api/trading-copilot/stock-intelligence?symbol=` 正式投影入口和九项预测readiness门禁；未校准演示默认关闭预测，只显示明确的 `m30` 定性情景及宽/窄研究包络，不再发布情景百分比、50%/80%或置信度标签。研究线由最近最多20点的确定性线性基线生成并可重放，非1D周期明确停用当前m30预测。Kronos固定为同门禁Challenger，不能凭模型名显示概率。对应前端全量为 `50 files / 313 tests passed`，lint与client/API build通过；该主线代码不下载Kronos权重、不连接券商、不写量化资本/订单/样本。生产文件、runtime、正式个股投影数据与入口部署仍未改变。

> **2026-08-02 TradingCopilot 共享能力边界：** 机器合同 `tradingagent.trading_copilot_shared_capability_boundary.v1` 把能力固定为共享只读底座、Quant Core 专属 authority 与 TradingCopilot 专属个人状态三组。行情/规则/PIT特征/事件/市场状态/基线与Kronos/OOS/校准/个股投影只维护一套研究证据；Quant独占候选/组合/资本/风险放行/订单/样本/晋级，Copilot独占申报资金/持仓/关注/人工意图/个人复盘。合同和负例测试禁止Copilot状态流入量化资本、执行、样本或晋级，也禁止前端生成的曲线成为正式预测证据。该边界只改变仓库架构合同与文档，不改变生产文件、runtime、timer、模型网络、broker或真实交易权限。

> **2026-08-02 TradingCopilot V4 个股终端候选：** 当前开发分支把个股首屏重排为行情主区与公司资料/证据共识/舆论温度右栏，关注列表改为抽屉；重大价格变化、公告、新闻和舆论按股票代码并列展示，证据共识明确不是分析师一致预期，舆论热度不转换为上涨概率。该候选只消费正式只读个股投影或明确 `demo_fixture`，只保留 Copilot `human_intent_only` 写面；未新增 Quant Core 资本、订单、样本或晋级写入口。生产文件、runtime 与正式个股投影数据仍未改变。

> **2026-08-02 周末双市场运行复核：** Crypto G5 核心 timer 为
> `enabled/active`，并固定到 immutable release
> `e2c159e90d458d8859c0a1b37b8de83f07665c4a`，不依赖服务器 TA `current`
> symlink。只读 health 与 acceptance timers 已在比对安装字节和回滚命令后启用；首个
> health 自动轮与随后核心自动轮均为 `success/exit0`。01:51 的 fresh 只读 health 与
> acceptance 读回为 `healthy/balanced=true`，G5 已有 56 个 completed 5-minute
> windows；最新连续段为 55 根/275 分钟，早期仍保留一个 10 分钟缺口。当前已完成两次
> BTC/ETH 规则约束下的模拟买入与卖出闭环，均为 ETHUSDT 的
> `momentum_reversal_observed` 退出；账户已平仓，现金/权益均为
> `9994.46946191 USDT`，累计费用 `3.99402809 USDT`，已实现损益
> `-5.53053809 USDT`，订单 4 笔、买卖各 2 笔、零重复 fill。该亏损只是早期工程样本，
> 不代表策略胜率。24 小时 288 槽 acceptance 尚未完成，状态为 `not_ready`，
> `next_action=continue_core_accumulation`，learning timer 继续关闭；
> 真实交易、Testnet/Live broker、模型网络、自动晋级和自动风险扩张均为 false。只读
> monitor 证据根为
> `/opt/investment/release-evidence/tradingagent/20260801T234300Z-crypto-g5-readonly-monitor/`。
>
> A股 20260731 状态包完成周末只读日结：48 个预期 5 分钟槽中实际 40 个，缺少
> 09:35、09:40、13:05、13:10、13:15、13:20、13:35、14:25；
> `full_session_complete=false`、`learning_eligible=false`。四个 shadow sleeve 均
> `fixture_reconciled`，共 6 笔模拟 fill、10 笔未成交、费用 30.19992 CNY，40 条已接收
> evidence 无新增拒绝。该报告只用于工程与决策账本复核，不进入训练、晋级或真实执行；
> A股 30-symbol session/paper timers 保持 `enabled/active`，500-symbol 仍等待下一个真实
> 交易时段的两根相邻 500/500 门禁。日结证据位于
> `/opt/investment/release-evidence/tradingagent/20260801T154443Z-ashare-weekend-readonly/`。
> 同一周末的 ridge/logistic 影子 canary 用时约 `1.4/11.879 ms`，仅生成 4 个 fixture
> score，保持 `authority=none/shadow_only=true`，证据位于
> `/opt/investment/release-evidence/tradingagent/20260801T154614Z-shadow-model-weekend/`。
>
> CNFutures formal 18082 只读复核仍为 NO-GO：`fut_basic` 有 receipt/lineage 但为
> `stale/degraded/quality invalid`，`ft_mins` 与 `rt_fut_min` query 均为 404；离线
> acceptance/training/readiness 共 78 项测试通过，只证明 fixture fail-closed，未启动
> runner、timer 或模拟成交。证据位于
> `/opt/investment/release-evidence/tradingagent/20260801T155212Z-cnfutures-formal-readonly/`。

> **2026-08-01 Tradings 清理边界：** MarketGraph 作为可选增强已暂停：API service
> disabled/inactive、8080 关闭、MG cron 清零；TradingAgent 不依赖 MG 才能生成
> observation/hold/risk reject/sim-only 样本。旧 `/opt/investment/SharedSignals` 活跃路径
> 已移至 root-only 退役归档，8082 关闭；TA 不恢复旧 route/SQLite/file fallback。
> TradingDatas 18082/18083 与其采集 timer 未因该动作停止，`REAL_TRADING_ENABLED=false`
> 不变。MG/SharedSignals 的研究与数据历史未删除，不能进入当前资本、模型或订单统计。

> **2026-08-01 dataset contract fingerprint 候选：** 独立分支
> `codex/dataset-contract-fingerprint-v1` 在已合入的 readiness 合同上增加 canonical
> per-dataset SHA-256 helper。它只投影 catalog 的 dataset/schema/default fields/
> operators/order/limits/identity，排除运行状态和全局 catalog 版本；调用方必须重算，
> 不能信任自报hash。候选尚未合入或部署，旧 consumer 在迁移前仍按现有manifest
> fail closed。

> **2026-08-01 有效运行版本解析候选：** 独立分支
> `codex/effective-runtime-release-v1` 新增只读工具，分别核验 `current` symlink、
> systemd effective ExecStart/WorkingDirectory 与 active process 的 cwd/exe/cmdline，
> 发现 drop-in 固定旧 release、process/unit 不一致、active无PID或release不可证明时
> fail closed。候选不读取 Environment/token/账本，不切换current、不重启service或
> timer；尚未合入main或部署。

> **2026-08-01 分层 readiness 合同候选：** 独立分支
> `codex/readiness-authority-v1` 新增机器合同
> `tradingagent.evidence_readiness.v1`，把 observation、historical PIT、
> delayed-paper 与 execution 四类权限拆开，并冻结按用途 freshness、逐 dataset
> contract fingerprint、routine 单遍历/高风险双遍历、A股局部连续窗口/99%零名义
> cohort shadow、Crypto 分段学习/288槽运行成熟度语义。该候选只改治理合同、校验器、
> 测试与文档，尚未合入 main、部署或改变任何现役 timer/runtime；真实执行、自动晋级、
> 自动风险扩张继续关闭。

> **2026-08-01 影子数值模型旁路验收：** M0 已新增依赖无关的 ridge 与 elastic-net
> logistic 控制组，M1 新增固定
> `numpy==2.0.2/scipy==1.18.0/lightgbm==4.6.0`、最多 2 CPU 线程的
> 浅层 LightGBM 可选 backend。冻结数据合同绑定特征/receipt/时点、训练截止、标签可见时间、
> 严格样本外预测向量和 artifact/prediction hash；无历史 PIT/revision authority 时只能作为
> engineering fixture，不能进入 predictive validation。三个普通 PR
> [#122](https://github.com/NicholasHan1226/Tradingagent.cc/pull/122)、
> [#123](https://github.com/NicholasHan1226/Tradingagent.cc/pull/123)、
> [#124](https://github.com/NicholasHan1226/Tradingagent.cc/pull/124) 已合入，权威 main
> `ac4f87098ced3be9899b636a9c7d99a458507cb5`；本地全仓为
> `4131 passed, 1 skipped`，三路 CI 的 `test/front` 均 PASS。
>
> 服务器以同 SHA 建立隔离候选与独立 venv，真实 Linux LightGBM 加载、训练、序列化和双跑
> 重放已完成，精确模型测试 `25 passed`；one-shot 墙钟约 0.24 秒、最大 RSS 53,632 KiB，
> ridge/logistic/LightGBM 全部保持 `authority=none`、`shadow_only=true`、无模型网络与真实
> 交易。证据位于
> `/opt/investment/release-evidence/tradingagent/20260801T-model-shadow-ac4f87098ced/`。
> 首轮安装暴露未固定 SciPy，次轮暴露 LightGBM 结尾换行归一问题，均以独立修正 PR 关闭且
> 原失败证据保留。服务器现役 release、crontab、18082/18083、市场 timer 均未改变；没有
> 模型 service/timer、自动训练、自动 Champion、资本、风险或订单权限。Kronos/Chronos/TimesFM
> 尚未安装或下载权重，只保留为下一阶段 batch benchmark。

> **2026-07-31 10:20 A股回滚运行与事件 shadow parity：** TradingDatas 正式
> `current` 仍为 30-symbol release
> `5ac3925c3931a81132ea02abb16f9745033fb6dc`，TradingAgent 正式 `current`
> 仍为 `2b7b52bfb552247478c5a78f854d365eb9fcc335`。500 live 门禁失败后没有继续
> 冒险放行；旧 30 session/paper timers 已恢复为 `enabled/active`。09:57 会话重新初始化
> PASS，09:58 仅通过显式 `--allow-late-start` 消费 09:45，并把 09:35/09:40 记为真实 gap；
> 当天保持 `full_session_complete=false/learning_eligible=false`。随后正常自动轮连续处理
> 09:50、09:55、10:00、10:05 的 30/30 快照，均有 30 个特征/候选、四个 sleeve 对账通过、
> 零持仓，`capital_authority=false`、`execution_authority=false`、
> `REAL_TRADING_ENABLED=false`。
>
> 事件适配代码已在 main `591c6a1f21f1d97701ea5b816c2ff2844f1ef5e4` 和同 SHA
> server immutable release 中，但没有切换现役 TA current 或 timer。使用 TradingDatas
> server immutable `7de9ed58ef17da8422a16be3a8eb1f9441471d46` 的短时只读 loopback
> 候选 API，对一个 20260731 主板公告完成真实 `GET /v1/catalog` + `POST /v1/query`
> 双跑：单页单行、same-observation=true、零审计拒绝。TA 只生成
> `deterministic_shadow_score_not_probability` 与 `SHADOW_ONLY` Decision Ledger 记录，
> `requested_notional/fill/cost=0`，`calibrated_probability=null`、
> `historical_known_time_proven=false`、`pit_feature_eligible=false`，没有 LLM 网络调用或
> 交易权限。全市场公告批量输入因混入创业板/科创板代码被适配器正确拒绝，证明正式接入必须
> 先按冻结主板 Universe 收窄。临时候选端口已停止；正式 18082 因当前仍挂载 5ac，尚不能
> 作为事件数据生产入口。

> **2026-07-31 多市场运行拓扑与模型路线合同：** 仓库新增
> `tradingagent.runtime_topology.v1`机器合同和校验器，冻结A股、CNFutures、
> Crypto三个独立market writer/fault domain/state namespace，以及
> `single_host_sim`、按市场拆机和独立`research-host`三种sim-only放置方式。
> 数据面仍只允许TradingDatas catalog/query；共享SQLite/NFS双写、第二active
> writer、provider fallback、自动Champion/风险扩张和真实执行均失败关闭。
> 架构文档同时冻结LightGBM/校准/DeepSeek evidence优先、Kronos与一个通用时序
> 模型后置挑战、HMM/GARCH与凸优化分阶段验收路线。这只是仓库架构与测试合同，
> 未部署新服务器、未迁移现役A股/Crypto状态、未调用模型网络，也未改变任何timer、
> broker或真实交易权限。

> **2026-07-30 20:15 加速预检收口：** 当前服务器不可变 TradingAgent
> release 仍为 `1c99cffa43d2f6de587538f371b85291c6ab1d55`，TradingDatas
> A股生产仍为已验证的 30 股 release
> `5ac3925c3931a81132ea02abb16f9745033fb6dc`。本轮没有切换生产数据面、
> broker、Testnet/Live、模型网络或真实交易权限。
>
> Crypto 核心 timer 全程保持 `enabled/active`，completion/observation 已继续
> 增长至 292/292。独立 learning full scrub 首次补齐 86 条历史缺失投影至
> 289，幂等重放恢复 0 条；incremental one-shot 随后投影并重放第 290 条；
> 本轮末再次对核心新增的 2 条 completion 执行 detached full scrub，
> `292 completion → 292 projection receipt`，服务 `success/exit0`。
> 学习侧 sample/KPI/Challenger/checkpoint 均为 292，Challenger 仍只建议且
> 人工晋级；`model_network_used=false`、`promotion_authorized=false`、
> `execution_authority=false`、`real_trading_enabled=false`。两个 learning
> timer 继续 `disabled/inactive`，因此核心不依赖学习成功，24 小时连续稳定
> 门禁仍未被绕过。首轮、幂等与增量证据分别位于
> `/opt/investment/release-evidence/tradingagent/20260730T115748Z-crypto-learning-full-scrub-retry-1c99cff/`、
> `/opt/investment/release-evidence/tradingagent/20260730T115858Z-crypto-learning-full-scrub-idempotent-1c99cff/`
> 与
> `/opt/investment/release-evidence/tradingagent/20260730T120457Z-crypto-learning-incremental-1c99cff/`。
>
> A股 500 股明日会话的仓外 Universe 已冻结为 500 个沪深主板代码，SHA256
> `2894024d2dad1a42d3891e7ebb66dcc55475724c2a8a3d454f25d98d63588814`；
> 正式策略口径为 468 个可进入候选、32 个风险警示代码仅观察。使用现役
> release、TA UID987、正式 18082 catalog/query 和 token-file 的
> 2026-07-31 隔离 initializer 已 PASS：500/500 上一交易日 reference、
> page/row budget 500、Universe SHA 一致，只生成
> `minute-manifest.json`、`reference-facts.json`、`universe.json`，没有
> state bundle、资本、订单、成交或真实交易权限。早期辅助审计脚本曾错误使用
> shell 引号包裹的相对路径；初始化结果未受影响，权威纠正回执为
> `/opt/investment/release-evidence/tradingagent/20260730T120602Z-ashare500-next-session-preflight-1c99cff/correction-v4-authoritative-audit.json`
> 及 `evidence-v5.sha256`。
>
> 当前剩余唯一 A股 500 放行门禁不是继续改 TA：TradingDatas 必须在
> 2026-07-31 真实交易时段提供两根相邻、同一 bar end、无重复/缺失、正式
> API 可终止分页的 500/500 快照，且逐轮
> `ready/success/fresh/valid/non-degraded`、receipt/lineage 完整。通过后才在
> `scale500` 隔离 root 运行 late-start runner 并切换 A股 timer；失败则继续
> 使用现有 30 股链路。当前 A股 timer 仍 `enabled/active`，明日 09:18
> initializer 与 09:49 minute timer 按现有 30 股生产合同运行。

> **2026-07-30 19:37 双市场收盘/恢复状态：** TradingAgent 本轮运行修复的
> 权威代码提交与服务器不可变 release 为
> `1c99cffa43d2f6de587538f371b85291c6ab1d55`；后续纯状态文档提交不改变该
> runtime。Crypto 在 15:30 后因
> 18083 catalog 响应超过旧 2 秒客户端超时而连续失败关闭；PR
> [#91](https://github.com/NicholasHan1226/Tradingagent.cc/pull/91)
> 将 loopback 单请求超时调整为 5 秒。首次生产恢复又诚实暴露旧
> `TimeoutStartSec=120s` 对两轮完整请求预算缺少运行开销余量，服务在第二轮
> pending 时被 systemd 终止；服务器立即原子回退，timer 保持关闭，pending
> 未删除或改写。PR
> [#92](https://github.com/NicholasHan1226/Tradingagent.cc/pull/92)
> 随后按真实上限 `2 × (1 catalog + 10 query pages) × 5s = 110s`
> 将服务停止线调整为 180 秒，保留两轮 backlog 与 outage-gap 恢复语义。
> 两个 PR 的 front/test CI 均 SUCCESS；最终 release 在服务器通过
> 34 项精确测试、Crypto 全量 `283 passed, 8 subtests passed` 与
> `systemd-analyze verify`。
>
> 现存 pending 已由同一正式 systemd 单元幂等恢复，后续 backlog 串行追赶，
> 没有直接 provider/SQLite、手工账本写入或跳槽。恢复 timer 后的两个相邻
> 自动轮分别推进到 `11:25Z` 与 `11:30Z`，completion
> `283 → 284 → 285`，均 `success/exit0`、pending 为空。最终只读重放：
> 285 observations/285 completions、575 条决策事件、1,145 条资本事件、
> 570 个 run bundles、2 笔既有模拟成交、重复 fill reference=0、
> 未完成资本 cycle=0、reserved cash=0；现金
> `7998.21225974 USDT`，BTC `0.01563`、ETH `0.5211`，累计费用
> `1.99978796 USDT`。Crypto 核心 timer 已恢复
> `enabled/active`；learning timer 继续关闭，新的 24 小时零失败观察窗口
> 从本次恢复后重新计算。
>
> A股当日状态保持真实缺口：分钟状态只接受 27/48 槽，迟到的 15:00
> 数据超过 delayed-paper 证据时限而被拒绝，不补写也不冒充完整交易日。
> A股离线学习 CLI 已由 PR
> [#90](https://github.com/NicholasHan1226/Tradingagent.cc/pull/90)
> 修复并完成幂等 one-shot；投影明确为
> `blocked/fixture_session_incomplete`，training sample=0，未自动训练或晋级。
> A股核心 timer 继续 `enabled/active`，离线学习 timer 尚未安装/启用。
> TradingDatas A股生产仍为验证过的 30 股链路；500 股代码修正已合入 TD
> main，但只有下一交易时段取得两根相邻、真实、完整 500/500 后才允许再次切换。

> **2026-07-30 14:30 双市场运行收口：** TradingAgent 当前运行代码基线与
> 服务器不可变 release 均为
> `db9707a5d1385d354035c829179289dfd1e1b5e8`。Crypto 5 分钟核心 timer
> 为 `enabled/active`，最近自动轮 `success/exit0`；10,000 USDT 模拟账户
> `balanced=true`，现金约 7,998.21 USDT，保留 BTC/ETH 两笔既有模拟持仓与
> 约 2.00 USDT 累计费用。全链继续
> `REAL_TRADING_ENABLED=false`、`execution_authority=false`、
> `production_eligible=false`，无 broker/Testnet/Live、无模型网络或自动
> 扩风险。离线 learning incremental/full-scrub one-shot 已在当前 release
> 对 203 个 completion 完成增量投影与全量校验；Challenger 仍需人工晋级。
> 两个 learning timer 继续 `disabled/inactive`，原因是此前 24 小时窗口仍有
> 5 次历史失败，尚不能诚实宣称零失败自动学习。
>
> A股 500 股会话初始化本身已 PASS：500 个日线 reference、500 个 Universe、
> Universe SHA256
> `2894024d2dad1a42d3891e7ebb66dcc55475724c2a8a3d454f25d98d63588814`，
> 且隔离目录未生成资金、订单或成交状态。但 TradingDatas 首个生产实时轮未能
> 对目标 13:00 bar 返回 500/500 完整快照，因此按门禁回滚至正式 30 股 release
> `5ac3925c3931a81132ea02abb16f9745033fb6dc`；这不能写成“500 股已运行”。
> TradingAgent 随后无损恢复原 30 股 state root，保留上午资金、持仓和账本，
> 没有删除或改写 scale500 初始化证据。14:24 手动恢复轮处理 14:10 快照 PASS；
> 14:29 首个自动轮处理 14:15 快照 PASS，30 个特征、30 个候选、0 数据拒绝，
> baseline/dynamic_position 模拟权益约 50,076.95 CNY，账本
> `real_trading_enabled=false`。A股 timer 已恢复为 `enabled/active`，中断期间
> 的 13:05–14:05 缺口被明确记录且不回填，因此当日
> `full_session_complete=false/learning_eligible=false`。服务器恢复证据位于
> `/opt/investment/release-evidence/tradingagent/20260730T062454Z-ashare30-runtime-restore/`。
>
> 当前下一停止线：A股继续以 30 股积累，500 股只在 TradingDatas 隔离环境重新
> 验证五个 100 股分片、单一 bar end、完整 receipt/lineage 与两根相邻实时快照，
> 未通过前不再次切生产；Crypto 核心继续 24×7 运行，学习 timer 只有在新的连续
> 24 小时零核心/投影失败窗口后才允许启用。

> **2026-07-30 10:10 A股分钟闭环恢复：** TradingDatas 先从误含
> 500 股 fanout 的 `78435bb37754fda5bb4d2be6d46a9b63211b7401`
> 原子恢复到已验证的固定 30 股 release
> `5ac3925c3931a81132ea02abb16f9745033fb6dc`。正式 18082 随后以 TA
> 身份精确读回 09:50、09:55 两根相邻快照；每根均为 30/30、单一 bar
> end、`ready/success/fresh/valid/non-degraded`，receipt 与 lineage
> 完整。TradingDatas 5 分钟 timer 已重新 `enabled/active`；500 股仍只在
> 独立数据库候选中验收，不进入当前生产数据面。
>
> 当日 TA 09:18 会话初始化成功，但 09:35–09:45 三个槽因数据 timer
> 事故缺失。10:03 使用仓库既有 `--allow-late-start` 事故恢复入口建立
> bundle，只消费 09:50 的 30/30 真实快照，并固定写入
> `late_start=true`、`gap_recovery=true`、
> `full_session_complete=false`、`learning_eligible=false`；没有补造
> 历史 K 线，也没有资本、订单或成交副作用。10:04 同槽自动重放为
> `bar_already_processed/noop`；10:09 正常 timer 无人工参数处理 09:55
> 快照，得到 30 个特征、30 个候选、0 数据拒绝，模拟权益仍为 50,000 CNY、
> 零持仓。A股分钟 timer 继续 `enabled/active`，全链仍为 delayed-paper，
> `REAL_TRADING_ENABLED=false`、`capital_authority=false`、
> `execution_authority=false`。本轮服务器证据保存在
> `/opt/investment/release-evidence/tradingagent/7bdb2f6701a8fe6e5a7e70678730997c44694108/ashare-20260730-late-start/`。
>
> 10:14 的下一次 TA 自动轮随后失败关闭。Fresh consumer readback 显示，
> TradingDatas 10:15 最新生产 receipt 触发同质快照校验失败；正式 18082
> 对 09:55、10:00、10:05 的精确查询均投影为
> `state=failed,degraded=true,quality.valid=false,reasons=[validation_failed]`。
> TA 没有绕过该 metadata，也没有继续产生候选、订单或资本变更。当前停止线是
> TradingDatas 以新的单一 bar end 30/30 成功 receipt 恢复数据 authority。
> 10:20 通用 collector 随后完成该门禁：正式 18082 对 10:15 精确查询返回
> 30/30、`ready/fresh/valid/non-degraded`，新 receipt 与 lineage 完整。
> 10:00–10:10 仍是不可补造的真实缺口；TA 将在延迟窗口到达 10:15 后按既有
> gap-recovery 规则取消跨缺口 pending、重置特征并继续，缺口日保持不可学习。
> 10:29 自动轮已完成该恢复：30/30，明确记录 10:00、10:05、10:10 三个
> gap slots，`candidate_count=0`、`pending_sleeves=[]`，没有人工参数、订单或
> 资本变化。10:34 下一正常自动轮处理 10:20 的 30/30 快照，恢复为 30 个
> 特征、30 个候选、0 数据拒绝，并重新建立 baseline/dynamic_position
> delayed-paper 待模拟动作；模拟权益仍为 50,000 CNY、零持仓。
>
> 同时，500 股隔离候选已取得 10:20 与 10:25 两根相邻实时快照；每根均为
> 5×100、500 个唯一代码、单一 bar end、零重复，耗时分别 38.3 秒和
> 15.9 秒。10:20 的隔离标准 API 读回为 5 页终止 cursor，500/500、
> `ready/success/fresh/valid/non-degraded`，receipt 与 lineage 完整。
> 500 股仍未合并或切入正式 18082；最终门禁是 10:25 同口径 API 分页读回、
> 最小回归与独立审查 P0/P1=0。

> **2026-07-30 09:42 Crypto 自动闭环恢复：** PR
> [#80](https://github.com/NicholasHan1226/Tradingagent.cc/pull/80) 已普通合并，
> 代码 release 为 `7bdb2f6701a8fe6e5a7e70678730997c44694108`；随后状态文档
> PR [#81](https://github.com/NicholasHan1226/Tradingagent.cc/pull/81) 合入后，
> `origin/main` 与永久 Crypto lane 均为
> `b067f46f33696980a637b33abb6293694beee09d`。两次 front/test CI 均 SUCCESS，
> 本地 Crypto 全量 `278 passed`，服务器 runtime `29 passed`。服务器
> `current` 已原子切换到同 SHA 的 root-owned、只读不可变 release，
> `1e08e905d13c778bab6fdc5cdf5c4cb7f74b7763` 保留为直接回滚点。
>
> 生产失败原因不是资本或交易逻辑，而是 TradingDatas 对旧历史窗口返回
> `crypto_5m_window_incomplete`；runtime 现只把它与既有明确历史
> `data_through` cutoff 拒绝一起识别为可审计、不可恢复的数据缺口。首次
> one-shot 仅追加一条 checksum/index/capital-head 绑定的 `data_gap`，
> skipped range 为 `2026-07-29T07:15:00Z` 至 `16:20:00Z`，恢复槽为
> `16:25Z`；候选、订单、成交和资本均未写入，资本与 runs 字节指纹不变。
> 同槽重放为 `noop`，0 次网络调用且全部账本指纹不变。
>
> 下一相邻窗口随后正常进入 core，之后两个相邻自动轮均 SUCCESS。自动运行
> 至 2026-07-30 09:40 CST 时，generation-2 epoch 已有 166 个
> observation、166 个 completion、332 个唯一 run，仍只有 1 条
> data_gap；最新市场槽为 `2026-07-30T01:35:00Z`，pending 为空。资本账本
> 669 个事件连续且 ID 唯一，现有 10,000 USDT 模拟基线、BTC/ETH 持仓与
> 费用历史均保留。Crypto TA timer 与 TradingDatas Crypto timer 当前均
> `enabled/active`。全链继续固定 `REAL_TRADING_ENABLED=false`、
> `production_eligible=false`、`execution_authority=false`，无
> broker/Testnet/Live、无模型网络、无学习阻塞、无自动晋级或风险扩张。

> **2026-07-29 20:32 A股分钟缺口恢复修复：** PR
> [#76](https://github.com/NicholasHan1226/Tradingagent.cc/pull/76) 已普通合并，
> GitHub 主线为 `cacb1b1a675665987c8e6d7243377a633f31a23c`；front/test CI
> 均通过，本地 A股全量测试为 `840 passed`。修复不补造缺失 K 线，也不改写
> 2026-07-29 的历史 bundle：遇到日内缺口时，所有跨缺口 pending 模拟订单先
> 形成未成交回执，缺口写入状态校验范围，滚动特征重置；恢复后的第一根完整 K 线
> 只建立新基线，至少再取得一根连续完整 K 线后才允许产生候选。缺口日永久保持
> `full_session_complete=false/learning_eligible=false`，但后续完整分钟仍可
> 继续 observation、反事实、盯市和对账积累。服务器随后从
> `bc8880dfd3c77ee358736d58e0cf9c377de154b3` 原子切换到不可变 release
> `946db638c9ac85410fa697f81dd1c6da02723903`；824 个文件的树 SHA256 为
> `8d038d075db11218aae9ada56b162323c76604f6c9628cfacba497be46da8b16`，
> 现役解释器模块导入与 systemd unit verify 通过。两个 A股 timer 继续
> `enabled/active`，下一次分别为 2026-07-30 09:18 与 09:49；没有新建或启用
> 其它任务。`REAL_TRADING_ENABLED=false`、无 broker/真实交易不变，旧 release
> 保留为直接回滚。

> **2026-07-29 17:58 次日会话预检：** TradingDatas 已将仅适用于
> `trade_calendar` 的 registry-declared 下一日窗口普通合入并安全发布为
> `64695852ff5be23b3cf8a8d1d03a13f7274e4586`，回滚 release
> `5ac3925c3931a81132ea02abb16f9745033fb6dc` 保留。17:50 通用采集后，
> 正式 18082 以 TA UID987 查询 `SSE/20260730` 得到唯一行
> `is_open=1,pretrade_date=20260729`，envelope 为
> `ready/success/fresh/valid/non-degraded`，receipt 与 lineage 完整。
> 17:55 盘后日线刷新完成后，当前审核 30 股的 `20260729 daily.close`
> 三批 10 只均取得 30/30。
>
> TA 随后在隔离 evidence root 对 2026-07-30 初始化双跑：首次
> `reused=false`，第二次 `reused=true`；两次均为 30 只、Universe SHA256
> `0e26f54fc2ab391f0187a5787f9955b90e8a2ff21969957565749b733e035203`。
> 隔离目录仅含 `minute-manifest.json`、`reference-facts.json`、
> `universe.json` 三项 0600 输入；无 state bundle、资本、订单、成交或账本。
> 正式 `/var/lib/tradingagent/ashare-minute-paper/20260730` 仍不存在，下一门禁
> 是 2026-07-30 09:18 正式 timer 独立初始化及随后完整分钟闭环。

> **2026-07-29 17:00 盘后增量：** TradingDatas 的 16:30 通用盘后轮已
> `success/exit0`。正式 18082 以 TA UID987 读取
> `cn.equity.daily(trade_date=20260729)` 为
> `ready/success/fresh/valid/non-degraded`，并带完整 receipt/lineage；
> 当前审核 30 股经 3 个每批 10 只的有界查询取得 30/30、30 个唯一股票，
> 双跑一致。明日会话初始化仍只阻塞于
> `cn.market.trade_calendar(exchange=SSE,cal_date=20260730)` 返回 0 行。
> 现役 TD 合同没有受控未来日期 override，因而未绕过合同执行 one-shot；
> TD 正在单独评审仅由 registry 声明 `known_future_horizon_days=1` 的通用修正。
> 隔离预检 root 只含 3 项 0600 输入模板，正式 `20260730` 目录仍不存在，
> 未创建 state bundle、资本、订单或成交。

> **2026-07-29 16:05 最新运行结论：** TradingDatas 的 A股与 Crypto
> 数据采集均在自动运行，但两个 TradingAgent 模拟闭环当前都保持失败关闭，
> 不能写成“全天稳定完成”。Crypto 数据面已扩为 10 个币种、20 个行情/规则
> dataset；正式 18083 readback 为 20/20 `ready/fresh/valid/non-degraded`。
> 10 个 5 分钟行情 dataset 各有 51,844 行，时间范围
> `2026-01-30T07:35:00Z` 至 `2026-07-29T07:50:00Z`，逐币种均无重复时间、
> 无 5 分钟间隔缺口；180 天回填与后续增量采集已经衔接。Crypto TA 核心 timer
> 仍为 `enabled/inactive`：补跑旧槽时，TradingDatas 返回行已按 `as_of`
> 正确过滤，但 envelope `data_through` 仍绑定更晚的全局 receipt，TA 因
> `metadata.data_through must not be after the requested as_of` 正确拒绝。
> 既有 10,000 USDT 模拟资本、持仓、成交和 append-only ledger 未改写。

> **A股同轮结论：** TradingDatas 30 股 5 分钟 timer 与正式 18082 API
> 持续运行；2026-07-29 下午 `13:10–15:00` 每个 bar end 均可正式查询
> 30/30，只有午后首根 `13:05` 返回零行。TA 当日状态最后成功处理
> `11:30`，且仍有待结算模拟动作，因此没有用 `13:10` 冒充下一根成交价，
> 下午所有轮次均以 `minute_query_returned_no_bars` / continuity gate
> 失败关闭。既有 bundle 停在 10 个已处理快照，资本与历史订单未回填或改写。
> A股 TA timer 保持 `enabled/active`，下一次为 2026-07-30 新会话；今天不补单，
> 不进入学习验收。500 股 TradingDatas 候选仅完成隔离单快照
> `500/500`，生产仍为 30 股，待下一交易日两根相邻实时快照后再决定切换。

> **代码与学习边界：** Crypto 离线学习 worker 已合入
> `origin/main@69e03e6bbfbfcfd2ee4541b471e106a67f7c8d1f`，但学习 timer
> 尚未部署或启用；核心连续运行未满 24 小时，不能自动产生 Challenger 晋级。
> A股盘后学习同样未启用。两市场继续固定
> `REAL_TRADING_ENABLED=false`，无 broker/Testnet/Live、无真实模型网络、
> 无自动晋级或风险扩张。详细证据见
> [2026-07-29 市场扩容与运行收口](docs/reports/2026-07-29-market-expansion-runtime.md)。

> **2026-07-29 11:09 当前运行事实：** 服务器 `current` 已原子切换到
> `bc8880dfd3c77ee358736d58e0cf9c377de154b3`。Crypto 在独立 generation-2
> epoch 中完成一次 one-shot、同槽幂等重放和 10:45、10:50 两个相邻自动轮，
> 随后又在新 release 上自动推进到 11:00；6 个 observation/completion、
> 12 个模拟 run、29 条资本事件与 12 条决策事件均无重复 ID，`pending=null`。
> Crypto timer 为 `enabled/active`，旧 epoch 组合 SHA 未变；全部运行仍为
> `REAL_TRADING_ENABLED=false`、无 broker/Testnet/Live、无模型网络、无自动晋级
> 或扩风险。

> **A股当前运行事实：** TradingDatas 已部署通用同质快照门禁；正式 18082
> 的 10:45 `rt_min` 精确查询为 30/30、30 个唯一股票和单一 bar end，历史
> 10:25 沪深错位 receipt 不再是当前合同 authority。当前上游约 47 秒可用，
> 因此仍不满足严格 30 秒执行证据；TA 只允许 12 分钟上限的
> `DELAYED_PAPER` 非生产模拟层使用。10:57 人工事故恢复从 10:45 合格快照建立
> 当日 bundle，明确 `late_start=true`、跳过 14 个历史槽、
> `full_session_complete=false/learning_eligible=false`；11:04 和 11:09
> 两个相邻自动连续轮成功处理 10:50、10:55 快照，每轮均为30/30、30 个特征、
> 30 个候选、0 数据拒绝。
> A股 minute timer 为 `enabled/active`，learning timer 未安装；不回填早盘、
> 不把 delayed 数据升级为低延迟执行证据，也不授予资本、broker 或真实交易
> authority。

> 2026-07-29 重启恢复说明：服务器续费恢复后发现 Crypto TA token 的
> `/run` leaf 没有重启重建规则，核心 timer 因 `AssertPathExists` 连续
> fail closed，未产生重复成交或账本改写。仓库候选已补充从既有 root-owned
> canonical source 进行 scoped tmpfiles copy 的规则；只有代码合并、服务器安装、
> leaf owner/mode/readback 和相邻自动轮全部验证后，才可把 Crypto 恢复标记为
> runtime PASS。

> 同轮 A股预开盘恢复发现：严格 token 读取恢复后，30-symbol
> `cn.equity.daily` 单次 `in` 查询被正式 18082 的读取预算以 HTTP 503
> 失败关闭。当前候选把上一交易日参考价读取拆为每批最多 10 只、每批独立双跑和
> envelope 证据绑定；只有正式 30/30 初始化与幂等重放通过后才进入当日分钟模拟。

> TradingDatas 通用采集在当前覆盖下每个 5 分钟边界后占用约 2 分钟，原 A股
> `:40` delayed-paper 和 09:20 initializer 与其稳定重叠。调度候选把 session
> 初始化提前到 09:18，并把 48 个分钟轮统一移到对应边界后约 4 分钟；策略仍只
> 消费固定晚一根的已完成 bar，不改变 48 槽、模型、仓位或资本规则。

> 2026-07-29 正式日线查询修复后，当日 session 在09:35之后才完成初始化。
> 为避免把事后回填伪装成实时模拟，新增仅人工可用的
> `--allow-late-start`事故恢复入口：从当前合格延迟K线开始、明确记录跳过槽位，
> 并将当日固定标为非完整交易日、不可进入学习验收。正常systemd timer不携带该
> 开关，后续K线仍逐槽连续。

> 同日正式 `rt_min` 读回进一步证明：10:15采集轮的最新完成bar是10:05，
> 生产可用滞后为两根5分钟K线。分钟runner据此把固定证据lag从5分钟修正为
> 10分钟，48个timer触发整体后移5分钟；不把`metadata=fresh`误解为请求的下一根
> bar已经存在。

## 当前结论

TradingAgent 已完成 TradingDatas 正式内部 API 的专用身份、Bearer token-file
消费合同、历史 26-active 首屏有界验收，以及 A股核心三数据集 observation 的
第一次正式只读运行。`20260724` current-observation 已形成并完成幂等重放。
2026-07-27 又完成动态 catalog/manifest builder 的仓库实现、服务器失败关闭验收
以及 freshness 修正后的正式重跑。正式目录现为 101 active，但系统仍只映射
calendar/security-master/daily 三个审核过的核心角色；builder 已发布内容寻址
manifest，隔离 observation one-shot 和同 root 幂等重放均 PASS。仓库现已包含
5分钟 fixture/mock 研究闭环：严格或显式 delayed-paper 分钟证据之后可生成透明
滚动特征、未经校准的确定性排名、第一根真正可达K线模拟结算、四个隔离反事实账本、
Decision Ledger 与对账。TradingDatas 已将 `cn.dataset.rt_min` 从10只扩为30只
主板并完成真实30/30自动回读；该数据约晚一个完整5分钟K线，只取得
observation/data accumulation 与 delayed-paper 资格，不满足 TA 的30秒执行证据
门禁。TA现役代码已支持显式审核的500只Universe：分钟分页/行预算按Universe与
catalog上限派生，Universe内容SHA进入manifest，上一日参考价和每根分钟快照都必须
500/500精确覆盖；但500只真实分片运行仍由TradingDatas单独验收，不能由代码容量
推断生产覆盖。TA observation worker 与
durable capital runtime 仍未启动；服务器已用正式18082的30只精确分钟快照手工推进
非生产 delayed-paper one-shot，并已启用仅用于 fixture 自动累计的分钟调度与次日
会话初始化调度。首次分钟自动触发因当日历史缺口失败关闭且账本不变；它不是一次成功
自动模拟轮次，更不授予 broker 或真实交易权限。

### Crypto 核心自动模拟

2026-07-28，Crypto “核心先跑、学习解耦”切片已普通合并，精确提交和 PR 证据
保存在日期化运行报告中。GitHub `front` 与 `test` CI 均通过。服务器 `current`
已切到同一不可变 release；正式 runtime manifest
绑定 18083 catalog `v1-e7ea3dd714066d3c`、BTCUSDT/ETHUSDT 的两组 closed-5m
与两组 rules dataset，以及 profile SHA
`4f5bb40106cf2f63b25a784acae0f13072112afca98dd380e11dab66e19fbe38`。

一次手工 one-shot、同窗口幂等重放和随后两个相邻自动轮次均 PASS。首个冻结检查点
的市场槽连续为 `15:15Z`、`15:20Z`、`15:25Z`；随后自动轮已继续到 `15:30Z`，
共有 4 个 observation、4 个 completion，`pending=null`。资本与决策事件序列连续
且 ID 唯一，全部运行快照的资本状态均 `balanced=true`；重放前后 13 个非锁文件的
组合 SHA 均为
`ec1646906218627a8b9122d5fa201618685556e7eb8be841d349a928e10e5350`，
未产生重复成交或账本改写。Crypto timer 当前为 `enabled/active`，但仍严格是
sim-only：`REAL_TRADING_ENABLED=false`、无 broker/Testnet/Live、无模型网络、
无自动晋级或扩风险。核心 runtime 不读取或创建 `evolution/`，
`learning_invoked=false`；离线学习 worker 尚未部署，不阻塞 5 分钟核心。

完整非敏感运行证据位于
`/opt/investment/release-evidence/tradingagent/20260728T151230Z-crypto-core-e8ba46d7e0cab847d0fa037290e7368c69c54655`。
24 小时连续观察尚未完成，因此当前结论是“自动 sim-only 数据/决策/资本/对账闭环
已启动”，不是工程稳定期已验收，更不是生产交易就绪。

- 本地、`origin/main` 与 GitHub `main` 的当前一致性以交付时
  `git rev-parse HEAD origin/main` 读回为准；本轮 observation 运行代码锚点为
  `6db813c…`；本轮 runtime/front 修复代码锚点为 `eb2e18a…`。
- 服务器已安装对应不可变代码 release：
  `/opt/investment/releases/tradingagent/6db813cdb9c9eaa36ab65c3529ebaeee145aeba2`。
  服务器另安装
  `/opt/investment/releases/tradingagent/eb2e18a6c38b1f5c1139679a8e910c6923fa3edb`
  用于 runtime/unit 验收；动态 builder 对应不可变 release 为
  `/opt/investment/releases/tradingagent/94fcdf767e9e531b18caa1ac0e9ea18cbb1af647`。
  本轮 worker preflight 代码锚点
  `724ea8818feff142df57c4a7bf7b558e29ec0a35` 也已作为
  root-owned、只读的不可变 release 安装到
  `/opt/investment/releases/tradingagent/724ea8818feff142df57c4a7bf7b558e29ec0a35`。
  5分钟 delayed-paper 与500→全量监控容量代码另以 root-owned、只读不可变
  release 安装在
  `/opt/investment/releases/tradingagent/ac828bf5da25ab061f0b3cc785577f18432334e2`；
  自动累计 wrapper、次日会话初始化器和 tracked unit 的上一回滚 release 是
  `/opt/investment/releases/tradingagent/437fa274f5cfc47bac6ae03f7a26270ec404659c`。
  500只会话消费能力的上一回滚 release 是
  `/opt/investment/releases/tradingagent/65ee4f012fc673b0680d63a5a87195b1c7061adb`；
  支持由 `ASHARE_MINUTE_UNIVERSE_SOURCE` 装载审核 Universe 的 root-owned、
  只读不可变 release 的直接回滚点是
  `/opt/investment/releases/tradingagent/f81acb983ed72d0bbfa9d2331a67d62837289dd2`。
  当前 `/opt/investment/releases/tradingagent/current` 指向
  `/opt/investment/releases/tradingagent/e8ba46d7e0cab847d0fa037290e7368c69c54655`；
  直接回滚点为
  `/opt/investment/releases/tradingagent/b4f5d600f3d8bb317375a05b2f613e8a06e89c52`。
  该切换增加 Crypto sim-only 核心，A股现役 minute session/paper 入口及四个
  unit 字节未变；front、observation worker、broker 与真实交易仍未启动。
- TradingDatas 正式内部端点为 `http://127.0.0.1:18082`，只消费
  `GET /v1/catalog` 与 `POST /v1/query`。TA 不固定跨仓 catalog 版本或 active
  数量，每次运行均以正式 catalog 动态发现并逐数据集失败关闭。
- TradingDatas 的通用
  `tradingdatas-provider-native-collect.timer` 已由 TradingDatas owner 启用，
  固定 `OnCalendar=*-*-* *:0/5:00`、`AccuracySec=1s`、
  `RandomizedDelaySec=15s`、`Persistent=true`；这不是 TA timer，也不授予
  TA 模拟或真实交易权限。TD 的 current release、catalog 和扩容回执以其正式
  独立任务 handoff 为准，不在 TA 文档中硬编码成永久当前值。
- `cn.dataset.rt_min` schema major 2 先以10只股票证明相邻bar连续性，随后
  TradingDatas release `d0edb51…` 完成30只主板的真实
  provider→SQLite receipt→正式18082 `30/30` 回读，元数据为
  `ready/success/fresh/valid/non-degraded`。上游约晚一个完整5分钟K线，不能
  宣称 `bar_end -> observed_at <= 30s`，且数据不含 L1/bid-ask。
- 2026-07-28 15:05自动采集轮已把最终15:00 bar落库；TA身份经正式18082精确
  查询为30/30、ready/success/fresh/valid/non-degraded，receipt与lineage完整。
  这证明当前30只链路完成当日自动收盘，不代表500只已在生产采集。
- 500只TA消费变更已通过全仓`3792 passed`与GitHub前后端CI，并普通合并为
  `65ee4f012fc673b0680d63a5a87195b1c7061adb`；服务器现役release对500行预算
  smoke PASS。TradingDatas的500候选固定为5个100只分片，只有同一bar五片全部
  成功且500个`(ts_code,time)`唯一完整时才可handoff；当前尚未取得连续两轮
  500/500正式证据，所以生产数据覆盖仍是30只。
- 当前首轮 delayed-paper 使用的 previous-close 来自上一完成交易日的
  `daily.close`；非停牌状态只对正式 `rt_min` 完成K线且成交量为正的30只成立，
  两类证据均以 row/envelope hash 绑定。TradingDatas 随后独立回读
  `stk_limit` 为 ready/fresh/valid，`suspend_d` 为带完整 receipt/lineage 的
  合法空、non-degraded/valid；它们是后续增强校验，不反向改写本轮已冻结参考
  证据。`adj_factor`、`sw_daily` 和其它 degraded 数据继续 fail closed。
- 专用运行身份是 `tradingagent:tradingagent`（UID/GID 987）。token 只从
  `/run/secrets/tradingagent/tradingdatas-read.token` 读取；parent 为
  `root:tradingagent 0710`，leaf 为 `tradingagent:tradingagent 0600` 的 regular
  single-link file。值和内容哈希没有进入代码、日志、消息、manifest 或回执。
- 26 个 active 均完成两次相同的 `limit=1` 首屏查询：
  3 ready、9 stale、14 unobserved；Evidence Gate 为 3 accept / 23 reject，
  query 合同失败为 0。12 项返回 `next_cursor`，因此本轮只证明目录、认证、
  默认请求、省略字段和 metadata fail-closed，不证明完整数据读取。
- 首屏验收时的 3 个 ready 是 `cn.market.trade_calendar`、
  `cn.equity.security_master`、`cn.dataset.index_classify`。此后
  `cn.equity.daily` 已对 `trade_date=20260724` 完成 5526 行采集并正式读回为
  ready/fresh；`cn.dataset.sw_daily` 同日上游返回 QuickSync `40101`
  permission-denied，继续 impaired/fail-closed。
- TradingDatas 随后补齐 `20260723`–`20260725` 日历，正式 18082 直接证明
  `20260724 is_open=1`。TA 用新鲜 decision time/state root 完整读取
  `trade_calendar + security_master + daily`，生成 3041 只沪深主板
  observation Universe，明确排除 2569 条不在权限/第一阶段范围或不满足数据门禁的
  个体。五项 committed evidence 和精确幂等重放均 PASS。
- `index_classify` 在本次重跑时返回 failed/degraded，作为 optional context 被从
  核心 manifest 移除；`context_probe_roles=[]`，不能据此做行业宽度或行业选股。
- `current_observation_snapshot_emitted=true`、
  `delayed_fixture_simulation_started=true`、`automatic_scheduler_started=true`、
  `automatic_simulation_successful_round=false`、`REAL_TRADING_ENABLED=false`。
- 2026-07-28 服务器以专用 UID、正式18082、不可变代码 release
  `ac828bf5…` 手工处理 `13:10` 至 `13:45` 八个精确30只分钟快照；每次均
  `30/30`、metadata ready/fresh/valid/non-degraded、audit rejection 为0。
  首根建立滚动基线，后续每根形成30个特征/候选；四个50,000 CNY隔离账本均
  对账通过。两笔历史候选在不可回看的K线上诚实记为 `not_filled`；13:45 快照
  随后以决策后第一根真正可达K线完成 `002436.SZ` 模拟买入：baseline 100股、
  dynamic-position 200股，成交价32.15 CNY，费用分别5.03215和5.0643 CNY。
  两账本各形成1个T+1持仓并继续保留下一候选挂单；同一13:45快照重放退出2且
  bundle SHA不变。状态 bundle 为 `non_production_fixture`、0600、可重启且无
  资本/执行 authority。
- 自动累计与会话初始化候选已普通提交并同步 GitHub main；当前代码锚点
  `437fa27…` 的本地候选测试为 `2511 passed`，服务器不可变 release 上本轮
  定向测试为 `20 passed`。
  `/etc/tradingagent/ashare-minute-paper.env`、tracked service/timer 与
  `current` 指针已安装并通过 `systemd-analyze verify`，环境仍固定
  `REAL_TRADING_ENABLED=false`。分钟 timer 与次日09:20会话 timer 当前均为
  `enabled/active`。14:25:44 首次自动分钟触发因手工状态停在13:45、13:50形成
  真实缺口而退出2；timer继续排定后续轮次，日志只记录fail-closed。正式 bundle
  SHA保持
  `2812f88d34d52df67ed2275f439f1a9904e5c1165a1988b2e3964edbf7511130`，
  系统没有回填、跳过或改写时间。TD中途发布参考数据后catalog由本会话冻结的
  `v1-d27aa31fbfb60a3c`变为`v1-541b1314702f4897`；已开始的会话不原地换约。
  使用当前catalog执行的TA受限只读查询分别对14:30、14:35、14:40、14:45与
  14:50精确bar完成双跑，五次均为30/30、
  ready/fresh/valid/non-degraded、receipt与lineage完整且
  same-replay一致。因此行情数据仍在累计，正式fixture账本不推进是连续性与
  冻结合同门禁，而不是把当天缺口或catalog漂移静默洗白。
- 现役分钟timer已把旧日历表达式收敛为每交易日精确48次：
  `09:40–11:35`与`13:10–15:05`。服务器展开验证为48个不重复触发点，安装后
  下一触发为`2026-07-29 09:40 CST`；已知缺口仍只保留失败关闭证据且不做历史
  补单，午休后段和收盘后不再重复空转。
- 2026-07-28 17:46 CST 对运行态复核发现旧 release `d6f5f2c…` 的 timer
  仍在15:05后触发至15:55，且缺少盘后只读日报模块；因此以对应改动的 CI
  `test/front` 双 PASS 和本地17项聚焦测试为门禁，普通合并并原子切换到
  root-owned immutable release `b4f5d600f3d8bb317375a05b2f613e8a06e89c52`。
  服务器 unit 与该 release 字节一致，收盘段只保留显式`15:00:40`和
  `15:05:40`，分钟与会话 timer 均为`enabled/active`，下一次分别为
  `2026-07-29 09:40`和`09:20`；`Ashare/minute_day_report.py`已进入现役
  release，环境仍为`REAL_TRADING_ENABLED=false`。切换前后全部既有
  `state-bundle.json`哈希一致，旧 release 与旧 unit 保留为回滚证据；尚未把
  明日正式09:20初始化、首个成功自动轮或任何盈利结果标为完成。
- TradingDatas 已修正 `20260729` SSE日历
  (`is_open=1/pretrade_date=20260728`) 的envelope水位：未来适用日只保留在row，
  `data_through=observed_at`为实际观察时刻。发布切换瞬间TA initializer曾一次
  `minute_session_catalog_http_failed`，5秒后重跑已通过catalog与calendar，
  当时只在`minute_session_daily_universe_incomplete`失败关闭；未创建
  `20260729`目录，既有bundle不变。
- TradingDatas 16:30盘后通用采集于16:30:25成功结束，`cn.equity.daily`与
  `cn.dataset.rt_min`均写入success receipt。TA UID987随后经正式18082对
  `trade_date=20260728`和30只审核Universe执行有界双跑：返回30/30，
  `state=ready/degraded=false/freshness=fresh/quality=valid`，lineage完整，
  receipt、`data_through`和`observed_at`非空，且无游标。使用现役
  `d6f5f2c…`在独立evidence root初始化`20260729`时首次PASS、重放
  `reused=true`，Universe SHA为
  `0e26f54fc2ab391f0187a5787f9955b90e8a2ff21969957565749b733e035203`。
  隔离目录仅生成三项0600输入；无`state-bundle`、资本、订单或执行authority，
  正式`/var/lib/tradingagent/ashare-minute-paper/20260729`仍不存在。一次辅助
  摘要脚本因误读非合同字段`runtime_state`失败，修正后只读取冻结的
  QueryMetadata字段并通过；没有掩盖失败或修改运行态。明早09:20 timer的实际
  正式初始化、首根连续分钟bar和首个成功自动模拟轮仍需独立读回。
- 动态 builder 服务器只读运行时，calendar 与 security-master 为
  `runtime_state=success/degraded=false`；daily 为
  `runtime_state=stale/degraded=true`。因此返回
  `core_dataset_evidence_rejected:cn.equity.daily`、退出码 2，且没有创建或更新
  manifest root。这是数据新鲜度停止线，不是认证、目录或代码故障。
- 2026-07-27 使用当前权威 `main` release、专用 UID、正式 18082 和隔离
  manifest root 再次执行 builder，仍以同一 reason code 退出 2，且隔离 root
  为空。TradingDatas 随后确认 `20260724` 的 5526 行 daily 和成功 receipt
  实际存在，但当前通用 freshness 投影用周五分区零点直接比较周末墙上时钟，
  触发 259200 秒 SLA，因此元数据仍诚实保持
  `state=stale/runtime_state=stale/degraded=true`。TA 不覆盖该状态，等待
  TradingDatas 修正交易会话感知的 freshness 合同。
- TradingDatas 在 immutable release
  `98fa9489c4c8e960d392487c99b06d59e3db8f76` 修正盘后日频 freshness 投影后，
  TA 受限身份实际读回 daily 为
  `ready/success/fresh/valid/degraded=false`。builder 随即发布
  `manifest_sha256=7e5bdc5dd75cc4cd33a1a1bb80b66645c34cd2e4ef4cee08612e26e2bdf09d1f`，
  session 为 `20260724`，且仍明确
  `historical_pit_eligible=false/execution_authority=false/simulation_started=false`。
- 隔离 observation one-shot 生成 3041 只沪深主板 observation Universe，排除
  2569 条不符合第一阶段权限、标的或数据门禁的个体；首次运行与同 root 重放的
  snapshot、Universe、ledger、receipt 和 transaction-complete SHA 全部一致，
  `idempotent_replay` 从 `false` 变为 `true`。该结果是
  `observation_only`，不是 candidate、TargetPosition、PaperFill 或账户变更。

正式通过证据：

- 代码 release：
  `/opt/investment/release-evidence/tradingagent/20260726T100807Z-ta-catalog26-code-7cec341`
- 26-active 读回：
  `/opt/investment/release-evidence/tradingagent/20260726T100914Z-ta-catalog26-readback-7cec341`
- 读回状态文件：
  `/var/lib/tradingagent/ashare-observation/catalog26-v1-c19a22c011fc363e.json`
  （`tradingagent:tradingagent 0600`）
- 当前会话代码、失败关闭与最终 PASS 证据：
  `/opt/investment/release-evidence/tradingagent/20260726T105403Z-ta-current-session-6db813c`
- 详细读回报告：
  [docs/reports/2026-07-26-ashare-current-session-readback.md](docs/reports/2026-07-26-ashare-current-session-readback.md)
- runtime/front 退役证据：
  `/opt/investment/release-evidence/tradingagent/20260726T114404Z-ta-runtime-retirement-eb2e18a`
  与
  `/opt/investment/release-evidence/tradingagent/20260726T114546Z-ta-front-base-forwardfix-eb2e18a`
- runtime/front 详细报告：
  [docs/reports/2026-07-26-ashare-runtime-retirement-readback.md](docs/reports/2026-07-26-ashare-runtime-retirement-readback.md)
- 动态 catalog/manifest builder 服务器证据：
  `/opt/investment/release-evidence/tradingagent/20260727T085600Z-ta-ashare-manifest-94fcdf7`
- 动态 builder 详细报告：
  [docs/reports/2026-07-27-ashare-dynamic-manifest-readback.md](docs/reports/2026-07-27-ashare-dynamic-manifest-readback.md)
- 99-active 增量目录读回证据：
  `/opt/investment/release-evidence/tradingagent/20260727T092955Z-ta-catalog99-94fcdf7`
- 99-active 增量报告：
  [docs/reports/2026-07-27-tradingdatas-catalog99-readback.md](docs/reports/2026-07-27-tradingdatas-catalog99-readback.md)
- 当前 `main` release、动态 builder 重跑和 worker 安装预检：
  [docs/reports/2026-07-27-ashare-worker-preflight.md](docs/reports/2026-07-27-ashare-worker-preflight.md)
- freshness 修正后的 manifest 与 observation one-shot：
  [docs/reports/2026-07-27-ashare-observation-pass.md](docs/reports/2026-07-27-ashare-observation-pass.md)
- 同一 catalog 的发布侧 fresh consumer parity 以 UID 987 和既有 TA read scope
  对 99 个 active dataset 逐项执行 `POST /v1/query limit=1`、省略 `as_of`：
  99/99 HTTP 200、0 query-contract failure、79 nonempty、20 legal empty；
  envelope metadata 为 3 ready、92 partial、4 stale。该证据只证明固定 API
  可达和 metadata parity，不是完整分页、研究资格、历史 PIT 或执行 authority。

本地主线与远端主线一致性必须在每次交付时重新执行
`git rev-parse HEAD origin/main`；顶部提交号只标记本轮证据，后续提交会自然作废。

## 六层事实

| 层级 | 当前事实 | 不能据此推断 |
|---|---|---|
| 本地主线 | `main` 已含 provider-neutral client、分页/证据门禁、动态 manifest builder 和 0710 secret parent 安全遍历 | 代码存在不等于服务器已激活 |
| GitHub 主线 | A股 delayed-paper、500只消费容量、A股离线学习候选和 Crypto 解耦核心均已普通合并，GitHub CI通过 | CI 不等于500只真实覆盖、A股学习已启用或24小时 Crypto稳定性已验收 |
| 服务器代码 | `e8ba46d…` 已成为 current，`b4f5d60…` 保留为直接回滚；A股现役入口字节未变 | current 只指TA simulation-only入口，不等于broker或真实交易 |
| 服务身份 | UID/GID 987；18082 与18083分别使用独立专用 token-file，权威TA transport认证可用 | token 可读不等于任一 dataset 可用 |
| 数据验收 | A股三核心日频 observation/重放 PASS，`rt_min` 有30只真实30/30回读；Crypto 四个冻结 dataset 已由正式18083提供 ready/fresh/valid/non-degraded 数据 | A股500只尚未正式发布/回读；Crypto历史回填不是历史PIT；两者均不是L1或低延迟执行证明 |
| 交易能力 | front inactive/disabled 且 runtime-masked，8787 closed；A股分钟/会话 timer 保持 enabled/active；Crypto one-shot、幂等重放与两个相邻自动轮次 PASS，Crypto timer enabled/active；无 broker 或真实交易 | 自动 sim-only 闭环不等于24小时稳定性、模型有效性、durable execution authority或真实执行 |

旧 `8082` listener 仍由旧系统所有者保留，当前 observation consumer 没有探测或
fallback 到该端口。legacy front drop-in 已移出 active systemd 目录，front base
unit 已与当前仓库字节一致；active unit 中旧 `8082`、`SharedSignals`、
`/opt/tradingagent` 和 `marketgraph` 身份引用均为零。TradingDatas owner 已
启用其自身通用5分钟 collector timer；TradingAgent 只读消费 18082，只启用
simulation-only分钟/会话timer，不负责修改 TradingDatas 采集调度。

## A股第一阶段边界

1. **个股范围**：只分析沪深主板普通 A股；创业板、科创板和北交所个股因当前
   账户权限边界不进入个股研究、候选或模拟交易。
2. **环境参考**：上述板块的指数、行业分类和汇总统计可以作为市场环境输入，
   但必须标为 `context_only`，不得反向把无权限个股加入 Universe。
3. **行业起点**：先深挖少数高活跃产业，采用动态研究池，而不是永久概念股票池。
   第一批可研究 AI 算力/半导体/数据中心、机器人/工业自动化、创新药；观察池可放
   商业航天、有色/能源/电网。研究优先级不是买入建议。
4. **小资金优势**：50,000 CNY 只用于 simulation authority。系统允许现金胜出、
   少量高质量机会、no-trade band、整数 100 股、最低经济订单、低容量机会和
   试探—确认—扩仓；不以交易次数或每月 20% 作为强制生产约束。
5. **多风格**：产业趋势、事件/预期差、跨市场错配和现金状态逻辑上独立，
   资金统一组合、订单统一净额；仓库 fixture 已能对主板样本生成未经校准的
   个股排序，并分别运行 baseline/event/flow/dynamic-position 反事实账本。
   这些分数、账本和成交均不可冒充概率、真实行业特征、生产策略或资金 authority。

## 当前架构边界

- **TradingDatas consumer**：只用 catalog/query。HTTP 200 不能覆盖
  stale/unobserved/degraded；source proof、cursor、page/row budget、identity 和
  same-observation 分别验证。未声明 public identity 的 21 个 active dataset 只能
  做 metadata accounting，不能成为研究或执行证据。
- **研究层**：provider-native rows 与 envelope metadata 分离。没有 first-seen 与
  revision authority 时标记 current observation，禁止伪造 PIT 或用于历史训练。
- **决策层**：市场、行业、个股、事件、资金、成本和不确定性输出结构化 evidence；
  `DecisionEvidence -> TargetPosition -> TradeIntent` 之间仍有组合与硬门禁。
- **执行层**：A股、CNFutures、Crypto 保持独立 adapter/account/ledger。当前无
  broker adapter 激活、无真实账户、无订单、成交或资金副作用。
- **LLM**：DeepSeek 只作为 evidence sidecar，用于公告/新闻结构化、产业关系抽取、
  历史事件检索和报告；不能生成最终仓位或绕过确定性校验。
- **自我进化**：Decision Ledger、counterfactual、Champion–Challenger 和漂移/
  校准监控只允许提出候选或自动收紧风险；自动晋级、恢复或扩大风险不在当前权限内。

## 兼容与退役

- `current-v1` 只指 provider-neutral catalog/query 消费链，不包含旧数据 reader。
- 仍有明确消费者的旧 A股路径保持 `active-compatibility` 或
  `retirement-pending`，只允许迁移和回归验证，不新增依赖。
- Mini/Hermes、旧直接执行和已清零的非核心市场入口保持 `hard-blocked`；
  仓库退役不自动证明其它主机的安装态已清理。

## 明确未完成

- 目前只积累 1 个 forward-collected 交易会话，未达到 21 个会话的 20 日特征最低
  覆盖；无正式历史 PIT/revision authority、标签成熟度、冻结 OOS 或
  60–120 交易日模拟样本。
- 申万日线仍 permission-denied，`index_classify` 本次为 failed/degraded；核心
  observation 没有行业上下文，不能冒充行业宽度、行业排名或产业研究输入。
- 当前日频 snapshot 只证明单次 current observation；`rt_min` 当前只证明
  30只主板的5分钟 OHLCV/amount 读取和既有小批连续性。二者都不是可训练历史
  PIT，也没有绑定 durable capital/outbox 或 TA 生产 worker。
- `rt_min` 约晚一根完整K线且不含 bid/ask；当前只用于数据与非生产模拟样本
  积累。保守下一可达K线策略已在手工 one-shot 中运行；自动 timer 已启用，但
  当日中途缺口使首轮自动触发失败关闭且不改账本。下一交易日输入已完成隔离
  30/30初始化与幂等重放，但正式状态仍必须由09:20 initializer冻结，再从首根
  连续K线启动。durable capital 或更高权限仍需独立门禁；连续5个交易日用于
  稳定性与扩容复核，不再阻塞首批模拟决策。
- front 继续停止；本阶段不恢复 `tradingagent.cc` 页面。tracked base unit 已安装
  但保持 inactive/disabled/runtime-masked，旧 drop-in 已退役。分钟累计已有
  独立 current pointer，simulation-only分钟/会话timer已激活；observation worker、
  durable capital 与真实执行均未激活。
- 专用 UID 987 已通过新的 root-owned versioned Python runtime 执行真实入口和
  audit；旧 `/opt/tradingagent/venv` 不再被 TA active unit 引用。动态 manifest
  rollover 已完成失败关闭和恢复后 PASS 验收，手工 one-shot 与幂等重放已完成；
  分钟自动累计的 tracked unit、secret-free env 与 `current` 指针已安装并启用。
  首次自动触发已证明缺口下失败关闭且bundle不变；真实次日pre-open初始化、首个成功
  自动轮次、崩溃恢复与连续运行验收仍未完成。次日隔离预检已证明30只参考输入
  可生成且精确重放，但它不替代09:20正式timer读回。
- 旧 8082、旧服务器 runtime 和退役文档只能按各自 ownership 与证据链清理；
  不以删除代替依赖清零证明。

## 下一阶段入口

依赖顺序固定为：

1. 保持 TradingDatas 30只主板/5MIN collector 连续运行，核验午休、下午恢复、
   全天48个bar slot的完整率、重复/冲突和失败receipt；首次真正可达K线模拟成交、
   资金/持仓对账和重复快照失败关闭已完成，继续积累当日手工样本；
2. 用现有通用采集链刷新 `daily`、`stk_limit`、`suspend_d`、`adj_factor`；
   数据集逐项 fresh/non-degraded 前不进入自动模拟成交；
3. TradingDatas 以100只/分片完成动态500只 `coverage_canary` 的500/500正式
   回读；500只不能按证券代码前缀偏置抽样，也不能冒充中证500、研究代表性样本或
   交易Universe。500稳定后扩到全部合格沪深主板，仍不新增公共route；
4. 等 `index_classify`/`sw_daily` 恢复健康后，独立加入行业上下文，不阻断核心
   主板 observation，也不把汇总数据冒充个股权限；
5. 当前真实 delayed-paper one-shot 已完成首次可达K线结算与重复快照不变复核；
   专用 TA 分钟 timer 与次日会话初始化 timer 已安装并启用。下一停止线是在下一
   交易日09:20核验正式目录与隔离预检一致，并从09:35首根数据到达后的09:40
   调度开始验证首轮、下一轮、重复轮、崩溃恢复和盘后
   停止；缺参考快照、分钟缺口或任何数据退化时继续失败关闭。连续5个交易日是
   稳定性/扩容复核门，不再阻塞首批模拟决策。
   durable capital 或更高权限的自动 paper
   仍须完整数据、日历、执行与资本权威通过后开放
   `Signal -> TargetPosition -> Risk -> PaperFill -> Reconcile -> Attribution`
   闭环；
6. 只有长期样本、校准和回撤门禁通过后，才讨论模型晋级；真实交易继续保持关闭。
