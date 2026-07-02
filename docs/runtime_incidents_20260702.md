# 2026-07-02 运行时事件日志

本文记录 2026-07-02 发生的 Mac Mini / TradingAgent 执行桥路径漂移、服务器数据 reader 回归，以及本次修复后的残余风险。当前权威规则见 [AGENTS.md](../AGENTS.md)，当前状态见 [STATUS.md](../STATUS.md)。

## 事件1: `~/Desktop/Investment` 被旧任务反复创建

**现象：**
- Nicholas 删除 `~/Desktop/Investment` 后，该目录仍会重新出现。
- 复查时目录只剩 `Ashare/outputs/account` 空目录，不是完整开发工作区。

**根因：**
- Mac Mini 上旧 LaunchAgent `ai.hermes.sim-remote-sync` 仍在加载。
- 它调用 `~/.hermes/scripts/sim-remote-sync.sh`，脚本默认 `ASHARE=$HOME/Desktop/Investment/Ashare`，会在没有真实交易 CSV 时仍创建 `outputs/account/remote_sync_state.json` 所在目录。

**修复：**
- 已停止并禁用 `ai.hermes.sim-remote-sync`。
- 原 plist 已改名为 `~/Library/LaunchAgents/ai.hermes.sim-remote-sync.plist.disabled`，备份保留为 `ai.hermes.sim-remote-sync.plist.bak.20260702-210140`。
- 只删除空目录 `~/Desktop/Investment/Ashare/outputs/account` 及其空父目录，未删除任何业务文件。

**验证：**
- `launchctl list | grep ai.hermes.sim-remote-sync` 无加载结果。
- `~/Desktop/Investment` 当前不存在。
- Mac Mini receiver `/health` 返回 `status=ok`、`pending=0`、`halted=false`、`execution_status=ready`。

## 事件2: Mini 执行器回写目录仍默认指向旧 `Tradings`

**现象：**
- Mac Mini live executor 通过 LaunchAgent 设置了 `ASHARE_ROOT=/Users/nicholashan/.hermes/ashare-runtime`，但没有设置服务器回写目录。
- `~/.hermes/scripts/sim-signal-executor.py` 的默认回写路径仍是 `/opt/investment/Tradings/signals`。

**修复：**
- 在 `~/Library/LaunchAgents/com.nicholashan.sim-signal-executor.plist` 中新增：
  - `SIM_REMOTE_TRADINGS_SIGNAL_DIR=/opt/investment/tradingagent/signals`
- 重启 `com.nicholashan.sim-signal-executor`。

**验证：**
- plist 中 `SIM_REMOTE_TRADINGS_SIGNAL_DIR` 已为 `/opt/investment/tradingagent/signals`。
- executor 正常加载。
- receiver `/health` 正常。

## 事件3: 服务器 `TradingagentDataReader` 导入回归

**现象：**
- 服务器 cron 入口 `job_ashare_sim_exec` 在交易时段报错：
  - `ImportError: cannot import name 'TradingagentDataReader' from 'shared.data.reader'`
- 这会导致 A 股模拟执行调度在发单前失败。

**根因：**
- 远端主线与服务器本地版本同时改动 `shared/data/reader.py`。
- 服务器热修先加了兼容 alias，但 GitHub main 上随后已有更完整的 `TradingagentDataReader` 实现。
- 合并后 `shared/data/__init__.py` 仍导出旧名字和不存在的交易日历函数，引发包初始化失败。

**修复：**
- 合并 GitHub main 到服务器 `/opt/investment/tradingagent`。
- 以 GitHub 主线的新版 `TradingagentDataReader` 为准，修复 `shared/data/__init__.py` 只导出当前真实存在的 reader 类。
- 修复 `shared/data/reader.py` 回归：
  - `MarketGraphCSVReader` 兼容 `data/intake` 与历史测试根目录下的 `intake`。
  - `get_regime()` 兼容 `data/all_weather_regime.csv` 与历史测试根目录下的 `all_weather_regime.csv`。
  - intraday 查询的日期型 `end_time` 自动扩展到当天 `T23:59:59`。
  - SQLite 查询失败记录 `last_error`，TradingagentDataReader 读取后设置 `stale=True` 并记录 errors。

**提交：**
- `ea65e11 fix: restore shared data reader exports`
- `3cc087e Merge remote-tracking branch 'origin/main'`
- `bd4a60c fix: align shared data reader package exports`

**验证：**
- 已推送到 GitHub `main`。
- 服务器 `/opt/investment/tradingagent` 与 `origin/main` 同步。
- `PYTHONPATH=. /opt/marketgraph/venv/bin/python3 -m pytest tests/test_data_reader.py -q`：`5 passed`。
- `from shared.wrappers import tradings_cron_entry` 与 `from shared.data.reader import TradingagentDataReader` 均可导入。
- `bash -n shared/wrappers/job_ashare_sim_exec.sh` 通过。

## 事件4: 旧 `condition-cleanup` 仍访问桌面根目录

**现象：**
- 深挖残余风险时发现 `ai.hermes.condition-cleanup` 仍处于加载状态。
- 它调用 `~/.hermes/scripts/condition-cleanup.sh`，脚本 `cd "$HOME/Desktop/Investment"` 后清理旧 `Ashare/data/tradebook/condition_lifecycle_log.jsonl`。
- 新 runtime `~/.hermes/ashare-runtime` 下没有该 lifecycle log。

**处置：**
- 该任务属于旧桌面 A-share tradebook 清理任务，当前没有对应新 runtime 事实源。
- 已停止并禁用 `ai.hermes.condition-cleanup`，避免继续访问已退役桌面路径。
- 原 plist 改名为 `~/Library/LaunchAgents/ai.hermes.condition-cleanup.plist.disabled`，备份为 `ai.hermes.condition-cleanup.plist.bak.20260702-213437`。

**验证：**
- `launchctl list | grep ai.hermes.condition-cleanup` 无加载结果。
- `~/Desktop/Investment` 当前不存在。

## 事件5: `execution_router.py` 迁移改动悬空

**现象：**
- 服务器工作树长期残留未提交改动 `shared/execution/execution_router.py`。
- 该改动将 A 股 `sim_broker` 从旧 `/opt/investment/Ashare/tools/a_share_simulated_trade_executor.py` 切到仓库内 `Ashare/sim_executor.py`。
- 悬空状态会导致运行代码与 GitHub main 不一致，后续 agent 容易误回退或覆盖。

**验证：**
- `tests/test_ashare_sim.py tests/test_sim_loop.py tests/test_t_plus_1_integration.py tests/test_real_money_boundary.py`：`21 passed`。
- 手动 mock route 返回 `channel=sim_broker`、`executed=True`、`status=filled`，证明 router 可以通过内部 A-share sim executor 工作。

**修复：**
- 已单独提交并推送到 GitHub main：
  - `fbc1776 fix: route ashare sim broker through internal executor`

## 事件6: Mini live 脚本默认值仍可回退旧路径

**现象：**
- `com.nicholashan.sim-signal-executor` 的 LaunchAgent 已设置正确环境变量，但 live 脚本自身默认值仍指向旧路径：
  - `ASHARE_ROOT` 默认 `~/Desktop/Investment/Ashare`
  - `SIM_REMOTE_TRADINGS_SIGNAL_DIR` 默认 `/opt/investment/Tradings/signals`
- `~/.hermes/scripts/health-check.sh` 的 pending 统计仍扫描 `/opt/investment/Tradings/signals/pending`。
- 这些默认值在正常 LaunchAgent 环境下被覆盖，但手动运行或环境变量丢失时会回退旧路径。

**修复：**
- Mac Mini `~/.hermes/scripts/sim-signal-executor.py` 默认 runtime 改为 `~/.hermes/ashare-runtime`。
- Mac Mini `~/.hermes/scripts/sim-signal-executor.py` 默认服务器 signals 改为 `/opt/investment/tradingagent/signals`。
- Mac Mini `~/.hermes/scripts/health-check.sh` pending 统计改为 `/opt/investment/tradingagent/signals/pending`。
- 已备份：
  - `sim-signal-executor.py.bak.20260702-215150.path_defaults`
  - `health-check.sh.bak.20260702-215150.path_defaults`

**验证：**
- `python3 -m py_compile ~/.hermes/scripts/sim-signal-executor.py` 通过。
- `bash -n ~/.hermes/scripts/health-check.sh` 通过。
- `com.nicholashan.sim-signal-executor` 已重启。
- `~/.hermes/health/status.json` 显示 `healthy=true`。
- `~/Desktop/Investment` 当前不存在。


## 事件7: 依赖项与参考副本旧路径清理

**排查范围：**
- 服务器 Git 跟踪源码中的旧路径引用。
- Mac Mini 当前加载 LaunchAgent 与 active scripts。
- TradingAgent Python 源码 compileall。
- Mini 参考消费者与 A-share simulated execution 相关测试。

**发现：**
- `mini/README.md` 与 `mini/mini_consumer.py` 仍把旧桌面执行器路径写成默认参考。
- `Ashare/AGENTS.md`、`docs/shadow_market_configs.md`、`shared/execution/sim_broker.py` 仍可能让后续 agent 误以为当前执行入口是旧 `a_share_simulated_trade_executor.py`。
- Mac Mini `health-check.sh` 只剩一行注释提到旧 `/opt/investment/Tradings`。

**修复：**
- `Ashare/AGENTS.md` 改为当前边界：服务器 `Ashare/sim_executor.py` 生成/发送 simulated signal，Mac Mini live executor 负责 UI 执行与回写。
- `mini/README.md` 改为当前 live path：`~/.hermes/scripts/sim-signal-executor.py`；明确 `mini/mini_consumer.py` 只是历史参考/测试兼容。
- `mini/mini_consumer.py` 移除旧桌面默认 executor path，避免误运行时指向 `~/Desktop/Investment`。
- `docs/shadow_market_configs.md` 与 `shared/execution/sim_broker.py` 更新为 `Ashare/sim_executor.py` / Mini receiver-executor 链路。
- Mac Mini `health-check.sh` 注释清理，不再出现旧 `/opt/investment/Tradings`。

**验证：**
- `tests/test_mini_consumer.py tests/test_ashare_sim.py tests/test_sim_loop.py tests/test_t_plus_1_integration.py tests/test_real_money_boundary.py tests/test_data_reader.py`：`30 passed`。
- `python -m compileall -q shared Ashare PM Crypto US HK mini tests` 通过。
- Mac Mini active scripts 旧路径审计无命中。
- Mac Mini `~/.hermes/health/status.json` 显示 `healthy=true`。


## 当前正确边界

- 代码主线：GitHub `NicholasHan1226/Tradingagent.cc`，服务器路径 `/opt/investment/tradingagent`。
- Mac Mini live runtime：`~/.hermes/ashare-runtime`。
- Mac Mini live executor / receiver：`~/.hermes/scripts/sim-signal-executor.py`、`~/.hermes/scripts/sim-signal-receiver.py`。
- 服务器执行事实写入：`/opt/investment/tradingagent/signals/{pending,filled,failed,positions,...}`。
- `~/Desktop/Investment` 不再是 active dev root，也不再是 live runtime root；除非复盘历史文件，不得新增自动任务依赖它。

## 残余风险

- Mac Mini `~/.hermes/scripts/` 中仍有若干历史脚本含 `~/Desktop/Investment`，但当前未由 LaunchAgent 或 crontab 激活。不要批量改写，除非先确认每个脚本的事实源和新 runtime 对应关系。
- 本次未发送测试交易信号；A 股模拟执行链路的下一次完整端到端验证需要在交易时段、且遵守 mini health gate 与模拟账户确认规则。
