# TradingAgent 当前状态

Observed at: 2026-09-06 01:33:50 Asia/Shanghai。Simulation only；未触发实盘、新资金或风险扩张。

## 当前源码与配置核验

| 层面 | 核验入口 |
|---|---|
| 本地主线 | 现场读取仓库，不将文档自身的提交写成永久当前值 |
| GitHub 主线 | 刷新远端后与本地分别核对 |

实时源码核验使用 `git rev-parse HEAD origin/main`；源码、安装配置、运行版本和自然回执分别核对。

- G5 日程修正 #655 已合入，配置来源为 `83e5fdd2dc2ff5dd489d10e457008c81b74422f5`；精确主线完整测试及发布包校验通过。已在 01:31:35 安装 `*:2/5:55` 日程，timer 保持 enabled/active、Persistent=false。
- 本次只发布日程配置，未切换整套不可变目录。当前代码 runtime 仍为 `14834d1640ae2b976b0c4a77e19b9ad06bd1c7e7`；已将发布包中 424 个生产 Python 文件与该 runtime 逐文件校验一致。未重启 G5 服务或前端服务；前端 API active，health 与本地页面返回 200。
- 01:32:58 的首次新日程唤醒于 01:33:19 完成：requested_window_consumed=true、idempotent_replay=true、replay_mode=completed_slot_without_fresh_query、processed_cycle_count=0、backlog_remaining=false。该窗口已被上一轮处理，这次未重新查询；不能据此宣称新的 source latency 或稳定性改善。下一次新窗口需要继续按自然回执验证。
- 整套目录切换预检遇到交错运行的观察任务，未发出 GitHub 整套部署请求。四十币观察 timer 曾在 01:26:26 暂停后续唤醒，01:29:30 恢复 enabled/active，并在原定 01:29:45 启动下一轮；没有中断正在执行的观察服务。
- #658 已修正文档检查对旧排版的依赖，候选和精确主线检查均通过。观察日期、现场主线读取和 Crypto 当前/历史边界仍受检查；未增加数据源全局稳定性门禁。Crypto 合同入口见 [Crypto README](Crypto/README.md)。

回退日程时使用发布前保存的原 unit，恢复 `*:0/5:55` 并重载/重启该 timer；代码 runtime 未切换，账本和回执保持不变。运行入口见 [operations](docs/operations.md) 与 [deployment](docs/DEPLOYMENT.md)。

## 历史快照（2026-09-05 17:03，以下不作为本轮重新验收）

### 源码、运行与网页

- 纯文档 CI 优化 #652、私有网页配置 #653 已合入；配置代码基线 `19b8dcc00d455a391444eea7ed496a89fcc790ba`，候选与精确主线 CI 通过。
- 现有不可变运行版本仍为 `14834d1640ae2b976b0c4a77e19b9ad06bd1c7e7`。14 项任务配置与前端实际进程均绑定该版本，health 200、real_trading_enabled=false。未运行的任务不据此宣称执行成功。
- Nginx 静态目录已从旧 `69ca4475` 固定路径改为 `releases/tradingagent/current/front/dist`。本地页面字节与 current 一致，同源快照 200；真实浏览器已加载该页面与快照，显示调度正常/空闲及独立模拟记录，未注入样例收益；三个域名别名的真实非 loopback 请求（含伪造转发头）均由源站返回 403。
- 本轮只更新网页入口配置，没有为纯文档/CI 修改重启整套模拟任务。源码 checkout 与最新 GitHub main 用现场 Git 读回。

### 远程入口仍未完成

`tradingagent.cc` 的 Cloudflare 隧道实际位于新加坡，配置却指向当地未监听的 80/8787；业务源站在广州。已修复源站页面版本和整站本地访问限制，尚未接通新远程链路。

Cloudflare DNS 管理与单用户 Access 配置仍需要控制台登录后完成。远程网页、同源快照、退出/撤销的认证验收尚未通过；本地 200 和匿名拒绝不等于远程个人入口已经恢复。不能将内部 API 直接匿名公开。入口与回退见 [front integration](front/docs/integration.md)。

### 历史运行结果与保留边界

- A股下一开市日的自然执行尚未发生；历史失败不通过 reset-failed 清除。修复/CI 不证明策略有效或允许实盘。
- source stable 不是开发、兼容测试、正常发布、有界试读或只读观察的全局前置条件。研究、策略和执行只拒绝不满足各自合同的具体样本或动作。

### Crypto track

G5 delayed-paper service 的发布配置与实际运行结果分别核验。9 月 5 日 15:01–15:02 的正式验收约 63.23 秒、eligible，learning_timer_enable_authorized=false；这是历史运行结果，未重新触发验收或开启学习定时器。G5 acceptance timer 保持 Persistent=no，下一次计划为 2026-09-06 09:05:32 CST（本轮 timer 读回）。latest continuous 288-bar segment 仅衡量 runtime maturity；历史断点 do not block safe-segment simulation，但不能被解释为策略有效或允许风险扩张。不放宽十币 +55 秒截止或滚动评估样本条件。

### Copilot and paused scopes

TradingCopilot 仍为独立人工辅助边界，不授予自动交易权限。#606/#608/#610/#612 的冻结研究、长期市场工作树、Mini 冻结研究版本及服务器恢复树保留；CNFutures 继续暂停。

### 保留范围与文档入口

Crypto 当前/历史运行边界见 [Crypto README](Crypto/README.md)；本页的带日期读回不替代模块合同，也不证明尚未观察的服务或样本段成功。

规划见 [EVOLUTION_PROGRAM](docs/EVOLUTION_PROGRAM.md)，运行见 [operations](docs/operations.md)，发布见 [DEPLOYMENT](docs/DEPLOYMENT.md)。纯文档快路径不产生 release artifact，不替代运行时发布证据。
