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
  `catalog_contract_sha256`；
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

`delayed_paper_learning_worker.py` 是独立离线候选；核心 runtime 不 import、
调用或恢复它，也不读取或创建 `evolution/`。学习 worker 只异步消费核心已完成的
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

增量模式只读取核心当前 completion state、自己的 checkpoint 头，以及至多一条
新增 completion 对应的 receipt/segments，正常单轮工作量与新增量相关。若 worker
落后超过一条，它返回 `full_scrub_required`，不在每 5 分钟路径扫描历史。写入
projection receipt 后再写 append-only checkpoint，最后原子更新
`worker_state.json`；checkpoint 已落盘而 state 尚未更新的崩溃可确定性恢复。

每日 full scrub 独立遍历全部
`completion → projection receipt → sample/KPI/Challenger segments` 和完整
checkpoint checksum 链。没有 checkpoint 声明的缺失投影可从核心权威证据补齐；
一旦 checkpoint 已声明成功，较早 receipt 缺失、旧 segment 篡改、completion
绑定不一致或 checkpoint 断链都必须失败关闭，不能用较新的成功覆盖。
Challenger 只追加建议且 `manual_review_required=true`，不得自动替换 Champion、
扩大风险、修改资本/仓位/订单或进入 Testnet/Live，也不调用网络模型。

tracked 候选包含：

- `tradingagent-crypto-delayed-paper-learning.service/.timer`：每根核心 5 分钟
  轮次之后独立运行增量 worker；
- `tradingagent-crypto-delayed-paper-learning-scrub.service/.timer`：每日独立
  full scrub。

两组 learning timer 均由仓库保持默认未启用。发布侧可先安装 unit、创建现役
epoch 的 `evolution/`、执行 disabled one-shot 与 full scrub；只有核心连续
24 小时门禁通过并经主集成复核后才可 enable timer。学习失败不得改变核心
status、exit code、资本或订单。

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

`delayed_paper_round_trip_runtime.py` 是隔离 round-trip epoch 的唯一 closed-5m
server wrapper。
它仅复用已冻结的 TradingDatas manifest、token-file transport 与 13-bar 门禁，
每次先后校验 epoch identity 与旧 g2 archive，再运行一个新/待恢复 observation。
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
策略晋级结论。

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
`production_eligible=false`、`manual_review_required=true`，没有自动 Champion
替换或风险扩张。

incremental worker 最多处理一条新增 completion，且必须先有一次成功 full scrub；
有 backlog 时只返回 `full_scrub_required`。full scrub 遍历所有
completion→receipt→checkpoint 映射；已被较早 checkpoint 声明的 receipt/segment
若缺失或变更，必须 fail closed，绝不重建。tracked G4 learning 与 daily scrub unit
静态绑定当前 G4 epoch，并只能写入该 epoch 的 `evolution/`。两组 timer 默认
disabled：先满足连续 24 小时核心门禁，再做 disabled full-scrub 与幂等重放，才可
人工启用 incremental timer；daily scrub 继续作为独立人工门禁，本次仓库变更不会
启用任何 learning timer。发布 preflight 必须先由受控安装步骤创建该空目录并读回
`tradingagent:tradingagent`、0700、非 symlink；unit 与 worker 都不会借缺失目录
创建或迁移核心 epoch。

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
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_*.py
```

## 明确未实现

- TradingDatas 18083、catalog、四个 dataset、专用 token leaf 和 TA 核心自动轮
  已有独立 readback；本 learning 候选不修改或重新部署其中任何一项；
- 候选回填的 `observed_at` 是采集时点，不是历史 PIT 或实时可用性证明；
- 核心 runtime 已有 sim-only 自动轮证据，但尚未通过连续 24 小时稳定性门禁；
- outage epoch 已在服务器以独立 generation-2 root 恢复自动轮；旧 root 保持
  只读封存，两个 epoch 的资本、收益和样本禁止聚合；
- 离线学习 worker/service/timer 仍是未部署候选，核心不会创建 `evolution/`
  或执行学习恢复；
- 没有 Binance Spot Testnet/Live adapter、真实账户、密钥、User Data Stream 或外部订单；
- 现役 core Champion 仍只覆盖 deterministic buy/observe；round-trip
  generation 与 epoch-g3/g4 目前只是未部署候选，不代表现役 timer 已有卖出；
- fixture 测试结果不是收益率、胜率或晋级证据。

停止本地运行即可回滚本批候选行为；已经产生的 append-only 资本与复盘输出应保留作审计，不得改写为其它账户事实。
