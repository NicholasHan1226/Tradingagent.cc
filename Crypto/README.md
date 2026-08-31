# Crypto fixture auto-simulation slice

本目录当前提供一个网络关闭、simulation-only 的 Crypto 现货最小纵向切片。它用于验证工程闭环，不证明真实 TradingDatas、持续调度、Testnet、Live 或策略盈利。

## 本批实现

`fixture_auto_sim.py` 按以下顺序运行：

1. 在任何状态写入前确认 `REAL_TRADING_ENABLED=false`，并拒绝 Testnet、Live、密钥、SQLite、provider route、`/tushare` 和 `/source_status` 字段。
2. 读取显式 fixture/mock；fixture 只声明未来 wire contract 为 `GET /v1/catalog` 与 `POST /v1/query`，不填写或猜测 TradingDatas dataset ID。
3. 验证 UTC、连续、closed 的 5 分钟 bar；最后 12 根形成 1h regime，最后 3 根形成 15m decision。成交必须使用 decision 已观察之后的 `next_executable_quote`，按 ask 加冻结 2 bps 滑点，不再使用同根 K 线收盘价。
4. 使用冻结的 `crypto-spot-15m-momentum-candidate-v1`。它只能是人工复核候选，不能自动晋级、扩风险或切换真实交易。
5. 从 [capital_policy.py](capital_policy.py) 的单一 `crypto-capital-v1` 本地 opening baseline 开立 10,000 USDT simulated fixture 账户；它不授予 execution/durable/production authority，`config.yaml` 不重复本金数值。
6. 用 Decimal 校验 price tick、quantity step、min quantity、min notional 和费用，生成不可变 OrderIntent 与本地 paper receipt。
7. 将 opening、reserve、fixture-simulated fill、reconcile 依次写入 Crypto 自有 append-only checksum chain；每次追加使用预期 head checksum，并保持账户全局 valuation watermark。若进程在账本落盘后、run bundle 写入前中断，可按确定性 reference 恢复而不重复扣款或增仓。
8. 输出余额、仓位、订单和权益对账，以及 `label_status=pending`、`promotion_authorized=false` 的样本复盘；回放同时校验 bundle hash、确定性 decision/order/receipt 和账本事件绑定。

实现拆分在 `fixture_sim/{contracts,evidence,ledger,replay,runtime}.py`，`fixture_auto_sim.py` 只保留兼容导出与 CLI。DeepSeek/LLM sidecar 单独追加到 `sidecars/llm_evidence.jsonl`，固定为 `authority=none`、`network_used=false`；同一根目录改变其文本不会改变或阻塞核心 bundle replay。

receipt 与 bundle 的状态固定为 `fixture_simulated`，并显式携带 `execution_eligible=false`、`execution_authority=false`、`durable_execution_receipt=false`、空 outbox/capital commit，以及 `durability_scope=local_fixture_fsync_only`。这些本地 fsync 文件不是 broker/outbox/生产资本回执。

本候选进一步要求 OrderIntent、receipt、bundle、capital snapshots/events、
sample review、safety 与 LLM sidecar 均显式携带
`production_eligible=false`。缺少该字段的旧本地 fixture artifact 会失败关闭；
它们应保留为只读历史证据，并使用新的输出根开始本候选，禁止原地补写或冒充迁移。

本地 fixture 当前只有 opening baseline generation 1，合同标记为 `local_fixture_opening_baseline_only`；它不是可长期写死的 current production generation。未来接入可轮换资本快照时，intent/receipt/replay 必须读取并传播当轮 current snapshot 的正整数 generation，本切片不得被当成该能力已经实现。

## 5分钟 delayed-paper mock-ready 切片

`five_minute_data.py` 新增 Crypto 自有的 provider-neutral data port。它没有
HTTP endpoint、token、Binance client、SQLite reader 或 fallback，只复用调用方
显式注入的 TradingDatas V1 typed client。当前 checked-in `config.yaml` 固定
`binding_scope=fixture_only`，不保存任何生产 dataset ID。

上游数据合同代码已合入 TradingDatas
`main@62d76f8cdcc7671a9523ac15905ab2eb3152e387`。isolated canary
`025fd24e2f9f33855b6d2f62ac6489d219033128`（catalog
`v1-e7ea3dd714066d3c`）提供本地查询证据，后续主集成已完成 18083、专用 token
leaf、仓外 profile 和核心自动轮的正式 readback。以下四个 ID 仍只能通过冻结
profile 和 catalog/query 消费，禁止由 TA 动态猜测、直连 Binance/SQLite 或
fallback：

- `crypto.spot.binance.btcusdt.5m`
- `crypto.spot.binance.ethusdt.5m`
- `crypto.spot.binance.btcusdt.rules`
- `crypto.spot.binance.ethusdt.rules`

bar identity 由 handoff 冻结为 `[symbol, open_time]`；默认字段为
`symbol/open_time/close_time/OHLC/volume/quote_volume/trade_count`。所有价格和
成交量均按 Decimal 字符串读取。source `close_time` 是 Binance inclusive
interval end（例如 `09:09:59.999Z`），本地只在它精确等于
`open_time + 5m - 1ms` 后派生逻辑 `09:10:00Z`。上游没有 `closed` 或
`frequency` row 字段；closed 资格来自 dataset contract、as-of 查询、receipt
水位和消费者逐根校验。rules 仅在 `status=TRADING` 且 tick/step/minQty/
minNotional、base/quote asset 全部有效时接受。

每个 dataset 均独立执行两次 bounded catalog/query 读取并绑定 receipt、
lineage、freshness、quality、分页 trace 与 same-observation。bar stop-line A
固定为 `symbol eq + open_time between [latest_open-60m, latest_open] + as_of +
10 fields + symbol:asc/open_time:desc + limit=13`；rules 使用
`symbol/status eq`、无 `as_of`。上述 isolated canary 已本地证明 BTC/ETH
均返回精确连续 13 根且 terminal cursor 为空。consumer 同时保留通用 bounded
cursor traversal；non-null cursor 本身不是错误，只有循环 cursor、跨页重复、
页/行预算超限或最终窗口缺失才失败关闭。正式 HTTP handoff 前仍只使用 fixture。
runner 接收 typed snapshot 时会再次核对 profile hash、四份
symbol/kind/dataset/catalog 绑定、请求窗口、cutoff、page/row budget、proof
freshness 与价格 tick；重算 market/observation digest 不能绕过这些门禁。

## 十币种数据健康观测（零资金权限）

`market_observation.py` 是与 BTC/ETH delayed-paper 资本链完全分离的只读
观测器。它只消费正式 TradingDatas `GET /v1/catalog` 与 `POST /v1/query`，对
BTC、ETH、SOL、XRP、BNB、DOGE、ADA、TRX、LINK、AVAX 各自固定查询 13 根已完成
5 分钟 bar，并验证 catalog、identity、连续 UTC 时间、OHLCV、terminal pagination、
ready/fresh/valid/non-degraded 元数据以及 receipt/lineage。输出只包含摘要与哈希，
固定 `authority=none`、零 capital/order/model/promotion 权限；它不改变既有
BTC/ETH profile、G5 epoch、timer、账本或模拟交易范围。首个 server one-shot/replay
验证通过前，不安装独立定时器。该观测是 current-health read，查询明确省略 `as_of`；
它不能成为历史 PIT、训练或资本证据。每次输出同时保留完整的
`observation_sha256`（含 receipt/watermark）与 `market_data_sha256`（只绑定
catalog、窗口、行顺序与 identity）：当前采集 receipt 在两次读取间推进时，前者可
诚实变化；只有后者相同才表示同一 13 根市场行的可重放一致，绝不把 receipt 漂移
误报为行情漂移。

`delayed_paper_runner.py` 的顺序为：

1. 两个 symbol 必须先全部通过数据、counterfactual 与 fixture 资格预检；任一
   失败只幂等追加 `data_reject`，资本目录不创建或保持字节不变。
2. 数据合格后先将完整 observation 与四份 source proof 写入 Crypto 本地
   audit store，再进入资本链；外层 cycle lock 串行化
   `pending→query→accept→execute→complete`，重启先恢复唯一 pending，
   不重新取数，也不允许第二个未完成 observation。
3. 每个 symbol 的前 12 根 bar 进入既有冻结 Champion；第 13 根 bar 的
   high/low/close/volume 不参与决策，只把 open 作为
   `next_closed_bar_open_counterfactual`。可用水位取 bar close、bar
   `observed_at` 与四份 source proof `observed_at` 的最大值；资本 quote 时点
   只可等于或晚于该水位，绝不回填到 bar close 之前。
4. 本地使用 1 bp/side、按 tick 取整的明确 spread model，再由既有 fixture
   simulator 应用冻结 2 bps slippage 和 10 bps taker fee。它不是 L1 quote 或
   broker fill。
5. BTC、ETH 按确定顺序调用现有 `run_fixture_auto_sim`；该函数仍是唯一资本
   writer。两份已资格化 fixture 会先组成同一 slot、绑定 receipt 与 evidence
   digest 的 account valuation context；每个 core cycle 都携带两资产 mark，
   因此停机跳过多根 bar 后也不会被另一持仓的旧 mark 卡死。已有持仓再次触发
   buy 时，核心写入无订单的 mark-only claim/reconcile，更新两资产估值水位
   但不增仓、不扣现金；外层只记录 `risk_reject`，不另建资本写入口。
6. 相同 observation 重放不重复成交；同一 global slot 的不同 payload 在调用
   资本链前失败关闭。所有外层 observation/completion/event 都固定
   `execution_authority=false`、`production_eligible=false`、空
   outbox/capital commit。
7. Decision Ledger 使用 checksum 连续序列与 16 MiB/段的原子 rotation；
   current 文件写入中断不会发布 partial tail，恢复会从已 fsync 的历史段继续，
   不会因单文件到达上限永久阻塞 pending。

## 本地运行

从 TradingAgent 仓库根目录执行：

```bash
export REAL_TRADING_ENABLED=false
python3 -m Crypto.fixture_auto_sim \
  --fixture Crypto/fixtures/auto_sim_spot_cycle_v1.json \
  --output-root /tmp/tradingagent-crypto-fixture
```

输出根包含：

- `capital/events.jsonl`：append-only 资本事件；
- `capital/head.json`：当前 sequence/checksum；
- `runs/<run_id>.json`：可重建的业务 bundle、OrderIntent、paper receipt、对账与样本复盘。
- `sidecars/llm_evidence.jsonl`：与核心 replay 分离的离线、无权威 LLM journal。

相同 fixture、Champion 和资本 generation 产生相同 run/order/receipt ID；同一输出根再次运行只读回已完成 bundle，不追加第二次 fill。

## Server runtime 与 systemd 候选

`delayed_paper_runtime.py` 是最小 loopback-only server CLI。主集成已在
2026-07-28 以 release
`e8ba46d7e0cab847d0fa037290e7368c69c54655` 完成 one-shot、幂等重放、相邻
自动轮和核心 timer enabled/active 的 sim-only 验证。实际运行仍必须由主任务
提供仓外、secret-free 的
`/etc/tradingagent/crypto-delayed-paper.runtime.json`；manifest 必须包含：

- `base_url`：loopback IP literal 的 TradingDatas authority；
- `catalog_version`、完整 `CryptoFiveMinuteDataProfile.to_payload()` 与
  `profile_sha256`；
- profile 内四个 dataset 的 schema、字段、filter/order、分页预算和
  `catalog_contract_sha256`；该 hash 直接调用 shared 的 canonical dataset
  fingerprint，只绑定 TradingDatas 的七项公开合同字段，不受 availability、
  queryability 或其它运行元数据影响。Crypto 自己的 selected fields、order、
  filters、identity 与页预算另由独立 `consumer_profile_sha256` 和外层
  `profile_sha256` 绑定；两层任一漂移都 fail closed。冻结的 catalog version
  和 query receipt 的 observed catalog version 都保留为 evidence，但单独的无关
  catalog version 变化不阻断四个目标 dataset；目标合同 hash、缺行或重复行仍失败关闭；
- 全部为 false 的 real/Testnet/Live/model-network/自动晋级/自动扩风险安全项。

runtime 不在本地动态发明 dataset ID，也不会根据新鲜 catalog 重新生成 profile。
manifest 在读 token 或创建 socket 前完成绝对仓外路径、regular/single-link、
owner/mode、重复 JSON key、读取中变更与 profile SHA 校验。所有 HTTP/HTTPS
base URL 均必须是 loopback IP literal；最终 transport 仍复用共享可信 token-file
边界，只允许：

- `GET /v1/catalog`
- `POST /v1/query`

token 与 current epoch root 均不可由 CLI 任意改写：

- `/run/secrets/tradingagent/tradingdatas-crypto-read.token`
- `/etc/tradingagent/crypto-delayed-paper.epoch.json` 精确绑定的
  `/var/lib/tradingagent/crypto-delayed-paper-epochs/<epoch_id>`

旧 `/var/lib/tradingagent/crypto-delayed-paper` 只作只读 archive，不再是 core
CLI 的合法输出根。

每个 slot 使用固定的 `bar close + 55s` observation cutoff；systemd jitter 或同
slot 重跑不会改变请求身份。runner 先检查并恢复 pending observation，只有没有
待恢复资本步骤且确实需要 fresh snapshot 时，才懒读取 token 并构造 HTTP client。
若崩溃
恢复跨过时槽，同一 invocation 最多执行两个连续 cycle：`pending + 下一缺失
window`，或两个连续缺失 window；仍有积压时显式 `backlog_pending`、返回非零并
由下一轮从最早缺口继续，不会直接跳到最新时槽。恢复项的旧 slot/profile provenance
与当前 manifest 的 fresh-query catalog/profile 分开报告，实际 transport 计数为
0 时不会声称使用了网络。

首轮不足 13 根或数据不合格只追加幂等 `data_reject`，不创建或改变资本；只有
账户从未出现 observation/completion/capital/decision evidence 时的第一次正常
`crypto_5m_window_incomplete` warm-up 返回成功，同一或后续持续缺窗返回非零。
401、catalog 漂移、degraded/stale/invalid metadata 同样不 fallback。资本或账本
损坏统一脱敏失败，不向 systemd stderr 暴露 traceback、路径或载荷。

核心 runtime 与学习完全解耦：它不 import、调用或恢复 learning，不读取或创建
`evolution/`，也不会因学习失败改变核心 status 或 exit code。每份 runtime
回执固定输出 `learning_mode=detached_offline_worker`、
`learning_authority=false`、`learning_invoked=false`；这些字段只声明边界，
不表示离线学习候选已部署或运行。

候选命令形态为：

```bash
export REAL_TRADING_ENABLED=false
python3 -m Crypto.delayed_paper_epoch_runtime \
  --epoch-manifest /etc/tradingagent/crypto-delayed-paper.epoch.json \
  --runtime-manifest /etc/tradingagent/crypto-delayed-paper.runtime.json \
  --token-file /run/secrets/tradingagent/tradingdatas-crypto-read.token
```

核心 unit 为：

- `Crypto/systemd/tradingagent-crypto-delayed-paper.service`
- `Crypto/systemd/tradingagent-crypto-delayed-paper.timer`
- `Crypto/systemd/tradingagent-crypto-read-token.tmpfiles.conf`

服务器重启后，Crypto TA read token 必须由上述 tmpfiles 规则从发布侧已经原子
安装的 root-owned canonical source
`/etc/tradingagent/tradingdatas-crypto-read.token` 重建到
`/run/secrets/tradingagent/tradingdatas-crypto-read.token`。规则只复制既有
publisher-provisioned credential，不生成、注册、读取或输出 token；source
缺失、owner/mode 不合格或 runtime leaf 不是
`tradingagent:tradingagent 0600` regular single-link file 时，核心 service
继续 fail closed。发布侧只能对该精确 tmpfiles 文件执行 scoped create，禁止
无参运行全局 `systemd-tmpfiles`。

timer 为 24×7、每 5 分钟边界后 55–58 秒触发，`Persistent=false`，包含
`[Install]`/`WantedBy=timers.target`；实际 enable/active 状态只以服务器读回为
准。2026-07-28 的正式上游 handoff 已给出 loopback
`http://127.0.0.1:18083`、catalog `v1-e7ea3dd714066d3c`、四个
schema-major-1 dataset 的 ready/fresh/valid/non-degraded query 证据，以及
`/run/secrets/tradingagent/tradingdatas-crypto-read.token` 为
`tradingagent:0600`。核心 unit 的安装态与这些上游输入仍是两份独立证据；本轮
learning 候选不读取或修改 manifest、token、18083、核心 unit 或 timer。

## 学习解耦边界

`delayed_paper_learning_worker.py` 是现有 G5 detached learning/scrub units 使用的
独立离线 worker；核心 runtime 不 import、调用或恢复它，也不读取或创建 `evolution/`。
学习 worker 只异步消费核心已完成的
append-only observation/completion/decision/capital 证据，所有输出固定
`learning_mode=detached_offline_worker`、`learning_authority=false`、
`execution_authority=false`、`production_eligible=false`。

worker 不接受自由 `--output-root`。它只接受固定
`--epoch-manifest /etc/tradingagent/crypto-delayed-paper.epoch.json`，复用核心
epoch loader 并以只读共享锁验证 manifest、`.current_epoch.json`、
`.current_epoch.lock` 和 `.epoch_identity.json`，再将唯一
`context.output_root` 交给学习函数；运行结束前再次验证同一 context。tracked
service 同时静态钉住现役
`crypto-delayed-paper-epoch-g2-20260729`，只给该 root 的 `evolution/` 写权限，
其余 epoch parent、资本、订单、运行与决策账本均只读。manifest/current/root
冲突立即失败关闭；未来切换 epoch 必须人工更新并复核 unit，不扫描 `latest`、
不自动换 generation、不回退旧 root。

增量模式只读取核心当前 completion state、自己的 checkpoint 头，以及有界的新增
completion 对应的 receipt/segments；每轮最多处理 8 条，并受 30 秒内部预算约束，
在 45 秒 oneshot timeout 前保留明确的退出余量。增量路径只重验已验证 checkpoint 链的
精确头部映射后追加 suffix；全部历史投影的逐条复核仍由 daily full scrub 独立承担。
它按 append-only、可恢复顺序写入 projection receipt、checkpoint 和
`worker_state.json`，返回 `projected`、`backlog_remaining` 或 `current` 及处理/剩余
数量；首轮没有既有 baseline 时仍返回 `full_scrub_required`，不会绕过全量完整性边界。
预算中断或 backlog 会在下一轮从下一个序号继续，checkpoint 已落盘而 state 尚未更新的崩溃可确定性恢复。

每日 full scrub 独立遍历全部
`completion → projection receipt → sample/KPI/Challenger segments` 和完整
checkpoint checksum 链。没有 checkpoint 声明的缺失投影可从核心权威证据补齐；
一旦 checkpoint 已声明成功，较早 receipt 缺失、旧 segment 篡改、completion
绑定不一致或 checkpoint 断链都必须失败关闭，不能用较新的成功覆盖。
Challenger 只追加建议；独立 outcome 不足时记录
`deterministic_non_live_gate_pending` 并继续积累，不等待逐候选人工复核。该投影
仍不得自动替换 Champion、扩大风险、修改资本/仓位/订单或进入 Testnet/Live，也不
调用网络模型。

tracked 候选包含：

- `tradingagent-crypto-delayed-paper-learning.service/.timer`：每根核心 5 分钟
  轮次之后独立运行增量 worker；
- `tradingagent-crypto-delayed-paper-learning-scrub.service/.timer`：每日独立
  full scrub。

两组 learning timer 的仓库安装默认值均为 disabled；该默认值不代表现役生产状态。
发布侧先安装 unit、创建现役 epoch 的 `evolution/`，在 disabled 状态完成同根 full
scrub、幂等 replay、unit/root/rollback 核对，再由 Controller 可回退地 enable 并读取
自然增量。最新连续 288 根/24 小时只用于 runtime maturity 与后续
promotion/risk/execution，不阻断完整 segment 的离线因子/策略滚动评估。任何当前
timer、release、checkpoint 或样本计数必须从同轮 systemd/运行 readback 或带时间的
状态报告取得，不能从本 README 推断。学习失败不得改变核心 status、exit code、资本
或订单。

## 退出影子与健康快照

现役 capital generation 的订单、成交和资本事件合同仍只支持
`buy/observe`。为避免改坏已有 append-only 回放，
`delayed_paper_exit_shadow.py` 独立读取最新已完成 observation、不可变 run
bundle、当前 capital head 和已模拟买入回执，按冻结的 v1 规则计算：

- `+3%` 止盈；
- `-2%` 止损；
- 最长持有 `24h`；
- `observe` 且 1h/15m 动量同时转弱；
- 卖出侧 2bps 保守滑点及 0.1% 费用后的完整往返反事实。

结果按 observation 写入 `evolution/exit_shadow/`，固定
`counterfactual_only=true`、`authority=none`、无 outbox/capital commit。
相同 observation 幂等重放必须逐字节相同；源 completion、bundle 或 capital
head 冲突即失败关闭。该投影不产生模拟卖出，也不改变现金、持仓、订单或核心
exit code；它也不能成为下面 round-trip 候选的触发输入。

## Round-trip capital generation 候选

`round_trip_capital.py` 新建独立 `crypto-round-trip-capital-v1`、capital
generation 2 和 `crypto_sim_round_trip` 账户，不扩展或改写旧
`crypto-capital-v1` ledger。新账户单独从 10,000 USDT simulated baseline
开始，`aggregate_with_prior_generations=false`；旧 generation-2 epoch 的现金、
持仓、订单、费用、PnL 和收益率不读取、不迁移、不聚合。

`delayed_paper_round_trip.py` 复用现有 provider-neutral closed-5m
snapshot 校验以及 1h/15m 决策边界。首次无持仓时仍按冻结 Champion 生成模拟
买入；持仓存在时按以下 v1 规则独立重算退出：

- `+3%` 止盈；
- `-2%` 止损；
- 最长持有 `24h`；
- decision 为 `observe`，且 1h regime return、15m decision return 同时小于 0。

卖出 intent 使用下一根已完成 5m bar 的 quote，bid 侧再扣 2bps 并按 tick
向下取整；费用继续使用 0.1% taker fee。请求数量按 step 对齐，完整、部分和
exchange-minimum/模拟流动性拒绝回执均写入独立 checksum ledger。相同 cycle
与 fill-capacity 重放不重复订单或成交；冲突回报、篡改事件、滞后/缺失 head
和两币中途崩溃均失败关闭或确定性恢复。退出影子仍只是
`authority=none` 对照，不能写该资本链。

`delayed_paper_round_trip_epoch.py` 定义不激活的 epoch-g3 迁移候选及其显式
epoch-g4 继任路径。旧固定 manifest、g3 manifest/receipt 与 g3 root 都是只读
失败证据，不能原地改写或复用。发布侧必须用模块 CLI 在独立版本化 manifest
路径创建 g3 迁移：它一次性冻结唯一 epoch/root、旧 manifest digest、
迁移原因、旧 generation-2 identity 与当时的 authority head sequence/checksum，
并写入同样不可覆盖的 generation-3 supersession receipt。重复同一请求只读回；
同 generation 换 root、旧 manifest/receipt 篡改、g2 head 前进或回退均失败关闭。
prepare 只读校验旧 root、创建新 identity；不会写 current-epoch pointer，也没有
tracked service/timer。若 g3 冻结后 g2 合法前进或 g3 one-shot 未完成，唯一允许的
恢复是显式创建 g4：它绑定 g3 manifest/receipt digest，并冻结新的 g2 authority
head；旧 g3 证据保持逐字节不变。正式发布前必须另行停止并锁定旧 writer、核对旧
root 字节、安装对应 manifest、执行 one-shot/同槽重放/相邻轮验收，获准后才能切 timer。

版本化 manifest 目录由 root 创建为 `root:tradingagent 0750`，manifest 与 receipt
为 `root:tradingagent 0640`；它们不含 token。round-trip unit 不再读取旧固定
manifest，而只读取 root-owned `0640`
`/etc/tradingagent/crypto-delayed-paper-round-trip.env` 的唯一
`ROUND_TRIP_EPOCH_MANIFEST=/etc/tradingagent/crypto-delayed-paper-round-trip-epochs/<epoch>.json`
选择。该 env file 仅传路径，不能绕过 runtime 对版本化 manifest、supersession
receipt、旧 manifest digest 与冻结 g2 head 的校验。

本候选所有 order/receipt/snapshot 均保持
`REAL_TRADING_ENABLED=false`、`execution_authority=false`、
`production_eligible=false`，无 Binance/Testnet/Live、网络模型、outbox、
capital commit、自动 Champion 晋级或风险扩张。

## Factor Research v1（只读影子）

`factor_research.py` 是与核心和 learning worker 都分离的纯研究模块。它从一份
已验证、连续的 13 根 closed-5m OHLCV 窗口构建版本化特征快照：5m/15m/1h
收益、15m/1h 实现波动率、5m 标准化区间和 1h 成交量 z-score，并生成 BTC/ETH
相对强弱诊断。每份快照必须
绑定 observation、receipt、lineage、`observed_at` 与 `data_through`；缺 bar、
非连续窗口、非 UTC 时间、未来标签或绑定不一致都会失败关闭。

它只比较三项预注册的研究假设：时间序列动量、趋势内回调、量能突破。标签固定
为未来 1h/4h/12h/24h 的保守费用后收益，均使用已观察的未来价格；没有任何一项
结果会变成订单、仓位、Champion、风险预算或自动晋级。50 个标签只是初筛下限，
绝不构成策略 edge 或晋级授权。它已通过下述 manifest-bound subordinate path 接入，
同时保持 append-only、full-scrub、幂等与 core-root 隔离边界。

`delayed_paper_factor_research.py` 与其 manifest-bound worker 没有 standalone
authority/unit，但已作为现有 G5 round-trip learning/scrub units 的 subordinate path
部署：它按 shared readiness contract 的连续 segment 独立处理，向 runtime-provided
epoch root 下的 `evolution/factor_research/` 追加 snapshot、receipt、checkpoint 和随后
可用的未来价格标签；G4 只保留为历史证据，不是当前部署 root。其冻结 consumer profile 为
`crypto-5m-ohlcv-13bar-forward-labels-v1`：13 根 5m OHLCV 输入，且每个样本的
1h future label 必须在同一连续 segment 内完整、证据一致，才会计入
`label_learning_eligible`。4h/12h/24h 是继续生成且每日 scrub 的辅助归因，当前
研究假设不会消费它们；若未来成为必要输入，必须升级 profile 版本。gap 永远切断
segment；任何 feature/label 不能跨缺口拼接。最近连续 288 根仍只表示 automatic
runtime maturity，不阻断已完整 segment 的 detached offline projection，也不授权
learning timer、Champion、风险扩张或真实执行。routine incremental 只处理本轮新增
completion 并验证前一 projection receipt/checkpoint；没有新增 resolved label/outcome
时，它通过 compact evaluation checkpoint 确定性返回 `no_new_outcome`，不扫描完整
strategy inventory，也不改写既有 artifact。只有显式新增 resolved label 才评估本轮
outcome；历史 labels 及其 strategy evaluation 只由 daily full scrub 补齐，full scrub
同时重验完整 core completion、投影、标签和 checkpoint 链。篡改、缺失或不连续会
fail closed；受控失败只形成可重试 evaluation debt，不回滚已完成的 learning/factor
投影。所有 factor/strategy artifact 与 recommendation 都是 private shadow，固定没有
learning、promotion、risk、execution、production 或 live authority；该路径不能写
core、capital、order、Champion 或现有 `round_trip_learning/`，也不使用网络或模型。
50 条标签初筛不构成策略有效或自动晋级。

## 历史 G4/G5 安装候选（不代表现役状态）

以下段落保留旧 G4/G5 one-shot、安装和迁移候选的审计语境；它们不能替代
`STATUS.md`/`AUTODEV_STATE.json` 的当前 release、unit、timer 或 checkpoint readback。

`delayed_paper_round_trip_runtime.py` 是隔离 round-trip epoch 的唯一 closed-5m
server wrapper。
它仅复用已冻结的 TradingDatas manifest、token-file transport 与 13-bar 门禁，
每次先后校验 epoch identity 与旧 g2 archive。若 G5 的 `Persistent=false` timer
因主机停机漏过已闭合 slot，runtime 从 checkpoint 的最早缺口按顺序补处理，单次最多
两个 cycle；未追平时返回 receipt-bound `backlog_pending`（非零退出），下轮继续，
绝不跳到最新 slot。receipt 的 `recovery_mode`、`cycle_results` 与
`backlog_remaining` 区分正常轮、pending recovery 与 outage backlog recovery。
`tradingagent-crypto-round-trip-delayed-paper.service/.timer` 与不会改写 G3 选择
文件的 `tradingagent-crypto-round-trip-g4-delayed-paper.service/.timer` 都是独立候选；
仓库默认不启用，且旧 g2 root 始终为只读路径。只有发布侧完成 one-shot、同槽幂等、
相邻两轮、资本/持仓/订单守恒与 g2 字节不变读回，才可停止旧 writer 并启用该 timer。

`delayed_paper_health.py` 是 no-write 健康读侧，分别报告核心
observation/completion/pending、资本守恒与 head、退出影子是否追平，以及学习
checkpoint 是否追平。它只输出单市场 USDT 状态，不跨市场汇总资金，不拥有调度、
晋级或交易 authority。

`delayed_paper_round_trip_health.py` 是现役 g4 round-trip epoch 的独立只读
健康/KPI 读侧。它只接受版本化 epoch manifest，不接受自由 output root；在读取
前后都重验 epoch identity、旧 g2 archive 锚点、observation/completion 状态、
Decision Ledger checksum 连续性和 round-trip capital head。缺 lock、缺 state、
state mtime 漂移、pending、账本链/守恒异常都会失败关闭，绝不重建索引、创建 lock、
修复 head、查询 TradingDatas 或改变订单/资金。报告的样本指标仅是已完成
observation、验证 decision event、capital cycle 与 receipt 分布，不是收益、胜率或
策略晋级结论。序列化 payload 的 `failure_count` 仅统计同一次 checksum-verified
Decision Ledger 读取中的持久化 `data_reject` 事件；它不代表 journal-only runtime
failure，也不统计 `data_gap`、`risk_reject` 或普通 `decision` 事件。

`tradingagent-crypto-round-trip-g4-health.service/.timer` 是该读侧的 tracked
候选：每 15 分钟运行一次，默认不启用、无 token/网络权限、无 ReadWritePaths，
只能读取已选 g4 epoch 和旧 g2 archive。发布侧必须先在 immutable release 上执行
one-shot，逐字节确认 g4 root 未变，再决定是否启用；health 失败只能告警，不能
重启、修复或停止核心 5 分钟 accumulator。g4 learning 仍未绑定/启用，不能复用
旧 g2 learning unit。

`delayed_paper_round_trip_learning.py` 与其 worker 是独立的 G4 学习路径。
它只读取已验证的 G4 observation/completion，以及 BTCUSDT/ETHUSDT 两条
checksum-bound decision event，再在 `g4/evolution/round_trip_learning/` 追加
模拟样本、单 observation KPI、Challenger 建议、receipt 和 checkpoint。这些
投影不是模型、预测、收益结论或交易 authority：所有输出固定
`learning_authority=false`、`execution_authority=false`、
`production_eligible=false`、`manual_review_required=false`，并由确定性非实盘
证据门禁决定后续；当前仍没有自动 Champion 替换或风险扩张。

incremental worker 在 30 秒内部预算内每轮最多处理 8 条新增 completion，并从已有
checkpoint 头 append-only、可恢复地继续；它只重验 checkpoint 链的精确头部映射，
全历史逐条完整性由 daily full scrub 负责。有 backlog 时返回 `backlog_remaining`，并
报告已处理与剩余数量。没有既有 baseline 时仍返回 `full_scrub_required`，且必须先有
一次成功 full scrub；full scrub 仍遍历所有
completion→receipt→checkpoint 映射；已被较早 checkpoint 声明的 receipt/segment
若缺失或变更，必须 fail closed，绝不重建。tracked G4 learning 与 daily scrub unit
静态绑定当前 G4 epoch，并只能写入该 epoch 的 `evolution/`。两组 timer 默认
disabled：先满足连续 48 小时、覆盖率至少 90% 的核心门禁，再做 disabled full-scrub 与幂等重放，才可
人工启用 incremental timer；daily scrub 继续作为独立人工门禁，本次仓库变更不会
启用任何 learning timer。发布 preflight 必须先由受控安装步骤创建该空目录并读回
`tradingagent:tradingagent`、0700、非 symlink；unit 与 worker 都不会借缺失目录
创建或迁移核心 epoch。

`delayed_paper_round_trip_report.py` 是 G4 的只读 KPI 与 acceptance gate。
它将三种不能混淆的指标分开输出：核心连续性/新鲜度/守恒、已验证 decision 与完整
模拟 round-trip/退出原因、以及仅供模拟审计的 equity/fees/realized PnL。后者固定
标记为 `not_strategy_edge=true`，5 分钟时序 observation 也不被当成独立交易样本。
每次成功 sell 会从已验证 capital cycle 的 before/after 中计算单笔模拟 realized PnL
变化；未完成退出继续是 pending outcome。报告还列出连续 completion 段及其间的
未归因缺口；它绝不把缺口自动归因于 TradingDatas、transport、systemd 或账本，也
不允许研究标签跨缺口拼接。1h/4h/12h/24h 的未来收益标签仍只由
factor-research 的精确后续 observation 绑定；MFE/MAE 或替代退出规则在没有逐根、
完整且可审计的路径合同前只能保持研究 backlog，不能从单一 13-bar snapshot 猜测并
改变资本事实。

`tradingagent-crypto-round-trip-g4-acceptance.service/.timer` 是每日 09:05 的
只读 gate 候选。它不写任何文件、不触发学习、不启用 timer，也不依赖 PnL；模拟盘
上线门禁要求累计 48 小时（576 根 closed-5m 窗口）、窗口覆盖率至少 90%、无配置/
凭证/写库/状态完整性错误、核心 fresh、无 pending、Decision Ledger 计数一致且
capital balanced。已由 checksum-bound `data_gap` receipt 明确标记的窗口计入覆盖计算，
但不生成 observation、decision、order 或 capital effect；因此数据缺口是可观察
的 `data_incomplete`，而不是整窗 fail-closed。快照 API 8787 的
`/api/trading-agent/snapshot` 可读性仍需部署后直接读回，不能由本地报告代替。即使
eligible，下一动作仍固定为 disabled full-scrub
并执行幂等 replay；该 unit 是日常报告和告警证据，不是交易调度器。

## G5 现役 detached learning/scrub 边界

G5 是从只读 G4 通过版本化 recovery manifest 与 append-only supersession
receipt 创建的独立 successor root，不能把 G4 manifest、runtime profile 或账本原地
改写后继续运行。`tradingagent-crypto-round-trip-g5-{delayed-paper,health,acceptance}`
三组 unit 各自只读取固定 G5 环境文件；core 仅能写唯一 G5 root，同时只读 G4 与
旧 G2 根以验证 predecessor anchors。仓库 install-default 可以保持 disabled，
但这只是安装安全默认，不是当前生产状态；现役 G5 learning/scrub units 的
immutable release、enablement 与自然 readback 以 `STATUS.md`/`AUTODEV_STATE.json`
同轮事实为准。发布侧仍须在新 release 上先完成 one-shot、同槽 replay、资本/持仓/订单/
receipt 守恒与零重复 fill 验收，再按可回退流程切换 unit。
G5 health 与十币种 health watch 只负责输出告警/记录和数据质量信息，不是模拟盘上线的
硬门禁；只有配置、凭证、写库或状态完整性错误仍可阻断运行。
G5 acceptance 的 48 小时/90% 覆盖门禁只衡量 runtime maturity 及后续
promotion/risk/execution，不是完整 segment 离线 projection/evaluation 的准入门槛；历史
288 根连续 closed-5m 只衡量 runtime maturity，不替代当前模拟盘上线门禁。

为避免独立的 Crypto 采集与演练 runtime 在同一根新 K 线上竞争，G5 只消费前一根已收盘的
5 分钟 K 线；观察截止时间仍是当前周期的固定 cutoff，不接受 cutoff 之后的 receipt，不放宽
PIT 校验。其 bounded backlog、journal 摘要、单请求超时和 checkpoint 恢复规则沿用既有
G5 合同；它们不改变 receipt、资本、timer 或任何执行权限。

G5 的现役 learning projection 使用既有
`tradingagent-crypto-round-trip-g5-learning` 与 daily `...-learning-scrub` units；
factor research 是这两组 units 的 subordinate path，没有 standalone unit 或 authority。
它们只能写入 `crypto-delayed-paper-round-trip-epoch-g5-20260801/evolution/`；G4/G2 根、
自由 output root、网络、核心 observation、capital、order、Champion 与自动风险扩张都不在
其权限内。未来 release 仍须先在 disabled 状态完成同根 full scrub 与精确幂等 replay，
并读回 projection/checkpoint identity；daily scrub 不替代 core 收集或成为交易调度器。

G5 full scrub 仍对该只读 store 的 decision ledger 做完整链校验，并逐条复核
observation/completion/event index、projection receipt 与 checkpoint checksum 链。
它受现有 systemd 预算约束；`deferred_inventory_time_budget` 与
`deferred_time_budget` 只保留可重试的 append-only evaluation debt，不写 full-scrub
certificate 或授予 incremental 学习资格。任何漂移都失败关闭，完整 scrub 与同根幂等
replay 均通过后才可考虑后续受控 release。

## Outage epoch restart 候选

2026-07-29 ECS 停机后，旧 delayed-paper root 的最后 completion 停在
`2026-07-28T15:55:00Z`。TradingDatas 当前 envelope 的 `data_through` 已晚于
核心要回补的历史 `as_of`；核心因此拒绝伪造 PIT 并失败关闭。为停止重复无效尝试，
主集成已只停用 TA Crypto timer，TradingDatas collector 不受影响。本候选不修改
历史 state，也不把当前数据伪装成历史可用证据。

`delayed_paper_epoch.py` 新增仓外 current-epoch 合同。manifest 固定为
`/etc/tradingagent/crypto-delayed-paper.epoch.json`，必须精确声明：

- 唯一 `epoch_id` 与本次停机恢复专用的 `epoch_generation=2`；
- current root
  `/var/lib/tradingagent/crypto-delayed-paper-epochs/<epoch_id>`；
- archived root `/var/lib/tradingagent/crypto-delayed-paper` 及
  `read_only_archive_no_resume`；
- `capital_baseline_policy_id=crypto-capital-v1`、
  `aggregate_with_archived_epoch=false`；
- real/Testnet/Live/model network/自动晋级/自动扩风险全部关闭。

epoch parent 首次 claim 时还会以进程锁原子写入不可变
`.current_epoch.json`，把 manifest SHA、唯一 generation-2 root 与 10,000
USDT baseline 绑定。相同 generation 改用另一 root、回退 generation、复播旧
manifest 或篡改 current anchor 都失败关闭；未来再建 epoch 必须使用新的审核
合同和候选，不能只改当前 manifest。

新 root 首次使用前写入不可变 `.epoch_identity.json`，其中的 10,000 USDT、
capital generation 1 和 `local_fixture_opening_baseline_only` 全部派生自
`capital_policy.py`，不是 manifest 中的第二资本 authority。已有非空 root 没有
匹配 identity、identity/manifest 冲突、root 复用、symlink/硬链接/权限异常都会
失败关闭。`delayed_paper_epoch_runtime.py` 只把已验证 current root 交给核心；
旧 `delayed_paper_runtime.py` 的直接 CLI 与任何未提供 epoch context 的 Python
调用均失败关闭，不能继续写 archived root 或另一个任意 root。
核心的历史 PIT、receipt/lineage、费用、精度、资本和幂等门禁保持不变。首轮从
调用时刻对应的最新已完成 closed-5m window 开始，不查询旧 epoch 缺失窗口。

tracked service 仍只有
`tradingagent-crypto-delayed-paper.service`，timer 也仍只有原
`tradingagent-crypto-delayed-paper.timer`。service 改为：

- 只读 old root；
- 只写 epoch parent；
- 读取一个 current-epoch manifest；
- 不接受 `--output-root`，因此 timer 无法同时指向两个 writer/root。

本仓只生成候选，不部署。发布/迁移顺序固定为：

1. 保持 TA Crypto timer disabled，确认旧 root 的 observation/completion、
   Decision Ledger、capital ledger 与 pending 状态并生成只读封存证据；
2. 在服务器外部原子安装 current-epoch manifest，并把旧 root 设为发布侧只读
   archive；创建仅供 `tradingagent` 写入的 epoch parent；
3. 安装候选 release 和同名 service，先运行一次 one-shot，验证 epoch identity、
   独立 10,000 USDT baseline、当前 closed-5m completion 和旧 root 字节不变；
4. 重跑同一 slot 验证幂等，再验证相邻两个自动轮连续写入同一 epoch；只有这些
   读回通过后才重新 enable 原 timer。

回滚不允许把 timer 指回旧 root。若 one-shot、幂等或相邻轮失败，保持 timer
disabled，停止新 service，并同时保留旧 archive 与新 epoch root 作只读审计；
修正 release 后使用同一 manifest、current anchor 与 epoch identity 继续；若必须
创建后续 epoch，须经独立人工批准并以新的合同候选实现，不能在本合同中直接提高
generation 或替换 root。任何情况下都不得跨 epoch 合并现金、仓位、订单、权益、
PnL、收益率或晋级样本。

current generation-2 epoch 内若再次停机，runtime 仍先严格读取下一历史
exact-as-of；只有该读取明确因历史 `data_through` 越过 cutoff，或因历史窗口已
无法重建为连续 13 根而拒绝、pending 为空、最新 13 根 closed-5m 窗口及
receipt/lineage/freshness/quality 全部合格，并且现有资本链完整守恒时，才追加
一条 checksum/index/ledger-bound `data_gap`。
它精确记录 skipped range、拒绝请求、source proof、资本 head 锚点和恢复首窗的
observation/counterfactual；不调用资本 writer，不创建 run、候选、订单或成交，
也不写 learning completion。相同槽只做无网络幂等校验，下一根连续窗口才回到
正常核心。任一 gap/index/ledger/资本篡改、pending 或 fresh 证据不完整均
fail closed；本机制不创建新 epoch，也不重置或聚合 10,000 USDT。

本批 `fixture_auto_sim.py` 是 `crypto-capital-v1` 本地 fixture opening 闭环的唯一可写入口，但它仍是非权威候选。旧 `workflow.py`、`simulator.py`、`sim_executor.py` 与 `shadow_runner.py` 已变为无条件 fail-closed tombstone，不能通过注入 reader、切换配置或恢复旧 authority 重新启用。`promotion.py` 只保留只读研究 scorecard，永久输出不可自动晋级；shared governance 已把 `crypto-shadow-sim-v1` 降为历史证据并登记 `crypto-capital-v1` 为 `local_fixture_simulated_candidate`，不构成 current/runtime/live authority。

资本链 checksum、进程锁和 package-private writer capability 只防止正常调用误写、协作进程冲突与常见落盘损坏，不是抵御可修改同一 Python 进程、代码或账本文件的恶意主体的安全边界。默认构造的 ledger 只读，writer 仅由 fixture runtime 内部工厂创建；但拥有相同用户文件写权限的恶意或失控进程仍可能改写并重算本地链。未来获得任何生产资本权威前，必须另行验证进程隔离、运行 UID/GID、目录 owner/mode/ACL、只允许单一 writer 以及外部 durable receipt；本地链不得被当作密码学签名或 broker attestation。

LLM sidecar 在核心资本 cycle lock 释放后独立追加，并有 1 MiB 本地读取上界。sidecar 损坏、超限或写入失败只返回 `degraded/authority=none` 诊断，不撤销、重复或阻塞已提交的核心资本与 bundle replay。

## 十币种 Shadow 观测积累器（零权限、独立故障域）

> **Universe 版本化（10 → 40）。** 本节描述的 10 币链是已落盘的 append-only
> 历史，**只读封存，不再回写**。新 40 币研究 universe 由
> `market_observation.OBSERVATION_SYMBOLS_V40` 冻结，使用独立
> `forty_symbol_*` 契约族（profile/event/head/pending/data_gap/bars/spread/
> spreads）与独立 store root `/var/lib/tradingagent/crypto-40-symbol-observation`
> 40 币观察链可通过独立的 thin runtime 和 systemd 候选启动，但不接
> 晋级/资本；因子投影仍保持 detached，不接 systemd/晋级。thin runtime 见
> `forty_symbol_observation_runtime.py`。40 币因子投影使用 feature set
> `crypto-5m-ohlcv-factor-research-v3` + consumer profile
> `crypto-5m-ohlcv-13bar-forward-labels-v3` + 投影命名空间
> `evolution/forty_symbol_factor_research/`，不写旧
> `evolution/ten_symbol_factor_research/`。

### 四十币滚动评估（research-only CLI）

`python3 -m Crypto.forty_symbol_rolling_evaluation --store-root <完整40币只读store>`
只读取完整的 observation store；不接受自由的 events 尾文件或 bars 目录。它以
`head.json` 前后字节一致、`events_read_only()` 全链和既有 sidecar eligibility
重建为输入锚点；head 缺失、漂移或链/边车不一致时失败关闭或要求重试，绝不修复
source store。`--out-json` 与 `--report` 仅写调用方明确指定、位于 source store
之外的研究输出路径（解析别名后仍检查，不能覆盖输入）。

产物将 `source_receipt_integrity_verified` 与 `tradeable_pit_verified` 分开：前者
仅说明选中的连续 eligible segment 可归因；后者固定为 `false`。动量信号必须等到
所有输入的 `observed_at` 后的下一根可用 bar 开盘才作模拟入场；没有后续 bar 即为
弃权。开盘入场后从当根 bar 开始检查退出，同根同时触及止盈止损先记止损。
收盘到收盘基线和 OHLC 触价退出都是描述性 bar-only counterfactual，不能作为
成交、收益晋级、资本或执行 authority。

既有每日任务的包装器源为 `deploy/run-crypto-forty-symbol-rolling-eval.sh`，
发布时替换原 `/usr/local/sbin/tradingagent-crypto-rolling-eval.sh`，不新增 timer。
仅传 `--store-root`，不再手工拼接事件分段；每次在既有独立研究 output root 下
创建唯一 attempt 目录，失败保留诊断且不记成功，不覆盖旧报告，不写 observation store。

`ten_symbol_observation_store.py`、`ten_symbol_observation_profile.py` 与
`ten_symbol_observation_runtime.py` 组成一条独立、append-only、receipt 绑定的
10 币 5 分钟观测积累链，为后续 factor research 扩到 10 币（横截面研究）提供
证据级数据源。样本速率目标是 10 币 × 288 槽/天。它与 delayed-paper
core/learning/factor 完全不共享 root、锁或状态，任何一方故障互不影响；所有
事件固定 `authority=none`、`execution_eligible=false`、
`capital_write_eligible=false`、`model_authority=false`。

### 设计

- store：append-only 观测账本，三类事件——`observation`（一个 slot 的完整
  10 币 13 根窗口证据，复用 `market_observation.CryptoMarketObservation`
  payload，含 per-source receipt/lineage/watermark/digest）、`data_reject`
  （slot 数据不合格，幂等追加，不含市场行）、`data_gap`（历史窗口确定不可
  恢复时追加）。`head.json` 发布 sequence+checksum 检查点；进程锁与 cycle
  锁串行化 invocation；16 MiB 段原子 rotation；current 文件整体原子重写，
  crash 后从已 fsync 事件链重建 head（head 落后但前缀一致才恢复，其余分歧
  fail closed）。同槽重放不重复追加；同槽不同 payload（含 observation 与
  data_gap 跨类型冲突）fail closed；terminal 槽位严格单调。
- profile：冻结 profile，不复用 `CryptoFiveMinuteDataProfile`，避免触碰
  BTC/ETH 资本路径。10 个 bar dataset 各自绑定 shared canonical dataset
  fingerprint（`catalog_contract_sha256`），统一 consumer 查询形状
  （字段/order/identity/filter bindings/page budget）绑定
  `consumer_profile_sha256`，外层 `profile_sha256` 任一漂移 fail closed；
  catalog 硬校验直接复用 `market_observation._verify_catalog`。运行时使用
  evidence-only catalog 绑定：同轮 query 必须匹配同轮观察版本，十个目标 dataset
  合同指纹仍严格不变，但无关 dataset 上线造成的全局 catalog version 前进不阻塞。
- runtime：server CLI，只接受仓外冻结 manifest
  `/etc/tradingagent/crypto-ten-symbol-observation.runtime.json`（loopback
  IP literal base_url、catalog_version、完整 profile payload + SHA、绑定的
  output_root、safety 合同）；manifest 校验在任何读写之前（绝对路径、
  regular/single-link、owner/mode、重复 JSON key、读取中变更）。token leaf
  固定 `/run/secrets/tradingagent/tradingdatas-crypto-read.token`；transport
  懒构造，pending/同槽恢复不需要网络或 token。输出根只能来自 manifest 绑定
  的 `/var/lib/tradingagent/crypto-ten-symbol-observation`，CLI 没有
  `--output-root`。wire 只有 `GET /v1/catalog` 与 `POST /v1/query`。
- bars sidecar：store 事件是 digest-only 证据账本，不含 13 根 OHLCV 行。
  fresh 采集成功后 runtime 先把该槽 10 币原始 bar 行原子写入
  `bars/<slot>.json`（canonical JSON、tmp+rename+fsync、0600），再落账
  事件——crash 顺序保证事件引用的 sidecar 必存在，孤儿 sidecar 无害且下轮
  零网络复用。sidecar 同内容重写幂等、异内容 fail closed；data_gap 恢复
  首窗按恢复槽同样写 sidecar。sidecar 每 source 携带原始行序列加上
  receipt/digest 元数据；消费方用分页层 canonicalization
  （`ensure_ascii=False`、sort_keys、无尾随换行）从行独立重算
  `identity_sha256`（`symbol`/`open_time` 序列）与 `market_data_sha256`
  （有序行序列），并与 store 事件的 per-source digest 逐值比对，重建的
  observation 还必须复现 `observation_sha256`。sidecar 本地校验失败一律
  fail closed，绝不记为 data_reject。观测事件合同与 digest 定义不变，bar
  行不进入任何事件 digest；补丁前的旧槽没有 sidecar，v2 视其为
  feature-ineligible（数量极少）。
- spreads sidecar（book_ticker 实测点差采样，附加证据）：每个 fresh 槽在
  10 币 13 根 bar 采集成功后，追加采样 10 个
  `crypto.spot.binance.<symbol>.book_ticker` current snapshot（best
  bid/ask 与 qty），供点差投影聚合、并由费用后评估用实测点差替代假设
  滑点成本。
  点差是**附加、降级容忍**的证据，绝不进入资本/策略路径，也绝不允许其失败
  导致 bar 观测丢失：spread leg 使用独立 client（dataset_ids 只配置 10 个
  book_ticker 数据集）与独立 catalog 读，因此 book_ticker 数据集缺失或合同
  drift 只降级 spread、不触碰 bar 链的 catalog 门禁；采样在 bar 成功的同一
  attempt 内跟随执行，其失败不触发 bar 的有界同槽重试；预算耗尽信号
  （`_InvocationBudgetExhausted`）永远穿透、不被降级吞掉。降级语义分两
  层——per-symbol：单 symbol 的 catalog 硬门禁（schema/fields/identity/
  `point_in_time=current_snapshot`/filter/limits）、查询/元数据门禁
  （ready/非 degraded/fresh 非 stale/quality valid/完整 lineage/receipt）、
  watermark（槽结束 ≤ receipt `observed_at` ≤ 槽 cutoff，与 bar 同一纪律；
  上游无事件时间戳，receipt 观测时刻是唯一时间权威）、行校验（正值
  bid/ask/qty 且
  ask ≥ bid）任一失败只把该 symbol 记为 `rejected` + 稳定 reason code，
  其余 symbol 照常采样；leg-wide：spread 自身 catalog 读或同形失败把整个
  leg 记为 `unavailable` + 单一 reason code。采样成功的槽把每 symbol
  原始快照行、receipt/observed_at/freshness/quality 元数据与实测
  `catalog_contract_sha256` 指纹原子写入 `spreads/<slot>.json`（与 bars
  sidecar 同一 immutable 写合同，写在 bars sidecar 之后、事件之前）；
  `observation` 与 `data_gap` 事件新增 `spread` 状态块（contract
  `tradingagent.crypto.ten_symbol_observation_spread.v1`：status ∈
  completed/degraded/unavailable、sampled/rejected 计数、rejected_reasons、
  `spread_sha256`、本 leg 实测 catalog_version），状态块只绑定 digest 与状
  态，快照行不进入任何 observation digest；`observation_sha256`/
  `market_data_sha256` 与 v1 观测 payload 字节不变。契约演进遵循
  sidecar 先例、不引入任何新版本号：event contract v1 不变（data_gap 本
  就是同合同多形态 payload，store 不锁定精确键集）；profile v1 不变——
  book_ticker 指纹**每槽实测记录**进 sidecar 而非冻结进 profile，因为冻结
  会让其 drift fail-closed 整条 bar 链，与附加证据的降级语义直接冲突；服
  务器既有冻结 manifest 因此继续有效，无需重新生成。升级前的旧槽没有
  `spread` 键和 spreads sidecar，下游视同 feature-ineligible（同
  pre-sidecar 槽先例）。零网络恢复路径绝不重采样点差：bars sidecar 复用时
  spreads sidecar 存在则逐值重算校验并重建同一状态块，缺失（crash 窗口）
  则记 `crypto_spread_sidecar_missing` 的 unavailable——保持
  "pending/同槽恢复不需要网络或 token" 不变式；spreads sidecar 本地校验失
  败一律 fail closed（`runtime_spreads_sidecar_invalid`），绝不记为
  data_reject。每 cycle 请求数为 22（bar leg 1 catalog + 10 query，spread
  leg 1 catalog + 10 query），仍受同一 120 秒绝对预算约束。点差数据的
  消费方是独立的 detached 只读投影 `ten_symbol_spread_projection.py`
  （见"十币种实测点差投影"章）：它经 store 事件链 + `spreads/<slot>.json`
  用与 bars sidecar 相同的只读路径获取实测点差；factor v2 投影与费用后
  评估的 record/评估合同均不变。

### slot / backlog / gap 语义

完整行情校验通过后，先保存不可变 bars sidecar，再采样可选 spread。
可选 catalog/book-ticker 耗尽预算仍返回 `backlog_pending`，不伪造完成；
已有 bars 与原 pending 定位保留，同槽或下一槽重启可零网络恢复原槽。
缺 spread sidecar 明确记录 `crypto_spread_sidecar_missing`，不回补采样。
跨 gap 恢复还须绑定原始 rejection、profile 与 cutoff，保留 skipped range；
行情自身超时、PIT 不合格或损坏 sidecar 不能借此绕过校验。

40 币独立配置的补充：cutoff 固定为 bar close +270 秒，timer 在 +285 秒
（`*:4/5:45`，另有最多3秒 jitter）触发；10 币配置不变。40 币当前槽的
query-shape 瞬时错误仍允许预算内 20/45 秒重试；严格早于本次冻结 current window
的历史槽只做一次 shape 查询，失败后立即进入既有缺口恢复。等待不会恢复历史 PIT，
反而可能令当前窗口被下一批 receipt 替代而越过其 cutoff。当前窗口仍必须通过
原始 watermark、quality、lineage、完整性校验后才能写 data_gap；没有合格数据则
继续如实拒收，不推进 checkpoint、不生成价格、样本或订单。

- `window_end` 与 `observation_cutoff` 固定为 bar close +55 秒，不随 systemd
  jitter 或重跑墙上时钟漂移。
- timer 固定在每根 bar close +3m25s，避开现役 Crypto core 的 close +55s
  调用并居中放在相邻 core cadence 之间。120 秒业务预算从 invocation 开始计时并
  包含全部 wire attempts 与 retry sleep；进程启动/停止开销不在该函数预算声明内，
  不能据此静态保证无重叠；发布验收必须取得前一 core、ten-symbol reader、后一 core
  三次自然运行的起止时间与 exit 0。两者共享 TD token/API/SQLite surface，禁止恢复为
  重叠并发。
- 每 invocation 最多 2 cycle（pending recovery + 1 fresh，或两个连续处理
  步骤），同时受 120 秒绝对 wall-clock budget 约束；每次 TD wire timeout
  压缩到剩余预算，预算耗尽时保留 pending 与已经完成的增量，不追加伪造的
  `data_reject`。单次 fresh 采集内部对**传输层瞬时错误**（timeout、
  connection 类：`TimeoutError`/`ConnectionError`/
  `urllib.error.URLError`（不含 HTTPError）/`http.client.HTTPException`，
  含其包装链）做有界同槽重试：最多 `MAX_COLLECT_ATTEMPTS=3` 次尝试、固定
  间隔 20s、每次完整独立构造 transport+client，slot cutoff 固定不随重试
  漂移；数据合同/校验失败（catalog 漂移、watermark、continuity、profile
  不符）、HTTP 状态错误（含 401/403，永不重试）与预算耗尽信号一律立即
  失败。全部尝试失败仍走原 fail-closed 路径（pending 保留、不伪造
  data_reject）；receipt 的 `collect_attempts` 记录实际尝试次数。仍落后
  当前槽时返回 `backlog_pending`；若本轮已经按序处理至少一个 cycle，则保留
  backlog/lag JSON 证据并退出码为 0，这是数据可见性信息项，不是状态完整性
  失败。若本轮 0 cycle 且预算耗尽，则退出非零，表示运行时没有进展。下一轮
  仍从最早缺口继续，槽位严格按序补，绝不跳过中间时槽。合同、凭证、完整性和
  不可恢复的数据失败仍返回非零。
- pending marker 只是 crash 簿记（不是证据）：fresh cycle 取数前写入、事件
  落账后清除。槽已有事件的 pending 恢复不需要网络；pending 槽已成历史时
  清除 marker 并由 data_gap 合同显式覆盖该槽——这也与 core 的 pending 必须
  完整恢复不同，因为积累器的 pending 不含任何已验证数据。
- data_gap 合同：current-read 查询不带 `as_of`，历史槽的
  `observed_at` 必然越过其 cutoff（`crypto_observation_watermark_invalid`），
  因此历史窗口确定不可恢复，不伪造 PIT。历史槽若因上游采集缺口导致源行
  永久缺失，则每次重试都在形状门禁失败
  （`crypto_observation_query_shape_invalid`），同样确定不可恢复；两种
  reason 都只在目标历史槽严格落后当前槽时进入缺口恢复，当前槽的合同漂移
  仍然 fail closed。只有 pending 为空、目标历史槽严格
  落后当前槽、且当前 10 币 13 根窗口全部 catalog/receipt/lineage/freshness/
  quality 门禁通过时，才追加一条 `data_gap`：记录精确 skipped range、拒绝
  原因与被拒窗口，并内嵌恢复首窗的完整 observation 证据；恢复槽不另写普通
  observation 事件。下一根连续窗口恢复正常积累。同槽重放只做无网络幂等
  校验。
- data_reject 是非 terminal 的 attempt 事实：相同确定性 event ID 重放幂等；
  同一 slot 的不同 reason/attempt 分别追加并保留，不能因为 timeout、metadata、
  watermark 等失败原因随重试变化而把该 slot 永久卡死。observation/data_gap
  仍保持 terminal 同槽唯一和异 payload fail closed。

### manifest / token / root 边界

manifest root-owned 0640、仓外、secret-free；token 只由最终 HTTP transport
从固定 leaf 注入；输出根、catalog version、10 份 dataset contract 全部由
manifest 冻结，runtime 不从当前 catalog 动态重建或放宽。book_ticker 点差采
样不进入该冻结 profile（指纹每槽实测记录进 spreads sidecar），因此既有
manifest 在引入点差采样后继续有效，无需重新生成。部署 runbook
（独立步骤，需 Nicholas 明确批准后执行）：

1. 服务器创建 `/var/lib/tradingagent/crypto-ten-symbol-observation`
   （`tradingagent:tradingagent`，非 symlink；unit 的 `StateDirectory=` 也可
   代为创建）。
2. 从 live catalog 读回 10 个 dataset 的 contract SHA，生成并原子安装
   `/etc/tradingagent/crypto-ten-symbol-observation.runtime.json`
   （root-owned 0640，不含 token）。
3. 验证现有 crypto read token 对 6 个新 dataset 的查询权限（bounded query
   smoke）。
4. 在 immutable release 上跑 one-shot → 同槽幂等重放 → 相邻两轮；全部通过
   后才 `enable --now` timer。
5. 回滚：停/禁 timer，保留 store 只读审计，不删除任何事件。

### 明确未实现

- 没有横截面 factor/IC 声称、rules dataset、历史回填证据化；50 标签初筛
  不构成晋级。factor v2 消费端已作为 install-default 不启用的 detached
  候选存在（见下章），不代表已启用或已有样本。
- book_ticker 点差落账（事件 `spread` 状态块 + spreads sidecar）已被独立的
  detached 只读投影消费（见"十币种实测点差投影"章）：镜像 bars sidecar
  消费路径（事件链 `spread_sha256` 逐值比对 + sidecar 重算），把实测点差
  聚合为样本级成本证据研究 artifact；factor v2 投影 record 合同不变，
  费用后策略评估已按该章合约接入实测半点差替代假设滑点（逐单元
  `cost_source` 标记，样本不足回退假设成本，见评估章成本口径条目）。
- systemd unit 是 install-default 不启用的候选，不代表现役 timer 状态。
- 该链不读取/写入 core、资本、learning、Champion 或 `evolution/`，也不构成
  任何 PIT 回填或交易 authority。

## 十币种横截面 Factor Research v2（detached 投影候选）

`ten_symbol_factor_research.py` 与 `ten_symbol_factor_research_worker.py`
是 10 币观测积累器的 detached offline 因子投影，机制镜像
`delayed_paper_factor_research.py`，输入换成观测 store 事件链 + bars
sidecar。它与 core/资本/learning/Champion 完全不共享写权限，固定
`authority=none`；任何投影失败都不能改变观测积累器的状态、退出码或事件链。

### 证据绑定与 consumer profile

- 输入单位是 terminal 槽（`observation` 或 `data_gap` 恢复首窗）。每个槽先
  读 `bars/<slot>.json` sidecar，从原始行重算每 source 的
  `identity_sha256`/`market_data_sha256`，并重建 observation 复现
  `observation_sha256`，再与 store 事件逐值比对；sidecar 缺失或 digest
  不符的槽永不投影，视同 gap 切断 segment，checkpoint 记录
  `projection_outcome=sidecar_ineligible` 与原因。
- 冻结 consumer profile `crypto-5m-ohlcv-13bar-forward-labels-v2`：10
  symbol（固定 `market_observation.OBSERVATION_SYMBOLS` 顺序）、13 根 5m
  bar、feature set `crypto-5m-ohlcv-factor-research-v2`（version 2）、
  required label horizon 60min、auxiliary 240/720/1440min。
  `factor_research.py` 仅增加 keyword-only 可选参数，默认 v1 行为字节不变。
- record 含 10 个 per-symbol snapshot（receipt_id + per-source
  `identity_sha256` 作 lineage 绑定材料）、标注为 context 的横截面 1h/15m
  return 排名/极差（`is_research_hypothesis=false`，不是新假设）、
  `segment_id`、`source_event_checksum` 与 `source_bars_sidecar_sha256`
  双重绑定。三个预注册假设不变，横截面不加新假设。
- segment：相邻 terminal 槽差 ≠5 分钟或前一槽 ineligible 即开新段
  （`crypto-5m-segment-<段首slot>`）；label 只在同段内按
  slot+horizon 结算，跨段/目标槽 ineligible 一律不结算。

### 运行模式与部署门禁

- 投影根固定 `<store_root>/evolution/ten_symbol_factor_research/`
  （records/receipts/labels/checkpoints + immutable 写 + checkpoint hash
  链）；checkpoint 与 terminal 槽 1:1（含 ineligible 槽），增量/全量计数
  语义与 v1 相同。incremental 不回填 label；落后时在单次 invocation 内按槽
  序有界自恢复（最多 `MAX_CATCHUP_UNITS=12` 个未投影 terminal unit，逐
  unit 走同一 eligibility/segment/checkpoint 逻辑，checkpoint 与 terminal
  槽保持 1:1，绝不跳槽）；追平返回 `projected_incremental`，处理完仍落后
  返回 `backlog_remaining`（带 projected_count/remaining_count，与
  `full_scrub_required` 同为非错误 status、退出码 0），下轮从下一序号
  继续；segment 延续判定逐 unit 滚动，与 full scrub 的 `_segment_ids`
  语义一致（测试保证两者产物字节一致）。daily full scrub 仍是唯一的全链
  校验 + label 结算 + hypothesis report 路径；超时走可重试
  `deferred_time_budget`/`deferred_inventory_time_budget` debt。
- worker 只从固定
  `/etc/tradingagent/crypto-ten-symbol-observation.runtime.json` 推导
  store root，CLI 没有 output-root/manifest 自由参数，执行前后重验
  manifest 字节与 root identity；`_assert_simulation_only` fail-closed，
  异常 stderr 脱敏 + exit 2。
- systemd 候选（install-default 不启用）：incremental unit
  `OnCalendar=*-*-* *:4/5:50`（观测在 close+3m25s 触发、典型周期约 50s，
  留约 80s margin；观测偶发长跑时本轮投影上一槽，下轮追平，且投影不共享
  TD wire surface），scrub unit 每日 `04:05:00`+5m jitter（错开 learning
  scrub 的 03:30 窗口）。unit 以 ReadOnlyPaths 挂观测 store、
  ReadWritePaths 仅 `evolution/ten_symbol_factor_research`；部署需先预建该
  目录（tradingagent:tradingagent 0700），再经 Nicholas 明确批准后方可在
  sidecar 积累出首个可用 segment 后启用。

### 费用后策略评估（scrub 下游）

`ten_symbol_factor_strategy_evaluation.py` 是 v1
`factor_strategy_evaluation` + post_projection 的 v2 port，只在 full scrub
之后由 worker 调用（incremental 从不结算 label，只走 compact checkpoint
快速路径）：

- 样本重建与核验：从投影根 records/labels/checkpoints 重建全部 resolved
  (snapshot, label) 样本（required 60min + auxiliary 240/720/1440min，同
  segment、label 文件存在），逐样本按 v2 合同核验——snapshot/label
  integrity（v2 feature set）、record/receipt/checkpoint 三件套互绑、
  `source_event_checksum` 对 store 事件链逐值比对、
  `source_bars_sidecar_sha256` 对磁盘 sidecar 重算比对、checkpoint 链完整
  重放（同一 inventory 的样本共享同一条链对象，重放一次复用）、cost
  policy `crypto-round-trip-taker-v1`（fee 0.001 双边 + slippage 2bps
  双边，来自 `round_trip_capital.TAKER_FEE_RATE/SLIPPAGE_BPS`）逐项比对、
  gross/net 按 entry/exit/fee 重算、future 不晚于 evaluation_as_of。任一
  不符 fail closed，不跳过样本。
- 每个 horizon × 每个假设一份评估 artifact（contract
  `tradingagent.crypto.ten_symbol_factor_strategy_evaluation.v1`），在同一
  bundle 内按 horizon 分组（`evaluations["60"|"240"|"720"|"1440"]`）：
  always-invest 基线（全部样本视为有信号的同成本曲线）、cash 基线（无仓
  位零成本零回撤）、metrics（signal/abstention/coverage/hit_rate/
  cost_adjusted_net_return/baseline_delta/cash_baseline_delta/drawdown/
  turnover/round_trip_leg_rate；drawdown 按等权 per-slot 权益曲线，
  turnover 即暴露率）。aux horizon 的 artifact 标注
  `research_attribution=true`，仅作方向性证据；样本不足的 horizon 在
  `horizon_status` 报 `insufficient_resolved_samples` 而不产出评估、不
  fail。recommendation：无信号 → `disable`，费用后均值 ≤0 →
  `downweight`，否则 `retain_for_more_evidence`；**recommendation 只基于
  required 60min 口径**（晋级判据不扩口径），`evaluated_status` 固定
  `exploratory_insufficient_edge`，不构成 edge、晋级或参数变更授权。
- 成本口径与实测点差接入：成本 = 既有 taker fee（label 内
  `fee_rate=0.001` 双边，逐项重算校验）+ 每边滑点。滑点优先取
  `ten_symbol_spread_projection` 当前 checkpoint 绑定 artifact 的实测
  点差（消费纪律见"十币种实测点差投影"章）：逐评估单元（source slot
  × symbol）取该 symbol 在不晚于样本槽日的最近一个**充足样本** UTC
  日桶的 `p75_bps` 半值作为每边滑点；桶充足阈值
  `SPREAD_COST_MIN_BUCKET_SAMPLES=12`（≥ 当日 288 个 5 分钟槽中的 12
  个样本，即至少一小时覆盖——低于一小时时 type-7 p75 只在不足十个
  观测间插值，只反映日内短窗口而非当日点差区间，故不外推）。样本
  不足的 symbol/桶、无 checkpoint 绑定 artifact、或投影命名空间尚未
  产出时，单元回退假设成本（2bps/边）并显式标记，绝不静默混合。
  checkpoint/artifact 链校验失败、artifact 缺失/损坏或 metric 合同
  漂移一律 fail closed（`evaluation_spread_artifact_invalid`）。
- 成本可见性：每个 horizon×假设的评估 artifact 带 `cost_model`（fee、
  假设滑点、实测规则、阈值、spread artifact 绑定或 null）且 metrics
  含精确 `cost_source_counts`；逐单元
  （observation_id/symbol/horizon 列表、`cost_source: assumed|measured`、
  实际每边滑点 bps、所用桶日期与样本数）写入 immutable 伴随 artifact
  `evolution/ten_symbol_factor_research/
  strategy_evaluation_cost_attributions/{outcome}.json`（contract
  `...evaluation_cost_attribution.v1`，自含
  `cost_attribution_sha256`，由 bundle 的
  `cost_attribution_sha256` 字段绑定；评估自身不回读该文件，主 bundle
  保持 O(1) 大小）。评估 outcome 身份纳入所消费 spread 投影的
  `outcome_sha256`：实测证据更新会重评估同样本，而不是被 compact
  checkpoint 掩盖；incremental 快速路径语义不变（新 outcome 只在
  full scrub 后的完整评估产生）。
- artifact immutable 写入
  `evolution/ten_symbol_factor_research/strategy_evaluations/{outcome_sha}.json`
  （outcome = resolved 样本集合的确定性 sha，只有样本集合变化才前进）；
  compact `strategy_evaluation_checkpoint.json`（tmp+os.replace 原子覆写）
  记录 last_evaluated_outcome_sha256/artifact_sha256。幂等：同 outcome
  返回 `no_new_outcome`；0 resolved 返回 `insufficient_resolved_samples`
  不产出 artifact；首次 scrub 前的 incremental 轮明确返回
  `no_evaluation_checkpoint` 而非失败。
- worker 合并语义：full-scrub 成功（recovered/scrubbed）后按序调用评估，
  结果合并进 stdout receipt 的 `strategy_evaluation` 子对象；评估失败只记
  `evaluation_failed` debt（下次 scrub 重试），绝不改变已完成 scrub 的
  事实与退出码。scrub deferred 时评估对应 `evaluation_deferred`。
- **重叠标签警告**：每个 horizon 的 metric_basis 按窗口写明重叠率与有效
  独立样本折算（60min=11/12 ≈ 1/12、240min=47/48 ≈ 1/48、720min=143/144、
  1440min=287/288）；HAC/非重叠子样本的统计显著性结论留待后续 slice，
  当前所有均值/命中率都只是探索性描述。

### 明确未实现

- 不做 HAC/非重叠子样本统计显著性；不做横截面 IC 研究合同（等 288 段
  成熟度达标后的独立 slice）；50 标签初筛与评估 recommendation 均不构成
  edge、晋级或参数变更授权；不做历史回填证据化；unit 不代表现役 timer
  状态。

## 十币种实测点差投影（detached 只读研究 artifact）

`ten_symbol_spread_projection.py` 是观测积累器 spreads sidecar 的
detached 只读消费投影：把落账的 book_ticker 实测点差聚合成样本级成本
证据研究 artifact，供后续费用后评估用实测点差替代假设成本。它与
factor v2 投影相互独立（独立投影根
`<store_root>/evolution/ten_symbol_spread_projection/`、独立锁、独立
checkpoint），不读写 core/资本/order/Champion/learning，也不改 factor
v2 的 record/label/evaluation 合同；固定 `authority=none`、
`research_only=true`、零网络。

### 证据绑定与 fail-closed 纪律

- 输入单位是 terminal 槽（`observation` 或 `data_gap` 恢复首窗）。事件链
  由 store 只读校验；每槽先 shape-check 事件 `spread` 状态块（contract
  `tradingagent.crypto.ten_symbol_observation_spread.v1`、status/计数/
  reason 一致性），再按 bars sidecar 消费先例逐值比对：
  `validate_ten_symbol_spreads_sidecar` 从持久化 sidecar 独立重算每 symbol
  行 digest 与顶层 `spread_sha256`，并由 entries 重建状态块与事件块
  canonical 逐值比对，sidecar `window_end`/`profile_sha256` 同时与事件
  比对。任何漂移（`ten_symbol_spread_projection_spread_digest_mismatch`）
  或 sidecar 本地校验失败
  （`ten_symbol_spread_projection_sidecar_invalid`）一律 fail closed，
  绝不静默剔除该槽后继续。
- 槽位排除只发生在三种显式情形并逐一记录进 artifact 的
  `skipped_slots`：旧槽无 `spread` 键（`feature_ineligible`，同
  pre-sidecar 先例）、leg-wide 降级无 sidecar（`spread_unavailable`
  + reason code）、事件声称有点差证据但 sidecar 缺失（`sidecar_missing`，
  镜像 bars sidecar 缺失即 ineligible 的先例）。data_gap 覆盖的历史跳过
  区间从未有点差证据，不参与统计。
- 聚合只计 `sampled` 条目；`rejected` 条目只进入拒收计数与
  `rejected_reason_counts`，绝不进入点差统计。

### 聚合口径与 artifact 合约

- 有界窗口 = symbol × UTC 自然日（`aggregation_window=
  utc_calendar_day_per_symbol`）。点差定义：`(ask-bid)/mid*10000` bps，
  `mid=(ask+bid)/2`；样本在摄入时统一量化到 1e-8 bps
  （ROUND_HALF_EVEN），统计输出同精度；分位数用 type-7 线性插值。
- 每桶产出：sample_count、rejected_count、rejection_rate、
  rejected_reason_counts、slot_count、first/last slot 与
  first/last observed_at、spread bps 的 mean/median/p25/p75/min/max
  （无样本时全 null）；另有 `symbol_totals` 与全局 `totals` 汇总。
- artifact（contract
  `tradingagent.crypto.ten_symbol_spread_projection.v1`）immutable 写入
  `evolution/ten_symbol_spread_projection/artifacts/{outcome_sha}.json`，
  自含 `artifact_sha256` 与 `outcome_sha256`；`source` 块绑定 store 链
  head checksum、每个被消费槽的 `window_end + source_event_checksum +
  spread_sha256` 三件套以及全部 skipped 槽。outcome = 被消费源集合 +
  skipped 集合的确定性 sha，只有输入集合变化才前进。
- compact `spread_projection_checkpoint.json`（contract
  `tradingagent.crypto.ten_symbol_spread_projection_checkpoint.v1`，
  tmp+os.replace 原子覆写）记录 last_projected_outcome_sha256/
  artifact_sha256；重跑先校验 checkpoint 与其绑定的 artifact，同 outcome
  返回 `no_new_outcome` 且字节不变——幂等可重跑。checkpoint/artifact
  篡改 fail closed。无 sampled 样本返回
  `insufficient_spread_samples` 不产 artifact；观测 pending 非空返回
  `deferred_core_pending`；退出码经
  `ten_symbol_spread_projection_exit_code`（authority 字段不符即 2）。
- 每次运行从已验证事件链确定性重建全量库存（O(N) 读），无增量
  catch-up 合同；artifact 因含逐槽绑定而随历史线性增长，读取沿用与
  factor 投影共享的 2 MiB canonical artifact 上限，超限 fail closed。

### 费用后评估的消费接入（已实现）

费用后评估 `ten_symbol_factor_strategy_evaluation.py` 已按本章合约消费
点差 artifact（见"费用后策略评估（scrub 下游）"章的成本口径条目）：
只读取 checkpoint 绑定的当前 artifact，先经
`spread_projection._validated_current` 重放 checkpoint→artifact 链
（checkpoint 自校验、绑定 `artifact_sha256`/`outcome_sha256`、authority
字段），再逐项比对 `spread_metric` 合同（公式/聚合窗口/分位数方法），
任何缺失（链内 artifact 文件）、损坏、篡改或合同漂移 fail closed
（`evaluation_spread_artifact_invalid`），绝不降级消费。投影命名空间
不存在或从未产出 artifact 是唯一允许的降级：全部单元显式回退假设
成本并标记 `cost_source=assumed`。消费规则：逐评估单元取
`buckets[symbol]` 中不晚于样本槽日的最近一个 sample_count ≥ 12 的
UTC 日桶 `p75_bps` 半值作每边滑点（替代
`crypto-round-trip-taker-v1` 的假设 2bps，taker fee 不变），样本不足
不外推；`source.spread_sources` 提供逐槽证据回溯。评估结论仍是
research-only：artifact 不作为实时成本、执行信号或晋级证据。

### 明确未实现

- 点差 artifact 的消费仅限上述费用后评估成本替代，未接入任何策略
  晋级/参数变更逻辑；无 worker CLI 与 systemd unit（当前由显式一次性
  调用运行）；不做跨日
  滚动窗口、成交量加权深度点差或 HAC 显著性；投影不构成成本、edge、
  晋级或参数变更授权。

## 研究进化闭环 第一阶段（假设重估调度器，detached 只读）

`ten_symbol_research_loop.py` 是研究进化闭环的第一阶段：一个离线、只读、
detached 的假设研究调度器。输入为观测 store 根目录加预筛配置
（`--horizon-bars`，默认 12,48,144,288 即 60/240/720/1440min），行为是把
**已注册**的四个预筛候选（`xs_rs`、`short_reversal`、`amihud_illiquidity`、
`momentum_vol_regime`）在最新观测证据上自动重估，产出 immutable 自动评审
报告 artifact（`review.recommendation=automatic_reevaluation_complete`，
逐候选给出 `positive_in_sample_only`/`nonpositive_in_sample`/`auto_retain`）。
v3 保留四个旧候选的描述性重估，增加一个固定的低换手趋势基准及封存的前向
窗口。不再由历史最优正收益格子给出晋级或资金分配指令；不接 systemd/执行，
`REAL_TRADING_ENABLED=false` 保持不变。旧 v1/v2 目录只读保留、不迁移。

### 数据路径与证据绑定

- 输入单位是 terminal 槽；事件链由 store 只读校验（`events_read_only`），
  每槽 bars sidecar 复用 factor v2 投影的 `_build_units` 资格门禁：独立重算
  per-source digest 并与 store 事件逐值比对。sidecar 缺失或 digest 漂移的槽
  标记 ineligible、视同 gap 切断 bar 历史；事件链损坏一律 fail closed。
- eligible 槽的 13 根窗口按 `open_time` 合并为每 symbol 的单调递增历史，
  同一 open_time 的行内容冲突 fail closed；合并后重走预筛的行校验
  （`_validate_history_rows`），缺口只记录不填补。
- 评估口径完全复用 `ten_symbol_factor_prescreen.analyze`（同一成本模型
  `crypto-round-trip-taker-v1`、同一特征/标签/非重叠子样本口径），不复制
  不修改；预筛顶层 banner 描述其自身历史回填路径，不嵌入本 artifact，数据
  出处由 artifact 自有 `source` 块声明。注册集合漂移（candidate id 集合
  变化）fail closed。

### Artifact 合约

- 评审报告（contract
  `tradingagent.crypto.ten_symbol_research_loop_review.v3`）immutable 写入
  `<store_root>/evolution/ten_symbol_research_loop.v3/reports/{report_sha256}.json`，
  自含 `report_sha256`；内容包括：store 链绑定（event 计数、head
  checksum、逐槽 terminal_units 三件套与其聚合 sha）、数据窗口、每个
  horizon 的完整预筛分析、`metrics_summary`（每候选 × horizon × variant
  的费用前 `mean_gross` 与费用后 `mean_net`、hit_rate、baseline_delta、
  非重叠子样本指标）、`diff_vs_previous`（与上一份报告的逐 cell 对比：
  change 分类、计数差、Decimal 指标差）以及自动
  `review.recommendation`（最优历史均值仅供描述，禁止据此晋级）、
  `trial_accounting` 和 `slow_trend` 冻结基准。artifact
  因含逐槽绑定随历史线性增长，读取沿用共享 2 MiB canonical 上限，超限
  fail closed。
- compact `research_loop_checkpoint.v3.json`（contract
  `tradingagent.crypto.ten_symbol_research_loop_checkpoint.v3`，原子覆写）
  绑定 `last_input_digest` 与 `report_sha256`。input digest 覆盖 horizon
  配置、低换手冻结计划 SHA、store 链 head 与逐槽资格材料：同输入重跑先重验 checkpoint 与其
  绑定报告，返回 `no_new_input` 且 artifact 字节不变（幂等）；checkpoint
  或报告篡改 fail closed。无 terminal 事件返回 `deferred_core_pending`，
  全部槽 ineligible 返回 `insufficient_eligible_slots`（非错误）；退出码经
  `ten_symbol_research_loop_exit_code`（authority 字段或
  `automatic_reevaluation` 不符即 2）。
- 一次性 CLI：`python3 -m Crypto.ten_symbol_research_loop --store-root
  <path> [--horizon-bars 12,48,144,288]`；无 worker、无 systemd unit。

### 模拟域自动晋级边界

上述研究循环 v3 无晋级权限；此处其它模块的既有模拟域合同不因 v3 研究结果
改变。`positive_in_sample_only` 不得路由到资本、Champion 或订单。

- **假设生成器（第二阶段）**：B 类候选通过可行性检查后自动标记
  `registration_status=auto_registered` 并写入 `registered_into_prescreen/
  registered_into_evaluation=true`；blocked 候选保持未注册。
- **自动晋级只在模拟域**：研究/评估/Champion/模拟资金全部自动，但绝不授予
  自动风险扩张、执行或真实交易 authority；`REAL_TRADING_ENABLED=false`
  继续作为独立硬闸，任何 `REAL_TRADING_ENABLED=true` 都 fail closed。

## 十币种观测链只读健康检查（health watch）

`ten_symbol_health_watch.py` 是十币种观测积累器与 TradingDatas 数据面的
独立只读 watchdog（G5 `delayed_paper_health.py` 的 ten-symbol 对应物）：
只经 store 的 lock-free 只读路径读事件链与 sidecar，绝不重建
head/index、绝不写任何 store 文件；任一损坏或合同漂移 fail closed。
固定 `authority=none`、零 capital/order/model/promotion 权限。背景是
9 小时级停滞无告警事故，目标是让停滞与数据面异常以机器可读状态暴露。

### 检查项与阈值依据

- `observation_chain_lag`：`latest_terminal_slot` 距 now 的滞后。观测
  timer 在 bar close+3m25s 启动、cutoff 为 close+55s，健康链滞后恒小于
  约 9 分钟；>600s（错过一个完整 timer 周期）记 degraded，>900s
  （15 分钟，事故复盘阈值）记 failed。pending 槽位作为证据带出。
- `reject_gap_rate`：最近 12 个 terminal 槽（1 小时）内 data_gap 与
  data_reject 的占比；>0 记 degraded，>0.25 记 failed。
- `spread_sampling`：最近 12 个 terminal 槽中携带 `spread` 块的槽
  （无块视同 feature-ineligible 不罚）；sidecar 缺失、槽级
  degraded/unavailable 记 degraded；sidecar 校验失败或 digest 漂移一律
  fail closed。
- `tradingdatas`：经冻结 runtime manifest 与专用 token leaf 构造
  transport，读 `GET /v1/catalog` 并逐族做一次有界 `POST /v1/query`
  探活：10 个冻结 bar dataset 的 catalog 指纹漂移 fail closed；bars
  freshness 预算 1800s（6 根 5m bar），book_ticker 600s，
  open_interest 3600s（本仓尚无冻结 OI cadence 合同，只能 degrade 不能
  单独 fail）；catalog/query 不可达或 bar 族失败记 failed，其余族异常
  记 degraded。open_interest dataset 仅从实测 catalog 发现（
  `*.open_interest` 后缀），不猜 provider 名称。

### 输出与退出码

- 单份 JSON（contract
  `tradingagent.crypto.ten_symbol_health_watch.v1`）：总体
  `status ∈ ok/degraded/failed`（取各检查项最坏值）、每检查项
  status/reason_code/证据摘要、阈值回显与固定 authority 字段；authority
  字段漂移退出码直接为 2。
- 退出码：0=ok，1=degraded，2=failed 或任何异常（fail closed，stderr
  只输出固定脱敏文案）。
- 一次性 CLI：`python3 -m Crypto.ten_symbol_health_watch --store-root
  <path> --runtime-manifest <path> [--token-file <path>]`；store root
  必须等于 manifest 冻结的
  `/var/lib/tradingagent/crypto-ten-symbol-observation`，token leaf 固定
  为 `/run/secrets/tradingagent/tradingdatas-crypto-read.token`。**不接
  systemd**；timer 安装/启用须经 Nicholas 明确批准。
## 研究进化闭环 第二阶段（假设生成器，proposal-only detached 只读）

`ten_symbol_hypothesis_generator.py` 是研究进化闭环的第二阶段：一个离线、
只读、detached 的假设生成器。它把仓内冻结、版本化的生成配置
（`crypto-ten-symbol-hypothesis-generation-v1`：因子族 × 参数网格 ×
horizon）确定性展开为候选假设集，对每个候选做轻量可行性检查（所需数据面
可用性与样本量下限），产出一份 immutable 注册提案 artifact。第二阶段对通过
可行性检查的候选**自动注册**（`registration_status=auto_registered`、
`registered_into_prescreen/registered_into_evaluation=true`），blocked 候选
保持未注册（预筛与一阶段注册集合漂移仍 fail closed）；**不运行任何
预筛/评估**、不接 systemd；晋级在模拟域内自动，实盘仍由
`REAL_TRADING_ENABLED=false` 硬闸。

### 生成配置与因子族

- 配置冻结在模块内（先例同观测 profile）：五个 B 类因子族，每族参数网格
  ≤5 组，horizon 固定 12/48/144/288（60/240/720/1440min），共 23 个候选：
  - `oi_change_rate`（5 组：lookback_bars × oi_change_threshold，
    12/0.005、12/0.01、48/0.01、144/0.02、288/0.02）；
  - `price_oi_divergence`（5 组：lookback × direction∈fade/follow，
    12/48/288）；
  - `oi_weighted_momentum`（5 组：momentum_lookback × oi_lookback ×
    top_k∈{2,3}，12/48/288）；
  - `spread_regime`（4 组：regime_threshold_bps∈1.0/1.5/2.0 ×
    mode∈narrow_only/wide_only；阈值网格夹住实测点差中位数 ~1.06bps）；
  - `premium_momentum`（4 组：lookback × premium_threshold，
    12/0.0005、48/±0.001、288/0.002）。
- 配置校验严格（family 集合与顺序、模板占位符与参数键一一对应、variant
  唯一、horizon 为允许子集且排序、Decimal 文本可解析）：任何配置漂移 fail
  closed（`hypothesis_generator_config_drift`）；改动网格须换 config id 并
  走人工评审。
- 候选 id 形如 `<family>__<variant>`，与预筛/一阶段注册集合保证不相交。

### 可行性检查与数据面

- 数据面注册表冻结四个 plane：`ohlcv_bars`（观测 store bars sidecar，样本量
  由已验证事件链实测，为 10 币合并历史的最小行数）、`realized_spreads`
  （spreads sidecar + 点差投影 artifact，下限 12，镜像费用后评估日桶最低
  样本）、`open_interest_5m`（`crypto.perp.binance.<symbol>.open_interest`，
  下限 10000）、`premium_index`（`crypto.perp.binance.<symbol>.premium_index`，
  下限 200）。
- `ohlcv_bars` 下限按候选为 lookback + 13 + 最短 horizon（信号可计算且最
  短 horizon 标签可结算）；B 类 plane 只能由调用方显式传入的数据面证据
  manifest（contract
  `tradingagent.crypto.ten_symbol_hypothesis_generator_data_planes.v1`，严格
  shape 校验、available 必须带 sample_count、禁止声明 ohlcv_bars）声明；
  未声明 plane 一律 `unavailable`，`accumulating` 不算 available。
- 逐候选 `feasibility.status ∈ {feasible_for_auto_registration, blocked}`
  附逐 plane 检查（observed vs min、reason code）；可行性决定候选是否
  自动注册进预筛集合（feasible 才 `auto_registered`）。

### Artifact 合约

- 提案（contract
  `tradingagent.crypto.ten_symbol_hypothesis_generator_proposal.v2`）
  immutable 写入
  `<store_root>/evolution/ten_symbol_hypothesis_generator/proposals/{proposal_sha256}.json`，
  自含 `proposal_sha256`；内容包括：冻结配置全文与其 sha、store 链绑定
  （event 计数、head checksum、逐槽 terminal_units 聚合 sha、数据窗口）、
  数据面 manifest sha 与 plane 状态、逐候选定义（假设文本、依据、所需证据、
  参数、horizon、可行性、注册状态）以及自动
  `review.recommendation`（feasible 为 `auto_register`，blocked 为
  `blocked`）。
- compact `hypothesis_generator_checkpoint.json`（contract
  `tradingagent.crypto.ten_symbol_hypothesis_generator_checkpoint.v2`，原子
  覆写）绑定 `last_input_digest` 与 `proposal_sha256`。input digest 覆盖
  配置 sha、manifest sha、store 链 head 与逐槽资格材料：同输入重跑先重验
  checkpoint 与其绑定提案，返回 `no_new_input` 且字节不变（幂等）；
  checkpoint 或提案篡改 fail closed。无 terminal 事件返回
  `deferred_core_pending`，全部槽 ineligible 返回
  `insufficient_eligible_slots`（非错误）；退出码经
  `ten_symbol_hypothesis_generator_exit_code`（authority 字段或
  `automatic_registration` 不符即 2）。
- 一次性 CLI：`python3 -m Crypto.ten_symbol_hypothesis_generator
  --store-root <path> [--data-plane-manifest <path>]`；无 worker、无
  systemd unit。测试：
  `REAL_TRADING_ENABLED=false python3 -m pytest -q
  tests/test_crypto_ten_symbol_hypothesis_generator.py`。

## 验证

```bash
python3 scripts/validate_market_lane.py --lane crypto
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_fixture_auto_sim.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_five_minute_data.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_delayed_paper_runner.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_delayed_paper_runtime.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_delayed_paper_epoch.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_systemd_candidate.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_delayed_paper_learning.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_delayed_paper_learning_systemd.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_delayed_paper_exit_shadow.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_delayed_paper_health.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_delayed_paper_round_trip_health.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_delayed_paper_round_trip_learning.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_round_trip_learning_systemd.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_delayed_paper_round_trip_report.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_round_trip_acceptance_systemd.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_ten_symbol_observation_store.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_ten_symbol_observation_profile.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_ten_symbol_observation_runtime.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_ten_symbol_observation_sidecar.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_ten_symbol_factor_research.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_ten_symbol_factor_research_worker.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_ten_symbol_factor_strategy_evaluation.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_ten_symbol_spread_projection.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_ten_symbol_research_loop.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_ten_symbol_health_watch.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_*.py
```

## 明确未实现

- TradingDatas 18083、catalog、四个 dataset、专用 token leaf 和 TA 核心自动轮
  已有独立 readback；本 learning 候选不修改或重新部署其中任何一项；
- 候选回填的 `observed_at` 是采集时点，不是历史 PIT 或实时可用性证明；
- 核心 runtime 已有 sim-only 自动轮证据，但尚未通过连续 48 小时/90% 覆盖稳定性门禁；
- outage epoch 已在服务器以独立 generation-2 root 恢复自动轮；旧 root 保持
  只读封存，两个 epoch 的资本、收益和样本禁止聚合；
- learning worker/service/timer 在仓库中的 install-default 可以保持 disabled，production
  current state 只以 STATUS/state 为准；现有 G5 learning/scrub units 已运行，核心仍不会
  创建 `evolution/` 或执行学习恢复；
- 没有 Binance Spot Testnet/Live adapter、真实账户、密钥、User Data Stream 或外部订单；
- 现役 core Champion 仍只覆盖 deterministic buy/observe；round-trip
  generation 与 epoch-g3/g4 目前只是未部署候选，不代表现役 timer 已有卖出；
- fixture 测试结果不是收益率、胜率或晋级证据。

停止本地运行即可回滚本批候选行为；已经产生的 append-only 资本与复盘输出应保留作审计，不得改写为其它账户事实。

## 低换手基准与套息核算（research-only）

`research_accounting.py` 按固定币数量核算现货/USDT 线性合约：费用逐腿乘实际
成交名义金额、滑点改变成交价；同数量现货多头和合约空头的相同价格变动必须
抵消。`funded_hedge` 只做完全抵押的 long-spot/short-perp 研究，投入资本分母
包括现货开仓现金和全部合约抵押金，资金费只计 `(entry, exit]` 内真实结算事件，
支持毫秒结算时间，不按持仓小时摊算。调用方必须提供独立完整结算日程，缺事件、
价格缺口、重复事件或保证金路径风险时不出可用净收益。保证金只按采样 mark 检查，
不声称交易所级清算或真实成交。不得将其用作现役模拟账户 writer。

`forty_symbol_funding_carry_research.evaluate_settled_carry` 是新纯函数入口：输入
`prices[{time,spot,perp,mark}]`、`funding[{funding_time,funding_rate,mark_price}]`、
`expected_funding_times`、数量/两腿费用/滑点/抵押金/maintenance rate，以及
`source` 的五项数据面证据（spot/perp_trade/perp_mark/settled_funding/funding_schedule）。
缺证据返回 `data_unavailable`、净收益 null；source 仍是调用方来源绑定，不是独立
PIT/晋级认证。旧 `analyze` 的方向性 premium 模型显式标记不可用于真实套息结论。
基差旧模型 v2 虽修正为等币数量/逐腿费用，仍有合成 perp 等限制；旧报告不重写。

`slow_trend_research.py` 固定一套基准，而不是参数网格：20/60 日 SMA、20 日历史
波动率缩仓、每币目标权重最多 10%、不加杠杆、每日一次、25% 相对目标偏离带。
对照为同期间现金和全仓持有 BTC；两种投资策略均计入开平仓费用和终端清仓成本。
初始研究单位沿用 opening baseline，但结果不是任何当前账户收益。BTC 为全敞口，
低敞口策略回撤较低不能自动解释为 alpha。日收盘回撤不代表日内最大回撤。
日线只需 23:55 bar close 与 00:05 bar open，缺其它未使用日内行不会阻断该日；
必需时点缺失绝不前填。缺特征的单币空仓，其它币仍可评估；缺组合估值日不能拼接。

历史诊断选择最长连续可估值段，不按收益选择。固定前向窗口
`2026-08-31T00:00:00Z` 至 `2026-11-29T00:00:00Z`，结束前仅输出覆盖数量，
不输出收益。到期需完整 90 天；不足则报告 `fixed_window_incomplete_no_return_claim`，
不延期、不换参数再追认。正结果仍需 first-seen/PIT 和独立科学证据，不能从
offline rows 自签晋级。现有 K10 的定义、窗口及一次性读数完全不变。

```bash
# 已验证 raw cache 的无网络历史诊断：
REAL_TRADING_ENABLED=false python3 -m Crypto.slow_trend_research \
  --raw-dir /absolute/research/raw --as-of 2026-08-30T06:00:00Z

# 服务身份下的显式正式 API 研究取数；最多400天，每次100个精确时间点：
REAL_TRADING_ENABLED=false python3 -m Crypto.slow_trend_research \
  --runtime-manifest /etc/tradingagent/crypto-ten-symbol-observation.runtime.json \
  --start 2026-02-01T00:00:00Z --end 2026-08-30T00:00:00Z \
  --as-of 2026-08-30T06:00:00Z --report /absolute/new-research-report.json

REAL_TRADING_ENABLED=false python3 -m pytest -q \
  tests/test_crypto_research_accounting.py tests/test_crypto_slow_trend_research.py \
  tests/test_crypto_ten_symbol_research_loop.py tests/test_crypto_ten_symbol_time_split.py
```

复用原有研究循环时 v3 会自动附带基准、封存窗口覆盖和试验计数，但不会新增
timer/service；代码接入不等于自动调度已生效。回滚停止调用新研究入口，保留
v3 与旧 v1/v2 产物，切回原代码即可；不回写原始数据、账户、K10 或历史结论。
