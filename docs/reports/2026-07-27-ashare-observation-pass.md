# A股动态 manifest 与 observation PASS

> 日期：2026-07-27 CST。本报告记录 TradingDatas freshness 修正后的 TA
> observation-only 读回。它不授权 worker、timer、模拟成交或真实交易。

## 结论

TradingDatas 将纯日期盘后分区的 freshness 参考点从交易日开始修正为交易日结束后，
`cn.equity.daily` 在 TA 专用身份下恢复为
`ready/success/fresh/valid/degraded=false`。TA 没有放宽 Evidence Gate，也没有
修改 TradingDatas。

动态 builder 随后发布内容寻址 manifest；TA 使用该 manifest 在隔离 state root
执行一次 current observation，并在同 root 精确重放。两次运行均 PASS，第二次为
幂等重放。全过程保持 `REAL_TRADING_ENABLED=false`。

## TradingDatas handoff

- production release：
  `98fa9489c4c8e960d392487c99b06d59e3db8f76`
- base URL：`http://127.0.0.1:18082`
- catalog：`v1-3c18b5d842eedfb2`
- catalog counts：190 total / 100 active / 90 paused
- daily filter：`trade_date eq 20260724`
- daily metadata：
  `ready/success/fresh/valid/degraded=false`
- receipt、lineage、data_through、observed_at：完整
- TradingDatas collector：inactive
- TradingDatas timer：disabled
- `cn.dataset.sw_daily`：继续独立 fail closed

## 动态 manifest

- TA release：
  `724ea8818feff142df57c4a7bf7b558e29ec0a35`
- evidence：
  `/opt/investment/release-evidence/tradingagent/20260727T123136Z-ta-manifest-pass-724ea88`
- observation session：`20260724`
- active contract SHA：
  `3ae63abd22540312489aa101388a59ad790db853cf848ca28167131e7e653eaf`
- manifest SHA：
  `7e5bdc5dd75cc4cd33a1a1bb80b66645c34cd2e4ef4cee08612e26e2bdf09d1f`
- `historical_pit_eligible=false`
- `execution_authority=false`
- `simulation_started=false`
- manifest 文件：`tradingagent:tradingagent 0600`

## Observation one-shot

- evidence：
  `/opt/investment/release-evidence/tradingagent/20260727T123136Z-ta-observation-one-shot-724ea88`
- mode：`observation_only`
- MarketGraph：`mg_off`
- observation Universe：3041 只沪深主板标的
- excluded：2569 条
- context roles：空；未伪造行业宽度
- same-as-of：匹配
- 首次运行：`status=pass`、`idempotent_replay=false`
- 同 root 重放：`status=pass`、`idempotent_replay=true`
- snapshot、Universe、ledger、receipt、transaction-complete SHA：两次一致
- `execution_authority=false`
- `historical_pit_eligible=false`
- `REAL_TRADING_ENABLED=false`

主要排除项包括：创业板 1398、科创板 611、北交所 330、风险警示 147、B 股
78；此外有 1 条 daily 缺失和少量新上市或非第一阶段标的。这些标的仅从个股
Universe 排除，健康的指数与行业汇总未来仍可按 `context_only` 单独接入。

## 未完成

- 没有安装 tracked worker unit、secret-free env 或 `current` 指针；
- 没有启 worker 或 timer；
- 没有运行 ranking、forecast、TargetPosition、Risk、PaperFill 或 Reconcile；
- 没有历史 PIT/revision authority；
- 没有可用行业宽度；
- 仍只有一个正式 forward-collected 交易会话，不能训练或宣称预测有效。

下一步必须先完成 inactive/static unit 的安装与 unit one-shot/失败恢复验证；timer
与自动 paper 闭环分别进入后续独立门禁。
