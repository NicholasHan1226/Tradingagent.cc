# TradingAgent 状态

> **给所有 agent：** 读完 [AGENTS.md](AGENTS.md) 理解规则后，读本文件理解"现在在哪、要去哪、能做什么"。
>
> **⚠️ 变更后必须更新本文件。**
>
> 最后更新：2026-07-02 (多市场 promotion tier 命名统一)

---

## 一、当前状态

- **A 股影子盘**：完整闭环运行（信号生成 → 影子账簿 → 复盘）
- **A 股模拟盘**：通过 Mac Mini Hermes 执行，收/发/回执链路已修复
- **执行桥**：Mac Mini `~/.hermes/` 下 Hermes 正常运行，只执行和回写，不做买卖判断；live runtime 为 `~/.hermes/ashare-runtime`，服务器回写为 `/opt/investment/tradingagent/signals`；mini live 脚本默认值也已改到新路径
- **PM（预测市场）**：影子盘每 10 分钟扫描运行；checked-in config 使用 USDC；PM shadow 写入 `signals/shadow/pending`
- **多市场**：PM/Crypto/US/HK sim executor 和 config schema 已加真实执行拒绝；Crypto/US/HK Phase D P0 工具已独立实现；US/HK P1 report/validation/promotion 工具已补齐；Crypto/PM P1 report/validation/promotion 工具已补齐
- **复盘节奏**：11:45 午盘 / 15:30 收盘 / 22:00 夜间校准 / 07:30 晨报
- **服务端**：杭州 `8.138.181.177`，生产路径 `/opt/investment/tradingagent/`
- **运行监控**：每小时运维报告（`ops_report.py`），覆盖执行队列、影子队列、回执完整性、PnL 摘要

## 二、已知问题

- Crypto/US/HK/PM 模拟盘完整生产闭环未验证（仅 A 股链路完整；多市场 P1 工具为本地模块级验证）
- 多市场代码依赖旧系统 symlink，已全部清除（61 个死 symlink），待独立实现
- 集合竞价支持标记为 STUB，未实现
- A 股实盘路径仍是人工（模拟盘信号 → 邮件发用户 → 手动执行）

## 三、下一步

1. [ ] **P2：Crypto/PM 多市场工具独立实现** — 继续从 manifest.csv 占位工具中补齐剩余 P2 能力
2. [ ] **P2：多市场模拟盘闭环** — 各自独立，不再依赖旧系统 symlink
3. [ ] **P2：A 股实盘路径设计** — 需先确认安全边界和人工确认环节
4. [x] **P2：SharedSignals HTTP API 消费迁移** — `TradingagentDataReader` 已对 `get_market_data` / `get_events` / `is_trading_day` 接入 API-first 访问；SQLite 只读回退保留

## 四、活跃任务

（当前无活跃迁移任务）

## 五、最近完成（2026-07-02）

### 多市场 promotion tier 命名统一（2026-07-02）

- [x] `Crypto/promotion.py`、`PM/promotion.py` 统一为 `research -> shadow_candidate -> shadow -> sim_candidate -> sim`，与 US/HK 命名一致。
- [x] Crypto/PM `eligible_for_sim` 与 `target_layer=simulated` 改为只在 `tier=sim` 时成立。
- [x] 更新 Crypto/PM P1 测试断言，覆盖统一五档 tier 名。
- [x] 验证：`tests/test_crypto_p1_tools.py`、`tests/test_pm_p1_tools.py`、`tests/test_us_hk_p1_tools.py` 共 17 项通过；完整 `python3 -m pytest tests/ -q --tb=line` 187 项通过。

### SharedSignals HTTP API 消费迁移（2026-07-02）

- [x] `TradingagentDataReader` 新增 `api_client` 参数；配置 `SHAREDSIGNALS_API_URL` 时自动创建 `SharedSignalsAPIClient`。
- [x] `get_market_data` / `get_events` / `is_trading_day` 优先走 SharedSignals HTTP API；API 不可用时回退 SQLite 只读路径并设置 `degraded=True`。
- [x] `SharedSignalsAPIClient` 移除 deprecated 状态，校准 15 个当前 API server 端点，补充 timeout / retry / backoff 配置，去除 `X-API-Key` 双重暴露。
- [x] `.env.example` 新增 `SHAREDSIGNALS_API_URL`（默认空，直接走 SQLite）、`SHAREDSIGNALS_API_KEY`、timeout/retry 配置。
- [x] 验证：`py_compile`、导入 smoke、`tests/test_data_reader.py` 通过。

### 多市场 P1 Codex review 修复（2026-07-02）

- [x] `PM/report.py`：`_outcome()` 改为显式 outcome 白名单，`cancelled`、`void`、`pending`、`unresolved` 等未知/未决状态不再计为 resolved YES。
- [x] `Crypto/validation.py`、`PM/validation.py`：OOS 日期比较前统一规整为 `YYYYMMDD`，兼容 `2026-07-02`、`20260702`、`2026-7-2`。
- [x] `US/validation.py`、`HK/validation.py`：补齐 `train_end` OOS 过滤，排除训练期及晚于 `as_of` 的记录；`_to_float()` 增加 NaN 防护。
- [x] 测试覆盖 PM 未决 outcome、Crypto/PM 日期格式规整、US/HK OOS 过滤和 NaN 防护；指定 P1 pytest 与变更文件 `py_compile` 通过。

### Crypto/PM P1 工具（2026-07-02）

- [x] `Crypto/report.py`：新增 `CryptoDailyReport(BaseReport)`，生成每日 shadow 复盘并执行 no-empty-trigger 规则，未触发时返回 no-send。
- [x] `Crypto/validation.py`：新增 `CryptoForwardValidation`，计算 OOS win rate、PnL、direction hit rate 与样本质量评分。
- [x] `Crypto/promotion.py`：新增 `CryptoStrategyPromotion`，提供 5-tier shadow→sim 晋级门。
- [x] `PM/report.py`：新增 `PMDailyReport(BaseReport)`，生成每日 Brier + PnL shadow 报告。
- [x] `PM/validation.py`：新增 `PMForwardValidation`，计算 OOS Brier、PnL 与校准分箱。
- [x] `PM/promotion.py`：新增 `PMStrategyPromotion`，提供 research→shadow→sim 晋级门。
- [x] 所有 P1 工具保持 shadow/sim 边界，拒绝 real/live/direct execution 配置或负载。
- [x] 新增 `tests/test_crypto_p1_tools.py` 与 `tests/test_pm_p1_tools.py`，Crypto/PM 各 4 项测试；`py_compile`、`compileall`、Crypto/PM P0 回归测试通过。

### US/HK P1 工具（2026-07-02）

- [x] `US/report.py` / `HK/report.py`：新增 Markdown 日度 shadow 报告；HK 报告包含 lot size；默认只渲染不发送。
- [x] `US/validation.py` / `HK/validation.py`：新增 OOS 前向验证；US 覆盖 earnings/momentum funnel，HK 使用 HKD 口径。
- [x] `US/promotion.py` / `HK/promotion.py`：新增 5-tier `research -> shadow_candidate -> shadow -> sim_candidate -> sim` 策略晋级分类。
- [x] 所有 P1 工具维持 shadow/sim 边界，拒绝 real/live/direct execution 配置或负载。
- [x] 新增 `tests/test_us_hk_p1_tools.py`，US/HK 各 3 项测试；`py_compile`、US/HK P0 回归和 diff 检查通过。

### R8/R9 多市场安全修复（2026-07-02）

- [x] `PM/config.yaml` currency 从 USD 修正为 USDC，并补充 `PMWorkflow()` 读取 checked-in config 的回归测试。
- [x] `PM/shadow_runner.py` 改为通过 `SignalStateMachine(signals_root / "shadow").write_pending()` 写入 `signals/shadow/pending`。
- [x] `shared/markets/safety.py`、`base_tools.py`、`config_schema.py` 增加真实执行/live broker/direct execution 拒绝；`execute_sim_order()` 不再把真实负载静默改写成 simulated 后派发。
- [x] `PM/simulator.py` 拒绝 real order，fill 结果固定返回 `capital_layer=simulated`、`account_type=simulated`。
- [x] `PM/Crypto/US` sim executor 入口新增真实执行负载拒绝；修复 `US/sim_executor.py` 删除 account 后再访问的 `UnboundLocalError`。
- [x] `HK/workflow.py` 补齐 `HKWorkflow` / `run_hk_shadow_cycle()`，对齐 US workflow 模式。
- [x] 新增/更新测试覆盖 PM config load、HK workflow smoke、US/HK live broker rejection、sim executor safety。

### Mini/服务器执行桥路径修复（2026-07-02）

- [x] 禁用旧 `ai.hermes.sim-remote-sync`，停止 `~/Desktop/Investment/Ashare/outputs/account` 被周期性重建。
- [x] 禁用旧 `ai.hermes.condition-cleanup`，停止访问已退役的 `~/Desktop/Investment` tradebook 清理路径。
- [x] Mac Mini executor 明确设置 `SIM_REMOTE_TRADINGS_SIGNAL_DIR=/opt/investment/tradingagent/signals`。
- [x] 服务器合并 GitHub 最新 main，修复 `TradingagentDataReader` 导入/导出和 reader 回归；`tests/test_data_reader.py` 通过。
- [x] `shared/execution/execution_router.py` 已提交：A 股 sim broker 正式通过仓库内 `Ashare/sim_executor.py`，不再依赖旧 `/opt/investment/Ashare/tools`。
- [x] Mac Mini live executor / receiver / health-check 默认路径已改到 `~/.hermes/ashare-runtime` 与 `/opt/investment/tradingagent/signals`。
- [x] 清理 `mini/README.md`、`mini/mini_consumer.py`、`Ashare/AGENTS.md` 等参考副本里的误导性旧执行器路径。
- [x] 归档 Mac Mini 非活跃旧脚本/备份/禁用 LaunchAgent；active scripts、LaunchAgents、crontab 旧路径审计为 0 命中。
- [x] 记录事件日志：[docs/runtime_incidents_20260702.md](docs/runtime_incidents_20260702.md)。

**残余风险：**
- 未在交易时段发送测试交易信号；完整端到端验证需等交易时段。

### Goal 2 审计 — SharedSignals → TradingAgent → MarketGraph 数据流

**2 轮审计，10 维度，46 发现，10 项修复全部完成。**

**Round 1（5 agents，17 发现）：**
- API 客户端：`is_trading_day()` 默认返回 False（fail-safe）、API sentinel→TTL 恢复、4xx/5xx 重试分化
- 健康检查：sockstat 端口检测替代 HTTP health check（30s SIGALRM 超时）
- 配置一致性：端口 8082/8900 不一致修复、MarketGraph `.env.example` port 更新

**Round 2（5 agents，29 发现，5 维度）：**
- 数据新鲜度：LRU cache 无 TTL、naive `datetime.now()`（20+ 处）、无每日管线 fallback cron
- 错误传播：dead `api` property、`errors`/`stale` 从未消费、SQLite 错误静默吞掉、无死人手刹
- 配置漂移：`SHAREDSIGNALS_ROOT` 指向错误、端口 8900/8082 不一致、`MARKETGRAPH_ENV_FILE` 路径冲突、15 个未文档化环境变量
- MarketGraph 直接读取器：从未使用 HTTP API、reference/ 下断 symlink、直接导入无鉴权
- 密钥暴露：`api_tokens.json` 在 git 中追踪、无盐 SHA256、X-API-Key 双重暴露、`.env.*` 不在 gitignore

**已应用修复（10 项）：**
1. [x] `SharedSignals/.gitignore`：添加 `config/api_tokens.json` + `.env.*`
2. [x] `.env.example`：`SHAREDSIGNALS_ROOT` 修正（MarketGraphRuntime → SharedSignals）
3. [x] `.env.example`：`MARKETGRAPH_ENV_FILE` 修正（MarketGraph/.env → marketgraph/.env）
4. [x] `SharedSignals/tools/api_server.py`：端口默认值 8900 → 8082（docstring + env.get）
5. [x] `shared_signals_api.py`：移除 X-API-Key 双重暴露（服务器仅检查 Authorization）
6. [x] `reader.py`（TradingagentDataReader）：移除 dead `api` property + 未使用的 `import time`
7. [x] `SharedSignals/auth.py`：添加 salt token hashing（PBKDF2-HMAC-SHA256，100k 迭代，向后兼容）
8. [x] `SharedSignals/reader.py`：LRU cache 失效 — 14 个缓存函数已注册，TTL（默认 5 分钟）+ 文件 mtime 自动检测，`clear_caches()` + `/cache/invalidate` + `/cache/status` 端点

### Goal 2 审计 Round 3（高强度终检 — TradingAgent 侧）

**TradingAgent 相关发现（CRITICAL/HIGH）：**
- **MarketGraphCSVReader 路径错误：** `intake` 路径缺少 `data/` 目录，`get_regime()` 路径错误 — 导致体制信号、事件候选、情绪信号三个关键 CSV 静默加载失败（已修复）
- **SharedSignalsAPIClient 孤儿代码：** `shared_signals_api.py` 已定义完整 HTTP 客户端（15 接口），但 `TradingagentDataReader` 从未实例化或使用它 — 所有数据仍走直接 SQLite 读取
- **TradingagentDataReader 无数据新鲜度检查：** `errors`/`stale` 字段只写从未被消费，stale=True 后无任何恢复逻辑
- **N+1 查询扇出：** 评分管线对每只股票做 5-6 次独立查询，20 只股票 > 100 次调用，无批量接口
- **直接 SQLite 读取绕过了 API 鉴权：** TradingAgent 绕开 SharedSignals HTTP API 直接读 SQLite，使得 API token/scope 安全模型形同虚设
- **无死人手刹：** 连续 N 次 SQLite 错误或 CSV 空返回后无告警

**已应用修复（Round 3，影响 TradingAgent）：**
9. [x] `tradingagent/shared/data/reader.py`：MarketGraphCSVReader `intake` 路径从 `self.root / "intake"` → `self.root / "data" / "intake"`，`get_regime()` 路径从 `self.root / "all_weather_regime.csv"` → `self.root / "data" / "all_weather_regime.csv"`

### Goal 2 审计 Round 4（终检 — TradingAgent 侧）

**TradingAgent 相关发现（CRITICAL/HIGH）：**
- **SharedSignalsAPIClient 孤儿代码：** 214 行 HTTP 客户端从未被生产代码导入使用（已加 DeprecationWarning）
- **TradingagentDataReader 无死人手刹：** `errors` 列表无限增长但从不消费，`stale` 标志从未检查 — 已修复：添加 `_maybe_alert()` 每 10 条错误 WARNING
- **SharedSignalsReader 无 SQLite busy_timeout：** 连接无超时，写锁期间读立即失败 — 已修复：添加 `busy_timeout = 5000`
- **静默回退到 `/dev/null/does_not_exist.sqlite`：** 初始化失败时所有查询返回空，零告警 — 已修复：`_maybe_alert()` 在回退激活时日志记录
- **重复的交易日历实现：** `t_plus_1.py` 和 `position_schema.py` 各自独立实现 is_trading_day，行为不一致
- **try/except 将类设为 None 反模式：** `daily_review.py` 和 `benchmark.py` 将导入失败转换为 None，隐藏真实错误
- **N+1 查询扇出：** 评分管线每只股票 5-6 次独立查询，无批量接口
- **数据类型不一致：** CSV 路径返回字符串，SQLite 路径返回正确 Python 类型

**已应用修复（Round 4，影响 TradingAgent）：**
10. [x] `reader.py`：SharedSignalsReader 连接添加 `PRAGMA busy_timeout = 5000`
11. [x] `reader.py`：TradingagentDataReader 添加 `_maybe_alert()` — errors 每 10 条 WARNING，所有 9 个 error.append 点均已接线
12. [x] `shared_signals_api.py`：添加 DeprecationWarning 模块级警告
13. [x] `SharedSignals/reader.py`：`get_market_data()` 查询添加 `market` 过滤（从 ts_code 后缀推导）
14. [x] `SharedSignals/collectors/rss/`：event_hash 从 64 位升级到 128 位（collector.py + gap_filler.py）
15. [x] `SharedSignals/api_server.py`：恢复 `log_message()` HTTP 请求日志 + 500 错误日志

### 2026-07-02 Goal 2 审计 Round 5（五维度最终审计 — 58 发现，23 修复）

**5 新维度并行审计。TradingAgent 相关发现和修复：**

**TradingAgent 相关发现（CRITICAL/HIGH）：**
- **Universe collapse（CRITICAL）：** `adapter.py:_exclude_asset()` 将 `None`（DB 错误）等价于"低流动性" — 所有股票被排除，TradingAgent 生成零交易
- **SQLite 读写模式（HIGH）：** `reader.py` 以 rw 模式打开 SharedSignals 只读模型 DB — 损坏风险，空 DB 静默创建
- **`/dev/null` fallback（HIGH）：** `SharedSignalsReader` 初始化失败静默回退到 `/dev/null/does_not_exist.sqlite` — 完全数据丢失零告警
- **DatabaseError 未捕获（HIGH）：** `_query()` 只捕获 `sqlite3.OperationalError` — DB 损坏绕过防御
- **硬编码 secrets（HIGH）：** `webhook_sender.py` 中 WEBHOOK_SECRET 和 WEBHOOK_URL 为硬编码字面量
- **env 自动加载在 import-time（MEDIUM）：** 多个模块在 import 时 mutate `os.environ`

**已应用修复（Round 5，5 TradingAgent 相关项）：**
1. [x] `Ashare/adapter.py`：`_exclude_asset()` — `amount is None` 返回 False（保留），记录 WARNING
2. [x] `shared/data/reader.py`：SQLite 连接从 rw 改为 `mode=ro` URI
3. [x] `shared/data/reader.py`：`DatabaseError` 和 `OperationalError` 同时捕获
4. [x] `shared/data/reader.py`：`/dev/null` fallback → RuntimeError（fail-fast）
5. [x] `shared/execution/webhook_sender.py`：Hardcoded secrets → env vars

### Goal 1 退役清理

- [x] Ashare 依赖迁移：`execution_router.py` sim_broker 通道从旧 `/opt/investment/Ashare/tools/a_share_simulated_trade_executor` 迁移到 `tradingagent/Ashare/sim_executor.py`
- [x] Tushare API 包装器迁移：`a_share_tushare_api.py` + `a_share_common.py` 已迁至 `/opt/investment/SharedSignals/collectors/tushare/`，服务器保留兼容性 symlink
- [x] Ashare/tools 全面退役：142 文件归档至 `_archive/Ashare_tools_20260702/`，目录仅剩 3 个 compat symlink
- [x] `Ashare/AGENTS.md` 添加迁移注释
- [x] 旧系统残留清理：删除 61 个死 symlink（PM 20 + Crypto 21 + US 20）
- [x] 代码层 Tradings/KimiWork 引用全部修复（0 残留）
- [x] 服务器 crontab 37 条旧注释清理
- [x] 所有修改提交并 push，服务器同步确认
- [x] `sim_broker.py` L8 + `slippage_model.py` L8 注释引用路径更新至 `_archive/Ashare_tools_20260702/`

## 六、7/1 事故复盘（已完成）

以下事项已修复并固化为 [AGENTS.md](AGENTS.md) 中的永久规则：

- 虚假成交确认 → Mini/Hermes 健康门 + 未确认回执 halt
- 过期 pending 清理 → job_self_heal
- 回执指纹闭环 → receipt_sha256 验证
- 影子信号过滤 → 200xxx.SZ 等非普通 A 股代码三层过滤

详细时间线：[docs/runtime_incidents_20260701.md](docs/runtime_incidents_20260701.md)

## 六、关联系统状态

- [SharedSignals STATUS](../SharedSignals/STATUS.md) — 数据采集与存储状态
- [MarketGraph STATUS](../MarketGraph/STATUS.md) — 研究图谱与因果状态
- [Finance STATUS](../STATUS.md) — 根工作区总览
