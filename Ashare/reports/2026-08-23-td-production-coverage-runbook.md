# TD 生产覆盖验证 Runbook（维护窗口用，research_only）

- 制定日期：2026-08-23。性质：只读核对流程，不写任何业务数据面，不构成晋级证据。
- 目的：在发布主机本机实测 TradingDatas 生产数据面的真实覆盖，回答三件事：
  1. 两个事件数据集（`share_float` / `disclosure_date`）的行覆盖是否支撑全市场日历（#414 消费端）；
  2. `cn_schedule` 是否含未来 `publish_date` 行——决定宏观日历消费端能否立项；
  3. 其余四个宏观数据集（CPI/PMI/GDP/M）的月度行量与字段形态。
- 执行前提（缺一不可）：
  - Nicholas 与两个会话（本会话 + nicholashan-5a）三方确认的维护窗口；
  - 发布主机上 0600 权限的 TD 读 token 文件；
  - 窗口内无其它 TD 写入/发布操作并发。

## 执行（单条命令）

```bash
python3 Ashare/event_td_coverage_probe.py \
  --token-file /etc/tradingagent/tradingdatas-read.token \
  --out-dir /tmp/td_coverage_probe_$(date +%Y%m%d_%H%M) --lookback-days 30
```

- 脚本行为：GET `/v1/catalog` 校验 7 个数据集 → 钉死 catalog_version → 事件集按
  `ann_date` 分区逐日计数（默认 30 天）→ 宏观集按月整月分页读取（近两个月 +
  `cn_schedule` 下个月）→ 写出 `coverage_receipt.json` + `coverage_view.md`。
- 只读边界：除 `--out-dir` 外零写入；不触碰 journal/ledger；异常记录进回执而不中断，
  仅目录缺失/事件集不可查/鉴权失败会以非零码退出。

## 结果判读

| 观察点 | 达标 | 不达标的含义 |
|---|---|---|
| 事件集 `rows_total>0` 且空天少 | 覆盖成立 | 全市场日历暂不可信，维持 Tushare 口径 |
| 事件集每日行量 vs Tushare 基线量级 | 同量级 | 记录差异，评估采集完整性后再切换 |
| `cn_schedule.forward_capable=true` | 可立项宏观未来日历 | 只能做已发布宏观读数，不做前瞻 |
| 宏观集月行数与首行字段 | 形态符合注册表 | 按 `first_rows` 样本修正消费端假设 |

## 留痕与同步

- 回执 JSON + view md 属过程性产物：归档到本次窗口的报告目录并在研究报告中补一节「生产覆盖首测」。
- 若 forward_capable 为真：下一步才建 `cn_schedule` 宏观日历消费端（复用 #414 模式）；
  为假则在报告中记录并关闭该方向。

## 边界

- marketgraph-root SSH 为发布操作专用，本验证不得使用它做临时查询；token 使用现有只读凭据。
- 全部输出 research_only / not_promotion_evidence；不授予任何资金或部署权限。
