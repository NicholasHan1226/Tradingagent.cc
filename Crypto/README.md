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

本批 `fixture_auto_sim.py` 是 `crypto-capital-v1` 本地 fixture opening 闭环的唯一可写入口，但它仍是非权威候选。旧 `workflow.py`、`simulator.py`、`sim_executor.py` 与 `shadow_runner.py` 已变为无条件 fail-closed tombstone，不能通过注入 reader、切换配置或恢复旧 authority 重新启用。`promotion.py` 只保留只读研究 scorecard，永久输出不可自动晋级；shared governance 已把 `crypto-shadow-sim-v1` 降为历史证据并登记 `crypto-capital-v1` 为 `local_fixture_simulated_candidate`，不构成 current/runtime/live authority。

资本链 checksum、进程锁和 package-private writer capability 只防止正常调用误写、协作进程冲突与常见落盘损坏，不是抵御可修改同一 Python 进程、代码或账本文件的恶意主体的安全边界。默认构造的 ledger 只读，writer 仅由 fixture runtime 内部工厂创建；但拥有相同用户文件写权限的恶意或失控进程仍可能改写并重算本地链。未来获得任何生产资本权威前，必须另行验证进程隔离、运行 UID/GID、目录 owner/mode/ACL、只允许单一 writer 以及外部 durable receipt；本地链不得被当作密码学签名或 broker attestation。

LLM sidecar 在核心资本 cycle lock 释放后独立追加，并有 1 MiB 本地读取上界。sidecar 损坏、超限或写入失败只返回 `degraded/authority=none` 诊断，不撤销、重复或阻塞已提交的核心资本与 bundle replay。

## 验证

```bash
python3 scripts/validate_market_lane.py --lane crypto
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_fixture_auto_sim.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_five_minute_data.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_delayed_paper_runner.py
REAL_TRADING_ENABLED=false python3 -m pytest -q tests/test_crypto_*.py
```

## 明确未实现

- TradingDatas 数据合同代码虽已在 `main@62d76f8…`，但没有 Crypto internal
  HTTP/runtime/timer、endpoint/port、带认证 transport 或正式 catalog/query
  receipt readback；Git main 本身不是可调用的数据服务；
- 候选回填的 `observed_at` 是采集时点，不是历史 PIT 或实时可用性证明；
- 没有 scheduler、常驻 daemon、持续 24x7 runtime 或服务器安装态；当前是可被
  外部安全调度器逐 closed-5m window 调用并恢复的本地 one-cycle runner；
- 没有 Binance Spot Testnet/Live adapter、真实账户、密钥、User Data Stream 或外部订单；
- 本批 Champion 只覆盖 deterministic buy/observe paper 样本，尚不是完整买卖 round trip；
- fixture 测试结果不是收益率、胜率或晋级证据。

停止本地运行即可回滚本批候选行为；已经产生的 append-only 资本与复盘输出应保留作审计，不得改写为其它账户事实。
