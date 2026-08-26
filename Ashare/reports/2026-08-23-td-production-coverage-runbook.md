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
# 08-26 实测修正：受信密钥根是 /run/secrets/tradingagent（/etc 路径会报
# service_root_required）；且必须以 tradingagent 身份运行——token 属主校验
# 要求 uid∈{0,euid}（auth 模块 tradingdatas_token_owner_invalid），root 直接跑会失败。
OUT=/tmp/td_coverage_probe_$(date +%Y%m%d_%H%M)
su tradingagent -s /bin/sh -c "cd <服务器上的探针树> && python3 Ashare/event_td_coverage_probe.py --token-file /run/secrets/tradingagent/tradingdatas-read.token --out-dir $OUT --lookback-days 30"
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

- marketgraph-root SSH 为发布操作专用。**已批准的一次性例外（2026-08-24，Nicholas 批准）**：
  本 runbook 的探针命令可在维护窗口内经 marketgraph-root 执行一次——仅限本条命令、
  仅限本窗口、仅限只读；不得用于任何其它临时查询，事后不构成先例。
  背景：现有受限诊断账号（marketgraph 用户）读不到 `/etc/tradingagent/tradingdatas-read.token`，
  实测确认无法执行；更干净的长期方案（受限只读账号）随 #29 另行走审批。
- 全部输出 research_only / not_promotion_evidence；不授予任何资金或部署权限。

## 探针脚本的更新链路（决策层发布机制，08-24 凌晨实测）

探针文件随决策层代码树发布：改 `Ashare/event_td_coverage_probe.py` 后，合并进 `main`
**不会**自动出现在服务器上。上线唯一通道是 GitHub Actions
`deploy-production.yml`（仅 `repository_dispatch: controller-accepted-deploy` 触发；
服务器侧无定时拉取，勿与 18084 管理面的 td-admin-autodeploy 混淆）。

- **dispatch 要件**：payload 必须带 `client_payload{sha=<40hex>, test_run_id=<数字>}`，
  且被引用 run 同时满足 name=`TradingAgent Tests`、conclusion=success、head_branch=main、
  event=push、head_sha==sha，并存在未过期 artifact `tradingagent-release-<sha>`；
  缺任一项被 workflow 守卫秒拒。
- **G5 安全窗（预检本质）**：`tradingagent-release` 预检要求三个 g5 单元
  （acceptance / delayed-paper / health）同时 inactive。delayed-paper 每轮激活约 4 分钟、
  非激活窗仅约 60 秒——按「定时器触发瞬间」避让必撞激活期。可靠打法＝服务器侧轮询三单元
  is-active 与 systemd NextElapseUSecRealtime（该属性输出人类可读时间串而非 usec，
  解析取第 2–3 个 token 按 `%Y-%m-%d %H:%M:%S` 处理），满足「三者全 inactive 且距下次
  触发 ≥45s」即发射；整个 deploy run 约 36 秒。
- **2026-08-24 ~05:1x 实际执行记录**：三次尝试——① 未带 payload 被守卫拒绝；
  ② 撞 delayed-paper activating 窗口预检失败；③ 轮询自动发射成功。current →
  `23f8cf8`（含 #438），探针脚本在位验证通过，g5 服务全部恢复正常。

## 窗口记录

- **2026-08-24（周一）09:45 CST 后，15 分钟内**：Nicholas 批准窗口与一次性 root 例外；
  与 nicholashan-5a 早盘预检（约 09:42–09:45 结束）衔接，无并发冲突。执行后在本节回填
  实际执行时间、catalog_version 与 `forward_capable` 判定结果。

## 2026-08-26 夜间执行记录（Nicholas 批准重跑；回执被数据面锁风暴阻塞）

- **鉴权门两处实测修正**（详见上方命令块）：受信密钥根 `/run/secrets/tradingagent/`；
  探针须以 `tradingagent` 身份运行（`su tradingagent -s /bin/sh`），root 会撞
  `tradingdatas_token_owner_invalid`。08-24 的「受限账号读不到 /etc token」前提作废——
  正确路径下 tradingagent 身份可直接运行。
- **读接口可用性语义（实测确认）**：`/v1/query` 在写方持权威锁期间 fail-closed 返回
  503（快照打开要拿共享 flock＋epoch 校验，10s×5 次即放弃）；catalog 端点不受影响。
  采集器定时器每 5 分钟触发一轮、单轮常跑 2.5–4 分钟 → 查询窗口只在每轮结束后的
  空闲段（当晚约相位 240–300s/300s）。探针发射须守候「服务 inactive 且距下个
  触发点足够远」，否则必撞锁。
- **生产异常观察（移交维护窗口议程）**：20:10–20:35 观测到采集器单元 25 分钟启动
  5 次、且出现新旧实例并存（旧进程持锁未退出、下一轮又起），伴生间歇性
  exit-code 失败；该重叠使空闲窗缩到秒级，是当晚探针无法落回执的直接原因。
  另：`cn.news.flash` 自锁当日仍未解除（09:00 起统计 108 轮
  `invalid_receipt_authority / data_through_in_future` vs 仅 2 轮 success，
  至 20:30 最新轮仍被拒排）。
- **本次落地**：探针自身确定性缺陷修复（事件分区计数改注册表主键身份，同分支）；
  过夜守候脚本部署在发布主机 `/tmp/td_overnight.sh`（仅凌晨 01:30–05:00 低谷期
  发射、只读、写 `--out-dir` 后自退），回执预期次晨取回归档。lookback 用 7 天
  （足以回答消费端取数与 forward 判定，缩短锁窗内暴露时间）。
