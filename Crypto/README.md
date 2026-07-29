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
`main@62d76f8cdcc7671a9523ac15905ab2eb3152e387`。此前 isolated canary
`025fd24e2f9f33855b6d2f62ac6489d219033128`（catalog
`v1-e7ea3dd714066d3c`）提供本地查询证据；但目前仍没有正式 Crypto internal
HTTP/runtime/timer、endpoint/port 或带认证 readback handoff。因此以下四个 ID
仍只可在本批显式 fixture/profile 注入中出现，不能据此声称 TA 已获得正式数据：

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

`delayed_paper_runtime.py` 是最小 loopback-only server CLI 候选。它尚未部署，
也不包含正式 catalog、dataset profile、token 或 base URL。实际运行必须由主任务
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

token 和输出根不可改写：

- `/run/secrets/tradingagent/tradingdatas-crypto-read.token`
- `/var/lib/tradingagent/crypto-delayed-paper`

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
不表示离线学习 worker 已实现或运行。

候选命令形态为：

```bash
export REAL_TRADING_ENABLED=false
python3 -m Crypto.delayed_paper_runtime \
  --runtime-manifest /etc/tradingagent/crypto-delayed-paper.runtime.json \
  --token-file /run/secrets/tradingagent/tradingdatas-crypto-read.token \
  --output-root /var/lib/tradingagent/crypto-delayed-paper
```

仓库只跟踪：

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

timer 为 24×7、每 5 分钟边界后 55–58 秒触发的候选，`Persistent=false`，包含
`[Install]`/`WantedBy=timers.target`，供发布侧在完整门禁通过后显式
`systemctl enable --now`。Git 仓库默认不会安装、enable 或 start；本批也不改
服务器。2026-07-28 的正式上游 handoff 已给出 loopback
`http://127.0.0.1:18083`、catalog `v1-e7ea3dd714066d3c`、四个
schema-major-1 dataset 的 ready/fresh/valid/non-degraded query 证据，以及
`/run/secrets/tradingagent/tradingdatas-crypto-read.token` 为
`tradingagent:0600`；这些是部署输入，不是本 unit 已安装的证明。正式部署仍须由
主任务生成并读回完整仓外 profile manifest（含 access policy、profile 与 contract
SHA），运行 Linux `systemd-analyze verify/calendar`，在 enable 前读回
disabled/inactive、enable 后读回 enabled/active，并证明同一 service UID 下不同
token leaf 的 OS 级隔离。

## 学习解耦边界

本核心候选不包含离线学习 worker 或学习测试，也不生成 `evolution/`。未来学习
实现只能异步消费核心 append-only observation/completion/decision/capital
证据，并拥有独立的 checkpoint、完整性审计、资源预算和调度。它必须保持
`learning_authority=false`，不得自动替换 Champion、扩大风险、修改资本或进入
Testnet/Live。历史完整性检查与 Challenger 建议属于离线 worker 验收，不能重新
进入每 5 分钟核心路径。

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
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_systemd_candidate.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_*.py
```

## 明确未实现

- TradingDatas 已交接正式 Crypto 18083 loopback、catalog version、四个 dataset
  query 与专用 token leaf 的上游 readback；TradingAgent 本批没有生成/安装仓外
  runtime manifest，也没有以本 CLI 做联合 readback，不能把 TD 上游成功说成 TA
  runtime 已运行；
- 候选回填的 `observed_at` 是采集时点，不是历史 PIT 或实时可用性证明；
- 仓库已有 runtime CLI 和默认 disabled 的 tracked service/timer 候选，但没有
  服务器安装态、enabled/active timer 或成功自动轮；文件存在不等于持续运行；
- 离线学习 worker 未纳入本核心候选，核心不会创建 `evolution/` 或执行学习恢复；
- 没有 Binance Spot Testnet/Live adapter、真实账户、密钥、User Data Stream 或外部订单；
- 本批 Champion 只覆盖 deterministic buy/observe paper 样本，尚不是完整买卖 round trip；
- fixture 测试结果不是收益率、胜率或晋级证据。

停止本地运行即可回滚本批候选行为；已经产生的 append-only 资本与复盘输出应保留作审计，不得改写为其它账户事实。
