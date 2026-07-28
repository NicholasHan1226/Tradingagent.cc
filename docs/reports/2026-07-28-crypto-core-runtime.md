# Crypto 核心自动模拟运行回执

> 日期：2026-07-28 CST。本报告只记录 BTCUSDT/ETHUSDT 的 5 分钟
> simulation-only 核心。它不授权 broker、Testnet、Live、模型网络、自动晋级、
> 风险扩张或真实交易。

## 结论

Crypto 5 分钟核心已按“核心先跑、学习解耦”完成代码合并、不可变发布、一次
one-shot、同窗口幂等重放和两个相邻自动轮次。核心只负责 TradingDatas
Evidence Gate、候选、Decimal 模拟成交、资本守恒、Decision Ledger、对账与恢复；
离线学习没有进入 5 分钟退出状态。

当前 timer 为 `enabled/active`，系统已经开始 24×7 积累 sim-only 数据与决策样本。
24 小时连续观察仍在进行，因此尚不能宣称长期工程稳定性通过。

## 代码与发布

- PR：`#62`
- merge/main：`e8ba46d7e0cab847d0fa037290e7368c69c54655`
- release tree：`80eae3ba75263d7c55ebe9135edd7d27edb8dc0c`
- GitHub CI：`front=SUCCESS`、`test=SUCCESS`
- release：
  `/opt/investment/releases/tradingagent/e8ba46d7e0cab847d0fa037290e7368c69c54655`
- current：同上
- 直接代码回滚点：
  `/opt/investment/releases/tradingagent/b4f5d600f3d8bb317375a05b2f613e8a06e89c52`
- release：810 个 regular file、0 symlink、0 writable

## TradingDatas 合同

- base URL：`http://127.0.0.1:18083`
- API：仅 `GET /v1/catalog` 与 `POST /v1/query`
- catalog：`v1-e7ea3dd714066d3c`
- dataset：BTCUSDT/ETHUSDT 各一个 closed-5m 与一个 rules，共 4 项
- profile SHA：
  `4f5bb40106cf2f63b25a784acae0f13072112afca98dd380e11dab66e19fbe38`
- manifest：`root:tradingagent 0640`、regular、single-link、无 secret
- token leaf：`tradingagent:tradingagent 0600`、regular、single-link
- credential 值与哈希未进入 Git、manifest、日志、消息或本报告
- 无 SQLite、8082、Binance 直连或 provider route fallback

初次部署门禁发现旧 token leaf 含不符合 TA 原始单行 bearer 合同的格式。代码没有
放宽；TradingDatas credential owner 在服务器内原子重装同 scope 的规范 leaf，
保留 root-only 回滚副本。随后 UID 987 使用 TA 权威
`build_runtime_transport` 新鲜读回 catalog HTTP 200。

## One-shot 与幂等

- one-shot requested window end：`2026-07-28T15:20:00Z`
- one-shot market slot：`2026-07-28T15:15:00Z`
- service exit：0
- 初始账户：`10,000 USDT`
- 同窗口重放：`status=noop`
- 重放网络访问：false
- 重放前后非锁文件数：13 / 13
- 重放前后组合 SHA：
  `ec1646906218627a8b9122d5fa201618685556e7eb8be841d349a928e10e5350`
- 重复成交：0
- 学习调用：false

## 两个相邻自动轮次

timer 固定为每个 UTC 5 分钟边界后 55–58 秒触发，给 TradingDatas closed-bar
collector 留出时间。

| 来源 | 市场槽 | 结果 |
|---|---|---|
| 手工 one-shot | `15:15Z` | PASS |
| 自动轮次 1 | `15:20Z` | PASS |
| 自动轮次 2 | `15:25Z` | PASS |

首个两个相邻自动轮次冻结检查点：

- observations：3
- completions：3
- pending：null
- capital events：17，sequence 连续、event ID 唯一
- simulated fill events：2，来自不同窗口
- decision events：6，sequence 连续、event ID 唯一
- `evolution/`：不存在
- service result：success / exit 0
- Crypto timer：enabled/active

随后只读审计确认自动轮已继续到 `15:30Z`：

- observations / completions：4 / 4
- pending：null
- capital events：21，sequence 连续、event ID 唯一
- decision events：8，sequence 连续、event ID 唯一
- 全部运行快照：`balanced=true`
- 全部运行快照：`real_trading_enabled=false`、
  `execution_authority=false`、`production_eligible=false`
- `evolution/`：仍不存在

## 安全边界

- `REAL_TRADING_ENABLED=false`
- `execution_authority=false`
- `production_eligible=false`
- broker network：false
- Testnet/Live：false
- model network：false
- automatic promotion：false
- automatic risk expansion：false
- learning mode：`detached_offline_worker`
- learning worker：尚未部署

A股 `minute-paper` 与 `minute-session` timers 继续 enabled/active；四个现役 A股
unit SHA 在发布前后完全一致。

## 证据与回滚

非敏感服务器证据：

`/opt/investment/release-evidence/tradingagent/20260728T151230Z-crypto-core-e8ba46d7e0cab847d0fa037290e7368c69c54655`

其中：

- `stage-receipt.txt` SHA：
  `596f29c1007d708a5e369553fafecd49318939e7b32899bfacbde1ad448fa091`
- `runtime-receipt.txt` SHA：
  `edd796815d0640b509c75038c779b45609d09fd81da2f8556c16c9b88f69212f`

首选运行回滚只停止 Crypto，不删除任何 append-only 数据：

```bash
systemctl disable --now tradingagent-crypto-delayed-paper.timer
```

如还需回退代码，停止 Crypto service 后再将 `current` 原子指回
`b4f5d600f3d8bb317375a05b2f613e8a06e89c52`。不得删除
`/var/lib/tradingagent/crypto-delayed-paper`。

## 下一停止线

1. 连续运行 24 小时，要求零重复成交、零账实差异、零
   Live/Testnet/模型网络调用。
2. 学习 worker 作为独立 PR 实现增量 checkpoint 与每日 full scrub；任何学习失败
   不得改变核心退出状态。
3. Challenger 只生成建议，必须人工晋级。
4. A股离线学习必须等待 2026-07-29 完整 48/48 时槽、零数据拒绝和资金/持仓/订单
   守恒后再启 timer。
